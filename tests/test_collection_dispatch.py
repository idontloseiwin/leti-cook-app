"""Offline coordinator contracts; no DB/HTTP/adapters or production lifecycle."""
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection.dispatch import (
    Ack, Completion, Coordinator, Deferred, DispatchBlocked, DuplicateWork,
    Prepared, QueueFull, Skipped, Work,
)


class Clock:
    def __init__(self):
        self.value = 100.0
    def __call__(self):
        return self.value


class ManualExecutor(Executor):
    """Actual Future states, controlled completion; no thread/network simulation claim."""
    def __init__(self):
        self.jobs = []
    def submit(self, callback, *args, **kwargs):
        future = Future()
        self.jobs.append((future, callback, args, kwargs))
        return future
    def run(self, index):
        future, callback, args, kwargs = self.jobs[index]
        if not future.running() and not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(callback(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)


class WaitRecorder:
    def __init__(self):
        self.delays = []
        self.signalled = False
    def set(self):
        self.signalled = True
    def clear(self):
        self.signalled = False
    def wait(self, delay):
        self.delays.append(delay)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.executor = ManualExecutor()
        self.prepared = []
        self.fetched = []
        self.settled = []

    def coordinator(self, count=2):
        return Coordinator(self.executor, count, clock=self.clock)

    def work(self, key, *, lane="operator", channel="A", kind="long",
             prepare=None, settle=None, exclusion=None, due=0):
        def default_prepare():
            self.prepared.append(key)
            return Prepared(lambda cancel: self.fetched.append(key) or key, 200, 50)
        def default_settle(result):
            self.settled.append((key, result))
            return Ack()
        return Work(key, lane, channel, kind, prepare or default_prepare,
                    settle or default_settle, due, exclusion)

    def test_hydration_batches_of_one_channel_can_fill_all_credits(self):
        c = self.coordinator(8)
        for n in range(16):
            c.offer(self.work(str(n)))
        turn = c.step()
        self.assertEqual(turn.submitted, 8)
        self.assertEqual(turn.snapshot.ready, 8)
        self.assertEqual((turn.snapshot.inflight, turn.snapshot.held), (8, 8))
        self.assertEqual(len(self.executor.jobs), 8)
        c.step()
        self.assertEqual(len(self.executor.jobs), 8)

    def test_continuation_retains_credit_and_prepares_once(self):
        c = self.coordinator(1)
        stages = []
        def settle(result):
            stages.append(result.phase)
            if len(stages) == 1:
                return Prepared(lambda cancel: 'counters', 200, 1)
            return Ack()
        c.offer(self.work('video', settle=settle))
        c.offer(self.work('next'))
        c.step()
        self.executor.run(0)
        turn = c.step()
        self.assertEqual(turn.acknowledged, ())
        self.assertEqual((turn.snapshot.held, turn.snapshot.ready, turn.submitted), (1, 1, 1))
        self.assertEqual(self.prepared, ['video'])
        self.executor.run(1)
        turn = c.step()
        self.assertEqual(turn.acknowledged, ('video',))
        self.assertEqual(self.prepared, ['video', 'next'])

    def test_cancel_between_steps_does_not_fetch_continuation_or_free_credit_early(self):
        c = self.coordinator(1)
        later = []
        completions = []
        def settle(result):
            completions.append(result.phase)
            if len(completions) == 1:
                return Prepared(lambda cancel: later.append('should not run'), 200)
            return Ack()
        c.offer(self.work('video', settle=settle))
        c.step()
        self.executor.run(0)
        c.cancel('video')
        turn = c.step()
        self.assertEqual(turn.snapshot.held, 1)
        self.assertEqual(len(self.executor.jobs), 1)
        self.assertEqual(turn.submitted, 0)
        turn = c.step()
        self.assertEqual(turn.snapshot.held, 0)
        self.assertEqual(completions, ['response', 'cancelled'])
        self.assertEqual(later, [])

    def test_paused_continuation_is_released_without_executor_submission(self):
        c = self.coordinator(1)
        phases = []
        def settle(result):
            phases.append(result.phase)
            return Prepared(lambda cancel: self.fail('paused provider'), 200) if len(phases) == 1 else Ack()
        c.offer(self.work('a', settle=settle))
        c.step()
        self.executor.run(0)
        c.pause()
        self.assertEqual(c.step().submitted, 0)
        self.assertEqual(c.snapshot().held, 1)
        self.assertEqual(c.step().acknowledged, ('a',))
        self.assertEqual(phases, ['response', 'cancelled'])
        self.assertEqual(len(self.executor.jobs), 1)

    def test_known_drain_skips_uncertainty_and_requires_explicit_independent_keys(self):
        c = self.coordinator(3)
        calls = []
        def uncertain(result):
            calls.append('uncertain')
            raise RuntimeError('unknown commit')
        c.offer(self.work('unknown', settle=uncertain))
        c.offer(self.work('known'))
        c.offer(self.work('not_selected'))
        c.step()
        c.offer(self.work('unprepared'))
        for index in range(3):
            self.executor.run(index)
        with self.assertRaises(DispatchBlocked):
            c.step()
        with self.assertRaises(ValueError):
            c.drain_known(('unknown', 'known'))
        turn = c.drain_known(('known',))
        self.assertEqual(turn.acknowledged, ('known',))
        self.assertEqual(turn.skipped, ('unprepared',))
        self.assertEqual((turn.snapshot.held, turn.snapshot.ready, turn.submitted), (2, 0, 0))
        self.assertEqual(calls, ['uncertain'])
        self.assertEqual([key for key, _ in self.settled], ['known'])
        self.assertEqual(tuple(f.key for f in c.failures()), ('unknown',))
        self.assertTrue(c.snapshot().blocked)
        self.assertTrue(c.snapshot().paused)
        self.assertEqual(c.drain_known(('not_selected',)).acknowledged, ('not_selected',))
        with self.assertRaises(DispatchBlocked):
            c.step()
        self.assertEqual(calls, ['uncertain'])
        c.reconcile('unknown', Ack())
        self.assertEqual(c.snapshot().held, 0)

    def test_multiple_drain_failures_keep_each_credit_and_never_replay(self):
        c = self.coordinator(3)
        calls = []
        def unknown(key):
            def settle(result):
                calls.append(key)
                raise RuntimeError('unconfirmed ' + key)
            return settle
        c.offer(self.work('a', settle=unknown('a')))
        c.offer(self.work('b', settle=unknown('b')))
        c.offer(self.work('c'))
        c.step()
        for index in range(3):
            self.executor.run(index)
        with self.assertRaises(DispatchBlocked):
            c.step()
        turn = c.drain_known(('b', 'c'))
        self.assertEqual(turn.acknowledged, ('c',))
        self.assertEqual(calls, ['a', 'b'])
        self.assertEqual(tuple(f.key for f in c.failures()), ('a', 'b'))
        self.assertEqual(c.snapshot().held, 2)
        c.reconcile('b', Ack())
        self.assertTrue(c.snapshot().blocked)
        with self.assertRaises(DispatchBlocked) as caught:
            c.step()
        self.assertEqual(caught.exception.key, 'a')
        c.reconcile('a', Ack())
        self.assertEqual(c.failures(), ())
        self.assertFalse(c.snapshot().blocked)
        self.assertEqual(calls, ['a', 'b'])

    def test_drain_continuation_retains_credit_until_exact_release_ack(self):
        c = self.coordinator(1)
        phases = []
        def settle(result):
            phases.append(result.phase)
            if len(phases) == 1:
                return Prepared(lambda cancel: self.fail('drained provider'), 200)
            self.assertFalse(result.fetch_started)
            self.assertTrue(result.cancel_requested)
            return Ack()
        c.offer(self.work('a', settle=settle))
        c.step()
        self.executor.run(0)
        turn = c.drain_known(('a',))
        self.assertEqual((turn.submitted, turn.snapshot.held), (0, 1))
        turn = c.drain_known(('a',))
        self.assertEqual(turn.acknowledged, ('a',))
        self.assertEqual(phases, ['response', 'cancelled'])
        self.assertEqual(len(self.executor.jobs), 1)

    def test_drain_running_fetch_keeps_credit_and_cannot_claim_ack(self):
        c = self.coordinator(1)
        c.offer(self.work('a'))
        c.step()
        future = self.executor.jobs[0][0]
        self.assertTrue(future.set_running_or_notify_cancel())
        turn = c.drain_known(('a',))
        self.assertEqual((turn.snapshot.inflight, turn.snapshot.held), (1, 1))
        self.assertEqual(turn.acknowledged, ())
        self.assertEqual(self.settled, [])
        self.executor.run(0)
        self.assertEqual(c.drain_known(('a',)).acknowledged, ('a',))

    def test_known_drain_validation_does_not_mutate_queue(self):
        c = self.coordinator(1)
        c.offer(self.work('a'))
        for keys in (None, ['a'], ('a',), ('a', 'a'), ('missing',), (False,)):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                c.drain_known(keys)
        self.assertFalse(c.snapshot().paused)
        self.assertEqual(c.snapshot().ready, 1)

    def test_ready_limit_and_completed_credit_survive_blocked_writer(self):
        c = self.coordinator(2)
        error = RuntimeError("synthetic uncertain settlement")
        def settle(result):
            raise error
        c.offer(self.work("a", settle=settle))
        c.offer(self.work("b"))
        c.step()
        for n in range(4):
            c.offer(self.work("ready" + str(n)))
        with self.assertRaises(QueueFull):
            c.offer(self.work("excess"))
        self.executor.run(0)
        self.executor.run(1)
        self.assertEqual(c.snapshot().unacked, 2)
        with self.assertRaises(DispatchBlocked) as caught:
            c.step()
        self.assertIs(caught.exception.cause, error)
        self.assertEqual((c.snapshot().ready, c.snapshot().held), (4, 2))
        for _ in range(3):
            with self.assertRaises(DispatchBlocked):
                c.step()
        self.assertEqual(len(self.executor.jobs), 2)
        self.assertEqual(c.pending("a").value, "a")
        c.reconcile("a", Ack())
        self.assertEqual(c.snapshot().held, 1)
        turn = c.step()
        self.assertIn("b", turn.acknowledged)
        self.assertEqual(turn.submitted, 2)

    def test_fair_lane_channel_and_format_round_robin(self):
        c = self.coordinator(4)
        order = []
        rows = [
            ("A-long0", "auto", "A", "long"), ("A-long1", "auto", "A", "long"),
            ("A-short", "auto", "A", "short"), ("A-stream", "auto", "A", "stream"),
            ("B-long0", "auto", "B", "long"), ("B-long1", "auto", "B", "long"),
            ("O0", "operator", "O", "long"), ("O1", "operator", "O", "long"),
        ]
        for key, lane, channel, kind in rows:
            def prepare(key=key):
                order.append(key)
                return Skipped()
            c.offer(self.work(key, lane=lane, channel=channel, kind=kind, prepare=prepare))
        turn = c.step()
        self.assertEqual(order, ["A-long0", "O0", "B-long0", "O1",
                                 "A-short", "B-long1", "A-stream", "A-long1"])
        self.assertEqual(len(turn.skipped), 8)
        self.assertEqual(c.snapshot().held, 0)

    def test_late_small_channel_gets_turn_before_large_channel_refill(self):
        c = self.coordinator(1)
        c.offer(self.work("big1"))
        c.offer(self.work("big2"))
        c.step()
        c.offer(self.work("small", channel="B"))
        self.executor.run(0)
        c.step()
        c.offer(self.work("big3"))
        self.executor.run(1)
        c.step()
        self.assertEqual(self.prepared, ["big1", "big2", "small"])
        self.executor.run(2)
        c.step()
        self.assertEqual(self.prepared[-1], "big3")

    def test_three_playlist_chains_and_next_page_only_after_ack(self):
        c = self.coordinator(3)
        for kind in ("long", "short", "stream"):
            c.offer(self.work(kind, kind=kind, exclusion="playlist:A:" + kind))
        c.step()
        self.assertEqual(c.snapshot().held, 3)
        self.executor.run(0)
        with self.assertRaises(DuplicateWork):
            c.offer(self.work("long-page2", exclusion="playlist:A:long"))
        self.assertEqual(len(self.executor.jobs), 3)
        c.step()
        c.offer(self.work("long-page2", exclusion="playlist:A:long"))
        c.step()
        self.assertEqual(len(self.executor.jobs), 4)
        # Cross-lane duplicate exclusion is also refused.
        with self.assertRaises(DuplicateWork):
            c.offer(self.work("auto-long", lane="auto", exclusion="playlist:A:long"))

    def test_deferred_format_does_not_block_other_ready_format(self):
        c = self.coordinator(2)
        c.offer(self.work("long", due=150))
        c.offer(self.work("short", kind="short"))
        c.step()
        self.assertEqual(self.prepared, ["short"])

    def test_deferred_prepare_waits_until_due_without_empty_spin(self):
        c = self.coordinator(1)
        calls = []
        def prepare():
            calls.append(self.clock())
            return Deferred(150) if len(calls) == 1 else Prepared(lambda cancel: 1, 200)
        c.offer(self.work("delayed", prepare=prepare))
        self.assertEqual(c.step().submitted, 0)
        wait = WaitRecorder()
        c._wakeup = wait
        c.wait(60)
        self.assertEqual(wait.delays, [50])
        for _ in range(3):
            self.assertEqual(c.step().submitted, 0)
        self.assertEqual(calls, [100])
        self.clock.value = 150
        self.assertEqual(c.step().submitted, 1)

    def test_idle_and_blocked_wait_have_positive_bounded_delay(self):
        c = self.coordinator()
        wait = WaitRecorder()
        c._wakeup = wait
        c.wait()
        self.assertEqual(wait.delays, [30])
        c.offer(self.work("bad", prepare=lambda: Deferred(100)))
        with self.assertRaises(DispatchBlocked):
            c.step()
        c.wait(10)
        self.assertEqual(wait.delays[-1], 10)

    def test_prepare_failure_is_held_and_never_replayed(self):
        c = self.coordinator()
        error = RuntimeError("unknown bind receipt")
        calls = []
        def prepare():
            calls.append(1)
            raise error
        c.offer(self.work("bad", prepare=prepare))
        c.offer(self.work("next"))
        with self.assertRaises(DispatchBlocked) as caught:
            c.step()
        self.assertEqual(caught.exception.phase, "prepare")
        self.assertEqual(c.snapshot().held, 1)
        self.assertIs(c.pending("bad").error, error)
        with self.assertRaises(DispatchBlocked):
            c.step()
        self.assertEqual(calls, [1])
        self.assertEqual(len(self.executor.jobs), 0)
        c.reconcile("bad", Ack())
        self.assertEqual(c.step().submitted, 1)

    def test_unknown_submit_ack_blocks_without_retry(self):
        class FailingExecutor(Executor):
            def __init__(self):
                self.calls = 0
            def submit(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("uncertain submit")
        executor = FailingExecutor()
        c = Coordinator(executor, 1, clock=self.clock)
        c.offer(self.work("a"))
        with self.assertRaises(DispatchBlocked):
            c.step()
        with self.assertRaises(DispatchBlocked):
            c.step()
        self.assertEqual(executor.calls, 1)
        self.assertEqual(c.snapshot().held, 1)
        self.assertEqual(c.pending("a").phase, "submit_error")

    def test_response_and_original_fetch_error_reach_settlement_once(self):
        c = self.coordinator(1)
        error = ValueError("synthetic payload must not be logged")
        def fetch(cancel):
            raise error
        c.offer(self.work("a", prepare=lambda: Prepared(fetch, 200)))
        c.step()
        self.executor.run(0)
        turn = c.step()
        result = self.settled[0][1]
        self.assertEqual(result.phase, "fetch_error")
        self.assertIs(result.error, error)
        self.assertTrue(result.fetch_started)
        self.assertEqual(turn.acknowledged, ("a",))
        c.step()
        self.assertEqual(len(self.settled), 1)
        self.assertEqual(len(self.executor.jobs), 1)

    def test_expired_prepared_work_settles_without_fetch(self):
        c = self.coordinator(1)
        c.offer(self.work("a"))
        c.step()
        self.clock.value = 200
        self.executor.run(0)
        c.step()
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.settled[0][1].phase, "expired")
        self.assertFalse(self.settled[0][1].fetch_started)

    def test_cancel_pending_future_keeps_credit_through_ack(self):
        c = self.coordinator(1)
        c.offer(self.work("a", exclusion="playlist:A:long"))
        c.step()
        self.assertTrue(c.cancel("a"))
        self.assertEqual(c.snapshot().held, 1)
        with self.assertRaises(DuplicateWork):
            c.offer(self.work("next", exclusion="playlist:A:long"))
        c.step()
        self.assertEqual(self.settled[0][1].phase, "cancelled")
        self.assertFalse(self.settled[0][1].fetch_started)
        self.assertEqual(c.snapshot().held, 0)

    def test_cancel_running_fetch_does_not_release_until_completion_and_ack(self):
        c = self.coordinator(1)
        c.offer(self.work("a"))
        c.step()
        future = self.executor.jobs[0][0]
        self.assertTrue(future.set_running_or_notify_cancel())
        c.cancel("a")
        c.step()
        self.assertEqual(c.snapshot().inflight, 1)
        self.assertEqual(c.snapshot().held, 1)
        # Wrapper has not started fetch yet; cancellation is still respected.
        self.executor.run(0)
        c.step()
        self.assertTrue(self.settled[0][1].cancel_requested)
        self.assertEqual(c.snapshot().held, 0)

    def test_cancel_after_fetch_started_waits_for_its_response_and_ack(self):
        entered, release = threading.Event(), threading.Event()
        result_seen = []
        with ThreadPoolExecutor(max_workers=1) as executor:
            c = Coordinator(executor, 1, clock=self.clock)
            def fetch(cancel):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("bounded test wait")
                return {"cancel_seen": cancel.is_set()}
            def settle(result):
                result_seen.append(result)
                return Ack()
            c.offer(self.work("a", prepare=lambda: Prepared(fetch, 200), settle=settle))
            c.step()
            try:
                self.assertTrue(entered.wait(1))
                c.cancel("a")
                c.step()
                self.assertEqual((c.snapshot().inflight, c.snapshot().held), (1, 1))
                self.assertEqual(result_seen, [])
                release.set()
                c.wait(1)
                c.step()
            finally:
                release.set()
        self.assertEqual(result_seen[0].phase, "response")
        self.assertTrue(result_seen[0].fetch_started)
        self.assertTrue(result_seen[0].cancel_requested)
        self.assertTrue(result_seen[0].value["cancel_seen"])
        self.assertEqual(c.snapshot().held, 0)

    def test_pause_drains_and_stop_cancels_ready_without_creating_new_work(self):
        c = self.coordinator(1)
        c.offer(self.work("a"))
        c.offer(self.work("b"))
        c.step()
        c.pause()
        self.executor.run(0)
        self.assertEqual(c.step().acknowledged, ("a",))
        self.assertEqual(len(self.executor.jobs), 1)
        c.stop()
        self.assertEqual(c.snapshot().ready, 0)
        self.assertTrue(c.snapshot().stopped)
        with self.assertRaises(RuntimeError):
            c.offer(self.work("c"))

    def test_resident_keys_include_ready_and_unknown_until_confirmed_release(self):
        c = self.coordinator(1)
        c.offer(self.work('unknown', settle=lambda result: (_ for _ in ()).throw(RuntimeError('unknown_ack'))))
        c.offer(self.work('queued'))
        original = c.keys()
        c.step()
        self.executor.run(0)
        with self.assertRaises(DispatchBlocked):
            c.step()
        self.assertEqual(c.keys(), ('unknown', 'queued'))
        self.assertTrue(c.cancel('queued'))
        self.assertEqual(c.keys(), ('unknown',))
        self.assertEqual(original, ('unknown', 'queued'))
        self.assertEqual(c.snapshot().held, 1)

    def test_resident_key_snapshot_requires_the_owner_thread(self):
        c = self.coordinator()
        errors = []
        def other():
            try:
                c.keys()
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['dispatch_coordinator_thread_required'])

    def test_mutation_requires_owner_thread_and_no_callback_reentry(self):
        c = self.coordinator()
        errors = []
        def other():
            try:
                c.offer(self.work("foreign"))
            except RuntimeError as error:
                errors.append(str(error))
            c.wake()  # The sole permitted cross-thread operation.
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["dispatch_coordinator_thread_required"])
        c.offer(self.work("reentry", prepare=lambda: c.offer(self.work("nested"))))
        with self.assertRaises(DispatchBlocked) as caught:
            c.step()
        self.assertEqual(str(caught.exception.cause), "dispatch_callback_reentry_forbidden")
        self.assertEqual(c.snapshot().ready, 0)

    def test_actual_worker_only_fetches_and_context_is_copied(self):
        marker = ContextVar("dispatch-test", default="missing")
        marker.set("captured")
        owner = threading.get_ident()
        calls = []
        fetched = threading.Event()
        release = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            c = Coordinator(executor, 1, clock=self.clock)
            def prepare():
                calls.append(("prepare", threading.get_ident(), marker.get()))
                def fetch(cancel):
                    calls.append(("fetch", threading.get_ident(), marker.get()))
                    fetched.set()
                    if not release.wait(2):
                        raise RuntimeError("bounded test wait")
                    return "response"
                return Prepared(fetch, 200)
            def settle(result):
                calls.append(("settle", threading.get_ident(), result.value))
                return Ack()
            c.offer(self.work("a", prepare=prepare, settle=settle))
            c.step()
            try:
                self.assertTrue(fetched.wait(1))
                self.assertEqual(c.snapshot().inflight, 1)
                release.set()
                c.wait(1)
                c.step()
            finally:
                release.set()
        self.assertEqual(calls[0], ("prepare", owner, "captured"))
        self.assertEqual(calls[1][0], "fetch")
        self.assertNotEqual(calls[1][1], owner)
        self.assertEqual(calls[1][2], "captured")
        self.assertEqual(calls[2], ("settle", owner, "response"))

    def test_invalid_config_work_defer_and_ack_fail_closed(self):
        for count in (0, 51, True, "8", 8.0):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.coordinator(count)
        for size in (0, 51, True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                Prepared(lambda cancel: None, 200, size)
        with self.assertRaises(ValueError):
            self.work("x", due=float("nan"))
        with self.assertRaises(ValueError):
            self.work("x", lane="unbounded")
        c = self.coordinator(1)
        c.offer(self.work("a", settle=lambda result: None))
        c.step()
        self.executor.run(0)
        with self.assertRaises(DispatchBlocked):
            c.step()
        self.assertEqual(c.snapshot().held, 1)
        with self.assertRaises(ValueError):
            c.reconcile("a", None)

    def test_c50_memory_work_bounds_without_worker_backlog(self):
        c = self.coordinator(50)
        for n in range(100):
            c.offer(self.work(str(n)))
        self.assertEqual(c.step().submitted, 50)
        for n in range(50):
            self.executor.run(n)
        self.assertEqual((c.snapshot().unacked, c.snapshot().ready), (50, 50))
        self.assertEqual(len(self.executor.jobs), 50)
        self.assertEqual(len(self.prepared), 50)
        # At most 50 IDs per Prepared -> at most 2,500 executing/unacked IDs.
        self.assertEqual(c.snapshot().held * 50, 2500)


if __name__ == "__main__":
    unittest.main()
