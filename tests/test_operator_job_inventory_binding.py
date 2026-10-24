"""Binding acknowledgement, lock/clock order, native flags and atomic ownership."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_inventory_binding as binding
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN

UUIDS=['f8500000-0000-4000-8000-00000000000'+str(i) for i in range(1,6)]
CHANNEL='UC0000000000000000000001'
ARGS=dict(zip(('job_id','job_lease_token','playlist_lease_token','scan_id','transition_id'),UUIDS))
def one(**value):return Result((value,))
def write():return Result(has_columns=False,rowcount=1)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        scope=patch.object(binding,'assert_full_initial_scope');self.scope=scope.start();self.addCleanup(scope.stop)
        fence=patch.object(binding,'assert_channel_scope');self.fence=fence.start();self.addCleanup(fence.stop)
    def queue(self,*values):self.connection.results.extend(values)
    def header(self,version=1,lease_invalid=False,checkpoint_invalid=False):
        self.queue(one(value=dict(ARGS)),one(replayed=False),one(channel_id=CHANNEL),one(channel_id=CHANNEL),
            one(value='{"id":"'+UUIDS[0]+'"}'),one(channel_id=CHANNEL,epoch=17,initial_scope_version=version,
            lease_invalid=lease_invalid,checkpoint_invalid=checkpoint_invalid))
    def children(self,identity=False,kind=False):
        self.queue(one(value='{"status":"leased"}'),one(invalid=False),one(value='{"status":"running"}'),
            one(identity_invalid=identity,kind_invalid=kind))
    def call(self,**changes):return binding.bind_initial_inventory_scan(self.database,**{**ARGS,**changes})
    def test_fresh_binding_locks_channel_job_playlist_scan_registers_then_fences_once(self):
        self.header();self.children();self.queue(one(has_source=False,invalid=False),write(),write())
        result=self.call()
        self.assertEqual(result,dict(status='bound',job_id=UUIDS[0],scan_id=UUIDS[3],transition_id=UUIDS[4],replayed=False,dispatch_allowed=True))
        queries=[sql for sql,params in self.connection.queries]
        locks=[sql for sql in queries if 'FOR UPDATE' in sql]
        self.assertEqual(len(locks),4)
        for actual,wanted in zip(locks,(binding._CHANNEL,binding._JOB,binding._PLAYLIST,binding._SCAN)):
            self.assertIn(wanted,actual)
        self.assertIn(binding._REGISTER_SOURCE,queries[-2]);self.assertIn(binding._BIND,queries[-1])
        self.scope.assert_called_once();self.fence.assert_called_once();self.assertEqual(self.fence.call_args.args[1:],(CHANNEL,17))
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
    def test_exact_old_binding_returns_ack_before_visibility_lease_locks_scope_or_writes(self):
        self.queue(one(value=dict(ARGS)),one(replayed=True));result=self.call()
        self.assertTrue(result['replayed']);self.assertFalse(result['dispatch_allowed'])
        self.assertEqual(len(self.connection.queries),2);self.scope.assert_not_called();self.fence.assert_not_called()
    def test_native_all_uuid_casts_precede_null_domain_gate(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:self.call(job_id=None,scan_id='bad')
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
    def test_each_null_identity_rejects_before_replay(self):
        for key in ARGS:
            self.queue(one(value={**ARGS,key:None}));before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,'initial_inventory_bind_identity_invalid'):self.call(**{key:None})
            self.assertEqual(len(self.connection.queries),before+1)
    def test_missing_job_or_channel_is_lease_lost_before_child_locks(self):
        for rows in ((Result(),),(one(channel_id=CHANNEL),Result()),(one(channel_id=CHANNEL),one(channel_id=CHANNEL),Result())):
            self.queue(one(value=dict(ARGS)),one(replayed=False),*rows)
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call()
        self.assertEqual(self.connection.commits,0)
    def test_job_lease_failure_precedes_checkpoint_failure(self):
        self.header(lease_invalid=True,checkpoint_invalid=True)
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.call()
        self.assertEqual(len(self.connection.queries),6)
    def test_checkpoint_failure_precedes_playlist_lock(self):
        self.header(checkpoint_invalid=True)
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_bind_conflict'):self.call()
        self.assertEqual(len(self.connection.queries),6)
    def test_playlist_clock_is_checked_only_after_waiting_lock_returns(self):
        self.header();self.queue(one(value='{}'),one(invalid=True))
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_playlist_lease_lost'):self.call()
        queries=[sql for sql,params in self.connection.queries]
        self.assertIn(binding._PLAYLIST,queries[-2]);self.assertIn(binding._PLAYLIST_CHECK,queries[-1])
        self.assertNotIn('clock_timestamp',binding._PLAYLIST);self.assertNotIn('clock_timestamp',binding._JOB)
        self.assertIn('clock_timestamp',binding._PLAYLIST_CHECK);self.assertIn('clock_timestamp',binding._JOB_CHECK)
    def test_scan_identity_precedes_kind_and_full_scope(self):
        self.header();self.children(identity=True,kind=True)
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_scan_identity_invalid'):self.call()
        self.scope.assert_not_called()
    def test_scan_kind_precedes_full_scope(self):
        self.header();self.children(kind=True)
        with self.assertRaisesRegex(DatabaseError,'initial_inventory_scan_kind_invalid'):self.call()
        self.scope.assert_not_called()
    def test_scope_failure_precedes_new_source_registration(self):
        self.header();self.children();self.scope.side_effect=DatabaseServerError('full_initial_scope_invalid',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'full_initial_scope_invalid'):self.call()
        self.assertEqual(len(self.connection.queries),10);self.assertEqual(self.connection.commits,0)
    def test_existing_source_keeps_native_integer_coverage_and_never_reregisters(self):
        self.header();self.children();self.queue(one(has_source=True,invalid=False),write());self.call()
        self.assertNotIn(binding._REGISTER_SOURCE,[sql for sql,params in self.connection.queries])
        self.assertIn("->>'acked_pages')::integer",binding._FULL_CHECK)
    def test_coverage_and_fresh_generation_have_distinct_public_errors(self):
        for source,token in ((True,'full_initial_source_coverage_invalid'),(False,'full_initial_fresh_generation_required')):
            self.header();self.children();self.queue(one(has_source=source,invalid=True))
            with self.assertRaisesRegex(DatabaseError,token):self.call()
        self.assertEqual(self.connection.commits,0)
    def test_legacy_binding_skips_full_source_proof_and_registration(self):
        self.header(version=0);self.children();self.queue(write());self.call();self.scope.assert_not_called()
        self.assertEqual(len(self.connection.queries),11)
    def test_late_scope_loss_rolls_back_source_registration_and_binding_together(self):
        self.header();self.children();self.queue(one(has_source=False,invalid=False),write(),write())
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):self.call()
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_caught_borrowed_error_cannot_commit_prior_write(self):
        self.queue(write(),one(value={**ARGS,'job_id':None}))
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('binding_owner')) as tx:
                tx.execute('UPDATE public.operator_jobs SET updated_at=now() WHERE id=%s::uuid',(UUIDS[0],))
                try:binding.bind_initial_inventory_scan(None,**ARGS,transaction=tx)
                except DatabaseServerError:pass
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(0,1,1))
    def test_unknown_commit_returns_no_dispatch_permission_and_never_retries(self):
        self.header();self.children();self.queue(one(has_source=False,invalid=False),write(),write())
        self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseError) as caught:self.call()
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
        self.assertEqual(sum(binding._BIND in sql for sql,params in self.connection.queries),1)
    def test_sql_is_fixed_bound_and_native_composite_no_business_function_calls(self):
        args={**ARGS,'channel_id':CHANNEL,'job':'{}','playlist':'{}','scan':'{}','epoch':17}
        for sql,params in ((binding._INPUT,tuple(UUIDS)),(binding._REPLAY,args),(binding._CHANNEL_ID,(UUIDS[0],)),
            (binding._CHANNEL,(CHANNEL,)),(binding._JOB,args),(binding._JOB_CHECK,args),(binding._PLAYLIST,('{}',)),
            (binding._PLAYLIST_CHECK,args),(binding._SCAN,(UUIDS[3],)),(binding._SCAN_CHECK,args),(binding._FULL_CHECK,args)):
            _reviewed_statement(sql,read=True,max_rows=1);PostgresQuery(Transformer()).convert(sql,params)
        for sql in (binding._REGISTER_SOURCE,binding._BIND):
            _reviewed_statement(sql,read=False,max_rows=0);PostgresQuery(Transformer()).convert(sql,args)
        source=(ROOT/'application/db/jobs/operator_job_inventory_binding.py').read_text()
        for forbidden in ('database.call(', 'operator_private.', 'public.typed_inventory_channel_eligible(', 'datetime.'):
            self.assertNotIn(forbidden,source)


if __name__=='__main__':unittest.main()
