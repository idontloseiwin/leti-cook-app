"""Evidence-only explanations; no scheduling, provider calls or recovery claims."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))

from collection_outcome import collection_explanation, sync_run_explanation


class CollectionExplanationTests(unittest.TestCase):
    def codes(self, value):
        return {item["code"]: item["count"] for item in collection_explanation(value)["reasons"]}

    def test_recorded_stats_followup_is_not_unavailability_or_error(self):
        original = {"completion_outcome": "partial", "incomplete": True,
                    "unresolved": 7, "metadata_deferred": 7,
                    "source_view_count_absent": 0, "errors": 0}
        before = deepcopy(original)
        value = collection_explanation(original)
        self.assertEqual(value["reasons"], [{"code": "metadata_followup", "count": 7}])
        self.assertEqual(value["unresolved"], 7)
        self.assertEqual(value["current_recovery"], "not_evaluated")
        self.assertEqual(value["next_attempt"], "stored_queue")
        self.assertEqual(value["scope"], "recorded_run")
        self.assertEqual(original, before)

    def test_counterless_profiles_and_stats_followup_remain_distinct(self):
        self.assertEqual(self.codes({"metadata_deferred": 5, "source_view_count_absent": 2}),
                         {"counters_pending": 2, "metadata_followup": 3})

    def test_old_or_inconsistent_fields_never_guess_followup_cause(self):
        self.assertEqual(self.codes({"metadata_deferred": 5}),
                         {"metadata_deferred_unspecified": 5})
        self.assertEqual(self.codes({"metadata_deferred": 2, "source_view_count_absent": 5}),
                         {"counters_pending": 5, "metadata_deferred_unspecified": 2})

    def test_publication_waiting_is_handled_not_partial_and_stream_subset_not_added(self):
        value = collection_explanation({"completion_outcome": "completed", "incomplete": False,
            "unresolved": 0, "publication_waiting": 3, "stream_lifecycle_probed": 2,
            "unavailable_marked": 4, "errors": 0})
        self.assertEqual(value["reasons"], [])
        self.assertEqual(value["handled"], {"publication_waiting": 3, "unavailable_marked": 4})
        self.assertEqual(collection_explanation({"stream_lifecycle_probed": 2})["handled"]["publication_waiting"], 2)

    def test_missing_general_ids_are_not_reported_as_transient_transport_failure(self):
        value = collection_explanation({"completion_outcome": "completed", "missing": 2,
                                        "unavailable_marked": 2, "errors": 0})
        self.assertEqual(value["reasons"], [])
        self.assertEqual(value["handled"]["unavailable_marked"], 2)
        self.assertEqual(value["current_recovery"], "not_evaluated")

    def test_recorded_stops_keep_distinct_causes_and_no_deadline(self):
        cases = (("settlement_unknown", "settlement_unknown"),
                 ("completion_unknown", "settlement_unknown"),
                 ("infrastructure_error", "infrastructure_error"),
                 ("quota_exhausted", "quota_wait"),
                 ("general_quota_exhausted", "quota_wait"),
                 ("batch_quota_exhausted", "quota_wait"),
                 ("daily_quota_exhausted", "quota_wait"),
                 ("rate_limited", "provider_rate_limited"),
                 ("batch_unavailable", "batch_unavailable"),
                 ("yield_requested", "scheduled_handoff"))
        for field, code in cases:
            with self.subTest(field=field):
                self.assertEqual(self.codes({field: True}), {code: None})
                self.assertEqual(self.codes({field: "true"}), {})
        self.assertEqual(self.codes({"quota_exhausted": True, "general_quota_exhausted": True}), {"quota_wait": None})

    def test_unknown_settlement_never_certifies_queued_deferral(self):
        value = collection_explanation({"metadata_deferred": 1, "source_view_count_absent": 0,
                                        "settlement_unknown": True, "errors": 1})
        self.assertEqual([item["code"] for item in value["reasons"]],
                         ["metadata_followup", "settlement_unknown", "recorded_errors"])
        self.assertEqual(value["current_recovery"], "not_evaluated")

    def test_validation_observations_do_not_claim_they_explain_all_errors(self):
        self.assertEqual(self.codes({"source_view_count_invalid": 2,
            "metadata_duplicate_items": 1, "errors": 4}),
            {"source_counter_invalid": 2, "source_duplicate_items": 1, "recorded_errors": 4})

    def test_legacy_and_missing_fields_keep_unknown_coverage(self):
        value = collection_explanation({})
        self.assertIsNone(value["completion_outcome"])
        self.assertIsNone(value["incomplete"])
        self.assertIsNone(value["unresolved"])
        self.assertEqual(value["reasons"], [])
        self.assertEqual(self.codes({"completion_outcome": "partial", "incomplete": True}),
                         {"cause_not_recorded": None})

    def test_only_selected_unclaimed_work_is_named_not_global_backlog(self):
        self.assertEqual(self.codes({"explicit_unclaimed": 3}), {"selected_not_claimed": 3})
        self.assertEqual(self.codes({"due_count": 100, "stats_limit": 50, "requested": 50}), {})

    def test_invalid_values_and_payloads_are_not_echoed(self):
        value = {"completion_outcome": {}, "incomplete": "true", "unresolved": False,
                 "metadata_deferred": -1, "source_view_count_absent": float("inf"),
                 "errors": "secret", "error_messages": ["secret"], "stopped_reason": "secret",
                 "metadata_duplicate_items": 1.0, "video_ids": ["secret"]}
        output = collection_explanation(value)
        self.assertEqual(output["reasons"], [])
        self.assertNotIn("secret", repr(output))
        self.assertIsNone(output["unresolved"])
        self.assertEqual(collection_explanation(None), collection_explanation({}))


class SyncRunExplanationTests(unittest.TestCase):
    def test_common_pool_uses_all_types_without_duplicate_long_projection(self):
        row = {"status": "partial", "errors": 0, "summary_json": {
            "completion_outcome": "partial", "incomplete": True,
            "video_collection": {"metadata_deferred": 3, "source_view_count_absent": 0,
                                 "unresolved": 3, "incomplete": True},
            "regular_video_stats": {"metadata_deferred": 3, "source_view_count_absent": 0},
        }}
        before = deepcopy(row)
        result = sync_run_explanation(row)
        self.assertEqual(result["reasons"], [{"code": "metadata_followup", "count": 3, "stage": "video_collection"}])
        self.assertEqual(set(result["stages"]), {"video_collection"})
        self.assertEqual(row, before)

    def test_inventory_does_not_repeat_legacy_discovery_projection(self):
        row = {"status": "error", "summary_json": {
            "typed_inventory": {"errors": 1}, "channel_discovery": {"errors": 1,
                "routing": {"mode": "shared_inventory_long_projection"}},
        }}
        result = sync_run_explanation(row)
        self.assertEqual(result["reasons"], [{"code": "recorded_errors", "count": 1, "stage": "typed_inventory"}])

    def test_legacy_discovery_is_not_discarded_without_shared_routing_proof(self):
        result = sync_run_explanation({"status": "error", "summary_json": {
            "typed_inventory": {"errors": 0}, "channel_discovery": {"errors": 1},
        }})
        self.assertEqual(result["reasons"], [{"code": "recorded_errors", "count": 1, "stage": "channel_discovery"}])

    def test_legacy_typed_sections_are_visible_without_common_pool(self):
        result = sync_run_explanation({"status": "partial", "summary_json": {
            "short_video_collection": {"metadata_deferred": 1},
            "completed_stream_collection": {"publication_waiting": 2},
        }})
        self.assertEqual(result["reasons"], [{"code": "metadata_deferred_unspecified", "count": 1, "stage": "short_video_collection"}])
        self.assertEqual(result["stages"]["completed_stream_collection"]["handled"]["publication_waiting"], 2)

    def test_historical_status_is_not_rewritten_or_recovered_by_other_data(self):
        for status in ("partial", "error"):
            row = {"status": status, "errors": 0, "summary_json": {"video_collection": {"updated": 100}},
                   "later_run": {"status": "completed", "errors": 0}}
            result = sync_run_explanation(row)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["current_recovery"], "not_evaluated")
            self.assertEqual(result["reasons"], [{"code": "cause_not_recorded", "count": None, "stage": "run"}])

    def test_error_column_survives_sparse_summary_but_does_not_identify_transport(self):
        result = sync_run_explanation({"status": "error", "errors": 1, "summary_json": {}})
        self.assertEqual(result["reasons"], [{"code": "recorded_errors", "count": 1, "stage": "run"}])

    def test_unrelated_dynamic_sections_and_raw_errors_are_ignored(self):
        row = {"status": [], "summary_json": {"untrusted": {"settlement_unknown": True},
                                              "error_messages": ["secret"]}}
        result = sync_run_explanation(row)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["stages"], {})
        self.assertNotIn("secret", repr(result))
        self.assertIsNone(result["status"])


if __name__ == "__main__":
    unittest.main()
