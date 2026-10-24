"""Bounded diagnostic projections never invent completed work or hidden history."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from telemetry_contract import build_event, encode_event
from scripts.read_pipeline_journal import read_journal

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.jsonl"
        self.sequence = 0

    def row(self, operation, status, **fields):
        self.sequence += 1
        fields.setdefault("sequence", self.sequence)
        fields.setdefault("writer_id", "1" * 32)
        return build_event("pipeline", "collector", "collector", operation, status=status,
                           timestamp="2026-10-15T10:00:00+00:00", **fields)

    def write(self, *rows):
        self.path.write_bytes(b"".join(encode_event(row)+b"\n" for row in rows))

    def test_video_filter_links_actual_batch_not_nearby_video(self):
        self.write(self.row("auto_sync", "started", span_id="2"*32, trace_id="3"*32),
                   self.row("selected", "ok", span_id="4"*32, parent_span_id="2"*32,
                            batch_id="batch1", video_id="v0000000001"),
                   self.row("http_started", "started", span_id="5"*32, batch_id="batch1", monotonic_ns=1),
                   self.row("unrelated", "ok", video_id="v0000000002"))
        result = read_journal(self.path, video_id="v0000000001")
        self.assertEqual([r["operation"] for r in result["events"]], ["auto_sync", "selected", "http_started"])
        self.assertFalse(result["complete_history"])
        self.assertEqual(result["transport"]["unpaired_boundaries"], 1)
        self.assertTrue(result["unfinished_observations"])

    def test_real_http_overlap_not_queue_duration(self):
        rows = [self.row("provider_waiting", "started", span_id="9"*32, monotonic_ns=0)]
        for operation,status,span,clock in [("http_started","started","2",10),("http_started","started","3",20),
                                            ("http_finished","received","2",30),("http_finished","received","3",40)]:
            rows.append(self.row(operation,status,span_id=span*32,monotonic_ns=clock,endpoint="videos",requested_count=50))
        self.write(*rows)
        result = read_journal(self.path)
        metric = result["transport"]["writers"][0]
        self.assertEqual(metric["observed_peak"], 2)
        self.assertAlmostEqual(metric["average_in_observed_window"], 40/30)
        self.assertEqual(result["transport"]["request_sizes"], {"videos": {50: 2}})

    def test_loss_corruption_rotation_and_limits_are_visible(self):
        rows = [self.row("step", "ok", sequence=i, dropped_total=2) for i in (1,4,5)]
        self.write(*rows)
        with self.path.open("ab") as stream:
            stream.write(b"{bad}\n")
        result = read_journal(self.path, limit=1)
        self.assertEqual(result["coverage"]["sequence_gaps"]["1"*32], 2)
        self.assertEqual(result["coverage"]["invalid_records"], 1)
        self.assertTrue(result["coverage"]["output_truncated"])
        tail = read_journal(self.path, max_bytes=300)
        self.assertTrue(tail["coverage"]["head_truncated"])
        bounded = read_journal(self.path, max_records=1)
        self.assertEqual(bounded["coverage"]["records_evicted_from_reader"], 2)

    def test_raw_secret_payload_is_rejected_not_echoed(self):
        row = self.row("step", "ok")
        row["raw_payload"] = "private-secret"
        self.path.write_text(json.dumps(row)+"\n", encoding="utf-8")
        result = read_journal(self.path)
        self.assertEqual(result["events"], [])
        self.assertNotIn("private-secret", json.dumps(result))

    def test_inventory_provider_and_settlement_link_by_explicit_operation(self):
        self.write(self.row("http_started", "started", trace_id="2"*32, span_id="3"*32, operation_id="page1"),
                   self.row("inventory_write", "confirmed", trace_id="2"*32, span_id="4"*32, operation_id="page1", video_ids=["v0000000001"]),
                   self.row("http_started", "started", trace_id="5"*32, span_id="6"*32, operation_id="page1"))
        result = read_journal(self.path, video_id="v0000000001")
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["span_id"], "3"*32)
        self.assertIn(result["events"][0]["id"], result["context_event_ids"])

    def test_actual_scope_and_receipt_ends_ignore_changed_reason(self):
        import pipeline_trace
        rows = []

        def capture(kind, source, target, operation, *, status, **facts):
            row = self.row(operation, status, **facts)
            rows.append(row)
            return row

        with patch("pipeline_trace.telemetry.emit_event", capture):
            with pipeline_trace.scope("auto_worker") as worker:
                pipeline_trace.event("sync_run_receipt", "started", sync_run_id=172, reason="auto_sync")
                with pipeline_trace.scope("auto_sync", sync_run_id=172, reason="scheduled") as sync:
                    for outcome, reason in (("completed", None), ("deferred", "quota_exhausted")):
                        with pipeline_trace.scope("collection_phase", phase="video_collection") as phase:
                            phase.finish(outcome, reason=reason)
                    sync.finish("completed", reason=None)
                pipeline_trace.event("sync_run_receipt", "confirmed", sync_run_id=172, reason="completed")
                # The worker learns the run ID only after it has started.
                worker.finish("completed", sync_run_id=172)
        self.write(*rows)
        result = read_journal(self.path, sync_run_id=172)
        self.assertEqual(result["unfinished_observations"], [])
        self.assertEqual(result["coverage"]["invalid_records"], 0)
        self.assertTrue(any(row.get("reason") == "quota_exhausted" for row in result["events"]))

    def test_earlier_completion_does_not_hide_repeated_start(self):
        identity = dict(trace_id="2"*32, span_id="3"*32, batch_id="batch1", video_id="v0000000001")
        first = self.row("video_write", "started", reason="complete_stats", **identity)
        completed = self.row("video_write", "confirmed", reason="complete_stats", **identity)
        later = self.row("video_write", "started", reason="complete_stats", **identity)
        self.write(first, completed, later)
        opened = read_journal(self.path)["unfinished_observations"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["start_event_id"], later["id"])
        self.assertEqual(opened[0]["matching"], "terminal_not_observed")

    def test_ambiguous_overlapping_repetitions_are_not_guessed(self):
        identity = dict(trace_id="2"*32, span_id="3"*32, video_id="v0000000001")
        first = self.row("video_write", "started", **identity)
        second = self.row("video_write", "started", **identity)
        self.write(first, second, self.row("video_write", "confirmed", **identity))
        opened = read_journal(self.path)["unfinished_observations"]
        self.assertEqual([row["start_event_id"] for row in opened], [first["id"], second["id"]])
        self.assertEqual({row["matching"] for row in opened}, {"ambiguous_repeated_identity"})

    def test_shared_span_does_not_mix_video_or_operation_identity(self):
        shared = dict(trace_id="2"*32, span_id="3"*32, batch_id="batch1")
        unfinished = self.row("video_write", "started", video_id="v0000000001", **shared)
        self.write(unfinished,
                   self.row("video_write", "started", video_id="v0000000002", **shared),
                   self.row("video_write", "confirmed", video_id="v0000000002", **shared),
                   self.row("video_batch", "completed", video_id="v0000000001", **shared))
        opened = read_journal(self.path, video_id="v0000000001")["unfinished_observations"]
        self.assertEqual([row["start_event_id"] for row in opened], [unfinished["id"]])

    def test_writer_trace_and_unit_identity_do_not_cross_close(self):
        identity = dict(writer_id="1"*32, trace_id="2"*32, span_id="3"*32,
                        batch_id="batch1", operation_id="work1", video_id="v0000000001",
                        channel_id="UC" + "a"*22, job_id="1", request_attempt_id="request1")
        for field, replacement in (("writer_id", "4"*32), ("trace_id", "5"*32),
                                   ("batch_id", "batch2"), ("operation_id", "work2"),
                                   ("channel_id", "UC" + "b"*22), ("job_id", "2"),
                                   ("request_attempt_id", "request2")):
            with self.subTest(field=field):
                first = self.row("video_write", "started", **identity)
                self.write(first, self.row("video_write", "confirmed", **(identity | {field: replacement})))
                opened = read_journal(self.path)["unfinished_observations"]
                self.assertEqual([row["start_event_id"] for row in opened], [first["id"]])

    def test_unknown_ack_remains_unknown_and_cannot_finish_later_attempt(self):
        identity = dict(trace_id="2"*32, span_id="3"*32, video_id="v0000000001")
        first = self.row("video_write", "started", reason="complete_stats", **identity)
        unknown = self.row("video_write", "unknown", reason="complete_stats", **identity)
        later = self.row("video_write", "started", reason="complete_stats", **identity)
        self.write(first, unknown, later,
                   self.row("video_batch", "completed", **identity))
        result = read_journal(self.path)
        self.assertEqual([row["start_event_id"] for row in result["unfinished_observations"]], [later["id"]])
        self.assertEqual(next(row["status"] for row in result["events"] if row["id"] == unknown["id"]), "unknown")

    def test_retained_tail_does_not_invent_missing_start_or_reuse_old_end(self):
        identity = dict(trace_id="2"*32, span_id="3"*32)
        self.write(self.row("collection_phase", "started", reason="scheduled", **identity),
                   self.row("collection_phase", "completed", **identity))
        complete = read_journal(self.path, limit=1)
        self.assertTrue(complete["coverage"]["output_truncated"])
        self.assertEqual(complete["unfinished_observations"], [])
        tail = read_journal(self.path, max_records=1)
        self.assertTrue(tail["coverage"]["head_truncated"])
        self.assertEqual(tail["unfinished_observations"], [])
        later = self.row("collection_phase", "started", reason="scheduled", **identity)
        with self.path.open("ab") as stream:
            stream.write(encode_event(later) + b"\n")
        tail = read_journal(self.path, max_records=1)
        self.assertEqual([row["start_event_id"] for row in tail["unfinished_observations"]], [later["id"]])
