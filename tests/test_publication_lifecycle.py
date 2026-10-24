"""Type-independent publication waiting through real provider/service planning."""
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "application"), str(Path(__file__).resolve().parent)]
import test_recurring_general_profiles as profiles
import test_initial_video_parallel as initial
from test_video_source_clocks import GENERAL, claim
from hot_storage_fields import video_source_fields
from collection_outcome import collection_outcome_fields
from channel_hydration import with_format_readiness, _typed_format
from video_collection_service import VideoCollectionService


def upcoming(identity, *, status="upcoming", scheduled=True):
    item = initial.video(identity)
    item["snippet"].update(liveBroadcastContent=status, publishedAt="2023-11-06T00:00:00Z")
    item["contentDetails"].pop("duration")
    item.pop("statistics")
    item["liveStreamingDetails"] = ({"scheduledStartTime": "2026-12-01T12:30:00Z"} if scheduled else {})
    return item


class PublicationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.h = profiles.RecurringGeneralProfileTests("runTest")
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_waiting_all_types_without_duration_counters_or_stats_fallback(self):
        kinds = ("long", "short", "stream", "unknown")
        claims = [claim(kind, kind, "identity_refresh" if kind == "unknown" else "metadata") for kind in kinds]
        self.h.items = [upcoming(kind) for kind in kinds] + [initial.video("ready")]
        result, calls = self.h.process(claims + [claim("ready")])
        self.assertEqual(calls, ["general"])
        self.h.stats.assert_not_called()
        self.assertEqual((result["updated"], result["publication_waiting"], result["stream_lifecycle_probed"], result["errors"]), (1, 4, 1, 0))
        self.assertEqual(result["completion_outcome"], "completed")
        for kind in kinds:
            operation, row = self.h.rows()[kind]
            self.assertEqual(operation, "lifecycle")
            self.assertEqual(row["video_type"], kind)
            self.assertEqual(row["publication_status"], "upcoming")
            self.assertEqual(row["scheduled_start_at"], "2026-12-01T12:30:00+00:00")
            self.assertEqual(row["recorded_at"], GENERAL.isoformat())
            self.assertIsNone(row["duration"])
            self.assertFalse({"views", "like_count", "comment_count"} & row.keys())

    def test_duration_and_counters_do_not_turn_upcoming_or_live_into_ready(self):
        for status in ("upcoming", "live"):
            with self.subTest(status=status):
                self.h.repo.writes.clear()
                item = upcoming("waiting", status=status, scheduled=False)
                item["contentDetails"]["duration"] = "PT10M"
                item["statistics"] = {"viewCount": "17"}
                self.h.items = [item]
                result, calls = self.h.process([claim("waiting")])
                self.assertEqual(calls, ["general"])
                self.assertEqual((result["updated"], result["publication_waiting"], result["errors"]), (0, 1, 0))
                row = self.h.rows()["waiting"][1]
                self.assertEqual(row["publication_status"], status)
                self.assertIsNone(row["scheduled_start_at"])
                self.assertNotIn("views", row)

    def test_changed_or_past_schedule_is_observation_not_completion(self):
        for stamp in ("2026-10-01T00:00:00Z", "2026-12-01T12:30:00Z", "2027-01-01T00:00:00Z", None):
            with self.subTest(stamp=stamp):
                self.h.repo.writes.clear()
                item = upcoming("waiting", scheduled=False)
                if stamp:
                    item["liveStreamingDetails"]["scheduledStartTime"] = stamp
                self.h.items = [item]
                result, calls = self.h.process([claim("waiting")])
                self.assertEqual((result["publication_waiting"], result["errors"]), (1, 0))
                row = self.h.rows()["waiting"][1]
                self.assertEqual(row["scheduled_start_at"], stamp.replace("Z", "+00:00") if stamp else None)
                self.assertEqual(row["published_at"], "2023-11-06T00:00:00Z")

    def test_finished_profile_uses_normal_completion_and_keeps_type(self):
        for kind in ("long", "short", "stream"):
            with self.subTest(kind=kind):
                self.h.repo.writes.clear()
                item = initial.video(kind)
                item["snippet"]["liveBroadcastContent"] = "none"
                item["liveStreamingDetails"] = {"actualStartTime": (GENERAL - timedelta(hours=2)).isoformat(),
                    "actualEndTime": (GENERAL - timedelta(hours=1)).isoformat()}
                self.h.items = [item]
                result, calls = self.h.process([claim(kind, kind, "stream_lifecycle" if kind == "stream" else "metadata")])
                self.assertEqual((result["updated"], result["publication_waiting"], result["errors"]), (1, 0, 0))
                operation, row = self.h.rows()[kind]
                self.assertIn(operation, ("metadata", "typed"))
                self.assertEqual(row["views"], 123)
                if kind != "long":
                    self.assertEqual(row["video_type"], kind)

    def test_invalid_evidence_preserves_retry_instead_of_marking_unavailable(self):
        cases = (("duration", "PT0.5S", "duration_invalid"),
                 ("scheduledStartTime", "not-a-date", "scheduled_start_time_invalid"),
                 ("actualStartTime", "2099-01-01T00:00:00Z", "actual_start_time_future"),
                 ("actualEndTime", "2099-01-01T00:00:00Z", "actual_end_time_future"),
                 ("actualEndTime", "2026-10-01T00:00:00Z", "publication_evidence_conflict"))
        for field, value, expected in cases:
            with self.subTest(field=field):
                self.h.repo.writes.clear()
                item = upcoming("bad")
                item["contentDetails" if field == "duration" else "liveStreamingDetails"][field] = value
                with self.assertRaises(ValueError) as failure:
                    VideoCollectionService._validate_common_metadata(claim("bad"), item, GENERAL)
                self.assertEqual(str(failure.exception), expected)
                self.h.items = [item, initial.video("good")]
                result, calls = self.h.process([claim("bad"), claim("good")])
                self.assertEqual((result["errors"], result["updated"], result["unavailable_marked"]), (1, 1, 0))
                self.assertEqual(self.h.rows()["bad"][0], "failure")

    def test_missing_regular_duration_still_invalid(self):
        item = initial.video("bad")
        item["contentDetails"].pop("duration")
        with self.assertRaisesRegex(ValueError, "duration_missing"):
            VideoCollectionService._validate_common_metadata(claim("bad"), item, GENERAL)

    def test_missing_item_keeps_existing_unavailable_policy(self):
        result, calls = self.h.process([claim("missing")])
        self.assertEqual(calls, ["general"])
        self.assertEqual((result["unavailable_marked"], result["publication_waiting"]), (1, 0))
        self.assertEqual(self.h.rows()["missing"][0], "missing")

    def test_retained_typed_entrypoint_accepts_waiting_without_statistics(self):
        for kind in ("short", "stream"):
            with self.subTest(kind=kind):
                self.h.repo.writes.clear()
                self.h.collector.fetch_full = Mock(return_value=[upcoming(kind)])
                result = self.h.collector.process_typed_claims([claim(kind, kind)], video_type=kind, reason="publication_test")
                self.assertEqual((result["publication_waiting"], result["errors"]), (1, 0))
                self.assertEqual(self.h.rows()[kind][0], "lifecycle")
                self.h.stats.assert_not_called()

    def test_initial_mixed_batch_is_atomic_and_waiting_is_terminal(self):
        kinds = {kind: (kind, "identity_refresh" if kind == "unknown" else "hydrate")
                 for kind in ("long", "short", "stream", "unknown")}
        repo = initial.AtomicRepository(kinds)
        fetch = Mock(return_value=[upcoming(kind) for kind in kinds] + [initial.video("ready")])
        collector = initial.service(repo, fetch)
        result = initial.run(collector, [*kinds, "ready"])
        self.assertEqual(len(repo.commits), 1)
        rows = {row["video_id"]: (command["operation"], row) for command in repo.commits[0] for row in command["rows"]}
        self.assertEqual(len(rows), 5)
        for kind in kinds:
            self.assertEqual(rows[kind][0], "complete_stream_lifecycle")
        self.assertEqual((result["updated"], result["publication_waiting"], result["errors"]), (1, 4, 0))
        self.assertEqual(result["completion_outcome"], "completed")
        collector.forbidden.assert_not_called()

    def test_initial_failed_commit_never_acknowledges_waiting(self):
        repo = initial.AtomicRepository()
        repo.commit_hook = Mock(side_effect=RuntimeError("database unavailable"))
        collector = initial.service(repo, Mock(return_value=[upcoming("waiting")]))
        result = initial.run(collector, ["waiting"])
        self.assertEqual((result["publication_waiting"], result["stream_lifecycle_probed"], result["updated"]), (0, 0, 0))
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["completion_outcome"], "error")

    def test_stream_subset_is_not_double_counted(self):
        summary = {"claimed": 5, "updated": 1, "publication_waiting": 3, "stream_lifecycle_probed": 2}
        self.assertEqual(collection_outcome_fields(summary)["unresolved"], 1)
        self.assertEqual(collection_outcome_fields({"claimed": 1, "stream_lifecycle_probed": 1})["completion_outcome"], "completed")

    def test_waiting_does_not_close_other_published_long_profiles(self):
        def row(updated=0, checked=0):
            return {"total": updated + checked, "updated": updated, "checked": checked,
                    "unavailable": 0, "pending": 0, "errors": 0, "inventory_ready": True,
                    "initial_check_complete": True, "current_profile_ready": not checked}
        payload = {"job": {"status": "completed", "phase": "verify", "errors": 0,
                   "initial_video_types": ["long", "short", "stream"], "initial_scope_version": 1,
                   "targets_frozen_at": GENERAL.isoformat(),
                   "summary": {"per_format": {"long": row(49, 1), "short": row(2, 1), "stream": row()}}}}
        result = with_format_readiness(payload)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["current_profile_ready"])
        self.assertTrue(result["all_initial_checks_complete"])
        self.assertFalse(result["formats"]["long"]["current_profile_ready"])
        self.assertEqual(result["formats"]["long"]["checked"], 1)
        payload["job"]["status"] = "running"
        self.assertFalse(with_format_readiness(payload)["current_profile_ready"])

    def test_short_waiting_is_checked_without_becoming_full_profile(self):
        row = {"known_count": 2, "hydrated_count": 1, "unavailable_count": 0,
               "awaiting_completion_count": 1, "pending_count": 0, "missing_queue_count": 0,
               "identity_unresolved_count": 0, "failed_count": 0,
               "inventory_ready": True, "runtime_enabled": True, "inventory_status": "ready"}
        result = _typed_format("short", row, channel_eligible=True, runtime_enabled=True)
        self.assertEqual(result["status"], "checked")
        self.assertTrue(result["initial_check_complete"])
        self.assertFalse(result["current_profile_ready"])

    def test_expired_publication_fields_follow_metadata_clock_not_future_date(self):
        row = {"metadata_checked_at": (GENERAL - timedelta(days=29, microseconds=1)).isoformat(),
               "publication_status": "upcoming", "scheduled_start_at": "2099-01-01T00:00:00Z"}
        masked = video_source_fields(row, now=GENERAL)
        self.assertIsNone(masked["publication_status"])
        self.assertIsNone(masked["scheduled_start_at"])
        row["metadata_checked_at"] = GENERAL.isoformat()
        self.assertEqual(video_source_fields(row, now=GENERAL)["scheduled_start_at"], row["scheduled_start_at"])


if __name__ == "__main__":
    unittest.main()
