"""Automatic composition: inventory does not force availability requests."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import io
import os
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection import availability as availability_owner
from collection import cycle
from collection.runs import sync_run_counts_from_summary
from sync_run_summary import compact_sync_run_summary

CHANNEL = "UC" + "a" * 22
OTHER = "UC" + "b" * 22
SCOPE = {"channel_id": CHANNEL, "control_epoch": 7}

class SharedCycleTests(unittest.TestCase):
    def run_cycle(self, inventory, *, metadata=None, new_limit=50):
        context = SimpleNamespace(database=object(),
            video=SimpleNamespace(run_batch=Mock(return_value={"errors": 0, "video_ids": []})),
            settings=SimpleNamespace(database_video_collection_enabled=True),
            policy=SimpleNamespace(short_video_collection_enabled=False, completed_stream_collection_enabled=False))
        repository = Mock()
        repository.enqueue_video_ids.side_effect = lambda ids, **kw: len(ids)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"}))
            for name in ("validate_required_auto_sync_rpcs", "recover_stale_running_statuses", "run_video_stats_sync_batch"):
                stack.enter_context(patch.object(cycle, name, return_value={"errors": 0}))
            stack.enter_context(patch.object(cycle, "run_channel_metadata_sync_batch", return_value=metadata or {"errors": 0}))
            run = stack.enter_context(patch.object(cycle, "run_typed_playlist_inventory_batch", side_effect=inventory))
            loss = stack.enter_context(patch.object(availability_owner, "run_channel_long_availability_loss_batch", return_value={"errors": 0}))
            regain = stack.enter_context(patch.object(availability_owner, "run_channel_availability_recheck", return_value={"errors": 0}))
            stack.enter_context(patch.object(cycle, "VideoCollectionRepository", return_value=repository))
            stack.enter_context(patch.object(cycle, "stop_at_boundary", return_value=False))
            stack.enter_context(patch.object(cycle, "update_sync_run_progress"))
            stack.enter_context(patch.object(cycle, "emit_phase"))
            output = stack.enter_context(patch("sys.stdout", new=io.StringIO()))
            result = cycle.run_auto_sync_cycle(context=context, inventory_page_budget=12, new_video_stats_limit=new_limit)
            self.last_output = output.getvalue()
        return result, run, loss, regain, repository

    def test_only_one_pass_owns_long_and_other_inventory(self):
        def inventory(**kw):
            kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE, video_type="long",
                status="completed", coverage="complete", inserted=1, new_video_ids=["video-id"],
                scan_kind="full_reconciliation", items_seen=1, full_generation_new_count=1,
                pending_channel_count_change=True))
            return dict(errors=0, inserted=1, provider_attempts=1)
        result, run, loss, regain, repository = self.run_cycle(inventory)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["limit"], 12)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["channel_discovery"]["new_videos_found"], 1)
        self.assertEqual(result["new_video_stats"]["enqueued"], 0)
        self.assertEqual(result["new_video_stats"]["routing"]["mode"], "inventory_owned_no_followup")
        self.assertEqual(result["new_video_ids"], ["video-id"])
        repository.enqueue_video_ids.assert_not_called()
        loss.assert_not_called()
        regain.assert_not_called()
        self.assertNotIn("run_due_channel_discovery_batch", vars(cycle))

    def test_unknown_ack_stops_before_follow_up_writes(self):
        result, run, loss, regain, repository = self.run_cycle(lambda **kw:
            dict(errors=1, settlement_unknown=True, provider_attempts=1))
        self.assertTrue(result["settlement_unknown"])
        loss.assert_not_called(); regain.assert_not_called(); repository.enqueue_video_ids.assert_not_called()

    def test_committed_inventory_counts_each_type_once_even_when_partial_or_priority_limited(self):
        for status in ("running", "completed"):
            for new_limit in (0, 50):
                with self.subTest(status=status, new_limit=new_limit):
                    def inventory(**kw):
                        for kind in ("long", "short", "stream"):
                            kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE,
                                video_type=kind, status=status, inserted=1,
                                new_video_ids=[kind + "-video"], full_generation_new_count=1))
                        return dict(errors=0, inserted=3, provider_attempts=3,
                            by_type={kind: {"inserted": 1} for kind in ("long", "short", "stream")})
                    result, _, _, _, repository = self.run_cycle(inventory, new_limit=new_limit)
                    counts = sync_run_counts_from_summary(result)
                    self.assertEqual(counts["new_videos"], 3)
                    self.assertEqual(len(result["new_video_ids"]), 3 if new_limit else 0)
                    self.assertEqual(result["new_video_ids_omitted"], 0 if new_limit else 3)
                    self.assertEqual(result["new_video_stats_deferred"], 0)
                    self.assertEqual(result["new_video_stats_video_ids"], [])
                    self.assertEqual(counts["stats_updated"], 0)
                    repository.enqueue_video_ids.assert_not_called()
                    completion_line = next(line for line in self.last_output.splitlines()
                        if line.startswith("[auto-sync] done "))
                    self.assertIn(" new_videos=3 ", completion_line)

    def test_legacy_separate_inventory_summary_keeps_its_distinct_counts(self):
        counts = sync_run_counts_from_summary({
            "channel_discovery": {"new_videos_found": 1},
            "new_video_ids": ["long-video"], "typed_inventory": {"inserted": 2},
        })
        self.assertEqual(counts["new_videos"], 3)

    def test_metadata_decrease_does_not_request_recent_long_videos(self):
        result, run, loss, regain, repository = self.run_cycle(lambda **kw: {"errors": 0},
            metadata={"errors": 0, "video_count_decreased_channel_ids": [CHANNEL],
                      "video_count_decreased_channel_scopes": [SCOPE]})
        self.assertEqual(result["errors"], 0)
        loss.assert_not_called()
        regain.assert_not_called()
        self.assertEqual(result["long_availability_loss"]["triggered"], 0)

    def test_diagnostic_sample_is_bounded_without_followup_writes_or_deferred_work(self):
        scope_b = {"channel_id": OTHER, "control_epoch": 19}
        def inventory(**kw):
            for scope, ids in ((SCOPE, ["first" + str(i) for i in range(1000)]),
                               (scope_b, ["last", "deferred"])):
                kw["on_page"](dict(channel_id=scope["channel_id"], channel_scope=scope,
                    video_type="long", status="running", new_video_ids=ids))
            return {"errors": 0}
        result, run, loss, regain, repository = self.run_cycle(inventory, new_limit=1001)
        repository.enqueue_video_ids.assert_not_called()
        self.assertEqual(len(result["new_video_ids"]), 1001)
        self.assertEqual(result["new_video_ids_omitted"], 1)
        self.assertEqual(result["new_video_stats_deferred"], 0)
        regain.assert_not_called()

    def test_short_or_stream_only_discovery_has_same_observation_without_long(self):
        for kind in ("short", "stream"):
            with self.subTest(kind=kind):
                def inventory(**kw):
                    kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE, video_type="long",
                        status="playlist_not_found", new_video_ids=[]))
                    kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE, video_type=kind,
                        status="completed", inserted=1, queue_inserted=1,
                        new_video_ids=[kind + "-video"], full_generation_new_count=1))
                    return dict(errors=0, inserted=1, provider_attempts=2)
                result, run, loss, regain, repository = self.run_cycle(inventory)
                self.assertEqual(result["new_video_ids"], [kind + "-video"])
                self.assertEqual(sync_run_counts_from_summary(result)["new_videos"], 1)
                self.assertEqual(result["channel_discovery"]["new_videos_found"], 0)
                self.assertEqual(result["new_video_stats"]["requested"], 0)
                run.assert_called_once()
                repository.enqueue_video_ids.assert_not_called()
                loss.assert_not_called(); regain.assert_not_called()

    def test_durable_summary_keeps_zero_work_without_promising_diagnostic_samples(self):
        def inventory(**kw):
            kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE, video_type="short",
                status="completed", inserted=1, new_video_ids=["short-video"], full_generation_new_count=1))
            return dict(errors=0, inserted=1, provider_attempts=1)
        result, *_ = self.run_cycle(inventory, new_limit=0)
        compact = compact_sync_run_summary(result)
        self.assertEqual(compact["new_video_stats"]["requested"], 0)
        self.assertEqual(compact["new_video_stats"]["updated"], 0)
        self.assertEqual(compact["new_video_ids_count"], 0)
        self.assertEqual(compact["typed_inventory"]["inserted"], 1)
        self.assertNotIn("new_video_ids_omitted", compact)
        self.assertNotIn("routing", compact["new_video_stats"])

    def test_discovery_new_pages_do_not_trigger_no_new_recheck_on_terminal_page(self):
        for resume_between_passes in (False, True):
            with self.subTest(resume_between_passes=resume_between_passes):
                def inventory(**kw):
                    if not resume_between_passes:
                        kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE, video_type="long",
                            status="running", inserted=50, new_video_ids=[f"new{index:08d}" for index in range(50)],
                            scan_kind="discovery", full_generation_new_count=50, latest_uulf_known=False))
                    kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE, video_type="long",
                        status="completed", coverage="partial", inserted=0, new_video_ids=[],
                        scan_kind="discovery", items_seen=50, full_generation_new_count=50,
                        latest_uulf_known=False, pending_channel_count_change=True))
                    return {"errors": 0}
                with patch.object(availability_owner, "load_availability_recheck_candidate_ids", return_value=["miss0000001"]) as candidates:
                    result, run, loss, regain, repository = self.run_cycle(inventory,
                        metadata={"errors": 0, "video_count_increased_channel_ids": [CHANNEL]})
                self.assertEqual(result["errors"], 0)
                self.assertEqual(result["channel_discovery"]["no_new_videos"], 0)
                regain.assert_not_called()
                candidates.assert_not_called()

    def test_known_head_count_increase_does_not_override_unavailable_schedule(self):
        def inventory(**kw):
            kw["on_page"](dict(channel_id=CHANNEL, channel_scope=SCOPE, video_type="long",
                status="completed", coverage="partial", inserted=0, new_video_ids=[],
                scan_kind="discovery", items_seen=0, full_generation_new_count=0,
                latest_uulf_known=True, pending_channel_count_change=True))
            return {"errors": 0}
        with patch.object(availability_owner, "load_availability_recheck_candidate_ids", return_value=["miss0000001"]) as candidates:
            result, run, loss, regain, repository = self.run_cycle(inventory,
                metadata={"errors": 0, "video_count_increased_channel_ids": [CHANNEL]})
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["channel_discovery"]["no_new_videos"], 1)
        self.assertEqual(result["channel_discovery"]["availability_recheck_triggered"], 0)
        candidates.assert_not_called()
        regain.assert_not_called()

    def test_receipt_pending_signal_does_not_read_retired_discovery_clock(self):
        with patch.object(availability_owner, "channel_video_count_changed_since_last_discovery") as old:
            for signal in (False, True):
                result = availability_owner.should_trigger_availability_recheck(CHANNEL,
                    dict(status="no_new_videos", scan_kind="discovery", latest_uulf_known=True,
                         pending_channel_count_change=signal), context=SimpleNamespace())
                self.assertIs(result, signal)
            old.assert_not_called()

if __name__ == "__main__": unittest.main()
