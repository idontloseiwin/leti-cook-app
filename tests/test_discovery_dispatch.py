"""Real discovery/coordinator threads; synthetic provider and durable owners."""
from collections import deque
import ast
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from collection.cycle import AutomaticDispatchWindow
from collection.discovery_feeder import DiscoveryFeeder
from collection.dispatch import Ack, Prepared, Work
from collection.provider_steps import ProviderStep
from db.base.postgres import DatabaseUnavailable
from discovery_policy import DiscoverySettings
from discovery_service import DiscoveryService
from youtube_errors import YouTubeRateLimitError

CHANNEL = 'UC' + 'd' * 22


class Clock:
    def __init__(self): self.now = 10.0
    def __call__(self): return self.now


class OwnedService:
    """Owner callbacks assert affinity; provider callbacks record actual threads."""
    def __init__(self, units=(), *, stop=None, uncertain=False):
        self.owner = get_ident()
        self.pending = deque(units)
        self.active_claims = ()
        self.provider_threads, self.receipts, self.events = [], [], []
        self.stop, self.uncertain = stop, uncertain
        self.claims = []

    def check_owner(self):
        assert get_ident() == self.owner

    def steps(self, *, origin, limit):
        self.check_owner()
        assert limit <= 50
        if origin != 'websub' or not self.pending:
            return dict(claimed=0, settled=0)
        identity, count = self.pending.popleft()
        self.active_claims = (identity,)
        self.claims.append(identity)
        for index in range(count):
            def fetch(ids, reason, number=index):
                assert get_ident() != self.owner
                self.provider_threads.append(get_ident())
                self.events.append(('discovery', identity, number))
                if self.stop is not None: self.stop.set()
                return {'identity': identity, 'step': number}
            response = yield ProviderStep(fetch, (identity,), 'discovery_metadata', channel=CHANNEL)
            self.check_owner()
            response.unwrap()
            if self.uncertain:
                raise DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
            self.receipts.append((identity, index))
        self.active_claims = ()
        return dict(claimed=1, settled=1)

    def hydration_steps(self, **options):
        self.check_owner()
        if False: yield
        return dict(claimed=0, next_after=None)


class ManualTurn:
    def __init__(self, *, coordinator, should_stop, require_ready, on_stop, count=0, events=None):
        self.coordinator, self.should_stop = coordinator, should_stop
        self.owner, self.remaining = get_ident(), count
        self.events = events if events is not None else []
        self.key = None
        self.stopped = False
        self.summary = dict(chunks_processed=0)

    @property
    def finished(self): return (self.stopped or not self.remaining) and self.key is None

    def advance(self, turn=None, *, ready_limit=0):
        assert get_ident() == self.owner
        if self.stopped or self.should_stop() or not self.remaining or self.key or not ready_limit: return
        key = self.key = 'manual:' + str(self.remaining)
        def prepare():
            assert get_ident() == self.owner
            def fetch(cancel):
                assert get_ident() != self.owner
                self.events.append(('manual', key))
                return None
            return Prepared(fetch, 100.0)
        def settle(completion):
            assert get_ident() == self.owner
            self.remaining -= 1
            self.summary['chunks_processed'] += 1
            self.key = None
            return Ack()
        self.coordinator.offer(Work(key, 'operator', 'manual-channel', 'long', prepare, settle))

    def stop(self, reason, **flags): self.stopped = True
    def resume_idle(self): return False


