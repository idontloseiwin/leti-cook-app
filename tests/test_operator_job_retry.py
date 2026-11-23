"""Explicit owner retry receipt, count snapshots and acceptance boundaries."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import re,sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_retry as h
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database,ID,CHANNEL
from channel_workflow_fixtures import stage_answers,stage_sql
REQUEST='f9700000-0000-4000-8000-000000000002';RAW='{"id":"'+ID+'","channel_id":"'+CHANNEL+'","summary_json":{"opaque":1e1000}}'


def args(full=False,**changes):
    values=dict(job_id=ID,request_id=REQUEST,max_items=None if full else 20,max_pages=None if full else 20,
        max_quota_units=None if full else 20,confirmed=True,expected_epoch=17);values.update(changes);return values
def prefix(full=False,**changes):
    return [args(full,**changes),dict(channel_id=CHANNEL),dict(channel_id=CHANNEL),dict(value=RAW),dict(version=1 if full else 0)]
def state(**changes):
    values=dict(invalid=False,below=False,mismatch=False,phase='inventory',target_count=3);values.update(changes);return values
def legacy(**changes):
    return [None,dict(invalid=False),dict(hidden=False),dict(invalid=False),dict(job=RAW),state(**changes),1,dict(present=False),1]
def counts(**changes):
    values=dict(total_count=3,pending_count=1,updated_count=1,skipped_count=0,unavailable_count=1,error_count=0);values.update(changes);return values
def mismatch():
    return [dict(invalid=False),dict(invalid=False),dict(value=3),dict(value=3),1,counts(),dict(value=4),dict(job=RAW),*stage_answers('retry_finish'),dict(value=RAW)]


class RetryTests(unittest.TestCase):
    def setUp(self):
        for name in ('_accept','assert_full_initial_scope','assert_channel_scope'):
            p=patch.object(h,name);setattr(self,name,p.start());self.addCleanup(p.stop)
    def call(self,db,full=False,**changes):
        values=dict(job_id=ID,retry_request_id=REQUEST,max_items=None if full else 20,max_pages=None if full else 20,
            max_quota_units=None if full else 20,confirmed=True,expected_epoch=17);values.update(changes)
        return h.retry_channel_initial_inventory_job_v5(db,**values)
    def test_native_cast_errors_precede_epoch_request_and_owner_checks(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:self.call(db,expected_epoch=None)
        self.assertIs(caught.exception,error);self.assertEqual([s for k,s,p in db.calls],[h._INPUT])
    def test_epoch_precedes_required_request_before_any_owner_lookup(self):
        for epoch in (None,0,-1):
            db=Database(args(expected_epoch=epoch,request_id=None))
            with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):self.call(db)
            self.assertEqual(len(db.calls),1)
        db=Database(args(request_id=None))
        with self.assertRaisesRegex(DatabaseError,'operator_job_retry_request_id_required'):self.call(db)
    def test_missing_channel_and_job_reject_in_global_lock_order(self):
        for index in (1,2,3):
            db=Database(*(prefix()[:index]+[None]))
            with self.assertRaisesRegex(DatabaseError,'operator_job_retry_not_available'):self.call(db)
            self.assertEqual(len(db.calls),index+1)
        self.assertIn('FOR UPDATE',h._CHANNEL);self.assertIn('FOR UPDATE',h._JOB)
    def test_legacy_receipt_replay_precedes_source_visibility_confirmation_and_cap(self):
        db=Database(*prefix(confirmed=False,max_items=None),dict(legacy_epoch_missing=False,not_full=True,conflict=False))
        self.assertEqual(self.call(db).encoded,RAW);self.assertEqual(db.calls[-1][1],h._REQUEST)
        self._accept.assert_not_called();self.assert_full_initial_scope.assert_not_called();self.assert_channel_scope.assert_not_called()
    def test_legacy_unverifiable_receipt_precedes_conflict(self):
        db=Database(*prefix(),dict(legacy_epoch_missing=True,conflict=True))
        with self.assertRaisesRegex(DatabaseError,'operator_job_retry_legacy_scope_unverifiable'):self.call(db)
        db=Database(*prefix(),dict(legacy_epoch_missing=False,conflict=True))
        with self.assertRaisesRegex(DatabaseError,'operator_job_retry_request_conflict'):self.call(db)
    def test_full_receipt_replay_ignores_current_confirmation_and_returns_captured_raw_job(self):
        db=Database(*prefix(True,confirmed=False),dict(not_full=False,conflict=False,legacy_epoch_missing=True))
        self.assertEqual(self.call(db,True).encoded,RAW);self.assertEqual(db.calls[-1][1],h._REQUEST);self.assert_full_initial_scope.assert_not_called()
    def test_full_receipt_requires_version_and_exact_limits_epoch(self):
        for row in (dict(not_full=True,conflict=False),dict(not_full=False,conflict=True)):
            db=Database(*prefix(True),row)
            with self.assertRaisesRegex(DatabaseError,'operator_job_retry_request_conflict'):self.call(db,True)
    def test_full_limits_precede_confirmation_and_confirmation_precedes_current_control(self):
        db=Database(*prefix(True,max_items=1,confirmed=False),None)
        with self.assertRaisesRegex(DatabaseError,'full_initial_limits_must_be_null'):self.call(db,True)
        db=Database(*prefix(True,confirmed=None),None)
        with self.assertRaisesRegex(DatabaseError,'operator_job_confirmation_required'):self.call(db,True)
        self.assertEqual(db.calls[-1][1],h._REQUEST)
    def test_full_availability_precedes_restart_and_proof(self):
        for invalid,restart,code in ((True,True,'operator_job_retry_not_available'),(False,True,'initial_inventory_restart_required')):
            db=Database(*prefix(True),None,dict(invalid=invalid,restart=restart,frozen=False))
            with self.assertRaisesRegex(DatabaseError,code):self.call(db,True)
        self.assert_full_initial_scope.assert_not_called()
    def test_full_fresh_retry_preserves_targets_and_source_clock_then_fences(self):
        db=Database(*prefix(True),None,dict(invalid=False,restart=False,frozen=True),1,dict(value=RAW))
        self.assertEqual(self.call(db,True).encoded,RAW)
        self.assertEqual([s for k,s,p in db.calls][-3:],[h._FULL_AVAILABLE,h._INSERT_FULL,h._FULL_UPDATE])
        self.assert_full_initial_scope.assert_called_once_with(db,RAW,True);self.assert_channel_scope.assert_called_once_with(db,CHANNEL,17)
        self._accept.assert_not_called();self.assertNotIn('requested_at=',h._FULL_UPDATE);self.assertNotIn('UPDATE public.operator_job_targets',h._FULL_UPDATE)
        self.assertIn("'full_retry_requested_at',clock_timestamp()",h._FULL_UPDATE)
    def test_full_proof_failure_occurs_before_receipt_insertion(self):
        db=Database(*prefix(True),None,dict(invalid=False,restart=False,frozen=False))
        self.assert_full_initial_scope.side_effect=DatabaseServerError('full_initial_scope_invalid',code='P0001')
        with self.assertRaises(DatabaseError):self.call(db,True)
        self.assertEqual(db.calls[-1][1],h._FULL_AVAILABLE);self.assert_channel_scope.assert_not_called()
    def test_legacy_expired_source_precedes_confirmation(self):
        db=Database(*prefix(confirmed=False),None,dict(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'operator_job_source_scope_expired'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._SOURCE);self.assertIn('clock_timestamp()',h._SOURCE)
    def test_legacy_confirmation_then_hidden_then_cap_order(self):
        for rows,code in (([None,dict(invalid=False)],'operator_job_confirmation_required'),
                ([None,dict(invalid=False),dict(hidden=True)],'operator_job_retry_not_available'),
                ([None,dict(invalid=False),dict(hidden=False),dict(invalid=True)],'operator_job_retry_cap_invalid')):
            db=Database(*prefix(confirmed=code!='operator_job_confirmation_required'),*rows)
            with self.assertRaisesRegex(DatabaseError,code):self.call(db)
        self._accept.assert_not_called()
    def test_normalized_status_is_native_captured_only_and_checked_before_observed_cap(self):
        db=Database(*prefix(),*legacy(invalid=True,below=True)[:6])
        with self.assertRaisesRegex(DatabaseError,'operator_job_retry_not_available'):self.call(db)
        self.assertIn("WHEN j.status='completed' AND j.phase='verify' AND j.error_count>0",h._NORMALIZE)
        self.assertIn("WHEN j.status IN('queued','running') AND j.accepted_epoch IS DISTINCT FROM",h._NORMALIZE)
        self.assertNotIn('UPDATE',h._NORMALIZE)
    def test_cap_below_observed_fails_before_other_job_mutation(self):
        db=Database(*prefix(),*legacy(below=True)[:6])
        with self.assertRaisesRegex(DatabaseError,'operator_job_retry_cap_below_observed'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._AVAILABLE)
    def test_active_new_generation_stops_after_stale_suspend_before_receipt(self):
        rows=legacy()[:8];rows[-1]=dict(present=True);db=Database(*prefix(),*rows)
        with self.assertRaisesRegex(DatabaseError,'operator_job_channel_has_active_job'):self.call(db)
        self.assertEqual([s for k,s,p in db.calls][-2:],[h._STALE,h._ACTIVE]);self.assertEqual(db.events,['rollback'])
    def test_inventory_retry_uses_captured_native_pending_count_without_target_reset(self):
        db=Database(*prefix(),*legacy(),dict(target_count=3,pending_count=2),dict(job=RAW,phase='inventory'),*stage_answers('retry_pending'),dict(value=RAW))
        result=self.call(db);self.assertEqual(result.encoded,RAW)
        sql=[s for k,s,p in db.calls];self.assertNotIn(h._RESET,sql);self.assertNotIn(h._PENDING,sql)
        self.assertEqual(sql[-(3+len(stage_sql('retry_pending'))):],[h._CURRENT_COUNTS,h._LEGACY_UPDATE,*stage_sql('retry_pending'),h._RESULT])
        self._accept.assert_called_once_with(db,dict(id=ID,channel_id=CHANNEL,expected_epoch=17))
    def test_channel_stage_receives_new_returned_job_not_prior_captured_job(self):
        latest = RAW.replace('1e1000', '2e1000')
        db=Database(*prefix(),*legacy(),dict(target_count=3,pending_count=2),
                    dict(job=latest,phase='inventory'),*stage_answers('retry_pending',changed=False),dict(value=latest))
        self.assertEqual(self.call(db).encoded,latest)
        proposal=next(call for call in db.calls if call[1]==stage_sql('retry_pending')[0])
        self.assertEqual(proposal[2],(latest,))
        self.assertNotEqual(proposal[2],(RAW,))
        self._accept.assert_called_once_with(db,dict(id=ID,channel_id=CHANNEL,expected_epoch=17))
        # Legacy acceptance owns its final control CAS; the full-only fence stays separate.
        self.assert_channel_scope.assert_not_called()
        self.assertEqual(db.events,['borrow','borrow','commit'])

    def test_hydration_retry_resets_only_errors_then_counts_but_keeps_frozen_target_count(self):
        db=Database(*prefix(),*legacy(phase='verify',target_count=7),2,dict(actual_count=3,pending_count=2),dict(job=RAW,phase='hydrate_videos'),*stage_answers('retry_finish'),dict(value=RAW))
        self.call(db);sql=[s for k,s,p in db.calls];update=db.calls[sql.index(h._LEGACY_UPDATE)][2]
        self.assertEqual((update['target_count'],update['actual_count'],update['pending_count']),(7,3,2));self.assertNotIn(h._CURRENT_COUNTS,sql)
        self.assertIn("status='error'",h._RESET);self.assertNotIn('heartbeat_at=',h._LEGACY_UPDATE)
    def test_initial_stats_inserts_inventory_long_then_resets_counts_actual_set(self):
        db=Database(*prefix(),*legacy(phase='initial_stats'),4,2,dict(actual_count=4,pending_count=3),dict(job=RAW,phase='hydrate_videos'),*stage_answers('retry_finish'),dict(value=RAW))
        self.call(db);sql=[s for k,s,p in db.calls]
        self.assertEqual(sql[14:17],[h._TARGET_INSERT,h._RESET,h._PENDING]);self.assertIn("inventory.video_type='long'",h._TARGET_INSERT)
        self.assertEqual(db.calls[sql.index(h._LEGACY_UPDATE)][2]['target_count'],4)
    def test_mismatch_receipt_precedes_owner_proof_then_two_separate_target_counts(self):
        db=Database(*prefix(),*legacy(mismatch=True,phase='verify'),*mismatch());self.call(db)
        sql=[s for k,s,p in db.calls]
        self.assertEqual(sql[13:],[h._INSERT,h._MISMATCH_OWNER,h._EVIDENCE,h._VERIFIED,h._COUNT,h._RESET,h._COUNTS,h._REQUIRED,h._MISMATCH_UPDATE,*stage_sql('retry_finish'),h._RESULT])
        self.assertIn('heartbeat_at=now()',h._MISMATCH_UPDATE)
    def test_mismatch_missing_native_evidence_precedes_integer_cast(self):
        db=Database(*prefix(),*legacy(mismatch=True),dict(invalid=False),dict(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'initial_hydration_workset_evidence_missing'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._EVIDENCE);self.assertEqual(db.events,['rollback'])
    def test_mismatch_native_overflow_precedes_target_snapshot(self):
        error=DatabaseServerError('database_operation_failed',code='22003')
        db=Database(*prefix(),*legacy(mismatch=True),dict(invalid=False),dict(invalid=False),error)
        with self.assertRaises(DatabaseError) as caught:self.call(db)
        self.assertIs(caught.exception,error);self.assertEqual(db.calls[-1][1],h._VERIFIED)
    def test_mismatch_precount_changed_precedes_cap_and_reset(self):
        db=Database(*prefix(max_items=1),*legacy(mismatch=True),dict(invalid=False),dict(invalid=False),dict(value=3),dict(value=2))
        with self.assertRaisesRegex(DatabaseError,'initial_hydration_workset_changed_after_verify'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._COUNT)
    def test_mismatch_count_over_cap_has_no_target_reset(self):
        db=Database(*prefix(max_items=2),*legacy(mismatch=True),dict(invalid=False),dict(invalid=False),dict(value=3),dict(value=3))
        with self.assertRaisesRegex(DatabaseError,'operator_job_retry_cap_below_observed'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._COUNT)
    def test_mismatch_postcount_loss_precedes_quota_and_rolls_back_receipt_reset(self):
        rows=mismatch()[:6];rows[-1]=counts(total_count=2);db=Database(*prefix(),*legacy(mismatch=True),*rows)
        with self.assertRaisesRegex(DatabaseError,'initial_hydration_workset_repair_incomplete'):self.call(db)
        self.assertEqual(db.calls[-1][1],h._COUNTS);self.assertEqual(db.events,['rollback'])
    def test_native_quota_overflow_precedes_job_channel_acceptance(self):
        error=DatabaseServerError('database_operation_failed',code='22003');db=Database(*prefix(),*legacy(mismatch=True),*mismatch()[:6],error)
        with self.assertRaises(DatabaseError) as caught:self.call(db)
        self.assertIs(caught.exception,error);self._accept.assert_not_called()
    def test_acceptance_cas_failure_rolls_back_receipt_job_and_channel_without_retry(self):
        db=Database(*prefix(),*legacy(),dict(target_count=3,pending_count=2),dict(job=RAW,phase='inventory'),*stage_answers('retry_pending'))
        self._accept.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaises(DatabaseError):self.call(db)
        self.assertEqual(db.events,['borrow','borrow','rollback']);self.assertEqual(len(db.operations),1)
    def test_borrowed_failure_poisons_owner_and_never_borrows_pool(self):
        db=Database(args(expected_epoch=None))
        with self.assertRaises(DatabaseError):self.call(db,transaction=db)
        self.assertEqual(db.events,['borrow','poison']);self.assertEqual(db.operations,[])
    def test_unknown_commit_has_no_implicit_receipt_replay(self):
        class LostAck(Database):
            @contextmanager
            def transaction(self,operation):
                with super().transaction(operation) as tx:yield tx
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
        db=LostAck(*prefix(True),None,dict(invalid=False,restart=False,frozen=False),1,dict(value=RAW))
        with self.assertRaises(DatabaseError) as caught:self.call(db,True)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(db.events,['commit']);self.assertEqual(len(db.operations),1)
    def test_owned_transaction_preserves_total_timeout_and_allowlisted_errors(self):
        db=Database(*prefix(),dict(legacy_epoch_missing=False,conflict=False));self.call(db)
        spec=h.get_operation('retry_channel_initial_inventory_job_v5');op=db.operations[0]
        self.assertEqual((op.name,op.statement_timeout_ms,op.total_timeout_ms,op.lock_timeout_ms,op.error_codes),
            (spec.name,spec.statement_timeout_ms,spec.statement_timeout_ms,spec.lock_timeout_ms,spec.error_codes))
    def test_all_native_statements_bind_and_pass_direct_statement_guard(self):
        returning={h._FULL_UPDATE,h._MISMATCH_UPDATE,h._LEGACY_UPDATE}
        for name,sql in vars(h).items():
            if not(name.startswith('_') and isinstance(sql,str) and sql.lstrip().startswith(('SELECT','WITH','INSERT','UPDATE'))):continue
            names=re.findall(r'%\(([^)]+)\)s',sql);params={key:None for key in names} if names else (None,)*len(re.findall('%s',sql))
            PostgresQuery(Transformer()).convert(sql,params);is_returning=sql in returning
            read=is_returning or sql.lstrip().startswith(('SELECT','WITH'))
            _reviewed_statement(sql,read=read,max_rows=1 if read else 0,returning=is_returning)
        source=Path(h.__file__).read_text();self.assertNotIn('.call(',source);self.assertNotIn('SELECT operator_private.',source)


if __name__=='__main__':unittest.main()
