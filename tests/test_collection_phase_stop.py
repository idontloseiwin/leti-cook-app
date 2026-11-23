"""A Batch failure or a 429 ends only its own step (quota only its bucket).

Exercises the
VideoCollectionService with a synthetic Batch transport, then the actual
automatic pass and dispatch window. No database, network or real key.
"""
from functools import partial
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import io
import sys
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).parent)]
import requests
import collection_shutdown
import provider_runtime
import youtube_batch_stats
import youtube_errors
from collection import cycle
from collection.cycle import AutomaticDispatchWindow
from collection_outcome import (COLLECTION_STOP_FLAGS, COLLECTION_STOP_SCOPES, PASS_STOP_FLAGS, PHASE_STOP_FLAGS,
    collection_outcome_fields, collection_stop_reason, copy_collection_stop, sync_completion_fields)
from db import VideoCollectionClaim
from operator_jobs import operator_collection_stop_reason
from test_collection_dispatch import Clock, ManualExecutor
from video_collection_service import VideoCollectionService, run_claimed_common_video_collection
from youtube_batch_stats import fetch_video_batch_stats, is_batch_stats_quota_error

CHANNEL = 'UC' + 'c' * 22
PHASES = ['video_collection', 'channel_metadata', 'typed_inventory']


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
    """Three due batches: 25 metadata + 25 counters claims each (synthetic IDs)."""
    def __init__(self):
        self.claim_calls, self.failed = 0, []

    def reconcile_typed(self, video_type, limit):
        return {'affected_count': 0}

    def claim_due_batch(self, limit, worker_id, run_id, lease_seconds):
        self.claim_calls += 1
        base = (self.claim_calls - 1) * 50
        rows = [VideoCollectionClaim(video_id=f'vid{base + i:08d}', channel_id=CHANNEL,
                    collection_mode='metadata' if i < 25 else 'counters', lease_token=f't{base + i}',
                    lease_expires_at='2026-11-04T00:15:00+00:00', next_due_at='2026-11-04T00:00:00+00:00')
                for i in range(50)] if self.claim_calls <= 3 else []
        return {'rows': rows, 'inspected_count': len(rows), 'paused_count': 0, 'scan_exhausted': self.claim_calls >= 3}

    def load_counter_presence(self, ids):
        return {i: True for i in ids}

    def fail_batch(self, rows):
        self.failed.extend(rows)
        return len(rows)

    def complete_identity_refresh(self, rows):
        return len(rows)


def metadata(ids, phase):
    return [{'id': v, 'snippet': {'channelId': CHANNEL, 'publishedAt': '2026-01-01T00:00:00Z', 'title': 'Synthetic'},
             'contentDetails': {'duration': 'PT10M'}, 'status': {'privacyStatus': 'public', 'uploadStatus': 'processed'}}
            for v in ids]


