"""Finite empty preflight behavior and authoritative direct-access proof, offline."""
from contextlib import nullcontext
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.readiness import collection_preflight as ready
from db.channel.channel_sync import ChannelSyncRepository
from db.videos.video_collection import VideoCollectionRepository
from db.base.postgres import DatabaseError,DatabaseServerError
from db.videos import video_claims
from db.readiness import video_failure_capabilities, inventory_chain_capabilities as chains
from test_capabilities import CHAIN_FACTS, reviewed_chain_facts

GOOD=dict.fromkeys(('roles_valid','relations_valid','privileges_valid','dependencies_valid'),True)
QUEUE=dict.fromkeys(('roles_valid','relations_valid','privileges_valid'),True)


class Transaction:
    def __init__(self):
        self.calls=[]
        self.first_error=None
        self.facts=dict(GOOD)
        self.queue=dict(QUEUE)
        self.chain={name:dict(facts) for name,facts in CHAIN_FACTS.items()}
    def borrow(self):return nullcontext(self)
    def fetch_one(self,statement,parameters=()):
        self.calls.append((statement,parameters))
        if statement==video_claims._PREPARE:
            if self.first_error:raise self.first_error
            ids,worker,run,scopes,lease=parameters
            return dict(video_ids=ids,worker_id=worker,run_id=run,scopes=scopes.obj,
                        lease_seconds=lease,id_count=len(ids),worker=worker.strip() if worker else None)
        if statement==video_claims._IDENTITY_MATCH:return {'matches':True}
        if statement in (ready.METADATA_SQL,ready.FORCED_SQL):return self.facts
        if statement==video_failure_capabilities.READINESS_SQL:return self.queue
        for name,sql in (('inventory_chain_acl',chains.ACL_SQL),('inventory_chain_catalog',chains.CATALOG_SQL),
                         ('inventory_chain_definitions',chains.definition_sql())):
            if statement==sql:return self.chain[name]
        raise AssertionError('unowned statement')
    def fetch_all(self,statement,parameters,*,max_rows):
        self.calls.append((statement,parameters))
        if statement not in (video_claims._LOCK_QUEUE,video_claims._LOCK_INVENTORY) or parameters!=([],):
            raise AssertionError('unexpected lock scope')
        return []


class Database:
    def __init__(self):self.tx=Transaction();self.operations=[]
    def transaction(self,operation):self.operations.append(operation);return nullcontext(self.tx)
    def call(self,*args,**kwargs):raise AssertionError('retained RPC executed')


