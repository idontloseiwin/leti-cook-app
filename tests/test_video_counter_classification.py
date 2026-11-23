"""Counter completeness stays explicit across all initial video types; offline."""
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from collection.initial_video_hydration import InitialResponse, settle_initial_response
from collection.video_payloads import build_video_collection_metadata_payload
from db import VideoCollectionClaim
from video_collection_service import VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid
from youtube_source_fields import parse_dt

CHANNEL = "UC" + "c" * 22
STAMP = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
END = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
OBSERVED = datetime.now(timezone.utc)


def claim(kind, video_id, *, mode="hydrate"):
    return VideoCollectionClaim(video_id=video_id, channel_id=CHANNEL, video_type=kind,
        collection_mode=mode, lease_token="token-" + video_id,
        lease_expires_at=STAMP, next_due_at=STAMP)


def video(kind, video_id):
    item = {"id": video_id,
        "snippet": {"channelId": CHANNEL, "publishedAt": STAMP, "title": "Title"},
        "contentDetails": {"duration": "PT10M"},
        "status": {"privacyStatus": "public", "uploadStatus": "processed"},
        "statistics": {"viewCount": "123", "likeCount": "7", "commentCount": "2"}}
    if kind == "stream":
        item["liveStreamingDetails"] = {"actualEndTime": END}
    return item


def service():
    repository = Mock()
    forbidden = Mock(side_effect=AssertionError("provider must not run while classifying a response"))
    build = partial(build_video_collection_metadata_payload, batch_stats_enabled=True,
        parse_dt=parse_dt, view_count_absent_error=YouTubeViewCountAbsent,
        view_count_invalid_error=YouTubeViewCountInvalid)
    return VideoCollectionService(repository, fetch_full=forbidden, fetch_metadata=forbidden,
        fetch_counters=forbidden, build_full_payload=build, build_counter_payload=forbidden)


