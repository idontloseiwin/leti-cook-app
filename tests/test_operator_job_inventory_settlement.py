"""Focused borrowed full-initial receipt settlement; no PostgreSQL/provider."""
from pathlib import Path
import json,re,sys,unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_inventory_settlement as helper,operator_job_scope as scope
from db.jobs.operator_job_inventory_evidence import _NativeObject
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database,ID,CHANNEL
from channel_workflow_fixtures import stage_answers,stage_sql


def raw(**changes):return json.dumps(dict(dict(id=ID,channel_id=CHANNEL,summary_json={'opaque':0},inventory_video_type='long'),**changes))
def inputs(**changes):return dict(dict(job=raw(),job_id=ID,channel_id=CHANNEL,invalid=False,evidence='{}',ack='{}',
    reserved=1,pages=1,items=2,inserted=1,updated=1,attempts=1),**changes)
def progress(**changes):return dict(dict(results='{}',has_long=False,has_stream=False,next_type='long'),**changes)


class InventorySettlementTests(unittest.TestCase):
    def setUp(self):
        p=patch.object(helper,'assert_full_initial_scope');self.proof=p.start();self.addCleanup(p.stop)
    def call(self,tx,**changes):
        values=dict(job={},evidence={},ack={},reserved=1,pages=1,items=2,inserted=1,updated=1,attempts=1);values.update(changes)
        return helper.settle_full_initial_inventory(tx,**values)
    def ordinary(self,*,count=2,added=2,has_long=False,done=False,next_type='long',has_stream=False,code=None):
        result=raw(target_count=added,status='queued' if code is None else 'blocked')
        tx=Database(inputs(),{'ids':['v'+str(i).zfill(10) for i in range(count)],'oversized':False},added,0,
            {'done':done,'absent':False},{'proof':'{}'},progress(has_long=has_long,has_stream=has_stream,next_type=next_type),
            {'code':code},{'value':result},*([*stage_answers('full_settlement')] if has_long else []))
        return tx,result
    def test_ordered_bounded_targets_have_constant_query_count(self):
        for count in (0,1,50):
            tx,result=self.ordinary(count=count,added=count);self.assertEqual(self.call(tx),json.loads(result))
            self.assertEqual([s for k,s,p in tx.calls],[helper._INPUT,helper._IDS,helper._INSERT,helper._TARGETS,
                helper._FLAGS,helper._PROOF,helper._RESULTS,helper._CONFIRM,helper._UPDATE])
            self.assertEqual(tx.events,['borrow']);self.assertEqual(len(tx.calls),9)
            self.assertEqual(tx.calls[2][2]['ids'],['v'+str(i).zfill(10) for i in range(count)])
        self.assertIn('ORDER BY part.video_id',helper._INSERT);self.assertIn('ON CONFLICT(job_id,video_id) DO NOTHING',helper._INSERT)
    def test_casts_precede_precondition_and_proof(self):
        error=DatabaseServerError('database_operation_failed',code='22003');tx=Database(error)
        with self.assertRaises(DatabaseError) as caught:self.call(tx,reserved=2**31)
        self.assertIs(caught.exception,error);self.assertEqual(len(tx.calls),1);self.proof.assert_not_called()
        self.assertEqual(tx.events,['borrow','poison'])
    def test_checkpoint_rejection_precedes_scope_and_page(self):
        tx=Database(inputs(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'full_initial_job_checkpoint_invalid'):self.call(tx)
        self.proof.assert_not_called();self.assertEqual(len(tx.calls),1)
    def test_native_nullable_precondition_is_not_python_tightened(self):
        tx,result=self.ordinary();tx.answers[0]['invalid']=None
        self.call(tx);self.proof.assert_called_once_with(tx,tx.calls[2][2]['job'],False)
        self.assertIn("IS DISTINCT FROM 'bound'",helper._INPUT)
    def test_scope_proof_precedes_evidence_expansion(self):
        tx=Database(inputs());self.proof.side_effect=DatabaseServerError('full_initial_scope_invalid',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'full_initial_scope_invalid'):self.call(tx)
        self.assertEqual(len(tx.calls),1)
    def test_oversized_page_returns_no_ids_before_target_writes(self):
        tx=Database(inputs(),{'ids':None,'oversized':True})
        with self.assertRaisesRegex(DatabaseError,'full_initial_page_coverage_invalid'):self.call(tx)
        self.assertEqual(len(tx.calls),2);self.assertIn('CASE WHEN cardinality(ids)<=50 THEN ids END',helper._IDS)
    def test_native_scalar_array_failure_precedes_mutation(self):
        error=DatabaseServerError('database_operation_failed',code='22023');tx=Database(inputs(),error)
        with self.assertRaises(DatabaseError) as caught:self.call(tx)
        self.assertIs(caught.exception,error);self.assertEqual(tx.calls[-1][1],helper._IDS)
    def test_null_and_duplicate_ids_are_left_to_native_constraints_and_conflicts(self):
        tx,result=self.ordinary(count=3,added=1);tx.answers[1]['ids']=['same','same',None]
        self.call(tx);self.assertEqual(tx.calls[2][2]['ids'],['same','same',None]);self.assertEqual(tx.calls[-1][2]['added'],1)
        self.assertNotIn('DISTINCT',helper._IDS)
    def test_identity_reuses_exact_native_predicate_without_per_row_calls(self):
        self.assertEqual(helper._INSERT_TYPE,scope._TYPE.replace('%(video_id)s::text','part.video_id'))
        self.assertEqual(helper._UPDATE_TYPE,scope._TYPE.replace('%(video_id)s::text','target.video_id'))
        self.assertEqual(helper._TARGETS.count(helper._UPDATE_TYPE),2)
        self.assertIn("target.status='pending' AND target.job_attempts=0",helper._TARGETS)
    def test_boolean_and_proof_errors_follow_target_effects_and_precede_confirmed(self):
        for prior,last in (([inputs(),{'ids':[],'oversized':False},0,0],helper._FLAGS),
            ([inputs(),{'ids':[],'oversized':False},0,0,{'done':False,'absent':False}],helper._PROOF)):
            error=DatabaseServerError('database_operation_failed',code='22P02');tx=Database(*prior,error)
            with self.assertRaises(DatabaseError) as caught:self.call(tx)
            self.assertIs(caught.exception,error);self.assertEqual(tx.calls[-1][1],last);self.assertEqual(tx.events[-1],'poison')
    def test_channel_long_status_precedes_optional_finish_using_returned_owner(self):
        tx,result=self.ordinary(has_long=True,done=True,next_type='stream',has_stream=True)
        with patch.object(helper,'finish_full_initial_inventory',return_value={'finished':True}) as finish:
            self.assertEqual(self.call(tx),{'finished':True});finish.assert_called_once_with(tx,ID)
        self.assertEqual([s for k,s,p in tx.calls][-len(stage_sql('full_settlement')):],stage_sql('full_settlement'))
        self.assertEqual(tx.calls[-len(stage_sql('full_settlement'))][2],('{}',CHANNEL))
        self.assertEqual(tx.calls[-3][2],('{"normalized":true}',CHANNEL))
    def test_finish_requires_all_four_native_conditions(self):
        for values in ({'done':False,'next_type':'stream','has_stream':True},
            {'done':True,'next_type':'short','has_stream':True},{'done':True,'next_type':'stream','has_stream':False},
            {'done':True,'next_type':'stream','has_stream':True,'code':'initial_inventory_confirmation_required'}):
            tx,result=self.ordinary(**values)
            with patch.object(helper,'finish_full_initial_inventory') as finish:self.call(tx);finish.assert_not_called()
    def test_opaque_native_json_survives_all_statement_parameters_and_return(self):
        source=raw().replace('"opaque": 0','"opaque": 1e1000');tx,result=self.ordinary();tx.answers[0]['job']=source
        tx.answers[5]['proof']='{"opaque":1e1000}';tx.answers[6]['results']='{"opaque":1e1000}';tx.answers[-1]['value']=source
        value=self.call(tx,job=_NativeObject(source),evidence=_NativeObject('{"opaque":1e1000}'))
        self.assertEqual(value.encoded,source);self.assertEqual(tx.calls[0][2][-1],source)
        self.assertEqual(tx.calls[-1][2]['proof'],'{"opaque":1e1000}');self.assertEqual(tx.calls[-1][2]['results'],'{"opaque":1e1000}')
    def test_native_counts_and_transaction_clocks_stay_in_update(self):
        self.assertIn('x.quota_units_reserved-%(reserved)s::integer+%(attempts)s::integer',helper._UPDATE)
        self.assertIn('updated_at=now(),heartbeat_at=now()',helper._UPDATE)
        self.assertIn('j.target_count+%s::integer>10000',helper._CONFIRM)
        self.assertNotIn('clock_timestamp',helper._UPDATE)
    def test_all_statements_bind_in_driver_and_no_authority_is_acquired(self):
        for name,value in vars(helper).items():
            if name.startswith('_') and isinstance(value,str) and value.lstrip().startswith(('SELECT','WITH','INSERT','UPDATE')):
                names=re.findall(r'%\(([^)]+)\)s',value);params={key:None for key in names} if names else (None,)*len(re.findall('%s',value))
                PostgresQuery(Transformer()).convert(value,params)
                returning=name=='_UPDATE';read=returning or value.lstrip().startswith(('SELECT','WITH'))
                _reviewed_statement(value,read=read,max_rows=1 if read else 0,returning=returning)
        source=(ROOT/'application/db/jobs/operator_job_inventory_settlement.py').read_text()
        for forbidden in ('.transaction(','.call(', 'fetch_all(', 'operator_private.'):
            self.assertNotIn(forbidden,source)


if __name__=='__main__':unittest.main()
