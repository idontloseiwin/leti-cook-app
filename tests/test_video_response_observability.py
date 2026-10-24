"""Real provider-wrapper evidence without network, database or provider payload logs."""
from copy import deepcopy
from pathlib import Path
from threading import Event
import json
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
import pipeline_trace as trace
import collection_shutdown
import youtube_batch_stats as batch
from youtube_response_trace import provider_response_observed
from youtube_errors import YouTubeAPIError
from youtube_video_profiles import fetch_initial_video_profiles, fetch_video_metadata
from test_common_video_dispatch import Repository
from test_initial_video_parallel import service, video

FIRST, SECOND, FOREIGN = "abcdefghijk", "lmnopqrstuv", "unwanted123"
SECRET = "not-a-real-private-provider-secret"


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
        self.headers = {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return deepcopy(self.payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("synthetic HTTP refusal")


class VideoResponseObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        def emit(kind, source, target, stage, **facts):
            self.events.append(dict(stage=stage, **facts))
        sink = patch.object(trace.telemetry, "emit_event", side_effect=emit)
        sink.start()
        self.addCleanup(sink.stop)
        stop = patch.object(collection_shutdown, "_requested", Event())
        stop.start()
        self.addCleanup(stop.stop)

    def observed(self, stage=None):
        return [item for item in self.events
                if item["stage"].startswith("video_provider_")
                and (stage is None or item["stage"] == stage)]

    def items(self):
        return self.observed("video_provider_item")

    def manifests(self):
        return self.observed("video_provider_manifest")

    def fetch(self, kind, payload, ids=(FIRST, SECOND), *, status=200,
              statistics=True, transport=None, reject_identity_anomalies=True):
        transport = transport or Mock(return_value=Response(payload, status))
        self.transport = transport
        dependencies = dict(youtube_api_get=transport, api_key=SECRET,
                            attempt_recorder=None, timeout_seconds=3,
                            emit_event=Mock(), perf_counter=lambda: 0)
        if kind == "metadata":
            return fetch_video_metadata(ids, include_statistics=statistics,
                                       reject_identity_anomalies=reject_identity_anomalies,
                                       **dependencies)
        if kind == "initial":
            return fetch_initial_video_profiles(ids, **dependencies)
        with patch.object(batch, "youtube_api_get", transport):
            return batch.fetch_video_batch_stats(ids, api_key=SECRET, attempt_recorder=None,
                parts=("id", "statistics") if statistics else ("id", "snippet"))

    def test_valid_general_and_initial_omissions_are_observations_not_deletion(self):
        for kind in ("metadata", "initial"):
            with self.subTest(kind=kind):
                self.events.clear()
                payload = {"items": [{"id": FIRST, "statistics": {"viewCount": "0"}}]}
                original = deepcopy(payload)
                result = self.fetch(kind, payload)
                self.assertEqual(result, original["items"])
                self.assertEqual(payload, original)
                self.assertEqual([(e["video_id"], e["reason"], e["status"])
                                  for e in self.items()], [(SECOND, "id_omitted", "observed")])
                manifest, = self.manifests()
                self.assertEqual((manifest["video_ids"], manifest["requested_count"],
                                  manifest["received_count"], manifest["missing_count"]),
                                 ([FIRST], 2, 1, 1))
                self.assertEqual(manifest["endpoint"], "videos")
                self.assertEqual(self.transport.call_count, 1)
                self.assertFalse(any(e["stage"] == "video_write" for e in self.events))

    def test_batch_distinguishes_explicit_failed_id_from_inferred_omission(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.events.clear()
                payload = {"items": [{"id": FIRST, "statistics": {"viewCount": "0"}}]}
                if explicit:
                    payload["summary"] = {"requestedVideoCount": 2, "succeededVideoCount": 1,
                                          "failedVideoCount": 1, "failedVideoIds": [SECOND]}
                result = self.fetch("batch", payload)
                self.assertEqual(result["summary"]["failed_ids"], [SECOND])
                self.assertEqual(result["summary"]["source"], "api" if explicit else "derived_from_items")
                self.assertEqual(result["items"][0]["views"], 0)
                item, = self.items()
                self.assertEqual(item["reason"], "explicit_failed_id" if explicit else "id_omitted")
                self.assertEqual(item["endpoint"], "videos:batchGetStats")
                self.assertEqual(self.transport.call_count, 1)

    def test_batch_normalized_explicit_failed_identity_is_still_explicit(self):
        payload = {"items": [], "summary": {"requestedVideoCount": 1,
            "succeededVideoCount": 0, "failedVideoCount": 1, "failedVideoIds": [" " + FIRST + " "]}}
        result = self.fetch("batch", payload, ids=(FIRST,))
        self.assertEqual(result["summary"]["failed_ids"], [FIRST])
        self.assertEqual([e["reason"] for e in self.items()], ["explicit_failed_id"])

    def test_batch_returned_identity_observation_matches_existing_normalization(self):
        for identity, returned in ((FIRST, " " + FIRST + " "), ("12345678901", 12345678901)):
            with self.subTest(identity=identity):
                self.events.clear()
                result = self.fetch("batch", {"items": [{"id": returned,
                    "statistics": {"viewCount": "0"}}]}, ids=(identity,))
                self.assertEqual(result["items"][0]["id"], identity)
                self.assertEqual(self.manifests()[0]["video_ids"], [identity])
                self.assertEqual(self.manifests()[0]["status"], "received")
                self.assertEqual(self.items(), [])

    def test_batch_omitted_empty_items_uses_accepted_batch_contract(self):
        result = self.fetch("batch", {})
        self.assertEqual(result["summary"]["failed_ids"], [FIRST, SECOND])
        self.assertEqual([e["reason"] for e in self.items()], ["id_omitted", "id_omitted"])

    def test_missing_invalid_and_zero_counters_remain_distinct(self):
        values = [(None, "view_count_missing"), ("", "view_count_missing"),
                  ("nonnumeric-private-value", "view_count_invalid"), (-1, "view_count_invalid"),
                  (True, "view_count_invalid"), (1.25, "view_count_invalid"),
                  (str(2**63), "view_count_invalid"), (0, None), ("0", None)]
        for kind in ("metadata", "initial", "batch"):
            for value, expected in values:
                with self.subTest(kind=kind, value=value):
                    self.events.clear()
                    self.fetch(kind, {"items": [{"id": FIRST, "statistics": {"viewCount": value}}]},
                               ids=(FIRST,))
                    self.assertEqual([e["reason"] for e in self.items()], [expected] if expected else [])
                    if expected:
                        self.assertEqual(self.items()[0]["phase"], "statistics.viewCount")
                    self.assertEqual(self.manifests()[0]["missing_count"], 0)
                    self.assertNotIn("nonnumeric-private-value", json.dumps(self.observed()))

    def test_absent_statistics_and_view_count_are_observed_without_values(self):
        for item in ({"id": FIRST}, {"id": FIRST, "statistics": {}},
                     {"id": FIRST, "statistics": None}):
            for kind in ("metadata", "initial", "batch"):
                with self.subTest(kind=kind, item=item):
                    self.events.clear()
                    self.fetch(kind, {"items": [item]}, ids=(FIRST,))
                    self.assertEqual([e["reason"] for e in self.items()], ["view_count_missing"])

    def test_invalid_statistics_shape_survives_batch_normalization_refusal(self):
        for kind in ("metadata", "initial", "batch"):
            with self.subTest(kind=kind):
                self.events.clear()
                payload = {"items": [{"id": FIRST, "statistics": "private-bad-section"}]}
                if kind == "batch":
                    with self.assertRaises(AttributeError):
                        self.fetch(kind, payload)
                    self.assertEqual(self.manifests()[0]["status"], "invalid")
                else:
                    self.fetch(kind, payload)
                self.assertEqual(self.items()[0]["reason"], "statistics_invalid")
                self.assertEqual(self.items()[0]["phase"], "statistics")
                self.assertNotIn("private-bad-section", json.dumps(self.observed()))

    def test_metadata_only_and_batch_without_statistics_do_not_report_counter_absence(self):
        for kind in ("metadata", "batch"):
            with self.subTest(kind=kind):
                self.events.clear()
                result = self.fetch(kind, {"items": [{"id": FIRST}]}, ids=(FIRST,), statistics=False)
                self.assertEqual(self.items(), [])
                self.assertEqual(self.manifests()[0]["status"], "received")
                self.assertNotIn("statistics", self.transport.call_args.kwargs["params"]["part"])
                if kind == "metadata":
                    self.assertEqual(result, [{"id": FIRST}])

    def test_general_identity_anomalies_never_turn_an_absent_id_into_trusted_omission(self):
        for items in ([{"id": FIRST}, {"id": FOREIGN}], [{"id": FIRST}, {"id": FIRST}],
                      [{"id": FIRST}, {"id": None}], [{"id": FIRST}, "bad-item"]):
            for kind in ("metadata", "initial"):
                with self.subTest(kind=kind, items=items):
                    self.events.clear()
                    with self.assertRaises(YouTubeAPIError):
                        self.fetch(kind, {"items": items})
                    self.assertEqual(self.manifests()[0]["status"], "invalid")
                    missing = next(e for e in self.items() if e["video_id"] == SECOND)
                    self.assertEqual(missing["reason"], "omission_untrusted")
                    self.assertNotIn(FOREIGN, json.dumps(self.observed()))
                    self.assertFalse(any(e["reason"] == "id_omitted" for e in self.items()))

    def test_common_general_tolerates_identity_anomalies_without_trusting_manifest(self):
        payload = {"items": [{"id": FIRST}, {"id": FOREIGN}]}
        result = self.fetch("metadata", payload, reject_identity_anomalies=False)
        self.assertEqual(result, payload["items"])
        self.assertEqual(self.manifests()[0]["status"], "invalid")
        self.assertEqual(next(e for e in self.items() if e["video_id"] == SECOND)["reason"],
                         "omission_untrusted")

    def test_batch_invalid_summary_or_identity_never_reports_trusted_omission(self):
        cases = [
            {"items": [{"id": FIRST}], "summary": {"requestedVideoCount": 2,
                "succeededVideoCount": 1, "failedVideoCount": 0, "failedVideoIds": [SECOND]}},
            {"items": [{"id": FIRST}], "summary": []},
            {"items": [{"id": FIRST}], "summary": {"failedVideoIds": "private-secret"}},
            {"items": [{"id": FIRST}], "summary": {"requestedVideoCount": 2,
                "succeededVideoCount": 1, "failedVideoCount": 1, "failedVideoIds": [FOREIGN]}},
            {"items": [{"id": FIRST}], "summary": {"requestedVideoCount": 2,
                "succeededVideoCount": 1, "failedVideoCount": 1, "failedVideoIds": [FIRST]}},
            {"items": [{"id": FIRST}], "summary": {"requestedVideoCount": 2,
                "succeededVideoCount": 1, "failedVideoCount": 0, "failedVideoIds": []}},
            {"items": [{"id": FIRST}, {"id": FOREIGN}]},
            {"items": [{"id": FIRST}, {"id": FIRST}]},
            {"items": [{"id": FIRST}, 7]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.events.clear()
                with self.assertRaises(batch.YouTubeBatchStatsError):
                    self.fetch("batch", payload)
                self.assertEqual(self.manifests()[0]["status"], "invalid")
                self.assertEqual(next(e for e in self.items() if e["video_id"] == SECOND)["reason"],
                                 "omission_untrusted")
                self.assertNotIn(FOREIGN, json.dumps(self.observed()))

    def test_nonlist_or_absent_general_items_never_produce_per_video_omissions(self):
        for kind in ("metadata", "initial", "batch"):
            for payload in ({"items": {}}, {"items": "private-items"}, []):
                with self.subTest(kind=kind, payload=payload):
                    self.events.clear()
                    with self.assertRaises((YouTubeAPIError, batch.YouTubeBatchStatsError)):
                        self.fetch(kind, payload)
                    self.assertEqual(self.items(), [])
                    self.assertTrue(all(e["status"] == "invalid" for e in self.manifests()))
        for kind in ("metadata", "initial"):
            self.events.clear()
            with self.assertRaises(YouTubeAPIError):
                self.fetch(kind, {})
            self.assertEqual(self.items(), [])

    def test_http_api_json_and_transport_failures_have_no_omission_evidence(self):
        for kind in ("metadata", "initial", "batch"):
            for status, payload in ((500, {"items": []}), (403, {"error": {"message": SECRET}}),
                                    (200, {"error": {"message": SECRET}}), (200, ValueError(SECRET))):
                with self.subTest(kind=kind, status=status, payload=payload):
                    self.events.clear()
                    with self.assertRaises(Exception):
                        self.fetch(kind, payload, status=status)
                    self.assertEqual(self.items(), [])
                    self.assertFalse(any(e["status"] == "received" for e in self.manifests()))
                    self.assertNotIn(SECRET, json.dumps(self.observed()))
            self.events.clear()
            error = TimeoutError("synthetic transport error")
            with self.assertRaises(TimeoutError) as caught:
                self.fetch(kind, None, transport=Mock(side_effect=error))
            self.assertIs(caught.exception, error)
            self.assertEqual(self.observed(), [])
            self.assertEqual(self.transport.call_count, 1)

    def test_sink_failure_preserves_wrapper_results_and_normalization_exceptions(self):
        for kind in ("metadata", "initial", "batch"):
            with self.subTest(kind=kind):
                payload = {"items": [{"id": FIRST, "statistics": {"viewCount": "12"}}]}
                expected = self.fetch(kind, payload)
                with patch.object(trace.telemetry, "emit_event", side_effect=OSError(SECRET)):
                    self.assertEqual(self.fetch(kind, payload), expected)
                    with self.assertRaises((YouTubeAPIError, batch.YouTubeBatchStatsError)):
                        self.fetch(kind, {"items": [{"id": FOREIGN}]})
        error = TimeoutError(SECRET)
        with patch.object(trace.telemetry, "emit_event", side_effect=OSError(SECRET)):
            with self.assertRaises(TimeoutError) as caught:
                self.fetch("batch", None, transport=Mock(side_effect=error))
        self.assertIs(caught.exception, error)

    def test_observations_redact_source_values_foreign_ids_and_credentials(self):
        payload = {"items": [{"id": FIRST, "snippet": {"title": "private-source-title"},
            "statistics": {"viewCount": "987654321234", "likeCount": "9988776655"},
            "description": SECRET}, {"id": FOREIGN, "statistics": {"viewCount": SECRET}}]}
        self.fetch("metadata", payload, reject_identity_anomalies=False)
        encoded = json.dumps(self.observed())
        for forbidden in ("private-source-title", "987654321234", "9988776655", SECRET, FOREIGN):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(self.manifests()[0]["video_ids"], [FIRST])

    def test_mixed_batch_observations_do_not_inherit_another_videos_identity(self):
        with trace.bind(channel_id="UC" + "c" * 22, video_type="long", video_id=FIRST,
                        video_ids=[FIRST, SECOND], batch_id="batch-mixed", sync_run_id=73):
            self.fetch("metadata", {"items": [{"id": FIRST, "statistics": {"viewCount": "1"}}]})
        manifest, = self.manifests()
        item, = self.items()
        self.assertEqual(item["video_id"], SECOND)
        self.assertIsNone(item.get("video_ids"))
        self.assertIsNone(manifest.get("video_id"))
        for event in (manifest, item):
            self.assertIsNone(event.get("channel_id"))
            self.assertIsNone(event.get("video_type"))
            self.assertEqual((event["batch_id"], event["sync_run_id"]), ("batch-mixed", 73))

    def test_response_manifests_are_bounded_per_real_fifty_id_request(self):
        identities = [f"v{index:010d}" for index in range(101)]
        def request(endpoint, **kwargs):
            return Response({"items": [{"id": value, "statistics": {"viewCount": "1"}}
                for value in kwargs["params"]["id"].split(",")]})
        result = self.fetch("metadata", None, ids=identities, transport=Mock(side_effect=request))
        self.assertEqual(len(result), 101)
        self.assertEqual([e["requested_count"] for e in self.manifests()], [50, 50, 1])
        self.assertEqual([len(e["video_ids"]) for e in self.manifests()], [50, 50, 1])
        self.assertEqual(self.transport.call_count, 3)
        for kind in ("initial", "batch"):
            self.events.clear()
            with self.assertRaises(ValueError):
                self.fetch(kind, {}, ids=identities[:51])
            self.transport.assert_not_called()
            self.assertEqual(self.observed(), [])

    def test_oversized_or_unsafe_direct_observation_cannot_expand_the_journal(self):
        for identities in ([f"v{index:010d}" for index in range(51)], [SECRET], [FIRST, None]):
            provider_response_observed(identities, {"items": []}, endpoint="videos")
        self.assertEqual(self.observed(), [])
        provider_response_observed([FIRST], {"items": [{"id": FIRST}] * 51}, endpoint="videos")
        self.assertEqual(self.items(), [])
        self.assertEqual(self.manifests()[0]["reason"], "items_invalid")

    def test_maximum_missing_response_emits_one_manifest_and_fifty_item_observations(self):
        identities = [f"v{index:010d}" for index in range(50)]
        self.fetch("batch", {"items": []}, ids=identities)
        self.assertEqual(len(self.manifests()), 1)
        self.assertEqual(len(self.items()), 50)
        self.assertEqual([event["video_id"] for event in self.items()], identities)
        self.assertTrue(all(event["reason"] == "id_omitted" for event in self.items()))

    def test_service_omission_then_reappearance_has_separate_manifests_and_write_receipts(self):
        repo = Repository()
        collector = service(repo, Mock())
        responses = iter(({"items": []}, {"items": [video(FIRST)]}))
        collector.fetch_metadata = lambda ids, reason: self.fetch("metadata", next(responses), ids=ids)
        with trace.scope("auto_sync", sync_run_id=41):
            missing = collector.process_common_claims(repo.claim()["rows"])
        with trace.scope("auto_sync", sync_run_id=42):
            present = collector.process_common_claims(repo.claim()["rows"])
        self.assertEqual((missing["unavailable_marked"], present["updated"]), (1, 1))
        self.assertEqual([operation for operation, rows in repo.writes], ["missing", "metadata"])
        manifests = self.manifests()
        self.assertEqual([(e["sync_run_id"], e["video_ids"]) for e in manifests], [(41, []), (42, [FIRST])])
        writes = [e for e in self.events if e["stage"] == "video_write" and e["status"] == "confirmed"]
        self.assertEqual([e["reason"] for e in writes], ["unavailable", "complete_metadata"])
        for manifest, receipt in zip(manifests, writes):
            self.assertEqual(manifest["batch_id"], receipt["batch_id"])
            self.assertLess(self.events.index(manifest), self.events.index(receipt))
        self.assertNotIn("restored", {e["status"] for e in self.events})
        self.assertNotIn("original-token", json.dumps(self.events))

    def test_service_positive_response_with_unknown_commit_is_not_restoration(self):
        repo = Repository()
        repo.fail_commit = True
        collector = service(repo, Mock())
        collector.fetch_metadata = lambda ids, reason: self.fetch("metadata", {"items": [video(FIRST)]}, ids=ids)
        result = collector.process_common_claims(repo.claim()["rows"])
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(self.manifests()[0]["video_ids"], [FIRST])
        self.assertEqual(repo.writes, [])
        writes = [e for e in self.events if e["stage"] == "video_write"]
        self.assertEqual([e["status"] for e in writes], ["started", "unknown"])
        self.assertFalse(any(e["status"] in ("restored", "confirmed") for e in writes))


if __name__ == "__main__":
    unittest.main()
