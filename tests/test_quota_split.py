"""An exhausted bucket stops only its own work.

One test per row of the table in docs/CONFIGURATION.md "YouTube API quota":
cause -> which steps of the pass ran and which quota bucket was called. Exercises the
VideoCollectionService with synthetic General and Batch transports inside the
actual automatic pass. No database, network or real key.
"""
from functools import partial
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
import io
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).parent)]
import requests
import collection_shutdown
import provider_runtime
import quota_buckets
import youtube_batch_stats
import youtube_client
import youtube_errors
from collection import cycle
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.common_feeder import CommonAutoFeeder
from collection.cycle import AutomaticDispatchWindow
from collection.dispatch import Coordinator
from collection.provider_steps import ProviderStep
from collection_outcome import collection_stop_reason, exhausted_buckets
from db import VideoCollectionClaim
from test_collection_dispatch import Clock, ManualExecutor
from video_collection_service import VideoCollectionService, run_claimed_common_video_collection
from youtube_batch_stats import fetch_video_batch_stats, is_batch_stats_quota_error

CHANNEL = 'UC' + 'q' * 22
PHASES = ['video_collection', 'channel_metadata', 'typed_inventory']
QUOTA = {'error': {'code': 403, 'message': 'quota', 'errors': [{'reason': 'quotaExceeded'}]}}


class Response:
    def __init__(self, status, payload):
        self.status_code, self.payload, self.headers = status, payload, {}

    def json(self):
        if self.payload is None:
            raise ValueError('not JSON')
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}')


class Repository:
    """Three due batches of 25 details claims and 25 counter-only claims (synthetic)."""
    def __init__(self, *, claim_error=None):
        self.claim_calls, self.failed, self.identities = 0, [], []
        self.claim_error = claim_error

    def reconcile_typed(self, video_type, limit):
        return {'affected_count': 0}

    def claim_due_batch(self, limit, worker_id, run_id, lease_seconds):
        self.claim_calls += 1
        if self.claim_error is not None:
            raise self.claim_error
        base = (self.claim_calls - 1) * 50
        rows = [VideoCollectionClaim(video_id=f'vid{base + i:08d}', channel_id=CHANNEL,
                    collection_mode='metadata' if i < 25 else 'counters', lease_token=f't{base + i}',
                    lease_expires_at='2026-11-08T00:15:00+00:00', next_due_at='2026-11-08T00:00:00+00:00')
                for i in range(50)] if self.claim_calls <= 3 else []
        return {'rows': rows, 'inspected_count': len(rows), 'paused_count': 0, 'scan_exhausted': self.claim_calls >= 3}

    def load_counter_presence(self, ids):
        return {i: True for i in ids}

    def fail_batch(self, rows):
        self.failed.extend(rows)
        return len(rows)

    def complete_identity_refresh(self, rows):
        self.identities.extend(rows)
        return len(rows)

    def complete_counters(self, rows):
        return len(rows)

    def complete_typed_metadata(self, video_type, rows):
        return len(rows)


