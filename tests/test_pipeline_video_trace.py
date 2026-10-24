"""Passive pipeline evidence through the real recurring and initial owners."""
from pathlib import Path
from threading import Barrier, Event
import sys
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
import pipeline_trace as trace
import collection_shutdown
from collection import initial_video_hydration as initial
from collection.dispatch import Completion
from test_common_video_dispatch import Repository
from test_initial_video_parallel import AtomicRepository, service, video, run


class PipelineVideoTraceTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        def emit(kind, source, target, stage, **facts):
            self.events.append(dict(stage=stage, **facts))
        self.sink = patch.object(trace.telemetry, "emit_event", side_effect=emit)
        self.sink.start()
        self.addCleanup(self.sink.stop)
        stopping = patch.object(collection_shutdown, "_requested", Event())
        stopping.start()
        self.addCleanup(stopping.stop)

    def writes(self, status):
        return [event for event in self.events
                if event["stage"] == "video_write" and event["status"] == status]

    def collector(self, repo):
        collector = service(repo, Mock())
        def full(ids, reason):
            trace.event("provider_test", "received")
            return [video(value) for value in ids]
        collector.fetch_metadata = full
        return collector

    def test_recurring_manifest_provider_and_confirmed_commit_share_batch(self):
        repo = Repository()
        collector = self.collector(repo)
        with trace.scope("auto_sync", sync_run_id=73):
            result = collector.process_common_claims(repo.claim()["rows"])
        self.assertEqual(result["updated"], 1)
        selected = next(e for e in self.events if e["stage"] == "video_selected")
        confirmed = self.writes("confirmed")
        provider = next(e for e in self.events if e["stage"] == "provider_test")
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(selected["batch_id"], provider["batch_id"])
        self.assertEqual(selected["batch_id"], confirmed[0]["batch_id"])
        self.assertEqual(selected["sync_run_id"], 73)
        self.assertEqual(selected["due_at"], repo.claim()["rows"][0].next_due_at)
        self.assertNotIn("due_at", confirmed[0])  # Writer did not return a new deadline.
        for forbidden in ("original-token", "Real normalized title", "viewCount", "likeCount"):
            self.assertNotIn(forbidden, repr(self.events))

    def test_initial_claim_failure_has_correlated_event_without_provider_or_retry(self):
        from db.base.postgres import DatabaseServerError
        repo = AtomicRepository()
        def refuse(index):
            raise DatabaseServerError("initial_video_collection_target_scope_invalid", code="P0001")
        repo.claim_hook = refuse
        provider = Mock()
        with trace.scope("initial_owner", sync_run_id=71):
            result = run(service(repo, provider), ["abcdefghijk"])
        self.assertTrue(result["settlement_unknown"])
        provider.assert_not_called()
        self.assertEqual(len(repo.claims), 1)
        claims = [e for e in self.events if e["stage"] == "video_claim"]
        self.assertEqual([e["status"] for e in claims], ["started", "unknown"])
        self.assertEqual(claims[0]["batch_id"], claims[1]["batch_id"])
        self.assertEqual(claims[1]["sync_run_id"], 71)
        self.assertEqual(claims[1]["error_code"], "P0001")
        self.assertEqual(claims[1]["error_type"], "DatabaseServerError")
        self.assertEqual(claims[1]["video_ids"], ["abcdefghijk"])
        self.assertNotIn("initial_video_collection_target_scope_invalid", repr(self.events))
        self.assertFalse(any(e["stage"] == "video_batch_manifest" for e in self.events))

    def test_unknown_write_never_claims_success_or_replays_failure(self):
        repo = Repository()
        repo.fail_commit = True
        result = self.collector(repo).process_common_claims(repo.claim()["rows"])
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(len(self.writes("unknown")), 1)
        self.assertEqual(self.writes("unknown")[0]["error_type"], "OSError")
        self.assertNotIn("synthetic unknown commit", repr(self.events))
        self.assertEqual(self.writes("confirmed"), [])
        self.assertEqual(repo.writes, [])

    def test_stats_absence_is_deferred_general_not_unavailable(self):
        repo = Repository()
        repo.mode = "counters"
        collector = self.collector(repo)
        collector.fetch_counters = lambda ids, reason: {"items": [], "summary": {"failed_ids": ids}}
        result = collector.process_common_claims(repo.claim()["rows"])
        self.assertEqual(result["unavailable_marked"], 0)
        self.assertEqual(self.writes("confirmed")[0]["reason"], "youtube_stats_counters_unavailable")
        self.assertEqual(self.writes("confirmed")[0]["retry_after_seconds"], 30)
        self.assertNotIn("due_at", self.writes("confirmed")[0])
        self.assertEqual([operation for operation, rows in repo.writes], ["failure"])

    def test_initial_atomic_plan_does_not_log_commit_before_actual_ack(self):
        repo = AtomicRepository()
        def fail_commit(index, commands):
            self.assertEqual(self.writes("confirmed"), [])
            raise OSError("synthetic unknown commit")
        repo.commit_hook = fail_commit
        collector = service(repo, lambda ids, reason: [video(value) for value in ids])
        result = run(collector, ["abcdefghijk"])
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["updated"], 0)
        self.assertEqual(self.writes("confirmed"), [])
        self.assertEqual(len(self.writes("unknown")), 1)
        self.assertFalse(any(e["stage"] == "video_batch" and e["status"] == "completed" for e in self.events))

    def test_two_parallel_initial_batches_preserve_distinct_manifest_context(self):
        repo = AtomicRepository()
        gate = Barrier(2)
        def fetch(ids, reason):
            trace.event("provider_test", "received", video_ids=ids)
            gate.wait(timeout=3)
            return [video(value) for value in ids]
        with trace.scope("operator", job_id="job"):
            result = run(service(repo, fetch), [f"video{i:06d}" for i in range(100)], concurrency=2)
        self.assertEqual(result["updated"], 100)
        providers = [e for e in self.events if e["stage"] == "provider_test"]
        self.assertEqual(len({e["batch_id"] for e in providers}), 2)
        by_video = {identity: event["batch_id"] for event in providers for identity in event["video_ids"]}
        self.assertEqual(len(self.writes("confirmed")), 100)
        for event in self.writes("confirmed"):
            self.assertEqual(event["batch_id"], by_video[event["video_id"]])
            self.assertEqual(event["job_id"], "job")

    def test_sink_failure_does_not_change_collection(self):
        repo = Repository()
        with patch.object(trace.telemetry, "emit_event", side_effect=OSError("disk unavailable")):
            result = self.collector(repo).process_common_claims(repo.claim()["rows"])
        self.assertEqual((result["updated"], result["settlement_unknown"], result["errors"]), (1, False, 0))
        self.assertEqual([operation for operation, rows in repo.writes], ["metadata"])

    def test_interleaved_generators_do_not_leak_context_and_cancel_without_write(self):
        repo = Repository()
        collector = self.collector(repo)
        with trace.bind(operation_id="owner"):
            first = collector.common_claim_steps(repo.claim()["rows"])
            second = collector.common_claim_steps(repo.claim()["rows"])
            a, b = next(first), next(second)
            self.assertNotIn("batch_id", trace.capture())
            self.assertNotEqual(a.trace_context["batch_id"], b.trace_context["batch_id"])
            first.close()
            second.close()
            self.assertEqual(trace.capture()["operation_id"], "owner")
        self.assertEqual(repo.writes, [])
        self.assertEqual(len([e for e in self.events if e["stage"] == "video_batch" and e["status"] == "interrupted"]), 2)

    def test_real_jsonl_contract_accepts_actual_identity_shapes(self):
        from telemetry_sink import JsonlTelemetryJournal, TelemetryEmitter
        with TemporaryDirectory(prefix="pipeline-video-proof-") as temp:
            path = Path(temp) / "events.jsonl"
            emitter = TelemetryEmitter(JsonlTelemetryJournal(path, max_bytes=1024 * 1024),
                                       queue_size=256, batch_size=32)
            # setUp captures the facade for other tests. This check deliberately
            # restores the real producer/validator/sink, with a real UUID job ID.
            self.sink.stop()
            try:
                with patch.object(trace.telemetry, "default_emitter", return_value=emitter), \
                     trace.scope("offline_proof", sync_run_id=99):
                    repo = Repository()
                    self.collector(repo).process_common_claims(repo.claim()["rows"])
                    repo = AtomicRepository()
                    collector = service(repo, lambda ids, reason: [video(value) for value in ids])
                    initial.run_initial_video_window(collector,
                        job_id="00000000-0000-4000-8000-000000000001",
                        channel_id="UC" + "c" * 22, video_ids=["abcdefghijk"],
                        worker_id="worker", run_id="private-lease-token")
                self.assertTrue(emitter.flush(2))
                metrics = emitter.metrics()
                self.assertEqual([metrics[key] for key in ("invalid", "dropped", "sink_errors")], [0, 0, 0])
                events = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(sum(e["operation"] == "video_write" and e["status"] == "confirmed"
                                     for e in events), 2)
            finally:
                emitter.close(2)
                self.sink.start()

    def test_cancelled_initial_unit_records_no_provider_or_committed_data(self):
        repo = AtomicRepository()
        fetch = Mock()
        collector = service(repo, fetch)
        unit = initial.InitialBatchUnit(collector, key="initial", job_id="job",
            channel_id="UC" + "c" * 22, video_ids=["abcdefghijk"], worker_id="worker",
            run_id="private-lease-token", require_ready=lambda: None, on_settled=lambda summary: None)
        unit.prepare()
        with patch.object(initial, "_drain_unsent", return_value={"remaining": 0}):
            unit.settle(Completion("cancelled"))
        fetch.assert_not_called()
        self.assertEqual(self.writes("confirmed"), [])
        self.assertNotIn("private-lease-token", repr(self.events))
        self.assertTrue(any(e["stage"] == "video_batch" and e["status"] == "cancelled" for e in self.events))


if __name__ == "__main__":
    unittest.main()
