"""Common physical position carry across bounded logical passes, no I/O."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest

import test_common_feeder as fixtures
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.common_feeder import CommonAutoFeeder
from collection.dispatch import Ack, DispatchBlocked, Prepared, Work


class PersistentCursorTests(unittest.TestCase):
    setUp = fixtures.FeederTests.setUp
    create = fixtures.FeederTests.create
    drive = fixtures.FeederTests.drive

    def test_late_501_channel_is_eventually_claimed_with_50_and_100_limits(self):
        for limit in (50, 100):
            with self.subTest(limit=limit):
                cursor = CommonCandidateCursor()
                repo = fixtures.Repository({fixtures.channel(501): 1},
                    physical=[fixtures.channel(i) for i in range(1,502)])
                updates, counters, starts = 0, [], []
                for _ in range(4):
                    feeder, coordinator, executor = self.create(repo, limit=limit, cursor=cursor)
                    starts.append(dict(cursor._after))
                    self.drive(feeder, coordinator, executor)
                    counters.append(feeder.state.candidate_inspected)
                    updates += feeder.summary['updated']
                    self.assertLessEqual(feeder.state.candidate_inspected, feeder.state.candidate_scan_limit)
                    feeder.finish_pass(coordinator)
                    if updates:
                        break
                self.assertEqual(updates, 1)
                self.assertEqual(len(repo.claims), 1)
                self.assertGreater(len(starts), 1)
                self.assertTrue(any(value is not None for value in starts[1].values()))
                self.assertEqual(counters[0], 1000 if limit == 50 else 2000)

    def test_physical_tail_continues_and_late_added_channel_is_seen(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({}, physical=[fixtures.channel(i) for i in range(1,502)])
        first, coordinator, executor = self.create(repo, limit=50, cursor=cursor)
        self.drive(first, coordinator, executor)
        tail = dict(cursor._after)
        self.assertEqual(first.pass_stop_reason, 'candidate_scan_budget')
        self.assertFalse(cursor.sweep_exhausted)
        first.finish_pass(coordinator)
        repo.physical.append(fixtures.channel(777))
        repo.rows[fixtures.channel(777)] = 1
        found = 0
        for _ in range(4):
            current, coordinator, executor = self.create(repo, limit=50, cursor=cursor)
            if found == 0:
                self.assertTrue(any(current.state._after.values()))
            self.drive(current, coordinator, executor)
            found += current.summary['updated']
            current.finish_pass(coordinator)
            if found:
                break
        self.assertEqual(found, 1)
        self.assertEqual(repo.claims[-1]['scopes'][0]['channel_id'], fixtures.channel(777))
        self.assertTrue(any(tail.values()))

    def test_only_full_eof_wraps_and_next_pass_sees_new_earlier_channel(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({}, physical=[fixtures.channel(5)])
        first, coordinator, executor = self.create(repo, limit=50, cursor=cursor)
        self.drive(first, coordinator, executor)
        self.assertTrue(cursor.sweep_exhausted)
        self.assertEqual(first.pass_stop_reason, 'sweep_exhausted')
        first.finish_pass(coordinator)
        repo.physical.insert(0, fixtures.channel(1))
        repo.rows[fixtures.channel(1)] = 1
        second, coordinator, executor = self.create(repo, limit=50, cursor=cursor)
        self.assertFalse(cursor.sweep_exhausted)
        self.assertTrue(all(value is None for value in cursor._after.values()))
        self.drive(second, coordinator, executor)
        self.assertEqual(second.summary['updated'], 1)
        second.finish_pass(coordinator)

    def test_active_held_and_unknown_pass_cannot_release_or_rebind(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({fixtures.channel(1): 1})
        feeder, coordinator, executor = self.create(repo, limit=50, cursor=cursor)
        with self.assertRaisesRegex(RuntimeError, 'pass_active'):
            self.create(repo, limit=50, cursor=cursor)
        for _ in range(10):
            if feeder.offer_to(coordinator):
                break
        with self.assertRaisesRegex(RuntimeError, 'release_unconfirmed'):
            feeder.finish_pass(coordinator)
        coordinator.step()
        with self.assertRaisesRegex(RuntimeError, 'release_unconfirmed'):
            feeder.finish_pass(coordinator)
        self.unknown_settlement = True
        executor.run(0)
        with self.assertRaises(DispatchBlocked):
            coordinator.step()
        with self.assertRaisesRegex(RuntimeError, 'release_unconfirmed'):
            feeder.finish_pass(coordinator)
        with self.assertRaisesRegex(RuntimeError, 'pass_active'):
            self.create(repo, limit=50, cursor=cursor)

    def test_unknown_candidate_page_cannot_be_dropped_with_a_new_pass(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({})
        repo.due_candidate_page = lambda **kw: (_ for _ in ()).throw(OSError('read failed'))
        feeder, coordinator, executor = self.create(repo, cursor=cursor)
        with self.assertRaises(OSError):
            feeder.offer_to(coordinator)
        with self.assertRaisesRegex(RuntimeError, 'release_unconfirmed'):
            feeder.finish_pass(coordinator)
        with self.assertRaisesRegex(RuntimeError, 'pass_active'):
            self.create(repo, cursor=cursor)

    def test_known_stop_can_release_after_own_ready_descriptors_cancelled(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({fixtures.channel(1): 1, fixtures.channel(2): 1})
        feeder, coordinator, executor = self.create(repo, cursor=cursor)
        feeder.offer_to(coordinator)
        feeder.state.pause('quota_exhausted')
        for key in tuple(feeder._offered):
            coordinator.cancel(key)
        feeder.finish_pass(coordinator)
        self.assertEqual(feeder._offered, set())
        with self.assertRaisesRegex(RuntimeError, 'pass_closed'):
            feeder.offer_to(coordinator)
        with self.assertRaisesRegex(RuntimeError, 'cursor_required'):
            feeder.state.begin_claim('late')
        next_pass, _, _ = self.create(repo, cursor=cursor)
        self.assertIs(cursor._active, next_pass.state)

    def test_operator_resident_does_not_prevent_known_common_pass_release(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({fixtures.channel(1): 1})
        feeder, coordinator, executor = self.create(repo, limit=1, cursor=cursor)
        self.drive(feeder, coordinator, executor)
        coordinator.offer(Work('operator', 'operator', fixtures.channel(9), 'channel',
            lambda: Prepared(lambda cancel: None, 200), lambda response: Ack()))
        feeder.finish_pass(coordinator)
        self.assertTrue(coordinator.contains('operator'))

    def test_exhausted_stats_pass_can_finish_with_full_unrelated_operator_queue(self):
        for offer_again in (False, True):
            with self.subTest(offer_again=offer_again):
                cursor = CommonCandidateCursor()
                repo = fixtures.Repository({fixtures.channel(1): 1})
                feeder, coordinator, executor = self.create(repo, limit=1, concurrency=2, cursor=cursor)
                feeder.offer_to(coordinator)
                coordinator.step()
                executor.run(0)
                coordinator.step()
                self.assertEqual((feeder.state.remaining, feeder.state.held), (0, 0))
                self.assertFalse(feeder.pass_finished)
                self.assertFalse(any(coordinator.contains(key) for key in feeder._offered))
                for number in range(4):
                    coordinator.offer(Work(f'operator:{number}', 'operator', fixtures.channel(9),
                        'channel', lambda: Prepared(lambda cancel: None, 200), lambda response: Ack()))
                self.assertEqual(coordinator.snapshot().ready, 4)
                page_count = len(repo.pages)
                if offer_again:
                    self.assertEqual(feeder.offer_to(coordinator), 0)
                    self.assertTrue(feeder.pass_finished)
                feeder.finish_pass(coordinator)
                self.assertEqual(feeder.pass_stop_reason, 'stats_limit')
                self.assertEqual(feeder._offered, set())
                self.assertEqual(len(repo.pages), page_count)
                self.assertTrue(all(coordinator.contains(f'operator:{n}') for n in range(4)))
                following, _, _ = self.create(repo, limit=1, cursor=cursor)
                self.assertIs(cursor._active, following.state)

    def test_tracked_keys_never_accumulate_beyond_coordinator_residency(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({fixtures.channel(i): 1 for i in range(1,401)})
        feeder, coordinator, executor = self.create(repo, limit=400, concurrency=2, cursor=cursor)
        for _ in range(3000):
            feeder.offer_to(coordinator)
            self.assertLessEqual(len(feeder._offered), 6)
            coordinator.step()
            for index, (future, *_) in enumerate(executor.jobs):
                if not future.done():
                    executor.run(index)
            if feeder.pass_finished and not coordinator.snapshot().held and not coordinator.snapshot().ready:
                break
        else:
            self.fail('pass did not finish')
        self.assertEqual(feeder._sequence, 8)
        feeder.finish_pass(coordinator)
        self.assertEqual(feeder._offered, set())

    def test_cursor_required_for_candidate_path_but_not_legacy_claim_budget(self):
        state = AutoPassState(1)
        self.assertEqual(state.begin_claim('legacy')['limit'], 1)
        with self.assertRaisesRegex(RuntimeError, 'cursor_required'):
            AutoPassState(1).candidate_request(1)
        with self.assertRaises(TypeError):
            CommonAutoFeeder(SimpleNamespace(), state=AutoPassState(1), worker_id='worker', require_ready=lambda: None)

    def test_cursor_and_contains_are_creator_thread_only_and_contains_does_not_collect(self):
        cursor = CommonCandidateCursor()
        repo = fixtures.Repository({fixtures.channel(1): 1})
        feeder, coordinator, executor = self.create(repo, cursor=cursor)
        for _ in range(10):
            if feeder.offer_to(coordinator):
                break
        key = next(iter(feeder._offered))
        before = coordinator.snapshot()
        self.assertTrue(coordinator.contains(key))
        self.assertFalse(coordinator.contains('absent'))
        self.assertEqual(coordinator.snapshot(), before)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(RuntimeError, 'owner_thread'):
                pool.submit(cursor._bind, AutoPassState(1)).result()
            with self.assertRaisesRegex(RuntimeError, 'coordinator_thread'):
                pool.submit(coordinator.contains, key).result()


if __name__ == '__main__':
    unittest.main()
