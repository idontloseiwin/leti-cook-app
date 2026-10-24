"""Passive trace privacy, correlation, loss visibility and bounded journal checks."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
import pipeline_trace as trace
import telemetry
from telemetry_contract import build_event, encode_event, normalize_stored_event, TelemetryContractError
from telemetry_sink import JsonlTelemetryJournal, TelemetryEmitter, MAX_READ_BYTES, configured_max_bytes, configured_path

VIDEO = "abcdefghijk"
CHANNEL = "UC" + "a" * 22


class TraceSetup(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE="self-hosted")
        self.env.start()
        self.addCleanup(self.env.stop)


class TraceTests(TraceSetup):
    def test_manifest_full_correlation_fits_default_event_budget_and_roundtrips(self):
        facts = dict(trace_id="a" * 32, span_id="b" * 32, parent_span_id="c" * 32,
                     operation_id="operation-123", sync_run_id=50, job_id="12345678-1234-1234-1234-123456789abc",
                     channel_id=CHANNEL, video_id=VIDEO, batch_id="d" * 32,
                     request_attempt_id="e" * 32, retry_of_request_attempt_id="f" * 32,
                     reason="scheduled", error_code="55P03", error_type="LockNotAvailable", due_at="2026-10-15T00:00:00Z", observed_at="2026-10-15T00:00:01Z",
                     requested_count=50, received_count=49, missing_count=1, quota_bucket="video_batch_get_stats", retry_after_seconds=30,
                     video_type="short", attempt=1, inflight=8, video_ids=[str(i).zfill(11) for i in range(50)],
                     item_index=49, endpoint="videos:batchGetStats", writer_id="0" * 32, sequence=23,
                     dropped_total=1, invalid_total=1, sink_errors_total=0, monotonic_ns=1234567890123456)
        item = build_event("pipeline", "collector", "collector", "request_finished", status="received", **facts)
        self.assertLessEqual(len(encode_event(item)), 4096)
        self.assertEqual(normalize_stored_event(item), item)

    def test_retry_delay_roundtrips_as_an_exact_optional_integer(self):
        for value in (0, 30, 2**53 - 1, "30"):
            with self.subTest(value=value):
                item = build_event("pipeline", "collector", "collector", "video_write",
                                   status="confirmed", retry_after_seconds=value)
                encoded = json.loads(encode_event(item))
                self.assertEqual(encoded["retry_after_seconds"], int(value))
                self.assertIs(type(encoded["retry_after_seconds"]), int)
                self.assertEqual(normalize_stored_event(encoded), item)
                self.assertNotIn("due_at", item)  # A requested delay is not a stored deadline.
        item = build_event("pipeline", "collector", "collector", "video_write",
                           status="confirmed", retry_after_seconds=None)
        self.assertNotIn("retry_after_seconds", item)

    def test_retry_delay_rejects_invalid_or_inexact_values(self):
        base = build_event("pipeline", "collector", "collector", "video_write", status="confirmed")
        for value in (-1, True, False, 0.5, float("nan"), float("inf"), 2**53, "-1", "0.5", {}, []):
            with self.subTest(value=value):
                with self.assertRaises(TelemetryContractError):
                    build_event("pipeline", "collector", "collector", "video_write", retry_after_seconds=value)
                self.assertIsNone(normalize_stored_event({**base, "retry_after_seconds": value}))

    def test_rejects_raw_payload_secret_and_invalid_identity(self):
        invalid = [dict(payload={"items": []}), dict(view_count=9000), dict(reason=ValueError("secret")),
                   dict(video_ids=[VIDEO] * 51), dict(video_ids=["title"]), dict(channel_id="A title"),
                   dict(request_attempt_id="AIza" + "A" * 35), dict(endpoint="https://youtube/?key=secret"),
                   dict(due_at="2026-10-15T00:00:00"), dict(inflight=-1), dict(monotonic_ns=True)]
        for facts in invalid:
            with self.subTest(facts=tuple(facts)):
                with self.assertRaises(TelemetryContractError):
                    build_event("pipeline", "collector", "collector", "sent", **facts)

    def test_stored_reader_is_profile_free_and_never_revives_legacy_detail(self):
        row = dict(schema_version=1, id="old-1", ts="2026-10-15T00:00:00Z",
                   kind="phase", source="collector", target="collector", operation="collect",
                   detail="Historical provider payload must not be displayed")
        with patch.dict(os.environ, {}, clear=True):
            normalized = normalize_stored_event(row)
            self.assertIsNotNone(normalized)
            self.assertNotIn("detail", normalized)
            self.assertNotIn("LIC_DEPLOYMENT_PROFILE", os.environ)

    def test_legacy_rows_still_read_and_unknown_fields_still_rejected(self):
        item = build_event("phase", "sync_engine", "sync_engine", "sync", status="completed")
        for version in (None, 0, 1, 2):
            old = dict(item)
            old["schema_version"] = version
            self.assertIsNotNone(normalize_stored_event(old))
        self.assertIsNone(normalize_stored_event(dict(item, payload={"secret": "x"})))

    def test_telemetry_failure_does_not_replace_domain_exception_or_leak_context(self):
        failure = RuntimeError("domain failure")
        with patch.object(telemetry, "emit_event", side_effect=OSError("sink broke")):
            with self.assertRaises(RuntimeError) as caught:
                with trace.scope("collection", sync_run_id=5):
                    raise failure
            self.assertIs(caught.exception, failure)
        self.assertEqual(dict(trace.capture()), {})

    def test_finish_is_unknown_without_confirmation_and_exception_preserves_unknown(self):
        rows = []
        with patch.object(telemetry, "emit_event", side_effect=lambda *args, **kw: rows.append(kw)):
            with trace.scope("collection"):
                pass
            with self.assertRaises(ValueError):
                with trace.scope("collection") as span:
                    span.finish("completed")
                    raise ValueError("failure after finish")
        self.assertEqual([r["status"] for r in rows], ["started", "unknown", "started", "unknown"])
        self.assertTrue(all(r["duration_ms"] >= 0 for r in rows if "duration_ms" in r))

    def test_parallel_handoffs_have_common_trace_distinct_spans_and_no_pool_leak(self):
        rows = []
        lock = threading.Lock()
        def emit(*args, **kw):
            row = build_event(*args, **kw)
            with lock:
                rows.append(row)
        def work(index):
            with trace.scope("video", video_id=str(index).zfill(11)) as span:
                trace.event("stored", "confirmed")
                span.finish("completed")
        with patch.object(telemetry, "emit_event", side_effect=emit):
            with trace.scope("sync", sync_run_id=7) as root:
                parent = trace.capture()
                with ThreadPoolExecutor(max_workers=8) as pool:
                    for future in [pool.submit(trace.run, parent, work, i) for i in range(24)]:
                        future.result()
                    self.assertTrue(all(not dict(pool.submit(trace.capture).result()) for _ in range(8)))
                root.finish("completed")
        stored = [r for r in rows if r["operation"] == "stored"]
        self.assertEqual(len(stored), 24)
        self.assertEqual(len({r["trace_id"] for r in rows}), 1)
        self.assertEqual(len({r["span_id"] for r in stored}), 24)
        self.assertEqual({r["parent_span_id"] for r in stored}, {parent["span_id"]})
        self.assertEqual(dict(trace.capture()), {})

    def test_bind_does_not_emit_lifecycle_and_run_none_inherits(self):
        rows = []
        with patch.object(telemetry, "emit_event", side_effect=lambda *a, **kw: rows.append(kw)):
            with trace.bind(batch_id="batch-1", video_ids=[VIDEO]):
                context = trace.capture()
                self.assertEqual(context["video_ids"], (VIDEO,))
                self.assertEqual(trace.run(None, lambda: trace.capture()["batch_id"]), "batch-1")
                with self.assertRaises(TypeError):
                    context["batch_id"] = "bad"
            self.assertEqual(dict(trace.capture()), {})
        self.assertEqual(rows, [])

    def test_scope_manifest_is_an_immutable_context_snapshot(self):
        ids = [VIDEO]
        with patch.object(telemetry, "emit_event"):
            with trace.scope("batch", video_ids=ids):
                captured = trace.capture()
                ids.append("zyxwvutsrqp")
                self.assertEqual(captured["video_ids"], (VIDEO,))
                self.assertEqual(trace.capture()["video_ids"], (VIDEO,))

    def test_clock_duration_uses_monotonic_even_wall_clock_moves(self):
        rows = []
        with patch.object(telemetry, "emit_event", side_effect=lambda *a, **kw: rows.append(kw)):
            with patch.object(trace.time, "monotonic_ns", side_effect=[100, 110, 250, 260]):
                with trace.scope("sync") as span:
                    span.finish("completed")
        self.assertEqual(rows[-1]["duration_ms"], 150 / 1_000_000)


class JournalTests(TraceSetup):
    def journal(self, directory, **kw):
        return JsonlTelemetryJournal(Path(directory) / "events.jsonl", **kw)

    def test_queue_overflow_and_sink_loss_visible_in_next_event(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.journal(directory)
            emitter = TelemetryEmitter(journal, queue_size=1, batch_size=1)
            with patch.object(emitter, "_ensure_thread"):
                first = emitter.emit("pipeline", "collector", "collector", "batch", status="started")
                self.assertIsNone(emitter.emit("pipeline", "collector", "collector", "batch", status="started"))
                terminal = emitter.emit("pipeline", "collector", "collector", "batch", status="completed")
                self.assertEqual((first["sequence"], terminal["sequence"]), (1, 3))
                emitter.buffer.get_batch(1, 0)
                emitter.buffer.task_done(1)
                fourth = emitter.emit("pipeline", "collector", "collector", "next", status="started")
                self.assertEqual(fourth["dropped_total"], 2)
                self.assertEqual(fourth["writer_id"], first["writer_id"])
            emitter.buffer.get_batch(1, 0)
            emitter.buffer.task_done(1)
            emitter.close(0)

    def test_sink_failure_is_passive_and_loss_counter_survives_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.journal(directory)
            emitter = TelemetryEmitter(journal, queue_size=4, batch_size=1)
            with patch.object(journal, "append_batch", side_effect=OSError("disk full")):
                emitter.emit("pipeline", "collector", "collector", "stored", status="confirmed")
                self.assertTrue(emitter.flush(2))
            next_row = emitter.emit("pipeline", "collector", "collector", "next", status="started")
            self.assertTrue(emitter.flush(2))
            self.assertEqual(next_row["dropped_total"], 1)
            self.assertEqual(next_row["sink_errors_total"], 1)
            self.assertEqual(len(journal.tail_snapshot()["events"]), 1)
            emitter.close(1)

    def test_rotation_and_tail_do_not_read_whole_file(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.journal(directory, max_bytes=4096)
            rows = [build_event("pipeline", "collector", "collector", "event", status="ok", item_index=i) for i in range(80)]
            with patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                journal.append_batch(rows)
                tail = journal.tail_snapshot(4)
            self.assertLessEqual(journal.path.stat().st_size, 4096)
            self.assertEqual([r["item_index"] for r in tail["events"]], [76, 77, 78, 79])

    def test_read_cursor_preserves_all_rows_instead_of_skipping_to_last_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.journal(directory)
            journal.append_batch([build_event("pipeline", "collector", "collector", "event", item_index=i) for i in range(13)])
            state = journal.state()
            position = 0
            indexes = []
            while position < state["size"]:
                result = journal.read_since(position, state["generation"], limit=3)
                self.assertGreater(result["position"], position)
                indexes.extend(r["item_index"] for r in result["events"])
                position = result["position"]
            self.assertEqual(indexes, list(range(13)))

    def test_read_byte_budget_keeps_cursor_before_unreturned_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = self.journal(directory)
            rows = [build_event("pipeline", "collector", "collector", "event", item_index=i,
                                video_ids=[str(n).zfill(11) for n in range(50)]) for i in range(1800)]
            journal.append_batch(rows)
            state = journal.state()
            first = journal.read_since(0, state["generation"], limit=2000)
            self.assertLessEqual(first["position"], MAX_READ_BYTES)
            self.assertLess(first["position"], state["size"])
            second = journal.read_since(first["position"], state["generation"], limit=2000)
            self.assertEqual([r["item_index"] for r in first["events"] + second["events"]], list(range(1800)))

    def test_state_volume_path_survives_recreation_and_override_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"TELEMETRY_PATH": "", "LIC_AUTH_DATABASE": str(Path(directory) / "auth.sqlite3")}):
                self.assertEqual(configured_path(), Path(directory) / "pipeline-telemetry.jsonl")
                with patch.dict(os.environ, {"TELEMETRY_PATH": str(Path(directory) / "custom.jsonl")}):
                    self.assertEqual(configured_path(), Path(directory) / "custom.jsonl")
            with patch.dict(os.environ, {"TELEMETRY_PATH": "", "LIC_AUTH_DATABASE": "relative.sqlite3"}):
                self.assertEqual(configured_path(), Path(tempfile.gettempdir()) / "youtube-tracker-telemetry.jsonl")

    def test_disk_configuration_has_safe_default_and_upper_bound(self):
        with patch.dict(os.environ, {"TELEMETRY_MAX_BYTES": "999999999999"}):
            self.assertEqual(configured_max_bytes(), 512 * 1024 * 1024)
        with patch.dict(os.environ, {"TELEMETRY_MAX_BYTES": "invalid"}):
            self.assertEqual(configured_max_bytes(), 128 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
