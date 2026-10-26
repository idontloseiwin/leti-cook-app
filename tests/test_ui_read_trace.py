"""UI read observations preserve responses, privacy, request isolation and bounds."""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

import ui_read_trace as trace
from telemetry_contract import build_event, normalize_stored_event
from hot_storage import HotSourceReadExpired

_REAL_ENABLED = trace.telemetry.is_enabled


class UiReadTraceTests(unittest.TestCase):
    def test_reviewed_query_post_records_only_fixed_phases(self):
        async def app(scope, receive, send):
            with trace.database_scope('research_query_page', 'db_execution'):
                pass
            with trace.database_scope('research_query_scope', 'db_execution'):
                pass
            await self.complete(send)
        self.request(app, path='/research/queries/execute', method='POST',
                     query_string=b'PRIVATE_FILTER_CURSOR', headers=[(b'cookie', b'PRIVATE_SESSION')])
        self.assertEqual(self.terminals()[0]['http_status'], 200)
        self.assertEqual(self.terminals()[0]['operation'], 'research_query')
        self.assertNotIn('PRIVATE_', json.dumps(self.events))

    def setUp(self):
        self.events = []
        self.env = patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE="self-hosted")
        self.enabled = patch.object(trace.telemetry, "is_enabled", return_value=True)
        self.emitter = patch.object(
            trace.telemetry, "emit_event",
            side_effect=lambda *args, **kwargs: self.events.append(build_event(*args, **kwargs)),
        )
        for patcher in (self.env, self.enabled, self.emitter):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.assertFalse(trace.is_active()))

    def request(self, app, *, path="/explorer/home-summary", method="GET", **extra):
        messages = []
        async def send(message):
            messages.append(message)
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        asyncio.run(trace.observe_http(
            app, {"type": "http", "path": path, "method": method, **extra}, receive, send,
        ))
        return messages

    @staticmethod
    async def complete(send, status=200, body=b'{"ok":true}'):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    def terminals(self):
        return [item for item in self.events if item["phase"] == "http"
                and item["direction"] == "response"]

    def test_request_correlates_nested_spans_and_does_not_inspect_body_or_inputs(self):
        original = {"type": "http.response.start", "status": 200,
                    "headers": [(b"set-cookie", b"PRIVATE_COOKIE"), (trace.TRACE_HEADER, b"untrusted")]}
        body = {"type": "http.response.body", "body": b"PRIVATE_BODY not JSON"}
        async def app(scope, receive, send):
            with trace.scope("inventory_counts"):
                with trace.database_scope("frontend_count_inventory_by_type", "db_execution"):
                    trace.event("recent_inventory_candidates", phase="scan", count=40)
            await send(original)
            await send(body)
        messages = self.request(app, query_string=b"key=PRIVATE_QUERY",
                                headers=[(trace.TRACE_HEADER, b"PRIVATE_HEADER")])
        self.assertIs(messages[-1], body)
        self.assertEqual(original["headers"][-1], (trace.TRACE_HEADER, b"untrusted"))
        header = dict(messages[0]["headers"])[trace.TRACE_HEADER].decode()
        self.assertRegex(header, r"^[a-f0-9]{32}$")
        self.assertTrue(all(item["trace_id"] == header for item in self.events))
        self.assertTrue(all(normalize_stored_event(item) == item for item in self.events))
        serialized = json.dumps(self.events)
        for value in ("PRIVATE_COOKIE", "PRIVATE_BODY", "PRIVATE_QUERY", "PRIVATE_HEADER", "untrusted"):
            self.assertNotIn(value, serialized)
        root, component, db = self.events[0], self.events[1], self.events[2]
        self.assertEqual(component["parent_span_id"], root["span_id"])
        self.assertEqual(db["parent_span_id"], component["span_id"])
        self.assertEqual(self.terminals()[0]["status"], "ok")

    def test_disabled_and_unobserved_routes_are_direct_fast_paths(self):
        cases = [
            (False, "/system-summary", "GET"),
            (True, "/telemetry/recent", "GET"),
            (True, "/telemetry/stream", "GET"),
            (True, "/internal/update-status", "GET"),
            (True, "/auth", "GET"),
            (True, "/explorer/home-summary", "POST"),
            (True, "/explorer/home-summary", "HEAD"),
            (True, "/explorer/home-summary/private-id", "GET"),
        ]
        async def app(scope, receive, send):
            self.assertFalse(trace.is_active())
            await self.complete(send)
        for enabled, path, method in cases:
            with self.subTest(enabled=enabled, path=path, method=method), \
                 patch.object(trace.telemetry, "is_enabled", return_value=enabled), \
                 patch.object(trace.uuid, "uuid4", side_effect=AssertionError("No identity needed")), \
                 patch.object(trace, "_clock", side_effect=AssertionError("No timing needed")):
                messages = self.request(app, path=path, method=method)
                self.assertNotIn(trace.TRACE_HEADER, dict(messages[0]["headers"]))
        self.assertEqual(self.events, [])

    def test_disabled_facade_does_not_start_writer_and_fails_closed(self):
        import telemetry
        from types import SimpleNamespace
        with patch.object(telemetry, "default_emitter", return_value=SimpleNamespace(enabled=False)):
            self.assertFalse(_REAL_ENABLED())
        with patch.object(telemetry, "default_emitter", side_effect=RuntimeError("private")):
            self.assertFalse(_REAL_ENABLED())

    def test_fail_soft_component_distinguishes_error_and_cached_unavailable(self):
        async def failed(scope, receive, send):
            trace.failure("inventory_counts", ValueError("DO_NOT_RECORD"))
            trace.unavailable("inventory_counts")
            await self.complete(send, body=b'{"saved_videos":null}')
        self.request(failed)
        self.assertEqual(self.terminals()[0]["http_status"], 200)
        self.assertEqual(self.terminals()[0]["status"], "partial")
        self.assertEqual(sum(e["status"] == "error" for e in self.events), 1)
        self.assertNotIn("DO_NOT_RECORD", json.dumps(self.events))
        self.events.clear()
        async def cached(scope, receive, send):
            trace.event("home_summary", phase="cache", status="hit")
            trace.unavailable("inventory_counts")
            await self.complete(send, body=b'{"saved_videos":null}')
        self.request(cached)
        self.assertEqual(self.terminals()[0]["status"], "partial")
        self.assertFalse(any(e["status"] == "error" for e in self.events))

    def test_recovered_read_attempt_does_not_degrade_final_response(self):
        async def app(scope, receive, send):
            try:
                with trace.scope("home_summary", phase="loader"):
                    raise HotSourceReadExpired("private clock")
            except HotSourceReadExpired:
                trace.event("home_summary", phase="cache", status="retry", reason="source_expired")
            with trace.scope("home_summary", phase="loader"):
                pass
            await self.complete(send)
        self.request(app)
        self.assertTrue(any(e.get("reason") == "source_expired" and e["status"] == "error" for e in self.events))
        self.assertEqual(self.terminals()[0]["status"], "ok")

    def test_handled_error_uses_actual_status_and_never_reads_exception_text(self):
        from db.base.postgres import DatabaseServerError
        async def app(scope, receive, send):
            trace.failure("request_guard", DatabaseServerError("PRIVATE_SQL", code="57014"))
            await self.complete(send, status=503)
        self.request(app)
        self.assertEqual(self.terminals()[0]["http_status"], 503)
        self.assertEqual(self.terminals()[0]["reason"], "http_error")
        failure = next(e for e in self.events if e.get("error_code"))
        self.assertEqual(failure["error_code"], "57014")
        self.assertNotIn("PRIVATE_SQL", json.dumps(self.events))

    def test_raw_transport_errors_are_fixed_codes_without_sql_or_endpoint(self):
        import psycopg
        from psycopg_pool import PoolTimeout, TooManyRequests, PoolClosed
        cases = [
            (PoolTimeout("PRIVATE_POOL"), "pool_timeout"),
            (TooManyRequests("PRIVATE_QUEUE"), "pool_queue_full"),
            (PoolClosed("PRIVATE_POOL"), "pool_closed"),
            (psycopg.errors.QueryCanceled("PRIVATE_SQL"), "database_error"),
        ]
        for error, reason in cases:
            with self.subTest(reason=reason):
                facts = trace._error_facts(error)
                self.assertEqual(facts["reason"], reason)
                self.assertNotIn("PRIVATE", json.dumps(facts))
        self.assertEqual(trace._error_facts(cases[-1][0])["error_code"], "57014")

    def test_uncaught_exception_cancellation_and_incomplete_delivery_keep_outcome(self):
        for error, expected in ((RuntimeError("PRIVATE_ERROR"), "error"),
                                (asyncio.CancelledError("PRIVATE_CANCEL"), "cancelled")):
            async def app(scope, receive, send):
                with trace.scope("inventory_counts"):
                    raise error
            with self.subTest(expected=expected), self.assertRaises(type(error)) as caught:
                self.request(app)
            self.assertIs(caught.exception, error)
            self.assertEqual(self.terminals()[-1]["status"], expected)
            self.assertNotIn("http_status", self.terminals()[-1])
            self.assertFalse(trace.is_active())
        async def incomplete(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
        self.request(incomplete)
        self.assertEqual(self.terminals()[-1]["status"], "incomplete")
        self.assertEqual(self.terminals()[-1]["http_status"], 200)

    def test_final_body_defines_duration_and_background_work_does_not_extend_trace(self):
        clock = [10.0]
        async def app(scope, receive, send):
            clock[0] = 10.125
            await self.complete(send)
            self.assertFalse(trace.is_active())
            clock[0] = 1000.0
            trace.failure("inventory_counts", RuntimeError("after response"))
        with patch.object(trace, "_clock", side_effect=lambda: clock[0]):
            self.request(app)
        self.assertEqual(self.terminals()[0]["duration_ms"], 125)
        self.assertEqual(self.terminals()[0]["status"], "ok")
        self.assertEqual(len(self.events), 2)

    def test_concurrent_requests_and_threadpool_reads_are_isolated(self):
        from starlette.concurrency import run_in_threadpool
        responses = {}
        async def run(path, operation):
            messages = []
            async def send(message):
                messages.append(message)
            def read():
                with trace.scope(operation):
                    self.assertTrue(trace.is_active())
                    return trace._CURRENT.get().request.trace_id
            async def app(scope, receive, send):
                await asyncio.sleep(0)
                identity = await run_in_threadpool(read)
                responses[path] = identity
                await self.complete(send)
            await trace.observe_http(app, {"type": "http", "method": "GET", "path": path}, None, send)
            self.assertEqual(dict(messages[0]["headers"])[trace.TRACE_HEADER].decode(), responses[path])
        async def both():
            await asyncio.gather(run("/system-summary", "recent_sync_runs"),
                                 run("/explorer/home-summary", "inventory_counts"))
        asyncio.run(both())
        self.assertEqual(len(set(responses.values())), 2)
        for path, identity in responses.items():
            operations = {e["operation"] for e in self.events if e["trace_id"] == identity}
            expected = {"system_summary", "recent_sync_runs"} if path == "/system-summary" else {"home_summary", "inventory_counts"}
            self.assertEqual(operations, expected)

    def test_event_budget_preserves_root_terminal_and_reports_omitted_observations(self):
        async def app(scope, receive, send):
            for _ in range(trace.MAX_CHILD_EVENTS + 7):
                trace.event("inventory_counts")
            trace.failure("inventory_counts", RuntimeError("failure after child budget"))
            await self.complete(send)
        self.request(app)
        self.assertEqual(len(self.events), trace.MAX_CHILD_EVENTS + 2)
        self.assertEqual(self.terminals()[0]["missing_count"], 8)
        self.assertEqual(self.terminals()[0]["status"], "partial")

    def test_unknown_codes_and_database_names_never_become_log_content(self):
        async def app(scope, receive, send):
            trace.event("PRIVATE_OPERATION")
            trace.event("inventory_counts", reason="PRIVATE_REASON")
            trace.event("inventory_counts", target="PRIVATE_TARGET")
            with trace.database_scope("PRIVATE_QUERY_WITH_ID"):
                pass
            await self.complete(send)
        self.request(app)
        self.assertNotIn("PRIVATE", json.dumps(self.events))
        self.assertTrue(any(e["operation"] == "database_read" for e in self.events))

    def test_single_observations_cannot_close_a_parent_span_in_journal_reader(self):
        from scripts.read_pipeline_journal import _unfinished_observations
        async def app(scope, receive, send):
            with trace.scope("home_summary", phase="cache"):
                trace.event("home_summary", phase="cache", status="miss")
                with trace.scope("home_summary", phase="loader"):
                    trace.unavailable("home_summary")
                    # Both enclosing spans and the HTTP root are still open.
                    pending = _unfinished_observations(self.events)
                    self.assertEqual(len(pending), 3)
                    self.assertTrue(all(item["matching"] == "terminal_not_observed" for item in pending))
            await self.complete(send)
        self.request(app)
        self.assertEqual(_unfinished_observations(self.events), [])

    def test_observer_failure_cannot_replace_domain_exception_or_response(self):
        error = ValueError("domain error")
        async def fail(scope, receive, send):
            with trace.scope("inventory_counts"):
                raise error
        async def success(scope, receive, send):
            await self.complete(send, body=b"unchanged")
        with patch.object(trace.telemetry, "emit_event", side_effect=RuntimeError("broken observer")):
            with self.assertRaises(ValueError) as caught:
                self.request(fail)
            self.assertIs(caught.exception, error)
            self.assertEqual(self.request(success)[-1]["body"], b"unchanged")


if __name__ == "__main__":
    unittest.main()
