"""Real channel HTTP concurrency and context tests, with no database/provider."""
import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import threading
import types
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from http_test_support import ROOT, channel_http, endpoint, jobs_stub, library_stub
from hot_storage import capture_read_lifetime, register_read_deadline
from self_hosted_configuration import STARTER_CHANNEL_URLS
from web.constants import VISIBLE_CHANNEL_FIELDS
from web.models import ChannelRequest, InitialHydrationRequest
from starlette.responses import Response


class ChannelAddRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.replace('web.channels.ChannelCommandRepository', return_value=types.SimpleNamespace())
        self.replace('web.channels.ChannelTrashRepository', return_value=types.SimpleNamespace())

    def replace(self, target, *args, **kwargs):
        mock = patch(target, *args, **kwargs)
        result = mock.start(); self.addCleanup(mock.stop)
        return result

    async def test_bounded_adds_leave_event_loop_responsive_and_copy_context(self):
        context = ContextVar('request_source', default='missing')
        release = threading.Event()
        lock = threading.Lock()
        active = maximum = 0
        started = []
        def work(url, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1; maximum = max(maximum, active); started.append(url)
            try:
                if not release.wait(2): raise AssertionError('event loop could not release blocking work')
                return context.get()
            finally:
                with lock: active -= 1
        self.replace('web.channels.channel_service.add_channel', side_effect=work)
        add = endpoint(channel_http().add_router, 'add_channel')
        token = context.set('source-context')
        tasks = [asyncio.create_task(add(ChannelRequest(url=str(i)), None)) for i in range(5)]
        try:
            for _ in range(100):
                if len(started) == 2: break
                await asyncio.sleep(.005)
            self.assertEqual(len(started), 2)
            self.assertFalse(any(t.done() for t in tasks))
        finally: release.set()
        try:
            self.assertEqual(await asyncio.gather(*tasks), ['source-context'] * 5)
            self.assertEqual(maximum, 2)
        finally: context.reset(token)

    async def test_starter_pool_allows_five_while_ordinary_pool_keeps_two(self):
        release = threading.Event()
        started = {'starter': 0, 'ordinary': 0}
        lock = threading.Lock()
        starter_url = next(iter(STARTER_CHANNEL_URLS))
        def work(url, **kwargs):
            with lock: started['starter' if url == starter_url else 'ordinary'] += 1
            if not release.wait(2): raise AssertionError('blocked test did not release')
            return {'status': 'added'}
        self.replace('web.channels.channel_service.add_channel', side_effect=work)
        self.replace('self_hosted_onboarding.get_manager', return_value=types.SimpleNamespace(
            begin_starter=lambda url: None, record_starter=lambda url, result: None))
        http = channel_http()
        starter = endpoint(http.add_router, 'add_starter_channel')
        ordinary = endpoint(http.add_router, 'add_channel')
        tasks = [asyncio.create_task(starter(ChannelRequest(url=starter_url), None)) for _ in range(6)]
        tasks += [asyncio.create_task(ordinary(ChannelRequest(url='@ordinary'), None)) for _ in range(3)]
        try:
            for _ in range(100):
                if started == {'starter': 5, 'ordinary': 2}: break
                await asyncio.sleep(.005)
            self.assertEqual(started, {'starter': 5, 'ordinary': 2})
        finally:
            release.set()
            await asyncio.gather(*tasks)

    async def test_starter_route_only_accepts_the_displayed_suggestions(self):
        import re
        html = (ROOT / 'application/self_hosted_ui/setup.html').read_text(encoding='utf-8')
        urls = set(re.findall(r'name="starter-channel" type="checkbox" value="([^"]+)"', html))
        self.assertEqual(urls, STARTER_CHANNEL_URLS)
        self.assertEqual(len(urls), 5)
        added, recorded = [], []
        self.replace('web.channels.channel_service.add_channel', side_effect=lambda url, **kw: added.append(url) or {'status': 'added'})
        self.replace('self_hosted_onboarding.get_manager', return_value=types.SimpleNamespace(
            begin_starter=lambda url: recorded.append(url), record_starter=lambda url, result: None))
        starter = endpoint(channel_http().add_router, 'add_starter_channel')
        for url in urls: await starter(ChannelRequest(url=url), None)
        self.assertEqual(set(recorded), urls)
        self.assertEqual(set(added), urls)
        with self.assertRaises(HTTPException) as error:
            await starter(ChannelRequest(url='https://www.youtube.com/@other'), None)
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(len(added), 5)

    async def test_two_aliases_create_one_channel_and_one_inventory_estimate(self):
        barrier = threading.Barrier(2, timeout=2)
        rows, inserts, estimates = {}, [], []
        channel_id = 'UC' + 'a' * 22
        self_test = self
        class Repository:
            def __init__(self, database): pass
            def load_channel_record(self, value): return rows.get(value)
            def preserve_user_input(self, value, locator): pass
            def insert_channel(self, row, *, permission):
                self_test.assertNotIn(row['channel_id'], rows)
                self_test.assertEqual(permission, 'accepted')
                inserts.append(row)
                canonical = {**row, 'collection_permission': permission,
                             'collection_control_epoch': 7, 'collection_mode': 'initial_inventory'}
                rows[row['channel_id']] = canonical
                return canonical
        def provider(*args, **kwargs):
            barrier.wait()
            return types.SimpleNamespace(status_code=200, json=lambda: {'items': [{'id': channel_id, 'statistics': {}, 'snippet': {}}]})
        def estimate(value, **kwargs):
            estimates.append(value)
            return {'scope': 'all', 'video_types': ['long', 'short', 'stream'],
                    'counts': {'long': 10, 'short': 0, 'stream': 0},
                    'statuses': {'long': 'ok', 'short': 'ok', 'stream': 'ok'}}
        self.replace('web.channels.FrontendRepository').return_value.load_channel.side_effect = lambda key: [dict(rows[key])]
        self.replace('web.channels.ChannelCommandRepository', Repository)
        self.replace('web.channels.youtube_api_get', side_effect=provider)
        self.replace('web.channels.channel_sources.estimate_initial_inventory', side_effect=estimate)
        self.replace('web.channels.collection_api.channel_metadata_update_values', return_value={'title': 'Test'})
        http = channel_http(jobs=jobs_stub(_prepare_channel_add_hydration=lambda *a, **kw: {'queued': True}))
        add = endpoint(http.add_router, 'add_channel')
        results = await asyncio.gather(*(add(ChannelRequest(url=handle), None) for handle in ('@first-alias', '@second-alias')))
        self.assertEqual(sorted(r['status'] for r in results), ['added', 'exists'])
        self.assertEqual(len(inserts), 1)
        self.assertEqual(estimates, [channel_id])
        self.assertEqual({tuple(r['channel']) for r in results}, {VISIBLE_CHANNEL_FIELDS})
        self.assertIn('initial_inventory_estimate', inserts[0])

    async def test_prepare_only_stays_read_only(self):
        seen = []
        row = {'channel_id': 'UC1', 'internal_restore_field': 'preserved'}
        def prepare(url, **kwargs):
            seen.append(kwargs['prepare_only'])
            return {'status': 'prepared', 'channel': row}
        self.replace('web.channels.channel_service.add_channel', side_effect=prepare)
        result = await channel_http().resolve_user_work_channel('@test', None)
        self.assertEqual(result, {'status': 'prepared', 'channel': row})
        self.assertEqual(seen, [True])

    async def test_added_response_rereads_channel_and_nested_hydration(self):
        channel_id = 'UC' + 'a' * 22
        receipt = {'status': 'added', 'title': 'Raw title', 'channel': {'channel_id': channel_id},
                   'hydration': {'job': {'summary': {'raw': 'provider data'}}}}
        self.replace('web.channels.channel_service.add_channel', return_value=receipt)
        self.replace('web.channels.FrontendRepository').return_value.load_channel.return_value = [
            {'channel_id': channel_id, 'title': 'Admitted title'}]
        current = {'job': {'summary': {'safe': 'reader data'}}}
        http = channel_http(jobs=jobs_stub(_load_initial_hydration_state=lambda row: current))
        result = await endpoint(http.add_router, 'add_channel')(ChannelRequest(url=channel_id), None)
        self.assertEqual(result['title'], 'Admitted title')
        self.assertEqual(result['hydration'], current)
        self.assertEqual(result['status'], 'added')

    async def test_post_add_read_failure_does_not_lose_confirmed_channel(self):
        channel_id = 'UC' + 'a' * 22
        receipt = {'status': 'added', 'title': 'Raw title', 'channel': {'channel_id': channel_id},
                   'hydration': {'job': {'summary': {'raw': 'provider data'}}}}
        write = self.replace('web.channels.channel_service.add_channel', return_value=receipt)
        self.replace('web.channels.FrontendRepository').return_value.load_channel.side_effect = RuntimeError('read failed')
        result = await endpoint(channel_http().add_router, 'add_channel')(ChannelRequest(url=channel_id), None)
        self.assertEqual(result['status'], 'added')
        self.assertEqual(result['channel']['channel_id'], channel_id)
        self.assertIsNone(result['title'])
        self.assertNotIn('provider data', str(result))
        self.assertIsNone(result['hydration']['job'])
        write.assert_called_once()

    async def test_optional_add_rereads_preserve_only_actually_used_deadlines(self):
        near = datetime.now(timezone.utc) + timedelta(seconds=1)
        channel_deadline = near + timedelta(minutes=20)
        hydration_deadline = near + timedelta(minutes=30)
        other_used = near + timedelta(hours=1)
        receipt = {'status': 'added', 'channel': {'channel_id': 'UC1'},
                   'hydration': {'provider': 'must not be reused'}}
        write = self.replace('web.channels.channel_service.add_channel', return_value=receipt)
        repository = self.replace('web.channels.FrontendRepository').return_value
        for channel_ok, hydration_ok in ((True, False), (False, True), (True, True), (False, False)):
            with self.subTest(channel_ok=channel_ok, hydration_ok=hydration_ok):
                def read_channel(_):
                    register_read_deadline(channel_deadline if channel_ok else near)
                    if not channel_ok:
                        raise RuntimeError('channel read failed after admission')
                    return [{'channel_id': 'UC1', 'title': 'Admitted title'}]
                def read_hydration(_):
                    register_read_deadline(hydration_deadline if hydration_ok else near)
                    if not hydration_ok:
                        raise RuntimeError('hydration read failed after admission')
                    return {'status': 'ready', 'job': None}
                repository.load_channel.side_effect = read_channel
                http = channel_http(jobs=jobs_stub(_load_initial_hydration_state=read_hydration))
                with capture_read_lifetime() as response:
                    register_read_deadline(other_used)
                    result = await endpoint(http.add_router, 'add_channel')(ChannelRequest(url='@test'), None)
                expected = min([other_used] + ([channel_deadline] if channel_ok else [])
                               + ([hydration_deadline] if hydration_ok else []))
                self.assertEqual(response.expires_at, expected)
                self.assertGreater(response.expires_at, near + timedelta(milliseconds=1))
                self.assertEqual(result['status'], 'added')
                self.assertNotIn('must not be reused', repr(result))
                self.assertEqual(result['title'], 'Admitted title' if channel_ok else None)
                self.assertEqual(result['hydration']['status'], 'ready' if hydration_ok else 'error')
        self.assertEqual(write.call_count, 4)

    async def test_failed_hydration_display_estimate_keeps_the_used_job_deadline(self):
        near = datetime.now(timezone.utc) + timedelta(seconds=1)
        used = near + timedelta(hours=1)
        channel = {'channel_id': 'UC1', 'collection_control_epoch': 1,
                   'initial_long_inventory_status': 'pending'}
        job = {'id': 'f8100000-0000-4000-8000-000000000001', 'channel_id': 'UC1',
               'job_type': 'channel_initial_long_inventory', 'status': 'queued',
               'phase': 'inventory', 'accepted_epoch': 1, 'summary_json': {}}
        def read_job(*args, **kwargs):
            register_read_deadline(used)
            return job
        def failed_estimate(_):
            register_read_deadline(near)
            raise RuntimeError('display estimate failed after partial read')
        self.replace('web.channels.operator_jobs_mode', return_value='durable')
        confirm = self.replace('web.channels.collection_api.confirm_initial_hydration', return_value=(None, job))
        self.replace('web.channels.get_operator_job_by_id', side_effect=read_job)
        http = channel_http(library=library_stub(load_visible_channel_or_404=lambda _: channel),
            jobs=jobs_stub(_prepare_initial_typed_inventory=lambda _: None,
                _with_initial_format_readiness=lambda channel, payload: payload,
                operator_job_repository=lambda: types.SimpleNamespace(estimate_initial_inventory=failed_estimate)))
        with capture_read_lifetime() as response:
            result = endpoint(http.hydration_router, 'enqueue_channel_hydration')(
                'UC1', InitialHydrationRequest(expected_items=None, expected_pages=None,
                    expected_quota_units=None, confirmed=True), None, Response())
        self.assertEqual(response.expires_at, used)
        self.assertGreater(response.expires_at, near + timedelta(milliseconds=1))
        self.assertEqual(result['job']['job_id'], job['id'])
        self.assertIsNone(result['estimate'])
        confirm.assert_called_once()


class TypedRepositoryTimingTests(unittest.TestCase):
    def test_disabled_prepare_never_constructs_repository(self):
        from collection.commands import prepare_initial_typed_inventory
        calls = []
        def factory():
            calls.append('constructed')
            raise AssertionError('disabled prepare must not construct a repository')
        prepare_initial_typed_inventory(
            'UC' + 'a' * 22, get_typed_repository=factory,
            typed_inventory_enabled=False,
        )
        self.assertEqual(calls, [])

    def test_frozen_job_read_never_constructs_inventory_repository(self):
        from collection.hydration import with_initial_format_readiness
        for status in ('queued', 'running'):
            for phase in ('inventory', 'hydrate_videos', 'verify'):
                for enabled in (False, True):
                    with self.subTest(status=status, phase=phase, typed_enabled=enabled):
                        calls = []
                        def factory():
                            calls.append('constructed')
                            raise AssertionError('frozen workset must not read current inventory')
                        # Inventory has no frozen summary yet. Later phases use
                        # the job's counts even when current typed lists are pending.
                        summary = {} if phase == 'inventory' else {'per_format': {
                            kind: {'total': 3, 'updated': 3 if phase == 'verify' else 1,
                                   'unavailable': 0, 'checked': 0,
                                   'pending': 0 if phase == 'verify' else 2, 'errors': 0,
                                   'inventory_ready': True,
                                   'initial_check_complete': phase == 'verify',
                                   'current_profile_ready': phase == 'verify'}
                            for kind in ('long', 'short', 'stream')
                        }}
                        payload = {'status': status, 'phase': phase,
                                   'job': {'initial_video_types': ['long', 'short', 'stream'],
                                           'initial_scope_version': 1, 'phase': phase,
                                           'status': status, 'summary': summary}}
                        result = with_initial_format_readiness(
                            {'channel_id': 'UC' + 'a' * 22}, payload,
                            get_typed_repository=factory, typed_inventory_enabled=enabled,
                        )
                        self.assertEqual(calls, [])
                        self.assertEqual(result['typed_coverage_scope'], 'frozen_initial_job')
                        self.assertIsNone(result['format_readiness_error'])
                        self.assertEqual((result['status'], result['phase']), (status, phase))
                        # Ready format counts cannot bypass final job verification.
                        self.assertFalse(result['current_profile_ready'])
                        self.assertFalse(result['all_initial_checks_complete'])
                        if phase != 'inventory':
                            for kind in ('long', 'short', 'stream'):
                                self.assertEqual(result['formats'][kind]['total'], 3)
                                self.assertEqual(result['formats'][kind]['pending'],
                                                 0 if phase == 'verify' else 2)

    def test_legacy_and_missing_jobs_still_use_current_inventory_read(self):
        from collection.hydration import with_initial_format_readiness
        channel_id = 'UC' + 'a' * 22
        for job in (None, {'initial_video_types': ['long'], 'phase': 'inventory', 'status': 'queued'}):
            for enabled in (False, True):
                with self.subTest(job_present=job is not None, typed_enabled=enabled):
                    calls = []
                    def read(requested_channel):
                        calls.append(requested_channel)
                        return None
                    result = with_initial_format_readiness(
                        {'channel_id': channel_id}, {'job': job, 'status': 'queued'},
                        get_typed_repository=lambda: types.SimpleNamespace(load_channel_initial_hydration=read),
                        typed_inventory_enabled=enabled,
                    )
                    self.assertEqual(calls, [channel_id])
                    self.assertNotEqual(result['typed_coverage_scope'], 'frozen_initial_job')

    def test_repository_construction_failure_keeps_original_fail_soft_boundary(self):
        from collection.commands import prepare_initial_typed_inventory
        from collection.hydration import with_initial_format_readiness
        def factory():
            raise RuntimeError('fixture repository unavailable')
        with patch('builtins.print'), patch('collection.commands.source_log_message', return_value='fixture'):
            prepare_initial_typed_inventory(
                'UC' + 'a' * 22, get_typed_repository=factory,
                typed_inventory_enabled=True,
            )
        result = with_initial_format_readiness(
            {'channel_id': 'UC' + 'a' * 22}, {'current_profile_ready': True},
            get_typed_repository=factory, typed_inventory_enabled=True,
        )
        self.assertTrue(result['current_profile_ready'])
        self.assertEqual(result['format_readiness_error'], 'RuntimeError')


if __name__ == '__main__':
    unittest.main()
