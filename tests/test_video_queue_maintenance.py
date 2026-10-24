"""Prepared queue maintenance preserves validation, lock and transaction order."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_queue_maintenance as helper
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
VIDEO='aaaaaaaaaaa';CHANNEL='UC0000000000000000000001';SCOPES=[dict(channel_id=CHANNEL,control_epoch=17)]
def one(**values):return Result((values,))


class QueueMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
        patcher=patch.object(helper,'ensure_video_collection_state_in_transaction',return_value=3)
        self.initializer=patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(helper,'assert_channel_scopes');self.fence=patcher.start();self.addCleanup(patcher.stop)
    def queue(self,*values):self.connection.results.extend(values)
    def backfill(self,*,ids=(VIDEO,),remaining=0):
        self.queue(one(limit=10),Result(tuple(dict(video_id=x) for x in ids)),Result(),Result(),one(scopes=SCOPES),
            one(count=remaining),Result(has_columns=False,rowcount=1))
    def enqueue(self,*,ids=None,scopes=None):
        ids=[VIDEO] if ids is None else ids;scopes=SCOPES if scopes is None else scopes
        self.queue(one(ids=ids,scopes=scopes,count=len(ids),invalid_id=False),one(video_id=VIDEO,policy_facts=native_policy_facts()),Result(),Result(),one(matches=True),
            Result(has_columns=False,rowcount=2),one(video_type='short',ids=ids,count=len(ids)),Result(has_columns=False,rowcount=3),
            one(video_type='stream',ids=ids,count=len(ids)),Result(has_columns=False,rowcount=4))
    def test_backfill_order_and_remaining_controls_are_atomic(self):
        self.backfill(remaining=7)
        self.assertEqual(helper.backfill_video_collection_state(self.database,10),[dict(selected_count=1,affected_count=3,remaining_count=7)])
        queries=self.connection.queries
        for (sql,params),expected in zip(queries,(helper._LIMIT,helper._SELECT,helper._LOCK_QUEUE,helper._LOCK_INVENTORY,helper._SCOPES,helper._REMAINING,helper._PROGRESS)):
            self.assertIn(expected,sql)
        self.assertEqual(queries[-1][1],(True,False));self.assertEqual(self.initializer.call_args.args[1],[VIDEO])
        self.fence.assert_called_once();self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
    def test_backfill_empty_still_records_success_and_calls_final_fence(self):
        self.backfill(ids=());self.initializer.return_value=0
        value=helper.backfill_video_collection_state(self.database,10)
        self.assertEqual(value,[dict(selected_count=0,affected_count=0,remaining_count=0)])
        self.assertEqual(self.connection.queries[-1][1],(False,True));self.fence.assert_called_once()
    def test_invalid_limit_rejects_before_selection_or_locks(self):
        for value in (None,0,-1,1001):
            self.queue(one(limit=value));before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,'database_operation_failed'):helper.backfill_video_collection_state(self.database,value)
            self.assertEqual(len(self.connection.queries),before+1)
        self.initializer.assert_not_called();self.assertEqual(self.connection.commits,0)
    def test_native_cast_failure_remains_before_domain_validation(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:helper.backfill_video_collection_state(self.database,'bad')
        self.assertEqual(caught.exception.code,'22P02');self.initializer.assert_not_called()
    def test_enqueue_orders_long_then_short_stream_and_single_fence(self):
        self.enqueue()
        self.assertEqual(helper.enqueue_video_collection_ids(self.database,[VIDEO],SCOPES),9)
        queries=self.connection.queries
        self.assertIn(helper._LONG_IDS,queries[1][0]);self.assertIn(helper._LOCK_QUEUE,queries[2][0]);self.assertIn(helper._LOCK_INVENTORY,queries[3][0])
        self.assertEqual(queries[6][1],('short',[VIDEO]));self.assertEqual(queries[8][1],('stream',[VIDEO]))
        self.assertEqual(self.initializer.call_args.args[1],[VIDEO]);self.fence.assert_called_once();self.assertEqual(self.connection.commits,1)
    def test_enqueue_invalid_ids_precede_selection_and_scope_validation(self):
        for count,invalid in ((None,False),(1001,False),(1,True)):
            self.queue(one(ids=None,scopes=None,count=count,invalid_id=invalid));before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,'common_pool_enqueue_input_invalid'):helper.enqueue_video_collection_ids(self.database,None,None)
            self.assertEqual(len(self.connection.queries),before+1)
        self.initializer.assert_not_called()
    def test_scope_shape_is_checked_after_long_selection_before_locks(self):
        for scopes in (None,{},SCOPES*1001):
            self.queue(one(ids=[VIDEO],scopes=scopes,count=1,invalid_id=False),Result());before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):helper.enqueue_video_collection_ids(self.database,[VIDEO],scopes)
            self.assertEqual(len(self.connection.queries),before+2)
        self.initializer.assert_not_called()
    def test_exact_identity_failure_precedes_all_initialization(self):
        self.queue(one(ids=[VIDEO],scopes=SCOPES,count=1,invalid_id=False),Result(),Result(),Result(),one(matches=False))
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):helper.enqueue_video_collection_ids(self.database,[VIDEO],SCOPES)
        self.initializer.assert_not_called();self.assertEqual(self.connection.commits,0)
    def test_final_fence_failure_rolls_back_all_prioritization(self):
        self.enqueue();self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):helper.enqueue_video_collection_ids(self.database,[VIDEO],SCOPES)
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_borrowed_domain_failure_cannot_be_swallowed_to_commit(self):
        self.queue(one(limit=0))
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('borrowed_queue')) as tx:
                try:helper.backfill_video_collection_state(None,0,transaction=tx)
                except DatabaseServerError:pass
        self.assertEqual((self.connection.commits,self.pool.borrowed),(0,1))
    def test_unknown_commit_never_retries(self):
        self.backfill();self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseError) as caught:helper.backfill_video_collection_state(self.database,10)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.initializer.call_count,1);self.assertEqual(self.pool.borrowed,1)
    def test_borrowed_priority_keeps_null_type_and_zero_input_contract(self):
        self.queue(one(video_type=None,ids=[VIDEO],count=1),Result(has_columns=False,rowcount=0))
        with self.database.transaction(Operation('borrowed_priority')) as tx:
            self.assertEqual(helper.prioritize_typed_video_ids_in_transaction(tx,None,[VIDEO]),0)
        self.queue(one(video_type='short',ids=[],count=0));before=len(self.connection.queries)
        with self.database.transaction(Operation('empty_priority')) as tx:
            self.assertEqual(helper.prioritize_typed_video_ids_in_transaction(tx,'short',[]),0)
        self.assertEqual(len(self.connection.queries),before+1)
    def test_fixed_parameterized_queries_are_accepted_and_bound(self):
        cases=((helper._LIMIT,(1,),True),(helper._SELECT,(1,10),True),(helper._REMAINING,(1,),True),(helper._SCOPES,([VIDEO],),True),
            (helper._PROGRESS,(False,True),False),(helper._INPUT,([VIDEO],Jsonb(SCOPES)),True),
            (helper._LONG_IDS,([VIDEO],),True),(helper._PRIORITIZE_LONG,([VIDEO],),False),
            (helper._TYPED_INPUT,('short',[VIDEO]),True),(helper._PRIORITIZE_TYPED,([VIDEO],'short'),False))
        for query,params,read in cases:
            _reviewed_statement(query,read=read,max_rows=1000 if read else None)
            PostgresQuery(Transformer()).convert(query,params)
        source=(ROOT/'application/db/videos/video_queue_maintenance.py').read_text()
        self.assertNotIn('database.call(',source);self.assertNotIn('SKIP LOCKED',source)

    def test_routine_priority_and_backfill_cannot_reopen_unavailable_episode(self):
        for query in (helper._GAPS,helper._PRIORITIZE_LONG,helper._PRIORITIZE_TYPED):
            self.assertIn('state.availability_first_missing_at IS NULL',query)
            self.assertIn("state.paused_reason IS DISTINCT FROM 'availability_retry_expired'",query)
        conflict=helper._PRIORITIZE_TYPED.split('ON CONFLICT(video_id) DO UPDATE SET',1)[1]
        self.assertNotIn('stats_due_at=',conflict)
        self.assertNotIn('availability_first_missing_at=',conflict)
        self.assertNotIn('availability_resume_observed_at=',conflict)

if __name__=='__main__':unittest.main()
