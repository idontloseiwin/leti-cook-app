"""Offline supervisor/ingress integration; fake processes and monotonic time."""
from datetime import datetime, timezone
from pathlib import Path
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from collection_schedule import CollectionSchedules
from discovery_policy import DiscoverySettings
from self_hosted_collection import (CollectionSupervisor, DISCOVERY_ALLOWED_ENV,
    DISCOVERY_DEADLINE_ENV, COLLECTION_DEADLINE_ENV, TERMINATE_GRACE_SECONDS)
from ops.run_discovery_worker import collection_deadline, portion_deadline, run
from test_collector_handoff import Clock, Process, settings


class DiscoverySupervisorTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.wall = Clock(), Clock()
        self.value = dict(prepared=True, phase='ready', settings=settings())
        self.manager = types.SimpleNamespace(lock=threading.RLock(),
            store=types.SimpleNamespace(read=lambda: self.value))
        self.starts = []
        self.probe = Mock(return_value=dict(enabled=True, runnable=True, next_retry=None))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.wake_path = Path(self.directory.name, 'wake')
        env = patch.dict(os.environ, {'OPERATOR_JOB_WAKE_PATH': str(self.wake_path)})
        env.start(); self.addCleanup(env.stop)
        def spawn(args, **options):
            process = Process()
            self.starts.append((args[-1], options, process))
            return process
        self.supervisor = CollectionSupervisor(self.manager, ready=lambda: None,
            schedules=CollectionSchedules(discovery=DiscoverySettings()), spawn=spawn,
            clock=self.clock, wall_clock=self.wall, discovery_probe=self.probe)
        self.supervisor.tick()
        self.auto = self.starts[0][2]
        self.boundary = self.supervisor.next_auto

    def finish_auto(self):
        self.auto.code = 0
        self.clock.now += 10
        self.supervisor.next_operator = self.boundary
        self.supervisor.tick()
        return self.starts[-1][2]

    def wake(self, revision):
        self.wake_path.write_bytes(b'd')
        os.utime(self.wake_path, ns=(revision * 1_000_000, revision * 1_000_000))

    def test_startup_and_lost_wake_probe_durable_work(self):
        self.finish_auto()
        self.assertEqual([row[0] for row in self.starts], ['auto', 'discovery'])
        self.assertFalse(self.wake_path.exists())
        self.probe.assert_called_once_with(interval_minutes=30)
        self.assertEqual(self.supervisor.next_auto, self.boundary)

    def test_ordinary_auto_transfers_the_same_fixed_parent_boundary(self):
        env = self.starts[0][1]['env']
        self.assertEqual(float(env[COLLECTION_DEADLINE_ENV]), self.boundary)
        self.assertEqual(collection_deadline(env, clock=self.clock), self.boundary)

    def test_active_burst_coalesces_without_second_child_or_deadline_extension(self):
        child = self.finish_auto()
        deadline = self.supervisor.deadline
        for revision in range(1, 15):
            self.wake(revision)
            self.clock.now += 3
            self.supervisor.tick()
        self.assertEqual(len(self.starts), 2)
        self.assertIs(self.supervisor.process, child)
        self.assertEqual(self.supervisor.deadline, deadline)
        self.assertEqual(self.supervisor.next_auto, self.boundary)
        self.assertEqual(self.starts[-1][1]['env'][DISCOVERY_DEADLINE_ENV], str(deadline))

    def test_supplement_reserves_existing_grace_and_auto_starts_after_confirmed_exit(self):
        self.clock.now = self.boundary - 300
        child = self.finish_auto()
        expected = self.boundary - 120 - TERMINATE_GRACE_SECONDS
        self.assertEqual(self.supervisor.deadline, expected)
        self.clock.now = expected
        self.supervisor.tick()
        self.assertEqual(child.stdin.commands, [b'stop\n'])
        self.clock.now = self.boundary
        self.supervisor.tick()
        self.assertEqual(len(self.starts), 2)
        child.code = 0
        # Discovery never earned a manual turn: an overdue manual starvation
        # slice runs without discovery before ordinary auto is admitted.
        self.supervisor.tick()
        self.assertEqual(self.starts[-1][0], 'operator')
        self.assertEqual(self.starts[-1][1]['env'][DISCOVERY_ALLOWED_ENV], '0')
        self.assertNotIn(DISCOVERY_DEADLINE_ENV, self.starts[-1][1]['env'])
        self.starts[-1][2].code = 0
        self.supervisor.tick()
        self.assertEqual(self.starts[-1][0], 'auto')

    def test_no_new_discovery_portion_near_scheduled_boundary(self):
        self.auto.code = 0
        self.supervisor.next_operator = self.boundary
        self.clock.now = self.boundary - 120
        self.supervisor.tick()
        self.assertEqual(len(self.starts), 1)
        self.probe.assert_not_called()

    def test_import_time_consumes_the_fixed_granted_portion(self):
        original = self.supervisor.spawn
        def delayed(*args, **options):
            process = original(*args, **options)
            self.clock.now += 25
            return process
        self.supervisor.spawn = delayed
        self.finish_auto()
        self.assertEqual(self.supervisor.deadline, 1010 + self.value['settings']['limits']['drain_max_seconds'])
        self.assertEqual(float(self.starts[-1][1]['env'][DISCOVERY_DEADLINE_ENV]), self.supervisor.deadline)

    def test_spent_budget_suppresses_wakes_until_persisted_retry(self):
        child = self.finish_auto()
        child.code = 0
        self.probe.return_value = dict(enabled=True, runnable=False, budget_wait=True,
            next_retry=datetime.fromtimestamp(self.wall.now + 600, timezone.utc))
        self.wake(1)
        self.supervisor.tick()
        calls = self.probe.call_count
        for revision in range(2, 12):
            self.clock.now += 3
            self.wake(revision)
            self.supervisor.tick()
        self.assertEqual([kind for kind, _, _ in self.starts], ['auto', 'discovery'])
        self.assertEqual(self.probe.call_count, calls)

    def test_manual_hint_still_admits_operator_when_discovery_budget_waits(self):
        child = self.finish_auto()
        child.code = 0
        self.probe.return_value = dict(enabled=True, runnable=False, budget_wait=True,
            next_retry=datetime.fromtimestamp(self.wall.now + 600, timezone.utc))
        self.wake(1)
        self.supervisor.tick()
        self.wake_path.write_bytes(b'1')
        os.utime(self.wake_path, ns=(2_000_000, 2_000_000))
        self.supervisor.tick()
        self.assertEqual(self.starts[-1][0], 'operator')

    def test_active_idle_operator_uses_same_fixed_deadline_and_manual_accounting(self):
        self.probe.return_value = dict(enabled=True, runnable=False, next_retry=None)
        self.auto.code = 0
        self.clock.now = self.boundary - 300
        self.supervisor.tick()
        kind, options, _ = self.starts[-1]
        self.assertEqual(kind, 'operator')
        self.assertEqual(options['env'][DISCOVERY_ALLOWED_ENV], '1')
        self.assertEqual(float(options['env'][DISCOVERY_DEADLINE_ENV]), self.supervisor.deadline)
        self.assertEqual(self.supervisor.deadline, self.boundary - 140)
        self.assertEqual(self.supervisor.last_started['operator'], self.clock.now)
        self.assertEqual(self.supervisor.next_auto, self.boundary)

    def test_inactive_database_never_launches_discovery(self):
        self.probe.return_value = dict(enabled=False, runnable=True)
        self.finish_auto()
        self.assertEqual(len(self.starts), 1)

    def test_v4_snapshot_does_not_probe_or_launch_discovery(self):
        supervisor = CollectionSupervisor(self.manager, ready=lambda: None,
            schedules=CollectionSchedules(), spawn=self.supervisor.spawn,
            clock=self.clock, wall_clock=self.wall, discovery_probe=self.probe)
        supervisor.next_auto = supervisor.next_operator = self.boundary
        supervisor.tick()
        self.probe.assert_not_called()

    def test_ready_failure_or_update_hold_does_not_probe(self):
        self.auto.code = 0
        self.supervisor.require_ready = Mock(side_effect=RuntimeError('held'))
        self.supervisor.tick()
        self.probe.assert_not_called()
        self.assertEqual(len(self.starts), 1)


