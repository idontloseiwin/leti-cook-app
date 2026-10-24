"""Offline atomicity/order tests for the prepared metadata/error family."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sys
import unittest

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.channel import channel_metadata_batch as metadata
from db.base.postgres import DatabaseError, DatabaseServerError, DatabaseUnavailable, PostgresConfig, PostgresDatabase
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN
from channel_workflow_fixtures import stage_reads, workflow_results, metadata_results

A, B = "UC0000000000000000000001", "UC0000000000000000000002"
JOB = "f6200000-0000-4000-8000-000000000001"


def one(**row): return Result((row,))
def changed(count=1): return Result(has_columns=False, rowcount=count)
def row(identity=A, epoch=7, **values): return {"channel_id": identity, "control_epoch": epoch, "values": values or {"title": "new"}}
def ordered(*rows): return Result(tuple({"row": item, "epoch": int(item["control_epoch"])} for item in rows))
def held(identity=A, epoch=7, **extra):
    return {"channel_id": identity, "control_epoch": epoch, "owner_permission": "accepted", "is_hidden": False,
            "initial_job_id": JOB, **extra}


class SavepointConnection(Connection):
    def __init__(self, events):
        super().__init__(events); self.depth = 0; self.savepoint_releases = self.savepoint_rollbacks = 0

    @contextmanager
    def transaction(self):
        self.depth += 1
        try:
            if self.depth == 1:
                with super().transaction(): yield self
            else:
                self.events.append(("savepoint",))
                try: yield self
                except BaseException:
                    self.savepoint_rollbacks += 1; self.events.append(("savepoint_rollback",)); raise
                else:
                    self.savepoint_releases += 1; self.events.append(("savepoint_release",))
        finally: self.depth -= 1


class ChannelMetadataTransactionTests(unittest.TestCase):
    def setUp(self):
        self.events = []; self.connection = SavepointConnection(self.events); self.pool = Pool(self.connection, self.events)
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None,
            pool_factory=lambda config: self.pool).open()
        self.addCleanup(self.database.close)

    def queue(self, *values): self.connection.results.extend(values)

    def expect(self, code, action, sqlstate="P0001"):
        with self.assertRaises(DatabaseServerError) as caught: action()
        self.assertEqual((caught.exception.code, caught.exception.message), (sqlstate, code))

    def test_batch_uses_db_order_native_values_savepoints_then_one_final_fence(self):
        first, second = row(A, title="O'Brien; not SQL", country_code=None), row(B, 8, views="123")
        inputs = [second, first]; before = deepcopy(inputs)
        self.queue(ordered(first, second), changed(), *metadata_results(B), Result((held(A), held(B, 8))))
        result = metadata.update_channel_metadata_batch(self.database, inputs)
        self.assertEqual(result, [{"channel_id": A, "status": "updated"}, {"channel_id": B, "status": "updated"}])
        self.assertEqual(inputs, before)
        queries = self.connection.queries
        self.assertIn("ORDER BY value->>'channel_id'", queries[0][0])
        self.assertEqual(queries[0][1][0].obj, inputs)
        self.assertIn('"country_code"=proposed."country_code"', queries[1][0])
        self.assertNotIn(first["values"]["title"], queries[1][0])
        self.assertEqual(queries[1][1], (queries[1][1][0], A))
        self.assertIn("ORDER BY channel_id FOR SHARE", queries[-1][0])
        self.assertEqual((self.connection.savepoint_releases, self.connection.commits, self.pool.borrowed), (2, 1, 1))

    def test_metadata_snapshot_finishes_before_final_channel_fence(self):
        from db.channel import channel_workflow_updates as workflow
        value=row(channel_metadata_status='ok',channel_metadata_checked_at='2026-10-06T01:00:00Z',views=123)
        self.queue(ordered(value),*metadata_results(A,capture=True),Result((held(),)))
        self.assertEqual(metadata.update_channel_metadata_batch(self.database,[value]),[{'channel_id':A,'status':'updated'}])
        queries=self.connection.queries
        self.assertIn('RETURNING to_jsonb(channel)::text AS new_text',queries[2][0])
        self.assertIn(workflow._SNAPSHOT_FACTS,queries[3][0])
        self.assertEqual(queries[4],(workflow._SNAPSHOT_INSERT,('{"metadata":"written"}',)))
        self.assertIn('FOR SHARE',queries[5][0])
        self.assertEqual((self.connection.savepoint_releases,self.connection.commits),(1,1))

    def test_native_per_row_failure_rolls_back_savepoint_and_continues_without_raw_error_text(self):
        first, second = row(), row(B, 8)
        self.queue(ordered(first, second), Result(execute_error=ServerFailure("23514", "private payload")),
                   changed(), Result((held(), held(B, 8))))
        result = metadata.update_channel_metadata_batch(self.database, [first, second])
        self.assertEqual(result, [{"channel_id": A, "status": "error", "error_code": "23514"},
                                  {"channel_id": B, "status": "updated"}])
        self.assertEqual((self.connection.savepoint_rollbacks, self.connection.savepoint_releases, self.connection.commits), (1, 1, 1))

    def test_targeted_effect_failure_recovers_only_after_row_savepoint_rollback(self):
        first, second = row(initial_long_inventory_status='completed'), row(B, 8)
        self.queue(ordered(first, second), one(**stage_reads('completed')[0]),
                   Result(execute_error=ServerFailure('22003', 'private native effect')),
                   changed(), Result((held(), held(B, 8))))
        result = metadata.update_channel_metadata_batch(self.database, [first, second])
        self.assertEqual(result, [{'channel_id': A, 'status': 'error', 'error_code': '22003'},
                                  {'channel_id': B, 'status': 'updated'}])
        self.assertIn('FOR NO KEY UPDATE OF channel', self.connection.queries[1][0])
        self.assertIn('AS completed', self.connection.queries[2][0])
        self.assertIn('FOR SHARE', self.connection.queries[-1][0])
        self.assertEqual((self.connection.savepoint_rollbacks, self.connection.savepoint_releases,
                          self.connection.commits, self.pool.borrowed), (1, 1, 1, 1))

    def test_stale_scope_after_per_row_work_rolls_back_whole_batch(self):
        first, second = row(), row(B, 8)
        self.queue(ordered(first, second), changed(), changed(), Result((held(), held(B, 9))))
        self.expect("channel_collection_scope_lost", lambda: metadata.update_channel_metadata_batch(self.database, [first, second]))
        self.assertEqual((self.connection.savepoint_releases, self.connection.commits, self.connection.rollbacks), (2, 0, 1))

    def test_missing_row_status_does_not_bypass_final_scope(self):
        self.queue(ordered(row()), changed(0), Result())
        self.expect("channel_collection_scope_lost", lambda: metadata.update_channel_metadata_batch(self.database, [row()]))

    def test_all_envelopes_precede_field_validation_and_locking(self):
        invalid = [None, {}, [row()] * 51, [row(), row()], [{**row(), "control_epoch": "7"}],
                   [{**row(), "control_epoch": 1.0}], [{**row(), "control_action": None}],
                   [{**row(), "control_action": "no_long_restart"}],
                   [row(forbidden="value"), {"channel_id": B, "control_epoch": 8, "values": {}}]]
        for value in invalid:
            self.expect("channel_metadata_batch_invalid", lambda: metadata.update_channel_metadata_batch(self.database, value))
        self.expect("channel_metadata_batch_field_invalid", lambda: metadata.update_channel_metadata_batch(self.database, [row(forbidden="value")]))
        self.assertEqual(self.connection.queries, [])

    def test_overflow_is_native_22003_before_any_channel_lock(self):
        self.queue(Result(execute_error=ServerFailure("22003", "private integer value")))
        self.expect("database_operation_failed", lambda: metadata.update_channel_metadata_batch(self.database,
            [{**row(), "control_epoch": 9223372036854775808}]), "22003")
        self.assertEqual((len(self.connection.queries), self.connection.savepoint_releases), (1, 0))

    def test_empty_batch_is_a_no_query_transaction(self):
        self.assertEqual(metadata.update_channel_metadata_batch(self.database, []), [])
        self.assertEqual((self.connection.queries, self.connection.commits), ([], 1))

    def test_discovery_update_locks_channel_then_playlist_and_retains_quota_trigger_skip(self):
        item = row(title="channel", channel_discovery_no_new_streak=-2)
        self.queue(ordered(item), one(channel_id=A), changed(), one(channel_id=A), one(policy_facts=native_policy_facts("active")),
                   changed(0), Result((held(),)))
        self.assertEqual(metadata.update_channel_metadata_batch(self.database, [item]), [{"channel_id": A, "status": "updated"}])
        queries = self.connection.queries
        self.assertIn("public.channels", queries[1][0]); self.assertIn("FOR UPDATE", queries[1][0])
        self.assertIn("public.channel_playlist_state", queries[3][0]); self.assertIn("FOR UPDATE", queries[3][0])
        self.assertEqual(queries[5][1]["values"].obj, {"no_new_streak": -2})
        self.assertTrue(queries[5][1]["eligible"])
        self.assertIn("greatest(coalesce(proposed.no_new_streak,0),0)", queries[5][0])
        self.assertNotIn("runtime_enabled=", queries[5][0])
        self.assertIn("current.lease_expires_at>now()", queries[5][0])

    def test_discovery_missing_rolls_back_channel_update_but_reports_per_row_code(self):
        item = row(title="channel", channel_discovery_next_after=None)
        self.queue(ordered(item), one(channel_id=A), changed(), Result(), Result((held(),)))
        self.assertEqual(metadata.update_channel_metadata_batch(self.database, [item]),
                         [{"channel_id": A, "status": "error", "error_code": "P0001"}])
        self.assertEqual((self.connection.savepoint_rollbacks, self.connection.commits), (1, 1))

    def test_stale_long_restart_is_rejected_without_stopping_other_channel_metadata(self):
        first = {**row(initial_long_inventory_status="pending", initial_28d_stats_status="pending"), "control_action": "no_long_restart"}
        second = row(B, 8)
        self.queue(ordered(first, second), one(status="skipped_no_long_playlist"), one(policy_facts=native_policy_facts("metadata_only")),
                   changed(), Result((held(), held(B, 8))))
        result = metadata.update_channel_metadata_batch(self.database, [second, first])
        self.assertEqual([item["status"] for item in result], ["error", "updated"])
        queries = self.connection.queries
        self.assertIn("ORDER BY channel_id FOR SHARE", queries[-1][0])
        self.assertFalse(any("initial_job_id=NULL" in query for query, _ in queries))
        self.assertEqual((self.connection.savepoint_rollbacks, self.connection.commits), (1, 1))

    def test_stale_long_count_cannot_hold_the_whole_accepted_channel(self):
        item = {**row(initial_long_inventory_status="manual_required", initial_28d_stats_status=None), "control_action": "no_long_manual_required"}
        self.queue(ordered(item), one(status="skipped_no_long_playlist"), one(policy_facts=native_policy_facts("metadata_only")),
                   Result((held(),)))
        self.assertEqual(metadata.update_channel_metadata_batch(self.database, [item])[0]["status"], "error")
        self.assertFalse(any("owner_permission='manual_required',initial_job_id=NULL" in query
                             for query, _ in self.connection.queries))
        self.assertEqual((self.connection.savepoint_rollbacks, self.connection.commits), (1, 1))

    def test_changed_action_source_is_per_row_error_but_changed_epoch_is_whole_batch_failure(self):
        item = {**row(initial_long_inventory_status="pending", initial_28d_stats_status="pending"), "control_action": "no_long_restart"}
        self.queue(ordered(item), one(status="completed"), one(policy_facts=native_policy_facts("active")), Result((held(),)))
        self.assertEqual(metadata.update_channel_metadata_batch(self.database, [item]),
                         [{"channel_id": A, "status": "error", "error_code": "P0001"}])
        self.queue(ordered(item), one(status="skipped_no_long_playlist"), one(policy_facts=native_policy_facts("metadata_only")),
                   Result((held(epoch=8),)))
        self.expect("channel_collection_scope_lost", lambda: metadata.update_channel_metadata_batch(self.database, [item]))

    def test_query_cancel_and_assertion_failure_are_not_per_row_errors(self):
        for code in ("57014", "P0004"):
            self.queue(ordered(row()), Result(execute_error=ServerFailure(code, "private detail")))
            self.expect("database_operation_failed", lambda: metadata.update_channel_metadata_batch(self.database, [row()]), code)
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 2))

    def test_transport_error_is_never_a_per_row_receipt_or_retry(self):
        self.queue(ordered(row()), Result(execute_error=psycopg.OperationalError("private socket")))
        with self.assertRaises(DatabaseUnavailable): metadata.update_channel_metadata_batch(self.database, [row()])
        self.assertEqual((self.pool.borrowed, self.connection.commits), (1, 0))

    def test_record_error_increments_natively_and_final_scope_fences_known_commit(self):
        value = {"channel_id": A, "error_code": "invalid_response", "error_at": "2026-10-06T01:00:00+00:00", "error_count": 3}
        self.queue(one(channel_id=A, error_code="invalid_response", epoch=7), *workflow_results('record_error',value=value), Result((held(),)))
        self.assertEqual(metadata.record_channel_initial_inventory_error(self.database, A, "invalid_response", 7), value)
        self.assertIn("initial_long_inventory_error_count+1", self.connection.queries[1][0])
        self.assertIn("(now())::timestamptz", self.connection.queries[1][0])
        self.assertIn("FOR SHARE", self.connection.queries[-1][0])
        self.assertEqual(self.connection.commits, 1)

    def test_clear_error_preserves_status_and_checks_missing_before_scope(self):
        self.queue(one(channel_id=A, epoch=7), one(channel_id=A), Result((held(),)))
        self.assertEqual(metadata.clear_channel_initial_inventory_error(self.database, A, 7), {"channel_id": A, "cleared": True})
        self.assertNotIn("initial_long_inventory_status=", self.connection.queries[1][0])
        self.queue(one(channel_id="", epoch=7), Result())
        self.expect("channel_sync_channel_not_found", lambda: metadata.clear_channel_initial_inventory_error(self.database, "", 7))
        self.assertEqual(len(self.connection.queries), 5)

    def test_record_validation_order_and_no_trimming_of_channel_identity(self):
        self.queue(one(channel_id="", error_code="bad!", epoch=0))
        self.expect("channel_collection_scope_invalid", lambda: metadata.record_channel_initial_inventory_error(self.database, "", "bad!", 0))
        self.queue(one(channel_id=" ", error_code="bad!", epoch=7))
        self.expect("channel_sync_channel_required", lambda: metadata.record_channel_initial_inventory_error(self.database, " ", "bad!", 7))
        self.queue(one(channel_id=A, error_code="bad!", epoch=7))
        self.expect("channel_sync_error_code_invalid", lambda: metadata.record_channel_initial_inventory_error(self.database, A, "bad!", 7))
        self.assertTrue(all("::bigint AS epoch" in query[0] for query in self.connection.queries))
        self.queue(one(channel_id=" " + A, error_code="error", epoch=7), Result())
        self.expect("channel_sync_channel_not_found", lambda: metadata.record_channel_initial_inventory_error(self.database, " " + A, "error", 7))
        self.assertEqual(self.connection.queries[-1][1], ("error", " " + A))

    def test_record_and_clear_stale_fences_roll_back_their_updates(self):
        self.queue(one(channel_id=A, error_code="error", epoch=7), *workflow_results('record_error',value={"channel_id": A}), Result((held(epoch=8),)))
        self.expect("channel_collection_scope_lost", lambda: metadata.record_channel_initial_inventory_error(self.database, A, "error", 7))
        self.queue(one(channel_id=A, epoch=7), one(channel_id=A), Result((held(is_hidden=True),)))
        self.expect("channel_collection_scope_lost", lambda: metadata.clear_channel_initial_inventory_error(self.database, A, 7))
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 2))

    def test_scalar_argument_native_cast_precedes_all_domain_validation(self):
        for clear in (False, True):
            action = (lambda value: metadata.clear_channel_initial_inventory_error(self.database, "", value)) if clear else (
                lambda value: metadata.record_channel_initial_inventory_error(self.database, "", "bad!", value))
            for value, code in ((9223372036854775808, "22003"), ("bad-bigint", "22P02")):
                self.queue(Result(execute_error=ServerFailure(code, "private cast input")))
                self.expect("database_operation_failed", lambda value=value: action(value), code)
                self.assertEqual(self.connection.queries[-1][1][-1], value)
            for value in (None, 0):
                self.queue(one(channel_id="", error_code="bad!", epoch=value))
                self.expect("channel_collection_scope_invalid", lambda value=value: action(value))
        self.assertTrue(all("::bigint AS epoch" in query[0] for query in self.connection.queries))
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 8))

    def test_scalar_string_epoch_is_native_accepted_and_unknown_commit_is_not_retried(self):
        for clear in (False, True):
            self.queue(one(channel_id=A, error_code="error", epoch=7),
                *([one(channel_id=A)] if clear else [*workflow_results('record_error',value={"channel_id": A})]), Result((held(),)))
            self.connection.commit_error = psycopg.OperationalError("private commit detail")
            action = (lambda: metadata.clear_channel_initial_inventory_error(self.database, A, "7")) if clear else (
                lambda: metadata.record_channel_initial_inventory_error(self.database, A, "error", "7"))
            before = self.pool.borrowed
            with self.assertRaises(DatabaseUnavailable) as caught: action()
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(self.pool.borrowed, before + 1)
            self.assertEqual(self.connection.queries[-3 if clear else -8][1][-1], "7")

    def test_unknown_commit_does_not_return_batch_result_or_replay(self):
        self.queue(ordered(row()), changed(), Result((held(),)))
        self.connection.commit_error = psycopg.OperationalError("private commit detail")
        with self.assertRaises(DatabaseUnavailable) as caught: metadata.update_channel_metadata_batch(self.database, [row()])
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual((self.pool.borrowed, self.connection.savepoint_releases), (1, 1))


if __name__ == "__main__": unittest.main()
