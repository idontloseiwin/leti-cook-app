"""Source availability is not an empty diagnostic sample or a domain event."""
import asyncio
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
import telemetry_routes as routes


def snapshot(*, failed=False, position=10, generation="journal", events=None):
    return {"read_error": failed, "position": position, "generation": generation,
            "events": events or []}


class TelemetryReadStatusTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes.router)

    def test_recent_failure_is_explicit_and_does_not_expose_diagnostics(self):
        failed = snapshot(failed=True)
        failed["detail"] = "DO_NOT_EXPOSE"
        with patch.object(routes, "telemetry_tail_snapshot", return_value=failed):
            response = TestClient(self.app).get("/telemetry/recent?limit=240")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["X-Telemetry-Status"], "unavailable")
        self.assertEqual(response.json(), {"status": "unavailable", "events": []})
        self.assertNotIn("DO_NOT_EXPOSE", response.text)

    def test_empty_read_is_success_and_limit_remains_bounded(self):
        with patch.object(routes, "telemetry_tail_snapshot", return_value=snapshot()) as read:
            response = TestClient(self.app).get("/telemetry/recent?limit=99999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "events": []})
        read.assert_called_once_with(limit=500)

    def stream(self, initial, updates):
        remaining = len(updates)

        async def is_disconnected():
            nonlocal remaining
            if not remaining:
                return True
            remaining -= 1
            return False

        request = types.SimpleNamespace(headers={}, query_params={}, is_disconnected=is_disconnected)
        real_sleep = asyncio.sleep

        async def collect():
            response = await routes.telemetry_stream(request)
            return [chunk async for chunk in response.body_iterator]

        with patch.object(routes, "telemetry_tail_snapshot", return_value=initial), \
             patch.object(routes, "read_events_since", side_effect=updates) as reads, \
             patch.object(routes.asyncio, "sleep", lambda _: real_sleep(0)):
            chunks = asyncio.run(collect())
        self.assertEqual(routes._streams["active"], 0)
        return chunks, reads.call_args_list

    def test_initial_failure_recovery_is_named_status_not_a_fake_event(self):
        failed = snapshot(failed=True, position=0, generation=None)
        chunks, _ = self.stream(failed, [failed, snapshot(events=[{"id": "real", "operation": "observed"}])])
        controls = [part for part in chunks if part.startswith("event: telemetry-status")]
        self.assertEqual(controls, [routes._format_source_status(True), routes._format_source_status(False)])
        self.assertTrue(all("\nid:" not in part for part in controls))
        self.assertEqual(sum(part.startswith("id: real\n") for part in chunks), 1)

    def test_midstream_failure_keeps_last_good_cursor_and_recovers_once(self):
        invalid_cursor = snapshot(failed=True, position=0, generation=None)
        chunks, calls = self.stream(snapshot(position=41), [
            invalid_cursor, invalid_cursor, snapshot(position=50), snapshot(position=50),
        ])
        controls = [part for part in chunks if part.startswith("event: telemetry-status")]
        self.assertEqual(len(controls), 3)
        self.assertEqual([json.loads(part.split("data: ")[1]) for part in controls],
                         [{"status": "ok"}, {"status": "unavailable"}, {"status": "ok"}])
        self.assertEqual([call.args[:2] for call in calls],
                         [(41, "journal"), (41, "journal"), (41, "journal"), (50, "journal")])


if __name__ == "__main__":
    unittest.main()
