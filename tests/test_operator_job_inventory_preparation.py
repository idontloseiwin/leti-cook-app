"""Preparation branches, replay ownership, bounds and transaction rollback."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_inventory_preparation as prepare
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
JOB='f8600000-0000-4000-8000-000000000001';TOKEN='f8600000-0000-4000-8000-000000000002'
TRANSITION='f8600000-0000-4000-8000-000000000003';CHANNEL='UC0000000000000000000001'
BASE=dict(id=JOB,channel_id=CHANNEL,initial_scope_version=1,phase='inventory',inventory_checkpoint={},
          initial_video_types=['long','short','stream'],inventory_video_type='long',inventory_scan_id=None)
def one(**value):
    if isinstance(value.get('value'),dict) and 'initial_scope_version' in value['value']:
        value['value']=json.dumps(value['value'])
    return Result((value,))
def write():return Result(has_columns=False,rowcount=1)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        for name in ('assert_channel_scope','assert_full_initial_scope','initial_inventory_checkpoint_evidence',
                     'restart_initial_inventory_discovery','reconcile_full_initial_targets'):
            mocked=patch.object(prepare,name);setattr(self,name,mocked.start());self.addCleanup(mocked.stop)
        self.initial_inventory_checkpoint_evidence.return_value=dict(kind='none',format_finished=False,format_absent=False,bound_live=False)
        self.restart_initial_inventory_discovery.return_value=True
    def queue(self,*values):self.connection.results.extend(values)
    def header(self,job=None,invalid=False,epoch=17,accepted=17,done=False):
        job=deepcopy(BASE if job is None else job)
        self.queue(one(job_id=JOB,lease_token=TOKEN,page_limit=5,lease_seconds=1200),one(channel_id=CHANNEL),one(channel_id=CHANNEL),
            one(value=job),one(epoch=epoch,accepted_epoch=accepted,invalid=invalid))
        if job['initial_scope_version']==1:self.queue(one(value=done))
        return job
    def call(self,**changes):return prepare.prepare_initial_inventory_job_chunk(self.database,JOB,TOKEN,**changes)
    def fresh(self,job=None):
        row=deepcopy(BASE if job is None else job)
        self.queue(one(exhausted=False,record_error=True),write(),write(),one(value=TRANSITION),one(value=row))
    def test_full_fresh_plan_one_page_initializes_then_reserves_and_fences(self):
        self.header();self.fresh();result=self.call()
        self.assertEqual((result['action'],result['page_limit'],result['reserved_units'],result['scope']),('inventory',1,1,'all'))
        self.assertEqual(result['transition_id'],TRANSITION);self.assertIsNone(result['resume_page_token'])
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1));self.assert_channel_scope.assert_called_once()
        sql=[query for query,params in self.connection.queries]
        for actual,wanted in zip(sql[-4:],(prepare._PLAYLIST_INSERT,prepare._WAKE,'SELECT gen_random_uuid()::text AS value',prepare._RESERVE)):
            self.assertIn(wanted,actual)
    def test_input_casts_precede_page_domain_validation(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:self.call(page_limit=0,lease_seconds='bad')
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
    def test_page_limit_validation_precedes_lease_seconds_and_any_lock(self):
        for page in (None,0,11):
            self.queue(one(job_id=JOB,lease_token=TOKEN,page_limit=page,lease_seconds=0))
            with self.assertRaisesRegex(DatabaseError,'operator_job_inventory_page_limit_invalid'):self.call(page_limit=page,lease_seconds=0)
        self.assertEqual(len(self.connection.queries),3)
    def test_lease_duration_validation_precedes_channel_lookup(self):
        for seconds in (None,59,3601):
            self.queue(one(job_id=JOB,lease_token=TOKEN,page_limit=1,lease_seconds=seconds))
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_seconds_invalid'):self.call(lease_seconds=seconds)
    def test_job_invalid_precedes_full_scope_and_evidence(self):
        self.header(invalid=True)
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call()
        self.assert_full_initial_scope.assert_not_called();self.initial_inventory_checkpoint_evidence.assert_not_called()
    def test_full_completed_sources_use_native_250_reconcile_and_direct_final_scope(self):
        self.header(done=True,epoch=17,accepted=18);resolved=dict(BASE,status='queued',phase='hydrate_videos')
        self.reconcile_full_initial_targets.return_value=resolved
        result=self.call();self.assertEqual(result['action'],'targets_reconciled');self.assertEqual(result['reserved_units'],0)
        self.reconcile_full_initial_targets.assert_called_once();self.assertEqual(self.reconcile_full_initial_targets.call_args.args[1:],(JOB,250))
        self.initial_inventory_checkpoint_evidence.assert_not_called()
        self.assertEqual(self.assert_channel_scope.call_args.args[1:],(CHANNEL,17))
    def test_legacy_live_discovery_returns_await_without_reload_refund_or_reservation(self):
        job=dict(BASE,initial_scope_version=0,initial_video_types=['long']);self.header(job)
        self.restart_initial_inventory_discovery.return_value=False
        self.queue(one(value={'action':'await_receipt','reason':'initial_inventory_discovery_in_progress','reserved_units':0}))
        result=self.call();self.assertEqual(result['action'],'await_receipt');self.assertEqual(len(self.connection.queries),6)
        self.assert_full_initial_scope.assert_not_called();self.assert_channel_scope.assert_called_once()
    def test_legacy_restart_reloads_detached_job_before_fresh_plan(self):
        job=dict(BASE,initial_scope_version=0,initial_video_types=['long']);self.header(job)
        self.queue(one(value=job));self.fresh(job);result=self.call()
        self.assertEqual(result['scope'],'long');self.assertIn(prepare._RELOAD,self.connection.queries[5][0])
        self.restart_initial_inventory_discovery.assert_called_once()
    def test_bound_receipt_reconcile_returns_before_scheduling(self):
        self.header(dict(BASE,inventory_checkpoint={'phase':'bound','reserved_units':1}))
        self.queue(one(value='reconcile_inventory'),one(value={'action':'reconcile_inventory','reserved_units':1}))
        self.assertEqual(self.call()['action'],'reconcile_inventory')
        self.assertFalse(any('UPDATE public.' in sql or 'INSERT INTO public.' in sql for sql,params in self.connection.queries))
    def test_exact_prepared_replay_returns_before_cap_or_playlist_state_mutation(self):
        self.header(dict(BASE,inventory_checkpoint={'phase':'prepared','job_lease_token':TOKEN,'reserved_units':1}))
        self.queue(one(value={'action':'inventory','reserved_units':1}))
        self.assertEqual(self.call()['reserved_units'],1);self.assertEqual(len(self.connection.queries),7)
        self.assertFalse(any('UPDATE public.' in sql or 'INSERT INTO public.' in sql for sql,params in self.connection.queries))
    def test_job_snapshot_passes_exact_large_numeric_json_to_all_native_consumers(self):
        job=dict(BASE,inventory_checkpoint={'phase':'prepared','job_lease_token':TOKEN,'reserved_units':1},summary_json={'opaque':'MARKER'})
        raw=json.dumps(job).replace('"MARKER"','1e1000')
        self.queue(one(job_id=JOB,lease_token=TOKEN,page_limit=5,lease_seconds=1200),one(channel_id=CHANNEL),one(channel_id=CHANNEL),
            one(value=raw),one(epoch=17,accepted_epoch=17,invalid=False),one(value=False),one(value={'action':'inventory'}))
        self.call()
        self.assertEqual(self.assert_full_initial_scope.call_args.args[1],raw)
        self.assertEqual(self.initial_inventory_checkpoint_evidence.call_args.args[1],raw)
        self.assertEqual(self.connection.queries[4][1]['job'],raw)
        self.assertEqual(self.connection.queries[-1][1]['job'],raw)
    def test_quota_evidence_preserves_large_numeric_json_through_action_and_return(self):
        raw='{"kind":"quota","format_finished":false,"retry_not_before":1e1000}'
        self.initial_inventory_checkpoint_evidence.return_value=prepare._NativeObject(raw)
        self.header(dict(BASE,inventory_checkpoint={'phase':'bound','reserved_units':1}))
        self.queue(one(value='reconcile_failure'),one(value={'action':'reconcile_failure'}));self.call()
        self.assertEqual(self.connection.queries[-2][1]['evidence'],raw)
        self.assertEqual(self.connection.queries[-1][1]['evidence'],raw)
    def test_reclaimed_unbound_command_refunds_then_generates_a_new_transition(self):
        checkpoint=dict(phase='prepared',job_lease_token=TRANSITION,reserved_units=1)
        self.header(dict(BASE,inventory_checkpoint=checkpoint));self.queue(one(value=deepcopy(BASE)));self.fresh()
        self.call();self.assertIn(prepare._REFUND,self.connection.queries[6][0])
        self.assertEqual(self.connection.queries[6][1][0].obj,checkpoint)
    def test_completed_format_reserves_zero_without_sequence_or_wake(self):
        self.header();self.initial_inventory_checkpoint_evidence.return_value=dict(kind='none',format_finished=True,scan_id=TRANSITION)
        self.queue(one(exhausted=False,record_error=True),one(value=TRANSITION),one(value=dict(BASE,inventory_scan_id=TRANSITION)))
        result=self.call();self.assertEqual(result['page_limit'],0);self.assertEqual(result['scan_id'],TRANSITION)
        self.assertFalse(any(prepare._PLAYLIST_INSERT in sql for sql,params in self.connection.queries))
    def test_cap_exhaustion_records_channel_error_then_fences_original_epoch(self):
        job=dict(BASE,initial_scope_version=0);self.header(job);self.queue(one(value=job),one(exhausted=True,record_error=True),
            one(value=dict(job,status='blocked',lease_token=None)),one(value={'error_count':1}))
        result=self.call();self.assertEqual(result['action'],'blocked')
        self.assertIn(prepare._RECORD_ERROR,self.connection.queries[-1][0]);self.assertEqual(self.assert_channel_scope.call_args.args[1:],(CHANNEL,17))
    def test_nonlong_full_legacy_cap_does_not_change_long_error_state(self):
        job=dict(BASE,initial_scope_version=0,inventory_video_type='short');self.header(job)
        self.queue(one(value=job),one(exhausted=True,record_error=False),one(value=dict(job,status='blocked')))
        self.call();self.assertIn(prepare._BLOCK,self.connection.queries[-1][0])
    def test_deferred_missing_epoch_rolls_back_reservation_at_original_final_fence(self):
        self.header(epoch=None,accepted=17);self.fresh()
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call()
        self.assertIn(prepare._RESERVE,self.connection.queries[-1][0]);self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_initial_stats_native_count_and_reservation_skip_inventory_dependencies(self):
        job=dict(BASE,initial_scope_version=0,phase='initial_stats');self.header(job)
        self.queue(one(value=51),one(value=4),one(value=job));result=self.call()
        self.assertEqual(result,dict(action='initial_stats',job=job,page_limit=0,reserved_units=4))
        self.initial_inventory_checkpoint_evidence.assert_not_called();self.restart_initial_inventory_discovery.assert_not_called()
    def test_source_expiry_or_late_fence_error_propagates_without_retry(self):
        self.header();self.fresh();self.assert_channel_scope.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):self.call()
        self.assertEqual(self.pool.borrowed,1);self.assertEqual(self.connection.commits,0)
    def test_unknown_commit_gives_no_plan_and_does_not_repeat_reservation(self):
        self.header();self.fresh();self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseError) as caught:self.call()
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
        self.assertEqual(sum(prepare._RESERVE in sql for sql,params in self.connection.queries),1)
    def test_caught_borrowed_precondition_cannot_commit_prior_write(self):
        self.queue(write(),one(job_id=JOB,lease_token=TOKEN,page_limit=0,lease_seconds=1200))
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('preparation_owner')) as tx:
                tx.execute('UPDATE public.operator_jobs SET updated_at=now() WHERE id=%s::uuid',(JOB,))
                try:prepare.prepare_initial_inventory_job_chunk(None,JOB,TOKEN,transaction=tx)
                except DatabaseServerError:pass
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_fixed_statements_keep_native_arithmetic_and_explicit_sequence_dependency(self):
        args=dict(job_id=JOB,channel_id=CHANNEL,lease_token=TOKEN,job=Jsonb(BASE),evidence=Jsonb({}),checkpoint=Jsonb({}),
            reserved=1,transition=TRANSITION,lease_seconds=1200,action='inventory',waiting_discovery=False,with_evidence=False)
        writes={prepare._PLAYLIST_INSERT,prepare._WAKE}
        returning={prepare._REFUND,prepare._BLOCK,prepare._RESERVE,prepare._RESERVE_STATS,prepare._RECORD_ERROR}
        for name,sql in vars(prepare).items():
            if not name.startswith('_') or not isinstance(sql,str) or not sql.lstrip().startswith(('SELECT','UPDATE','INSERT')):continue
            params=args if '%(' in sql else tuple('{}' for _ in range(sql.count('%s')))
            PostgresQuery(Transformer()).convert(sql,params)
            _reviewed_statement(sql,read=sql not in writes,max_rows=0 if sql in writes else 1,returning=sql in returning)
        self.assertIn('lease_expires_at<=now()',prepare._LEASE)
        self.assertIn('cardinality(j.initial_video_types)=3',prepare._CAP)
        self.assertIn('j.items_processed>j.max_items ELSE j.items_processed>=j.max_items',prepare._CAP)
        source=(ROOT/'application/db/jobs/operator_job_inventory_preparation.py').read_text()
        self.assertIn('inventory_dispatch_seq USAGE',source);self.assertNotIn('database.call(',source)


if __name__=='__main__':unittest.main()
