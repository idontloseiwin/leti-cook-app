"""Committed replies use reader evidence or source-free user command receipts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.channel.channel_sync import ChannelCommandRepository, ChannelSyncRepository
from db.reads.frontend import FrontendRepository
from hot_storage import capture_read_lifetime, register_read_deadline
from video_type_reviews import VideoTypeReviewService
from web.channels import _read_committed_job, _source_free_job_receipt

CASE_ID = "f8100000-0000-4000-8000-000000000001"
COMMAND_ID = "f8100000-0000-4000-8000-000000000002"
CHANNEL = "UC" + "c" * 22


def case():
    return dict(
        case_id=CASE_ID, video_id="v" * 11, channel_id=CHANNEL,
        source_detached_at=None, canonical_type="short", observed_types=["long", "short"],
        state="resolved", revision=2, first_seen_at="2026-10-09T10:00:00+00:00",
        last_seen_at="2026-10-09T10:00:00+00:00", can_decide=True, blocked_reason=None,
        decision=dict(video_type="short", reason="My decision",
                      decided_at="2026-10-09T10:00:00+00:00", revision=2),
    )


def command():
    return dict(request_id=COMMAND_ID, expected_revision=1, video_type="short", reason="My decision")


class CommandPreflightTests(unittest.TestCase):
    def test_channel_preflight_uses_the_same_admitted_frontend_projection(self):
        database = SimpleNamespace()
        admitted = dict(channel_id=CHANNEL, title=None)
        repository = ChannelCommandRepository(database)
        with patch.object(FrontendRepository, 'load_channel', return_value=[admitted]) as load:
            self.assertEqual(repository.load_channel_record(CHANNEL), admitted)
            load.assert_called_once_with(CHANNEL)
        with patch.object(FrontendRepository, 'load_channel', return_value=[]):
            self.assertIsNone(repository.load_channel_record(CHANNEL))
        self.assertIsNot(ChannelCommandRepository.load_channel_record,
                         ChannelSyncRepository.load_channel_record)

    def test_video_preflight_preserves_video_presence_without_raw_access(self):
        database = SimpleNamespace(read_row=Mock(return_value=dict(channel_id=CHANNEL)))
        repository = ChannelCommandRepository(database)
        self.assertEqual(repository.load_video_channel_id('v' * 11), CHANNEL)
        operation, statement, parameters = database.read_row.call_args.args
        self.assertIn('FROM readable.videos', statement)
        self.assertNotIn('public.', statement)
        self.assertNotIn('video_inventory', statement)
        self.assertEqual(parameters, ('v' * 11,))
        database.read_row.return_value = None
        self.assertIsNone(repository.load_video_channel_id('v' * 11))


class ReviewPostwriteTests(unittest.TestCase):
    def service(self, current=None, error=None, callback=None):
        receipt = dict(outcome="ready", case=case(),
                       mutation=dict(request_id=COMMAND_ID, replayed=False, applied_revision=2))
        repository = SimpleNamespace(
            decide=Mock(return_value=receipt), get_case=Mock(return_value=current, side_effect=error),
        )
        return VideoTypeReviewService(repository, on_decision=callback), repository, receipt

    def test_response_observations_come_from_independent_reader(self):
        current = case()
        current["observed_types"] = ["short"]
        service, repository, _ = self.service(current)
        result = service.decide(CASE_ID, command())
        self.assertEqual(result["case"]["observed_types"], ["short"])
        repository.decide.assert_called_once()
        repository.get_case.assert_called_once_with(CASE_ID)
        self.assertEqual(result["mutation"]["applied_revision"], 2)

    def test_read_failure_retains_successful_ack_without_provider_observations(self):
        service, repository, receipt = self.service(error=RuntimeError("reader unavailable"))
        result = service.decide(CASE_ID, command())
        self.assertEqual(result["mutation"]["applied_revision"], 2)
        self.assertEqual(result["case"]["observed_types"], [])
        self.assertEqual(result["case"]["canonical_type"], "short")  # user's saved decision
        self.assertEqual(result["case"]["decision"]["reason"], "My decision")
        self.assertEqual(result["case"]["blocked_reason"], "source_unavailable")
        self.assertFalse(result["case"]["can_decide"])
        self.assertEqual(receipt["case"]["observed_types"], ["long", "short"])
        repository.decide.assert_called_once()
        repository.get_case.assert_called_once()

    def test_newer_concurrent_decision_does_not_invalidate_original_receipt(self):
        current = case()
        current.update(revision=3, canonical_type="long", observed_types=["stream"])
        current["decision"] = dict(video_type="long", reason="Later decision",
                                   decided_at="2026-10-09T11:00:00+00:00", revision=3)
        service, repository, _ = self.service(current)
        result = service.decide(CASE_ID, command())
        self.assertEqual(result["case"]["revision"], 2)
        self.assertEqual(result["case"]["decision"]["video_type"], "short")
        self.assertEqual(result["case"]["observed_types"], [])
        self.assertFalse(result["mutation"]["replayed"])
        repository.decide.assert_called_once()

    def test_missing_or_invalid_reader_case_has_the_same_safe_receipt(self):
        for current in (None, {}, dict(case(), case_id=COMMAND_ID)):
            with self.subTest(current=current):
                service, repository, _ = self.service(current)
                result = service.decide(CASE_ID, command())
                self.assertEqual(result["case"]["observed_types"], [])
                repository.decide.assert_called_once()

    def test_cache_invalidation_failure_does_not_hide_confirmed_write(self):
        callback = Mock(side_effect=RuntimeError("cache failure"))
        service, repository, _ = self.service(case(), callback=callback)
        with self.assertLogs("video_type_reviews", level="WARNING"):
            result = service.decide(CASE_ID, command())
        self.assertEqual(result["mutation"]["applied_revision"], 2)
        repository.decide.assert_called_once()

    def test_only_used_reread_deadlines_join_the_response_lifetime(self):
        near = datetime.now(timezone.utc) + timedelta(seconds=1)
        other_used = near + timedelta(hours=1)
        for outcome in ('failure', 'newer_revision', 'used'):
            with self.subTest(outcome=outcome):
                service, repository, _ = self.service()
                def read(_):
                    register_read_deadline(near)
                    if outcome == 'failure':
                        raise RuntimeError('read failed after partial result')
                    current = case()
                    if outcome == 'newer_revision':
                        current['revision'] = 3
                    return current
                repository.get_case.side_effect = read
                with capture_read_lifetime() as response:
                    register_read_deadline(other_used)
                    result = service.decide(CASE_ID, command())
                self.assertEqual(response.expires_at, near if outcome == 'used' else other_used)
                self.assertEqual(result['mutation']['applied_revision'], 2)
                if outcome != 'used':
                    self.assertEqual(result['case']['observed_types'], [])
                    self.assertGreater(response.expires_at, near + timedelta(milliseconds=1))
                repository.decide.assert_called_once()

    def test_bad_ack_never_triggers_reader_or_success(self):
        service, repository, receipt = self.service(case())
        receipt["mutation"]["applied_revision"] = 4
        with self.assertRaises(Exception):
            service.decide(CASE_ID, command())
        repository.get_case.assert_not_called()


class JobPostwriteTests(unittest.TestCase):
    def receipt(self):
        return dict(
            id=CASE_ID, channel_id=CHANNEL, job_type="channel_current_stats_backfill",
            status="queued", phase="video_targets", accepted_epoch=1,
            requested_at=datetime.now(timezone.utc).isoformat(), target_count=50,
            summary_json={"provider_name": "private source"}, last_error_code="provider_error",
            last_error_summary="private message", resume_page_token="private token",
            initial_inventory_checkpoint={"provider": "private"}, unexpected_source="private",
        )

    def test_same_receipt_identity_reread_wins(self):
        receipt = self.receipt()
        current = {**receipt, "summary_json": {"admitted": True}}
        database = object()
        with patch("web.channels.get_operator_job_by_id", return_value=current) as read:
            self.assertIs(_read_committed_job(database, receipt), current)
        read.assert_called_once_with(database, CASE_ID, channel_id=CHANNEL,
                                     job_type="channel_current_stats_backfill")

    def test_reader_failure_keeps_only_command_control_and_progress(self):
        receipt = self.receipt()
        with patch("web.channels.get_operator_job_by_id", side_effect=RuntimeError("reader failed")) as read:
            result = _read_committed_job(object(), receipt)
        self.assertEqual(result["id"], CASE_ID)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["target_count"], 50)
        self.assertEqual(result["summary_json"], {})
        self.assertIsNone(result["last_error_code"])
        self.assertIsNone(result["last_error_summary"])
        self.assertIsNone(result["resume_page_token"])
        self.assertNotIn("initial_inventory_checkpoint", result)
        self.assertNotIn("unexpected_source", result)
        self.assertEqual(receipt["summary_json"], {"provider_name": "private source"})
        read.assert_called_once()

    def test_discarded_job_read_cannot_expire_its_source_free_receipt(self):
        near = datetime.now(timezone.utc) + timedelta(seconds=1)
        other_used = near + timedelta(hours=1)
        receipt = self.receipt()
        for outcome in ('failure', 'different_job', 'used'):
            with self.subTest(outcome=outcome):
                def read(*args, **kwargs):
                    register_read_deadline(near)
                    if outcome == 'failure':
                        raise RuntimeError('control check failed after partial read')
                    return {**receipt, 'id': COMMAND_ID if outcome == 'different_job' else CASE_ID}
                with capture_read_lifetime() as response:
                    register_read_deadline(other_used)
                    with patch('web.channels.get_operator_job_by_id', side_effect=read):
                        result = _read_committed_job(object(), receipt)
                self.assertEqual(response.expires_at, near if outcome == 'used' else other_used)
                self.assertEqual(result['id'], CASE_ID)
                if outcome != 'used':
                    self.assertEqual(result['summary_json'], {})
                    self.assertGreater(response.expires_at, near + timedelta(milliseconds=1))

    def test_an_unrelated_latest_job_cannot_replace_committed_identity(self):
        receipt = self.receipt()
        for current in (None, {**receipt, "id": COMMAND_ID}, {**receipt, "channel_id": "other"},
                        {**receipt, "job_type": "channel_initial_long_inventory"}):
            with self.subTest(current=current), patch("web.channels.get_operator_job_by_id", return_value=current):
                result = _read_committed_job(object(), receipt)
                self.assertEqual(result, _source_free_job_receipt(receipt))


if __name__ == "__main__":
    unittest.main()
