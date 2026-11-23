"""Pipeline telemetry is switched off until it is redesigned.

Nothing is queued or written, no writer thread starts, the journal is neither
read nor created, and TELEMETRY_ENABLED cannot turn it back on. No server.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import telemetry  # noqa: E402
import telemetry_routes  # noqa: E402
import telemetry_sink  # noqa: E402
from telemetry_contract import build_event, encode_event  # noqa: E402

VIDEO_ID = "dQw4w9WgXcQ"


class Forbidden:
    """A lock or factory that fails the test if anything reaches it."""

    def __call__(self, *args, **kwargs):
        raise AssertionError("telemetry was used while switched off")

    acquire = release = __enter__ = __exit__ = __call__


def writer_threads():
    return [thread for thread in threading.enumerate() if thread.name == "telemetry-jsonl-writer"]


class SwitchedOffTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        # The installation's state-volume journal, and an explicit request to enable it.
        environment = patch.dict(os.environ, {
            "TELEMETRY_PATH": "", "TELEMETRY_ENABLED": "true", "LIC_DEPLOYMENT_PROFILE": "self-hosted",
            "LIC_AUTH_DATABASE": str(self.folder / "auth.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        self.journal = self.folder / "pipeline-telemetry.jsonl"
        self.assertEqual(telemetry_sink.configured_path(), self.journal)

    def test_the_product_switch_is_off_and_telemetry_enabled_cannot_turn_it_on(self):
        self.assertIs(telemetry_sink.PIPELINE_TELEMETRY_OFF, True)
        self.assertFalse(telemetry_sink.pipeline_telemetry_on())
        self.assertFalse(telemetry.is_enabled())

    def test_emit_is_a_no_op_without_emitter_lock_thread_or_file(self):
        before = writer_threads()
        with patch.object(telemetry_sink, "_EMITTER_LOCK", Forbidden()), \
             patch.object(telemetry, "default_emitter", Forbidden()), \
             patch.dict(telemetry_sink._EMITTERS, clear=True):
            for index in range(50):
                self.assertIsNone(telemetry.emit_event("pipeline", "collector", "collector", "video_batch",
                                                       status="completed", count=index, video_id=VIDEO_ID))
            self.assertIsNone(telemetry.emit_phase("video_collection", "started"))
            self.assertTrue(telemetry.flush_telemetry(1.0))
            self.assertEqual(telemetry_sink._EMITTERS, {})
        self.assertEqual(writer_threads(), before)
        self.assertEqual(list(self.folder.iterdir()), [])
        metrics = telemetry.telemetry_metrics()
        self.assertTrue(metrics["off"])
        self.assertEqual((metrics["emitted"], metrics["written"], metrics["sink_errors"]), (0, 0, 0))

    def test_an_older_journal_is_never_read_or_locked(self):
        old = encode_event(build_event("pipeline", "collector", "collector", "video_batch",
                                       status="completed", video_id=VIDEO_ID)) + b"\n"
        self.journal.write_bytes(old)
        with patch.object(telemetry_sink, "journal_file_lock", Forbidden()):
            self.assertEqual(telemetry.telemetry_tail_snapshot(240)["events"], [])
            self.assertEqual(telemetry.read_events_since(0, None, 240)["events"], [])
            self.assertTrue(telemetry.telemetry_file_state()["off"])
            self.assertEqual(telemetry.recent_events(), [])
        self.assertEqual(self.journal.read_bytes(), old)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["pipeline-telemetry.jsonl"])

    def test_routes_say_off_and_expose_no_older_events(self):
        self.journal.write_bytes(encode_event(build_event(
            "pipeline", "collector", "collector", "video_batch", status="completed", video_id=VIDEO_ID)) + b"\n")
        app = FastAPI()
        app.include_router(telemetry_routes.router)
        response = TestClient(app).get("/telemetry/recent?limit=240")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "off", "events": []})
        self.assertEqual(response.headers["X-Telemetry-Status"], "off")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(VIDEO_ID, response.text)

        async def stream():
            request = types.SimpleNamespace(headers={}, query_params={}, is_disconnected=Forbidden())
            response = await telemetry_routes.telemetry_stream(request)
            return [chunk async for chunk in response.body_iterator]
        with patch.object(telemetry_routes, "telemetry_tail_snapshot", Forbidden()), \
             patch.object(telemetry_routes, "read_events_since", Forbidden()):
            chunks = asyncio.run(stream())
        self.assertEqual(chunks, ['retry: 3600000\nevent: telemetry-status\ndata: {"status":"off"}\n\n'])
        self.assertEqual(telemetry_routes._streams["active"], 0)
        self.assertFalse((self.folder / "pipeline-telemetry.jsonl.lock").exists())

    def test_a_disabled_emitter_returns_before_any_lock_queue_or_thread(self):
        emitter = telemetry_sink.TelemetryEmitter(
            telemetry_sink.JsonlTelemetryJournal(self.journal), queue_size=16, batch_size=1, enabled=False)
        emitter._emit_lock = Forbidden()
        emitter._metrics_lock = Forbidden()
        emitter._start_lock = Forbidden()
        self.assertIsNone(emitter.emit("pipeline", "collector", "collector", "video_batch", status="completed"))
        self.assertIsNone(emitter._thread)
        self.assertEqual(emitter.buffer.size(), 0)
        self.assertFalse(self.journal.exists())

    def test_the_product_emitter_is_disabled_even_when_reached(self):
        with patch.dict(telemetry_sink._EMITTERS, clear=True):
            emitter = telemetry_sink.default_emitter()
            self.assertFalse(emitter.enabled)
            self.assertIsNone(emitter.emit("pipeline", "collector", "collector", "video_batch", status="completed"))
            self.assertIsNone(emitter._thread)
        self.assertFalse(self.journal.exists())

    def test_fixture_can_enable_pipeline_telemetry(self):
        from telemetry_isolation import telemetry_switched_on
        case = unittest.TestCase()
        telemetry_switched_on(case)
        try:
            self.assertTrue(telemetry_sink.pipeline_telemetry_on())
            with patch.dict(os.environ, {"TELEMETRY_ENABLED": "false"}):
                self.assertFalse(telemetry_sink.pipeline_telemetry_on())
        finally:
            case.doCleanups()
        self.assertFalse(telemetry_sink.pipeline_telemetry_on())


if __name__ == "__main__":
    unittest.main()
