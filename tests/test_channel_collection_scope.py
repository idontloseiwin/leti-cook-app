"""Offline phase 2 channel authority and captured scope checks."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from channel_collection_scope import channel_scope, require_control_epoch
from channel_sync_policy import channel_sync_eligibility
CHANNEL = "UC" + "a" * 22
class ChannelCollectionScopeTests(unittest.TestCase):
    def row(self, **changes):
        return {"channel_id": CHANNEL, "is_hidden": False, "collection_permission": "accepted",
                "collection_control_epoch": 12, "collection_mode": "active", **changes}
    def test_epoch_strict_positive_integer(self):
        for value in [None, True, False, "12", 1.0, 0, -1, 9223372036854775808]:
            with self.subTest(value=value), self.assertRaises(ValueError): require_control_epoch(value)
        self.assertEqual(require_control_epoch(9223372036854775807), 9223372036854775807)
    def test_scope_is_a_copy_of_original_generation(self):
        row = self.row(); captured = channel_scope(row); row["collection_control_epoch"] = 13
        self.assertEqual(captured, {"channel_id": CHANNEL, "control_epoch": 12})
    def test_bad_identity_rejected(self):
        for identity in [None, "", " spaced ", 12, "a" * 129]:
            with self.subTest(identity=identity), self.assertRaises(ValueError): channel_scope(self.row(channel_id=identity))
    def test_legacy_status_cannot_grant_permission(self):
        for status in ["completed", "pending", "running", "skipped_no_long_playlist"]:
            policy = channel_sync_eligibility({"channel_id": CHANNEL, "is_hidden": False, "initial_long_inventory_status": status})
            self.assertEqual(policy.mode, "operator_hold")
            self.assertFalse(policy.allow_channel_metadata or policy.allow_video_collection or policy.allow_initial_inventory)
    def test_error_progress_does_not_revoke_owner_permission(self):
        policy = channel_sync_eligibility(self.row(initial_long_inventory_status="error"))
        self.assertEqual(policy.mode, "active"); self.assertTrue(policy.allow_video_collection)
    def test_incomplete_linked_job_wins_over_old_completed_long(self):
        policy = channel_sync_eligibility(self.row(collection_mode="initial_inventory", initial_long_inventory_status="completed"))
        self.assertTrue(policy.allow_initial_inventory); self.assertFalse(policy.allow_discovery or policy.allow_video_collection)
    def test_manual_hold_wins_over_active_projection(self):
        policy = channel_sync_eligibility(self.row(collection_permission="manual_required"))
        self.assertEqual(policy.reason, "inventory_manual_required"); self.assertFalse(policy.allow_initial_inventory or policy.allow_video_collection)
    def test_missing_epoch_fails_closed(self):
        self.assertEqual(channel_sync_eligibility(self.row(collection_control_epoch=None)).mode, "operator_hold")
    def test_hidden_wins_over_all_progress(self):
        policy = channel_sync_eligibility(self.row(is_hidden=True)); self.assertEqual(policy.mode, "hidden")
        self.assertFalse(policy.allow_channel_metadata or policy.allow_initial_inventory)
    def test_no_long_keeps_transitional_metadata_only(self):
        policy = channel_sync_eligibility(self.row(collection_mode="metadata_only"))
        self.assertTrue(policy.allow_channel_metadata and policy.allow_typed_inventory); self.assertFalse(policy.allow_video_collection)
if __name__ == "__main__": unittest.main()
