"""Bounded sweep progress with real coordinator/unit and controlled Futures."""
from pathlib import Path
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import collection_shutdown
from collection.dispatch import Coordinator, DispatchBlocked
from collection.reconnect_feeder import ReconnectFeeder
from channel_reconnect_outcome import ReconnectResponse
from db.user_work.user_work_reconnect import ReconnectCandidateCursor
from test_collection_dispatch import ManualExecutor, Clock
from test_reconnect_dispatch import Repository, CHANNEL

STAMP = '2026-10-10T00:00:00.000000+00:00'


def identity(number):
    return f'a0000000-0000-4000-8000-{number:012d}'


class CandidateRepository(Repository):
    def __init__(self, count, eligible):
        super().__init__()
        self.population = [identity(i) for i in range(1, count + 1)]
        self.eligible = set(eligible)
        self.pages = []

    def reconnect_candidate_page(self, **request):
        self.pages.append(request)
        after = request['after']
        physical = [key for key in self.population if after is None or key > after.id][:request['limit']]
        cursor = ReconnectCandidateCursor(None, STAMP, physical[-1]) if physical else after
        return dict(candidates=[{'id': key} for key in physical if key in self.eligible],
                    next_after=cursor, ceiling=STAMP, inspected=len(physical),
                    exhausted=len(physical) < request['limit'])

    def claim_reconnect(self, **kwargs):
        self.claim['id'] = kwargs['identity']
        return super().claim_reconnect(**kwargs)

    def bind_reconnect(self, *args, **kwargs):
        result = super().bind_reconnect(*args, **kwargs)
        self.eligible.discard(args[0])
        return result


class ReconnectFeederTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(collection_shutdown, '_requested', Event())
        guard.start()
        self.addCleanup(guard.stop)

    def create(self, repository, concurrency=2):
        feeder = ReconnectFeeder(repository,
            resolve_channel=lambda *args, **kw: ReconnectResponse(channel={'channel_id': CHANNEL}),
            require_ready=lambda: None, require_source=lambda row: None)
        executor = ManualExecutor()
        coordinator = Coordinator(executor, concurrency, clock=Clock())
        return feeder, coordinator, executor

    def drain(self, coordinator, executor):
        coordinator.step()
        for index, (future, *unused) in enumerate(executor.jobs):
            if not future.done():
                executor.run(index)
        coordinator.step()

    def test_ineligible_prefix_advances_without_a_total_channel_cap(self):
        repo = CandidateRepository(501, {identity(501)})
        feeder, coordinator, executor = self.create(repo)
        for unused in range(130):
            feeder.offer_to(coordinator)
            self.drain(coordinator, executor)
            if feeder.exhausted:
                break
        self.assertTrue(feeder.exhausted)
        self.assertEqual(feeder.inspected, 501)
        self.assertEqual([args[0] for name, args, kw in repo.events if name == 'bind'], [identity(501)])
        self.assertTrue(all(page['limit'] <= 4 for page in repo.pages))
        self.assertEqual(len(repo.pages), 126)

    def test_full_ready_queue_stops_candidate_reads_without_private_backlog(self):
        repo = CandidateRepository(20, {identity(i) for i in range(1, 21)})
        feeder, coordinator, executor = self.create(repo)
        self.assertEqual(feeder.offer_to(coordinator), 4)
        self.assertEqual(repo.events, [])
        self.assertEqual(feeder.offer_to(coordinator), 0)
        self.assertEqual(len(repo.pages), 1)
        coordinator.step()
        self.assertEqual(feeder.offer_to(coordinator), 2)
        snapshot = coordinator.snapshot()
        self.assertEqual((snapshot.ready, snapshot.held), (4, 2))
        self.assertEqual(len(repo.pages), 2)
        self.assertEqual([kw['identity'] for name, args, kw in repo.events if name == 'claim'],
                         [identity(1), identity(2)])

    def test_slice_limit_preserves_the_cursor_and_remaining_candidates(self):
        repo = CandidateRepository(5, {identity(i) for i in range(1, 6)})
        feeder, coordinator, executor = self.create(repo)
        self.assertEqual(feeder.offer_to(coordinator, limit=1), 1)
        self.assertEqual(repo.pages[-1]['limit'], 1)
        self.assertFalse(feeder.exhausted)
        self.drain(coordinator, executor)
        self.assertEqual(feeder.offer_to(coordinator, limit=2), 2)
        self.assertEqual(repo.pages[-1]['after'].id, identity(1))
        self.assertEqual(coordinator.snapshot().ready, 2)

    def test_zero_slice_budget_does_not_read_or_advance(self):
        repo = CandidateRepository(5, {identity(1)})
        feeder, coordinator, executor = self.create(repo)
        self.assertEqual(feeder.offer_to(coordinator, limit=0), 0)
        self.assertEqual(repo.pages, [])
        self.assertIsNone(feeder.after)
        self.assertEqual(feeder.inspected, 0)

    def test_invalid_slice_limits_fail_before_repository_reads(self):
        repo = CandidateRepository(5, {identity(1)})
        feeder, coordinator, executor = self.create(repo)
        for limit in (-1, 101, True, 1.0, '1'):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, 'limit_invalid'):
                    feeder.offer_to(coordinator, limit=limit)
        self.assertEqual(repo.pages, [])

    def test_new_sweep_cannot_reset_prefix_and_deduplicates_resident_identity(self):
        repo = CandidateRepository(1, {identity(1)})
        feeder, coordinator, executor = self.create(repo)
        with self.assertRaisesRegex(RuntimeError, 'not_exhausted'):
            feeder.begin_sweep()
        self.assertEqual(feeder.offer_to(coordinator), 1)
        feeder.begin_sweep()
        self.assertEqual(feeder.offer_to(coordinator), 0)
        self.assertEqual(coordinator.snapshot().ready, 1)
        self.drain(coordinator, executor)
        self.assertEqual(len([r for r in repo.events if r[0] == 'claim']), 1)
        repo.population.append(identity(2))
        repo.eligible.add(identity(2))
        feeder.begin_sweep()
        self.assertEqual(feeder.offer_to(coordinator), 1)
        self.drain(coordinator, executor)
        self.assertEqual([args[0] for name, args, kw in repo.events if name == 'bind'],
                         [identity(1), identity(2)])

    def test_unknown_claim_blocks_reread_reset_and_provider(self):
        repo = CandidateRepository(1, {identity(1)})
        repo.unknown = 'claim'
        feeder, coordinator, executor = self.create(repo)
        feeder.offer_to(coordinator)
        with self.assertRaises(DispatchBlocked):
            coordinator.step()
        self.assertEqual(feeder.offer_to(coordinator), 0)
        with self.assertRaisesRegex(RuntimeError, 'not_admitted'):
            feeder.begin_sweep()
        self.assertEqual(len(repo.pages), 1)
        self.assertEqual(executor.jobs, [])
        self.assertEqual(coordinator.snapshot().held, 1)

    def test_paused_feeder_does_not_read_or_change_cursor(self):
        repo = CandidateRepository(5, set())
        feeder, coordinator, executor = self.create(repo)
        feeder.offer_to(coordinator)
        previous = feeder.after
        coordinator.pause(True)
        self.assertEqual(feeder.offer_to(coordinator), 0)
        self.assertEqual(feeder.after, previous)
        self.assertEqual(len(repo.pages), 1)
        coordinator.pause(False)
        feeder.offer_to(coordinator)
        self.assertTrue(feeder.exhausted)
        self.assertEqual(feeder.inspected, 5)


if __name__ == '__main__':
    unittest.main()
