"""Offline protocol/statement guards for the accepted bounded typed reconciliation."""
from pathlib import Path
import json,sys,unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_typed_reconciliation as helper
from db.videos import typed_reconciliation_schedule as schedule
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
VIDEO='aaaaaaaaaaa';SCOPES=[dict(channel_id='UC0000000000000000000001',control_epoch=17)]
def one(**values):return Result((values,))

STAMP='2026-10-06T12:00:00+00:00'
def acquired(video_id,kind):
    # Native acquisition returns the actual locked row separately from the source.
    previous=dict(video_id=video_id,video_type=kind,paused_reason='typed_identity_not_eligible',
        next_due_at=STAMP,consecutive_failures=0,typed_collection_checked_at=None,typed_collection_status='pending')
    proposed=dict(video_id=video_id,channel_id=SCOPES[0]['channel_id'],video_type=kind,
        hydration_needed=True,typed_source_published_at=None,typed_source_published_acquired_at=None)
    return dict(video_id=video_id,previous=json.dumps(previous),proposed=json.dumps(proposed),
        transaction_at=STAMP,fallback_at=STAMP,typed_input_invalid=False,typed_clock_invalid=False,
        hot_as_of_invalid=False,hot_availability_invalid=True,hot_cap_at=None,cadence=None)

