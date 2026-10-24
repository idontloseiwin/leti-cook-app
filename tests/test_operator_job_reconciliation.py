"""Offline protocol ordering for prepared borrowed full-initial writers."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_reconciliation as writer,operator_job_scope as scope
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database,ID,CHANNEL


def job(**changes):
    value=dict(id=ID,channel_id=CHANNEL,target_count=2,phase='inventory',summary_json={},targets_frozen_at=None)
    value.update(changes);return {'value':json.dumps(value)}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(writer,'assert_full_initial_scope');self.proof=self.patch.start();self.addCleanup(self.patch.stop)
    def test_incomplete_sources_return_original_after_proof_without_count_or_write(self):
        held=job();tx=Database(held,{'value':False})
        self.assertEqual(writer.finish_full_initial_inventory(tx,ID),json.loads(held['value']))
        self.proof.assert_called_once_with(tx,held['value'],False)
        self.assertEqual([sql for kind,sql,p in tx.calls],[writer._JOB,writer._DONE]);self.assertEqual(tx.events,['borrow'])
    def test_count_mismatch_precedes_confirmed_cast_and_poisons_owner(self):
        tx=Database(job(summary_json={'confirmed':'bad'}),{'value':True},{'value':1})
        with self.assertRaisesRegex(DatabaseError,'full_initial_target_count_mismatch'):writer.finish_full_initial_inventory(tx,ID)
        self.assertEqual(tx.calls[-1][1],writer._COUNT);self.assertEqual(tx.events,['borrow','poison'])
    def test_each_block_reason_uses_exact_native_block_statement_without_reset(self):
        for code in ('initial_inventory_confirmation_required','initial_inventory_identity_unresolved'):
            result=job(status='blocked');tx=Database(job(),{'value':True},{'value':2},{'code':code},result)
            self.assertEqual(writer.finish_full_initial_inventory(tx,ID),json.loads(result['value']))
            self.assertEqual(tx.calls[-1],('returning',writer._BLOCK,(code,ID)))
        self.assertNotIn('processed_count=',writer._BLOCK);self.assertNotIn('heartbeat_at=',writer._BLOCK)
    def test_success_keeps_quota_arithmetic_summary_merge_and_clocks_native(self):
        result=job(phase='hydrate_videos');tx=Database(job(),{'value':True},{'value':2},{'code':None},result)
        self.assertEqual(writer.finish_full_initial_inventory(tx,ID),json.loads(result['value']))
        self.assertEqual(tx.calls[-1],('returning',writer._FINISH,(ID,)))
        self.assertIn('quota_units_observed_estimate+2*((target_count+49)/50)',writer._FINISH)
        self.assertIn("summary_json=summary_json||jsonb_build_object",writer._FINISH)
        self.assertIn('targets_frozen_at=now()',writer._FINISH);self.assertNotIn('heartbeat_at=',writer._FINISH)
    def test_native_count_boolean_and_quota_errors_propagate_without_retry(self):
        for prefix in ((job(),{'value':True}), (job(),{'value':True},{'value':2}),
                       (job(),{'value':True},{'value':2},{'code':None})):
            error=DatabaseServerError('database_operation_failed',code='22003');tx=Database(*prefix,error)
            with self.assertRaises(DatabaseServerError) as caught:writer.finish_full_initial_inventory(tx,ID)
            self.assertIs(caught.exception,error);self.assertEqual(len(tx.calls),len(prefix)+1);self.assertEqual(tx.events[-1],'poison')
    def test_missing_job_still_delegates_null_composite_to_shared_clock_validation(self):
        tx=Database(None);error=DatabaseServerError('hot_source_clock_out_of_window',code='22023');self.proof.side_effect=error
        with self.assertRaises(DatabaseError) as caught:writer.finish_full_initial_inventory(tx,None)
        self.assertIs(caught.exception,error);self.proof.assert_called_once_with(tx,None,False)
    def test_reconcile_native_parameter_casts_precede_read_and_scope(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');tx=Database(error)
        with self.assertRaises(DatabaseError):writer.reconcile_full_initial_targets(tx,None,'bad')
        self.assertEqual(tx.calls,[('one',writer._INPUT,(None,'bad'))]);self.proof.assert_not_called()
    def test_scope_failure_precedes_invalid_limit_domain_check(self):
        held=job();tx=Database({'job_id':ID,'limit':0},held)
        self.proof.side_effect=DatabaseServerError('operator_job_source_scope_expired',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'operator_job_source_scope_expired'):writer.reconcile_full_initial_targets(tx,ID,0)
        self.assertEqual(len(tx.calls),2)
    def test_invalid_phase_frozen_or_limit_reject_before_target_update(self):
        tx=Database({'job_id':ID,'limit':0},job(),{'invalid':True})
        with self.assertRaisesRegex(DatabaseError,'full_initial_reconciliation_invalid'):writer.reconcile_full_initial_targets(tx,ID,0)
        self.assertEqual(tx.calls[-1][1],writer._VALIDATE);self.assertEqual(tx.events[-1],'poison')
    def test_partial_progress_requeues_after_one_set_update_and_remaining_read(self):
        result=job(status='queued');tx=Database({'job_id':ID,'limit':250},job(),{'invalid':False},250,{'value':True},result)
        self.assertEqual(writer.reconcile_full_initial_targets(tx,ID),json.loads(result['value']))
        self.assertEqual([sql for kind,sql,p in tx.calls],[writer._INPUT,writer._JOB,writer._VALIDATE,writer._RESOLVE,writer._REMAINING,writer._REQUEUE])
        self.assertEqual(tx.calls[3][2],dict(job_id=ID,channel_id=CHANNEL,limit=250))
        self.assertNotIn('processed_count=',writer._REQUEUE)
    def test_zero_progress_or_fully_resolved_delegates_finish_under_same_owner(self):
        for changed,remaining in ((0,True),(1,False),(0,False)):
            tx=Database({'job_id':ID,'limit':1},job(),{'invalid':False},changed,{'value':remaining})
            with patch.object(writer,'finish_full_initial_inventory',return_value={'status':'finished'}) as finish:
                self.assertEqual(writer.reconcile_full_initial_targets(tx,ID,1),{'status':'finished'})
                finish.assert_called_once_with(tx,ID)
            self.assertEqual(len(tx.calls),5)
    def test_reconciliation_preserves_opaque_native_json_through_finish_and_return(self):
        raw=job()['value'].replace('"summary_json": {}','"summary_json": {"opaque_large": 1e1000}')
        held={'value':raw};result_raw=raw.replace('"phase": "inventory"','"phase": "hydrate_videos"')
        tx=Database({'job_id':ID,'limit':250},held,{'invalid':False},0,{'value':False},
            held,{'value':True},{'value':2},{'code':None},{'value':result_raw})
        result=writer.reconcile_full_initial_targets(tx,ID)
        self.assertEqual(result.encoded,result_raw)
        self.assertEqual(result['summary_json']['opaque_large'],float('inf'))
        self.assertEqual([call.args for call in self.proof.call_args_list],[(tx,raw,False),(tx,raw,False)])
        for kind,sql,params in tx.calls:
            if sql==writer._VALIDATE:self.assertEqual(params,(250,raw))
            elif sql in {writer._DONE,writer._DECISION}:self.assertEqual(params,(raw,))
        self.assertEqual(len(tx.calls),10)
        self.assertEqual(tx.events,['borrow','borrow'])
    def test_null_limit_is_native_set_operation_without_python_row_materialization(self):
        tx=Database({'job_id':ID,'limit':None},job(),{'invalid':None},500,{'value':True},job())
        writer.reconcile_full_initial_targets(tx,ID,None)
        self.assertIsNone(tx.calls[3][2]['limit']);self.assertIn('LIMIT %(limit)s::integer',writer._RESOLVE)
        self.assertEqual([kind for kind,sql,p in tx.calls].count('execute'),1)
    def test_shared_identity_predicate_is_used_as_a_fixed_native_batch_expression(self):
        self.assertEqual(writer._BATCH_TYPE,scope._TYPE.replace('%(video_id)s::text','part.video_id'))
        self.assertIn('WITH part AS MATERIALIZED',writer._RESOLVE);self.assertIn('ORDER BY t.video_id LIMIT',writer._RESOLVE)
        self.assertIn('resolved.video_type IS NOT NULL',writer._RESOLVE)
        self.assertNotIn('operator_private.',writer._RESOLVE)
    def test_statements_bind_in_driver_and_do_not_open_new_transactions(self):
        for sql,params in ((writer._INPUT,(ID,250)),(writer._JOB,(ID,)),(writer._DONE,(Jsonb({}),)),
            (writer._COUNT,(ID,)),(writer._DECISION,(Jsonb({}),)),(writer._VALIDATE,(None,Jsonb({}))),(writer._REMAINING,(ID,))):
            _reviewed_statement(sql,read=True,max_rows=1);PostgresQuery(Transformer()).convert(sql,params)
        for sql,params in ((writer._BLOCK,('initial_inventory_identity_unresolved',ID)),(writer._FINISH,(ID,)),(writer._REQUEUE,(ID,))):
            _reviewed_statement(sql,read=True,max_rows=1,returning=True);PostgresQuery(Transformer()).convert(sql,params)
        _reviewed_statement(writer._RESOLVE,read=False,max_rows=0)
        PostgresQuery(Transformer()).convert(writer._RESOLVE,dict(job_id=ID,channel_id=CHANNEL,limit=250))
        source=(ROOT/'application/db/jobs/operator_job_reconciliation.py').read_text()
        for forbidden in ('database.call(','.transaction(', 'fetch_all('):self.assertNotIn(forbidden,source)
        for scalar in (writer._BLOCK,writer._FINISH,writer._REQUEUE):self.assertNotIn('clock_timestamp()',scalar)


if __name__=='__main__':unittest.main()
