"""Public ensure input/lock/identity/final-fence order, without a database."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_queue_ensure as ensure
from db.base.postgres import DatabaseError,DatabaseServerError,PostgresConfig,PostgresDatabase,Operation,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN

VIDEO='aaaaaaaaaaa';CHANNEL='UC0000000000000000000001'
SCOPES=[{'channel_id':CHANNEL,'control_epoch':17}]
def one(**values):return Result((values,))


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        self.patch=patch.object(ensure,'ensure_video_collection_state_in_transaction',return_value=3)
        self.initializer=self.patch.start();self.addCleanup(self.patch.stop)
    def queue(self,*values):self.connection.results.extend(values)
    def header(self,ids=None,scopes=None,count=1):
        self.queue(one(ids=[VIDEO] if ids is None else ids,scopes=SCOPES if scopes is None else scopes,count=count),Result(),Result(),one(matches=True))
    def test_native_input_then_queue_update_inventory_share_identity_initialization_final_fence(self):
        self.header();self.queue(one(channel_id=CHANNEL,control_epoch=17,is_hidden=False,owner_permission='accepted'))
        def initialize(tx,ids):
            self.assertEqual(len(self.connection.queries),4);return 3
        self.initializer.side_effect=initialize
        self.assertEqual(ensure.ensure_video_collection_state(self.database,[VIDEO],SCOPES),3)
        sql=[query for query,params in self.connection.queries]
        for actual,expected in zip(sql,(ensure._INPUT,ensure._LOCK_QUEUE,ensure._LOCK_INVENTORY,ensure._IDENTITY)):
            self.assertIn(expected,actual)
        self.assertIn('FOR SHARE',sql[-1]);self.assertIn('channel_collection_state',sql[-1])
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
    def test_owned_operation_preserves_declared_timeouts_and_known_error_policy(self):
        self.header();seen=[];self.database._admission=lambda name:seen.append(name)
        with patch.object(ensure,'assert_channel_scopes'):
            ensure.ensure_video_collection_state(self.database,[VIDEO],SCOPES)
        # An operation label does not confer a stored maintenance capability.
        self.assertTrue(seen);self.assertTrue(all(name is None for name in seen))
        setup,parameters=next((sql,params) for sql,params in self.connection.setup if 'lock_timeout' in sql)
        self.assertIn('statement_timeout',setup);self.assertIn('lock_timeout',setup)
        self.assertEqual(parameters,('60000ms','0ms'))
    def test_ids_null_and_over1000_scopes_null_scalar_object_reject_before_any_lock(self):
        for count,scopes in ((None,[]),(1001,[]),(1,None),(1,{}),(1,'x'),(1,True)):
            self.queue(one(ids=None,count=count,scopes=scopes));before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):
                ensure.ensure_video_collection_state(self.database,None,scopes)
            self.assertEqual(len(self.connection.queries),before+1)
        self.initializer.assert_not_called();self.assertEqual(self.connection.commits,0)
    def test_native_cast_failure_precedes_public_validation(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:ensure.ensure_video_collection_state(self.database,'bad',None)
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
    def test_native_identity_mismatch_rejects_after_both_locks_before_write(self):
        self.queue(one(ids=[VIDEO],count=1,scopes=[None]),Result(),Result(),one(matches=False))
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):
            ensure.ensure_video_collection_state(self.database,[VIDEO],[None])
        self.initializer.assert_not_called();self.assertEqual(len(self.connection.queries),4)
        self.assertEqual(ensure._IDENTITY.count('EXCEPT'),2);self.assertIn('UNION ALL',ensure._IDENTITY)
    def test_scope_cardinality_and_item_checks_remain_after_initializer(self):
        for scopes in (SCOPES*1001,SCOPES*2,[{'channel_id':CHANNEL,'control_epoch':'17'}]):
            self.header(scopes=scopes)
            with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):
                ensure.ensure_video_collection_state(self.database,[VIDEO],scopes)
        self.assertEqual(self.initializer.call_count,3);self.assertEqual(self.connection.commits,0)
    def test_initializer_native_error_precedes_late_scope_cardinality_failure(self):
        self.header(scopes=SCOPES*1001)
        self.initializer.side_effect=DatabaseServerError('database_operation_failed',code='0A000')
        with self.assertRaises(DatabaseError) as caught:ensure.ensure_video_collection_state(self.database,[VIDEO],SCOPES*1001)
        self.assertEqual(caught.exception.code,'0A000')
    def test_empty_ids_still_pass_both_locks_identity_initializer_and_empty_fence(self):
        self.header(ids=[],scopes=[],count=0);self.initializer.return_value=0
        self.assertEqual(ensure.ensure_video_collection_state(self.database,[],[]),0)
        self.assertEqual(len(self.connection.queries),4);self.assertEqual(self.initializer.call_args.args[1],[])
    def test_multidimensional_and_null_array_items_remain_native_not_flattened_or_deduplicated(self):
        ids=[[VIDEO,None],[VIDEO,'missing']];self.header(ids=ids,count=4)
        with patch.object(ensure,'assert_channel_scopes'):
            ensure.ensure_video_collection_state(self.database,ids,SCOPES)
        self.assertEqual(self.initializer.call_args.args[1],ids)
        self.assertEqual(self.connection.queries[1][1],(ids,));self.assertEqual(self.connection.queries[3][1]['ids'],ids)
    def test_final_generation_failure_rolls_back_initializer_writes(self):
        self.header();self.queue(Result(has_columns=False,rowcount=1),one(channel_id=CHANNEL,control_epoch=18,is_hidden=False,owner_permission='accepted'))
        def initialize(tx,ids):return tx.execute('UPDATE public.video_collection_state SET updated_at=now() WHERE video_id=%s',(VIDEO,))
        self.initializer.side_effect=initialize
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):ensure.ensure_video_collection_state(self.database,[VIDEO],SCOPES)
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_borrowed_domain_failure_cannot_be_caught_to_commit_prior_writes(self):
        self.queue(Result(has_columns=False,rowcount=1),one(ids=None,count=None,scopes=[]))
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('test_ensure_owner')) as tx:
                tx.execute('UPDATE public.video_collection_state SET updated_at=now() WHERE video_id=%s',(VIDEO,))
                try:ensure.ensure_video_collection_state(None,None,[],transaction=tx)
                except DatabaseServerError:pass
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(0,1,1))
    def test_unknown_commit_propagates_once_without_initializer_retry(self):
        self.header();self.connection.commit_error=psycopg.OperationalError('private detail')
        with patch.object(ensure,'assert_channel_scopes'),self.assertRaises(DatabaseError) as caught:
            ensure.ensure_video_collection_state(self.database,[VIDEO],SCOPES)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.initializer.call_count,1);self.assertEqual(self.pool.borrowed,1)
    def test_sql_is_bounded_parameterized_and_consumer_rpc_is_not_used(self):
        for sql,params in ((ensure._INPUT,([VIDEO],Jsonb(SCOPES))),(ensure._LOCK_QUEUE,([VIDEO],)),
            (ensure._LOCK_INVENTORY,([VIDEO],)),(ensure._IDENTITY,dict(ids=[VIDEO],scopes=Jsonb(SCOPES)))):
            _reviewed_statement(sql,read=True,max_rows=1000);PostgresQuery(Transformer()).convert(sql,params)
        source=(ROOT/'application/db/videos/video_queue_ensure.py').read_text()
        self.assertNotIn('database.call(',source);self.assertNotIn('SKIP LOCKED',source)
        from db.videos import video_queue_initialization as initialization
        for statement in (initialization._LONG_INPUT,initialization._LONG_UPSERT,initialization._TYPED_INSERT):
            self.assertNotIn('public.typed_inventory_channel_eligible(',statement)


if __name__=='__main__':unittest.main()
