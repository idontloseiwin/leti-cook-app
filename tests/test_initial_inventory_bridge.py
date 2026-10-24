"""Offline job/page handshake and recovery; no network, server or database."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection import initial
from collection.settings import CollectionSettings
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseUnavailable
from operator_jobs import OperatorJobSettings, run_one_operator_job_chunk
from hot_runtime import HotDatabaseAdmissionDenied

CHANNEL = "UC" + "a" * 22
JOB = "10000000-0000-0000-0000-000000000001"
LEASE = "20000000-0000-0000-0000-000000000001"
PLAYLIST = "30000000-0000-0000-0000-000000000001"
SCAN = "40000000-0000-0000-0000-000000000001"
TRANSITION = "50000000-0000-0000-0000-000000000001"
SETTINGS = OperatorJobSettings(max_quota_units=100, max_target_attempts=3, chunk_size=50, lease_seconds=1200)


def job(**changes):
    return dict(id=JOB, channel_id=CHANNEL, lease_token=LEASE, lease_channel_epoch=7,
                job_type="channel_initial_long_inventory", phase="inventory", status="running", **changes)


def prepared(action="inventory", **changes):
    value = dict(job=job(), action=action, video_type="long", page_limit=1,
                 reserved_units=1, transition_id=TRANSITION, resume_page_token=None)
    value.update(changes)
    return value


class InitialJobBridgeTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start(); self.addCleanup(network.stop)
        self.repo = Mock()
        self.repo.claim.return_value = job()
        self.repo.prepare_initial_inventory_chunk.return_value = prepared()
        self.repo.bind_initial_inventory_scan.return_value = dict(
            status="bound", job_id=JOB, scan_id=SCAN, transition_id=TRANSITION,
            replayed=False, dispatch_allowed=True)
        self.repo.settle_initial_inventory_chunk.return_value = {**job(), "status": "queued"}
        self.repo.fail_initial_inventory.return_value = {**job(), "status": "queued"}
        self.stats = Mock(return_value={"status": "done"})
        self.provider = Mock()

    def run_job(self, callback=None):
        return run_one_operator_job_chunk(self.repo, Mock(), settings=SETTINGS, worker_id="worker",
            run_initial_inventory_chunk=callback or self.collect, run_initial_stats=self.stats)

    def collect(self, channel_id, **options):
        self.assertEqual(channel_id, CHANNEL)
        self.assertEqual(options["expected_epoch"], 7)
        self.assertEqual(options["max_pages"], 1)
        self.assertIsNone(options["page_token"])
        self.assertEqual(options["transition_id"], TRANSITION)
        claim = SimpleNamespace(channel_id=CHANNEL, video_type=options["video_type"],
                                lease_token=PLAYLIST, lease_channel_epoch=7)
        if not options["before_dispatch"](claim, {"scan_id": SCAN}, TRANSITION):
            return {"settlement_unknown": True}
        self.provider()
        return dict(pages=1, items=3, inserted=2, updated=1, outcome="running")

    def assert_no_destructive_cleanup(self):
        self.repo.fail_initial_inventory.assert_not_called()
        self.repo.release.assert_not_called()

    def test_all_formats_use_same_bound_page_and_null_job_cursor(self):
        for kind in ("long", "short", "stream"):
            with self.subTest(kind=kind):
                self.repo.prepare_initial_inventory_chunk.return_value = prepared(video_type=kind)
                result = self.run_job()
                self.assertEqual(result["processed_pages"], 1)
                self.assertEqual(self.repo.prepare_initial_inventory_chunk.call_args.kwargs["page_limit"], 1)
                self.repo.bind_initial_inventory_scan.assert_called_with(JOB, LEASE,
                    playlist_lease_token=PLAYLIST, scan_id=SCAN, transition_id=TRANSITION)
                self.repo.settle_initial_inventory_chunk.assert_called_with(JOB, LEASE,
                    reserved_units=1, pages_processed=1, items_processed=3, inserted=2, updated=1,
                    next_page_token=None, summary={"transition_id": TRANSITION})
        self.assertEqual(self.provider.call_count, 3)
        self.assert_no_destructive_cleanup(); self.stats.assert_not_called()

    def test_stored_full_evidence_uses_no_claim_bind_or_provider(self):
        self.repo.prepare_initial_inventory_chunk.return_value = prepared(page_limit=0, reserved_units=0)
        callback = Mock(side_effect=AssertionError("provider path forbidden"))
        self.assertEqual(self.run_job(callback)["processed_pages"], 0)
        callback.assert_not_called(); self.repo.bind_initial_inventory_scan.assert_not_called()
        self.assertEqual(self.repo.settle_initial_inventory_chunk.call_args.kwargs,
            dict(reserved_units=0, pages_processed=0, items_processed=0, inserted=0, updated=0,
                 next_page_token=None, summary={"transition_id": TRANSITION}))

    def test_lost_job_ack_recovers_committed_page_without_second_provider(self):
        self.repo.settle_initial_inventory_chunk.side_effect = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        with self.assertRaises(DatabaseUnavailable): self.run_job()
        self.assert_no_destructive_cleanup()
        self.repo.settle_initial_inventory_chunk.side_effect = None
        self.repo.prepare_initial_inventory_chunk.return_value = prepared("reconcile_inventory", evidence={
            "transition_id": TRANSITION, "pages_processed": 1, "items_processed": 3,
            "inserted": 2, "updated": 1})
        self.assertEqual(self.run_job()["processed_pages"], 1)
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.repo.bind_initial_inventory_scan.call_count, 1)
        calls = self.repo.settle_initial_inventory_chunk.call_args_list
        self.assertEqual(calls[0], calls[1])

    def test_prepare_unknown_ack_never_replaces_checkpoint_with_failure(self):
        self.repo.prepare_initial_inventory_chunk.side_effect = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        with self.assertRaises(DatabaseUnavailable): self.run_job()
        self.assert_no_destructive_cleanup(); self.provider.assert_not_called()

    def test_bind_unknown_ack_never_replaces_checkpoint_with_failure(self):
        self.repo.bind_initial_inventory_scan.side_effect = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        with self.assertRaises(DatabaseUnavailable): self.run_job()
        self.assert_no_destructive_cleanup(); self.provider.assert_not_called()
        self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_exact_bind_replay_forbids_provider_and_settlement(self):
        self.repo.bind_initial_inventory_scan.return_value.update(replayed=True, dispatch_allowed=False)
        self.assertTrue(self.run_job()["settlement_unknown"])
        self.provider.assert_not_called(); self.repo.settle_initial_inventory_chunk.assert_not_called()
        self.assert_no_destructive_cleanup()

    def test_invalid_binding_identity_cannot_dispatch(self):
        for field, value in (("scan_id", "foreign"), ("transition_id", "foreign"),
                             ("job_id", "foreign"), ("dispatch_allowed", 1), ("replayed", True)):
            with self.subTest(field=field):
                old = dict(self.repo.bind_initial_inventory_scan.return_value)
                self.repo.bind_initial_inventory_scan.return_value[field] = value
                with self.assertRaisesRegex(RuntimeError, "binding_receipt_invalid"): self.run_job()
                self.repo.bind_initial_inventory_scan.return_value = old
        self.provider.assert_not_called(); self.assert_no_destructive_cleanup()

    def test_prepared_epoch_cannot_replace_original_job_scope(self):
        self.repo.prepare_initial_inventory_chunk.return_value["job"]["lease_channel_epoch"] = 9
        with self.assertRaisesRegex(RuntimeError, "identity_mismatch"): self.run_job()
        self.repo.bind_initial_inventory_scan.assert_not_called(); self.provider.assert_not_called()
        self.assert_no_destructive_cleanup()

    def test_live_bound_child_awaits_without_any_provider(self):
        self.repo.prepare_initial_inventory_chunk.return_value = prepared("await_receipt")
        self.assertTrue(self.run_job()["settlement_unknown"])
        self.provider.assert_not_called(); self.assert_no_destructive_cleanup()

    def test_expired_unknown_child_accounts_only_through_recovery_rpc(self):
        self.repo.prepare_initial_inventory_chunk.return_value = prepared("reconcile_unknown")
        self.run_job()
        self.repo.fail_initial_inventory.assert_called_once_with(JOB, LEASE,
            error_code="initial_inventory_outcome_unknown")
        self.provider.assert_not_called(); self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_quota_recovery_uses_receipt_code_without_provider(self):
        self.repo.prepare_initial_inventory_chunk.return_value = prepared("reconcile_failure",
            evidence={"error_code": "initial_inventory_quota_exhausted"})
        result = self.run_job()
        self.assertTrue(result["quota_exhausted"])
        self.assertEqual(result["stopped_reason"], "youtube_quota_exhausted")
        self.repo.fail_initial_inventory.assert_called_once_with(JOB, LEASE,
            error_code="initial_inventory_quota_exhausted")
        self.provider.assert_not_called()

    def test_confirmed_failures_have_exact_codes_and_no_page_success(self):
        for outcome, code in (("deferred", "initial_inventory_deferred"),
                              ("quota_deferred", "initial_inventory_quota_exhausted"),
                              ("error", "typed_playlist_request_failed")):
            with self.subTest(outcome=outcome):
                result = self.run_job(Mock(return_value={"outcome": outcome, "error_code": code}))
                self.assertEqual(result["processed_pages"], 0)
                self.repo.fail_initial_inventory.assert_called_with(JOB, LEASE, error_code=code)
        self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_missing_playlist_is_zero_successful_pages(self):
        self.run_job(Mock(return_value=dict(outcome="playlist_not_found", pages=0, items=0)))
        self.assertEqual(self.repo.settle_initial_inventory_chunk.call_args.kwargs["pages_processed"], 0)
        self.assert_no_destructive_cleanup()

    def test_unknown_page_or_quota_ack_preserves_checkpoint(self):
        for quota in (False, True):
            result = self.run_job(Mock(return_value={"settlement_unknown": True, "quota_exhausted": quota}))
            self.assertTrue(result["settlement_unknown"])
            self.assertEqual(result["quota_exhausted"], quota)
        self.assert_no_destructive_cleanup(); self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_numeric_tamper_is_not_coerced_into_success(self):
        for value in (True, -1, 51, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_job(Mock(return_value={"outcome": "completed", "pages": 1, "items": value}))
        self.assert_no_destructive_cleanup(); self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_initial_stats_is_retained_and_does_not_bind_inventory(self):
        self.repo.prepare_initial_inventory_chunk.return_value = prepared("initial_stats", reserved_units=2)
        self.repo.settle_initial_stats.return_value = {**job(), "status": "completed"}
        self.assertEqual(self.run_job()["status"], "completed")
        self.stats.assert_called_once_with(CHANNEL, expected_epoch=7)
        self.repo.bind_initial_inventory_scan.assert_not_called(); self.provider.assert_not_called()


class InitialSharedExecutorTests(InitialJobBridgeTests):
    """The real operator -> initial -> shared executor -> binding path."""
    # Inherited cases are re-exercised through the real executor as well.
    def collect(self, channel_id, **options):
        from datetime import datetime, timezone
        from db.inventory.typed_inventory import TypedInventoryClaim
        from typed_playlist import typed_playlist_id
        from youtube_errors import YouTubeDailyQuotaExceededError
        now = datetime.now(timezone.utc).isoformat()
        kind = options["video_type"]
        child = Mock()
        child.claim_channels.return_value = [TypedInventoryClaim(
            channel_id=CHANNEL, video_type=kind, playlist_id=typed_playlist_id(CHANNEL, kind),
            lease_token=PLAYLIST, lease_channel_epoch=7, lease_expires_at=now,
            no_new_streak=0, active_scan_id=None, resume_page_token=None,
            scan_kind="initial_inventory", scan_boundary_at=None)]
        child.claim_initial_source.return_value = child.claim_channels.return_value
        child.begin_scan.return_value = dict(status="running", scan_id=SCAN,
                                            scan_boundary_at=now, resume_page_token=None)
        def settle(owned, **kw):
            return dict(channel_id=CHANNEL, video_type=kind, scan_id=SCAN, lease_token=PLAYLIST,
                transition_id=TRANSITION, page_items=3, inserted=2, already_matching=1,
                conflicts=0, items_seen=3, canonical_video_ids=["vid00000001"], new_video_ids=[],
                status="running", coverage="partial", resume_page_token="scan-owned-cursor")
        child.settle_page.side_effect = getattr(self, "page_ack_error", None) or settle
        child.fail_scan.return_value = {"items_seen": 0}
        child.defer_claims_after_daily_quota.return_value = dict(
            status="deferred", active_scan_state_preserved=True, requested=1, released=1, rejected=0)
        if getattr(self, "quota_ack_error", False):
            child.defer_claims_after_daily_quota.side_effect = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        def provider(*args, **kw):
            self.provider()
            if getattr(self, "quota", False): raise YouTubeDailyQuotaExceededError("offline quota")
            return {"items": [{"contentDetails": {"videoId": "vid00000001", "videoPublishedAt": now}}],
                    "nextPageToken": "scan-owned-cursor"}
        with patch.object(initial, "TypedInventoryRepository", return_value=child):
            result = initial.collect_initial_inventory_chunk(channel_id, **options,
                database=object(), settings=CollectionSettings(True, True, True), worker_id="worker",
                fetch_page=provider, get_longform_playlist_id=lambda value: "UULF" + value[2:],
                parse_dt=lambda value: value, playlist_not_found_error=LookupError,
                daily_quota_exceeded_error=YouTubeDailyQuotaExceededError)
        self.child = child
        return result

    def test_real_hot_page_refusal_releases_parent_and_stops_without_page_success(self):
        # Full scope-one job: real job -> adapter -> shared inventory error path.
        self.repo.claim.return_value['initial_scope_version'] = 1
        self.repo.prepare_initial_inventory_chunk.return_value['job']['initial_scope_version'] = 1
        error = HotDatabaseAdmissionDenied()
        error.control_abort_attempted = True
        error.control_receipt = {"playlist_released": 1}
        self.page_ack_error = error
        with patch("db.hot.hot_collection_control.HotCollectionControlRepository.abort",
                   return_value={"operator_released": 1}) as abort:
            result = self.run_job()
        self.assertEqual(result['status'], 'interrupted')
        self.assertTrue(result['infrastructure_error'])
        self.assertTrue(result['hot_runtime_interrupted'])
        self.assertTrue(result['parent_released'])
        self.assertFalse(result.get('settlement_unknown', False))
        abort.assert_called_once_with(operators=[{"job_id": JOB, "lease_token": LEASE}])
        self.provider.assert_called_once()
        self.child.fail_scan.assert_not_called()
        self.assert_no_destructive_cleanup()
        self.repo.settle_initial_inventory_chunk.assert_not_called()
        # Next owner reconciles the retained checkpoint before any new page.
        self.repo.prepare_initial_inventory_chunk.return_value = prepared('reconcile_unknown')
        self.run_job()
        self.provider.assert_called_once()
        self.repo.fail_initial_inventory.assert_called_once_with(
            JOB, LEASE, error_code='initial_inventory_outcome_unknown')

    def test_real_hot_uncertain_page_or_child_abort_never_releases_parent(self):
        for flag in ('outcome_may_be_committed', 'control_abort_failed'):
            with self.subTest(flag=flag):
                error = HotDatabaseAdmissionDenied()
                error.control_abort_attempted = True
                setattr(error, flag, True)
                self.page_ack_error = error
                with patch("db.hot.hot_collection_control.HotCollectionControlRepository.abort") as abort:
                    result = self.run_job()
                self.assertTrue(result['settlement_unknown'])
                self.assertTrue(result['infrastructure_error'])
                self.assertTrue(result['hot_runtime_interrupted'])
                abort.assert_not_called()
        self.assert_no_destructive_cleanup()
        self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_real_hot_parent_abort_unknown_ack_is_not_retried_or_replaced(self):
        error = HotDatabaseAdmissionDenied()
        error.control_abort_attempted = True
        self.page_ack_error = error
        with patch("db.hot.hot_collection_control.HotCollectionControlRepository.abort",
                   side_effect=DatabaseUnavailable('database_connection_failed', outcome_unknown=True)) as abort:
            result = self.run_job()
        self.assertTrue(result['settlement_unknown'])
        self.assertTrue(result['infrastructure_error'])
        abort.assert_called_once_with(operators=[{"job_id": JOB, "lease_token": LEASE}])
        self.assert_no_destructive_cleanup()
        self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_real_shared_page_ack_loss_preserves_job_and_never_fails_page(self):
        self.page_ack_error = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        self.assertTrue(self.run_job()["settlement_unknown"])
        self.provider.assert_called_once()
        self.child.fail_scan.assert_not_called()
        self.assert_no_destructive_cleanup()
        self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_real_daily_quota_uses_planned_singleton_then_job_failure_receipt(self):
        self.quota = True
        result = self.run_job()
        self.assertTrue(result["quota_exhausted"])
        self.provider.assert_called_once()
        self.child.fail_scan.assert_not_called()
        args, kwargs = self.child.defer_claims_after_daily_quota.call_args
        self.assertEqual(len(args[0]), 1)
        self.assertEqual(args[0][0].lease_token, PLAYLIST)
        self.assertEqual(kwargs["transition_id"], TRANSITION)
        self.repo.fail_initial_inventory.assert_called_once_with(JOB, LEASE,
            error_code="initial_inventory_quota_exhausted")

    def test_real_daily_quota_lost_ack_leaves_job_checkpoint(self):
        self.quota = True
        self.quota_ack_error = True
        result = self.run_job()
        self.assertTrue(result["settlement_unknown"])
        self.assertTrue(result["quota_exhausted"])
        self.provider.assert_called_once(); self.assert_no_destructive_cleanup()
        self.child.fail_scan.assert_not_called()

    def test_bind_unknown_ack_never_replaces_checkpoint_with_failure(self):
        self.repo.bind_initial_inventory_scan.side_effect = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        self.assertTrue(self.run_job()["settlement_unknown"])
        self.assert_no_destructive_cleanup(); self.provider.assert_not_called()
        self.repo.settle_initial_inventory_chunk.assert_not_called()

    def test_invalid_binding_identity_cannot_dispatch(self):
        self.repo.bind_initial_inventory_scan.return_value["scan_id"] = "foreign"
        self.assertTrue(self.run_job()["settlement_unknown"])
        self.provider.assert_not_called(); self.assert_no_destructive_cleanup()



class InitialRepositoryBridgeTests(unittest.TestCase):
    def test_preparation_preserves_checkpoint_identity_and_evidence(self):
        repo = OperatorJobRepository.__new__(OperatorJobRepository)
        payload = prepared("reconcile_inventory", scan_id=SCAN, evidence={"transition_id": TRANSITION})
        repo._rpc = Mock(return_value=payload)
        self.assertEqual(repo.prepare_initial_inventory_chunk(JOB, LEASE, page_limit=1), payload)
        self.assertFalse(repo._rpc.call_args.kwargs["abort_on_denied"])

    def test_bind_has_exact_rpc_shape_and_conservative_replay_policy(self):
        repo = OperatorJobRepository.__new__(OperatorJobRepository)
        repo._rpc = Mock(return_value={"status": "bound", "replayed": True, "dispatch_allowed": False})
        result = repo.bind_initial_inventory_scan(JOB, LEASE, playlist_lease_token=PLAYLIST,
                                                scan_id=SCAN, transition_id=TRANSITION)
        self.assertFalse(result["dispatch_allowed"])
        repo._rpc.assert_called_once_with("bind_initial_inventory_scan", {
            "p_job_id": JOB, "p_job_lease_token": LEASE, "p_playlist_lease_token": PLAYLIST,
            "p_scan_id": SCAN, "p_transition_id": TRANSITION}, abort_on_denied=False)

    def test_admission_denial_during_bind_does_not_abort_prior_binding(self):
        repo = OperatorJobRepository.__new__(OperatorJobRepository)
        repo.database = Mock(spec_set=['call', 'transaction'])
        error = HotDatabaseAdmissionDenied("cleanup_unavailable")
        repo.database.transaction.side_effect = error
        with patch("db.hot.hot_collection_control.HotCollectionControlRepository") as control:
            with self.assertRaises(HotDatabaseAdmissionDenied):
                repo.bind_initial_inventory_scan(JOB, LEASE, playlist_lease_token=PLAYLIST,
                                                 scan_id=SCAN, transition_id=TRANSITION)
        control.assert_not_called()
        repo.database.transaction.assert_called_once()
        repo.database.call.assert_not_called()
        self.assertTrue(error.outcome_may_be_committed)


if __name__ == "__main__": unittest.main()