def batch_transport(kind):
    def get(*args, **kwargs):
        if kind == 'timeout':
            raise requests.Timeout('synthetic read timeout')
        return {'503': Response(503, {'error': {'code': 503, 'message': 'Backend Error', 'errors': [{'reason': 'backendError'}]}}),
                '500-nobody': Response(500, None),
                '403-quota': Response(403, {'error': {'code': 403, 'message': 'quota', 'errors': [{'reason': 'quotaExceeded'}]}}),
                '429': Response(429, {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED'}})}[kind]
    return get


def run_video_phase(kind):
    repo = Repository()
    def fetch_counters(ids, phase):
        with patch.object(youtube_batch_stats, 'youtube_api_get', batch_transport(kind)):
            return fetch_video_batch_stats(list(ids), api_key=None, attempt_recorder=None)
    service = VideoCollectionService(repo, fetch_full=metadata, fetch_metadata=metadata,
        fetch_counters=fetch_counters, build_full_payload=lambda *a: {}, build_counter_payload=lambda *a: {},
        is_quota_exhausted=partial(youtube_errors.is_youtube_daily_quota_exhausted,
                                   is_batch_stats_quota_error=is_batch_stats_quota_error))
    with patch('sys.stdout', io.StringIO()):
        return run_claimed_common_video_collection(repo, service, limit=150, worker_id='phase-stop-test'), repo


class Dispatch:
    """Stands in for bind_process_common_dispatch(whole_cycle=True): step callables only."""
    def __init__(self, video_summary, events, *, metadata_summary=None):
        self.video_summary, self.events = video_summary, events
        self.metadata_summary = metadata_summary or {'requested': 0, 'processed': 0, 'updated': 0, 'errors': 0}

    def __call__(self, *, limit, progress_callback=None, common_preflight_checked=False, context=None):
        self.events.append('video_collection')
        return dict(self.video_summary)

    def metadata(self, *, limit, context=None):
        self.events.append('channel_metadata')
        return dict(self.metadata_summary)

    def inventory(self, *, limit, run_id, on_page, context=None):
        self.events.append('typed_inventory')
        return {'enabled': True, 'claimed': 0, 'errors': 0}


class Gate:
    """The installation limiter's pause as the cycle reads it."""
    def __init__(self, pauses, events):
        self.pauses, self.events = list(pauses), events

    def pause_seconds(self):
        value = self.pauses.pop(0) if self.pauses else 0.0
        self.events.append(('pause', value))
        return value


def run_pass(video_summary, *, metadata_summary=None, pauses=(), stop_during_wait=False):
    events = []
    dispatch = Dispatch(video_summary, events, metadata_summary=metadata_summary)
    requested = Event()
    def wait(seconds):
        events.append(('wait', seconds))
        if stop_during_wait:
            requested.set()
        return requested.is_set()
    context = SimpleNamespace(settings=SimpleNamespace(typed_playlist_inventory_enabled=True), database=None)
    with patch.object(cycle, 'validate_required_auto_sync_rpcs', lambda **k: {'status': 'ok'}), \
         patch.object(cycle, 'recover_stale_running_statuses', lambda **k: {'errors': 0}), \
         patch.object(cycle, 'update_sync_run_progress', lambda *a, **k: None), \
         patch.object(cycle, 'emit_phase', lambda *a, **k: None), \
         patch.object(cycle, 'wait_for_handoff', wait), \
         patch.object(collection_shutdown, '_requested', requested), \
         patch.object(provider_runtime, '_runtime', SimpleNamespace(gate=Gate(pauses, events)) if pauses else None), \
         patch('sys.stdout', io.StringIO()):
        summary = cycle._run_auto_sync_cycle(stats_limit=20000, new_video_stats_limit=2500, metadata_limit=1000,
            sync_run_id=1, inventory_page_budget=20000, context=context, common_dispatch=dispatch)
    return events, summary


def phases(events):
    return [event for event in events if isinstance(event, str)]


class PassPhaseTests(unittest.TestCase):
    def test_batch_failure_ends_only_the_video_step(self):
        for kind in ('503', 'timeout', '500-nobody'):
            with self.subTest(kind=kind):
                video, repo = run_video_phase(kind)
                self.assertEqual((video['batch_unavailable'], video['stopped_reason']), (True, 'batch_unavailable'))
                # Inside the step nothing changes: no further batch after the refusal.
                self.assertEqual(repo.claim_calls, 1)
                self.assertEqual({row.get('retry_after_seconds') for row in repo.failed}, {1800})
                events, summary = run_pass(video)
                self.assertEqual(phases(events), PHASES)
                self.assertEqual(summary['phase_stops'], {'video_collection': 'batch_unavailable'})
                self.assertTrue(summary['batch_unavailable'])
                self.assertIsNone(summary.get('stopped_reason'))
                self.assertGreaterEqual(summary['errors'], 1)
                self.assertEqual(summary['completion_outcome'], 'error')
                self.assertEqual(summary['current_phase'], 'completed')
                self.assertNotIn(('wait', 0.0), events)
                self.assertFalse([event for event in events if isinstance(event, tuple)])

    def test_rate_limit_ends_its_step_and_the_next_waits_for_the_limiter_pause(self):
        video, repo = run_video_phase('429')
        self.assertEqual((video['rate_limited'], video['stopped_reason']), (True, 'provider_rate_limited'))
        self.assertEqual({row.get('retry_after_seconds') for row in repo.failed}, {60})
        # The limiter pause was 40 s and a late answer extended it by 5 s.
        events, summary = run_pass(video, pauses=(40.0, 5.0, 0.0))
        self.assertEqual(events, ['video_collection', ('pause', 40.0), ('wait', 40.0), ('pause', 5.0),
            ('wait', 5.0), ('pause', 0.0), 'channel_metadata', 'typed_inventory'])
        self.assertEqual(summary['phase_stops'], {'video_collection': 'rate_limited'})
        self.assertTrue(summary['rate_limited'])
        self.assertIsNone(summary.get('stopped_reason'))
        self.assertEqual(summary['completion_outcome'], 'error')

    def test_rate_limit_in_the_profile_step_delays_only_the_playlist_step(self):
        events, summary = run_pass({'requested': 0, 'updated': 0, 'errors': 0},
            metadata_summary={'requested': 50, 'updated': 10, 'errors': 1, 'rate_limited': True,
                              'stopped_reason': 'provider_rate_limited'}, pauses=(12.0, 0.0))
        self.assertEqual(events, ['video_collection', 'channel_metadata', ('pause', 12.0), ('wait', 12.0),
                                  ('pause', 0.0), 'typed_inventory'])
        self.assertEqual(summary['phase_stops'], {'channel_metadata': 'rate_limited'})

    def test_wait_is_bounded_by_60_seconds(self):
        video, _ = run_video_phase('429')
        clock = iter(range(0, 1000, 30))
        with patch('time.monotonic', lambda: float(next(clock))):
            events, _ = run_pass(video, pauses=(60.0,) * 10)
        waits = [event[1] for event in events if isinstance(event, tuple) and event[0] == 'wait']
        self.assertLessEqual(sum(waits), 60)
        self.assertEqual(phases(events), PHASES)

    def test_handoff_during_the_pause_ends_the_pass_at_once(self):
        video, _ = run_video_phase('429')
        events, summary = run_pass(video, pauses=(30.0, 30.0), stop_during_wait=True)
        self.assertEqual(phases(events), ['video_collection'])
        self.assertEqual([event for event in events if event[0] == 'wait'], [('wait', 30.0)])
        self.assertEqual((summary['yield_requested'], summary['stopped_reason']), (True, 'scheduled_handoff'))

    def test_exhausted_stats_quota_ends_only_stats_work(self):
        # The pass goes on; tests/test_quota_split.py
        # covers every row of the table.
        video, repo = run_video_phase('403-quota')
        self.assertEqual({row.get('retry_after_seconds') for row in repo.failed}, {3600})
        events, summary = run_pass(video)
        self.assertEqual(phases(events), PHASES)
        self.assertEqual((summary.get('stopped_reason'), summary['quota_exhausted'], summary['batch_quota_exhausted']),
                         (None, True, True))
        self.assertNotIn('phase_stops', summary)

    def test_clean_video_step_runs_all_steps_without_error(self):
        events, summary = run_pass({'requested': 0, 'updated': 0, 'errors': 0})
        self.assertEqual(phases(events), PHASES)
        self.assertEqual((summary['errors'], summary.get('phase_stops')), (0, None))


class StopTableTests(unittest.TestCase):
    def test_one_table_names_what_each_reason_stops(self):
        self.assertEqual(COLLECTION_STOP_SCOPES, {
            'settlement_unknown': 'pass', 'quota_control_unknown': 'pass',
            'infrastructure_error': 'pass', 'quota_exhausted': 'pass',
            'general_quota_exhausted': 'general', 'batch_quota_exhausted': 'stats',
            'batch_unavailable': 'phase', 'rate_limited': 'phase'})
        from collection_outcome import BUCKET_STOP_FLAGS
        self.assertEqual(set(PASS_STOP_FLAGS) | set(PHASE_STOP_FLAGS) | set(BUCKET_STOP_FLAGS),
                         set(COLLECTION_STOP_FLAGS))
        self.assertEqual(PHASE_STOP_FLAGS, ('batch_unavailable', 'rate_limited'))
        import operator_jobs
        self.assertIs(operator_jobs.COLLECTION_STOP_FLAGS, COLLECTION_STOP_FLAGS)

    def test_reason_order_and_scope_filter(self):
        both = dict(rate_limited=True, batch_unavailable=True, quota_exhausted=True)
        self.assertEqual(collection_stop_reason(both), 'quota_exhausted')
        self.assertEqual(operator_collection_stop_reason(both), 'quota_exhausted')
        self.assertEqual(collection_stop_reason(both, 'phase'), 'batch_unavailable')
        self.assertIsNone(collection_stop_reason(dict(rate_limited=True), 'pass'))
        self.assertIsNone(collection_stop_reason({}))

    def test_every_reason_still_marks_its_work_failed(self):
        for name in COLLECTION_STOP_FLAGS:
            with self.subTest(name=name):
                self.assertEqual(collection_outcome_fields({name: True})['completion_outcome'], 'error')
                self.assertEqual(sync_completion_fields({name: True})['completion_outcome'], 'error')
                target = {}
                self.assertTrue(copy_collection_stop(target, {name: True}))
                self.assertTrue(target[name])


class Feeder:
    """One step's feeder: settles the given unit summaries on its first offer."""
    def __init__(self, window, settled=(), *, exhausted=False):
        self.window, self.settled, self.exhausted = window, list(settled), exhausted
        self.summary, self.paused, self.finished = {'errors': 0}, None, False
        self.stopping_seen = []

    def offer_to(self, owner, *, limit=None):
        self.stopping_seen.append((self.window.stopping(), self.window.pass_stopping()))
        while self.settled:
            self.window.stop_all(self.settled.pop(0))
        return 0

    def pause(self, reason):
        self.paused = reason

    def finish_pass(self, owner):
        self.finished = True


class WindowTests(unittest.TestCase):
    def window(self, *, should_stop=lambda: False):
        self.operator = SimpleNamespace(advance=Mock(), stop=Mock(), resume_idle=Mock(), summary={}, finished=True)
        self.options = {}
        def factory(**options):
            self.options.update(options)
            return self.operator
        window = AutomaticDispatchWindow(concurrency=2, require_ready=lambda: None, should_stop=should_stop,
            operator_factory=factory, executor=ManualExecutor(), clock=Clock())
        self.addCleanup(window.close)
        return window

    def test_step_reason_ends_its_step_but_not_the_next_or_one_off_jobs(self):
        for reason in PHASE_STOP_FLAGS:
            with self.subTest(reason=reason):
                window = self.window()
                stopped = Feeder(window, [{reason: True, 'stopped_reason': reason, 'errors': 1}])
                result = window.run_phase(stopped)
                self.assertEqual((result[reason], result['stopped_reason']), (True, reason))
                self.assertEqual(stopped.paused, reason)
                self.assertTrue(stopped.finished)
                self.assertIsNone(window.stop_reason)
                self.assertTrue(window.stop_flags[reason])
                self.operator.stop.assert_not_called()
                # The next step starts clean, and only the step had stopped.
                following = Feeder(window, exhausted=True)
                result = window.run_phase(following)
                self.assertNotIn(reason, result)
                self.assertNotIn('stopped_reason', result)
                self.assertIsNone(following.paused)
                self.assertEqual(following.stopping_seen[0], (False, False))

    def test_units_drained_by_the_step_stop_are_not_a_handoff(self):
        window = self.window()
        feeder = Feeder(window, [{'rate_limited': True, 'stopped_reason': 'provider_rate_limited'},
                                 {'yield_requested': True, 'stopped_reason': 'collection_stopped'},
                                 {'batch_unavailable': True, 'yield_requested': True}])
        window.run_phase(feeder)
        self.assertIsNone(window.stop_reason)
        self.assertNotIn('yield_requested', window.stop_flags)
        # Automatic work of the stopped step sees a stop; one-off jobs do not.
        self.assertEqual(self.options['should_stop'], window.pass_stopping)
        self.assertEqual(self.options['on_stop'], window.operator_stopped)
        window._feeder, window.phase_stop_reason = feeder, 'rate_limited'
        self.assertEqual((window.stopping(), window.pass_stopping()), (True, False))
        window._feeder = None

    def test_handoff_hot_interruption_and_pass_reasons_stop_the_pass(self):
        cases = (
            (lambda: False, [{'quota_control_unknown': True, 'errors': 1}], 'quota_control_unknown'),
            (lambda: True, [{'rate_limited': True}, {'yield_requested': True}], 'scheduled_handoff'),
            (lambda: False, [{'rate_limited': True}, {'yield_requested': True, 'hot_runtime_interrupted': True}],
             'scheduled_handoff'),
            (lambda: False, [{'rate_limited': True}, {'quota_exhausted': True}], 'quota_exhausted'),
            (lambda: False, [{'yield_requested': True, 'stopped_reason': 'expired'}], 'scheduled_handoff'))
        for should_stop, settled, expected in cases:
            with self.subTest(expected=expected, settled=settled):
                window = self.window(should_stop=should_stop)
                feeder = Feeder(window, settled)
                result = window.run_phase(feeder)
                self.assertEqual((window.stop_reason, result['stopped_reason']), (expected, expected))
                self.operator.stop.assert_called()
                following = Feeder(window, exhausted=True)
                self.assertEqual(window.run_phase(following)['stopped_reason'], expected)
                self.assertEqual(following.paused, expected)

    def test_one_off_job_step_reason_is_recorded_without_stopping_automatic_work(self):
        window = self.window()
        on_stop = self.options['on_stop']
        on_stop({'rate_limited': True, 'batch_unavailable': True, 'stopped_reason': 'rate_limited'})
        self.assertIsNone(window.stop_reason)
        self.assertTrue(window.stop_flags['rate_limited'])
        feeder = Feeder(window, exhausted=True)
        result = window.run_phase(feeder)
        self.assertNotIn('rate_limited', result)
        self.assertIsNone(feeder.paused)
        on_stop({'quota_exhausted': True, 'stopped_reason': 'youtube_quota_exhausted'})
        self.assertEqual(window.stop_reason, 'quota_exhausted')


class WaitHelperTests(unittest.TestCase):
    def test_no_installation_gate_means_no_wait(self):
        with patch.object(provider_runtime, '_runtime', None):
            self.assertEqual(provider_runtime.wait_for_rate_limit_pause(Mock(side_effect=AssertionError)), 0)

    def test_unreadable_gate_is_left_to_the_next_request(self):
        from provider_admission import AdmissionUnavailable
        gate = SimpleNamespace(pause_seconds=Mock(side_effect=AdmissionUnavailable('provider_admission_unavailable')))
        with patch.object(provider_runtime, '_runtime', SimpleNamespace(gate=gate)):
            provider_runtime.wait_for_rate_limit_pause(Mock(side_effect=AssertionError))

    def test_gate_reports_the_remaining_pause(self):
        import tempfile
        import provider_admission
        from test_provider_admission import BOOT, GENERATION, IDENTITY, Clock as GateClock, ThreadLockSimulator
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        clock = GateClock()
        gate = provider_admission.AdmissionGate.initialize(Path(folder.name) / 'gate',
            provider_admission.AdmissionConfig(installation_id=IDENTITY, generation=GENERATION, boot_id=BOOT,
                                               concurrency=2), _locks=ThreadLockSimulator(), _clock=clock)
        self.assertEqual(gate.pause_seconds(), 0.0)
        gate.set_cooldown(20)
        self.assertEqual(gate.pause_seconds(), 20.0)
        clock.value += 15
        self.assertEqual(gate.pause_seconds(), 5.0)
        self.assertTrue(gate.snapshot()['cooling_down'])
        clock.value += 6
        self.assertEqual(gate.pause_seconds(), 0.0)
        self.assertFalse(gate.snapshot()['cooling_down'])


if __name__ == '__main__':
    unittest.main()
