"""Moved availability scenarios keep their scope and trigger policy; no I/O."""
import ast
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection import availability as owner, channel_metadata, cycle

CHANNEL = "UC" + "a" * 22

class AvailabilityOwnerTests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock(return_value={"claimed": 1, "updated": 1, "video_ids": ["v0000000001"], "errors": 0})
        self.context = SimpleNamespace(database=object(), video=SimpleNamespace(run_batch=self.runner))
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")).start()
        patch.object(owner, "source_log_message", return_value="").start()

    def test_new_owner_has_no_backward_bootstrap_or_legacy_import(self):
        tree = ast.parse(Path(owner.__file__).read_text())
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse(set(imports) & {"core", "server", "sync_service", "collection._previous_long"})
        cycle_tree = ast.parse(Path(cycle.__file__).read_text())
        cycle_imports = [n.module for n in ast.walk(cycle_tree) if isinstance(n, ast.ImportFrom)]
        self.assertNotIn("collection.availability", cycle_imports)
        # Ordinary General/Stats own missing detection. The auto cycle must not
        # reconnect the old count-loss or discovery-triggered availability loops.
        called = {node.func.id for node in ast.walk(cycle_tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertTrue(called.isdisjoint({"run_channel_availability_recheck",
                                          "run_channel_long_availability_loss_batch"}))

    def test_disabled_count_signal_still_allows_only_stale_recheck(self):
        discovery = {"status": "no_new_videos", "latest_uulf_known": True}
        with patch.object(owner, "load_availability_recheck_candidate_ids", return_value=["v0000000001"]) as candidates:
            result = owner.run_channel_availability_recheck(CHANNEL, discovery, False, context=self.context, expected_epoch=7)
        self.assertEqual(result["trigger"], "stale_unavailable")
        self.assertEqual(candidates.call_args.kwargs["recheck_after"], timedelta(days=1))
        self.assertEqual(self.runner.call_args.kwargs["channel_scopes"], [{"channel_id": CHANNEL, "control_epoch": 7}])
        self.assertTrue(self.runner.call_args.kwargs["force_metadata"])

    def test_full_generation_trigger_uses_cumulative_completion_not_last_page(self):
        discovery = dict(scan_kind="full_reconciliation", scan_complete=True,
            pending_channel_count_change=True, full_generation_new_count=1, full_generation_items_seen=51)
        with patch.object(owner, "channel_video_count_changed_since_last_discovery", side_effect=AssertionError("no old timing read")):
            self.assertFalse(owner.should_trigger_availability_recheck(CHANNEL, discovery, context=self.context))
            self.assertTrue(owner.should_trigger_availability_recheck(CHANNEL, {**discovery, "full_generation_new_count": 0}, context=self.context))
            self.assertFalse(owner.should_trigger_availability_recheck(CHANNEL, {**discovery, "full_generation_new_count": 0, "scan_complete": False}, context=self.context))

    def test_loss_check_does_not_read_followup_status_after_unknown_ack(self):
        self.runner.return_value = {"claimed": 1, "errors": 1, "settlement_unknown": True,
            "infrastructure_error": True, "stopped_reason": "settlement_failed"}
        with patch.object(owner, "load_initial_long_inventory_status", return_value="completed"), \
             patch.object(owner, "load_long_availability_loss_candidate_ids", return_value=["v0000000001"]), \
             patch.object(owner, "load_video_availability_status_counts") as counts:
            result = owner.run_channel_long_availability_loss_check(CHANNEL, context=self.context, expected_epoch=7)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["settlement_unknown"])
        counts.assert_not_called()
        self.assertEqual(self.runner.call_args.kwargs["channel_scopes"], [{"channel_id": CHANNEL, "control_epoch": 7}])

    def test_scope_is_validated_before_selection_or_dispatch(self):
        with patch.object(owner, "AvailabilityRepository") as repository:
            with self.assertRaises(ValueError):
                owner.run_channel_availability_recheck(CHANNEL, {}, context=self.context, expected_epoch=None)
            with self.assertRaises(ValueError):
                owner.run_channel_long_availability_loss_batch([CHANNEL], context=self.context, channel_scopes=[])
        repository.assert_not_called()
        self.runner.assert_not_called()

    def test_existing_canonical_readiness_predicate_remains_equivalent(self):
        for mode in ("active", "operator_hold", "initial_inventory", "metadata_only"):
            row = dict(channel_id=CHANNEL, is_hidden=False, collection_mode=mode,
                       collection_control_epoch=7, collection_permission="accepted", initial_long_inventory_status="pending")
            expected = "completed" if channel_metadata.has_completed_initial_long_inventory(row) else "pending"
            with patch.object(owner, "ChannelDiscoveryRepository") as repository:
                repository.return_value.load_initial_inventory_status.return_value = row
                self.assertEqual(owner.load_initial_long_inventory_status(CHANNEL, context=self.context), expected)

if __name__ == "__main__": unittest.main()
