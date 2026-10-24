"""Concurrent UI reads share work without sharing responses or stale lifetimes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import queue
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from fastapi import HTTPException
from hot_read_cache import TransientReadCache
from hot_storage import HotSourceReadExpired, capture_read_lifetime, register_read_deadline
import telemetry
from telemetry_contract import build_event
import ui_read_trace
from web import cache as cache_module


class _ObservedEvent:
    def __init__(self, waiting):
        self.event = threading.Event()
        self.waiting = waiting

    def wait(self, timeout):
        self.waiting.put(timeout)
        return self.event.wait(timeout)

    def set(self):
        self.event.set()


class _Worker:
    def __init__(self, fn):
        self.done = threading.Event()
        self.value = self.error = None
        def run():
            try:
                self.value = fn()
            except BaseException as error:
                self.error = error
            finally:
                self.done.set()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


class ReadCoalescingTests(unittest.TestCase):
    def setUp(self):
        self.waiting = queue.Queue()
        self.workers, self.releases = [], []
        original_flight = cache_module._ReadFlight
        patches = (
            # Retention timers are independent; keep this suite deterministic.
            patch.object(cache_module, "TransientReadCache",
                         side_effect=lambda lock: TransientReadCache(lock, auto_prune=False,
                             monotonic=lambda: cache_module.time.monotonic())),
            patch.object(cache_module, "_ReadFlight",
                         side_effect=lambda revision: original_flight(
                             revision, completed=_ObservedEvent(self.waiting))),
        )
        for setting in patches:
            setting.start()
            self.addCleanup(setting.stop)
        self.cache = cache_module.create_read_cache()

    def tearDown(self):
        for release in self.releases:
            release.set()
        for worker in self.workers:
            worker.thread.join(2)

    def release_event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def start(self, fn):
        worker = _Worker(fn)
        self.workers.append(worker)
        return worker

    def result(self, worker):
        self.assertTrue(worker.done.wait(2), "Concurrent read did not finish")
        if worker.error is not None:
            raise worker.error
        return worker.value

    def error(self, worker):
        self.assertTrue(worker.done.wait(2), "Failed loader did not wake its waiter")
        self.assertIsNotNone(worker.error)
        return worker.error

    def await_waiters(self, count=1):
        for _ in range(count):
            timeout = self.waiting.get(timeout=2)
            self.assertLessEqual(timeout, cache_module._CACHE_WAIT_SECONDS + 0.000001)

    def traced_read(self, fn):
        async def app(scope, receive, send):
            try:
                value, status = fn(), 200
            except HTTPException:
                value, status = None, 503
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b"{}", "more_body": False})
            return value
        async def send(message):
            pass
        # The fake ASGI exchange has no suspending I/O; drive it without a
        # platform event-loop/socket fixture in these threading-only tests.
        coroutine = ui_read_trace.observe_http(app, {
            "type": "http", "method": "GET", "path": "/explorer/home-summary",
        }, None, send)
        try:
            coroutine.send(None)
        except StopIteration as result:
            return result.value
        finally:
            coroutine.close()
        self.fail("ASGI fixture unexpectedly suspended")

    def test_same_key_uses_one_loader_with_independent_copies_and_source_deadlines(self):
        entered, release = threading.Event(), self.release_event()
        calls = []
        deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
        def load():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            register_read_deadline(deadline)
            return {"items": ["stored"]}
        def read():
            with capture_read_lifetime() as lifetime:
                value = self.cache.cached_read("sidebar-data", 20, load)
            return value, lifetime.expires_at
        owner = self.start(read)
        self.assertTrue(entered.wait(2))
        waiters = [self.start(read) for _ in range(5)]
        self.await_waiters(5)
        release.set()
        results = [self.result(worker) for worker in [owner, *waiters]]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(item_deadline == deadline for _, item_deadline in results))
        self.assertEqual(len({id(value) for value, _ in results}), 6)
        results[0][0]["items"].append("caller changed")
        self.assertTrue(all(value["items"] == ["stored"] for value, _ in results[1:]))
        self.assertEqual(self.cache.cached_read("sidebar-data", 20, lambda: self.fail("cache miss")),
                         {"items": ["stored"]})

    def test_blocked_key_does_not_hold_the_global_lock(self):
        entered, release = threading.Event(), self.release_event()
        def load():
            entered.set()
            self.assertTrue(release.wait(2))
            return "first"
        owner = self.start(lambda: self.cache.cached_read("one", 20, load))
        self.assertTrue(entered.wait(2))
        waiter = self.start(lambda: self.cache.cached_read("one", 20, load))
        self.await_waiters()
        other = self.start(lambda: self.cache.cached_read("two", 20, lambda: "independent"))
        self.assertEqual(self.result(other), "independent")
        release.set()
        self.assertEqual(self.result(owner), "first")
        self.assertEqual(self.result(waiter), "first")

    def test_failure_wakes_every_waiter_without_repeating_or_caching_the_loader(self):
        entered, release = threading.Event(), self.release_event()
        original = HTTPException(429, detail="Retry later", headers={"Retry-After": "1"})
        calls = []
        def load():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            raise original
        owner = self.start(lambda: self.cache.cached_read("sidebar-data", 20, load))
        self.assertTrue(entered.wait(2))
        waiters = [self.start(lambda: self.cache.cached_read("sidebar-data", 20, load)) for _ in range(3)]
        self.await_waiters(3)
        release.set()
        failures = [self.error(worker) for worker in [owner, *waiters]]
        self.assertIs(failures[0], original)
        self.assertEqual(len({id(error) for error in failures}), 4)
        self.assertTrue(all(error.status_code == 429 and error.detail == "Retry later"
                            and error.headers == {"Retry-After": "1"} for error in failures))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.cache.cached_read("sidebar-data", 20, lambda: "recovered"), "recovered")

    def test_owner_cancellation_does_not_cancel_other_requests_or_leave_a_flight(self):
        class Abandoned(BaseException):
            pass
        entered, release = threading.Event(), self.release_event()
        def load():
            entered.set()
            self.assertTrue(release.wait(2))
            raise Abandoned()
        owner = self.start(lambda: self.cache.cached_read("one", 20, load))
        self.assertTrue(entered.wait(2))
        waiter = self.start(lambda: self.cache.cached_read("one", 20, load))
        self.await_waiters()
        release.set()
        self.assertIsInstance(self.error(owner), Abandoned)
        self.assertEqual(self.error(waiter).status_code, 503)
        self.assertEqual(self.cache.cached_read("one", 20, lambda: "recovered"), "recovered")

    def test_timeout_does_not_start_duplicate_work_or_discard_the_owner_result(self):
        entered, release = threading.Event(), self.release_event()
        calls = []
        def load():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            return "eventually"
        owner = self.start(lambda: self.cache.cached_read("one", 20, load))
        self.assertTrue(entered.wait(2))
        with patch.object(cache_module, "_CACHE_WAIT_SECONDS", 0.01):
            waiter = self.start(lambda: self.cache.cached_read("one", 20, load))
            self.await_waiters()
            self.assertEqual(self.error(waiter).status_code, 503)
        self.assertEqual(len(calls), 1)
        release.set()
        self.assertEqual(self.result(owner), "eventually")
        self.assertEqual(self.cache.cached_read("one", 20, lambda: self.fail("cache miss")), "eventually")

    def test_invalidation_wakes_waiters_and_old_owner_cannot_remove_the_new_flight(self):
        old_entered, fresh_entered = threading.Event(), threading.Event()
        old_release, fresh_release = self.release_event(), self.release_event()
        calls = []
        def load():
            calls.append(1)
            if len(calls) == 1:
                old_entered.set()
                self.assertTrue(old_release.wait(2))
                return "old"
            self.assertEqual(len(calls), 2)
            fresh_entered.set()
            self.assertTrue(fresh_release.wait(2))
            return "fresh"
        owner = self.start(lambda: self.cache.cached_read("sidebar-data", 20, load))
        self.assertTrue(old_entered.wait(2))
        waiter = self.start(lambda: self.cache.cached_read("sidebar-data", 20, load))
        self.await_waiters()
        self.cache.clear_read_cache("sidebar")
        self.assertTrue(fresh_entered.wait(2))
        old_release.set()
        self.assertEqual(self.result(owner), "old")
        later = self.start(lambda: self.cache.cached_read("sidebar-data", 20, load))
        self.await_waiters()
        fresh_release.set()
        self.assertEqual(self.result(waiter), "fresh")
        self.assertEqual(self.result(later), "fresh")
        self.assertEqual(self.cache.cached_read("sidebar-data", 20, lambda: self.fail("cache miss")), "fresh")
        self.assertEqual(len(calls), 2)

    def test_source_expiry_retry_is_shared_and_discarded_provenance_does_not_escape(self):
        entered, release = threading.Event(), self.release_event()
        calls = []
        def load():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
                ui_read_trace.failure("inventory_counts", ValueError("private"))
                raise HotSourceReadExpired()
            return {"items": ["fresh"]}
        def read():
            with ui_read_trace.capture_unavailability() as availability:
                value = self.cache.cached_read("sidebar-data", 20, load)
            return value, availability.codes
        owner = self.start(read)
        self.assertTrue(entered.wait(2))
        waiter = self.start(read)
        self.await_waiters()
        release.set()
        self.assertEqual(self.result(owner), ({"items": ["fresh"]}, ()))
        self.assertEqual(self.result(waiter), ({"items": ["fresh"]}, ()))
        self.assertEqual(len(calls), 2)

    def test_final_source_expiry_wakes_waiters_after_exactly_two_attempts(self):
        entered, release = threading.Event(), self.release_event()
        calls = []
        def load():
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(2))
            raise HotSourceReadExpired()
        owner = self.start(lambda: self.cache.cached_read("one", 20, load))
        self.assertTrue(entered.wait(2))
        waiter = self.start(lambda: self.cache.cached_read("one", 20, load))
        self.await_waiters()
        release.set()
        self.assertEqual(self.error(owner).status_code, 503)
        self.assertEqual(self.error(waiter).status_code, 503)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.cache.cached_read("one", 20, lambda: "fresh"), "fresh")

    def test_reentrant_same_key_is_an_independent_read_instead_of_a_deadlock(self):
        def outer():
            return {"inner": self.cache.cached_read("same", 20, lambda: "nested")}
        worker = self.start(lambda: self.cache.cached_read("same", 20, outer))
        self.assertEqual(self.result(worker), {"inner": "nested"})
        self.assertEqual(self.cache.cached_read("same", 20, lambda: self.fail("cache miss")),
                         {"inner": "nested"})

    def test_nested_keys_do_not_deadlock_when_other_threads_own_each_other_keys(self):
        both_started = threading.Barrier(2)
        def outer(key, other):
            both_started.wait(timeout=2)
            return self.cache.cached_read(other, 20, lambda: f"{key} nested")
        first = self.start(lambda: self.cache.cached_read("a", 20, lambda: outer("a", "b")))
        second = self.start(lambda: self.cache.cached_read("b", 20, lambda: outer("b", "a")))
        self.assertIn(self.result(first), {"a nested", "b nested"})
        self.assertIn(self.result(second), {"a nested", "b nested"})
        self.assertTrue(self.waiting.empty())

    def test_zero_ttl_remains_independent_and_never_publishes_a_cached_value(self):
        both_started = threading.Barrier(2)
        def load():
            both_started.wait(timeout=2)
            return []
        first = self.start(lambda: self.cache.cached_read("same", 0, load))
        second = self.start(lambda: self.cache.cached_read("same", 0, load))
        self.assertIsNot(self.result(first), self.result(second))
        self.assertEqual(self.cache.cached_read("same", 0, lambda: "new"), "new")
        self.assertTrue(self.waiting.empty())

    def test_shared_unavailability_is_private_and_replayed_to_each_call(self):
        entered, release = threading.Event(), self.release_event()
        def load():
            entered.set()
            self.assertTrue(release.wait(2))
            ui_read_trace.failure("inventory_counts", ValueError("private payload"))
            return {"counts": None}
        def read():
            with ui_read_trace.capture_unavailability() as availability:
                value = self.cache.cached_read("sidebar-data", 20, load)
            return value, availability.codes
        owner = self.start(read)
        self.assertTrue(entered.wait(2))
        waiter = self.start(read)
        self.await_waiters()
        release.set()
        expected = ({"counts": None}, ("inventory_counts",))
        self.assertEqual(self.result(owner), expected)
        self.assertEqual(self.result(waiter), expected)
        self.assertEqual(read(), expected)
        with ui_read_trace.capture_unavailability() as availability:
            self.cache.cached_read("independent", 20, lambda: "ok")
        self.assertEqual(availability.codes, ())

    def test_expiry_is_measured_from_completion_and_not_extended_by_waiters(self):
        entered, release = threading.Event(), self.release_event()
        tick = [100.0]
        def load():
            entered.set()
            self.assertTrue(release.wait(2))
            return "original"
        with patch.object(cache_module.time, "monotonic", side_effect=lambda: tick[0]):
            owner = self.start(lambda: self.cache.cached_read("one", 3, load))
            self.assertTrue(entered.wait(2))
            waiter = self.start(lambda: self.cache.cached_read("one", 3, load))
            self.await_waiters()
            tick[0] = 110.0
            release.set()
            self.assertEqual(self.result(owner), "original")
            self.assertEqual(self.result(waiter), "original")
            tick[0] = 112.0
            self.assertEqual(self.cache.cached_read("one", 3, lambda: self.fail("early expiry")), "original")
            tick[0] = 113.0
            self.assertEqual(self.cache.cached_read("one", 3, lambda: "new"), "new")

    def test_waiter_journal_has_its_own_trace_and_wait_duration_without_fake_loader(self):
        entered, release = threading.Event(), self.release_event()
        rows = []
        def record(*args, **kwargs):
            rows.append(build_event(*args, **kwargs))
        def load():
            entered.set()
            self.assertTrue(release.wait(2))
            return {"items": []}
        def read():
            return self.traced_read(lambda: self.cache.cached_read("explorer-home-summary", 20, load))
        with patch.object(telemetry, "is_enabled", return_value=True), \
                patch.object(telemetry, "emit_event", side_effect=record):
            owner = self.start(read)
            self.assertTrue(entered.wait(2))
            waiter = self.start(read)
            self.await_waiters()
            release.set()
            self.result(owner)
            self.result(waiter)
        waits = [row for row in rows if row["phase"] == "cache_wait"]
        self.assertEqual([row["status"] for row in waits], ["started", "ok"])
        self.assertGreaterEqual(waits[-1]["duration_ms"], 0)
        waiter_trace = waits[-1]["trace_id"]
        self.assertFalse(any(row["phase"] == "loader" and row["trace_id"] == waiter_trace for row in rows))
        self.assertEqual(len([row for row in rows if row["phase"] == "loader"]), 2)
        terminals = [row for row in rows if row["phase"] == "http" and row["status"] == "ok"]
        self.assertEqual(len(terminals), 2)
        self.assertEqual(len({row["trace_id"] for row in terminals}), 2)

    def test_wait_timeout_has_a_fixed_journal_reason_and_no_fake_database_failure(self):
        entered, release = threading.Event(), self.release_event()
        rows = []
        def record(*args, **kwargs):
            rows.append(build_event(*args, **kwargs))
        def load():
            entered.set()
            self.assertTrue(release.wait(2))
            return "loaded"
        with patch.object(telemetry, "is_enabled", return_value=True), \
                patch.object(telemetry, "emit_event", side_effect=record), \
                patch.object(cache_module, "_CACHE_WAIT_SECONDS", 0.01):
            owner = self.start(lambda: self.cache.cached_read("explorer-home-summary", 20, load))
            self.assertTrue(entered.wait(2))
            waiter = self.start(lambda: self.traced_read(
                lambda: self.cache.cached_read("explorer-home-summary", 20, load)))
            self.await_waiters()
            self.assertIsNone(self.result(waiter))
            release.set()
            self.assertEqual(self.result(owner), "loaded")
        failures = [row for row in rows if row["phase"] == "cache_wait" and row["status"] == "error"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["reason"], "cache_wait_timeout")
        self.assertFalse(any(row["target"] == "postgres" for row in rows))
        self.assertEqual([row["http_status"] for row in rows if row["phase"] == "http"
                          and row["status"] == "error"], [503])


if __name__ == "__main__":
    unittest.main()
