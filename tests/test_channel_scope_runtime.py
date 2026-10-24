"""Focused offline checks of original channel scope across callbacks and recovery."""
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection import channel_metadata, initial
from db.channel.channel_sync import ChannelSyncRepository
from db.base.postgres import DatabaseUnavailable
from db.inventory.typed_inventory import TypedInventoryClaim
from collection import inventory as inventory_service

CHANNEL = "UC" + "a" * 22
NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)

class ChannelScopeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")).start()
        patch("sys.stdout", io.StringIO()).start()
        patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"}).start()

    def test_metadata_batch_rejects_missing_epoch_before_dispatch(self):
        database = Mock(spec_set=['call', 'read_rows', 'read_row', 'write', 'write_rows'])
        repository = ChannelSyncRepository(database)
        with self.assertRaises(ValueError):
            repository.update_metadata_batch([{"channel_id": CHANNEL, "values": {"title": "x"}}])
        database.call.assert_not_called()

    def test_metadata_mark_keeps_original_epoch_in_single_batch(self):
        database = Mock(spec_set=['call', 'read_rows', 'read_row', 'write', 'write_rows'])
        with patch('db.channel.channel_sync.channel_metadata_batch.update_channel_metadata_batch',
                   return_value=[{"channel_id": CHANNEL, "status": "updated"}]) as settle:
            ChannelSyncRepository(database).mark_metadata_running(
                [{"channel_id": CHANNEL, "collection_control_epoch": 7}], NOW.isoformat())
        settle.assert_called_once_with(database,[{"channel_id": CHANNEL,"control_epoch":7,
            "values":{"channel_metadata_status":"running","channel_metadata_started_at":NOW.isoformat()}}])
        database.call.assert_not_called()
        database.read_rows.assert_not_called()
        database.read_row.assert_not_called()
        database.write.assert_not_called()
        database.write_rows.assert_not_called()

    def test_unknown_ack_recovery_cannot_use_restored_epoch(self):
        original = {"channel_id": CHANNEL, "collection_control_epoch": 7}
        current = {**original, "collection_control_epoch": 9, "channel_metadata_status": "ok",
                   "channel_metadata_checked_at": NOW.isoformat(), "is_hidden": False}
        repository = Mock(); repository.load_channel_record.return_value = current
        @contextmanager
        def fresh(): yield repository
        context = SimpleNamespace(fresh_channel_repository=fresh, sources=SimpleNamespace(parse_dt=datetime.fromisoformat))
        with patch.object(channel_metadata, "mark_channel_metadata_error") as mark:
            committed = channel_metadata._reconcile_or_mark_channel_metadata_persistence_error(
                original, NOW, DatabaseUnavailable('database_connection_failed', outcome_unknown=True), context=context)
        self.assertFalse(committed)
        self.assertIs(mark.call_args.args[0], original)

    def test_initial_inventory_invalid_epoch_prevents_provider_request(self):
        fetch = Mock()
        with patch.object(initial, "run_typed_inventory_batch") as runner, self.assertRaises(ValueError):
            initial.collect_initial_inventory_chunk(CHANNEL, expected_epoch=True, worker_id="test-worker",
                database=object(), settings=SimpleNamespace(typed_playlist_inventory_enabled=True),
                fetch_page=fetch, get_longform_playlist_id=lambda value: "UULF"+value[2:],
                parse_dt=datetime.fromisoformat, playlist_not_found_error=LookupError,
                daily_quota_exceeded_error=RuntimeError)
        fetch.assert_not_called()
        runner.assert_not_called()

    def test_initial_typed_callback_passes_original_parent_epoch(self):
        fetch = Mock()
        with patch.object(initial, "run_typed_inventory_batch", return_value={"results": []}) as runner:
            initial.collect_initial_inventory_chunk(CHANNEL, video_type="short", expected_epoch=7, worker_id="test-worker",
                database=object(), settings=SimpleNamespace(typed_playlist_inventory_enabled=True),
                fetch_page=fetch, get_longform_playlist_id=lambda value: "UULF"+value[2:],
                parse_dt=datetime.fromisoformat, playlist_not_found_error=LookupError,
                daily_quota_exceeded_error=RuntimeError)
        self.assertEqual(runner.call_args.kwargs["expected_channel_scopes"], [{"channel_id": CHANNEL, "control_epoch": 7}])
        fetch.assert_not_called()

    def test_typed_page_renewal_keeps_first_claim_epoch(self):
        common = dict(channel_id=CHANNEL, video_type="short", playlist_id="UUSH"+CHANNEL[2:],
                      lease_channel_epoch=7, scan_kind="initial_inventory")
        first = SimpleNamespace(**common, lease_token="first", active_scan_id=None, resume_page_token=None)
        second = SimpleNamespace(**common, lease_token="second", active_scan_id="scan", resume_page_token="next")
        repository = Mock(); repository.claim_pass.side_effect=[[first], [second]]
        results = [{"status": "running", "scan_id": "scan", "resume_page_token": "next", "transition_id": "page-ack"},
                   {"status": "completed", "scan_id": "scan", "resume_page_token": None}]
        with patch.object(inventory_service, "run_typed_inventory_claim", side_effect=results):
            outcome = inventory_service.run_typed_inventory_batch(worker_id="test-worker", repository=repository,
                fetch_page=Mock(), limit=2, prepare=False, now=lambda: NOW)
        self.assertEqual(outcome["errors"], 0)
        self.assertEqual(outcome["completed"], 1)
        self.assertEqual(repository.claim_pass.call_args.kwargs["worker_id"], "test-worker")
        self.assertEqual(repository.claim_pass.call_args.kwargs["continuations"],
                         [{"channel_id": CHANNEL, "video_type": "short", "scan_id": "scan",
                           "control_epoch": 7, "last_transition_id": "page-ack"}])


if __name__ == "__main__": unittest.main()