class DispatchTests(unittest.TestCase):
    def window(self, service, *, stop=None, manual=0, concurrency=2):
        self.clock = Clock()
        self.revision = 0
        self.released = []
        def factory(**options):
            self.manual = ManualTurn(**options, count=manual, events=service.events)
            return self.manual
        window = AutomaticDispatchWindow(concurrency=concurrency, require_ready=service.check_owner,
            should_stop=(stop.is_set if stop is not None else lambda: False),
            operator_factory=factory, clock=self.clock, wake_revision=lambda: self.revision)
        def release(rows):
            service.check_owner()
            self.released.append(tuple(rows))
        feeder = DiscoveryFeeder(service, deadline=100, require_ready=service.check_owner,
            should_stop=window.pass_stopping, clock=self.clock, release_claims=release,
            on_settled=window.operator_stopped)
        window.discovery = feeder
        self.addCleanup(window.close)
        return window, feeder

    def test_real_worker_threads_keep_claims_receipts_and_release_on_owner(self):
        service = OwnedService([('a', 3), ('b', 2)])
        window, feeder = self.window(service)
        summary = window.run_phase(feeder)
        window.close()
        self.assertEqual(summary['settled'], 2)
        self.assertEqual(service.receipts, [('a', 0), ('a', 1), ('a', 2), ('b', 0), ('b', 1)])
        self.assertTrue(service.provider_threads)
        self.assertNotIn(service.owner, service.provider_threads)
        self.assertEqual(window.coordinator.keys(), ())
        self.assertTrue(window.checkpoint_ready)

    def test_manual_lane_progresses_between_discovery_provider_steps(self):
        service = OwnedService([('burst', 8)])
        window, feeder = self.window(service, manual=3, concurrency=1)
        window.run_phase(feeder, wait_operator=True)
        self.assertEqual(self.manual.summary['chunks_processed'], 3)
        kinds = [event[0] for event in service.events]
        self.assertEqual(kinds.count('discovery'), 8)
        self.assertLess(kinds.index('manual'), 3)
        self.assertEqual(feeder.summary['settled'], 1)

    def test_late_wake_reopens_eof_with_same_deadline_and_executor(self):
        service = OwnedService()
        window, feeder = self.window(service)
        window.run_phase(feeder)
        self.assertTrue(feeder.exhausted)
        executor = window.executor
        service.pending.append(('late', 1))
        self.revision += 1
        self.clock.now += 3
        window.run_phase(feeder)
        self.assertEqual(service.receipts, [('late', 0)])
        self.assertEqual(feeder.deadline, 100)
        self.assertIs(window.executor, executor)

    def test_lost_wake_periodic_probe_reopens_eof_without_new_deadline(self):
        service = OwnedService()
        window, feeder = self.window(service)
        window.run_phase(feeder)
        service.pending.append(('lost', 1))
        self.clock.now += 60
        window.run_phase(feeder)
        self.assertEqual(service.receipts, [('lost', 0)])
        self.assertEqual(feeder.deadline, 100)

    def test_warm_child_wait_is_bounded_and_reuses_original_wake_cadence(self):
        service = OwnedService()
        window, feeder = self.window(service)
        window.run_phase(feeder)
        waits = []
        def wait(seconds):
            waits.append(seconds)
            self.clock.now += seconds
            self.revision += 1
        with patch('collection.cycle.time.sleep', side_effect=wait):
            self.assertTrue(window.wait_for_wake(100))
        self.assertEqual(waits, [3])
        self.assertEqual(feeder.deadline, 100)
        self.assertEqual(service.provider_threads, [])

    def test_warm_child_deadline_stops_wait_without_budget_or_clock_renewal(self):
        service = OwnedService()
        window, feeder = self.window(service)
        window.run_phase(feeder)
        waits = []
        def wait(seconds):
            waits.append(seconds)
            self.clock.now += seconds
        with patch('collection.cycle.time.sleep', side_effect=wait):
            self.assertFalse(window.wait_for_wake(20))
        self.assertEqual(waits, [3, 3, 3, 1])
        self.assertEqual(self.clock.now, 20)
        self.assertEqual(service.claims, [])

    def test_handoff_settles_known_response_and_releases_only_unfinished_claim(self):
        stop = Event()
        service = OwnedService([('partial', 2)], stop=stop)
        window, feeder = self.window(service, stop=stop)
        result = window.run_phase(feeder)
        window.close()
        self.assertEqual(len(service.provider_threads), 1)
        self.assertEqual(service.receipts, [('partial', 0)])
        self.assertEqual(result['settled'], 0)
        self.assertIn(('partial',), self.released)
        self.assertEqual(window.coordinator.keys(), ())

    def test_unknown_writer_receipt_keeps_fence_and_never_replays_or_releases(self):
        service = OwnedService([('uncertain', 1)], uncertain=True)
        window, feeder = self.window(service)
        result = window.run_phase(feeder)
        window.close()
        self.assertTrue(result['settlement_unknown'])
        self.assertTrue(window.coordinator.snapshot().blocked)
        self.assertEqual(service.active_claims, ('uncertain',))
        self.assertEqual(service.claims, ['uncertain'])
        self.assertEqual(len(service.provider_threads), 1)
        self.assertEqual(self.released, [])
        self.assertFalse(window.checkpoint_ready)

    def test_expired_portion_cannot_reopen_on_wake_or_claim_new_work(self):
        service = OwnedService()
        window, feeder = self.window(service)
        window.run_phase(feeder)
        service.pending.append(('after-deadline', 1))
        self.clock.now = 100
        self.revision += 1
        window.run_phase(feeder)
        self.assertEqual(service.claims, [])
        self.assertEqual(service.provider_threads, [])

    def test_real_service_429_defers_frozen_rows_and_stops_discovery_phase(self):
        owner = get_ident()
        rows = [dict(video_id='late4290001', channel_id=CHANNEL, work_id='1',
            kind='entry', work_phase='metadata_wait', discovery_attempts=0)]
        repo, video, budget = Mock(), Mock(), Mock()
        repo.claim_event_batch.return_value = rows
        repo.target_state.return_value = {'1': dict(queue_exists=False, video_type=None)}
        budget.reserve.return_value = dict(allowed=True)
        budget.begin_dispatch.return_value = True
        def fetch(ids, reason):
            self.assertNotEqual(get_ident(), owner)
            raise YouTubeRateLimitError(retry_after_seconds=30)
        video.fetch_metadata.side_effect = fetch
        service = DiscoveryService(repo, video, budget, settings=DiscoverySettings(),
            fetch_membership=Mock(), worker_id='fixture', run_id=str(uuid4()))
        service.events = []
        service.check_owner = lambda: self.assertEqual(get_ident(), owner)
        window, feeder = self.window(service)
        result = window.run_phase(feeder)
        self.assertTrue(result['rate_limited'])
        self.assertTrue(window.stop_flags['rate_limited'])
        self.assertIsNone(window.stop_reason)
        video.fetch_metadata.assert_called_once()
        self.assertEqual(repo.defer.call_args.args[0], rows)
        repo.acknowledge.assert_not_called()


