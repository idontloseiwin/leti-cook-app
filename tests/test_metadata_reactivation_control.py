"""Canonical metadata reactivation boundaries; no database or provider calls."""
from contextlib import ExitStack
from datetime import datetime, timezone
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection import channel_metadata as metadata
from db.channel.channel_sync import ChannelSyncRepository

CHANNEL = "UC" + "a" * 22
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

def channel(mode="metadata_only", legacy="skipped_no_long_playlist"):
    return dict(channel_id=CHANNEL, collection_control_epoch=7,
                collection_permission="accepted", collection_mode=mode,
                initial_long_inventory_status=legacy)

class MetadataReactivationControlTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")).start()
        patch("sys.stdout", io.StringIO()).start()

    def test_explicit_recheck_action_keeps_the_original_epoch(self):
        context = SimpleNamespace(database=object())
        for status, action in [("long_playlist_available", "no_long_restart"),
                               ("long_playlist_manual_required", "no_long_manual_required")]:
            with self.subTest(status=status):
                repository = Mock()
                repository.update_metadata_batch.return_value = {CHANNEL: "updated"}
                prepared = dict(channel=channel(), values={"video_count": 8}, recheck_summary_status=status)
                with patch.object(metadata, "ChannelSyncRepository", return_value=repository):
                    result = metadata._persist_channel_metadata_batch([prepared], NOW, context=context)
                self.assertEqual(result, [None])
                self.assertEqual(repository.update_metadata_batch.call_args.args[0],
                    [{"channel_id": CHANNEL, "control_epoch": 7, "values": {"video_count": 8}, "control_action": action}])

    def test_legacy_values_never_create_control_authority(self):
        repository = Mock()
        repository.update_metadata_batch.return_value = {CHANNEL: "updated"}
        for legacy in ("manual_required", "pending", "completed"):
            prepared = dict(channel=channel(), values={"initial_long_inventory_status": legacy})
            with patch.object(metadata, "ChannelSyncRepository", return_value=repository):
                metadata._persist_channel_metadata_batch([prepared], NOW, context=SimpleNamespace(database=object()))
            self.assertNotIn("control_action", repository.update_metadata_batch.call_args.args[0][0])

    def test_invalid_action_stops_before_database(self):
        database = Mock()
        repository = ChannelSyncRepository(database)
        for bad in (None, "accepted", "", True):
            with self.subTest(action=bad), self.assertRaises(ValueError):
                repository.update_metadata_batch([{"channel_id": CHANNEL, "control_epoch": 7,
                    "values": {"title": "test"}, "control_action": bad}])
        self.assertEqual(database.mock_calls, [])

    def test_verified_active_channel_does_not_reenter_legacy_initialization(self):
        with patch.object(metadata, "get_long_video_count") as provider:
            for legacy in ("skipped_no_long_playlist", "pending", "error"):
                row = channel("active", legacy)
                self.assertTrue(metadata.has_completed_initial_long_inventory(row))
                self.assertIsNone(metadata.recheck_no_long_playlist_after_video_count_change(
                    row, {}, context=SimpleNamespace()))
            provider.assert_not_called()

    def test_old_completed_marker_cannot_grant_discovery(self):
        for mode in ("initial_inventory", "metadata_only", "operator_hold", ""):
            self.assertFalse(metadata.has_completed_initial_long_inventory(channel(mode, "completed")))

    def test_manual_hold_is_not_dispatched_as_followup_work(self):
        row = channel()
        context = SimpleNamespace(database=object(), enqueue_initial_inventory=Mock(),
            settings=SimpleNamespace(typed_playlist_inventory_enabled=True),
            sources=SimpleNamespace(fetch_channel_metadata_batch=Mock(return_value=[{"id": CHANNEL}]),
                                    is_youtube_daily_quota_exhausted=lambda error: False))
        prepared = dict(channel=row, values={}, video_count_changed=True, initial_inventory_completed=False,
            change_direction="increased", discovery_prioritized=False,
            recheck_summary_status="long_playlist_manual_required", recheck_error_message=None,
            run_initial_inventory=False)
        def prepare_steps(*args, **kwargs):
            yield from ()
            return prepared
        with ExitStack() as stack:
            stack.enter_context(patch.object(metadata, "load_due_channel_metadata_channels", return_value=[row]))
            stack.enter_context(patch.object(metadata, "ChannelSyncRepository", return_value=Mock(spec=ChannelSyncRepository)))
            stack.enter_context(patch.object(metadata, "channel_metadata_update_steps", side_effect=prepare_steps))
            stack.enter_context(patch.object(metadata, "_persist_channel_metadata_batch", return_value=[None]))
            stack.enter_context(patch.object(metadata, "stop_at_boundary", return_value=False))
            initial = stack.enter_context(patch.object(context, "enqueue_initial_inventory"))
            typed = stack.enter_context(patch.object(metadata, "TypedInventoryRepository"))
            result = metadata.run_channel_metadata_sync_batch(context=context)
        self.assertEqual((result["updated"], result["manual_required"], result["video_count_changed"]), (1, 1, 1))
        self.assertEqual(result["errors"], 0)
        for key in ("video_count_changed_channel_ids", "video_count_changed_channel_scopes",
                    "video_count_increased_channel_ids", "video_count_decreased_channel_ids",
                    "video_count_decreased_channel_scopes"):
            self.assertEqual(result[key], [], key)
        initial.assert_not_called()
        typed.assert_not_called()

if __name__ == "__main__":
    unittest.main()