class DiscoveryEntrypointTests(unittest.TestCase):
    def test_child_labels_use_existing_real_provider_admission_scopes(self):
        import provider_runtime
        from test_provider_runtime import Gate
        from ops.self_hosted_collect import _collect
        for kind, expected in (('discovery', 'auto'), ('auto', 'auto'), ('operator', 'operator')):
            with self.subTest(kind=kind):
                gate = Gate()
                provider = provider_runtime._install(gate)
                self.addCleanup(provider_runtime.stop_runtime, provider)
                core = types.ModuleType('core')
                core.COLLECTION_SCHEDULES = CollectionSchedules(discovery=DiscoverySettings())
                value = dict(prepared=True, settings=settings())
                env = {'LIC_SETUP_DIRECTORY': 'fixture-only', 'LIC_COLLECTION_SUPERVISED': '1',
                    'LIC_DISCOVERY_ALLOWED': '1', 'LIC_YOUTUBE_REQUEST_CONCURRENCY': '8',
                    DISCOVERY_DEADLINE_ENV: str(__import__('time').monotonic() + 300)}
                def collect(_value):
                    # Real request_scope and admit_request exercise the finite
                    # provider vocabulary; only the external gate is a double.
                    with provider_runtime.admit_request():
                        return 0
                with patch.dict(sys.modules, {'core': core}), patch.dict(os.environ, env), \
                     patch.object(sys, 'argv', ['worker', kind]), \
                     patch('self_hosted_configuration.ConfigurationStore') as store, \
                     patch('hot_runtime.require_hot_runtime_ready'), \
                     patch('collection_shutdown.handoff_requested', return_value=False), \
                     patch('provider_runtime.start_worker', return_value=provider), \
                     patch('ops.self_hosted_collect._run_collection', side_effect=collect):
                    store.return_value.read.return_value = value
                    self.assertEqual(_collect(), 0)
                self.assertEqual([call[0] for call in gate.calls], [expected])
                self.assertIsNone(provider_runtime._runtime)
        with self.assertRaisesRegex(ValueError, 'provider_admission_kind_invalid'):
            with provider_runtime.request_scope('discovery'):
                self.fail('discovery must not create another provider lane')

    def worker(self, kind, *, active_result=0, allowed=True):
        from ops.self_hosted_collect import _run_collection
        lock = Mock()
        worker = types.ModuleType('scripts.run_operator_job_worker')
        worker.acquire_shared_sync_lock = Mock(return_value=lock)
        def legacy(arguments):
            if allowed:
                lock.close.assert_called_once()
            return 7
        worker.cli = Mock(side_effect=legacy)
        core = types.ModuleType('core')
        core.COLLECTION_SCHEDULES = CollectionSchedules(discovery=DiscoverySettings())
        env = {'LIC_COLLECTION_SUPERVISED': '1', DISCOVERY_ALLOWED_ENV: '1' if allowed else '0',
               DISCOVERY_DEADLINE_ENV: str(__import__('time').monotonic() + 300)}
        with patch.dict(sys.modules, {'scripts.run_operator_job_worker': worker, 'core': core}), \
             patch.dict(os.environ, env), patch.object(sys, 'argv', ['worker', kind]), \
             patch('ops.run_discovery_worker.run', return_value=active_result) as narrow:
            result = _run_collection(dict(settings=settings()))
        return result, worker, narrow, lock

    def test_idle_operator_uses_shared_narrow_window_without_ordinary_auto(self):
        result, worker, narrow, lock = self.worker('operator')
        self.assertEqual(result, 0)
        narrow.assert_called_once_with(operator_fallback=True)
        worker.cli.assert_not_called()
        lock.close.assert_called_once()

    def test_inactive_operator_fallback_releases_lock_before_legacy_admission(self):
        result, worker, narrow, lock = self.worker('operator', active_result=None)
        self.assertEqual(result, 7)
        narrow.assert_called_once_with(operator_fallback=True)
        worker.cli.assert_called_once()
        lock.close.assert_called_once()

    def test_starved_operator_never_enters_discovery_window(self):
        result, worker, narrow, lock = self.worker('operator', allowed=False)
        self.assertEqual(result, 7)
        narrow.assert_not_called()
        worker.acquire_shared_sync_lock.assert_not_called()

    def test_discovery_kind_only_uses_narrow_entrypoint(self):
        result, worker, narrow, lock = self.worker('discovery')
        self.assertEqual(result, 0)
        narrow.assert_called_once_with(operator_fallback=False)
        worker.cli.assert_not_called()
        lock.close.assert_called_once()

    def test_expired_portion_and_manual_starvation_cannot_import_runtime(self):
        env = {'LIC_COLLECTION_SUPERVISED': '1', DISCOVERY_ALLOWED_ENV: '1', DISCOVERY_DEADLINE_ENV: '100'}
        self.assertEqual(run(environ=env, clock=lambda: 100), 0)
        self.assertIsNone(portion_deadline({**env, DISCOVERY_ALLOWED_ENV: '0'}, clock=lambda: 1))

    def test_deadline_is_fixed_and_invalid_values_fail_closed(self):
        env = {'LIC_COLLECTION_SUPERVISED': '1', DISCOVERY_ALLOWED_ENV: '1'}
        for value in ('NaN', 'inf', '-1', 'bad'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'discovery_deadline_invalid'):
                portion_deadline({**env, DISCOVERY_DEADLINE_ENV: value}, clock=lambda: 1)
        self.assertEqual(portion_deadline({**env, DISCOVERY_DEADLINE_ENV: '900'}, clock=lambda: 50), 900)

    def test_unsupervised_or_old_auto_has_no_discovery_allowance(self):
        self.assertIsNone(collection_deadline({}))
        self.assertIsNone(collection_deadline({'LIC_COLLECTION_SUPERVISED': '1', DISCOVERY_ALLOWED_ENV: '1'}))
        env = {'LIC_COLLECTION_SUPERVISED': '1', DISCOVERY_ALLOWED_ENV: '1', COLLECTION_DEADLINE_ENV: '900'}
        self.assertEqual(collection_deadline(env, clock=lambda: 50), 900)
        self.assertIsNone(collection_deadline(env, clock=lambda: 900))


if __name__ == '__main__':
    unittest.main()
