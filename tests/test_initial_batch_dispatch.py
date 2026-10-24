"""Actual initial batch adapter with deterministic provider/atomic DB doubles."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
import sys
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
sys.path.insert(0, str(ROOT / 'tests'))
from collection.dispatch import Ack, Completion, Coordinator, DispatchBlocked
from collection.initial_video_hydration import InitialBatchUnit
from collection import initial_video_hydration as initial
import collection_shutdown
from test_initial_video_parallel import AtomicRepository, CHANNEL, IN_TRANSACTION, service, video


class InitialBatchDispatchTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(collection_shutdown, '_requested', Event())
        gate.start()
        self.addCleanup(gate.stop)
        self.repo = AtomicRepository()
        self.provider_threads = []
        def fetch(ids, phase):
            self.assertFalse(IN_TRANSACTION.get())
            self.assertNotEqual(get_ident(), self.repo.owner)
            self.provider_threads.append(get_ident())
            return [video(value) for value in ids]
        self.service = service(self.repo, fetch)
        self.progress = Mock()
        self.ready = Mock()

    def unit(self, ids=('abcdefghijk',), **kwargs):
        return InitialBatchUnit(self.service, key='initial:' + ids[0], job_id='job',
            channel_id=CHANNEL, video_ids=ids, worker_id='worker', run_id='run',
            require_ready=self.ready, on_settled=self.progress, **kwargs)

    def complete(self, unit):
        prepared = unit.prepare()
        with ThreadPoolExecutor(max_workers=1) as executor:
            value = executor.submit(prepared.fetch, Event()).result(timeout=5)
        return unit.settle(Completion('response', value=value, fetch_started=True))

    def test_offer_only_has_ids_claim_and_atomic_ack_stay_on_owner(self):
        unit = self.unit()
        self.assertEqual(self.repo.claims, [])
        self.ready.assert_not_called()
        self.assertIsInstance(self.complete(unit), Ack)
        self.ready.assert_called_once()
        self.assertEqual(len(self.repo.claims), 1)
        self.assertEqual(len(self.repo.commits), 1)
        self.assertEqual(unit.phase, 'acknowledged')
        self.progress.assert_called_once()
        self.assertEqual(self.progress.call_args.args[0]['updated'], 1)
        self.service.forbidden.assert_not_called()

    def test_general_missing_count_uses_existing_single_batch_fallback(self):
        original = self.service.fetch_initial_profiles
        def missing(ids, reason):
            values = original(ids, reason)
            for value in values:
                value['statistics'] = {}
            return values
        self.service.fetch_initial_profiles = missing
        def counters(ids, reason):
            self.assertNotEqual(get_ident(), self.repo.owner)
            return {'items': [{'id': value, 'views': 123, 'like_count': 7,
                               'comment_count': 2} for value in ids],
                    'summary': {'failed_ids': []}}
        self.service.fetch_counters = Mock(side_effect=counters)
        unit = self.unit()
        self.assertIsInstance(self.complete(unit), Ack)
        self.service.fetch_counters.assert_called_once()
        self.assertEqual(len(self.repo.commits), 1)
        self.assertEqual(unit.summary['updated'], 1)

    def test_cancelled_or_expired_unsent_unit_releases_without_provider(self):
        for phase in ('cancelled', 'expired'):
            with self.subTest(phase=phase):
                unit = self.unit()
                unit.prepare()
                with patch.object(initial, '_drain_unsent', side_effect=self.repo.release):
                    self.assertIsInstance(unit.settle(Completion(phase)), Ack)
                self.assertEqual(unit.phase, 'released')
        self.assertEqual(self.provider_threads, [])
        self.assertEqual(self.repo.commits, [])
        self.progress.assert_not_called()

    def test_cancel_before_actual_fetch_returns_no_dispatch_response(self):
        unit = self.unit()
        prepared = unit.prepare()
        cancelled = Event()
        cancelled.set()
        response = prepared.fetch(cancelled)
        self.assertFalse(response.dispatched)
        with patch.object(initial, '_drain_unsent', side_effect=self.repo.release):
            self.assertIsInstance(unit.settle(Completion('response', value=response)), Ack)
        self.assertEqual(self.provider_threads, [])

    def test_unknown_commit_never_acknowledged_or_replayed(self):
        self.repo.commit_hook = Mock(side_effect=RuntimeError('unknown commit'))
        unit = self.unit()
        with self.assertRaisesRegex(RuntimeError, 'initial_batch_settlement_unknown'):
            self.complete(unit)
        self.assertEqual(unit.phase, 'settlement_unknown')
        self.assertEqual(len(self.repo.attempts), 1)
        self.progress.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.settle(Completion('expired'))
        self.assertEqual(len(self.repo.attempts), 1)

    def test_progress_ack_failure_does_not_rewrite_confirmed_video_batch(self):
        self.progress.side_effect = RuntimeError('unknown progress acknowledgement')
        unit = self.unit()
        with self.assertRaisesRegex(RuntimeError, 'unknown progress'):
            self.complete(unit)
        self.assertEqual(unit.phase, 'progress')
        self.assertEqual(len(self.repo.commits), 1)
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.settle(Completion('expired'))
        self.progress.assert_called_once()

    def test_claim_ack_failure_is_not_retried(self):
        self.repo.claim_initial_video_ids = Mock(side_effect=RuntimeError('unknown claim'))
        unit = self.unit()
        with self.assertRaisesRegex(RuntimeError, 'unknown claim'):
            unit.prepare()
        with self.assertRaisesRegex(RuntimeError, 'not_repeatable'):
            unit.prepare()
        self.repo.claim_initial_video_ids.assert_called_once()
        self.assertEqual(self.provider_threads, [])

    def test_coordinator_keeps_credit_after_unknown_atomic_commit(self):
        self.repo.commit_hook = Mock(side_effect=RuntimeError('unknown commit'))
        unit = self.unit()
        with ThreadPoolExecutor(max_workers=1) as executor:
            coordinator = Coordinator(executor, concurrency=1)
            coordinator.offer(unit.work)
            self.assertEqual(coordinator.step().submitted, 1)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    coordinator.step()
                except DispatchBlocked:
                    break
                coordinator.wait(0.01)
            self.assertTrue(coordinator.snapshot().blocked)
            self.assertEqual(coordinator.snapshot().held, 1)
            self.assertEqual(coordinator.snapshot().unacked, 1)
            with self.assertRaises(DispatchBlocked):
                coordinator.step()
        self.assertEqual(len(self.repo.attempts), 1)
        self.progress.assert_not_called()

    def test_shared_coordinator_can_finish_small_batch_while_earlier_one_waits(self):
        slow = Event()
        fast_done = Event()
        original = self.service.fetch_initial_profiles
        def fetch(ids, reason):
            if ids == ['aaaaaaaaaaa']:
                if not slow.wait(5):
                    raise AssertionError('test gate timed out')
            else:
                fast_done.set()
            return original(ids, reason)
        self.service.fetch_initial_profiles = fetch
        first, second = self.unit(('aaaaaaaaaaa',)), self.unit(('bbbbbbbbbbb',))
        with ThreadPoolExecutor(max_workers=2) as executor:
            coordinator = Coordinator(executor, concurrency=2)
            coordinator.offer(first.work)
            coordinator.offer(second.work)
            try:
                self.assertEqual(coordinator.step().submitted, 2)
                self.assertTrue(fast_done.wait(5))
                deadline = time.monotonic() + 5
                while second.phase != 'acknowledged' and time.monotonic() < deadline:
                    coordinator.step()
                    coordinator.wait(0.01)
                self.assertEqual(second.phase, 'acknowledged')
                self.assertEqual(first.phase, 'prepared')
                self.assertEqual(coordinator.snapshot().held, 1)
            finally:
                slow.set()
            deadline = time.monotonic() + 5
            while coordinator.snapshot().held and time.monotonic() < deadline:
                coordinator.step()
                coordinator.wait(0.01)
            self.assertEqual(coordinator.snapshot().held, 0)
        self.assertEqual(len(self.repo.commits), 2)

    def test_batch_bound_rejected_before_any_claim(self):
        for ids in ((), ('a', 'a'), ([],), tuple(str(i) for i in range(51))):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                InitialBatchUnit(self.service, key='bad', job_id='job', channel_id=CHANNEL,
                    video_ids=ids, worker_id='worker', run_id='run', require_ready=self.ready,
                    on_settled=self.progress)
        self.assertEqual(self.repo.claims, [])


if __name__ == '__main__':
    unittest.main()
