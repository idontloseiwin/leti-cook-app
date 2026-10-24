"""Real transport path, fake network: passive facts must not change effects."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from test_provider_transport_integration import TransportTests, Response, client
import pipeline_trace
import telemetry
from telemetry_sink import JsonlTelemetryJournal, TelemetryEmitter
import requests

class ProviderTraceTests(unittest.TestCase):
    setUp = TransportTests.setUp
    request = TransportTests.request
    assert_released = TransportTests.assert_released

    def observe(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        journal = JsonlTelemetryJournal(Path(temp.name) / "events.jsonl")
        emitter = TelemetryEmitter(journal, queue_size=256, batch_size=16)
        self.addCleanup(emitter.close)
        context = patch.object(telemetry, "default_emitter", return_value=emitter)
        context.start()
        self.addCleanup(context.stop)
        self.recorder.finish_attempt.return_value = True
        return emitter, journal

    def events(self, emitter, journal):
        self.assertTrue(emitter.flush(2))
        return journal.tail_snapshot(limit=200)["events"]

    def test_transport_links_to_run_and_attempt_without_payload(self):
        emitter, journal = self.observe()
        with pipeline_trace.scope("auto_sync", sync_run_id=81) as run:
            result = self.request()
            run.finish("completed")
        rows = self.events(emitter, journal)
        self.assertEqual(result.json(), {"items": []})
        boundaries = [r for r in rows if r["operation"] in {"http_started", "http_finished"}]
        self.assertEqual(len(boundaries), 2)
        self.assertEqual({r["sync_run_id"] for r in boundaries}, {81})
        self.assertEqual({r["request_attempt_id"] for r in boundaries}, {"attempt"})
        self.assertEqual(len({r["span_id"] for r in boundaries}), 1)
        self.assertEqual(boundaries[0]["requested_count"], 1)
        text = json.dumps(rows)
        self.assertNotIn("synthetic-key", text)
        self.assertNotIn('"items"', text)
        self.assertNotIn("headers", text)
        self.assertEqual(emitter.metrics()["invalid"], 0)
        self.assert_released()

    def test_wait_cancel_is_not_sent_or_accounted(self):
        emitter, journal = self.observe()
        self.gate.on_enter = lambda: setattr(self.ready, "side_effect", RuntimeError("hot_unavailable"))
        with self.assertRaisesRegex(RuntimeError, "hot_unavailable"):
            self.request()
        rows = self.events(emitter, journal)
        self.assertIn("provider_waiting", {r["operation"] for r in rows})
        self.assertNotIn("http_started", {r["operation"] for r in rows})
        self.assertEqual(rows[-1]["status"], "unknown")
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_http_and_accounting_outcomes_are_separate(self):
        emitter, journal = self.observe()
        self.recorder.finish_attempt.return_value = False
        self.response = Response(self.gate, status=403)
        self.request()
        rows = self.events(emitter, journal)
        end = next(r for r in rows if r["operation"] == "http_finished")
        accounting = next(r for r in rows if r["operation"] == "quota_accounting")
        self.assertEqual((end["http_status"], end["status"]), (403, "error"))
        self.assertEqual(accounting["status"], "unknown")
        self.assert_released()

    def test_sink_failure_keeps_original_network_result_and_exception(self):
        emitter, journal = self.observe()
        with patch.object(journal, "append_batch", side_effect=OSError("full")):
            self.assertIs(self.request(), self.response)
            self.http.side_effect = requests.Timeout("private transport content")
            with self.assertRaisesRegex(requests.Timeout, "private transport content"):
                self.request()
            emitter.flush(2)
        self.assertEqual(self.http.call_count, 2)
        self.assert_released(attempts=2)
        self.assertGreater(emitter.metrics()["sink_errors"], 0)

    def test_stats_endpoint_has_valid_correlation(self):
        emitter, journal = self.observe()
        client.youtube_api_get("videos:batchGetStats", phase="test", quota_bucket="video_batch_get_stats",
                               attempt_recorder=self.recorder, params={"id": "v0000000001"})
        rows = self.events(emitter, journal)
        start = next(r for r in rows if r["operation"] == "http_started")
        self.assertEqual(start["quota_bucket"], "video_batch_get_stats")
        self.assertEqual(emitter.metrics()["invalid"], 0)