class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close);p=patch.object(helper,'assert_channel_scopes');self.fence=p.start();self.addCleanup(p.stop)
        automatic=patch('db.inventory.automatic_type_reconciliation.reconcile_automatic_video_types',return_value=[])
        self.automatic=automatic.start();self.addCleanup(automatic.stop)
        initialize=patch('db.videos.video_queue_initialization.ensure_typed_collection_state_in_transaction',return_value=0)
        self.initialize=initialize.start();self.addCleanup(initialize.stop)
    def queue(self,*values):self.connection.results.extend(values)
    def success(self,*,kind='short',limit=1,queue=None,inventory=None,paused=0,affected=1):
        queue=[VIDEO] if queue is None else queue;inventory=[VIDEO] if inventory is None else inventory
        self.queue(one(video_type=kind,limit=limit),one(queue_cursor='before_q',inventory_cursor='before_i'),
            one(ids=queue),one(ids=inventory),Result(has_columns=False,rowcount=paused),Result(),Result(),one(scopes=SCOPES),Result(rows=tuple(acquired(video_id,kind) for video_id in inventory[:affected])))
        if affected:self.queue(Result(has_columns=False,rowcount=affected))
        self.queue(Result(has_columns=False,rowcount=1))
    def test_order_and_exact_result_shape_are_one_transaction(self):
        self.success(paused=2)
        value=helper.reconcile_typed_video_collection_state(self.database,'short',1)
        self.assertEqual(value,dict(enabled=True,video_type='short',selected_count=1,affected_count=1,paused_count=2,
            queue_examined_count=1,inventory_examined_count=1,queue_scan_complete=False,inventory_scan_complete=False,
            remaining_count=None,remaining_count_exact=False))
        statements=(helper._INPUT,helper._RUNTIME,helper._QUEUE_IDS,helper._INVENTORY_IDS,helper._PAUSE,
            helper._LOCK_QUEUE,helper._LOCK_INVENTORY,helper._SCOPES,schedule._ACQUIRE,schedule._FINALIZE,helper._PROGRESS)
        self.assertEqual(len(self.connection.queries),len(statements))
        for (query,params),expected in zip(self.connection.queries,statements):self.assertIn(expected,query)
        self.fence.assert_called_once();self.assertEqual(self.fence.call_args.args[1],SCOPES)
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
    def test_raw_full_pages_need_empty_eof_even_with_zero_affected(self):
        self.success(affected=0);value=helper.reconcile_typed_video_collection_state(self.database,'short',1)
        self.assertEqual(value['selected_count'],0);self.assertFalse(value['queue_scan_complete']);self.assertFalse(value['inventory_scan_complete'])
        self.success(queue=[],inventory=[],affected=0);value=helper.reconcile_typed_video_collection_state(self.database,'short',1)
        self.assertTrue(value['queue_scan_complete']);self.assertTrue(value['inventory_scan_complete'])
    def test_queue_and_inventory_progress_are_independent(self):
        self.success(kind='stream',limit=2,queue=[VIDEO],inventory=[VIDEO,'bbbbbbbbbbb'],affected=0)
        value=helper.reconcile_typed_video_collection_state(self.database,'stream',2)
        self.assertTrue(value['queue_scan_complete']);self.assertFalse(value['inventory_scan_complete'])
    def test_valid_limit_1000_has_constant_query_count(self):
        counts=[]
        for length in (1,1000):
            ids=[str(v).zfill(11) for v in range(length)];self.success(limit=1000,queue=ids,inventory=ids,affected=length)
            before=len(self.connection.queries);value=helper.reconcile_typed_video_collection_state(self.database,'short',1000)
            counts.append(len(self.connection.queries)-before);self.assertEqual(value['inventory_examined_count'],length)
        self.assertEqual(counts,[11,11])
    def test_disjoint_cursor_pages_reconcile_in_bounded_chunks_before_other_mutations(self):
        queue=[f'q{i:010d}' for i in range(1000)]
        inventory=[f'i{i:010d}' for i in range(1000)]
        self.success(limit=1000,queue=queue,inventory=inventory,affected=0)
        seen=[]
        def reconcile(tx,ids):
            self.assertLessEqual(len(ids),1000)
            self.assertEqual(len(self.connection.queries),4)
            self.fence.assert_not_called()
            seen.extend(ids)
            return []
        self.automatic.side_effect=reconcile
        helper.reconcile_typed_video_collection_state(self.database,'short',1000)
        self.assertEqual(set(seen),set(queue+inventory))
        self.initialize.assert_called_once()
        self.fence.assert_called_once()

    def test_cast_errors_precede_python_validation(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseError) as caught:helper.reconcile_typed_video_collection_state(self.database,None,'bad')
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
    def test_domain_bounds_reject_before_runtime_lock(self):
        for kind,limit in ((None,1),('long',1),('SHORT',1),('short',None),('short',0),('stream',1001)):
            self.queue(one(video_type=kind,limit=limit));before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,'typed_video_collection_reconcile_input_invalid'):helper.reconcile_typed_video_collection_state(self.database,kind,limit)
            self.assertEqual(len(self.connection.queries)-before,1)
        self.assertEqual(self.connection.commits,0);self.fence.assert_not_called()
    def test_missing_cursor_is_not_created(self):
        self.queue(one(video_type='short',limit=1),Result())
        with self.assertRaisesRegex(DatabaseError,'common_pool_reconcile_cursor_missing'):helper.reconcile_typed_video_collection_state(self.database,'short',1)
        self.assertEqual(len(self.connection.queries),2);self.assertEqual(self.connection.commits,0)
    def test_final_fence_failure_rolls_back_progress_and_children(self):
        self.success();self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):helper.reconcile_typed_video_collection_state(self.database,'short',1)
        self.assertEqual(self.connection.queries[-1][0],helper._PROGRESS);self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_borrowed_domain_failure_poison_cannot_commit(self):
        self.queue(one(video_type='long',limit=1))
        with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
            with self.database.transaction(Operation('reconciliation_parent',total_timeout_ms=60000)) as tx:
                try:helper.reconcile_typed_video_collection_state(None,'long',1,transaction=tx)
                except DatabaseServerError:pass
        self.assertEqual((self.connection.commits,self.pool.borrowed),(0,1))
    def test_successful_borrowed_operation_uses_parent_scope(self):
        self.success()
        with self.database.transaction(Operation('reconciliation_parent',total_timeout_ms=60000)) as tx:
            self.assertEqual(helper.reconcile_typed_video_collection_state(None,'short',1,transaction=tx)['affected_count'],1)
        self.assertEqual((self.connection.commits,self.pool.borrowed),(1,1))
    def test_unknown_commit_has_no_automatic_retry(self):
        self.success();self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseError) as caught:helper.reconcile_typed_video_collection_state(self.database,'short',1)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1);self.fence.assert_called_once()
    def test_raw_pages_do_not_filter_eligibility_or_lock_rows(self):
        for sql in (helper._QUEUE_IDS,helper._INVENTORY_IDS):
            self.assertIn('limit %(limit)s::integer',sql);self.assertIn('order by',sql)
            for word in ('typed_video_collection_candidates','paused_reason','lease_token','FOR UPDATE'):self.assertNotIn(word,sql)
        self.assertIn('for update of state skip locked',helper._PAUSE)
        self.assertNotIn('skip locked',helper._LOCK_QUEUE.lower());self.assertIn('FOR SHARE',helper._LOCK_INVENTORY)
    def test_concurrent_conflict_policy_reads_locked_state_not_input_projection(self):
        conflict=schedule._ACQUIRE.split('ON CONFLICT(video_id) DO UPDATE SET',1)[1]
        self.assertIn('updated_at=state.updated_at',conflict)
        self.assertIn('where state.lease_token is null',conflict)
        self.assertIn('RETURNING state.video_id,state AS previous',conflict)
        self.assertIn('(admitted.previous).consecutive_failures>0',conflict)
        self.assertIn(schedule._SOURCE,schedule._ACQUIRE);self.assertIn('AS MATERIALIZED',schedule._ACQUIRE)
        self.success()
        # OLD failure backoff wins even though the proposed source requests now().
        receipt=self.connection.results[8].rows[0]
        previous=json.loads(receipt['previous']);previous.update(paused_reason='channel_hidden',
            consecutive_failures=2,next_due_at='2026-10-07T00:00:00+00:00')
        receipt['previous']=json.dumps(previous)
        helper.reconcile_typed_video_collection_state(self.database,'short',1)
        plan=self.connection.queries[-2][1][0].obj
        self.assertEqual(plan[0]['base_at'],previous['next_due_at'])
        self.assertEqual(plan[0]['seconds'],0)
        self.assertEqual(plan[0]['channel_id'],SCOPES[0]['channel_id'])
    def test_schedule_uses_shared_python_owner_without_retired_policy_calls(self):
        source=Path(helper.__file__).read_text()+Path(schedule.__file__).read_text()
        self.assertNotIn('database.call(',source)
        for name in ('typed_video_collection_due_at','select_video_collection_policy','hot_video_metadata_due_at'):
            self.assertNotIn('public.'+name+'(',source)
        self.assertIn('schedule = reconcile_schedule(previous, row)',source)
        self.assertIn('tx.execute(_FINALIZE, (Jsonb(plans),))',source)
    def test_lost_type_schedules_identity_check_without_overwriting_unavailable_episode(self):
        self.assertIn('set hydration_needed=true',helper._PAUSE)
        self.assertNotIn("paused_reason='typed_identity_not_eligible',",helper._PAUSE)
        for sql in (helper._PAUSE,schedule._FINALIZE):
            self.assertIn('state.availability_first_missing_at IS NOT NULL',sql)
            self.assertIn("state.paused_reason='availability_retry_expired'",sql)
            self.assertIn('THEN state.next_due_at',sql)
            self.assertNotIn('stats_due_at=null',sql)
            self.assertNotIn('stats_due_at=NULL',sql)
            self.assertNotIn('availability_first_missing_at=',sql)
            self.assertNotIn('availability_resume_observed_at=',sql)
    def test_parameterized_fixed_statements_pass_driver_and_psycopg_binding(self):
        args=dict(video_type='short',limit=1,queue_cursor=None,inventory_cursor=None,queue_ids=[VIDEO],inventory_ids=[VIDEO],scopes=Jsonb(SCOPES))
        for name in ('_INPUT','_RUNTIME','_QUEUE_IDS','_INVENTORY_IDS','_PAUSE','_LOCK_QUEUE','_LOCK_INVENTORY','_SCOPES','_PROGRESS'):
            sql=getattr(helper,name);read=name not in ('_PAUSE','_PROGRESS')
            _reviewed_statement(sql,read=read,max_rows=1000 if read else None)
            PostgresQuery(Transformer()).convert(sql,('short',1) if name=='_INPUT' else args)
        PostgresQuery(Transformer()).convert(schedule._ACQUIRE,schedule.acquisition_parameters(self.database,{key:args[key] for key in schedule._ACQUIRE_KEYS}))
        # The generic write entry still rejects leading WITH; acquisition uses its named mode.
        with self.assertRaises(DatabaseError):_reviewed_statement(schedule._ACQUIRE,read=False,max_rows=None)
        _reviewed_statement(schedule._FINALIZE,read=False,max_rows=None)
        PostgresQuery(Transformer()).convert(schedule._FINALIZE,(Jsonb([]),))
        self.assertIn('(%(queue_ids)s::text[])[cardinality',helper._PROGRESS)

if __name__=='__main__':unittest.main()
