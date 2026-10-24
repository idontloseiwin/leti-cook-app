"""Offline transition receipt/failure atomicity with the actual transaction driver."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.inventory import inventory_scan_steps as steps
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError, DatabaseServerError, DatabaseUnavailable, PostgresDatabase, PostgresConfig, Operation
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN

A = "UC0000000000000000000001"
SCAN = "f6300000-0000-4000-8000-000000000001"
LEASE = "f6300000-0000-4000-8000-000000000002"
TRANSITION = "f6300000-0000-4000-8000-000000000003"
STAMP = "2026-10-06T12:00:00+00:00"
NOW = datetime(2026, 10, 6, 12, 1, tzinfo=timezone.utc)
HASH = "a" * 32


def one(**values): return Result((values,))
def changed(count=1): return Result(has_columns=False, rowcount=count)
def inputs(**changes):
    return {"channel_id": A, "video_type": "short", "lease_token": LEASE, "scan_id": SCAN,
        "transition_id": TRANSITION, "expected_page_token": "next", "outcome": "error", "error_code": "provider_failed",
        "source_checked_at": STAMP, **changes}
def source(**changes):
    return {"channel_id": A, "video_type": "short", "runtime_enabled": True, "status": "leased", "lease_token": LEASE,
        "lease_expires_at": "2026-10-06T13:00:00+00:00", "active_scan_id": SCAN, "lease_channel_epoch": 7,
        "no_new_streak": 4, "attempt_count": 2, "discovery_error_count": 2, "product_exposure_ready": False,
        "inventory_state_origin": "typed_native", "inventory_status": "running", **changes}
def scan(**changes):
    return {"scan_id": SCAN, "channel_id": A, "video_type": "short", "status": "running", "resume_page_token": "next",
        "scan_kind": "initial_inventory", "token_history_verified": True, **changes}
def receipt(**changes):
    return {"scan_id": SCAN, "channel_id": A, "video_type": "short", "transition_kind": "failure",
        "source_checked_at": STAMP, "request_hash": HASH, "result": {"request_hash": HASH, "status": "failed", "replayed": False}, **changes}
def control(**changes):
    return {"channel_id": A, "owner_permission": "accepted", "is_hidden": False, "control_epoch": 7, **changes}


class InventoryFailureTests(unittest.TestCase):
    def setUp(self):
        self.events = []; self.connection = Connection(self.events); self.pool = Pool(self.connection, self.events)
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None, pool_factory=lambda config: self.pool,
            cadence_snapshot=CollectionSchedules().cadence_snapshot()).open()
        self.addCleanup(self.database.close)

    def queue(self, *rows): self.connection.results.extend(rows)
    def invoke(self, **changes): return steps.fail_typed_inventory_scan(self.database, **inputs(**changes))
    def start(self, values=None, stored_receipt=None):
        self.queue(one(value=values or inputs(), future=False), one(valid=True), one(value=HASH), one(lock=None),
                   Result() if stored_receipt is None else one(value=stored_receipt))
    def owned(self, discovery=None, stored_scan=None, *, full=False):
        self.queue(one(value=discovery or source()), one(active_scan_id=SCAN), one(value=stored_scan or scan()),
                   one(valid=True), one(valid=True), one(present=full), one(at=NOW))
    def finish(self, *, job=False, held=None, due="2026-10-06T14:01:00+00:00"):
        self.queue(one(present=job), changed(), one(next_due_at=due), changed(), one(valid=True), one(**(held or control())))
    def expect(self, code, callback, sqlstate="P0001"):
        with self.assertRaises(DatabaseServerError) as caught: callback()
        self.assertEqual((caught.exception.code, caught.exception.message), (sqlstate, code))

    def test_new_failure_locks_receipt_then_playlist_scan_and_releases_before_final_scope(self):
        self.start(); self.owned(); self.finish()
        result = self.invoke()
        self.assertEqual(result, {"scan_id": SCAN, "channel_id": A, "video_type": "short", "lease_token": LEASE,
            "transition_id": TRANSITION, "page_items": 0, "canonical_video_ids": [], "inserted": 0, "already_matching": 0,
            "provider_attempts": 1, "scan_retained": False, "resume_page_token": None, "status": "failed", "coverage": "partial",
            "scan_kind": "initial_inventory", "outcome": "error", "error_code": "provider_failed", "inventory_status": "error",
            "next_due_at": "2026-10-06T14:01:00+00:00", "replayed": False, "request_hash": HASH})
        statements = [query for query, _ in self.connection.queries]
        self.assertIn("pg_advisory_xact_lock", statements[3]); self.assertIn("typed_inventory_receipts", statements[4])
        self.assertIn("channel_playlist_state", statements[5]); self.assertIn("channel_video_inventory_scans", statements[7])
        self.assertIn("FOR SHARE", statements[-1]); self.assertIn("INSERT INTO public.typed_inventory_receipts", statements[-3])
        self.assertEqual(self.connection.queries[-3][1][3].obj, result)
        self.assertEqual((self.connection.commits, self.pool.borrowed), (1, 1))

    def test_exact_replay_precedes_current_lease_and_visibility_and_does_not_mutate(self):
        saved = receipt(result={"request_hash": HASH, "status": "failed", "immutable": [1], "replayed": False})
        self.start(stored_receipt=saved); self.queue(one(valid=True), one(valid=True), one(valid=True))
        self.assertEqual(self.invoke(), {**saved["result"], "replayed": True})
        self.assertEqual(len(self.connection.queries), 8)
        self.assertFalse(any("FOR UPDATE" in query or query.startswith("INSERT") for query, _ in self.connection.queries))
        self.assertEqual(self.connection.commits, 1)

    def test_missing_playlist_writer_uses_selected_type_snapshot(self):
        base=CollectionSchedules()
        for kind,seconds in (('long',1234),('short',2345),('stream',3456)):
            settings=replace(base,**{kind+'_inventory':replace(getattr(base,kind+'_inventory'),missing_playlist_seconds=seconds)})
            self.start(values=inputs(video_type=kind,outcome='playlist_not_found'))
            self.owned(discovery=source(video_type=kind),stored_scan=scan(video_type=kind))
            self.queue(changed(),one(next_due_at=STAMP),changed(),one(valid=True),one(**control()))
            with patch.object(self.database,'_cadence_snapshot',settings.cadence_snapshot()):
                self.invoke(video_type=kind,outcome='playlist_not_found')
            release=[params for sql,params in self.connection.queries if 'UPDATE public.channel_playlist_state' in sql][-1]
            self.assertEqual(release['next_due_at'],NOW+timedelta(seconds=seconds))

    def test_acknowledged_failure_keeps_deadline_without_schedule_recomputation(self):
        saved=receipt(result={'request_hash':HASH,'status':'failed','next_due_at':STAMP,'replayed':False})
        self.start(stored_receipt=saved);self.queue(one(valid=True),one(valid=True),one(valid=True))
        with patch.object(steps,'require_cadence_snapshot',side_effect=AssertionError('must not recompute')):
            self.assertEqual(self.invoke(),{**saved['result'],'replayed':True})

    def test_replay_rejects_expired_legacy_and_changed_request_before_owner_locks(self):
        cases = [(receipt(result={"source_expired": False}), "hot_inventory_receipt_source_expired", []),
                 (receipt(scan_id=LEASE), "typed_inventory_transition_id_reused", [one(valid=True)]),
                 (receipt(result={}), "typed_inventory_receipt_legacy_unverifiable", [one(valid=True)]),
                 (receipt(request_hash="b"*32), "typed_inventory_transition_id_reused", [one(valid=True)])]
        for saved, code, tail in cases:
            self.start(stored_receipt=saved); self.queue(*tail); self.expect(code, self.invoke)
        self.assertFalse(any("FOR UPDATE" in query for query, _ in self.connection.queries))

    def test_source_hot_clock_precedes_domain_identity_and_native_input_cast_precedes_both(self):
        self.queue(Result(execute_error=ServerFailure("22P02", "private uuid")))
        self.expect("database_operation_failed", lambda: self.invoke(lease_token="bad", channel_id=""), "22P02")
        self.queue(one(value=inputs(channel_id="", source_checked_at=None), future=None), one(valid=False))
        self.expect("hot_source_clock_out_of_window", lambda: self.invoke(channel_id="", source_checked_at=None), "22023")
        self.queue(one(value=inputs(error_code="bad!"), future=False), one(valid=True))
        self.expect("typed_inventory_failure_identity_invalid", lambda: self.invoke(error_code="bad!"))
        self.assertFalse(any("pg_advisory" in query for query, _ in self.connection.queries))

    def test_receipt_retention_is_rechecked_before_returning_replay(self):
        self.start(stored_receipt=receipt()); self.queue(one(valid=True), one(valid=True), one(valid=False))
        self.expect("hot_source_clock_out_of_window", self.invoke, "22023")
        self.assertEqual(self.connection.commits, 0)

    def test_lease_clock_is_sampled_after_scan_lock_and_current_source_assertion(self):
        self.start(); self.queue(one(value=source()), one(active_scan_id=SCAN), one(value=scan()), one(valid=True), one(valid=False))
        self.expect("typed_inventory_failure_lease_not_owned", self.invoke)
        self.assertIn("channel_video_inventory_scans", self.connection.queries[-3][0])
        self.assertIn("clock_timestamp()", self.connection.queries[-1][0])
        self.assertFalse(any(query.startswith("UPDATE") for query, _ in self.connection.queries))

    def test_mismatched_checkpoint_and_daily_quota_never_write_or_make_receipt(self):
        self.start(); self.queue(one(value=source()), one(active_scan_id=SCAN), one(value=scan(resume_page_token="different")), one(valid=True), one(valid=True))
        self.expect("typed_inventory_failure_checkpoint_mismatch", self.invoke)
        self.start(values=inputs(error_code="youtube_quota_exhausted"))
        self.queue(one(value=source()), one(active_scan_id=SCAN), one(value=scan()), one(valid=True), one(valid=True), one(present=False))
        self.expect("inventory_quota_defer_required", lambda: self.invoke(error_code="youtube_quota_exhausted"))
        self.assertFalse(any(query.startswith("UPDATE") for query, _ in self.connection.queries))

    def test_full_reconciliation_retains_and_full_job_overrides_other_format_policy(self):
        for kind, video_type, bound_job in (("full_reconciliation", "long", False), ("initial_inventory", "short", True)):
            self.start(values=inputs(video_type=video_type))
            self.owned(discovery=source(video_type=video_type), stored_scan=scan(video_type=video_type, scan_kind=kind))
            self.finish(job=bound_job)
            result = self.invoke(video_type=video_type)
            self.assertTrue(result["scan_retained"]); self.assertEqual(result["resume_page_token"], "next")
            self.assertEqual(result["inventory_status"], "running")
            write = self.connection.queries[-4][1]
            self.assertTrue(write["retain"])
            self.assertEqual(write["next_due_at"], NOW.replace(hour=16) if video_type == "long" else NOW.replace(hour=14))

    def test_rate_retains_verified_cursor_even_without_scalar_parent_and_waits_60s(self):
        self.start(values=inputs(error_code='provider_rate_limited'))
        self.owned()
        self.finish(due='2026-10-06T12:02:00+00:00')
        result=self.invoke(error_code='provider_rate_limited')
        self.assertTrue(result['scan_retained'])
        self.assertEqual(result['resume_page_token'],'next')
        self.assertEqual(result['inventory_status'],'running')
        self.assertEqual(self.connection.queries[-4][1]['next_due_at'],NOW.replace(minute=2))
        self.assertEqual(result['provider_attempts'],1)
        self.assertEqual(self.connection.commits,1)

    def test_invalid_token_history_never_queries_parent_override_or_retains(self):
        self.start(); self.owned(stored_scan=scan(token_history_verified=False))
        self.queue(changed(), one(next_due_at=STAMP), changed(), one(valid=True), one(**control()))
        self.assertFalse(self.invoke()["scan_retained"])
        self.assertFalse(any("JOIN public.operator_jobs" in query for query, _ in self.connection.queries))

    def test_playlist_missing_preserves_completed_exposure_and_does_not_increment_error(self):
        self.start(values=inputs(outcome="playlist_not_found")); self.owned(discovery=source(product_exposure_ready=True))
        self.queue(changed(), one(next_due_at=STAMP), changed(), one(valid=True), one(**control()))
        result = self.invoke(outcome="playlist_not_found")
        self.assertEqual(result["inventory_status"], "completed")
        write = self.connection.queries[-4][1]
        self.assertEqual(write["outcome"], "playlist_not_found")
        self.assertIn("CASE WHEN %(outcome)s='error' AND %(error_code)s<>'provider_rate_limited' THEN 1 ELSE 0 END", self.connection.queries[-4][0])

    def test_stale_final_scope_rolls_back_scan_source_and_receipt_together(self):
        self.start(); self.owned(); self.finish(held=control(control_epoch=8))
        self.expect("channel_collection_scope_lost", self.invoke)
        self.assertEqual((self.connection.commits, self.connection.rollbacks, self.pool.borrowed), (0, 1, 1))
        self.assertTrue(any("INSERT INTO public.typed_inventory_receipts" in query for query, _ in self.connection.queries))

    def test_lost_source_update_and_denied_receipt_abort_without_retries(self):
        self.start(); self.owned(); self.queue(one(present=False), changed(), Result())
        self.expect("typed_inventory_failure_lease_lost", self.invoke)
        self.start(); self.owned(); self.queue(one(present=False), changed(), one(next_due_at=STAMP),
            Result(execute_error=ServerFailure("42501", "private ACL detail")))
        self.expect("database_operation_failed", self.invoke, "42501")
        self.assertEqual((self.connection.commits, self.pool.borrowed), (0, 2))

    def test_unknown_commit_propagates_once_and_does_not_acknowledge(self):
        self.start(); self.owned(); self.finish()
        self.connection.commit_error = psycopg.OperationalError("private commit data")
        with self.assertRaises(DatabaseUnavailable) as caught: self.invoke()
        self.assertTrue(caught.exception.outcome_unknown); self.assertEqual(self.pool.borrowed, 1)

    def test_borrowed_domain_failure_cannot_be_caught_to_commit_prior_work(self):
        self.queue(changed(), one(value=inputs(error_code="bad!"), future=False), one(valid=True))
        with self.assertRaises(DatabaseError) as caught:
            with self.database.transaction(Operation("inventory_borrow_test")) as tx:
                tx.execute("UPDATE public.channels SET title='prior' WHERE false")
                with self.assertRaises(DatabaseServerError): steps.fail_scan_in_transaction(tx, **inputs(error_code="bad!"))
        self.assertEqual(caught.exception.message, "database_transaction_aborted")
        self.assertEqual(self.connection.commits, 0)


if __name__ == "__main__": unittest.main()
