"""F6 prototype only: portable file protocol uses a labelled lock simulator.

Native flock tests honestly skip Windows. No YouTube, sockets, DB or runtime
composition is used. The separate Linux harness proves cross-process behaviour.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
import provider_admission as pa

IDENTITY = "11111111-1111-4111-8111-111111111111"
GENERATION = "22222222-2222-4222-8222-222222222222"
BOOT = "33333333-3333-4333-8333-333333333333"


class ThreadLockSimulator:
    """Models independent open handles in this process; NOT a flock proof."""
    def __init__(self):
        self.guard = threading.Lock()
        self.locks = {}
        self.owned = {}

    def acquire(self, fd):
        info = os.fstat(fd)
        key = info.st_dev, info.st_ino
        with self.guard:
            lock = self.locks.setdefault(key, threading.Lock())
            if not lock.acquire(blocking=False):
                return False
            self.owned[fd] = lock
            return True

    def release(self, fd):
        with self.guard:
            self.owned.pop(fd).release()


class Clock:
    def __init__(self):
        self.value = 100.0
    def __call__(self):
        return self.value


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "gate"
        self.locks = ThreadLockSimulator()
        self.threads = []
        self.stops = []

    def config(self, count=2, **changes):
        return pa.AdmissionConfig(**dict(
            installation_id=IDENTITY, generation=GENERATION, boot_id=BOOT,
            concurrency=count, **changes))

    def make(self, count=2, clock=time.monotonic):
        return pa.AdmissionGate.initialize(self.directory, self.config(count),
                                            _locks=self.locks, _clock=clock)

    def eventually(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.005)
        self.fail("bounded condition did not arrive")

    def start(self, callback):
        thread = threading.Thread(target=callback)
        self.threads.append(thread)
        thread.start()
        return thread

    def join(self):
        for thread in self.threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in self.threads),
                         "worker did not finish bounded wait")

    def read(self):
        return json.loads((self.directory / "state.json").read_text())

    def write(self, state):
        (self.directory / "state.json").write_text(json.dumps(state))

    def test_default_and_bounds_are_strict_before_files(self):
        self.assertEqual(pa.AdmissionConfig(IDENTITY, GENERATION, BOOT).concurrency, 8)
        for value in (0, -1, 51, True, "8", 1.2, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config(value)
        for value in ("", "not-a-uuid", None, IDENTITY.upper()):
            if value == IDENTITY:
                continue
            with self.subTest(identity=value), self.assertRaises(ValueError):
                pa.AdmissionConfig(value, GENERATION, BOOT)
        self.assertFalse(self.directory.exists())

    def test_requires_explicit_initialize_and_absolute_private_directory(self):
        with self.assertRaises(ValueError):
            pa.AdmissionGate("relative", self.config(), _locks=self.locks)
        gate = pa.AdmissionGate(self.directory, self.config(), _locks=self.locks)
        with self.assertRaises(pa.AdmissionUnavailable):
            gate.acquire("auto")
        self.assertFalse(self.directory.exists())

    def test_no_private_fallback_on_windows(self):
        with patch.object(pa, "fcntl", None):
            with self.assertRaisesRegex(pa.AdmissionUnavailable, "flock_required"):
                pa.AdmissionGate(self.directory, self.config())
        self.assertFalse(self.directory.exists())

    def test_reopen_preserves_live_inodes_and_configuration(self):
        gate = self.make()
        with gate.acquire("auto"):
            identities = self.read()["files"]
            other = pa.AdmissionGate.initialize(self.directory, self.config(), _locks=self.locks)
            self.assertEqual(other.snapshot()["active"], 1)
            self.assertEqual(identities, self.read()["files"])
            for config in (self.config(3), pa.AdmissionConfig(IDENTITY, BOOT, BOOT, 2),
                           pa.AdmissionConfig(IDENTITY, GENERATION, GENERATION, 2)):
                with self.subTest(config=config), self.assertRaises(pa.AdmissionUnavailable):
                    pa.AdmissionGate.initialize(self.directory, config, _locks=self.locks)
            self.assertEqual(identities, self.read()["files"])
        self.assertEqual(gate.snapshot()["tickets"], 0)

    def test_two_gate_instances_share_cap_and_bounded_queue(self):
        gate = self.make(3)
        other = pa.AdmissionGate(self.directory, self.config(3), _locks=self.locks)
        active = peak = 0
        lock = threading.Lock()
        release = threading.Event()
        errors = []
        def work(selected):
            nonlocal active, peak
            try:
                with selected.acquire("operator", timeout=3):
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    release.wait(2)
                    with lock:
                        active -= 1
            except BaseException as error:
                errors.append(type(error).__name__)
        for i in range(6):
            self.start(lambda i=i: work(gate if i % 2 else other))
        try:
            self.eventually(lambda: gate.snapshot()["tickets"] == 6)
            self.assertEqual(gate.snapshot()["active"], 3)
            with self.assertRaises(pa.AdmissionFull):
                other.acquire("interactive")
        finally:
            release.set()
            self.join()  # Always drain every thread before temporary-file cleanup.
        self.assertEqual((peak, errors), (3, []))
        self.assertEqual(gate.snapshot()["tickets"], 0)

    def test_release_after_reap_never_reuses_still_referenced_slot(self):
        for count in (1, 2):
            with self.subTest(concurrency=count):
                self.directory = Path(self.temp.name) / f"gate-{count}"
                gate = self.make(count)
                other = pa.AdmissionGate(self.directory, self.config(count), _locks=self.locks)
                first, second = gate.acquire("operator"), None
                original = other._slot
                decisions = []
                def release_after_reap(state):
                    # Exact valid interleaving: other's guard prevents _forget;
                    # real close times out, then releases the kernel handles.
                    first.close()
                    referenced = {row["slot"] for row in state["tickets"].values()}
                    chosen, handle = original(state)
                    decisions.append((chosen, referenced))
                    return chosen, handle
                try:
                    with patch.object(other, "_slot", side_effect=release_after_reap):
                        second = other.acquire("operator", timeout=2)
                    for chosen, referenced in decisions:
                        if chosen is not None:
                            self.assertNotIn(chosen, referenced)
                    state = self.read()
                    slots = [row["slot"] for row in state["tickets"].values() if row["slot"] is not None]
                    self.assertEqual(len(slots), len(set(slots)))
                    self.assertEqual(other.snapshot()["active"], 1)
                finally:
                    if second is not None:
                        second.close()
                    first.close()
                self.assertEqual(other.snapshot()["tickets"], 0)
                self.assertFalse(self.locks.owned)

    def test_release_race_keeps_busy_high_slots_in_reduced_effective_cap(self):
        gate = self.make(3)
        other = pa.AdmissionGate(self.directory, self.config(3), _locks=self.locks)
        held = [gate.acquire("operator") for _ in range(3)]
        try:
            held[0].close()
            state = self.read()
            state["effective"] = 1
            self.write(state)
            original = other._slot
            decisions = []
            def release_after_reap(state):
                held[1].close()
                chosen, handle = original(state)
                decisions.append(chosen)
                return chosen, handle
            with patch.object(other, "_slot", side_effect=release_after_reap), \
                    patch.object(other, "_wait", side_effect=pa.AdmissionTimeout("bounded test stop")):
                with self.assertRaises(pa.AdmissionTimeout):
                    other.acquire("auto")
            self.assertEqual(decisions, [None])
            self.assertEqual(other.snapshot()["active"], 1)  # slot2 remains above effective1.
            self.assertEqual(other.snapshot()["effective"], 1)
        finally:
            for permit in held:
                permit.close()
        self.assertFalse(self.locks.owned)

    def test_timeout_reaps_ticket_and_does_not_expire_active_permit(self):
        gate = self.make(1)
        with gate.acquire("operator"):
            with self.assertRaises(pa.AdmissionTimeout):
                gate.acquire("auto", timeout=0.035)
            self.assertEqual(gate.snapshot()["tickets"], 1)
            state = self.read()
            next(iter(state["tickets"].values()))["deadline"] = 0.0
            self.write(state)
            self.assertEqual(gate.snapshot()["active"], 1)
        with gate.acquire("auto"):
            self.assertEqual(gate.snapshot()["active"], 1)

    def test_cancel_wakes_waiter_without_disturbing_active_request(self):
        gate = self.make(1)
        cancel, outcomes = threading.Event(), []
        def work():
            try:
                gate.acquire("auto", timeout=3, cancel=cancel)
            except pa.AdmissionCancelled:
                outcomes.append("cancelled")
        with gate.acquire("operator"):
            self.start(work)
            self.eventually(lambda: gate.snapshot()["waiting"] == 1)
            started = time.monotonic()
            cancel.set()
            self.join()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(outcomes, ["cancelled"])
            self.assertEqual(gate.snapshot()["active"], 1)
            self.assertEqual(gate.snapshot()["waiting"], 0)

    def test_pre_cancelled_request_creates_no_ticket(self):
        gate = self.make()
        event = threading.Event()
        event.set()
        with self.assertRaises(pa.AdmissionCancelled):
            gate.acquire("auto", cancel=event)
        self.assertEqual(gate.snapshot()["tickets"], 0)

    def test_class_round_robin_and_fifo_make_ready_auto_progress(self):
        # This checks scheduling order, not wall-clock expiry while four
        # waiters are enrolled. Timeout behavior has its own tests below.
        gate = self.make(4, Clock())
        held = [gate.acquire("operator") for _ in range(4)]
        order, errors = [], []
        cancel = threading.Event()
        events = {name: threading.Event() for name in ("first", "second", "third", "auto")}
        def work(name, kind):
            try:
                with gate.acquire(kind, timeout=3, cancel=cancel):
                    order.append(name)
                    if not events[name].wait(10):
                        raise AssertionError("permit release was not signalled")
            except BaseException as error:
                errors.append(type(error).__name__)
        try:
            for count, name in enumerate(("first", "second", "third", "auto"), 1):
                self.start(lambda name=name: work(name, "auto" if name == "auto" else "operator"))
                self.eventually(lambda count=count: gate.snapshot()["waiting"] == count)
            held.pop().close()
            self.eventually(lambda: len(order) == 1)
            self.assertEqual(order, ["auto"])
            for name, expected in (("auto", "first"), ("first", "second"), ("second", "third")):
                events[name].set()
                self.eventually(lambda expected=expected: order[-1] == expected)
            events["third"].set()
            self.join()
        finally:
            cancel.set()
            for event in events.values():
                event.set()
            for permit in held:
                permit.close()
            self.join()
        self.assertEqual(errors, [])
        self.assertEqual(order, ["auto", "first", "second", "third"])

    def test_cooldown_holds_no_new_slot_and_counts_all_old_slots(self):
        clock = Clock()
        gate = self.make(3, clock)
        held = [gate.acquire("auto") for _ in range(3)]
        gate.set_cooldown(2, effective=1)
        got = threading.Event()
        cancel = threading.Event()
        def work():
            with gate.acquire("operator", timeout=10, cancel=cancel):
                got.set()
        self.start(work)
        self.eventually(lambda: gate.snapshot()["waiting"] == 1)
        held.pop(0).close()
        held.pop(0).close()  # the only occupied slot now has index 2 >= effective
        self.assertFalse(got.is_set())
        clock.value += 3
        gate._notify()
        threading.Event().wait(0.12)
        self.assertFalse(got.is_set())
        held.pop().close()
        self.join()
        self.assertTrue(got.is_set())
        self.assertEqual(gate.snapshot()["effective"], 1)

    def test_wait_loop_is_bounded_not_busy(self):
        # Windows may return a timed condition wait just before its deadline.
        # Check the requested blocking intervals with an exact clock instead
        # of treating OS timer rounding as an extra busy-loop iteration.
        clock = Clock()
        gate = self.make(1, clock)
        waits = []
        def blocking_wait(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, pa.POLL_SECONDS)
            waits.append(seconds)
            clock.value += seconds
        with gate.acquire("operator"), patch.object(gate._condition, "wait", side_effect=blocking_wait):
            with self.assertRaises(pa.AdmissionTimeout):
                gate.acquire("auto", timeout=0.22)
        self.assertEqual(len(waits), 3)
        self.assertAlmostEqual(sum(waits), 0.22)

    def test_invalid_state_fails_closed_without_rewriting_it(self):
        gate = self.make()
        path = self.directory / "state.json"
        for raw in (b"{", b'{"format":1,"format":1}', b"x" * (pa.MAX_STATE_BYTES + 1)):
            with self.subTest(raw_length=len(raw)):
                path.write_bytes(raw)
                with self.assertRaises(pa.AdmissionUnavailable):
                    gate.acquire("auto")
                self.assertEqual(path.read_bytes(), raw)
                with self.assertRaises(pa.AdmissionUnavailable):
                    pa.AdmissionGate.initialize(self.directory, self.config(), _locks=self.locks)
                self.assertEqual(path.read_bytes(), raw)

    def test_missing_live_lock_is_never_recreated(self):
        gate = self.make()
        for name in ("slot-0.lock", "guard.lock"):
            path = self.directory / name
            path.unlink()
            with self.assertRaises(pa.AdmissionUnavailable):
                gate.acquire("auto")
            with self.assertRaises(pa.AdmissionUnavailable):
                pa.AdmissionGate.initialize(self.directory, self.config(), _locks=self.locks)
            self.assertFalse(path.exists())

    def test_acknowledgement_loss_releases_local_locks_without_dispatch(self):
        gate = self.make(1)
        original = gate._save
        def uncertain(state):
            original(state)
            raise OSError("synthetic after replace")
        with patch.object(gate, "_save", uncertain):
            with self.assertRaises(pa.AdmissionUnavailable):
                gate.acquire("auto")
        self.assertEqual(gate.snapshot()["tickets"], 0)
        with gate.acquire("operator"):
            self.assertEqual(gate.snapshot()["active"], 1)

    def test_close_is_idempotent_and_releases_even_if_state_is_corrupt(self):
        gate = self.make(1)
        permit = gate.acquire("auto")
        path = self.directory / "state.json"
        good = path.read_bytes()
        path.write_bytes(b"{")
        permit.close()
        permit.close()
        self.assertEqual(len(self.locks.owned), 0)
        self.assertEqual(path.read_bytes(), b"{")
        path.write_bytes(good)  # explicit test-only restoration, no automatic repair
        self.assertEqual(gate.snapshot()["tickets"], 0)

    def test_process_change_is_refused(self):
        gate = self.make(1)
        with patch.object(pa.os, "getpid", return_value=gate._pid + 1):
            with self.assertRaisesRegex(pa.AdmissionUnavailable, "process_changed"):
                gate.acquire("auto")
        self.assertEqual(gate.snapshot()["tickets"], 0)

    def test_partial_initialization_is_not_repaired(self):
        self.directory.mkdir(mode=0o700)
        with self.assertRaises(pa.AdmissionUnavailable):
            pa.AdmissionGate.initialize(self.directory, self.config(), _locks=self.locks)
        (self.directory / "guard.lock").touch(mode=0o600)
        with self.assertRaises(pa.AdmissionUnavailable):
            pa.AdmissionGate.initialize(self.directory, self.config(), _locks=self.locks)
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"guard.lock"})

    def test_idle_waits_do_not_rewrite_state(self):
        gate = self.make(1)
        with gate.acquire("operator"):
            with patch.object(gate, "_save", wraps=gate._save) as save:
                gate.snapshot()
                self.assertEqual(save.call_count, 0)
                with self.assertRaises(pa.AdmissionTimeout):
                    gate.acquire("auto", timeout=0.22)
                self.assertEqual(save.call_count, 1)  # registration only

    def test_deadline_is_rechecked_after_durable_admission(self):
        clock = Clock()
        gate = self.make(1, clock)
        original = gate._save
        def slow_save(state):
            original(state)
            clock.value += 2
        with patch.object(gate, "_save", slow_save):
            with self.assertRaises(pa.AdmissionTimeout):
                gate.acquire("auto", timeout=1)
        self.assertEqual(gate.snapshot()["tickets"], 0)

    def test_staging_hardlink_is_rejected_before_truncation(self):
        gate = self.make(1)
        victim = Path(self.temp.name) / "unrelated-data"
        victim.write_bytes(b"preserve")
        try:
            os.link(victim, self.directory / "state.next")
        except OSError:
            self.skipTest("filesystem does not permit test hardlinks")
        with self.assertRaises(pa.AdmissionUnavailable):
            gate.acquire("auto")
        self.assertEqual(victim.read_bytes(), b"preserve")
        self.assertEqual(len(self.locks.owned), 0)

    def test_invalid_requests_and_cooldown_change_nothing(self):
        gate = self.make()
        for timeout in (0, -1, 61, True, float("inf"), float("nan"), "1"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                gate.acquire("auto", timeout=timeout)
        with self.assertRaises(ValueError):
            gate.acquire("unknown")
        for seconds, effective in ((0, None), (61, None), (1, 0), (1, 3), (1, True)):
            with self.subTest(seconds=seconds, effective=effective), self.assertRaises(ValueError):
                gate.set_cooldown(seconds, effective=effective)
        self.assertEqual(gate.snapshot()["tickets"], 0)


@unittest.skipUnless(pa.fcntl is not None and os.name == "posix", "native flock requires Linux/Unix")
class NativeFlockTests(unittest.TestCase):
    def test_separate_real_open_handles_cannot_exceed_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate"
            config = pa.AdmissionConfig(IDENTITY, GENERATION, BOOT, 1)
            first = pa.AdmissionGate.initialize(path, config)
            second = pa.AdmissionGate(path, config)
            with first.acquire("auto"):
                with self.assertRaises(pa.AdmissionTimeout):
                    second.acquire("operator", timeout=0.03)
            with second.acquire("operator"):
                self.assertEqual(first.snapshot()["active"], 1)


if __name__ == "__main__":
    unittest.main()