class Provider:
    """The two quota buckets as the pass meets them; every call is recorded."""
    def __init__(self, *, general='ok', stats='ok'):
        self.general, self.stats, self.calls = general, stats, []

    def fetch_metadata(self, ids, phase):
        self.calls.append(('general', len(ids)))
        if self.general == 'quota':
            # What youtube_response_error raises for a General quotaExceeded answer.
            raise youtube_errors.YouTubeDailyQuotaExceededError('provider_quota_exceeded')
        return [{'id': v, 'snippet': {'channelId': CHANNEL, 'publishedAt': '2026-01-01T00:00:00Z', 'title': 'Synthetic'},
                 'contentDetails': {'duration': 'PT10M'}, 'status': {'privacyStatus': 'public', 'uploadStatus': 'processed'},
                 'statistics': {'viewCount': '10', 'likeCount': '1', 'commentCount': '0'}} for v in ids]

    def fetch_counters(self, ids, phase):
        self.calls.append(('stats', len(ids)))
        def get(*args, **kwargs):
            if self.stats == 'timeout':
                raise requests.Timeout('synthetic read timeout')
            return {'ok': Response(200, {'items': [{'id': v, 'statistics': {'viewCount': '5'}} for v in ids]}),
                    'quota': Response(403, QUOTA),
                    '503': Response(503, {'error': {'code': 503, 'errors': [{'reason': 'backendError'}]}}),
                    '429': Response(429, {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED'}})}[self.stats]
        with patch.object(youtube_batch_stats, 'youtube_api_get', get):
            return fetch_video_batch_stats(list(ids), api_key=None, attempt_recorder=None)

    def count(self, bucket):
        return sum(1 for name, _ in self.calls if name == bucket)


def video_service(repository, provider):
    service = VideoCollectionService(repository, fetch_full=provider.fetch_metadata,
        fetch_metadata=provider.fetch_metadata, fetch_counters=provider.fetch_counters,
        build_full_payload=lambda *a: {}, build_counter_payload=lambda *a: {},
        is_quota_exhausted=partial(youtube_errors.is_youtube_daily_quota_exhausted,
                                   is_batch_stats_quota_error=is_batch_stats_quota_error))
    # Successful completions are acknowledged; only failures and saved profiles matter here.
    service._complete_common_long_metadata = lambda pairs, summary, **kw: summary.__setitem__(
        'updated', summary['updated'] + len(pairs))
    service._complete_common_counters = lambda pairs, summary, **kw: summary.__setitem__(
        'updated', summary['updated'] + len(pairs))
    return service


class Dispatch:
    """The pass's three steps: the actual video step, and General-only profile and playlist steps."""
    def __init__(self, repository, provider, events, *, profiles='ok'):
        self.repository, self.provider, self.events, self.profiles = repository, provider, events, profiles

    def __call__(self, *, limit, progress_callback=None, common_preflight_checked=False, context=None):
        self.events.append('video_collection')
        return run_claimed_common_video_collection(self.repository, video_service(self.repository, self.provider),
                                                   limit=limit, worker_id='quota-split-test')

    def metadata(self, *, limit, context=None):
        self.events.append('channel_metadata')
        self.provider.calls.append(('general', 'channels'))
        if self.profiles == 'quota':
            return {'requested': 50, 'processed': 0, 'updated': 0, 'errors': 1, 'quota_exhausted': True,
                    'general_quota_exhausted': True, 'stopped_reason': 'quota_exhausted'}
        return {'requested': 50, 'processed': 50, 'updated': 50, 'errors': 0}

    def inventory(self, *, limit, run_id, on_page, context=None):
        self.events.append('typed_inventory')
        self.provider.calls.append(('general', 'playlistItems'))
        return {'enabled': True, 'claimed': 0, 'errors': 0}


def run_pass(provider, *, repository=None, remembered=(), profiles='ok', pauses=()):
    repository = repository or Repository()
    events = []
    context = SimpleNamespace(settings=SimpleNamespace(typed_playlist_inventory_enabled=True), database=None)
    gate = SimpleNamespace(pause_seconds=lambda: pauses.pop(0) if pauses else 0.0)
    with patch.object(cycle, 'validate_required_auto_sync_rpcs', lambda **k: {'status': 'ok'}), \
         patch.object(cycle, 'recover_stale_running_statuses', lambda **k: {'errors': 0}), \
         patch.object(cycle, 'update_sync_run_progress', lambda *a, **k: None), \
         patch.object(cycle, 'emit_phase', lambda *a, **k: None), \
         patch.object(cycle, 'wait_for_handoff', lambda seconds: False), \
         patch.object(collection_shutdown, '_requested', Event()), \
         patch.object(provider_runtime, '_runtime', SimpleNamespace(gate=gate)), \
         patch.object(quota_buckets, 'read_remembered', lambda database: tuple(remembered)), \
         patch('sys.stdout', io.StringIO()):
        summary = cycle._run_auto_sync_cycle(stats_limit=20000, new_video_stats_limit=2500, metadata_limit=1000,
            sync_run_id=1, inventory_page_budget=20000, context=context,
            common_dispatch=Dispatch(repository, provider, events, profiles=profiles))
    return events, summary, repository


def failed(repository, mode):
    lo, hi = (0, 25) if mode == 'metadata' else (25, 50)
    return [row for row in repository.failed if lo <= int(row['video_id'][3:]) % 50 < hi]


class StopTableRowTests(unittest.TestCase):
    """docs/CONFIGURATION.md, "A problem stops only the work it affects"."""

    def tearDown(self):
        self.assertFalse(quota_buckets.active())  # the pass ended its memory

    def test_general_quota_exhausted(self):
        provider = Provider(general='quota')
        events, summary, repository = run_pass(provider)
        # Stops: video details, channel profiles, playlist checks.
        self.assertEqual(events, ['video_collection'])
        self.assertEqual(provider.count('general'), 1)
        self.assertNotIn(('general', 'channels'), provider.calls)
        self.assertNotIn(('general', 'playlistItems'), provider.calls)
        # Continues: counters through Stats, in every batch, the refused one too.
        self.assertEqual(provider.calls.count(('stats', 25)), 3)
        self.assertFalse(failed(repository, 'counters'))
        # Details wait for the reset; only their own deadline moves.
        rows = failed(repository, 'metadata')
        self.assertEqual(len(rows), 75)
        self.assertEqual({(row['error_type'], row['retry_after_seconds'], row['quota_bucket']) for row in rows},
                         {('youtube_quota_exhausted', 3600, 'general')})
        # The pass is not stopped; it records the bucket and one refused request.
        self.assertIsNone(summary.get('stopped_reason'))
        self.assertEqual((summary['general_quota_exhausted'], summary.get('batch_quota_exhausted', False),
                          summary['quota_exhausted']), (True, False, True))
        self.assertEqual((summary['video_collection']['errors'], summary['video_collection']['quota_wait_deferred']),
                         (1, 50))
        self.assertEqual(summary['channel_metadata']['skipped_reason'], 'general_quota_exhausted')
        self.assertEqual(summary['typed_inventory']['skipped_reason'], 'general_quota_exhausted')
        self.assertEqual(summary['current_phase'], 'completed')

    def test_stats_quota_exhausted(self):
        provider = Provider(stats='quota')
        events, summary, repository = run_pass(provider)
        # Continues: all General work; video details already carry counters.
        self.assertEqual(events, PHASES)
        self.assertEqual(provider.calls.count(('general', 25)), 3)
        self.assertIn(('general', 'channels'), provider.calls)
        self.assertIn(('general', 'playlistItems'), provider.calls)
        self.assertFalse(failed(repository, 'metadata'))
        # Stops: counter-only refreshes, after one refused request.
        self.assertEqual(provider.count('stats'), 1)
        rows = failed(repository, 'counters')
        self.assertEqual(len(rows), 75)
        self.assertEqual({(row['error_type'], row['retry_after_seconds'], row['quota_bucket']) for row in rows},
                         {('youtube_quota_exhausted', 3600, 'stats')})
        self.assertIsNone(summary.get('stopped_reason'))
        self.assertEqual((summary['batch_quota_exhausted'], summary.get('general_quota_exhausted', False)), (True, False))
        self.assertNotIn('phase_stops', summary)

    def test_stats_quota_also_stops_the_extra_counter_request_of_details_without_views(self):
        provider = Provider(stats='quota')
        original = provider.fetch_metadata
        def without_views(ids, phase):
            items = original(ids, phase)
            for item in items:
                del item['statistics']['viewCount']
            return items
        provider.fetch_metadata = without_views
        events, summary, repository = run_pass(provider)
        self.assertEqual(events, PHASES)
        # The first batch asks Stats once (counter-only claims and the
        # fallback together); later fallbacks send nothing and keep the
        # General details without counters.
        self.assertEqual(provider.count('stats'), 1)
        self.assertEqual(len(repository.identities), 75)
        self.assertTrue(all(row['counterless_profile'] for row in repository.identities))

    def test_rate_limit_ends_only_its_step(self):
        provider = Provider(stats='429')
        events, summary, repository = run_pass(provider, pauses=[3.0, 0.0])
        self.assertEqual(events, PHASES)
        self.assertEqual((repository.claim_calls, provider.count('stats')), (1, 1))
        self.assertEqual(summary['phase_stops'], {'video_collection': 'rate_limited'})
        self.assertFalse(exhausted_buckets(summary))

    def test_failed_batch_request_ends_only_the_video_step(self):
        for kind in ('503', 'timeout'):
            with self.subTest(kind=kind):
                provider = Provider(stats=kind)
                events, summary, repository = run_pass(provider)
                self.assertEqual(events, PHASES)
                self.assertEqual((repository.claim_calls, provider.count('stats')), (1, 1))
                self.assertEqual(summary['phase_stops'], {'video_collection': 'batch_unavailable'})
                self.assertIn(('general', 'channels'), provider.calls)

    def test_unconfirmed_write_stops_the_whole_pass(self):
        provider = Provider()
        events, summary, _ = run_pass(provider, repository=Repository(claim_error=OSError('lost ACK')))
        self.assertEqual(events, ['video_collection'])
        self.assertEqual(provider.calls, [])
        self.assertEqual((summary['stopped_reason'], summary['settlement_unknown']), ('claim_failed', True))

    def test_general_exhausted_in_the_profile_step_skips_only_playlists(self):
        provider = Provider()
        events, summary, _ = run_pass(provider, profiles='quota')
        self.assertEqual(events, ['video_collection', 'channel_metadata'])
        self.assertIsNone(summary.get('stopped_reason'))
        self.assertTrue(summary['general_quota_exhausted'])
        self.assertEqual(summary['typed_inventory']['skipped_reason'], 'general_quota_exhausted')


class RememberedExhaustionTests(unittest.TestCase):
    """No request goes to an exhausted bucket until midnight Pacific."""

    def test_next_pass_sends_nothing_to_a_bucket_refused_today(self):
        provider = Provider()
        events, summary, repository = run_pass(provider, remembered=('general',))
        self.assertEqual(provider.count('general'), 0)
        self.assertEqual(provider.calls.count(('stats', 25)), 3)
        self.assertEqual(events, ['video_collection'])
        # Waiting is not a failed request.
        self.assertEqual((summary['video_collection']['errors'], summary['video_collection']['quota_wait_deferred']),
                         (0, 75))
        self.assertEqual({row['quota_bucket'] for row in failed(repository, 'metadata')}, {'general'})
        self.assertTrue(summary['general_quota_exhausted'])
        self.assertEqual(summary['errors'], 0)

    def test_remembered_stats_sends_no_counter_request(self):
        provider = Provider()
        events, summary, repository = run_pass(provider, remembered=('stats',))
        self.assertEqual((provider.count('stats'), provider.calls.count(('general', 25))), (0, 3))
        self.assertEqual(events, PHASES)
        self.assertEqual({row['quota_bucket'] for row in failed(repository, 'counters')}, {'stats'})

    def test_both_buckets_exhausted_send_and_move_nothing(self):
        provider = Provider()
        events, summary, repository = run_pass(provider, remembered=('general', 'stats'))
        self.assertEqual((provider.calls, repository.claim_calls), ([], 0))
        self.assertEqual(events, [])
        self.assertEqual(summary['video_collection']['skipped_reason'], 'quota_exhausted')

    def test_after_midnight_pacific_the_bucket_is_asked_again(self):
        quota_buckets.start_pass(('general',), now=datetime(2026, 11, 8, 7, 59, tzinfo=timezone.utc))
        self.addCleanup(quota_buckets.end_pass)
        before = datetime(2026, 11, 8, 7, 59, tzinfo=timezone.utc)   # 23:59 PST
        after = before + timedelta(minutes=2)                         # 00:01 PST
        self.assertTrue(quota_buckets.is_exhausted('general', now=before))
        self.assertFalse(quota_buckets.is_exhausted('general', now=after))
        quota_buckets.note_exhausted('stats', now=after)
        self.assertEqual(quota_buckets.exhausted(now=after), ('stats',))
        # A fresh ledger day knows no refusal: the pass asks General as usual.
        provider = Provider()
        quota_buckets.end_pass()
        run_pass(provider, remembered=())
        self.assertEqual(provider.calls.count(('general', 25)), 3)

    def test_memory_is_per_pass_and_inactive_outside(self):
        self.assertFalse(quota_buckets.active())
        quota_buckets.note_exhausted('general')
        self.assertEqual(quota_buckets.exhausted(), ())
        with quota_buckets.remembered():
            quota_buckets.note_exhausted('general')
            self.assertTrue(quota_buckets.is_exhausted('general'))
            with quota_buckets.remembered():   # a nested owner reuses it
                self.assertTrue(quota_buckets.is_exhausted('general'))
            self.assertTrue(quota_buckets.active())
        self.assertFalse(quota_buckets.active())

    def test_ledger_read_names_stop_table_buckets(self):
        from db.quota.youtube_quota import YouTubeQuotaRepository
        reads = []
        class Database:
            def read_rows(self, operation, statement, parameters, *, max_rows):
                reads.append((operation, statement.as_string(None) if hasattr(statement, 'as_string') else statement,
                              parameters, max_rows))
                return [{'name': 'video_batch_get_stats'}]
        self.assertEqual(YouTubeQuotaRepository(Database(), project_id='primary').exhausted_buckets(), ('stats',))
        operation, statement, parameters, max_rows = reads[0]
        self.assertEqual((operation, max_rows), ('youtube_quota_exhausted_buckets', 2))
        self.assertEqual(parameters, (['general', 'video_batch_get_stats'], 'primary', 'quotaExceeded'))
        self.assertIn('readable.quota_attempts', statement)
        self.assertIn("(now() AT TIME ZONE 'America/Los_Angeles')::date", statement)

    def test_ledger_records_a_daily_quota_refusal_label(self):
        finished = []
        class Recorder:
            def start_attempt(self, **kw):
                return 'attempt-1'
            def finish_attempt(self, attempt_id, **kw):
                finished.append(kw)
        def send(adapter, request, **kwargs):
            response = requests.Response()
            response.status_code, response.url, response.request = 403, request.url, request
            # The bounded reader takes the body from the stream, as from the network.
            response.raw = io.BytesIO(b'{"error":{"code":403,"errors":[{"reason":"quotaExceeded"}]}}')
            return response
        import os
        from contextlib import nullcontext
        import hot_runtime
        environ = {k: v for k, v in os.environ.items() if k != 'LIC_SETUP_DIRECTORY'}
        environ.update({'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
                        'LIC_DATABASE_ID': '91f5d9f3-9230-4ee6-b170-155d4c3c1719',
                        'LIC_HOT_RUNTIME_SECRET': 'synthetic-internal-admission-secret-32'})
        ready = SimpleNamespace(require_ready=lambda: None, require_collection_ready=lambda: None,
                                require_database_ready=lambda: None)
        with patch.dict(os.environ, environ, clear=True), patch.object(hot_runtime, '_local_controller', ready),              patch('provider_runtime.admit_request', side_effect=lambda **kw: nullcontext(kw.get('cancel'))),              patch.object(requests.adapters.HTTPAdapter, 'send', send):
            response = youtube_client.youtube_api_get('videos', phase='test', quota_bucket='general',
                attempt_recorder=Recorder(), params={'part': 'snippet', 'id': 'abc'}, api_key='AIza' + 'k' * 35)
        self.assertEqual(response.status_code, 403)
        self.assertEqual((finished[0]['http_status'], finished[0]['error_type']), (403, 'quotaExceeded'))
        self.assertIsNone(youtube_client._ledger_refusal_type(
            SimpleNamespace(json=lambda: {'error': {'errors': [{'reason': 'forbidden'}]}}), 403, 'general'))


class HealthTests(unittest.TestCase):
    def test_health_shows_each_bucket_refused_today(self):
        from youtube_pipeline_health import collect_pipeline_health
        now = datetime.now(timezone.utc)
        latest = {'status': 'completed', 'errors': 0, 'finished_at': now.isoformat()}
        ledger = SimpleNamespace(summarize_day=lambda *a, **k: {'truncated': False},
                                 exhausted_buckets=lambda: ('stats',))
        with patch('youtube_pipeline_health.read_provider_quota_control', return_value={
                 'status': 'ok', 'exhausted_buckets': {'general': False, 'video_batch_get_stats': True}}), \
             patch('youtube_pipeline_health.read_collection_capacity', return_value={'status': 'ok', 'stats_limit': 20000}), \
             patch('youtube_pipeline_health.SyncRunRepository', return_value=SimpleNamespace(
                 latest=lambda: latest, latest_completed=lambda: latest, list_since=lambda *a, **k: [])), \
             patch('youtube_pipeline_health.fetch_paginated', return_value=([], False)), \
             patch('youtube_pipeline_health.get_all_video_queue_health', return_value={'due': 0, 'leased': 0, 'failed': 0}), \
             patch('youtube_pipeline_health.get_all_discovery_queue_health', return_value={'due': 0, 'leased': 0, 'failed': 0}), \
             patch('youtube_pipeline_health.ChannelSyncRepository', return_value=SimpleNamespace(health=lambda: {})), \
             patch('youtube_pipeline_health.YouTubeQuotaRepository', return_value=ledger):
            report = collect_pipeline_health(object(), now=now,
                database_video_collection_enabled=True, database_channel_discovery_enabled=True,
                quota_buffer_status_reader=lambda: {'status': 'ok', 'pending_events': 0})
        self.assertEqual(report['youtube_quota']['exhausted_buckets'],
                         {'general': False, 'video_batch_get_stats': True})
        self.assertFalse([e for e in report['collection_errors'] if e['component'] == 'youtube_api_request_attempts'])


class StopRuleTests(unittest.TestCase):
    def test_bucket_flags_are_not_pass_reasons(self):
        self.assertIsNone(collection_stop_reason({'quota_exhausted': True, 'general_quota_exhausted': True}, 'pass'))
        self.assertEqual(collection_stop_reason({'quota_exhausted': True}, 'pass'), 'quota_exhausted')
        self.assertEqual(collection_stop_reason({'batch_quota_exhausted': True}, 'stats'), 'batch_quota_exhausted')
        self.assertEqual(exhausted_buckets({'general_quota_exhausted': True, 'batch_quota_exhausted': True}),
                         ('general', 'stats'))

    def test_window_ends_a_general_only_step_and_keeps_the_video_step(self):
        operator = SimpleNamespace(advance=lambda *a, **k: None, stop=lambda *a, **k: None,
                                   resume_idle=lambda: None, summary={}, finished=True)
        window = AutomaticDispatchWindow(concurrency=2, require_ready=lambda: None, should_stop=lambda: False,
            operator_factory=lambda **kw: operator, executor=ManualExecutor(), clock=Clock())
        self.addCleanup(window.close)
        paused = []
        window._feeder = SimpleNamespace(quota_buckets=('general', 'stats'), pause=paused.append)
        window.stop_all({'general_quota_exhausted': True, 'quota_exhausted': True})
        self.assertEqual((window.stop_reason, window.phase_stop_reason, paused), (None, None, []))
        self.assertTrue(quota_buckets.is_exhausted('general'))
        window._feeder = SimpleNamespace(quota_buckets=('general',), pause=paused.append)
        window.stop_all({'general_quota_exhausted': True, 'quota_exhausted': True})
        self.assertEqual((window.stop_reason, window.phase_stop_reason, paused),
                         (None, 'general_quota_exhausted', ['general_quota_exhausted']))
        # Its drained units are not a handoff.
        window.stop_all({'yield_requested': True})
        self.assertIsNone(window.stop_reason)
        window._feeder = None
        # A one-off job's exhausted Stats is remembered for the automatic work too.
        window.operator_stopped({'batch_quota_exhausted': True, 'quota_exhausted': True})
        self.assertIsNone(window.stop_reason)
        self.assertTrue(window.stop_flags['batch_quota_exhausted'])


class TwoOperationRepository:
    """Every channel has due details and due counters; reads and claims filter by operation."""
    def __init__(self, channels, *, metadata, counters):
        self.owner = get_ident()
        self.physical = sorted(channels)
        self.due = {(channel, operation): count for channel in channels
                    for operation, count in (('metadata', metadata), ('counters', counters))}
        self.claims = []

    def due_candidate_page(self, **request):
        assert get_ident() == self.owner
        physical = [key for key in self.physical if key > (request['after'] or '')][:request['limit']]
        rows = []
        for key in physical:
            if request['video_type'] != 'long':
                continue
            for operation in ('metadata', 'counters'):
                frontier = (request.get('frontiers') or {}).get(operation, {})
                if request['operation'] not in (None, operation) or (request['operation'] is None and (
                        frontier.get('exhausted') or key <= (frontier.get('after') or ''))):
                    continue
                if self.due[(key, operation)]:
                    rows.append(dict(channel_id=key, video_type='long', operation=operation,
                                     ready_count=min(50, self.due[(key, operation)]), due_at='2026-11-08T00:00:00Z'))
                    break
        return dict(rows=rows, next_after=physical[-1] if physical else request['after'],
                    inspected_count=len(physical), scan_exhausted=len(physical) < request['limit'])

    def claim_due_batch(self, **request):
        assert get_ident() == self.owner
        self.claims.append((request['operation'], request['limit']))
        rows = []
        for scope in request['scopes']:
            key = (scope['channel_id'], request['operation'])
            while self.due[key] and len(rows) < request['limit']:
                self.due[key] -= 1
                rows.append(SimpleNamespace(video_id=f"{key[0]}:{key[1]}:{self.due[key]}",
                                            channel_id=key[0], video_type='long'))
        return dict(rows=rows, inspected_count=len(rows), paused_count=0, scan_exhausted=len(rows) < request['limit'])


class PassLimitTests(unittest.TestCase):
    """stats_limit counts separately for General (details) and Stats (counters)."""

    def drive(self, repository, limit):
        def steps(claims, **kwargs):
            result = yield ProviderStep(lambda ids, reason: None, tuple(row.video_id for row in claims), 'auto')
            result.unwrap()
            return dict(updated=len(claims), batches=1, video_ids=[])
        service = SimpleNamespace(repository=repository, common_claim_steps=steps,
                                  finish_common_claims=lambda claims, summary: summary)
        state = AutoPassState(limit, concurrency=2)
        feeder = CommonAutoFeeder(service, state=state, cursor=CommonCandidateCursor(),
                                  worker_id='worker', require_ready=lambda: None)
        executor = ManualExecutor()
        coordinator = Coordinator(executor, 2, clock=Clock())
        with patch.object(collection_shutdown, '_requested', Event()):
            for _ in range(2000):
                feeder.offer_to(coordinator)
                coordinator.step()
                for index, (future, *_) in enumerate(executor.jobs):
                    if not future.done():
                        executor.run(index)
                snapshot = coordinator.snapshot()
                if feeder.exhausted and not snapshot.ready and not snapshot.held:
                    return state, feeder
        self.fail('bounded pass did not finish')

    def test_each_bucket_gets_the_whole_limit(self):
        channels = ['UC' + str(n).zfill(22) for n in range(1, 5)]
        repository = TwoOperationRepository(channels, metadata=150, counters=150)
        state, feeder = self.drive(repository, 100)
        self.assertEqual(state.claimed_by, {'metadata': 100, 'counters': 100, None: 0})
        self.assertEqual((state.claimed, feeder.summary['updated']), (200, 200))
        self.assertTrue(all(limit <= 50 for _, limit in repository.claims))
        self.assertEqual(feeder.pass_stop_reason, 'stats_limit')

    def test_a_spent_bucket_waits_while_the_other_finishes_its_work(self):
        channels = ['UC' + str(n).zfill(22) for n in range(1, 4)]
        repository = TwoOperationRepository(channels, metadata=200, counters=10)
        state, feeder = self.drive(repository, 100)
        self.assertEqual(state.claimed_by, {'metadata': 100, 'counters': 30, None: 0})
        self.assertTrue(state.operation_spent('metadata'))
        self.assertFalse(state.operation_spent('counters'))
        # No claim was offered past a spent budget (none skipped for budget).
        self.assertEqual(sum(limit for operation, limit in repository.claims if operation == 'metadata'), 100)

    def test_mixed_claims_share_one_number_as_before(self):
        state = AutoPassState(60, concurrency=1)
        self.assertEqual(state.begin_claim('a'), dict(limit=50, run_id=state.run_id))
        state.on_claimed('a', dict(rows=[SimpleNamespace(video_id=str(i)) for i in range(50)], inspected_count=50,
                                   paused_count=0, scan_exhausted=False))
        self.assertEqual((state.remaining, state.remaining_for('metadata'), state.remaining_for('counters')), (10, 10, 10))


class DeadlineRowTests(unittest.TestCase):
    """The failure write moves only the exhausted bucket's deadline (fake DB; PG17: quota_split_pg)."""

    def write(self, bucket, error='youtube_quota_exhausted'):
        from db.videos import video_failures
        from test_video_failures import Database, locked, parsed, payload
        row = payload(error_type=error, retry_after_seconds=3600, **({'quota_bucket': bucket} if bucket else {}))
        db = Database([parsed(error_type=error, retry_after_seconds=3600, quota_bucket=bucket)], [locked()], 1)
        with patch('db.channel.channel_control.assert_channel_scopes', create=True):
            self.assertEqual(video_failures.fail_video_collection_batch(db, [row]), 1)
        return db.calls[-1][2][0].obj[0], db.calls[-1][1]

    def test_general_moves_the_details_deadline_and_stats_the_counters_deadline(self):
        update, sql = self.write('general')
        self.assertEqual(update['defer'], 'metadata')
        self.assertIn("WHEN input.defer='metadata'", sql)
        update, _ = self.write('stats')
        self.assertEqual(update['defer'], 'counters')
        update, _ = self.write(None)
        self.assertIsNone(update['defer'])
        # A bucket beside another error moves nothing special.
        update, _ = self.write('general', error='youtube_metadata_request_error')
        self.assertIsNone(update['defer'])

    def test_an_unknown_bucket_is_refused_before_any_lock(self):
        from db.videos import video_failures
        from db.base.postgres import DatabaseServerError
        from test_video_failures import Database, parsed, payload
        db = Database([parsed(quota_bucket='search')])
        with self.assertRaisesRegex(DatabaseServerError, 'common_video_collection_fail_row_invalid'):
            video_failures.fail_video_collection_batch(db, [payload(quota_bucket='search')])
        self.assertEqual(len(db.calls), 1)


if __name__ == '__main__':
    unittest.main()
