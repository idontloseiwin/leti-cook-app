"""UI-only database timing, using recording pools and a deterministic clock."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
sys.path.insert(0, str(ROOT / "tests"))

import psycopg
from psycopg_pool import PoolTimeout
from db.base.postgres import DatabaseError, DatabaseUnavailable, Operation, PostgresConfig, PostgresDatabase
from db.base.read_context import collection_scope, http_method_scope, http_read_scope
import ui_read_trace as trace
from test_direct_pg_transactions import Connection, DSN, Pool, Result


class Clock:
    value = 100.0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class TimedConnection(Connection):
    def __init__(self, events, clock):
        super().__init__(events)
        self.clock = clock

    def execute(self, statement, parameters=()):
        self.clock.advance(.003)
        return super().execute(statement, parameters)

    @contextmanager
    def transaction(self):
        with super().transaction() as connection:
            yield connection
            self.clock.advance(.013)


class TimedPool(Pool):
    def __init__(self, connection, events, clock):
        super().__init__(connection, events)
        self.clock = clock

    @contextmanager
    def connection(self, *, timeout):
        self.clock.advance(.007)
        with super().connection(timeout=timeout) as connection:
            try:
                yield connection
            finally:
                self.clock.advance(.005)


class UiReadDbTraceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events, self.records = [], []
        self.connection = TimedConnection(self.events, self.clock)
        self.pool = TimedPool(self.connection, self.events, self.clock)
        self.database = PostgresDatabase(
            PostgresConfig(DSN), admission=lambda name: None,
            pool_factory=lambda config: self.pool,
        ).open()
        self.addCleanup(self.database.close)
        for target, kwargs in (
            ("socket.socket.connect", {"side_effect": AssertionError("network forbidden")}),
            ("ui_read_trace._clock", {"side_effect": self.clock.now}),
            ("ui_read_trace.telemetry.is_enabled", {"return_value": True}),
            ("ui_read_trace.telemetry.emit_event", {"side_effect": self.record}),
        ):
            mock = patch(target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def record(self, kind, source, target, operation, **facts):
        self.records.append(dict(kind=kind, source=source, target=target, operation=operation, **facts))

    def request(self, action):
        async def app(scope, receive, send):
            with http_read_scope(), http_method_scope("GET"):
                action()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            pass

        coroutine = trace.observe_http(
            app, {"type": "http", "method": "GET", "path": "/explorer/home-summary"},
            receive, send,
        )
        # The local ASGI fake has no suspension/I/O; run it directly without
        # constructing Windows asyncio's loopback socketpair.
        try:
            try:
                coroutine.send(None)
            except StopIteration:
                pass
            else:
                self.fail("Unexpected I/O in offline request fixture")
        finally:
            coroutine.close()

    def read(self, name="frontend_count_inventory_by_type"):
        self.connection.results.append(Result(rows=({"value": 9},)))
        with self.database.transaction(Operation(name, read_only=True)) as tx:
            result = tx.fetch_one("SELECT 9 AS value")
            self.clock.advance(.011)
            return result

    def terminal(self, phase):
        return next(row for row in self.records
                    if row["phase"] == phase and row["status"] != "started")

    def test_pool_wait_excludes_execution_and_return_and_execution_includes_commit(self):
        self.request(lambda: self.assertEqual(self.read(), {"value": 9}))
        self.assertAlmostEqual(self.terminal("db_pool_wait")["duration_ms"], 7)
        self.assertAlmostEqual(self.terminal("db_execution")["duration_ms"], 33)
        self.assertAlmostEqual(self.terminal("db_transaction")["duration_ms"], 45)
        self.assertEqual((self.pool.borrowed, self.pool.returned, self.connection.commits), (1, 1, 1))
        self.assertEqual(self.connection.setup[0][0], "SET LOCAL ROLE lic_reader")
        total = self.terminal("db_transaction")
        for phase in ("db_pool_wait", "db_execution"):
            self.assertEqual(self.terminal(phase)["parent_span_id"], total["span_id"])
            self.assertEqual(self.terminal(phase)["trace_id"], total["trace_id"])
        self.assertEqual(total["status"], "ok")

    def test_pool_failure_retains_mapped_error_and_records_safe_reason(self):
        self.pool.acquire_error = PoolTimeout("private connection and payload")
        with self.assertRaises(DatabaseUnavailable) as caught:
            self.request(self.read)
        self.assertEqual(caught.exception.reason, "pool_timeout")
        self.assertEqual(caught.exception.message, "database_pool_unavailable")
        self.assertEqual(self.terminal("db_transaction")["reason"], "pool_timeout")
        self.assertEqual(self.terminal("db_pool_wait")["status"], "error")
        self.assertFalse(any(row["phase"] == "db_execution" for row in self.records))
        self.assertNotIn("private", json.dumps(self.records))
        self.assertFalse(self.database._local.in_transaction)
        self.assertEqual((self.pool.borrowed, self.pool.returned), (0, 0))

    def test_commit_failure_propagates_without_replaying_or_relabeling_success(self):
        self.connection.commit_error = psycopg.OperationalError("private commit detail")
        with self.assertRaises(DatabaseUnavailable) as caught:
            self.request(self.read)
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(caught.exception.reason, "connection_failed")
        self.assertEqual(self.terminal("db_execution")["status"], "error")
        self.assertEqual(self.terminal("db_transaction")["reason"], "connection_failed")
        self.assertEqual((self.pool.borrowed, self.pool.returned, self.connection.rollbacks), (1, 1, 1))
        self.assertEqual(len(self.connection.queries), 1)
        self.assertNotIn("private", json.dumps(self.records))

    def test_post_acquisition_admission_still_precedes_begin_and_returns_connection(self):
        calls = []
        refusal = RuntimeError("synthetic admission closed")

        def admission(name):
            calls.append(name)
            if len(calls) == 2:
                raise refusal

        self.database._admission = admission
        with self.assertRaises(RuntimeError) as caught:
            self.request(self.read)
        self.assertIs(caught.exception, refusal)
        self.assertNotIn(("begin",), self.events)
        self.assertEqual((self.pool.borrowed, self.pool.returned), (1, 1))
        self.assertFalse(self.database._local.in_transaction)

    def test_no_db_events_without_ui_request_or_inside_collection_scope(self):
        with http_read_scope():
            self.read()
        self.assertEqual(self.records, [])

        def collection():
            with collection_scope():
                self.read()

        self.request(collection)
        self.assertFalse(any(row["phase"].startswith("db_") for row in self.records))
        self.assertEqual(self.connection.setup[-3][0], "SET LOCAL ROLE service_role")

    def test_unknown_operation_token_never_enters_journal(self):
        self.request(lambda: self.read("private_operation_token"))
        database_records = [row for row in self.records if row["target"] == "postgres"]
        self.assertTrue(database_records)
        self.assertTrue(all(row["operation"] == "database_read" for row in database_records))
        self.assertNotIn("private_operation_token", json.dumps(self.records))

    def test_observer_sink_failure_cannot_change_database_result(self):
        with patch("ui_read_trace.telemetry.emit_event", side_effect=RuntimeError("sink failed")):
            self.request(lambda: self.assertEqual(self.read(), {"value": 9}))
        self.assertEqual((self.pool.borrowed, self.pool.returned, self.connection.commits), (1, 1, 1))

    def test_http_write_guard_unchanged_and_does_not_borrow(self):
        def write():
            with self.database.transaction(Operation("write_fixture")):
                self.fail("GET must reject write before yield")

        with self.assertRaisesRegex(DatabaseError, "database_http_write_forbidden"):
            self.request(write)
        self.assertEqual((self.pool.borrowed, self.pool.returned), (0, 0))
        self.assertFalse(any(row["phase"].startswith("db_") for row in self.records))


if __name__ == "__main__":
    unittest.main()
