"""Ordering, native ownership and atomicity of prepared public page settlement."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import json,re,sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_inventory_page_settlement as helper
from db.jobs.operator_job_inventory_evidence import _NativeObject
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from test_operator_job_lifecycle import Database,ID,CHANNEL
from channel_workflow_fixtures import stage_answers,stage_sql
TOKEN='f8700000-0000-4000-8000-000000000002';TRANSITION='f8700000-0000-4000-8000-000000000003'
RAW=json.dumps(dict(id=ID,channel_id=CHANNEL,summary_json={'opaque':0}))


def inputs(**changes):
    result=dict(job_id=ID,lease_token=TOKEN,reserved=1,pages=1,items=2,inserted=1,updated=1,next_page_token=None,
        summary=json.dumps({'transition_id':TRANSITION}));result.update(changes);return result
def totals(**changes):
    result=dict(total_pages=1,total_items=2,actual_targets=0,required_quota=0,current_type='long',max_pages=20,max_items=100)
    result.update(changes);return result
def prefix(*,version=1,type_count=3,prepared=False,done=False,absent=False):
    return [inputs(),{'invalid':False},{'transition':TRANSITION,'request_hash':'hash'},None,
        {'channel_id':CHANNEL},{'channel_id':CHANNEL},{'value':RAW},
        dict(epoch=17,accepted_epoch=17,version=version,type_count=type_count,invalid=False),
        {'invalid':False,'checkpoint_phase':'prepared' if prepared else 'bound'},
        *([{'invalid':False}] if prepared else [{'invalid':False},{'invalid':False},{'value':1}]),
        {'done':done,'absent':absent},{'value':'{"server":"ack"}'}]


class PageSettlementTests(unittest.TestCase):
    def setUp(self):
        for name in ('assert_channel_scope','initial_inventory_checkpoint_evidence','settle_full_initial_inventory'):
            p=patch.object(helper,name);setattr(self,name,p.start());self.addCleanup(p.stop)
        self.initial_inventory_checkpoint_evidence.return_value=_NativeObject('{"native":1e1000}')
        self.settle_full_initial_inventory.return_value=_NativeObject(RAW)
    def call(self,db,**changes):
        args=dict(job_id=ID,lease_token=TOKEN,reserved_units=1,pages_processed=1,items_processed=2,
            inserted=1,updated=1,next_page_token=None,summary={'transition_id':TRANSITION})
        args.update(changes);return helper.settle_initial_inventory_job_chunk(db,**args)
    def test_native_casts_then_validation_hash_and_unlocked_replay(self):
        db=Database(inputs(),{'invalid':False},{'transition':TRANSITION,'request_hash':'h'},
            {'job':RAW,'replayed':True,'conflict':False},{'value':'{"inventory_ack_replayed":true}'})
        self.assertEqual(self.call(db),{'inventory_ack_replayed':True})
        self.assertEqual([s for k,s,p in db.calls],[helper._INPUT,helper._VALIDATE,helper._HASH,helper._REPLAY,helper._REPLAY_RESULT])
        self.assertFalse(any('FOR UPDATE' in s for k,s,p in db.calls));self.initial_inventory_checkpoint_evidence.assert_not_called()
        self.assert_channel_scope.assert_not_called();self.assertEqual(db.events,['commit'])
    def test_recorded_request_conflict_precedes_all_locks_and_generation(self):
        db=Database(inputs(),{'invalid':False},{'transition':TRANSITION,'request_hash':'h'},{'job':RAW,'replayed':True,'conflict':True})
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_job_ack_conflict'):self.call(db)
        self.assertEqual(len(db.calls),4);self.assertEqual(db.events,['rollback'])
    def test_native_invalid_argument_beats_summary_and_missing_job(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:self.call(db,pages_processed='bad',summary=None)
        self.assertIs(caught.exception,error);self.assertEqual(len(db.calls),1)
    def test_invalid_summary_count_or_cursor_stops_before_hash(self):
        db=Database(inputs(),{'invalid':True})
        with self.assertRaisesRegex(DatabaseError,'operator_job_inventory_settlement_invalid'):self.call(db)
        self.assertEqual(len(db.calls),2)
        self.assertIn('AS MATERIALIZED',helper._VALIDATE)
        self.assertNotIn('%(summary)s::jsonb-',helper._VALIDATE)
    def test_hash_keeps_native_jsonb_uuid_integer_coercion_and_order(self):
        self.assertIn('md5(jsonb_build_array(%(job_id)s::uuid,%(lease_token)s::uuid',helper._HASH)
        self.assertIn('%(reserved)s::integer,%(pages)s::integer,%(items)s::integer,%(inserted)s::integer,%(updated)s::integer)::text)',helper._HASH)
        self.assertNotIn('json.dumps',Path(helper.__file__).read_text())
    def test_full_path_keeps_channel_job_evidence_then_borrowed_full_writer_final_fence(self):
        db=Database(*prefix());value=self.call(db)
        statements=[s for k,s,p in db.calls]
        self.assertEqual(statements[4:9],[helper._CHANNEL_ID,helper._CHANNEL,helper._JOB,helper._LEASE,helper._CHECKPOINT])
        self.assertEqual(statements[9:],[helper._RECEIPT,helper._COUNTS,helper._ATTEMPTS,helper._FLAGS,helper._ACK])
        self.initial_inventory_checkpoint_evidence.assert_called_once_with(db,RAW)
        self.settle_full_initial_inventory.assert_called_once_with(db,RAW,'{"native":1e1000}',
            '{"server":"ack"}',1,1,2,1,1,1)
        self.assert_channel_scope.assert_called_once_with(db,CHANNEL,17)
        self.assertEqual(value.encoded,RAW);self.assertEqual(db.events,['commit'])
    def test_lease_clock_is_evaluated_after_blocking_job_lock(self):
        self.assertIn('clock_timestamp()',helper._LEASE);self.assertNotIn('<=now()',helper._LEASE)
        self.assertNotIn('clock_timestamp',helper._JOB)
        self.assertIn('lease_token IS DISTINCT FROM',helper._LEASE)
        self.assertIn('accepted_epoch IS DISTINCT FROM j.lease_channel_epoch',helper._LEASE)
    def test_missing_channel_or_job_rejects_before_evidence(self):
        for cutoff in (4,5,6):
            rows=prefix()[:cutoff]+[None];db=Database(*rows)
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call(db)
        self.initial_inventory_checkpoint_evidence.assert_not_called()
    def test_bad_post_lock_lease_precedes_checkpoint_cast_or_receipt(self):
        rows=prefix()[:8];rows[-1]['invalid']=True;db=Database(*rows)
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call(db)
        self.assertEqual(db.calls[-1][1],helper._LEASE);self.initial_inventory_checkpoint_evidence.assert_not_called()
    def test_checkpoint_mismatch_precedes_child_evidence(self):
        rows=prefix()[:9];rows[-1]['invalid']=True;db=Database(*rows)
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_job_checkpoint_mismatch'):self.call(db)
        self.initial_inventory_checkpoint_evidence.assert_not_called()
    def test_prepared_terminal_path_has_no_provider_attempt_or_bound_count_cast(self):
        db=Database(*prefix(prepared=True,done=True));self.call(db)
        statements=[s for k,s,p in db.calls]
        self.assertIn(helper._TERMINAL,statements);self.assertNotIn(helper._ATTEMPTS,statements);self.assertNotIn(helper._COUNTS,statements)
        self.assertEqual(self.settle_full_initial_inventory.call_args.args[-1],0)
    def test_terminal_invalid_stops_before_flags_and_ack(self):
        rows=prefix(prepared=True)[:10];rows[-1]['invalid']=True;db=Database(*rows)
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_terminal_evidence_missing'):self.call(db)
        self.assertEqual(db.calls[-1][1],helper._TERMINAL)
    def test_receipt_kind_failure_precedes_count_cast_failure(self):
        rows=prefix()[:10];rows[-1]['invalid']=True;db=Database(*rows)
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_job_receipt_required'):self.call(db)
        self.assertEqual(db.calls[-1][1],helper._RECEIPT)
    def test_receipt_count_failure_precedes_attempt_and_flags(self):
        rows=prefix()[:11];rows[-1]['invalid']=True;db=Database(*rows)
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_job_receipt_counts_mismatch'):self.call(db)
        self.assertEqual(db.calls[-1][1],helper._COUNTS)
    def test_long_absence_has_its_own_completion_and_channel_branch(self):
        db=Database(*prefix(version=0,type_count=1,absent=True),{'value':RAW},*stage_answers('page_absent'))
        self.call(db);self.assertEqual([s for k,s,p in db.calls][-(1+len(stage_sql('page_absent'))):],[helper._ABSENT,*stage_sql('page_absent')])
        self.assertNotIn(helper._TOTALS,[s for k,s,p in db.calls]);self.assert_channel_scope.assert_called_once()
    def test_long_partial_updates_job_then_pending_channel(self):
        db=Database(*prefix(version=0,type_count=1),totals(),{'blocked':False},{'value':RAW},*stage_answers('page_pending'))
        self.call(db);self.assertEqual([s for k,s,p in db.calls][-(3+len(stage_sql('page_pending'))):],[helper._TOTALS,helper._CAP,helper._UPDATE_LONG,*stage_sql('page_pending')])
    def test_long_cap_records_shared_error_before_final_fence(self):
        db=Database(*prefix(version=0,type_count=1),totals(),{'blocked':True},{'value':RAW},*stage_answers('record_error',value={}))
        self.call(db);self.assertEqual([s for k,s,p in db.calls][-len(stage_sql('record_error')):],stage_sql('record_error'))
        self.assertEqual(db.calls[-len(stage_sql('record_error'))][2],('initial_hydration_estimate_exceeded',CHANNEL))
        self.assert_channel_scope.assert_called_once()
    def test_long_finish_inserts_then_counts_native_quota_and_retains_blocked_finish(self):
        db=Database(*prefix(version=0,type_count=1,done=True),totals(),{'blocked':True},2,{'value':2},{'value':3},
            {'value':True},{'value':RAW},*stage_answers('page_finish'))
        self.call(db);statements=[s for k,s,p in db.calls]
        self.assertEqual(statements[-(5+len(stage_sql('page_finish'))):],[helper._INSERT_LONG,helper._TARGET_COUNT,helper._QUOTA,helper._TARGET_CAP,helper._UPDATE_LONG,*stage_sql('page_finish')])
        self.assertTrue(db.calls[-(1+len(stage_sql('page_finish')))][2]['blocked']);self.assertTrue(db.calls[-(1+len(stage_sql('page_finish')))][2]['finished'])
    def test_all_format_absence_catalogue_conflict_precedes_progress_and_freeze(self):
        db=Database(*prefix(version=0,absent=True,done=True),totals(),{'blocked':False},{'present':True},
            {'results':'{}','next_type':'long','finished':False},{'value':RAW},{'value':False})
        self.call(db);self.assertEqual(db.calls[-2][2]['code'],'initial_inventory_absence_conflicts_with_catalogue')
        self.assertNotIn(helper._INSERT_ALL,[s for k,s,p in db.calls])
    def test_all_identity_conflict_blocks_without_materializing_targets(self):
        db=Database(*prefix(version=0,done=True),totals(current_type='stream'),{'blocked':False},
            {'results':'{}','next_type':'stream','finished':True},{'invalid':True},{'value':RAW},{'value':False})
        self.call(db);args=db.calls[-2][2]
        self.assertEqual(args['code'],'initial_inventory_identity_unresolved');self.assertFalse(args['finished'])
        self.assertNotIn(helper._INSERT_ALL,[s for k,s,p in db.calls])
    def test_all_finish_keeps_separate_identity_insert_count_and_late_channel_write(self):
        db=Database(*prefix(version=0,done=True),totals(current_type='stream'),{'blocked':False},
            {'results':'{"long":{}}','next_type':'stream','finished':True},{'invalid':False},3,{'value':3},{'value':3},
            {'value':False},{'value':RAW},{'value':True},*stage_answers('page_all'))
        self.call(db);statements=[s for k,s,p in db.calls]
        self.assertEqual(statements[-(7+len(stage_sql('page_all'))):],[helper._IDENTITY,helper._INSERT_ALL,helper._TARGET_COUNT,helper._QUOTA,
            helper._TARGET_CAP,helper._UPDATE_ALL,helper._HAS_LONG,*stage_sql('page_all')])
    def test_native_unknown_boolean_is_not_rejected_or_rewritten_by_python(self):
        rows=prefix(version=0,type_count=1,done=None,absent=None);db=Database(*rows,totals(),{'blocked':None},{'value':RAW},*stage_answers('page_pending'))
        self.call(db);self.assertIsNone(db.calls[-(1+len(stage_sql('page_pending')))][2]['blocked']);self.assertIsNone(db.calls[-(1+len(stage_sql('page_pending')))][2]['finished'])
    def test_shared_full_failure_rolls_back_and_precedes_fence(self):
        db=Database(*prefix());self.settle_full_initial_inventory.side_effect=DatabaseServerError('full_initial_page_coverage_invalid',code='P0001')
        with self.assertRaises(DatabaseError):self.call(db)
        self.assert_channel_scope.assert_not_called();self.assertEqual(db.events,['rollback'])
    def test_final_fence_failure_rolls_back_and_does_not_retry(self):
        db=Database(*prefix());self.assert_channel_scope.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaises(DatabaseError):self.call(db)
        self.settle_full_initial_inventory.assert_called_once();self.assertEqual(db.events,['rollback'])
    def test_borrowed_owner_and_unknown_commit_have_no_implicit_replay(self):
        db=Database(*prefix());self.call(db,transaction=db);self.assertEqual(db.events,['borrow']);self.assertEqual(db.operations,[])
        class LostAck(Database):
            @contextmanager
            def transaction(self,operation):
                with super().transaction(operation) as tx:yield tx
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
        db=LostAck(*prefix())
        with self.assertRaises(DatabaseError) as caught:self.call(db)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(len(db.operations),1);self.assertEqual(db.events,['commit'])
    def test_owned_operation_uses_retained_single_total_budget(self):
        db=Database(*prefix());self.call(db);spec=helper.get_operation('settle_initial_inventory_job_chunk')
        self.assertEqual(db.operations[0].total_timeout_ms,spec.statement_timeout_ms)
        self.assertEqual(db.operations[0].lock_timeout_ms,spec.lock_timeout_ms)
    def test_default_sql_null_and_explicit_json_null_keep_native_inputs(self):
        for value in (helper._DEFAULT,None,Jsonb(None)):
            db=Database(inputs(),{'invalid':True})
            with self.assertRaises(DatabaseError):self.call(db,summary=value)
            bound=db.calls[0][2][-1]
            if value is helper._DEFAULT:self.assertEqual(bound.obj,{})
            elif value is None:self.assertIsNone(bound)
            else:self.assertIs(bound,value)
    def test_every_fixed_statement_binds_in_direct_guard(self):
        returning={helper._ABSENT,helper._UPDATE_ALL,helper._UPDATE_LONG,helper._RECORD_ERROR}
        for name,sql in vars(helper).items():
            if not(name.startswith('_') and isinstance(sql,str) and sql.lstrip().startswith(('SELECT','WITH','INSERT','UPDATE'))):continue
            names=re.findall(r'%\(([^)]+)\)s',sql);params={key:None for key in names} if names else (None,)*len(re.findall('%s',sql))
            PostgresQuery(Transformer()).convert(sql,params);is_returning=sql in returning
            read=is_returning or sql.lstrip().startswith(('SELECT','WITH'))
            _reviewed_statement(sql,read=read,max_rows=1 if read else 0,returning=is_returning)
        source=Path(helper.__file__).read_text();self.assertNotIn('.call(',source);self.assertNotIn('operator_private.',source)


if __name__=='__main__':unittest.main()
