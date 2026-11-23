"""Owner actions cannot drain the YouTube quota. Offline: no server, database or provider."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import quota_guards
from quota_guards import ChannelLookupCache, RateLimiter, backfill_cooldown_seconds, wait_text
from fastapi import HTTPException
from unittest.mock import patch
from http_test_support import channel_http, endpoint, jobs_stub, library_stub
from web.models import ChannelRequest
from channels.sources import CHANNEL_LOOKUP_UNAVAILABLE

CHANNEL = 'UC' + 'a' * 22


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LookupCacheTests(unittest.TestCase):
    def test_found_and_missing_answers_expire_separately(self):
        clock = Clock()
        cache = ChannelLookupCache(found_ttl=100, not_found_ttl=10, clock=clock)
        cache.put('handle', '@Found', CHANNEL)
        cache.put('handle', '@missing', None)
        self.assertEqual(cache.get('handle', '@found'), (True, CHANNEL))
        self.assertEqual(cache.get('handle', '@missing'), (True, None))
        clock.now += 10
        self.assertEqual(cache.get('handle', '@missing'), (False, None))
        self.assertEqual(cache.get('handle', '@found'), (True, CHANNEL))
        clock.now += 90
        self.assertEqual(cache.get('handle', '@found'), (False, None))

    def test_ids_are_case_sensitive_and_size_is_bounded(self):
        cache = ChannelLookupCache(max_entries=2, clock=Clock())
        cache.put('video', 'abcDEF12345', CHANNEL)
        self.assertEqual(cache.get('video', 'ABCdef12345'), (False, None))
        cache.put('video', 'second00000', CHANNEL)
        cache.put('video', 'third000000', CHANNEL)
        self.assertEqual(cache.get('video', 'abcDEF12345'), (False, None))
        self.assertEqual(cache.get('video', 'third000000'), (True, CHANNEL))


class RateLimiterTests(unittest.TestCase):
    def test_every_window_is_enforced(self):
        clock = Clock()
        limiter = RateLimiter(((60, 2), (3600, 3)), clock=clock)
        self.assertEqual([limiter.acquire(), limiter.acquire()], [0, 0])
        self.assertEqual(limiter.acquire(), 60)
        clock.now += 60
        self.assertEqual(limiter.acquire(), 0)
        self.assertEqual(limiter.acquire(), 3540)  # the hourly window is full
        clock.now += 3540
        self.assertEqual(limiter.acquire(), 0)

    def test_refused_calls_are_not_counted(self):
        clock = Clock()
        limiter = RateLimiter(((60, 1),), clock=clock)
        limiter.acquire()
        for _ in range(5):
            self.assertTrue(limiter.acquire())
        clock.now += 60
        self.assertEqual(limiter.acquire(), 0)

    def test_the_route_limits_leave_manual_use_alone(self):
        self.assertEqual(quota_guards.CHANNEL_ADD_WINDOWS, ((60, 20), (86400, 300)))
        self.assertEqual([wait_text(30), wait_text(61), wait_text(5400)], ['1 minute', '2 minutes', '2 hours'])


class BackfillCooldownTests(unittest.TestCase):
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)

    def job(self, status='completed', targets=10, hours_ago=1.0):
        finished = self.now - timedelta(hours=hours_ago)
        # PostgreSQL trims trailing fractional zeros.
        return {'status': status, 'target_count': targets, 'finished_at': finished.isoformat().replace('+00:00', '.12+00:00')}

    def test_only_a_completed_job_with_videos_waits(self):
        cooldown = quota_guards.CURRENT_STATS_BACKFILL_COOLDOWN_SECONDS
        self.assertEqual(cooldown, 6 * 3600)
        # Finished 0.12 s after the hour, so the remaining wait rounds up.
        self.assertEqual(backfill_cooldown_seconds(self.job(), now=self.now), 5 * 3600 + 1)
        self.assertEqual(backfill_cooldown_seconds(self.job(hours_ago=6.1), now=self.now), 0)
        for job in (None, self.job(status='failed'), self.job(status='running'), self.job(targets=0)):
            self.assertEqual(backfill_cooldown_seconds(job, now=self.now), 0)
        self.assertEqual(backfill_cooldown_seconds(self.job(hours_ago=-2), now=self.now), cooldown)

    def test_an_unreadable_finish_time_is_an_error(self):
        with self.assertRaises(ValueError):
            backfill_cooldown_seconds({'status': 'completed', 'target_count': 1, 'finished_at': None}, now=self.now)


class Provider:
    """Stands in for youtube_api_get; records each paid request."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.channels = {'@known': CHANNEL, CHANNEL: CHANNEL}
        self.videos = {'abcdefghijk': CHANNEL}

    def __call__(self, endpoint, *, params, **kwargs):
        self.calls.append((endpoint, params.get('forHandle') or params.get('id')))
        if endpoint == 'videos':
            found = self.videos.get(params['id'])
            items = [{'snippet': {'channelId': found}}] if found else []
        else:
            key = params.get('id') or params['forHandle'].casefold()
            found = self.channels.get(key)
            items = [{'id': found, 'statistics': {}, 'snippet': {}}] if found else []
        body = {'items': items} if self.status == 200 else {'error': {'code': self.status}}
        return types.SimpleNamespace(status_code=self.status, json=lambda: body)


class AddChannelLookupTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.rows = {}
        self.catalogue = {}
        rows, catalogue = self.rows, self.catalogue
        self.expected_permission = 'accepted'
        self_test = self

        class Repository:
            def __init__(self, db): pass
            def load_channel_record(self, value): return rows.get(value)
            def load_video_channel_id(self, video_id): return catalogue.get(video_id)
            def preserve_user_input(self, value, locator): pass
            def insert_channel(self, row, *, permission):
                self_test.assertEqual(permission, self_test.expected_permission)
                self_test.assertEqual(row['initial_long_inventory_status'],
                                      'pending' if permission == 'accepted' else 'manual_required')
                canonical = {**row, 'collection_permission': permission,
                             'collection_control_epoch': 7,
                             'collection_mode': 'initial_inventory' if permission == 'accepted' else 'operator_hold'}
                rows[row['channel_id']] = canonical
                return canonical

        def replace(target, *args, **kwargs):
            mock = patch(target, *args, **kwargs)
            value = mock.start(); self.addCleanup(mock.stop)
            return value
        replace('web.channels.FrontendRepository').return_value.load_channel.side_effect = lambda key: [dict(rows[key])]
        replace('web.channels.ChannelCommandRepository', Repository)
        replace('web.channels.ChannelTrashRepository', return_value=types.SimpleNamespace())
        replace('web.channels.youtube_api_get', self.provider)
        self.estimate = replace('web.channels.channel_sources.estimate_initial_inventory', return_value={
            'scope': 'all', 'video_types': ['long', 'short', 'stream'],
            'counts': {'long': 1, 'short': 0, 'stream': 0},
            'statuses': {'long': 'ok', 'short': 'ok', 'stream': 'ok'}})
        replace('web.channels.collection_api.channel_metadata_update_values', return_value={'title': 'Known'})
        self.http = channel_http(jobs=jobs_stub(_prepare_channel_add_hydration=lambda *a, **k: {'queued': True}))
        self.add_route = endpoint(self.http.add_router, 'add_channel')

    def add(self, url):
        return asyncio.run(self.add_route(ChannelRequest(url=url), None))

    def test_a_tracked_handle_is_resolved_once(self):
        added = self.add('@known')
        self.assertEqual(added['status'], 'added')
        self.assertEqual(added['channel']['channel_id'], self.rows[CHANNEL]['channel_id'])
        self.assertEqual(self.rows[CHANNEL]['collection_permission'], 'accepted')
        self.assertEqual(self.add('https://www.youtube.com/@KNOWN/videos')['status'], 'exists')
        self.assertEqual(self.provider.calls, [('channels', '@known')])

    def test_inventory_above_the_automatic_limit_requires_manual_permission(self):
        from operator_jobs import INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT
        self.expected_permission = 'manual_required'
        self.estimate.return_value = {
            'scope': 'all', 'video_types': ['long', 'short', 'stream'],
            'counts': {'long': INITIAL_CHANNEL_HYDRATION_AUTO_LIMIT + 1, 'short': 0, 'stream': 0},
            'statuses': {'long': 'ok', 'short': 'ok', 'stream': 'ok'}}
        added = self.add('@known')
        self.assertEqual(added['status'], 'added')
        self.assertEqual(added['channel']['channel_id'], self.rows[CHANNEL]['channel_id'])
        self.assertEqual(self.rows[CHANNEL]['collection_permission'], 'manual_required')
        self.assertEqual(self.rows[CHANNEL]['collection_mode'], 'operator_hold')
        self.assertEqual(added['channel']['initial_long_inventory_status'], 'manual_required')
        self.assertEqual(self.provider.calls, [('channels', '@known')])

    def test_a_missing_handle_is_remembered(self):
        for _ in range(3):
            self.assertEqual(self.add('@nobody'), {'error': 'Channel not found'})
        self.assertEqual(self.provider.calls, [('channels', '@nobody')])

    def test_a_missing_channel_id_is_remembered(self):
        missing = 'UC' + 'b' * 22
        for _ in range(2):
            self.assertEqual(self.add(missing), {'error': 'Channel not found'})
        self.assertEqual(self.provider.calls, [('channels', missing)])

    def test_text_that_is_no_locator_asks_for_one_without_a_request(self):
        for text in ('hello world', 'https://example.com/channel', 'https://youtube.com/playlist?list=PL1'):
            self.assertEqual(self.add(text), {'error': 'Enter a YouTube channel link, @handle or channel ID.',
                                              'code': 'user_channel_input_invalid'}, text)
        self.assertEqual(self.provider.calls, [])

    def test_a_video_link_is_resolved_once(self):
        self.assertEqual(self.add('https://youtu.be/abcdefghijk')['status'], 'added')
        self.assertEqual(self.add('https://www.youtube.com/watch?v=abcdefghijk')['status'], 'exists')
        self.assertEqual(self.provider.calls, [('videos', 'abcdefghijk'), ('channels', CHANNEL)])

    def test_legacy_channel_links_have_specific_guidance_without_lookup(self):
        for text in ('https://youtube.com/c/example', 'www.youtube.com/user/example/videos',
                     'https://m.youtube.com/%63/example?view=0'):
            result = self.add(text)
            self.assertEqual(result['code'], 'unsupported_legacy_channel_link')
            self.assertIn('@handle', result['error'])
            self.assertIn('youtube.com/channel/', result['error'])
        self.assertEqual(self.provider.calls, [])

    def test_a_catalogued_video_names_its_channel_without_a_request(self):
        self.add('@known')
        self.catalogue['abcdefghijk'] = CHANNEL
        self.assertEqual(self.add('https://youtu.be/abcdefghijk')['status'], 'exists')
        self.assertEqual(self.add('https://www.youtube.com/shorts/abcdefghijk')['status'], 'exists')
        self.assertEqual(self.provider.calls, [('channels', '@known')])

    def test_a_provider_error_is_reported_and_not_remembered(self):
        unavailable = CHANNEL_LOOKUP_UNAVAILABLE
        self.provider.status = 403
        self.assertEqual(self.add('@known'), unavailable)
        self.assertEqual(self.add('https://youtu.be/abcdefghijk'), unavailable)
        self.provider.status = 200
        self.assertEqual(self.add('@known')['status'], 'added')
        self.assertEqual(self.provider.calls, [('channels', '@known'), ('videos', 'abcdefghijk'), ('channels', '@known')])

    def test_restore_preparation_reads_the_provider_only_for_a_new_channel(self):
        self.add('@known')
        # A tracked channel: the restore item merges into it without provider data.
        prepared = asyncio.run(self.http.resolve_user_work_channel('@known', None))
        self.assertEqual(prepared, {'status': 'prepared', 'channel': {'channel_id': CHANNEL}})
        self.assertEqual(self.provider.calls, [('channels', '@known')])
        # A remembered handle whose channel is no longer tracked is read again.
        self.rows.clear()
        prepared = asyncio.run(self.http.resolve_user_work_channel('@known', None))
        self.assertEqual(prepared['status'], 'prepared')
        self.assertEqual(self.provider.calls, [('channels', '@known'), ('channels', CHANNEL)])


