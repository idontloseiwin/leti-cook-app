"""Regular complete General profiles through real runtime/service; offline."""
from functools import partial
import importlib.util
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
from collection.settings import CollectionSettings
from collection import video_trace
import pipeline_trace
from telemetry_contract import build_event, normalize_stored_event
from collection.video_payloads import build_video_counter_payload
import collection_shutdown
from runtime import collection as runtime
from youtube_client import _validate_video_request_boundary
from youtube_errors import YouTubeDailyQuotaExceededError, YouTubeRateLimitError
from youtube_video_profiles import fetch_video_metadata
from test_initial_video_parallel import service
from test_video_source_clocks import GENERAL, STATS, Repo, claim, video


class RecurringGeneralProfileTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(collection_shutdown, "_requested", Event())
        gate.start()
        self.addCleanup(gate.stop)
        self.repo = Repo()
        self.collector = service(self.repo, Mock(side_effect=AssertionError("initial path used")))
        self.collector.build_counter_payload = build_video_counter_payload
        self.transport = Mock(side_effect=self.general_response)
        self.items, self.general_error, self.stats_error = [], None, None
        self.stats = Mock(side_effect=self.counter_response)
        self.inputs = dict(database=self.repo.database, settings=CollectionSettings(True, True, True),
            api_error=RuntimeError,
            get_video_metadata=partial(fetch_video_metadata, youtube_api_get=self.transport,
                api_key="offline", attempt_recorder=object(), timeout_seconds=17,
                emit_event=Mock(), perf_counter=time.perf_counter),
            fetch_video_batch_stats=self.stats, api_key="offline", attempt_recorder=object(),
            timeout_seconds=17, counter_parts=("views", "likes", "comments"),
            build_full_payload=Mock(), build_counter_payload=build_video_counter_payload,
            is_quota_exhausted=self.collector.is_quota_exhausted)
        dependencies = runtime.video_collection_dependencies(**self.inputs)
        self.collector.fetch_metadata = dependencies["fetch_metadata"]
        self.collector.fetch_counters = dependencies["fetch_counters"]

    def general_response(self, endpoint, **arguments):
        self.assertEqual(endpoint, "videos")
        self.assertTrue(arguments["complete_profile"])
        self.assertNotIn("initial_profile", arguments)
        _validate_video_request_boundary(endpoint, arguments["quota_bucket"],
            arguments["params"], complete_profile=arguments["complete_profile"])
        if self.general_error:
            raise self.general_error
        return SimpleNamespace(json=lambda: {"items": self.items}, raise_for_status=lambda: None)

    def counter_response(self, ids, **arguments):
        if self.stats_error:
            raise self.stats_error
        return {"items": [{"id": identity, "views": 456, "like_count": None, "comment_count": None}
                          for identity in ids], "summary": {"failed_ids": []}}

    def process(self, claims):
        steps, attempted = self.collector.common_claim_steps(claims), []
        try:
            step = next(steps)
            while True:
                general = step.fetch == self.collector.fetch_metadata
                attempted.append("general" if general else "stats")
                with patch("collection.provider_steps.datetime") as clock:
                    clock.now.return_value = GENERAL if general else STATS
                    result = step.execute()
                step = steps.send(result)
        except StopIteration as done:
            return done.value, attempted
        finally:
            steps.close()

    def rows(self):
        return {row["video_id"]: (operation, row)
                for operation, values in self.repo.writes for row in values}

    def test_complete_general_profiles_need_no_stats_and_keep_general_clock(self):
        claims = [claim("long", "long"), claim("short", "short"), claim("stream", "stream")]
        self.items = [video(row.video_id, ended=row.video_type == "stream") for row in claims]
        result, calls = self.process(claims)
        self.assertEqual(calls, ["general"])
        self.stats.assert_not_called()
        self.assertEqual((result["updated"], result["metadata_requests"], result["batch_requests"]), (3, 1, 0))
        self.assertEqual(self.transport.call_args.kwargs["params"]["id"], "long,short,stream")
        for operation, row in self.rows().values():
            self.assertEqual(row["views"], 123)
            self.assertEqual(row["recorded_at"], GENERAL.isoformat())
            self.assertEqual(row["metadata_observed_at"], GENERAL.isoformat())
        self.collector.fetch_initial_profiles.assert_not_called()

    def test_only_missing_required_counts_use_targeted_stats_with_distinct_clock(self):
        self.items = [video("healthy"), video("fallback")]
        self.items[1]["statistics"].pop("viewCount")
        result, calls = self.process([claim("healthy"), claim("fallback"), claim("onlystats", mode="counters")])
        self.assertEqual(calls, ["general", "stats"])
        self.assertEqual(self.stats.call_args.args[0], ["onlystats", "fallback"])
        rows = self.rows()
        self.assertEqual(rows["healthy"][1]["views"], 123)
        self.assertEqual(rows["healthy"][1]["recorded_at"], GENERAL.isoformat())
        self.assertEqual(rows["fallback"][1]["views"], 456)
        self.assertEqual(rows["fallback"][1]["recorded_at"], STATS.isoformat())
        self.assertEqual(rows["fallback"][1]["metadata_observed_at"], GENERAL.isoformat())
        self.assertNotIn("metadata_observed_at", rows["onlystats"][1])
        self.assertEqual(result["updated"], 3)
        self.assertEqual(sum(len(values) for _, values in self.repo.writes), 3)

    def test_optional_missing_counts_stay_null_without_extra_request(self):
        self.items = [video("video")]
        self.items[0]["statistics"] = {"viewCount": "0"}
        result, calls = self.process([claim("video")])
        self.assertEqual(calls, ["general"])
        row = self.rows()["video"][1]
        self.assertEqual(row["views"], 0)
        self.assertIsNone(row["like_count"])
        self.assertIsNone(row["comment_count"])
        self.assertEqual(result["updated"], 1)

    def test_counter_only_has_no_general_request_or_metadata_renewal(self):
        result, calls = self.process([claim("video", mode="counters")])
        self.assertEqual(calls, ["stats"])
        self.transport.assert_not_called()
        operation, row = self.rows()["video"]
        self.assertEqual(operation, "counters")
        self.assertEqual(row["recorded_at"], STATS.isoformat())
        self.assertNotIn("metadata_observed_at", row)
        self.assertEqual(result["updated"], 1)

    def test_all_three_types_share_one_stats_request_and_keep_type_accounting(self):
        claims = [claim(kind, kind, "counters") for kind in ("long", "short", "stream")]
        result, calls = self.process(claims)
        self.assertEqual(calls, ["stats"])
        self.transport.assert_not_called()
        self.assertEqual(self.stats.call_args.args[0], ["long", "short", "stream"])
        self.assertEqual((result["updated"], result["partial_counter_updates"]), (3, 3))
        for kind in ("long", "short", "stream"):
            self.assertEqual(result[f"{kind}_updated"], 1)
            operation, row = self.rows()[kind]
            self.assertEqual(operation, "counters")
            self.assertEqual(row["recorded_at"], STATS.isoformat())
            self.assertNotIn("metadata_observed_at", row)

    def test_retained_typed_entrypoint_cannot_convert_stats_into_general(self):
        for kind in ("short", "stream"):
            with self.subTest(kind=kind):
                self.repo.writes.clear()
                self.stats.reset_mock()
                result = self.collector.process_typed_claims([claim(kind, kind, "counters")],
                    video_type=kind, reason="addressed_typed")
                self.transport.assert_not_called()
                self.stats.assert_called_once()
                self.assertEqual((result["requested"], result["claimed"], result["updated"]), (1, 1, 1))
                self.assertEqual(result[f"{kind}_updated"], 1)
                self.assertEqual(self.rows()[kind][0], "counters")

    def test_typed_stats_quota_releases_unsent_remainder_through_common_failure(self):
        self.stats_error = YouTubeDailyQuotaExceededError()
        claims = [claim(f"short-{index}", "short", "counters") for index in range(51)]
        result = self.collector.process_typed_claims(claims, video_type="short", reason="typed_stats")
        self.assertTrue(result["quota_exhausted"])
        self.transport.assert_not_called()
        self.stats.assert_called_once()
        self.assertEqual([len(rows) for operation, rows in self.repo.writes if operation == "failure"], [50, 1])
        self.assertEqual(set(self.rows()), {row.video_id for row in claims})

    def test_stats_omission_promotes_general_for_each_type_without_marking_missing(self):
        claims = [claim(kind, kind, "counters") for kind in ("long", "short", "stream")]
        self.stats.side_effect = lambda *args, **kwargs: {"items": [], "summary": {"failed_ids": []}}
        result, calls = self.process(claims)
        self.assertEqual(calls, ["stats"])
        self.assertEqual((result["updated"], result["missing"], result["unavailable_marked"]), (0, 0, 0))
        for kind in ("long", "short", "stream"):
            operation, row = self.rows()[kind]
            self.assertEqual(operation, "failure")
            self.assertEqual(row["video_type"], kind)
            self.assertEqual(row["error_type"], "youtube_stats_counters_unavailable")
            self.assertEqual(row["retry_after_seconds"], 30)

    def test_typed_stats_quota_or_transport_errors_do_not_become_unavailability(self):
        for error, expected in ((YouTubeDailyQuotaExceededError(), "youtube_quota_exhausted"),
                                (TimeoutError("offline"), "youtube_batch_request_error")):
            with self.subTest(error=type(error).__name__):
                self.repo.writes.clear()
                self.stats_error = error
                result, calls = self.process([claim("short", "short", "counters"),
                                              claim("stream", "stream", "counters")])
                self.assertEqual(calls, ["stats"])
                self.assertEqual(result["unavailable_marked"], 0)
                self.assertEqual({row["error_type"] for _, row in self.rows().values()}, {expected})

    def test_retained_types_can_refresh_identity_without_collecting_counters(self):
        claims = [claim(kind, kind, "identity_refresh") for kind in ("long", "short", "stream")]
        self.items = [video(row.video_id) for row in claims]
        result, calls = self.process(claims)
        self.assertEqual(calls, ["general"])
        self.stats.assert_not_called()
        self.assertEqual(result["updated"], 3)
        for kind in ("long", "short", "stream"):
            operation, row = self.rows()[kind]
            self.assertEqual(operation, "identity")
            self.assertEqual(row["video_type"], kind)
            self.assertEqual(result[f"{kind}_updated"], 1)
            self.assertFalse({"views", "like_count", "comment_count"} & row.keys())

    def test_missing_retained_identity_has_one_identity_settlement_per_type(self):
        claims = [claim(kind, kind, "identity_refresh") for kind in ("long", "short", "stream")]
        result, calls = self.process(claims)
        self.assertEqual(calls, ["general"])
        self.assertEqual(result["missing"], 3)
        self.assertEqual(sum(len(rows) for _, rows in self.repo.writes), 3)
        for kind in ("long", "short", "stream"):
            operation, row = self.rows()[kind]
            self.assertEqual(operation, "identity")
            self.assertTrue(row["missing"])
            self.assertEqual(row["video_type"], kind)
            self.assertEqual(result[f"{kind}_missing"], 1)

    def test_invalid_view_count_is_rejected_instead_of_spending_fallback(self):
        for invalid in ("bad", "-1", {}, True):
            with self.subTest(invalid=invalid):
                self.repo.writes.clear()
                self.items = [video("video")]
                self.items[0]["statistics"]["viewCount"] = invalid
                result, calls = self.process([claim("video")])
                self.assertEqual(calls, ["general"])
                self.assertEqual(result["updated"], 0)
                self.assertEqual(self.rows()["video"][1]["error_type"], "youtube_view_count_invalid")

    def test_unfinished_stream_and_unknown_identity_never_fetch_counters(self):
        for statistics in ({}, {"viewCount": "123", "likeCount": "7", "commentCount": "2"}):
            with self.subTest(statistics=statistics):
                self.repo.writes.clear()
                self.items = [video("pending"), video("unknown")]
                for item in self.items:
                    item["statistics"] = statistics
                result, calls = self.process([claim("pending", "stream", "stream_lifecycle"),
                                              claim("unknown", "unknown", "identity_refresh")])
                self.assertEqual(calls, ["general"])
                self.assertEqual(self.rows()["pending"][0], "lifecycle")
                self.assertEqual(self.rows()["pending"][1]["availability_status"], "public")
                self.assertEqual(self.rows()["unknown"][0], "identity")
                for _, row in self.rows().values():
                    self.assertFalse({"views", "like_count", "comment_count"} & row.keys())
                self.assertEqual(result["stream_lifecycle_probed"], 1)

    def test_foreign_channel_and_duplicate_id_do_not_enter_fallback(self):
        self.items = [video("wrong"), video("duplicate"), video("duplicate")]
        self.items[0]["snippet"]["channelId"] = "UC" + "x" * 22
        for item in self.items:
            item["statistics"] = {}
        result, calls = self.process([claim("wrong"), claim("duplicate")])
        self.assertEqual(calls, ["general"])
        self.assertEqual(result["updated"], 0)
        self.assertEqual({row["error_type"] for _, row in self.rows().values()},
                         {"provider_identity_or_lifecycle_error", "duplicate_video_metadata_item"})

    def test_validation_refusal_records_safe_field_reason_without_changing_settlement(self):
        cases = [
            ("snippet", None, "snippet_missing_or_invalid", "snippet", "long"),
            ("snippet.channelId", "UC" + "x" * 22, "channel_id_mismatch", "snippet.channelId", "long"),
            ("snippet.publishedAt", None, "published_at_missing", "snippet.publishedAt", "long"),
            ("snippet.publishedAt", "2026-10-09T00:00:00", "published_at_invalid", "snippet.publishedAt", "long"),
            ("contentDetails", [], "content_details_missing_or_invalid", "contentDetails", "long"),
            ("status", [], "status_missing_or_invalid", "status", "long"),
            ("contentDetails.duration", None, "duration_missing", "contentDetails.duration", "long"),
            ("contentDetails.duration", "PT0.5S", "duration_invalid", "contentDetails.duration", "long"),
            ("status.privacyStatus", "PAYLOAD_MUST_NOT_ESCAPE", "privacy_status_invalid", "status.privacyStatus", "long"),
            ("status.uploadStatus", "", "upload_status_invalid", "status.uploadStatus", "long"),
            ("liveStreamingDetails", [], "live_streaming_details_invalid", "liveStreamingDetails", "stream"),
            ("liveStreamingDetails.actualEndTime", "PAYLOAD_MUST_NOT_ESCAPE", "actual_end_time_invalid", "liveStreamingDetails.actualEndTime", "stream"),
            ("liveStreamingDetails.actualEndTime", None, "stream_completion_evidence_regressed", "liveStreamingDetails.actualEndTime", "stream"),
            ("liveStreamingDetails.actualEndTime", "2099-01-01T00:00:00Z", "actual_end_time_future", "liveStreamingDetails.actualEndTime", "stream"),
        ]
        for path, value, reason, field, kind in cases:
            with self.subTest(reason=reason):
                self.repo.writes.clear()
                bad, good = claim("bad00000001", kind), claim("good0000001")
                item = video(bad.video_id, ended=(kind == "stream"))
                item["snippet"]["title"] = "PAYLOAD_MUST_NOT_ESCAPE"
                target = item
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target[part]
                if value is None:
                    target.pop(parts[-1], None)
                else:
                    target[parts[-1]] = value
                self.items = [item, video(good.video_id)]
                events, rejected = [], []
                def record(*args, **facts):
                    try:
                        events.append(build_event(*args, **facts))
                    except Exception as error:
                        rejected.append(type(error).__name__)
                with patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE="self-hosted"), \
                        patch("telemetry.emit_event", side_effect=record), \
                        pipeline_trace.bind(trace_id="a" * 32, span_id="b" * 32, sync_run_id=201):
                    result, calls = self.process([bad, good])
                self.assertEqual(rejected, [])
                self.assertEqual(calls, ["general"])
                self.assertEqual((result["errors"], result["updated"], result["unavailable_marked"]), (1, 1, 0))
                self.assertEqual(self.rows()[bad.video_id], ("failure", {
                    "video_id": bad.video_id, "video_type": kind, "lease_token": bad.lease_token,
                    "error_type": "provider_identity_or_lifecycle_error", "retry_after_seconds": None,
                }))
                self.assertEqual(self.rows()[good.video_id][0], "metadata")
                validation = [row for row in events if row["operation"] == "video_validation"]
                self.assertEqual(len(validation), 1)
                failure = validation[0]
                self.assertEqual((failure["status"], failure["phase"], failure["reason"]), ("failed", field, reason))
                self.assertEqual((failure["video_id"], failure["channel_id"], failure["video_type"]),
                                 (bad.video_id, bad.channel_id, kind))
                self.assertEqual((failure["trace_id"], failure["sync_run_id"]), ("a" * 32, 201))
                self.assertEqual(failure["batch_id"], next(row["batch_id"] for row in events
                                 if row["operation"] == "video_batch_manifest"))
                self.assertEqual(normalize_stored_event(failure), failure)
                self.assertTrue(any(row["operation"] == "video_write" and row["status"] == "confirmed"
                                    and row["reason"] == "provider_identity_or_lifecycle_error" for row in events))
                recorded = json.dumps(events)
                for private in ("PAYLOAD_MUST_NOT_ESCAPE", bad.lease_token, '"viewCount"', '"duration"'):
                    self.assertNotIn(private, recorded)

    def test_retained_typed_validation_reports_fields_and_preserves_its_failure_codes(self):
        cases = [
            ("snippet", None, "snippet_missing_or_invalid", "snippet", "short", "metadata"),
            ("snippet.channelId", "UC" + "x" * 22, "channel_id_mismatch", "snippet.channelId", "short", "metadata"),
            ("snippet.publishedAt", None, "published_at_missing", "snippet.publishedAt", "short", "metadata"),
            ("snippet.publishedAt", "no-date", "published_at_invalid", "snippet.publishedAt", "short", "metadata"),
            ("statistics", [], "statistics_missing_or_invalid", "statistics", "short", "metadata"),
            ("contentDetails", [], "content_details_missing_or_invalid", "contentDetails", "short", "metadata"),
            ("status", [], "status_missing_or_invalid", "status", "short", "metadata"),
            ("contentDetails.duration", None, "duration_missing", "contentDetails.duration", "short", "metadata"),
            ("contentDetails.duration", "PT0.5S", "duration_invalid", "contentDetails.duration", "short", "metadata"),
            ("status.privacyStatus", "PAYLOAD_MUST_NOT_ESCAPE", "privacy_status_invalid", "status.privacyStatus", "short", "metadata"),
            ("status.uploadStatus", "", "upload_status_invalid", "status.uploadStatus", "short", "metadata"),
            ("liveStreamingDetails", [], "live_streaming_details_invalid", "liveStreamingDetails", "stream", "metadata"),
            ("liveStreamingDetails.actualEndTime", "PAYLOAD_MUST_NOT_ESCAPE", "actual_end_time_invalid", "liveStreamingDetails.actualEndTime", "stream", "metadata"),
            ("liveStreamingDetails.actualEndTime", None, "stream_completion_evidence_regressed", "liveStreamingDetails.actualEndTime", "stream", "metadata"),
            ("liveStreamingDetails.actualEndTime", "2099-01-01T00:00:00Z", "actual_end_time_future", "liveStreamingDetails.actualEndTime", "stream", "metadata"),
        ]
        self.repo.fail_typed = Mock(side_effect=lambda kind, rows: self.repo.record("typed_failure", rows))
        for path, value, reason, field, kind, mode in cases:
            with self.subTest(reason=reason, kind=kind, mode=mode):
                self.repo.writes.clear()
                bad, good = claim("bad00000001", kind, mode), claim("good0000001", kind)
                item = video(bad.video_id, ended=(kind == "stream" and mode != "stream_lifecycle"))
                item["snippet"]["title"] = "PAYLOAD_MUST_NOT_ESCAPE"
                target = item
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target[part]
                if value is None:
                    target.pop(parts[-1], None)
                else:
                    target[parts[-1]] = value
                fetch = Mock(return_value=[item, video(good.video_id, ended=(kind == "stream"))])
                self.collector.fetch_full = fetch
                events, rejected = [], []
                def record(*args, **facts):
                    try:
                        events.append(build_event(*args, **facts))
                    except Exception as error:
                        rejected.append(type(error).__name__)
                with patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE="self-hosted"), \
                        patch("telemetry.emit_event", side_effect=record), \
                        pipeline_trace.bind(trace_id="a" * 32, span_id="b" * 32, sync_run_id=201, batch_id="typed-batch"):
                    result = self.collector.process_typed_claims([bad, good], video_type=kind, reason="typed_offline")
                self.assertEqual(rejected, [])
                fetch.assert_called_once_with([bad.video_id, good.video_id], "typed_offline")
                self.assertEqual((result["errors"], result["updated"], result["unavailable_marked"]), (1, 1, 0))
                durable_code = ("typed_stream_completion_evidence_regressed"
                                if reason == "stream_completion_evidence_regressed"
                                else "typed_provider_identity_or_lifecycle_error")
                self.assertEqual(self.rows()[bad.video_id], ("typed_failure", {
                    "video_id": bad.video_id, "lease_token": bad.lease_token,
                    "error_type": durable_code, "retry_after_seconds": None,
                }))
                self.assertEqual(self.rows()[good.video_id][0], "typed")
                refusals = [row for row in events if row["operation"] == "video_validation"]
                self.assertEqual(len(refusals), 1)
                refusal = refusals[0]
                self.assertEqual((refusal["status"], refusal["phase"], refusal["reason"]), ("failed", field, reason))
                self.assertEqual((refusal["video_id"], refusal["channel_id"], refusal["video_type"]),
                                 (bad.video_id, bad.channel_id, kind))
                self.assertEqual((refusal["trace_id"], refusal["sync_run_id"], refusal["batch_id"]),
                                 ("a" * 32, 201, "typed-batch"))
                self.assertEqual(normalize_stored_event(refusal), refusal)
                self.assertNotIn("PAYLOAD_MUST_NOT_ESCAPE", json.dumps(events))
                self.assertNotIn(bad.lease_token, json.dumps(events))

    def test_retained_typed_recorder_failure_does_not_change_retry_settlement(self):
        bad, good = claim("bad00000001", "short"), claim("good0000001", "short")
        item = video(bad.video_id)
        item["contentDetails"].pop("duration")
        self.collector.fetch_full = Mock(return_value=[item, video(good.video_id)])
        self.repo.fail_typed = Mock(side_effect=lambda kind, rows: self.repo.record("typed_failure", rows))
        with patch.object(video_trace, "event", side_effect=OSError("sink unavailable")):
            result = self.collector.process_typed_claims([bad, good], video_type="short", reason="typed_offline")
        self.assertEqual((result["errors"], result["updated"], result["unavailable_marked"]), (1, 1, 0))
        self.assertFalse(result["infrastructure_error"])
        self.assertEqual(self.rows()[bad.video_id][1]["error_type"], "typed_provider_identity_or_lifecycle_error")
        self.assertIsNone(self.rows()[bad.video_id][1]["retry_after_seconds"])
        self.assertEqual(self.rows()[good.video_id][0], "typed")

    def test_unexpected_validation_exception_is_generic_and_recorder_failure_is_passive(self):
        bad, good = claim("bad00000001"), claim("good0000001")
        self.items = [video(bad.video_id), video(good.video_id)]
        validate = self.collector._validate_common_metadata
        def checked(current, item, observed):
            if current.video_id == bad.video_id:
                raise RuntimeError("PAYLOAD_MUST_NOT_ESCAPE")
            return validate(current, item, observed)
        events = []
        with patch.object(self.collector, "_validate_common_metadata", side_effect=checked), \
                patch.object(video_trace, "event", side_effect=lambda *args, **facts: events.append((args, facts))):
            result, calls = self.process([bad, good])
        refusal = [facts for args, facts in events if args == ("video_validation", "failed")]
        self.assertEqual([(row["phase"], row["reason"]) for row in refusal], [("metadata", "validation_unexpected")])
        self.assertNotIn("PAYLOAD_MUST_NOT_ESCAPE", json.dumps(events))
        writes = list(self.repo.writes)
        self.repo.writes.clear()
        with patch.object(self.collector, "_validate_common_metadata", side_effect=checked), \
                patch.object(video_trace, "event", side_effect=OSError("sink unavailable")):
            failed_recording, failed_calls = self.process([bad, good])
        self.assertEqual([row for row in self.repo.writes if row[0] == "failure"],
                         [row for row in writes if row[0] == "failure"])
        self.assertEqual([row[0] for row in self.repo.writes], [row[0] for row in writes])
        self.assertEqual(failed_calls, calls)
        for key in ("errors", "updated", "unavailable_marked", "settlement_unknown", "completion_outcome"):
            self.assertEqual(failed_recording[key], result[key])

    def test_unclassified_validation_code_never_enters_journal(self):
        events = []
        with patch.object(video_trace, "event", side_effect=lambda *args, **facts: events.append(facts)):
            for value in ("PAYLOAD_MUST_NOT_ESCAPE", {"raw": "PAYLOAD_MUST_NOT_ESCAPE"}):
                video_trace.metadata_validation_failed(claim(), value)
        self.assertEqual([row["reason"] for row in events], ["validation_unexpected"] * 2)
        self.assertNotIn("PAYLOAD_MUST_NOT_ESCAPE", json.dumps(events))

    def test_missing_general_id_keeps_confirmed_unavailable_semantics(self):
        result, calls = self.process([claim("absent")])
        self.assertEqual(calls, ["general"])
        self.assertEqual(self.rows()["absent"][0], "missing")
        self.assertEqual(result["missing"], 1)

    def test_general_quota_refusal_does_not_spend_stats_or_mark_unavailable(self):
        self.general_error = YouTubeDailyQuotaExceededError()
        result, calls = self.process([claim("metadata"), claim("stats", mode="counters")])
        self.assertEqual(calls, ["general"])
        self.assertTrue(result["general_quota_exhausted"])
        self.assertEqual(result["unavailable_marked"], 0)
        self.assertEqual({row["error_type"] for _, row in self.rows().values()}, {"youtube_quota_exhausted"})

    def test_fallback_refusal_preserves_healthy_general_result_and_original_tokens(self):
        for error in (YouTubeDailyQuotaExceededError(), YouTubeRateLimitError(), RuntimeError("offline")):
            with self.subTest(error=type(error).__name__):
                self.repo.writes.clear()
                self.items = [video("healthy"), video("fallback")]
                self.items[1].pop("statistics")
                self.stats_error = error
                result, calls = self.process([claim("healthy"), claim("fallback")])
                self.assertEqual(calls, ["general", "stats"])
                rows = self.rows()
                self.assertEqual((rows["healthy"][0], rows["fallback"][0]), ("metadata", "identity"))
                self.assertEqual(rows["healthy"][1]["recorded_at"], GENERAL.isoformat())
                self.assertEqual(rows["fallback"][1]["lease_token"], "token-fallback")
                self.assertEqual(rows["fallback"][1]["recorded_at"], GENERAL.isoformat())
                self.assertTrue(rows["fallback"][1]["counterless_profile"])
                self.assertFalse(rows["fallback"][1]["missing"])
                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["metadata_deferred"], 1)
                self.assertEqual(sum(len(values) for _, values in self.repo.writes), 2)

    def test_counterless_general_for_each_type_restores_identity_without_inventing_counters(self):
        claims=[claim(kind,kind) for kind in ('long','short','stream')]
        self.items=[video(row.video_id,ended=row.video_type=='stream') for row in claims]
        for item in self.items:item['statistics']={}
        self.stats.side_effect=lambda ids,**kw:dict(items=[],summary=dict(failed_ids=ids))
        result,calls=self.process(claims)
        self.assertEqual(calls,['general','stats'])
        self.assertEqual((result['updated'],result['metadata_deferred'],result['unavailable_marked']),(0,3,0))
        self.assertEqual(result['video_ids'],[])
        for kind,(operation,row) in self.rows().items():
            self.assertEqual(operation,'identity')
            self.assertTrue(row['counterless_profile'])
            self.assertEqual(row['video_type'],kind)
            self.assertEqual(row['recorded_at'],GENERAL.isoformat())
            self.assertFalse(row['missing'])
            self.assertNotIn('views',row)

    def test_unknown_general_ack_stops_before_fallback_without_replaying_success(self):
        self.items = [video("healthy"), video("fallback")]
        self.items[1]["statistics"] = {}
        self.repo.fail_commit = True
        result, calls = self.process([claim("healthy"), claim("fallback")])
        self.assertEqual(calls, ["general"])
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(self.repo.writes, [])

    def test_explicit_complete_profile_flag_keeps_transport_boundary_strict(self):
        complete = "snippet,contentDetails,status,topicDetails,liveStreamingDetails,statistics"
        _validate_video_request_boundary("videos", "general", {"part": complete}, complete_profile=True)
        for endpoint, bucket, parts in (("videos", "general", "statistics"),
                                       ("videos", "video_batch_get_stats", complete),
                                       ("videos:batchGetStats", "general", complete)):
            with self.subTest(endpoint=endpoint, bucket=bucket, parts=parts), self.assertRaises(ValueError):
                _validate_video_request_boundary(endpoint, bucket, {"part": parts}, complete_profile=True)
        with self.assertRaises(TypeError):
            _validate_video_request_boundary("videos", "general", {"part": complete}, complete_profile=1)

    def test_actual_operator_worker_regular_callback_requests_complete_general_once(self):
        core = SimpleNamespace(database=object(), process_database_resources=Mock(),
            API_KEY="offline", YOUTUBE_QUOTA_ATTEMPT_RECORDER=object(),
            YOUTUBE_API_TIMEOUT_SECONDS=17, COLLECTION_SCHEDULES=object())
        spec = importlib.util.spec_from_file_location("quota03_worker_composition",
            ROOT / "application/scripts/run_operator_job_worker.py")
        worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"core": core}):
            spec.loader.exec_module(worker)
        self.items = [video("abcdefghijk")]
        repo = self.repo
        repo.reconcile_typed = Mock(return_value={"affected_count": 0})
        repo.claim_due_batch = Mock(return_value={"rows": [claim()], "inspected_count": 1,
                                                   "paused_count": 0, "scan_exhausted": True})
        with patch.object(runtime.video_execution, "VideoCollectionRepository", return_value=repo), \
             patch.object(worker, "youtube_api_get", self.transport), \
             patch.object(worker, "fetch_video_batch_stats", self.stats), patch.object(worker, "emit_event"):
            callbacks = worker._collection_callbacks(repo.database, CollectionSettings(True, True, True))
            result = callbacks["run_stats"](limit=1, common_preflight_checked=True)
        self.assertEqual((result["updated"], result["metadata_requests"], result["batch_requests"]), (1, 1, 0))
        self.transport.assert_called_once()
        self.stats.assert_not_called()
        self.assertEqual(self.transport.call_args.kwargs["quota_bucket"], "general")


if __name__ == "__main__":
    unittest.main()