class NarrowLoopTests(unittest.TestCase):
    def narrow(self, namespace):
        # Execute the real nested composition loop in isolation from bootstrap,
        # retaining its imports and call contract. Provider/lease adapters have
        # their own real-coordinator tests above.
        source = ROOT / 'application/runtime/process_collection.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        parent = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'bind_process_common_dispatch')
        function = next(node for node in parent.body if isinstance(node, ast.FunctionDef) and node.name == 'narrow')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), str(source), 'exec'), namespace)
        return namespace['narrow']

    def test_late_count_and_full_source_after_eof_reuse_fixed_window_without_ordinary_sweep(self):
        from self_hosted_settings import browser_environment
        from test_collector_handoff import settings
        events, pending = [], deque(['first-count-head'])
        budget = SimpleNamespace(settings=DiscoverySettings(), project='fixture', interval=30)
        discovery_feeder = SimpleNamespace(deadline=100.0, summary={'errors': 0})
        active = SimpleNamespace(discovery=discovery_feeder, operator=SimpleNamespace(summary={}),
                                 pass_stopping=lambda: False)
        def inventory(**options):
            self.assertIs(options['signals_only'], True)
            self.assertEqual(options['limit'], settings()['limits']['inventory_page_budget'])
            events.append(('inventory', tuple(pending)))
            pending.clear()
            return {'errors': 0}
        waits = []
        def wait(deadline):
            waits.append(deadline)
            if len(waits) == 1:
                pending.extend(('late-count-head', 'late-full-continuation'))
                return True
            return False
        active.wait_for_wake = wait
        namespace = dict(budget=budget, window=lambda: active, _inventory=inventory,
            discovery=lambda **options: events.append(('discovery', options['wait_operator'])) or {'errors': 0},
            uuid4=uuid4, configuration=browser_environment(settings()), context=SimpleNamespace(database=object()),
            _policy_formats=lambda context: ('long', 'short'))
        with patch('db.discovery.DiscoveryRepository') as repository:
            repository.return_value.runtime_status.return_value = {'runnable': False}
            result = self.narrow(namespace)()
        self.assertEqual(events, [('inventory', ('first-count-head',)), ('discovery', True),
            ('inventory', ('late-count-head', 'late-full-continuation')), ('discovery', True)])
        self.assertEqual(waits, [100.0, 100.0])
        self.assertEqual(result['errors'], 0)
        self.assertIs(namespace['budget'], budget)

    def test_exhausted_discovery_stops_portion_without_wait_or_source_replay(self):
        from self_hosted_settings import browser_environment
        from test_collector_handoff import settings
        feeder = SimpleNamespace(deadline=100.0, summary={'errors': 0})
        active = SimpleNamespace(discovery=feeder, operator=SimpleNamespace(summary={}),
            pass_stopping=lambda: False, wait_for_wake=Mock(side_effect=AssertionError('busy waiting')))
        inventory = Mock(return_value={'errors': 0})
        def discovery(**options):
            feeder.summary['stopped_reason'] = 'provider_rate_limited'
            return feeder.summary
        namespace = dict(budget=SimpleNamespace(settings=DiscoverySettings()), window=lambda: active,
            _inventory=inventory, discovery=discovery, uuid4=uuid4,
            configuration=browser_environment(settings()))
        self.narrow(namespace)()
        inventory.assert_called_once()
        active.wait_for_wake.assert_not_called()

    def test_runnable_advice_without_claim_progress_waits_instead_of_spinning(self):
        from self_hosted_settings import browser_environment
        from test_collector_handoff import settings
        feeder = SimpleNamespace(deadline=100.0, summary={'errors': 0, 'units': 0}, resume_idle=Mock())
        stops = []
        def stopping():
            stops.append(True)
            return len(stops) > 20  # Safety bound only if the regression spins.
        active = SimpleNamespace(discovery=feeder, operator=SimpleNamespace(summary={}),
            pass_stopping=stopping, wait_for_wake=Mock(return_value=False))
        inventory = Mock(return_value={'errors': 0, 'claimed': 0, 'pages_processed': 0})
        namespace = dict(budget=SimpleNamespace(settings=DiscoverySettings(), project='fixture', interval=30),
            window=lambda: active, _inventory=inventory, discovery=lambda **options: feeder.summary, uuid4=uuid4,
            configuration=browser_environment(settings()), context=SimpleNamespace(database=object()),
            _policy_formats=lambda context: ('long', 'short'))
        with patch('db.discovery.DiscoveryRepository') as repository:
            repository.return_value.runtime_status.return_value = {'runnable': True}
            self.narrow(namespace)()
        inventory.assert_called_once()
        active.wait_for_wake.assert_called_once_with(100.0)


if __name__ == '__main__':
    unittest.main()