class PreflightTests(unittest.TestCase):
    def setUp(self):reviewed_chain_facts(self)

    def test_public_metadata_uses_one_transaction_with_finite_catalog_reads_and_no_write(self):
        database=Database()
        self.assertIsNone(ChannelSyncRepository(database).assert_metadata_batch_ready())
        self.assertEqual(database.tx.calls,[(sql,()) for sql in (ready.METADATA_SQL,chains.ACL_SQL,chains.CATALOG_SQL,chains.definition_sql())])
        self.assertEqual(len(database.operations),1)
        op=database.operations[0]
        self.assertTrue(op.read_only)
        self.assertEqual((op.name,op.statement_timeout_ms,op.total_timeout_ms,op.lock_timeout_ms),
                         ('update_channel_metadata_batch',60000,60000,0))

    def test_forced_public_empty_path_order_and_permitted_empty_arguments(self):
        for worker,run,lease in (('worker','00000000-0000-4000-8000-000000000001',300),('',None,0),(None,None,None)):
            database=Database()
            with patch.object(ready,'emit_event'):
                self.assertEqual(VideoCollectionRepository(database).assert_forced_metadata_claim_ready(
                    worker_id=worker,run_id=run,lease_seconds=lease),[])
            self.assertEqual([call[0] for call in database.tx.calls],[video_claims._PREPARE,video_claims._LOCK_QUEUE,
                video_claims._LOCK_INVENTORY,video_claims._IDENTITY_MATCH,ready.FORCED_SQL,video_failure_capabilities.READINESS_SQL,
                chains.ACL_SQL,chains.CATALOG_SQL,chains.definition_sql()])
            params=database.tx.calls[0][1]
            self.assertEqual((params[0],params[1],params[2],params[3].obj,params[4]),([],worker,run,[],lease))
            self.assertEqual(len(database.operations),1)
            self.assertFalse(database.operations[0].read_only)
            self.assertEqual(database.operations[0].total_timeout_ms,60000)

    def test_native_cast_failure_precedes_catalog_proof_and_never_retries(self):
        for code in ('22P02','22003'):
            database=Database();error=DatabaseServerError('database_operation_failed',code=code)
            database.tx.first_error=error;database.tx.facts['privileges_valid']=False
            with patch.object(ready,'emit_event'),self.assertRaises(DatabaseServerError) as caught:
                ready.assert_forced_metadata_claim_ready(database,worker_id='',run_id='bad',lease_seconds='bad')
            self.assertIs(caught.exception,error)
            self.assertEqual(len(database.tx.calls),1)
            self.assertEqual(len(database.operations),1)

    def test_missing_or_extra_capabilities_fail_closed(self):
        for key in GOOD:
            database=Database();database.tx.facts[key]=False
            with self.assertRaises(DatabaseError) as caught:ready.assert_metadata_batch_ready(database)
            self.assertEqual((caught.exception.message,caught.exception.code),('database_operation_failed','P0001'))
        for row in (None,{},dict(GOOD,extra=True),dict(GOOD,privileges_valid=1)):
            with self.assertRaises(DatabaseError) as caught:ready._validate(row)
            self.assertEqual(caught.exception.message,'database_result_shape_invalid')
        database=Database();database.tx.queue['privileges_valid']=False
        with patch.object(ready,'emit_event'),self.assertRaisesRegex(DatabaseError,'video_failure_acl_invalid'):
            ready.assert_forced_metadata_claim_ready(database,worker_id='x',run_id=None)

    def test_forced_telemetry_preserves_name_phase_zero_counts_and_error(self):
        database=Database()
        with patch.object(ready,'emit_event') as emit,patch.object(ready.time,'perf_counter',side_effect=[1,1.002]):
            self.assertEqual(ready.assert_forced_metadata_claim_ready(database,worker_id='x',run_id=None),[])
        self.assertEqual(emit.call_args_list[0].args,('rpc','sync_engine','db_selectors','claim_video_metadata_collection_ids'))
        self.assertEqual(emit.call_args_list[0].kwargs,dict(phase='preflight',direction='request',status='started',count=0))
        self.assertEqual(emit.call_args_list[1].kwargs,dict(phase='preflight',direction='response',status='ok',count=0,duration_ms=2))
        database.tx.first_error=DatabaseServerError('database_operation_failed',code='22P02')
        with patch.object(ready,'emit_event') as emit,self.assertRaises(DatabaseServerError):
            ready.assert_forced_metadata_claim_ready(database,worker_id='x',run_id='bad')
        self.assertEqual(emit.call_count,2)
        self.assertEqual(emit.call_args.kwargs['status'],'error')
        self.assertEqual(emit.call_args.kwargs['detail'],'DatabaseServerError')
        self.assertNotIn('count',emit.call_args.kwargs)

    def test_chain_proof_failure_stays_in_existing_preflight_transaction(self):
        for forced in (False,True):
            for alias,facts in CHAIN_FACTS.items():
                for field in facts:
                    database=Database();database.tx.chain[alias][field]=False
                    with self.subTest(forced=forced,alias=alias,field=field),patch.object(ready,'emit_event'):
                        with self.assertRaises(DatabaseError):
                            if forced:ready.assert_forced_metadata_claim_ready(database,worker_id='x',run_id=None)
                            else:ready.assert_metadata_batch_ready(database)
                    self.assertEqual(len(database.operations),1)
                    self.assertEqual(database.operations[0].read_only,not forced)
                    self.assertEqual(len([sql for sql,_ in database.tx.calls if sql==chains.ACL_SQL]),1)

    def test_pending_pins_do_not_open_a_second_transaction_or_read_domain(self):
        for forced in (False,True):
            database=Database()
            with patch.object(chains,'DEFINITION_PINS',{}),patch.object(ready,'emit_event'):
                with self.assertRaisesRegex(DatabaseError,'definition_pins_missing'):
                    if forced:ready.assert_forced_metadata_claim_ready(database,worker_id='x',run_id=None)
                    else:ready.assert_metadata_batch_ready(database)
            self.assertEqual(len(database.operations),1)
            self.assertEqual(len(database.tx.calls),6 if forced else 1)
            self.assertFalse(any(sql==chains.ACL_SQL for sql,_ in database.tx.calls))

    def test_metadata_writer_fields_and_invoker_view_reads_are_proven(self):
        self.assertTrue(ready.channel_metadata_batch._CHANNEL_FIELDS<=set(ready._METADATA_READ['channels']))
        self.assertEqual(set(ready._METADATA_READ['operator_jobs']),set(ready._MODE_JOB_READ))
        for statement,reads in ((ready.METADATA_SQL,ready._METADATA_READ),(ready.FORCED_SQL,ready._FORCED_READ)):
            for table,columns in reads.items():
                for column in columns:self.assertIn("('%s','%s')"%(table,column),statement)
            self.assertIn('has_column_privilege',statement)
            self.assertIn('aclexplode',statement)
            self.assertIn('is_grantable',statement)
            self.assertIn('security_invoker=true',statement)
        for column in ready._CONTROL_UPDATE:self.assertIn("('channel_collection_state','%s','UPDATE')"%column,ready.METADATA_SQL)
        self.assertIn('channel_collection_epoch_seq',ready.METADATA_SQL)
        self.assertNotIn('typed_inventory_channel_eligible',ready.FORCED_SQL)
        for sql in (ready.METADATA_SQL,ready.FORCED_SQL):
            self.assertIn(ready._INTRINSIC_READINESS_SQL,sql)
            self.assertIn('roles_valid AND functions_valid AND triggers_valid',sql)
        self.assertEqual(set(ready._FORCED_READ['channel_collection_state']),
                         {'channel_id','user_work_id','owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id'})
        self.assertNotIn('update_channel_metadata_batch(',ready.METADATA_SQL)
        self.assertNotIn('claim_video_metadata_collection_ids(',ready.FORCED_SQL)

    def test_expected_table_rights_match_authoritative_current_schema(self):
        source=(ROOT/'schema/schema.sql').read_text()
        table_rights={};column_rights={};sequence_rights={}
        for match in re.finditer(r'GRANT\s+([^;]+?)\s+ON (TABLE|SEQUENCE) ([^;]+?) TO service_role;',source,re.S):
            rights,kind,targets=match.groups()
            if ';' in rights:continue
            for table in targets.split(','):
                table=table.strip().removeprefix('public.')
                cm=re.fullmatch(r'(UPDATE|INSERT)\s*\(([^)]+)\)',rights.strip())
                if cm:
                    column_rights.setdefault(table,set()).update((name.strip(),cm[1]) for name in cm[2].split(','))
                else:
                    mapping=sequence_rights if kind=='SEQUENCE' else table_rights
                    mapping.setdefault(table,set()).update(rights.split(','))
        for name,kind,rights in {*ready._METADATA_TABLES,*ready._FORCED_TABLES}:
            self.assertEqual(table_rights[name],set(rights),name)
        self.assertEqual(column_rights['channel_collection_state'],{(name,'UPDATE') for name in ready._CONTROL_UPDATE} | {(name,'INSERT') for name in ready._CONTROL_INSERT})
        self.assertEqual(column_rights['channel_snapshots'],{('recorded_at','UPDATE')})
        self.assertEqual(sequence_rights['channel_collection_epoch_seq'],{'USAGE'})

    def test_admission_denial_propagates_without_fallback_or_retry(self):
        database=Mock();error=DatabaseError('database_operation_failed')
        database.transaction.side_effect=error
        for invoke in (lambda:ready.assert_metadata_batch_ready(database),
                       lambda:ready.assert_forced_metadata_claim_ready(database,worker_id='x',run_id=None)):
            with patch.object(ready,'emit_event'),self.assertRaises(DatabaseError) as caught:invoke()
            self.assertIs(caught.exception,error)
        self.assertEqual(database.transaction.call_count,2)
        database.call.assert_not_called()


if __name__=='__main__':unittest.main()
