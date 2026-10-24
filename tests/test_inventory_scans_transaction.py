"""Offline begin-scan protocol tests against the real transaction driver."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.inventory import inventory_scans as scans
from db.base.postgres import DatabaseError, DatabaseServerError, DatabaseUnavailable, Operation, PostgresConfig, PostgresDatabase
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN

CHANNEL = "UC0000000000000000000001"
SCAN = "f6000000-0000-4000-8000-000000000001"
LEASE = "f6000000-0000-4000-8000-000000000002"
BOUNDARY = "2026-10-05T12:00:00+00:00"


def one(**row): return Result((row,))
def changed(count=1): return Result(has_columns=False, rowcount=count)


def source(**changes):
    return {"channel_id": CHANNEL, "video_type": "short", "playlist_id": "UUSH0000000000000000000001",
            "runtime_enabled": True, "status": "leased", "lease_token": LEASE,
            "lease_expires_at": "2026-10-05T12:15:00+00:00", "active_scan_id": None,
            "lease_channel_epoch": 7, **changes}


def scan(**changes):
    return {"scan_id": SCAN, "channel_id": CHANNEL, "video_type": "short", "playlist_id": source()["playlist_id"],
            "scan_kind": "initial_inventory", "status": "running", "coverage": "partial",
            "scan_boundary_at": BOUNDARY, "started_at": BOUNDARY, "resume_page_token": "opaque",
            "pages_completed": 2, "items_seen": 90, "token_history_verified": True, **changes}


def scope(**changes):
    return {"channel_id": CHANNEL, "owner_permission": "accepted", "is_hidden": False, "control_epoch": 7, **changes}


class InventoryScanTransactionTests(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.connection = Connection(self.trace)
        self.pool = Pool(self.connection, self.trace)
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None,
            pool_factory=lambda config: self.pool).open()
        self.addCleanup(self.database.close)

    def queue(self, *rows): self.connection.results.extend(rows)

    def inputs(self, *, boundary_valid=True, **changes):
        self.queue(one(value={"scan_id": SCAN, "lease_token": LEASE, "scan_boundary_at": BOUNDARY, **changes},
                       boundary_valid=boundary_valid))

    def locked(self, *, discovery=None, existing=None, matches=True, active_scan_id=None):
        self.inputs()
        self.queue(one(value=discovery or source()), one(active_scan_id=active_scan_id),
                   Result() if existing is None else one(value=existing, boundary_matches=matches))

    def begin(self, **changes):
        values = dict(channel_id=CHANNEL, video_type="short", lease_token=LEASE, scan_id=SCAN,
                      scan_kind="initial_inventory", scan_boundary_at=BOUNDARY)
        return scans.begin_typed_inventory_scan(self.database, **{**values, **changes})

    def expect(self, error, action, code="P0001"):
        with self.assertRaises(DatabaseServerError) as caught: action()
        self.assertEqual((caught.exception.code, caught.exception.message), (code, error))

    def test_new_scan_retains_lock_write_fence_order_and_known_commit(self):
        self.locked()
        self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)), changed(), changed(), Result((scope(),)))
        result = self.begin(channel_id=" " + CHANNEL + " ")
        self.assertEqual(result, {"scan_id": SCAN, "status": "running", "coverage": "partial",
            "scan_kind": "initial_inventory", "scan_boundary_at": BOUNDARY, "resume_page_token": None,
            "pages_completed": 0, "items_seen": 0, "replayed": False})
        queries = [text for text, _ in self.connection.queries]
        self.assertIn("channel_playlist_state source", queries[1]); self.assertIn("FOR UPDATE", queries[1])
        self.assertIn("channel_video_inventory_state", queries[2])
        self.assertIn("channel_video_inventory_scans scan", queries[3]); self.assertIn("FOR UPDATE", queries[3])
        self.assertIn("clock_timestamp()", queries[4])
        self.assertTrue(queries[6].lstrip().startswith("INSERT INTO public.channel_video_inventory_scans"))
        self.assertTrue(queries[7].lstrip().startswith("UPDATE public.channel_playlist_state"))
        self.assertIn("ORDER BY channel_id FOR SHARE", queries[8])
        self.assertEqual(self.connection.queries[1][1], (CHANNEL, "short"))
        self.assertEqual((self.pool.borrowed, self.connection.commits), (1, 1))
        self.assertFalse(any("public.begin_typed_inventory_scan(" in text or "assert_hot_source_clock(" in text
                             or "typed_inventory_channel_eligible(" in text for text in queries))

    def test_completed_replay_precedes_lease_eligibility_source_and_final_scope(self):
        self.locked(discovery=source(runtime_enabled=False, status="paused", lease_token=None, lease_expires_at=None),
                    existing=scan(status="completed", coverage="complete", token_history_verified=False))
        result = self.begin()
        self.assertTrue(result["replayed"])
        self.assertEqual((result["status"], result["pages_completed"], result["items_seen"]), ("completed", 2, 90))
        self.assertEqual((len(self.connection.queries), self.connection.commits), (4, 1))

    def test_terminal_replay_identity_conflict_still_rejects_before_lease(self):
        for kwargs in ({"scan_kind": "discovery"}, {"playlist_id": "other"}):
            self.locked(existing=scan(status="completed", **kwargs))
            self.expect("typed_inventory_scan_identity_conflict", self.begin)
        self.locked(existing=scan(status="completed"), matches=False)
        self.expect("typed_inventory_scan_identity_conflict", self.begin)
        self.assertEqual((len(self.connection.queries), self.connection.rollbacks), (12, 3))

    def test_running_replay_rechecks_clocks_active_owner_and_final_epoch_without_writes(self):
        self.locked(discovery=source(active_scan_id=SCAN), existing=scan(), active_scan_id=SCAN)
        self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)), one(valid=True), one(valid=True), Result((scope(),)))
        result = self.begin()
        self.assertTrue(result["replayed"])
        self.assertEqual(result["resume_page_token"], "opaque")
        queries = [text for text, _ in self.connection.queries]
        self.assertEqual(sum("WITH observed AS MATERIALIZED" in text for text in queries), 2)
        self.assertTrue(all(text.startswith(("SELECT", "WITH")) for text in queries))
        self.assertIn("FOR SHARE", queries[-1])

    def test_running_token_history_error_precedes_source_clock_checks(self):
        self.locked(discovery=source(active_scan_id=SCAN), existing=scan(token_history_verified=False), active_scan_id=SCAN)
        self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)))
        self.expect("inventory_token_history_invalid", self.begin)
        self.assertEqual(len(self.connection.queries), 6)

    def test_running_source_clock_error_preserves_22023_and_precedes_active_ownership(self):
        for first in (True, False):
            self.locked(existing=scan())
            self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)))
            if not first: self.queue(one(valid=True))
            self.queue(one(valid=False))
            self.expect("hot_source_clock_out_of_window", self.begin, "22023")
        self.assertEqual(self.connection.rollbacks, 2)

    def test_running_active_identity_and_final_scope_are_mandatory(self):
        self.locked(existing=scan())
        self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)), one(valid=True), one(valid=True))
        self.expect("typed_inventory_active_scan_not_owned", self.begin)
        self.locked(discovery=source(active_scan_id=SCAN), existing=scan(), active_scan_id=SCAN)
        self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)), one(valid=True), one(valid=True), Result((scope(is_hidden=True),)))
        self.expect("channel_collection_scope_lost", self.begin)

    def test_missing_playlist_or_format_fail_in_retained_order(self):
        self.inputs(); self.queue(Result())
        self.expect("typed_inventory_discovery_state_missing", self.begin)
        self.inputs(); self.queue(one(value=source()), Result())
        self.expect("typed_inventory_format_state_missing", self.begin)
        self.assertEqual(len(self.connection.queries), 5)

    def test_null_video_type_reaches_missing_state_like_sql_unknown_not_in(self):
        self.inputs(); self.queue(Result())
        self.expect("typed_inventory_discovery_state_missing", lambda: self.begin(video_type=None))

    def test_native_uuid_error_precedes_domain_validation_and_is_not_retried(self):
        self.queue(Result(execute_error=ServerFailure("22P02", "private native detail")))
        self.expect("database_operation_failed", lambda: self.begin(channel_id="", lease_token="bad"), "22P02")
        self.assertEqual((len(self.connection.queries), self.pool.borrowed), (1, 1))

    def test_domain_identity_validation_follows_native_input_and_takes_no_locks(self):
        for changes in ({"channel_id": " "}, {"video_type": "video"}, {"scan_kind": "bad"}):
            self.inputs(); self.expect("typed_inventory_scan_identity_invalid", lambda: self.begin(**changes))
        self.inputs(boundary_valid=False)
        self.expect("typed_inventory_scan_identity_invalid", self.begin)
        self.assertEqual(len(self.connection.queries), 4)

    def test_lease_clock_sample_follows_potential_scan_lock_wait(self):
        self.locked(); self.queue(one(fresh=False))
        self.expect("typed_inventory_scan_lease_not_owned", self.begin)
        self.assertIn("FOR UPDATE", self.connection.queries[3][0])
        self.assertIn("clock_timestamp()", self.connection.queries[4][0])
        self.assertEqual(len(self.connection.queries), 5)

    def test_ineligible_channel_never_creates_scan(self):
        self.locked(); self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(False)))
        self.expect("typed_inventory_channel_not_eligible", self.begin)
        self.assertEqual(len(self.connection.queries), 6)

    def test_existing_active_scan_blocks_new_identity_before_generation_or_writes(self):
        self.locked(discovery=source(active_scan_id="other")); self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)))
        self.expect("typed_inventory_active_scan_conflict", self.begin)
        self.assertEqual(len(self.connection.queries), 6)

    def test_discovery_requires_successful_complete_full_generation(self):
        self.locked(); self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)), one(present=False))
        self.expect("typed_inventory_discovery_requires_full_generation", lambda: self.begin(scan_kind="discovery"))
        query = self.connection.queries[-1][0]
        self.assertIn("status='completed' AND coverage='complete'", query)
        self.assertIn("scan_kind IN ('initial_inventory','full_reconciliation')", query)

    def test_lost_bind_or_final_scope_rolls_back_insert_and_never_returns_receipt(self):
        self.locked(); self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)), changed(), changed(0))
        self.expect("typed_inventory_scan_lease_lost", self.begin)
        self.locked(); self.queue(one(fresh=True), one(policy_facts=native_eligible_facts(True)), changed(), changed(), Result())
        self.expect("channel_collection_scope_lost", self.begin)
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 2))

    def test_bind_preserves_prior_completion_legacy_origin_and_non_target_fields(self):
        self.assertIn("inventory_status='completed' OR last_completed_scan_id IS NOT NULL", scans._BIND)
        self.assertIn("inventory_state_origin='legacy_long_backfill' AND last_completed_scan_id IS NULL", scans._BIND)
        for field in ("source_checked_at", "product_exposure_ready", "lease_expires_at", "attempt_count"):
            self.assertNotIn(field, scans._BIND)

    def test_caught_borrowed_prevalidation_failure_poisoning_prevents_commit(self):
        self.queue(changed()); self.inputs(boundary_valid=False)
        with self.assertRaisesRegex(DatabaseError, "database_transaction_aborted"):
            with self.database.transaction(Operation("scan_probe_composed")) as tx:
                tx.execute("UPDATE public.channels SET title=%s WHERE channel_id=%s", ("prior", CHANNEL))
                try:
                    scans.begin_scan_in_transaction(tx, channel_id=CHANNEL, video_type="short", lease_token=LEASE,
                        scan_id=SCAN, scan_kind="initial_inventory", scan_boundary_at=BOUNDARY)
                except DatabaseServerError: pass
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))

    def test_unknown_commit_is_not_acknowledged_or_replayed(self):
        self.locked(existing=scan(status="completed"))
        self.connection.commit_error = psycopg.OperationalError("private connection detail")
        with self.assertRaises(DatabaseUnavailable) as caught: self.begin()
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual((self.pool.borrowed, len(self.connection.queries)), (1, 4))


class InventoryBeginRepositoryWiringTests(unittest.TestCase):
    def test_definite_denial_releases_once_but_denied_ack_replay_never_releases(self):
        from db.inventory.typed_inventory import TypedInventoryClaim, TypedInventoryRepository
        from hot_runtime import HotDatabaseAdmissionDenied
        claim = TypedInventoryClaim(CHANNEL, "short", source()["playlist_id"], LEASE, 7, BOUNDARY,
                                    0, None, None, "initial_inventory")
        for unknown_first in (False, True):
            denied = HotDatabaseAdmissionDenied("hot_runtime_unavailable")
            errors = ([DatabaseUnavailable("database_connection_failed", outcome_unknown=True)] if unknown_first else []) + [denied]
            database = SimpleNamespace(call=Mock(side_effect=AssertionError("begin RPC fallback forbidden")))
            with patch("db.inventory.typed_inventory.inventory_scans.begin_typed_inventory_scan", side_effect=errors) as helper, \
                 patch("db.inventory.typed_inventory.emit_event"), patch("db.hot.hot_collection_control.HotCollectionControlRepository.abort") as abort:
                with self.assertRaises(HotDatabaseAdmissionDenied) as caught:
                    TypedInventoryRepository(database).begin_scan(claim, scan_id=SCAN,
                        scan_boundary_at=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
            self.assertIs(caught.exception, denied)
            self.assertEqual(helper.call_count, 2 if unknown_first else 1)
            if unknown_first:
                abort.assert_not_called()
                self.assertTrue(denied.outcome_may_be_committed)
            else:
                abort.assert_called_once_with(playlists=[{"channel_id": CHANNEL, "video_type": "short", "lease_token": LEASE}])
            database.call.assert_not_called()

    def test_prepared_begin_preserves_owned_dispatch_telemetry_and_existing_bounded_replay(self):
        from db.inventory.typed_inventory import TypedInventoryClaim, TypedInventoryRepository
        claim = TypedInventoryClaim(CHANNEL, "short", source()["playlist_id"], LEASE, 7, BOUNDARY,
                                    0, None, None, "initial_inventory")
        for first in (None, DatabaseUnavailable("database_connection_failed", outcome_unknown=True),
                      DatabaseServerError("typed_inventory_scan_lease_not_owned", code="P0001")):
            database = SimpleNamespace(call=Mock(side_effect=AssertionError("begin RPC fallback forbidden")))
            expected = {"scan_id": SCAN, "status": "running"}
            responses = [expected] if first is None else [first, expected]
            with patch("db.inventory.typed_inventory.inventory_scans.begin_typed_inventory_scan", side_effect=responses) as helper, \
                 patch("db.inventory.typed_inventory.emit_event") as telemetry, \
                 patch("db.hot.hot_collection_control.dispatch_owned_rpc", side_effect=lambda database, name, params, action, **kwargs: action()) as dispatch:
                result = TypedInventoryRepository(database).begin_scan(claim, scan_id=SCAN,
                    scan_boundary_at=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
            self.assertEqual(result, expected)
            count = 1 if first is None else 2
            self.assertEqual((helper.call_count, dispatch.call_count), (count, count))
            self.assertEqual(helper.call_args.args, (database,))
            self.assertEqual(helper.call_args.kwargs, {"channel_id": CHANNEL, "video_type": "short", "lease_token": LEASE,
                "scan_id": SCAN, "scan_kind": "initial_inventory", "scan_boundary_at": BOUNDARY})
            self.assertEqual(dispatch.call_args_list[0].args[1], "begin_typed_inventory_scan")
            if first is not None:
                self.assertEqual(dispatch.call_args_list[1].kwargs, {"abort_on_denied": False})
                self.assertEqual(helper.call_args_list[0], helper.call_args_list[1])
            labels = [call.args[3] for call in telemetry.call_args_list]
            self.assertIn("rpc:begin_typed_inventory_scan", labels)
            if first is not None: self.assertIn("rpc-retry:begin_typed_inventory_scan", labels)
            database.call.assert_not_called()


if __name__ == "__main__": unittest.main()