class VideoCounterClassificationTests(unittest.TestCase):
    def test_absent_views_preserve_positive_general_for_all_types(self):
        for kind in ("long", "short", "stream"):
            with self.subTest(kind=kind):
                collector = service()
                item = video(kind, "absent")
                item["statistics"].pop("viewCount")
                summary = collector._process_common_claims([claim(kind, "absent")],
                    initial_response=InitialResponse(metadata_observed_at=OBSERVED, items=(item,)))
                rows = collector.repository.complete_identity_refresh.call_args.args[0]
                self.assertEqual([(row["counterless_profile"], row["missing"], row["availability_status"])
                                  for row in rows], [(True, False, "public")])
                self.assertEqual(rows[0]["recorded_at"], OBSERVED.isoformat())
                self.assertNotIn("views", rows[0])
                collector.repository.fail_batch.assert_not_called()
                self.assertEqual((summary["metadata_deferred"], summary["source_view_count_absent"],
                    summary["source_view_count_invalid"], summary["errors"], summary["updated"]), (1, 1, 0, 0, 0))
                collector.repository.complete_metadata.assert_not_called()
                collector.repository.complete_typed_metadata.assert_not_called()
                collector.repository.complete_missing.assert_not_called()

    def test_invalid_views_are_not_deferral_or_generic_payload_errors(self):
        for kind in ("long", "short", "stream"):
            with self.subTest(kind=kind):
                collector = service()
                item = video(kind, "invalid")
                item["statistics"]["viewCount"] = "invalid-counter"
                summary = collector._process_common_claims([claim(kind, "invalid")],
                    initial_response=InitialResponse(metadata_observed_at=OBSERVED, items=(item,)))
                rows = collector.repository.fail_batch.call_args.args[0]
                self.assertEqual([(row["error_type"], row["retry_after_seconds"]) for row in rows],
                    [("youtube_view_count_invalid", None)])
                self.assertEqual((summary["metadata_deferred"], summary["source_view_count_absent"],
                    summary["source_view_count_invalid"], summary["errors"], summary["updated"]), (0, 0, 1, 1, 0))

    def test_unfinished_stream_without_counters_remains_a_lifecycle_probe(self):
        collector = service()
        item = video("stream", "unfinished")
        item.pop("statistics")
        item.pop("liveStreamingDetails")
        summary = collector._process_common_claims([claim("stream", "unfinished", mode="stream_lifecycle")],
            initial_response=InitialResponse(metadata_observed_at=OBSERVED, items=(item,)))
        collector.repository.complete_stream_lifecycle.assert_called_once()
        collector.repository.fail_batch.assert_not_called()
        self.assertEqual((summary["stream_lifecycle_probed"], summary["metadata_deferred"],
            summary["errors"], summary["updated"]), (1, 0, 0, 0))

    def test_unrelated_payload_failure_keeps_its_own_error(self):
        for kind in ("long", "short", "stream", "unknown"):
            for field in ("missing_title", "invalid_title", "topic_categories"):
                with self.subTest(kind=kind, field=field):
                    collector = service()
                    item = video(kind, "malformed")
                    if field == "missing_title":
                        item["snippet"].pop("title")
                    elif field == "invalid_title":
                        item["snippet"]["title"] = {"private": "provider-value"}
                    else:
                        item["topicDetails"] = {"topicCategories": "private-provider-value"}
                    with patch("collection.video_trace.event") as observed:
                        summary = collector._process_common_claims(
                            [claim(kind, "malformed", mode="identity_refresh" if kind == "unknown" else "hydrate")],
                            initial_response=InitialResponse(metadata_observed_at=OBSERVED, items=(item,)))
                    row = collector.repository.fail_batch.call_args.args[0][0]
                    self.assertEqual((row["error_type"], row["retry_after_seconds"]), ("payload_build_error", None))
                    self.assertEqual((summary["source_view_count_absent"], summary["source_view_count_invalid"],
                        summary["errors"], summary["updated"]), (0, 0, 1, 0))
                    diagnostics = [call for call in observed.call_args_list
                                   if call.args[:2] == ("video_validation", "failed")]
                    self.assertEqual(len(diagnostics), 1)
                    self.assertEqual((diagnostics[0].kwargs["reason"], diagnostics[0].kwargs["phase"]),
                        ("topic_categories_invalid", "topicDetails.topicCategories") if field == "topic_categories"
                        else ("title_invalid", "snippet.title"))
                    self.assertNotIn("provider-value", str(diagnostics[0].kwargs))
                    collector.repository.complete_stream_lifecycle.assert_not_called()
                    collector.repository.complete_identity_refresh.assert_not_called()

    def test_early_profile_validation_preserves_retained_typed_payload_error(self):
        for kind in ("short", "stream"):
            for field in ("title", "topics"):
                with self.subTest(kind=kind, field=field):
                    collector = service()
                    bad = video(kind, "bad")
                    if field == "title":
                        bad["snippet"].pop("title")
                    else:
                        bad["topicDetails"] = {"topicCategories": "invalid-shape"}
                    collector.fetch_full = Mock(return_value=[bad, video(kind, "good")])
                    summary = collector.process_typed_claims(
                        [claim(kind, "bad"), claim(kind, "good")], video_type=kind, reason="ordinary_profile")
                    row = collector.repository.fail_typed.call_args.args[1][0]
                    self.assertEqual((row["error_type"], row["retry_after_seconds"]), ("payload_build_error", None))
                    self.assertEqual((summary["errors"], summary["updated"]), (1, 1))
                    collector.repository.complete_typed_metadata.assert_called_once()

    def test_waiting_profile_refusal_keeps_its_lifecycle_classification(self):
        for field in ("title", "topics"):
            with self.subTest(field=field):
                collector = service()
                item = video("long", "waiting")
                item["snippet"]["liveBroadcastContent"] = "upcoming"
                item["contentDetails"].pop("duration")
                item.pop("statistics")
                if field == "title":
                    item["snippet"].pop("title")
                else:
                    item["topicDetails"] = {"topicCategories": "invalid-shape"}
                summary = collector._process_common_claims([claim("long", "waiting")],
                    initial_response=InitialResponse(metadata_observed_at=OBSERVED, items=(item,)))
                row = collector.repository.fail_batch.call_args.args[0][0]
                self.assertEqual((row["error_type"], row["retry_after_seconds"]),
                                 ("provider_identity_or_lifecycle_error", None))
                self.assertEqual((summary["errors"], summary["publication_waiting"]), (1, 0))
                collector.repository.complete_stream_lifecycle.assert_not_called()

    def test_atomic_plan_keeps_good_rows_and_specific_bad_rows_in_one_commit(self):
        collector = service()
        collector.repository.complete_initial_batch.return_value = 3
        items = [video("long", "good"), video("short", "absent"), video("stream", "invalid")]
        items[1]["statistics"].pop("viewCount")
        items[2]["statistics"]["viewCount"] = "invalid-counter"
        summary = settle_initial_response(collector,
            [claim("long", "good"), claim("short", "absent"), claim("stream", "invalid")],
            InitialResponse(metadata_observed_at=OBSERVED, items=tuple(items)), reason="initial_channel_hydration")
        collector.repository.complete_initial_batch.assert_called_once()
        commands = collector.repository.complete_initial_batch.call_args.args[0]
        rows = {row["video_id"]: (command["operation"], row)
            for command in commands for row in command["rows"]}
        self.assertEqual(set(rows), {"good", "absent", "invalid"})
        self.assertEqual((rows["good"][0], rows["good"][1]["views"]), ("complete_metadata", 123))
        self.assertEqual((rows["absent"][0], rows["absent"][1]["counterless_profile"],
            rows["absent"][1]["missing"]), ("complete_identity_refresh", True, False))
        self.assertNotIn("views", rows["absent"][1])
        self.assertEqual((rows["invalid"][0], rows["invalid"][1]["error_type"]),
            ("fail_batch", "youtube_view_count_invalid"))
        self.assertEqual((summary["updated"], summary["metadata_deferred"], summary["errors"]), (1, 1, 1))
        self.assertFalse(summary["settlement_unknown"])


    def test_batch_refusal_defers_only_affected_items_and_keeps_correct_bucket(self):
        for quota in (False, True):
            with self.subTest(quota=quota):
                collector = service()
                collector.is_quota_exhausted = lambda error: quota
                collector.repository.complete_initial_batch.return_value = 2
                items = [video("long", "good"), video("short", "affected")]
                items[1]["statistics"].pop("viewCount")
                response = SimpleNamespace(items_or_raise=lambda: tuple(items), error=None, elapsed=0,
                    counter_ids=("affected",), counter_error=RuntimeError("private provider response"),
                    batch_requests=1, metadata_observed_at=OBSERVED, counter_observed_ids=())
                summary = settle_initial_response(collector,
                    [claim("long", "good"), claim("short", "affected")], response,
                    reason="initial_channel_hydration")
                commands = collector.repository.complete_initial_batch.call_args.args[0]
                rows = {row["video_id"]: (command["operation"], row)
                    for command in commands for row in command["rows"]}
                self.assertEqual(rows["good"][0], "complete_metadata")
                self.assertEqual(rows["affected"][0], "complete_identity_refresh")
                self.assertTrue(rows["affected"][1]["counterless_profile"])
                self.assertFalse(rows["affected"][1]["missing"])
                self.assertEqual(rows["affected"][1]["availability_status"], "public")
                self.assertNotIn("views", rows["affected"][1])
                self.assertEqual((summary["metadata_requests"], summary["batch_requests"],
                    summary["degraded_batch_requests"], summary["updated"]), (1, 1, 1, 1))
                self.assertEqual(summary["batch_quota_exhausted"], quota)
                self.assertEqual(summary["quota_exhausted"], quota)
                self.assertEqual(summary["batch_unavailable"], not quota)
                self.assertFalse(summary["general_quota_exhausted"])
                self.assertFalse(summary["settlement_unknown"])
                self.assertEqual(summary["stopped_reason"], "quota_exhausted" if quota else "batch_unavailable")
                self.assertEqual(summary["source_view_count_absent"], 1)
                self.assertNotIn("private provider response", str(summary))

    def test_successful_fallback_does_not_disguise_remaining_absent_views(self):
        collector = service()
        item = video("stream", "absent")
        item["statistics"].pop("viewCount")
        response = SimpleNamespace(items_or_raise=lambda: (item,), counter_error=None,
            counter_ids=("absent",), batch_requests=1, metadata_observed_at=OBSERVED, counter_observed_ids=())
        summary = collector._process_common_claims([claim("stream", "absent")], initial_response=response)
        row = collector.repository.complete_identity_refresh.call_args.args[0][0]
        self.assertEqual((row["counterless_profile"], row["missing"], row["availability_status"]),
                         (True, False, "public"))
        self.assertNotIn("views", row)
        collector.repository.fail_batch.assert_not_called()
        self.assertEqual((summary["batch_requests"], summary["source_view_count_absent"],
            summary["errors"], summary["updated"]), (1, 1, 0, 0))
        self.assertFalse(summary["batch_unavailable"])


if __name__ == "__main__":
    unittest.main()
