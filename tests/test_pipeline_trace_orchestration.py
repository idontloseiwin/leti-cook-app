"""Passive correlation on actual orchestration paths; no DB or provider I/O."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sys
import os
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
import pipeline_trace
from telemetry_contract import build_event
from collection.dispatch import Ack, Coordinator, DispatchBlocked, Prepared, Work
from collection.inventory_steps import InventoryPageStep
import tests.test_shared_inventory_cycle as cycle_fixture
import tests.test_channel_metadata_steps as metadata_fixture
import tests.test_collector_handoff as supervisor_fixture
from ops import self_hosted_collect as child


class PipelineOrchestrationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"})
        environment.start()
        self.addCleanup(environment.stop)
        self.events = []
        self.rejected = []
        self.addCleanup(lambda: self.assertEqual(self.rejected, []))
        def record(kind, source, target, stage, **fields):
            if kind == "pipeline":
                try:
                    build_event(kind, source, target, stage, **fields)
                except Exception as error:
                    self.rejected.append((stage, str(error)))
                    raise
                self.events.append((stage, fields))
            return fields
        self.record = patch("telemetry.emit_event", side_effect=record)
        self.record.start()
        self.addCleanup(self.record.stop)

    def test_parallel_work_keeps_its_offer_trace_across_later_phase_and_all_callbacks(self):
        barrier = threading.Barrier(2)
        seen = {}
        expected = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner = Coordinator(pool, 2, clock=lambda: 1)
            for index in range(2):
                key = "batch" + str(index)
                def prepare(key=key):
                    seen[(key, "prepare")] = dict(pipeline_trace.capture())
                    def fetch(cancel):
                        seen[(key, "fetch")] = dict(pipeline_trace.capture())
                        barrier.wait(timeout=2)
                        return key
                    return Prepared(fetch, 100, 1)
                def settle(result, key=key):
                    seen[(key, "settle")] = dict(pipeline_trace.capture())
                    self.assertEqual(result.value, key)
                    return Ack()
                with pipeline_trace.scope("phase", sync_run_id=index + 1) as phase:
                    expected[key] = dict(pipeline_trace.capture())
                    owner.offer(Work(key, "auto", "channel", "long", prepare, settle))
                    phase.finish("offered")
            with pipeline_trace.scope("unrelated_phase", sync_run_id=900) as later:
                self.assertEqual(owner.step().submitted, 2)
                for held in owner._held.values():
                    held.future.result(timeout=3)
                self.assertEqual(len(owner.step().acknowledged), 2)
                later.finish("completed")
            self.assertEqual(pool.submit(pipeline_trace.capture).result(), {})
        for (key, stage), identity in seen.items():
            self.assertEqual(identity["trace_id"], expected[key]["trace_id"], stage)
            self.assertEqual(identity["sync_run_id"], expected[key]["sync_run_id"], stage)
            self.assertEqual(identity["operation_id"], key, stage)
        self.assertEqual(len(seen), 6)
        self.assertEqual(pipeline_trace.capture(), {})

    def test_uncertain_settlement_is_not_acknowledged_or_retried(self):
        calls = []
        def settle(result):
            calls.append(result.phase)
            raise RuntimeError("secret-payload-must-not-be-recorded")
        with ThreadPoolExecutor(max_workers=1) as pool:
            owner = Coordinator(pool, 1, clock=lambda: 1)
            with pipeline_trace.scope("sync", sync_run_id=7) as span:
                owner.offer(Work("uncertain", "auto", "channel", "short",
                    lambda: Prepared(lambda cancel: "payload-not-for-journal", 100), settle))
                owner.step()
                owner._held["uncertain"].future.result(timeout=2)
                with self.assertRaises(DispatchBlocked):
                    owner.step()
                with self.assertRaises(DispatchBlocked):
                    owner.step()
                span.finish("unknown")
            self.assertEqual(calls, ["response"])
            self.assertEqual(owner.snapshot().held, 1)
        self.assertTrue(any(stage == "dispatch_blocked" and fields["status"] == "unknown"
                            for stage, fields in self.events))
        self.assertFalse(any(stage == "dispatch_settle" and fields["status"] == "acknowledged"
                             for stage, fields in self.events))
        self.assertNotIn("secret-payload", repr(self.events))
        self.assertNotIn("payload-not-for-journal", repr(self.events))

    def test_inventory_page_carries_prepare_context_and_never_logs_token_or_payload(self):
        observed = []
        at = datetime(2026, 10, 15, tzinfo=timezone.utc)
        def provider(playlist_id, **options):
            observed.append((playlist_id, options, dict(pipeline_trace.capture())))
            return {"items": [], "nextPageToken": "secret-response-token"}
        with pipeline_trace.scope("inventory_prepare", operation_id="transition-1", sync_run_id=11) as span:
            expected = dict(pipeline_trace.capture())
            step = InventoryPageStep(provider, "playlist", "secret-page-token", "typed_inventory_short",
                                     lambda: at, at, pipeline_trace.capture())
            span.finish("prepared")
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(step.execute).result(timeout=2)
        self.assertEqual(observed[0][2]["trace_id"], expected["trace_id"])
        self.assertEqual(observed[0][2]["operation_id"], "transition-1")
        self.assertEqual(len(observed), 1)
        self.assertEqual(result.value["nextPageToken"], "secret-response-token")
        self.assertNotIn("secret-page-token", repr(self.events))
        self.assertNotIn("secret-response-token", repr(self.events))

    def test_real_cycle_has_one_trace_and_reports_terminal_outcome(self):
        fixture = cycle_fixture.SharedCycleTests()
        result, *_ = fixture.run_cycle(lambda **options: {"errors": 0})
        starts = [fields for stage, fields in self.events if stage == "auto_sync" and fields["status"] == "started"]
        ends = [fields for stage, fields in self.events if stage == "auto_sync" and fields["status"] != "started"]
        phases = [fields for stage, fields in self.events if stage == "cycle_phase"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["status"], "completed")
        self.assertTrue(phases)
        self.assertEqual({fields["trace_id"] for fields in phases}, {starts[0]["trace_id"]})
        self.assertEqual(result["errors"], 0)

    def test_supervisor_and_child_share_launch_identity_and_record_abnormal_exit(self):
        fixture = supervisor_fixture.SupervisorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        environment = fixture.starts[0][1]["env"]
        launches = [fields for stage, fields in self.events if stage == "worker_launch" and fields["status"] == "spawned"]
        self.assertEqual(len(launches), 1)
        with patch.dict(os.environ, environment), patch.object(sys, "argv", ["collect.py", "auto"]), patch.object(child, "_main", return_value=0):
            self.assertEqual(child.main(), 0)
        entered = next(fields for stage, fields in self.events if stage == "worker_child" and fields["status"] == "started")
        self.assertEqual(entered["trace_id"], launches[0]["trace_id"])
        self.assertEqual(entered["parent_span_id"], launches[0]["span_id"])
        fixture.process.code = -9
        fixture.value["settings"]["automatic_collection"] = False
        fixture.supervisor.tick()
        fixture.supervisor.tick()
        exits = [fields for stage, fields in self.events if stage == "worker_exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["status"], "failed")
        self.assertEqual(exits[0]["reason"], "signal_9")
        self.assertEqual(exits[0]["trace_id"], entered["trace_id"])
        gated = [fields for stage, fields in self.events if stage == "worker_admission" and fields.get("reason") == "paused"]
        self.assertEqual(len(gated), 2)  # One started and one deferred, not every supervisor tick.

    def test_invalid_trace_environment_never_blocks_child_or_enters_journal(self):
        with patch.dict(os.environ, {"LIC_PIPELINE_TRACE_ID": "private-secret-value", "LIC_PIPELINE_PARENT_SPAN_ID": "invalid"}), patch.object(sys, "argv", ["collect.py", "auto"]), patch.object(child, "_main", return_value=0):
            self.assertEqual(child.main(), 0)
        self.assertNotIn("private-secret-value", repr(self.events))
        self.assertTrue(any(stage == "worker_child" and fields["status"] == "completed" for stage, fields in self.events))

    def test_count_decrease_is_a_confirmed_notice_without_extra_provider_work(self):
        fixture = metadata_fixture.MetadataProviderStepsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result = fixture.drive(fixture.steps([{**metadata_fixture.row(), "video_count": 2}]))
        notices = [fields for stage, fields in self.events if stage == "channel_count_change"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["reason"], "decrease_notice_only")
        self.assertEqual(notices[0]["status"], "confirmed")
        self.assertEqual(result["video_count_decreased"], 1)
        self.assertEqual([item for item in fixture.events if isinstance(item, tuple)],
                         [("fetch", (metadata_fixture.CHANNEL,))])


if __name__ == "__main__":
    unittest.main()