class AddRouteLimitTests(unittest.TestCase):
    def test_excess_adds_are_refused_before_any_lookup(self):
        clock = Clock()
        added = []

        def add_channel(url, **kwargs):
            added.append(url)
            return {'status': 'added'}
        add = endpoint(channel_http(channel_add_admission=RateLimiter(((60, 2),), clock=clock)).add_router,
                       'add_channel')
        request = ChannelRequest(url='@known')
        with patch('web.channels.ChannelCommandRepository'), patch('web.channels.ChannelTrashRepository'), \
             patch('web.channels.channel_service.add_channel', side_effect=add_channel):
            for _ in range(2): asyncio.run(add(request, None))
            with self.assertRaises(HTTPException) as refused: asyncio.run(add(request, None))
            self.assertEqual(refused.exception.status_code, 429)
            self.assertEqual(refused.exception.headers, {'Retry-After': '60'})
            self.assertEqual(refused.exception.detail['code'], 'channel_add_rate_limited')
            self.assertEqual(len(added), 2)
            clock.now += 60
            asyncio.run(add(request, None))
            self.assertEqual(len(added), 3)


class BackfillRouteCooldownTests(unittest.TestCase):
    def run_route(self, latest):
        enqueued = []

        class Repository:
            def get_channel_job(self, channel_id):
                if isinstance(latest, Exception):
                    raise latest
                return latest

            def enqueue_current_stats(self, channel_id, **limits):
                assert limits == dict(expected_epoch=7, max_items=None, max_quota_units=10, max_target_attempts=3)
                enqueued.append(channel_id)
                return {'created': True, 'job': {'status': 'queued'}}

        http = channel_http(
            library=library_stub(load_visible_channel_or_404=lambda id: {'channel_id': id, 'collection_control_epoch': 7}),
            jobs=jobs_stub(operator_job_repository=Repository, _request_operator_job_worker_wake=lambda id: None))
        start = endpoint(http.backfill_router, 'start_channel_backfill_current_stats')
        with patch('web.channels.operator_jobs_mode', return_value='durable'), \
             patch('web.channels.channel_sync_eligibility', return_value=types.SimpleNamespace(allow_current_stats_job=True)), \
             patch('web.channels.load_operator_job_settings', return_value=types.SimpleNamespace(max_quota_units=10, max_target_attempts=3)), \
             patch('web.channels.operator_job_snapshot', side_effect=lambda row, **kw: dict(row)):
            try: return start(CHANNEL, None), enqueued
            except HTTPException as error: return error, enqueued

    def finished(self, hours_ago, status='completed'):
        at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return {'status': status, 'target_count': 120, 'finished_at': at.isoformat()}

    def test_a_recent_completed_backfill_waits(self):
        refused, enqueued = self.run_route(self.finished(1))
        self.assertEqual((refused.status_code, refused.detail['code'], enqueued), (429, 'current_stats_backfill_cooldown', []))
        self.assertLessEqual(int(refused.headers['Retry-After']), 5 * 3600)

    def test_older_failed_or_running_jobs_do_not_wait(self):
        for latest in (None, self.finished(7), self.finished(1, status='failed'), {'status': 'running', 'target_count': 120}):
            result, enqueued = self.run_route(latest)
            self.assertEqual((result['status'], enqueued), ('queued', [CHANNEL]))

    def test_an_unreadable_job_status_refuses_instead_of_spending(self):
        refused, enqueued = self.run_route(RuntimeError('database unavailable'))
        self.assertEqual((refused.status_code, enqueued), (503, []))


class PackagingTests(unittest.TestCase):
    def test_the_module_ships(self):
        files = (ROOT / 'packaging/files.json').read_text(encoding='utf-8')
        self.assertIn('"application/quota_guards.py": "application/quota_guards.py"', files)


if __name__ == '__main__':
    unittest.main()
