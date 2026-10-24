"""Full initial admission/progress contracts; all provider and DB I/O is fake."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from channel_inventory_estimate import initial_inventory_count_bounds, long_inventory_estimate_values
from channels.sources import estimate_initial_inventory
from collection import commands, hydration
from db.jobs.operator_jobs import OperatorJobRepository
from operator_jobs import load_operator_job_settings, operator_job_snapshot
from youtube_errors import YouTubePlaylistNotFoundError

CHANNEL = "UC" + "a" * 22
STAMP = datetime.now(timezone.utc).isoformat()
KINDS = ["long", "short", "stream"]
SCHEMA = dict(ready=True, schema_version=9, initial_quota_limit_enforced=False,
              initial_inventory_scan_bridge=True, initial_chunk_pages=1,
              full_initial_scope_version=1, initial_total_limits_enforced=False)


def observed(long=5, short=3, stream=2):
    counts = dict(zip(KINDS, (long, short, stream)))
    return dict(scope="all", video_types=KINDS, counts=counts,
                statuses={kind: "ok" if value is not None else "error" for kind, value in counts.items()})


def full_job(**changes):
    row = dict(id="11111111-1111-4111-8111-111111111111", channel_id=CHANNEL,
               job_type="channel_initial_long_inventory", initial_video_types=KINDS,
               initial_scope_version=1, requested_at=STAMP, status="queued", phase="inventory",
               accepted_epoch=7, target_count=4, items_processed=7, pages_processed=1,
               max_items=None, max_pages=None, max_quota_units=None, targets_frozen_at=None,
               summary_json=dict(estimated_items=10, estimated_pages=3, items_lower_bound=5,
                                 estimated_quota_units=5), quota_units_estimated=5)
    row.update(changes)
    return row


class FullInitialEstimateTests(unittest.TestCase):
    def test_overlapping_playlists_do_not_claim_an_exact_distinct_total(self):
        for counts, lower, upper in (((6000, 6000, 0), 6000, 12000), ((9999, 1, 0), 9999, 10000),
                                      ((10001, 0, 0), 10001, 10001)):
            with self.subTest(counts=counts):
                result = hydration.normalized_initial_hydration_estimate(observed(*counts))
                self.assertEqual(result["estimate_kind"], "upper_bound")
                self.assertEqual((result["items_lower_bound"], result["items_upper_bound"]), (lower, upper))

    def test_initial_estimate_charges_one_general_request_per_fifty_profiles(self):
        for count, quota in ((0, 3), (50, 4), (51, 6), (400, 18), (20000, 802)):
            with self.subTest(count=count):
                value = hydration.normalized_initial_hydration_estimate(observed(count, 0, 0))
                self.assertEqual(value["quota_units"], quota)

    def test_legacy_retry_keeps_split_cost_and_accepted_history(self):
        job = operator_job_snapshot(full_job(initial_scope_version=0, phase="hydrate_videos",
            targets_frozen_at=STAMP, target_count=100, processed_count=0,
            quota_units_estimated=7, quota_units_reserved=2, max_items=100, max_pages=3, max_quota_units=20),
            channel_id=CHANNEL)
        estimate = dict(items=100, pages=3, quota_units=7, items_lower_bound=0,
            items_upper_bound=100, estimate_kind="upper_bound", scope="all", basis="accepted_job")
        result = hydration.project_initial_hydration_retry_estimate(estimate, job)
        self.assertEqual(result["quota_units"], 7)
        self.assertEqual(job["quota_units_estimated"], 7)

    def test_unknown_count_is_not_zero_or_a_fake_quota_estimate(self):
        value = observed(9, None, 1)
        result = hydration.normalized_initial_hydration_estimate(value)
        self.assertEqual([result[key] for key in ("items", "pages", "quota_units", "items_upper_bound")], [None] * 4)
        self.assertEqual(result["items_lower_bound"], 9)
        self.assertEqual(result["estimate_kind"], "unknown")

    def test_missing_playlist_and_legacy_zero_both_count_as_empty(self):
        # A channel without this list has nothing in it to load; only a failed probe is unknown.
        for count in (None, 0):
            value = observed(count, 7, 1)
            value["statuses"]["long"] = "playlist_not_found"
            bounds = initial_inventory_count_bounds(value)
            self.assertEqual((bounds["estimate_kind"], bounds["items_upper_bound"]), ("upper_bound", 8))
        failed = observed(None, 7, 1)
        failed["statuses"]["long"] = "error"
        self.assertIsNone(initial_inventory_count_bounds(failed)["items_upper_bound"])

    def test_a_channel_without_shorts_or_streams_has_a_usable_estimate(self):
        # The stored and SQL shapes of CGP Grey on 8006 (28.09): both lists absent, counts null.
        value = observed(149, None, None)
        value["statuses"].update(short="playlist_not_found", stream="playlist_not_found")
        for source in (value, {**value, "estimate_kind": "upper_bound", "estimated_items": 149,
                               "items_lower_bound": 149, "items_upper_bound": 149, "estimated_quota_units": 8}):
            with self.subTest(source=sorted(source)):
                result = hydration.normalized_initial_hydration_estimate(source)
                self.assertEqual((result["estimate_kind"], result["items"], result["pages"]), ("upper_bound", 149, 5))
                self.assertEqual(result["quota_units"], 8)
                payload = hydration.initial_hydration_payload(
                    {"channel_id": CHANNEL, "initial_long_inventory_status": "pending"},
                    row=None, estimate=source, mode="durable")
                self.assertEqual(payload["estimate"]["items"], 149)
        self.assertIsNone(long_inventory_estimate_values(0, status="playlist_not_found")["initial_inventory_estimate"]["counts"]["long"])

    def test_single_format_channel_estimate_does_not_require_a_long_playlist(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                value = observed(None, None, None)
                value["counts"][kind] = 51
                value["statuses"] = {current: "ok" if current == kind else "playlist_not_found"
                                     for current in KINDS}
                result = hydration.normalized_initial_hydration_estimate(value)
                self.assertEqual((result["items"], result["pages"], result["quota_units"]), (51, 4, 6))
                self.assertEqual(result["scope"], "all")

    def test_invalid_counts_fail_closed_including_booleans(self):
        for count in (True, -1, "2", 2.0):
            value = observed(count, 0, 0)
            value["statuses"]["long"] = "ok"
            self.assertIsNone(hydration.normalized_initial_hydration_estimate(value))
        value = observed(None, 1, 0)
        value["estimate_kind"] = "upper_bound"
        self.assertIsNone(hydration.normalized_initial_hydration_estimate(value))

    def test_unrepresentable_sum_is_unknown_not_saturated(self):
        result = hydration.normalized_initial_hydration_estimate(observed(2147483647, 1, 0))
        self.assertEqual(result["estimate_kind"], "unknown")
        self.assertIsNone(result["items"])
        self.assertEqual(result["items_lower_bound"], 2147483647)

    def test_absent_source_probe_preserves_outcome_without_zero(self):
        source = Mock(side_effect=[YouTubePlaylistNotFoundError("missing"),
                                  {"pageInfo": {"totalResults": 5}}, {"pageInfo": {"totalResults": 0}}])
        result = estimate_initial_inventory(CHANNEL, fetch_playlist_items_page=source,
                                            clock=lambda: datetime.now(timezone.utc))
        self.assertIsNone(result["counts"]["long"])
        self.assertEqual(result["statuses"]["long"], "playlist_not_found")
        self.assertEqual(result["counts"]["stream"], 0)
        self.assertEqual(source.call_count, 3)


class FullInitialCommandTests(unittest.TestCase):
    def setUp(self):
        self.jobs = Mock()
        self.jobs.schema_status.return_value = dict(SCHEMA)
        self.jobs.enqueue_initial_inventory.return_value = dict(created=True, job=full_job())
        self.channel = dict(channel_id=CHANNEL, collection_control_epoch=7, initial_long_inventory_status="manual_required")
        self.load = Mock(return_value=load_operator_job_settings({}, require_quota_cap=False))
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start(); self.addCleanup(network.stop)

    def test_unknown_can_be_explicitly_accepted_without_a_sentinel(self):
        self.jobs.estimate_initial_inventory.return_value = observed(1, None, 2)
        commands.enqueue_initial_hydration(self.channel, repository=self.jobs, load_settings=self.load, confirmed=True)
        args = self.jobs.enqueue_initial_inventory.call_args.kwargs
        for field in ("expected_items", "expected_pages", "expected_quota_units", "max_items", "max_pages", "max_quota_units"):
            self.assertIsNone(args[field])
        self.assertTrue(args["confirmed"])

    def test_short_or_stream_only_channel_uses_the_same_full_initial_command(self):
        for kind in ("short", "stream"):
            with self.subTest(kind=kind):
                estimate = observed(None, None, None)
                estimate["counts"][kind] = 51
                estimate["statuses"] = {current: "ok" if current == kind else "playlist_not_found"
                                        for current in KINDS}
                self.jobs.estimate_initial_inventory.return_value = estimate
                result, raw, _ = commands.enqueue_initial_hydration(
                    self.channel, repository=self.jobs, load_settings=self.load, confirmed=False)
                args = self.jobs.enqueue_initial_inventory.call_args.kwargs
                self.assertEqual((args["expected_items"], args["expected_pages"], args["expected_quota_units"]),
                                 (51, 4, 6))
                self.assertFalse(args["confirmed"])
                self.assertEqual(result["job"]["initial_video_types"], KINDS)
                self.assertEqual(raw["statuses"]["long"], "playlist_not_found")

    def test_large_known_channel_has_no_python_item_or_page_cutoff(self):
        self.jobs.estimate_initial_inventory.return_value = observed(120001, 0, 0)
        commands.enqueue_initial_hydration(self.channel, repository=self.jobs, load_settings=self.load, confirmed=True)
        args = self.jobs.enqueue_initial_inventory.call_args.kwargs
        self.assertEqual(args["expected_items"], 120001)
        self.assertGreater(args["expected_pages"], 2000)
        self.assertIsNone(args["max_items"]); self.assertIsNone(args["max_pages"])

    def test_retry_confirms_same_workset_with_null_caps(self):
        row = full_job(status="blocked", target_count=10001,
                       last_error_code="initial_inventory_confirmation_required")
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.retry_initial_inventory.return_value = {**row, "status": "queued"}
        raw, _ = hydration.load_initial_hydration_context(self.jobs, CHANNEL)
        estimate = hydration.project_initial_hydration_retry_estimate(
            hydration.normalized_initial_hydration_estimate(raw), operator_job_snapshot(row, channel_id=CHANNEL))
        request = SimpleNamespace(expected_items=estimate["items"], expected_pages=estimate["pages"],
                                  expected_quota_units=estimate["quota_units"], confirmed=True,
                                  retry_request_id="22222222-2222-4222-8222-222222222222")
        _, result = commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        self.assertEqual(result["id"], row["id"])
        args = self.jobs.retry_initial_inventory.call_args.kwargs
        self.assertTrue(args["confirmed"])
        self.assertEqual([args[k] for k in ("max_items", "max_pages", "max_quota_units")], [None] * 3)
        self.jobs.enqueue_initial_inventory.assert_not_called()

    def test_legacy_retry_does_not_convert_scope_or_raise_stored_caps(self):
        row = full_job(initial_scope_version=0, initial_video_types=["long"], status="blocked",
                       max_items=100, max_pages=5, max_quota_units=12)
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.retry_initial_inventory.return_value = {**row, "status": "queued"}
        raw, _ = hydration.load_initial_hydration_context(self.jobs, CHANNEL)
        estimate = hydration.project_initial_hydration_retry_estimate(
            hydration.normalized_initial_hydration_estimate(raw), operator_job_snapshot(row, channel_id=CHANNEL))
        request = SimpleNamespace(expected_items=estimate["items"], expected_pages=estimate["pages"],
                                  expected_quota_units=estimate["quota_units"], confirmed=True,
                                  retry_request_id="22222222-2222-4222-8222-222222222222")
        commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        args = self.jobs.retry_initial_inventory.call_args.kwargs
        self.assertEqual([args[k] for k in ("max_items", "max_pages", "max_quota_units")], [100, 5, 12])
        self.jobs.enqueue_initial_inventory.assert_not_called()

    def legacy_capped(self):
        return full_job(initial_scope_version=0, initial_video_types=["long"], status="blocked",
                        max_items=100, max_pages=5, max_quota_units=12,
                        last_error_code="initial_inventory_cap_exhausted")

    def test_polling_a_legacy_cap_projects_new_scope_without_writing(self):
        row = self.legacy_capped()
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.estimate_initial_inventory.return_value = observed(150, None, 0)
        estimate, old = hydration.load_initial_hydration_context(self.jobs, CHANNEL)
        value = hydration.initial_hydration_payload(self.channel, row=old, estimate=estimate, mode="durable")
        self.assertEqual(value["action_required"], "start")
        self.assertEqual(value["estimate"]["scope"], "all")
        self.assertIsNone(value["estimate"]["items"])
        self.assertEqual(value["job"]["initial_scope_version"], 0)
        self.jobs.enqueue_initial_inventory.assert_not_called(); self.jobs.retry_initial_inventory.assert_not_called()

    def test_explicit_full_load_keeps_old_capped_row_and_creates_new_intent(self):
        import copy
        row = self.legacy_capped(); before = copy.deepcopy(row)
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.estimate_initial_inventory.return_value = observed(150, None, 0)
        replacement = full_job(id="66666666-6666-4666-8666-666666666666")
        self.jobs.enqueue_initial_inventory.return_value = dict(created=True, job=replacement)
        request = SimpleNamespace(expected_items=None, expected_pages=None, expected_quota_units=None,
                                  retry_request_id=None, confirmed=True)
        _, value = commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        self.assertEqual(value["id"], replacement["id"])
        self.assertEqual(row, before)
        self.jobs.retry_initial_inventory.assert_not_called()
        self.assertTrue(self.jobs.enqueue_initial_inventory.call_args.kwargs["confirmed"])

    def test_legacy_retry_race_does_not_claim_full_scope_success(self):
        row = self.legacy_capped()
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.estimate_initial_inventory.return_value = observed(150, None, 0)
        self.jobs.enqueue_initial_inventory.return_value = dict(created=False, job={**row, "status": "queued"})
        request = SimpleNamespace(expected_items=None, expected_pages=None, expected_quota_units=None,
                                  retry_request_id=None, confirmed=True)
        with self.assertRaisesRegex(RuntimeError, "estimate_changed"):
            commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_failed_owned_scan_offers_new_job_without_reusing_old_estimate(self):
        row = full_job(status="error", last_error_code="initial_inventory_restart_required", target_count=250)
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.estimate_initial_inventory.return_value = observed(10, None, 0)
        estimate, old = hydration.load_initial_hydration_context(self.jobs, CHANNEL)
        payload = hydration.initial_hydration_payload(self.channel, row=old, estimate=estimate, mode="durable")
        self.assertEqual(payload["action_required"], "start")
        self.assertEqual(payload["estimate"]["items_lower_bound"], 10)
        self.assertIn("cannot be resumed", payload["job"]["message"])
        self.jobs.enqueue_initial_inventory.assert_not_called()
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_failed_owned_scan_requires_explicit_replacement_and_keeps_old_intent(self):
        import copy
        row = full_job(status="error", last_error_code="initial_inventory_restart_required")
        before = copy.deepcopy(row)
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.estimate_initial_inventory.return_value = observed(10, None, 0)
        replacement = full_job(id="66666666-6666-4666-8666-666666666666")
        self.jobs.enqueue_initial_inventory.return_value = dict(created=True, job=replacement)
        request = SimpleNamespace(expected_items=None, expected_pages=None, expected_quota_units=None,
                                  retry_request_id=None, confirmed=False)
        with self.assertRaisesRegex(RuntimeError, "confirmation_required"):
            commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        self.jobs.enqueue_initial_inventory.assert_not_called()
        request.confirmed = True
        _, value = commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        self.assertEqual(value["id"], replacement["id"])
        self.assertEqual(row, before)
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_restart_never_claims_same_full_job_as_new_success(self):
        row = full_job(status="error", last_error_code="initial_inventory_restart_required")
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.estimate_initial_inventory.return_value = observed(10, None, 0)
        self.jobs.enqueue_initial_inventory.return_value = dict(created=False, job=row)
        request = SimpleNamespace(expected_items=None, expected_pages=None, expected_quota_units=None,
                                  retry_request_id=None, confirmed=True)
        with self.assertRaisesRegex(RuntimeError, "estimate_changed"):
            commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        self.jobs.retry_initial_inventory.assert_not_called()

    def restored_job(self, status, **changes):
        self.channel["collection_control_epoch"] = 9
        row = full_job(status=status, accepted_epoch=7, last_error_code="channel_sync_not_ready", **changes)
        self.jobs.get_initial_inventory_job.return_value = row
        self.jobs.estimate_initial_inventory.return_value = observed(1, None, 0)
        return row

    def restored_request(self, *, confirmed=True):
        return SimpleNamespace(expected_items=None, expected_pages=None, expected_quota_units=None,
                               retry_request_id=None, confirmed=confirmed)

    def test_changed_epoch_projects_fresh_start_without_mutating_old_intent(self):
        for status, phase in (("queued", "inventory"), ("running", "inventory"),
                              ("suspended", "inventory"), ("suspended", "hydrate_videos")):
            with self.subTest(status=status, phase=phase):
                row = self.restored_job(status, phase=phase, target_count=10001,
                                       targets_frozen_at=STAMP if phase == "hydrate_videos" else None)
                before = dict(row)
                raw, current = hydration.load_initial_hydration_context(self.jobs, CHANNEL, expected_epoch=9)
                value = hydration.initial_hydration_payload(self.channel, row=current, estimate=raw, mode="durable")
                self.assertEqual(value["action_required"], "start")
                self.assertEqual(value["status"], "suspended")
                self.assertIsNone(value["estimate"]["items"])
                self.assertEqual(value["estimate"]["items_lower_bound"], 1)
                self.assertEqual(row, before)
        self.assertEqual(self.jobs.estimate_initial_inventory.call_count, 4)
        self.jobs.enqueue_initial_inventory.assert_not_called()
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_changed_epoch_requires_confirmation_even_when_old_job_is_queued(self):
        for status in ("queued", "suspended"):
            with self.subTest(status=status):
                self.restored_job(status)
                with self.assertRaisesRegex(RuntimeError, "confirmation_required"):
                    commands.confirm_initial_hydration(self.channel, self.restored_request(confirmed=False),
                                                       repository=self.jobs, load_settings=self.load)
        self.jobs.enqueue_initial_inventory.assert_not_called()
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_changed_epoch_confirms_new_job_without_retrying_or_rebasing_old_job(self):
        for status in ("queued", "suspended"):
            with self.subTest(status=status):
                row = self.restored_job(status)
                before = dict(row)
                replacement = full_job(id="66666666-6666-4666-8666-666666666666", accepted_epoch=9)
                self.jobs.enqueue_initial_inventory.return_value = dict(created=True, job=replacement)
                estimate, result = commands.confirm_initial_hydration(self.channel, self.restored_request(),
                                                                      repository=self.jobs, load_settings=self.load)
                self.assertIsNone(estimate["items"])
                self.assertEqual(result["id"], replacement["id"])
                args = self.jobs.enqueue_initial_inventory.call_args.kwargs
                self.assertEqual(args["expected_epoch"], 9)
                self.assertTrue(args["confirmed"])
                self.assertEqual([args[k] for k in ("max_items", "max_pages", "max_quota_units")], [None] * 3)
                self.assertEqual(row, before)
        self.jobs.retry_initial_inventory.assert_not_called()
        self.jobs.mark_completed_initial_hydration_failed.assert_not_called()

    def test_changed_epoch_rejects_replacement_ack_from_another_generation(self):
        self.restored_job("queued")
        for epoch in (7, 10, None):
            with self.subTest(epoch=epoch):
                self.jobs.enqueue_initial_inventory.return_value = dict(created=False, job=full_job(
                    id="66666666-6666-4666-8666-666666666666", accepted_epoch=epoch))
                with self.assertRaisesRegex(RuntimeError, "estimate_changed"):
                    commands.confirm_initial_hydration(self.channel, self.restored_request(),
                                                       repository=self.jobs, load_settings=self.load)
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_expired_changed_epoch_still_requires_exact_new_job_ack(self):
        for status in ("queued", "completed"):
            with self.subTest(status=status):
                row = self.restored_job(status, phase="verify" if status == "completed" else "inventory",
                                       error_count=1, requested_at="2000-01-01T00:00:00+00:00")
                self.jobs.estimate_initial_inventory.return_value = {**observed(1, None, 0), "enqueue_allowed": True}
                replacement = full_job(id="66666666-6666-4666-8666-666666666666", accepted_epoch=10)
                self.jobs.enqueue_initial_inventory.return_value = dict(created=False, job=replacement)
                with self.assertRaisesRegex(RuntimeError, "estimate_changed"):
                    commands.confirm_initial_hydration(self.channel, self.restored_request(),
                                                       repository=self.jobs, load_settings=self.load)
                replacement["accepted_epoch"] = 9
                _, result = commands.confirm_initial_hydration(self.channel, self.restored_request(),
                                                              repository=self.jobs, load_settings=self.load)
                self.assertEqual(result["id"], replacement["id"])
                self.assertEqual(row["accepted_epoch"], 7)
        self.jobs.retry_initial_inventory.assert_not_called()
        self.jobs.mark_completed_initial_hydration_failed.assert_not_called()

    def test_changed_epoch_checks_fresh_estimate_for_previously_queued_job(self):
        self.restored_job("queued")
        request = self.restored_request()
        request.expected_items = 10
        with self.assertRaisesRegex(RuntimeError, "estimate_changed"):
            commands.confirm_initial_hydration(self.channel, request, repository=self.jobs, load_settings=self.load)
        self.jobs.enqueue_initial_inventory.assert_not_called()
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_same_epoch_queued_job_does_not_request_a_new_intent(self):
        row = full_job()
        self.jobs.get_initial_inventory_job.return_value = row
        _, result = commands.confirm_initial_hydration(self.channel, self.restored_request(confirmed=False),
                                                      repository=self.jobs, load_settings=self.load)
        self.assertIs(result, row)
        self.jobs.estimate_initial_inventory.assert_not_called()
        self.jobs.enqueue_initial_inventory.assert_not_called()
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_completed_clean_milestone_is_not_restarted_after_restore(self):
        row = self.restored_job("completed", phase="verify", error_count=0, targets_frozen_at=STAMP)
        self.assertFalse(hydration.initial_hydration_requires_new_job(row, expected_epoch=9))
        hydration.load_initial_hydration_context(self.jobs, CHANNEL, expected_epoch=9)
        self.jobs.estimate_initial_inventory.assert_not_called()
        self.jobs.enqueue_initial_inventory.assert_not_called()
        self.jobs.retry_initial_inventory.assert_not_called()

    def test_retryable_errors_do_not_replace_the_owned_job(self):
        for code in ("youtube_request_failed", "youtube_daily_quota_exceeded_error",
                     "initial_inventory_identity_unresolved"):
            self.assertFalse(hydration.initial_hydration_requires_new_job(full_job(status="error", last_error_code=code)))
        for phase, status in (("hydrate_videos", "error"), ("inventory", "running")):
            self.assertFalse(hydration.initial_hydration_requires_new_job(full_job(phase=phase, status=status,
                last_error_code="initial_inventory_restart_required")))

    def test_current_stats_quota_is_still_required(self):
        with self.assertRaisesRegex(ValueError, "OPERATOR_JOB_MAX_QUOTA_UNITS"):
            load_operator_job_settings({})
        settings = load_operator_job_settings({"OPERATOR_JOB_MAX_QUOTA_UNITS": "200"})
        self.assertEqual(settings.max_quota_units, 200)
        self.assertFalse(hasattr(settings, "max_items"))
        for value in ("0", "251"):
            with self.assertRaises(ValueError):
                load_operator_job_settings({"OPERATOR_JOB_CHUNK_SIZE": value}, require_quota_cap=False)

    def test_initial_concurrency_bound_does_not_expand_manual_chunk_size(self):
        for count in (1, 8, 16, 32, 50):
            settings = load_operator_job_settings({"LIC_YOUTUBE_REQUEST_CONCURRENCY": str(count),
                "OPERATOR_JOB_CHUNK_SIZE": "250"}, require_quota_cap=False)
            self.assertEqual((settings.initial_load_concurrency, settings.chunk_size), (min(count, 8), 250))
        for invalid in ("0", "51", "-1", "true", "1.5"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                load_operator_job_settings({"LIC_YOUTUBE_REQUEST_CONCURRENCY": invalid}, require_quota_cap=False)
        self.assertEqual(load_operator_job_settings({}, require_quota_cap=False).initial_load_concurrency, 8)

    def test_repository_preserves_nulls_in_full_enqueue_abi(self):
        repo = OperatorJobRepository.__new__(OperatorJobRepository)
        repo._rpc = Mock(return_value=dict(created=True, job=full_job()))
        repo.enqueue_initial_inventory(CHANNEL, expected_epoch=7, expected_items=None, expected_pages=None,
            expected_quota_units=None, max_items=None, max_pages=None, max_quota_units=None, confirmed=True)
        args = repo._rpc.call_args.args[1]
        self.assertIsNone(args["p_expected_items"])
        self.assertIsNone(args["p_max_items"])
        self.assertIsNone(args["p_max_pages"])


class FullInitialProgressTests(unittest.TestCase):
    def payload(self, row):
        repo = Mock(); repo.get_initial_inventory_job.return_value = row
        estimate, _ = hydration.load_initial_hydration_context(repo, CHANNEL)
        return hydration.initial_hydration_payload(dict(channel_id=CHANNEL, initial_long_inventory_status="pending"),
                                                   row=row, estimate=estimate, mode="durable")

    def test_discovery_reports_distinct_count_but_unknown_denominator(self):
        value = self.payload(full_job())
        self.assertEqual(value["contract_version"], 2)
        self.assertEqual(value["progress"], dict(processed=4, discovered=4, total=None, percent=None))
        self.assertFalse(value["current_profile_ready"])

    def test_original_quota_estimate_survives_discovery_freeze_and_completion(self):
        for status, phase, frozen in (("queued", "inventory", False), ("running", "inventory", False),
                ("running", "hydrate_videos", True), ("blocked", "hydrate_videos", True),
                ("completed", "verify", True)):
            with self.subTest(status=status, phase=phase):
                row = full_job(status=status, phase=phase, target_count=20243, pages_processed=406,
                    targets_frozen_at=STAMP if frozen else None, quota_units_estimated=1216,
                    quota_units_observed_estimate=830,
                    summary_json=dict(estimated_items=20243, estimated_pages=406,
                                      items_lower_bound=20000, estimated_quota_units=811))
                estimate = self.payload(row)["estimate"]
                self.assertEqual(estimate["quota_units"], 811)
                self.assertEqual(estimate["quota_basis"], "before_start")
                self.assertEqual(estimate["items"], 20243)
                # The original evidence is not overwritten with observed spending.
                self.assertEqual(row["quota_units_estimated"], 1216)
                self.assertEqual(row["quota_units_observed_estimate"], 830)

    def test_initial_unknown_or_missing_quota_does_not_invent_an_original_estimate(self):
        for original in (None, -1, True, "bad"):
            with self.subTest(original=original):
                row = full_job(targets_frozen_at=STAMP, phase="hydrate_videos", quota_units_estimated=7,
                    summary_json=dict(estimated_items=4, estimated_pages=3, estimated_quota_units=original))
                estimate = self.payload(row)["estimate"]
                self.assertEqual(estimate["quota_units"], 7)
                self.assertNotIn("quota_basis", estimate)

    def test_zero_initial_estimate_is_not_replaced_by_a_later_forecast(self):
        row = full_job(targets_frozen_at=STAMP, phase="hydrate_videos", quota_units_estimated=7,
            summary_json=dict(estimated_items=4, estimated_pages=3, estimated_quota_units=0))
        self.assertEqual(self.payload(row)["estimate"]["quota_units"], 0)
        self.assertEqual(self.payload(row)["estimate"]["quota_basis"], "before_start")

    def test_unknown_unstarted_channel_can_be_confirmed(self):
        value = hydration.initial_hydration_payload(dict(channel_id=CHANNEL, initial_long_inventory_status="manual_required"),
                                                    estimate=observed(None, 1, 0), mode="durable")
        self.assertEqual(value["status"], "confirmation_required")
        self.assertEqual(value["action_required"], "confirm")
        self.assertIsNone(value["progress"]["total"])
        self.assertIsNone(value["progress"]["percent"])
        self.assertIsNone(value["estimate"]["items"])

    def test_read_error_does_not_keep_a_confirmation_action(self):
        for estimate in (None, observed(None, 1, 0)):
            value = hydration.initial_hydration_error_payload(
                dict(channel_id=CHANNEL, initial_long_inventory_status="manual_required"),
                typed_inventory_enabled=True, estimate=estimate)
            self.assertEqual(value["status"], "error")
            self.assertFalse(value["confirmation_required"])
            self.assertIsNone(value["action_required"])

    def test_observed_10001_requests_confirmation_for_existing_job(self):
        value = self.payload(full_job(status="blocked", target_count=10001,
                                     last_error_code="initial_inventory_confirmation_required"))
        self.assertEqual((value["status"], value["action_required"]), ("confirmation_required", "confirm"))
        self.assertTrue(value["confirmation_required"])
        self.assertEqual(value["progress"]["discovered"], 10001)
        self.assertEqual(value["estimate"]["items_lower_bound"], 10001)

    def test_frozen_workset_has_exact_progress_without_cap(self):
        value = self.payload(full_job(phase="hydrate_videos", targets_frozen_at=STAMP, processed_count=2))
        self.assertEqual(value["progress"], dict(processed=2, discovered=4, total=4, percent=50))
        self.assertEqual(value["estimate"]["estimate_kind"], "exact_workset")
        self.assertEqual(value["estimate"]["items"], 4)
        self.assertIsNone(value["job"]["max_items"])

    def test_completion_without_freeze_cannot_open_profile_even_with_forged_summary(self):
        ready = {kind: dict(total=0, updated=0, unavailable=0, checked=0, pending=0, errors=0,
                           inventory_ready=True, initial_check_complete=True, current_profile_ready=True)
                 for kind in KINDS}
        row = full_job(status="completed", phase="verify", summary_json={"per_format": ready, "current_profile_ready": True})
        value = self.payload(row)
        self.assertFalse(value["current_profile_ready"])
        self.assertFalse(value["all_initial_checks_complete"])
        self.assertEqual(value["status"], "error")


class FullInitialWorkerTests(unittest.TestCase):
    def setUp(self):
        from operator_jobs import run_one_operator_job_chunk
        self.run = run_one_operator_job_chunk
        self.repo, self.inventory, self.video, self.stats = Mock(), Mock(), Mock(), Mock()
        self.claim = full_job(status="running", lease_token="33333333-3333-4333-8333-333333333333",
                              lease_channel_epoch=7)
        self.repo.claim.return_value = self.claim
        self.settings = load_operator_job_settings({}, require_quota_cap=False)

    def execute(self):
        return self.run(self.repo, self.video, settings=self.settings, worker_id="test",
                        run_initial_inventory_chunk=self.inventory, run_initial_stats=self.stats)

    def test_initial_window_uses_concurrency_and_forwards_unsent_ids(self):
        self.claim.update(phase="hydrate_videos", targets_frozen_at=STAMP, lease_owner="test")
        ids = ["v" + str(index).zfill(10) for index in range(400)]
        self.repo.settle_initial_hydration_chunk.return_value = full_job()
        for concurrency, manual_size in ((1, 250), (8, 1)):
            with self.subTest(concurrency=concurrency):
                self.settings = load_operator_job_settings({"LIC_YOUTUBE_REQUEST_CONCURRENCY": str(concurrency),
                    "OPERATOR_JOB_CHUNK_SIZE": str(manual_size)}, require_quota_cap=False)
                selected = ids[:50 * concurrency]
                self.repo.prepare_initial_hydration_chunk.return_value = dict(video_ids=selected, job=self.claim)
                self.video.return_value = dict(requested=len(selected), metadata_requests=1, batch_requests=0,
                    unstarted_video_ids=selected[50:])
                self.execute()
                self.assertEqual(self.repo.prepare_initial_hydration_chunk.call_args.kwargs["limit"], 50 * concurrency)
                options = self.video.call_args.kwargs
                self.assertEqual(options["initial_load_concurrency"], concurrency)
                self.assertEqual(options["explicit_video_ids"], selected)
                settled = self.repo.settle_initial_hydration_chunk.call_args.kwargs
                self.assertEqual(settled["observed_quota_units"], 1)
                self.assertEqual(settled["summary"]["hydration_unstarted_video_ids"], selected[50:])

    def test_legacy_hydration_keeps_manual_bound_and_split_provider_route(self):
        self.claim.update(initial_scope_version=0, phase="hydrate_videos", lease_owner="test")
        self.settings = load_operator_job_settings({"OPERATOR_JOB_CHUNK_SIZE": "250"}, require_quota_cap=False)
        self.repo.prepare_initial_hydration_chunk.return_value = dict(video_ids=["v0000000000"], job=self.claim)
        self.video.return_value = dict(metadata_requests=1, batch_requests=1)
        self.repo.settle_initial_hydration_chunk.return_value = full_job()
        self.execute()
        self.assertEqual(self.repo.prepare_initial_hydration_chunk.call_args.kwargs["limit"], 250)
        options = self.video.call_args.kwargs
        self.assertNotIn("initial_job", options)
        self.assertNotIn("initial_load_concurrency", options)
        self.assertEqual(options["channel_scopes"], [{"channel_id": CHANNEL, "control_epoch": 7}])
        self.assertEqual(self.repo.settle_initial_hydration_chunk.call_args.kwargs["observed_quota_units"], 2)

    def test_full_page_dispatch_carries_exact_job_lease(self):
        transition = "44444444-4444-4444-8444-444444444444"
        self.repo.prepare_initial_inventory_chunk.return_value = dict(job=self.claim, action="inventory",
            video_type="short", page_limit=1, reserved_units=1, transition_id=transition, resume_page_token=None)
        self.inventory.return_value = dict(outcome="completed", pages=1, items=0)
        self.repo.settle_initial_inventory_chunk.return_value = full_job()
        self.execute()
        options = self.inventory.call_args.kwargs
        self.assertEqual(options["initial_job_id"], self.claim["id"])
        self.assertEqual(options["initial_job_lease_token"], self.claim["lease_token"])
        self.assertEqual(options["expected_epoch"], 7)
        self.assertEqual(options["max_pages"], 1)

    def test_reconciled_targets_never_dispatch_or_settle_a_playlist_page(self):
        for phase, frozen in (("inventory", None), ("hydrate_videos", STAMP)):
            row = full_job(phase=phase, targets_frozen_at=frozen, lease_token=None)
            self.repo.prepare_initial_inventory_chunk.return_value = dict(job=row, action="targets_reconciled",
                page_limit=0, reserved_units=0, transition_id=None)
            result = self.execute()
            self.assertEqual(result["job"]["phase"], phase)
            self.assertEqual(result["processed_pages"], 0)
        self.inventory.assert_not_called(); self.video.assert_not_called(); self.stats.assert_not_called()
        self.repo.settle_initial_inventory_chunk.assert_not_called()
        self.repo.fail_initial_inventory.assert_not_called()

    def test_invalid_reconciliation_ack_never_becomes_destructive_cleanup(self):
        for change in ({"channel_id": "foreign"}, {"accepted_epoch": 8}, {"initial_scope_version": 0},
                       {"phase": "hydrate_videos", "targets_frozen_at": None}):
            row = full_job(lease_token=None, **change)
            self.repo.prepare_initial_inventory_chunk.return_value = dict(job=row, action="targets_reconciled",
                page_limit=0, reserved_units=0, transition_id=None)
            with self.assertRaisesRegex(RuntimeError, "reconciliation_invalid"):
                self.execute()
        self.inventory.assert_not_called(); self.repo.fail_initial_inventory.assert_not_called()
        self.repo.settle_initial_inventory_chunk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
