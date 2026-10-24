"""Prepared control writers on the real bounded driver; no live PostgreSQL."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from db.channel import channel_control as control
from db.base.postgres import DatabaseError, DatabaseServerError, DatabaseUnavailable, PostgresConfig, PostgresDatabase
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN
from channel_workflow_fixtures import stage_reads, workflow_results, metadata_results

CHANNEL = "UC0000000000000000000001"
OWNER = "a0000000-0000-4000-8000-000000000001"
STAMP = "2026-10-05T12:00:00+00:00"
OLD_STAMP = "2026-10-04T12:00:00+00:00"


def state(**changes):
    return {"channel_id": CHANNEL, "user_work_id": OWNER, "owner_permission": "accepted", "is_hidden": False,
            "hidden_at": None, "control_epoch": 5, "initial_job_id": None, **changes}


def one(**row):
    return Result((row,))


def changed(count=1):
    return Result(has_columns=False, rowcount=count)


def create_reads(completed=False):
    rows = [one(matches=True), one(columns_text='{"new":true}', estimate_text=None),
            one(proposed_text='{"new":true}', completed=completed, errored=False, was_error=False, previous_count=0)]
    if completed:
        rows.append(one(proposed_text='{"normalized":true}'))
    return rows


class ChannelControlTransactionTests(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.connection = Connection(self.trace)
        self.pool = Pool(self.connection, self.trace)
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None,
                                         pool_factory=lambda config: self.pool).open()
        self.addCleanup(self.database.close)

    def queue(self, *results):
        self.connection.results.extend(results)

    def expect_error(self, name, action):
        with self.assertRaises(DatabaseServerError) as caught:
            action()
        self.assertEqual((caught.exception.code, caught.exception.message), ("P0001", name))

    def trash_input(self, *, hidden=False, matches=True, restorable=True, missing=False):
        self.queue(one(stamp=STAMP, user_work_id=OWNER, expected_hidden_at=OLD_STAMP),
                   Result() if missing else one(value=state(is_hidden=hidden, hidden_at=OLD_STAMP if hidden else None),
                                                hidden_at_matches=matches, restorable=restorable))

    def test_create_native_casts_omissions_python_owned_effects_and_known_commit(self):
        values = {"channel_id": CHANNEL, "title": "O'Brien; SELECT 'private'", "country_code": None}
        before = deepcopy(values)
        self.queue(*create_reads(), one(channel_id=CHANNEL), changed(), one(value={"channel_id": CHANNEL}, long_initialized=True))
        self.assertEqual(control.create_channel_for_collection(self.database, values, "accepted"), {"channel_id": CHANNEL})
        self.assertEqual(values, before)
        statements = [q for q, _ in self.connection.queries]
        self.assertIn('pg_get_expr', statements[0])
        self.assertIn('input."country_code"', statements[1])
        self.assertNotIn('input."added_at"', statements[1])
        self.assertIn("jsonb_populate_record", statements[1])
        self.assertNotIn(values["title"], statements[1])
        self.assertEqual(self.connection.queries[1][1][0].obj, values)
        self.assertEqual(statements[1].count('gen_random_uuid()'), 1)
        self.assertIn('INSERT INTO public.channels', statements[3])
        self.assertEqual(self.connection.queries[3][1], (None, '{"new":true}'))
        self.assertTrue(statements[4].startswith("UPDATE public.channel_collection_state SET owner_permission"))
        self.assertIn("INSERT INTO public.channel_playlist_state", statements[3])
        self.assertIn("INSERT INTO public.channel_snapshots", statements[3])
        self.assertFalse(any("public.create_channel_for_collection" in q for q in statements))
        self.assertEqual((self.connection.commits, self.pool.borrowed), (1, 1))

    def test_create_missing_canonical_or_trigger_owned_long_row_rolls_back(self):
        self.queue(*create_reads(), one(channel_id=CHANNEL), changed(0))
        self.expect_error("channel_collection_state_missing",
                          lambda: control.create_channel_for_collection(self.database, {"channel_id": CHANNEL}, "accepted"))
        self.queue(*create_reads(), one(channel_id=CHANNEL), changed(), one(value={"channel_id": CHANNEL}, long_initialized=False))
        with self.assertRaises(DatabaseError):
            control.create_channel_for_collection(self.database, {"channel_id": CHANNEL}, "accepted")
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 2))

    def test_create_completion_effect_precedes_insert_and_unknown_commit_is_not_retried(self):
        self.queue(*create_reads(completed=True), one(channel_id=CHANNEL), changed(),
                   one(value={'channel_id': CHANNEL}, long_initialized=True))
        self.connection.commit_error = psycopg.OperationalError('private commit acknowledgement')
        with self.assertRaises(DatabaseUnavailable) as caught:
            control.create_channel_for_collection(self.database,
                {'channel_id': CHANNEL, 'initial_long_inventory_status': 'completed'}, 'accepted')
        self.assertTrue(caught.exception.outcome_unknown)
        queries = self.connection.queries
        self.assertEqual(queries[4][1], (None, '{"normalized":true}'))
        self.assertEqual(sum('INSERT INTO public.channels' in q for q, _ in queries), 1)
        self.assertEqual(self.pool.borrowed, 1)

    def test_create_public_validation_before_any_dispatch(self):
        for values, permission in (({}, "accepted"), ({"channel_id": CHANNEL}, "legacy_hold"),
                                   ({"channel_id": CHANNEL, "is_hidden": 0}, "accepted"),
                                   ({"channel_id": CHANNEL, "hidden_at": STAMP}, "accepted"),
                                   ({"channel_id": CHANNEL, "initial_job_id": OWNER}, "accepted")):
            self.expect_error("channel_collection_create_invalid",
                              lambda: control.create_channel_for_collection(self.database, values, permission))
        self.assertEqual(self.pool.borrowed, 0)

    def test_update_writes_before_final_share_fence_and_commits_once(self):
        self.queue(changed(), Result((state(),)))
        self.assertEqual(control.update_channel_collection_fields(self.database, CHANNEL, {"title": None}, 5), 1)
        statements = [q for q, _ in self.connection.queries]
        self.assertTrue(statements[0].startswith("UPDATE public.channels"))
        self.assertIn("FOR SHARE", statements[1])
        self.assertEqual(self.connection.queries[1][1], ([CHANNEL],))
        self.assertEqual((self.connection.commits, self.pool.borrowed), (1, 1))

    def test_targeted_completed_update_uses_native_effect_before_final_scope(self):
        self.queue(*workflow_results('completed'), Result((state(),)))
        values = {'initial_long_inventory_status': 'completed', 'title': 'new'}
        self.assertEqual(control.update_channel_collection_fields(self.database, CHANNEL, values, 5), 1)
        queries = self.connection.queries
        self.assertIn('FOR NO KEY UPDATE OF channel', queries[0][0])
        self.assertEqual(queries[0][1][0].obj, values)
        self.assertIn('AS completed', queries[1][0])
        self.assertEqual(queries[3][1], ('{"normalized":true}', CHANNEL))
        self.assertIn('"initial_long_inventory_error_count"=proposed.', queries[3][0])
        self.assertIn('FOR SHARE', queries[6][0])
        self.assertEqual((self.connection.commits, self.pool.borrowed), (1, 1))

    def test_targeted_missing_row_still_reaches_scope_without_effect_or_write(self):
        self.queue(Result(), Result())
        self.expect_error('channel_collection_scope_lost', lambda: control.update_channel_collection_fields(
            self.database, CHANNEL, {'initial_long_inventory_status': 'completed'}, 5))
        self.assertEqual(len(self.connection.queries), 2)
        self.assertIn('FOR SHARE', self.connection.queries[-1][0])
        self.assertEqual(self.connection.rollbacks, 1)

    def test_update_stale_hidden_held_missing_fence_rolls_back_child_write(self):
        for held in (None, state(control_epoch=6), state(is_hidden=True), state(owner_permission="manual_required")):
            self.queue(changed(), Result() if held is None else Result((held,)))
            self.expect_error("channel_collection_scope_lost",
                              lambda: control.update_channel_collection_fields(self.database, CHANNEL, {"title": "new"}, 5))
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 4))

    def test_update_zero_affected_still_requires_scope_and_padded_id_is_not_trimmed(self):
        self.queue(changed(0), Result())
        self.expect_error("channel_collection_scope_lost",
                          lambda: control.update_channel_collection_fields(self.database, CHANNEL, {"title": "x"}, 5))
        self.queue(changed(0))
        self.expect_error("channel_collection_scope_invalid",
                          lambda: control.update_channel_collection_fields(self.database, " " + CHANNEL, {"title": "x"}, 5))
        self.assertEqual(self.connection.queries[-1][1][1], " " + CHANNEL)

    def test_update_validates_scope_before_fields_and_preserves_native_errors(self):
        self.expect_error("channel_collection_scope_invalid",
                          lambda: control.update_channel_collection_fields(self.database, None, {}, None))
        self.expect_error("channel_collection_fields_invalid",
                          lambda: control.update_channel_collection_fields(self.database, CHANNEL, {"is_hidden": True}, 5))
        self.queue(Result(execute_error=ServerFailure("42703")))
        with self.assertRaises(DatabaseServerError) as caught:
            control.update_channel_collection_fields(self.database, CHANNEL, {"title": "x"}, 5)
        self.assertEqual(caught.exception.code, "42703")
        self.assertEqual(len(self.connection.queries), 1)

    def test_permission_noop_does_not_allocate_epoch(self):
        self.queue(one(value=state()))
        self.assertEqual(control.set_channel_collection_permission(self.database, CHANNEL, 5, "accepted"),
                         {"channel_id": CHANNEL, "collection_permission": "accepted", "collection_control_epoch": 5})
        self.assertEqual(len(self.connection.queries), 1)
        self.assertIn("FOR UPDATE", self.connection.queries[0][0])

    def test_permission_change_allocates_once_and_preserves_other_control_fields(self):
        self.queue(one(value=state()), one(channel_id=CHANNEL, owner_permission="manual_required", control_epoch=9))
        result = control.set_channel_collection_permission(self.database, CHANNEL, 5, "manual_required")
        self.assertEqual(result["collection_control_epoch"], 9)
        sql = self.connection.queries[1][0]
        self.assertEqual(sql.count("nextval("), 1)
        self.assertNotIn("initial_job_id", sql)
        self.assertNotIn("channel_playlist_state", sql)

    def test_permission_rejects_stale_hidden_and_missing_without_write(self):
        for held in (None, state(is_hidden=True), state(control_epoch=9)):
            self.queue(Result() if held is None else one(value=held))
            self.expect_error("channel_collection_scope_lost",
                              lambda: control.set_channel_collection_permission(self.database, CHANNEL, 5, "accepted"))
        self.assertEqual(len(self.connection.queries), 3)

    def test_hide_captures_clock_before_lock_and_releases_bounded_work_in_order(self):
        self.trash_input()
        self.queue(changed(), changed(), changed(3))
        result = control.set_channel_trash_state(self.database, channel_id=CHANNEL)
        self.assertEqual(result, {"outcome": "removed", "channel_id": CHANNEL, "id": OWNER, "removed_at": STAMP})
        queries = self.connection.queries
        self.assertIn("clock_timestamp()", queries[0][0])
        self.assertIn("FOR UPDATE", queries[1][0])
        self.assertIn("UPDATE public.channel_collection_state", queries[2][0])
        self.assertIn("UPDATE public.operator_jobs", queries[3][0])
        self.assertIn("FOR UPDATE SKIP LOCKED", queries[3][0])
        self.assertIn("ORDER BY locked.video_type FOR UPDATE SKIP LOCKED", queries[4][0])
        self.assertIn("lease_channel_epoch = NULL", queries[3][0])
        self.assertIn("lease_run_id = NULL", queries[4][0])
        self.assertIn("interval '30 seconds'", queries[4][0])
        self.assertEqual(queries[4][1], (STAMP, STAMP, STAMP, CHANNEL))
        self.assertEqual((self.connection.commits, self.pool.borrowed), (1, 1))

    def test_repeated_hide_keeps_hidden_clock_epoch_but_still_releases_work(self):
        self.trash_input(hidden=True)
        self.queue(changed(0), changed(0))
        result = control.set_channel_trash_state(self.database, channel_id=CHANNEL)
        self.assertEqual(result["removed_at"], OLD_STAMP)
        self.assertEqual(len(self.connection.queries), 4)
        self.assertFalse(any("nextval(" in sql for sql, _ in self.connection.queries))

    def test_restore_uses_native_exact_clock_and_retention_open_cutoff(self):
        self.trash_input(hidden=True)
        self.queue(changed(), changed(0), changed(0))
        result = control.set_channel_trash_state(self.database, user_work_id=OWNER,
                                                 expected_hidden_at=OLD_STAMP, restore=True)
        self.assertEqual(result["outcome"], "restored")
        self.assertIsNone(result["removed_at"])
        self.assertIn("hidden_at IS NOT DISTINCT FROM", self.connection.queries[1][0])
        self.assertIn("hidden_at > %s::timestamptz - interval '696 hours'", self.connection.queries[1][0])
        self.assertIn("hidden_at <= %s::timestamptz", self.connection.queries[1][0])
        self.assertIn("is_hidden = false, hidden_at = NULL", self.connection.queries[2][0])

    def test_restore_rejection_outcomes_have_no_cleanup_or_writes(self):
        for kwargs, expected in (({"missing": True}, "not_found"), ({"hidden": False}, "conflict"),
                                  ({"hidden": True, "matches": False}, "conflict"),
                                  ({"hidden": True, "restorable": False}, "expired")):
            self.trash_input(**kwargs)
            self.assertEqual(control.set_channel_trash_state(self.database, user_work_id=OWNER,
                              expected_hidden_at=OLD_STAMP, restore=True), {"outcome": expected})
        self.assertEqual(len(self.connection.queries), 8)
        self.assertEqual(self.connection.commits, 4)

    def test_owner_identity_wins_when_both_identifiers_supplied(self):
        self.trash_input(hidden=True)
        self.queue(changed(), changed(0), changed(0))
        other = "UC0000000000000000000002"
        result = control.set_channel_trash_state(self.database, channel_id=other, user_work_id=OWNER,
                                                 expected_hidden_at=OLD_STAMP, restore=True)
        self.assertEqual(result["channel_id"], CHANNEL)
        self.assertEqual(self.connection.queries[1][1][-3:], (OWNER, OWNER, other))
        self.assertEqual(self.connection.queries[2][1], (CHANNEL,))

    def test_side_effect_failure_rolls_back_canonical_hide_and_is_not_retried(self):
        self.trash_input()
        self.queue(changed(), Result(execute_error=ServerFailure("P0001", "operator_job_source_scope_expired")))
        self.expect_error("operator_job_source_scope_expired",
                          lambda: control.set_channel_trash_state(self.database, channel_id=CHANNEL))
        self.assertEqual((self.connection.commits, self.connection.rollbacks, self.pool.borrowed), (0, 1, 1))

    def test_unknown_commit_propagates_without_replay(self):
        self.queue(one(value=state()))
        self.connection.commit_error = psycopg.OperationalError("private connection detail")
        with self.assertRaises(DatabaseUnavailable) as caught:
            control.set_channel_collection_permission(self.database, CHANNEL, 5, "accepted")
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual((self.pool.borrowed, len(self.connection.queries)), (1, 1))

    def test_timeout_and_lock_budgets_preserve_whole_rpc_envelope(self):
        for name in ("create_channel_for_collection", "update_channel_collection_fields", "set_channel_collection_permission"):
            op = control._operation(name)
            self.assertEqual((op.statement_timeout_ms, op.total_timeout_ms, op.lock_timeout_ms), (60000, 60000, 0))
        op = control._operation("set_channel_trash_state")
        self.assertEqual((op.statement_timeout_ms, op.total_timeout_ms, op.lock_timeout_ms), (5000, 5000, 250))

    def test_batch_scope_validates_all_before_lock_and_rejects_duplicate_or_fractional_epochs(self):
        for scopes in (None, {}, [state()], [{"channel_id": CHANNEL, "control_epoch": 5}] * 2,
                       [{"channel_id": CHANNEL, "control_epoch": 1.0}],
                       [{"channel_id": CHANNEL, "control_epoch": 9223372036854775808}],
                       [{"channel_id": CHANNEL, "control_epoch": True}],
                       [{"channel_id": CHANNEL, "control_epoch": 5}, {"channel_id": " padded", "control_epoch": 6}]):
            with self.assertRaisesRegex(DatabaseError, "database_transaction_aborted"):
                with self.database.transaction(control._operation("update_channel_collection_fields")) as tx:
                    self.expect_error("channel_collection_scope_invalid", lambda: control.assert_channel_scopes(tx, scopes))
        self.assertEqual(self.connection.queries, [])

    def test_batch_scope_one_sorted_query_empty_noop_and_native_scientific_epoch(self):
        other = "UC0000000000000000000002"
        self.queue(Result((state(), state(channel_id=other, control_epoch=10**18))))
        with self.database.transaction(control._operation("update_channel_collection_fields")) as tx:
            control.assert_channel_scopes(tx, [])
            control.assert_channel_scopes(tx, [{"channel_id": other, "control_epoch": 1e18},
                                              {"channel_id": CHANNEL, "control_epoch": 5}])
        self.assertEqual(len(self.connection.queries), 1)
        self.assertIn("ORDER BY channel_id FOR SHARE", self.connection.queries[0][0])
        self.assertEqual(self.connection.queries[0][1], ([other, CHANNEL],))

    def test_batch_scope_missing_or_stale_member_rolls_back_transaction(self):
        for rows in ((state(),), (state(), state(channel_id="other", control_epoch=7))):
            self.queue(Result(rows))
            with self.assertRaises(DatabaseServerError) as caught:
                with self.database.transaction(control._operation("update_channel_collection_fields")) as tx:
                    control.assert_channel_scopes(tx, [{"channel_id": CHANNEL, "control_epoch": 5},
                                                       {"channel_id": "other", "control_epoch": 6}])
            self.assertEqual(caught.exception.message, "channel_collection_scope_lost")
        self.assertEqual(self.connection.rollbacks, 2)

    def test_caught_borrowed_scope_failure_cannot_commit_prior_child_write(self):
        for invalid in (False, True):
            self.queue(changed())
            if not invalid:
                self.queue(Result((state(control_epoch=6),)))
            with self.assertRaisesRegex(DatabaseError, "database_transaction_aborted"):
                with self.database.transaction(control._operation("update_channel_collection_fields")) as tx:
                    tx.execute("UPDATE public.channels SET title=%s WHERE channel_id=%s", ("unsafe", CHANNEL))
                    try:
                        control.assert_channel_scope(tx, CHANNEL, 0 if invalid else 5)
                    except DatabaseServerError:
                        pass
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 2))


class ChannelControlTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = (ROOT / "schema/schema.sql").read_text(encoding="utf-8")

    def test_python_readiness_checks_the_exact_effective_acl(self):
        from db.readiness import capabilities
        # The reviewed effective-privilege predicate (from the retired SQL readiness function,
        # with nullable OIDs behind the explicit prerequisite) must stay exact in Python.
        predicate = (ROOT / "tests/fixtures/channel_control_acl_predicate.sql").read_text(encoding="utf-8")
        query = capabilities._QUERIES["assert_channel_sync_control_ready"]
        self.assertIn(predicate, query)
        self.assertIn("channel_collection_epoch_seq", capabilities._PREREQUISITES["assert_channel_sync_control_ready"][0])
        for expression in ("has_any_column_privilege", "has_column_privilege", "has_sequence_privilege",
                           "acl.is_grantable", "rolbypassrls", "a.attname=ANY", "'SELECT,UPDATE'"):
            self.assertIn(expression, predicate)
        self.assertIn("ARRAY['owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id']", predicate)

    def test_current25_transfers_workflow_and_retains_integrity(self):
        from db.readiness.intrinsic_capabilities import READINESS_SQL
        self.assertNotIn('FUNCTION public.assert_channel_sync_control_ready()',self.schema)
        self.assertIn('CREATE FUNCTION public.bind_channel_user_work_owner()',self.schema)
        self.assertIn('functions_valid',READINESS_SQL)
        self.assertIn('triggers_valid',READINESS_SQL)

    def test_schema_checksums_agree(self):
        manifest = json.loads((ROOT / "schema/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["schema.sql"], hashlib.sha256((ROOT / "schema/schema.sql").read_bytes()).hexdigest())
        self.assertEqual((ROOT / "schema/schema.sql.sha256").read_text(encoding="utf-8").strip(),
                         manifest["files"]["schema.sql"] + "  schema.sql")


if __name__ == "__main__":
    unittest.main()
