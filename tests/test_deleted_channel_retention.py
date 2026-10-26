"""Focused protocol tests; native PG parity is a separate acceptance gate."""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.retention import deleted_channel_retention as subject
from db.base.postgres import DatabaseServerError
from db.retention.inventory_retention import video_payload

AT='2026-10-06T00:00:00+00:00'
FLOOR='2026-09-08T00:00:00+00:00'
CHANNEL='UC'+'a'*22
OWNER='e0000000-0000-4000-8000-000000000001'

class Transaction:
    def __init__(self):
        self.events=[];self.candidates=[];self.pending=[];self.busy=False
        self.locked=dict(channel_id=CHANNEL,owner=OWNER,control_epoch=7,has_payload=False)
        self.fence=True;self.lock_error=None;self.final_error=None;self.failed=False;self.depth=0
    @contextmanager
    def borrow(self):
        try:yield self
        except BaseException:self.failed=True;raise
    @contextmanager
    def savepoint(self):
        self.depth+=1
        try:yield self
        finally:self.depth-=1
    def fetch_one(self,sql,params=()):
        self.events.append((sql,params,self.depth))
        if sql==subject._HEADER:
            return dict(target=params[0],after_id=params[1],limit=params[2],at=AT,cutoff=FLOOR)
        if sql==subject._LOCK:
            if self.lock_error:raise DatabaseServerError('database_operation_failed',code=self.lock_error)
            return {**self.locked,'channel_id':params['channel']} if self.locked is not None else None
        if sql==subject._FENCE:
            if self.final_error:raise DatabaseServerError('database_operation_failed',code=self.final_error)
            return {'channel_id':params['channel_id'],'valid':self.fence}
        if sql==subject._BUSY:return {'busy':self.busy}
        raise AssertionError(sql)
    def fetch_all(self,sql,params=(),*,max_rows):
        self.events.append((sql,params,self.depth))
        if sql==subject._PAGE:return self.candidates
        if sql==subject._PENDING_PAGE:return self.pending
        raise AssertionError(sql)
    def execute(self,sql,params=()):
        self.events.append((sql,params,self.depth))
        if sql=='DELETE FROM public.channels WHERE channel_id=ANY(%s::text[])':return len(params[0])
        raise AssertionError(sql)

class DeletedChannelRetentionTests(unittest.TestCase):
    def invoke(self,tx,**kwargs):
        def drain(transaction,ids):
            self.assertIs(transaction,tx)
            if ids:
                self.assertEqual(tx.events[-1][0],subject._FENCE)
                self.assertEqual(ids,[CHANNEL])
                self.assertTrue(tx.fence)
                self.assertFalse(tx.busy)
                self.assertFalse(tx.locked['has_payload'])
        with patch.object(subject,'drain_channel_memberships',side_effect=drain):return subject.cleanup_hot_deleted_channels(tx,target=kwargs.pop('target','deleted_channels'),**kwargs)
    def candidate(self):
        tx=Transaction();tx.candidates=[{'channel_id':CHANNEL}];return tx
    def test_empty_and_null_target_keep_original_sql_semantics(self):
        for target in ('deleted_channels',None):
            tx=Transaction();result=self.invoke(tx,target=target)
            self.assertEqual((result['target'],result['deleted'],result['scanned']),(target,0,0))
            self.assertTrue(result['complete'])
    def test_invalid_limit_or_target_precedes_any_candidate(self):
        for changes in ({'limit':None},{'limit':0},{'limit':101},{'target':'invalid'}):
            tx=Transaction()
            with self.assertRaises(DatabaseServerError) as error:self.invoke(tx,**changes)
            self.assertEqual(str(error.exception),'channel_trash_invalid_cleanup')
            self.assertEqual(len(tx.events),1);self.assertTrue(tx.failed)
    def test_busy_owner_defers_but_still_receives_final_fence(self):
        tx=self.candidate();tx.busy=True
        with patch.object(subject,'_children',side_effect=AssertionError('must not delete')):
            result=self.invoke(tx)
        self.assertEqual((result['deleted'],result['deferred'],result['unknown_expiry_remaining'],result['confirmed_expired_remaining']),(0,1,0,1))
        self.assertEqual([x[2] for x in tx.events if x[0]==subject._FENCE],[0])
    def test_payload_defers_without_children(self):
        tx=self.candidate();tx.locked['has_payload']=True
        with patch.object(subject,'_children',side_effect=AssertionError('must not delete')):
            self.assertEqual(self.invoke(tx)['deferred'],1)
    def test_restore_before_channel_lock_skips_all_child_and_fence_work(self):
        tx=self.candidate();tx.locked=None
        result=self.invoke(tx)
        self.assertEqual((result['scanned'],result['deleted'],result['deferred']),(1,0,0))
        self.assertNotIn(subject._FENCE,[x[0] for x in tx.events])
    def test_completed_children_delete_identity_only_after_fence(self):
        tx=self.candidate()
        with patch.object(subject,'_children',return_value=(4,True)):
            result=self.invoke(tx,limit=1)
        self.assertEqual((result['deleted'],result['complete'],result['next_cursor']),(5,False,CHANNEL))
        self.assertEqual(tx.events[-2][0],subject._FENCE)
        self.assertEqual(tx.events[-1][1],([CHANNEL],))
    def test_incomplete_children_keep_identity_and_report_progress(self):
        tx=self.candidate()
        with patch.object(subject,'_children',return_value=(4,False)):
            result=self.invoke(tx)
        self.assertEqual((result['deleted'],result['deferred']),(4,1))
        self.assertEqual(tx.events[-1][1],([],))
    def test_unfinished_removal_reopens_the_page_without_a_cursor(self):
        # The pass reads the page again at once, so a large catalogue does not
        # take one page of children per pass; no progress closes the page.
        tx=self.candidate()
        with patch.object(subject,'_children',return_value=(4,False)):
            result=self.invoke(tx)
        self.assertEqual((result['deleted'],result['deferred'],result['complete'],result['next_cursor']),(4,1,False,None))
        tx=self.candidate()
        with patch.object(subject,'_children',return_value=(0,False)):
            result=self.invoke(tx)
        self.assertEqual((result['deferred'],result['complete'],result['next_cursor']),(1,True,None))
    def test_only_rows_with_data_keep_the_channel_busy(self):
        # What expiry leaves (identity-only inventory, video rows without API
        # data) is removed as a child once nothing else refers to the video.
        self.assertIn('video_inventory WHERE channel_id=%(channel)s::text AND skeleton_since IS NULL',subject._BUSY)
        self.assertIn(video_payload('v'),subject._BUSY)
        videos,inventory=subject._CHILD_DELETES[-2:]
        self.assertTrue(videos.startswith('DELETE FROM public.videos ') and 'NOT '+video_payload('v2') in videos)
        self.assertTrue(inventory.startswith('DELETE FROM public.video_inventory ') and 'i2.skeleton_since IS NOT NULL' in inventory)
        for table in ('videos','video_snapshots','video_inventory_memberships','typed_video_collection_receipts','video_type_review_cases'):
            self.assertIn('NOT EXISTS(SELECT 1 FROM public.'+table+' ',inventory)
        for table in ('video_inventory','videos'):
            self.assertIn('EXISTS(SELECT 1 FROM public.'+table+' WHERE channel_id=%(channel)s::text)',subject._CHILDREN)
    def test_row_lock_conflict_and_deadlock_do_not_acquire_final_scope(self):
        for code in ('55P03','40P01'):
            tx=self.candidate();tx.lock_error=code
            result=self.invoke(tx)
            self.assertEqual((result['deleted'],result['deferred'],result['unknown_expiry_remaining'],result['confirmed_expired_remaining']),(0,1,0,1))
            self.assertNotIn(subject._FENCE,[x[0] for x in tx.events])
    def test_child_error_rolls_back_counter_and_candidate_scope(self):
        tx=self.candidate()
        with patch.object(subject,'_children',side_effect=DatabaseServerError('database_operation_failed',code='55P03')):
            result=self.invoke(tx)
        self.assertEqual((result['deleted'],result['deferred']),(0,1))
        self.assertNotIn(subject._FENCE,[x[0] for x in tx.events])
    def test_final_scope_loss_aborts_instead_of_becoming_deferred(self):
        tx=self.candidate();tx.fence=False
        with patch.object(subject,'_children',return_value=(4,True)):
            with self.assertRaises(DatabaseServerError) as error:self.invoke(tx)
        self.assertEqual(str(error.exception),'channel_collection_scope_lost')
        self.assertTrue(tx.failed)
        self.assertFalse(any(x[0].startswith('DELETE FROM public.channels') for x in tx.events))
    def test_final_lock_failure_is_not_caught_by_candidate_handler(self):
        tx=self.candidate();tx.final_error='55P03';tx.busy=True
        with self.assertRaises(DatabaseServerError):self.invoke(tx)
        self.assertTrue(tx.failed)
    def test_pending_only_runs_on_null_cursor_and_does_not_change_scanned(self):
        tx=Transaction();tx.pending=[{'id':OWNER}]
        with patch.object(subject,'_pending',return_value=3) as pending:
            result=self.invoke(tx)
            self.assertEqual((result['scanned'],result['deleted']),(0,3));pending.assert_called_once()
        tx=Transaction();tx.pending=[{'id':OWNER}]
        with patch.object(subject,'_pending',side_effect=AssertionError('must not run')):
            self.invoke(tx,after_id='')
        self.assertNotIn(subject._PENDING_PAGE,[x[0] for x in tx.events])
    def test_unexpected_row_error_aborts_without_retry(self):
        tx=self.candidate();tx.lock_error='42501'
        with self.assertRaises(DatabaseServerError) as error:self.invoke(tx)
        self.assertEqual(error.exception.code,'42501');self.assertTrue(tx.failed)
        self.assertEqual(sum(x[0]==subject._LOCK for x in tx.events),1)

    def test_first_invalid_scope_precedes_later_locked_scope(self):
        second='UC'+'b'*22
        class Ordered(Transaction):
            def fetch_one(self,sql,params=()):
                if sql==subject._FENCE and params['channel_id']==second:
                    raise DatabaseServerError('database_operation_failed',code='55P03')
                return super().fetch_one(sql,params)
        tx=Ordered();tx.candidates=[{'channel_id':CHANNEL},{'channel_id':second}];tx.busy=True;tx.fence=False
        with self.assertRaises(DatabaseServerError) as error:self.invoke(tx)
        self.assertEqual(error.exception.message,'channel_collection_scope_lost')
        self.assertEqual([params['channel_id'] for sql,params,depth in tx.events if sql==subject._FENCE],[CHANNEL])
        self.assertTrue(tx.failed)

    def test_pending_snapshot_keeps_101st_header_after_concurrent_last_member_remove(self):
        ids=[f'c0000000-0000-4000-8000-{i:012d}' for i in range(1,102)]
        class Pending:
            def __init__(self):self.reads=[];self.headers=[];self.revisions=[]
            def fetch_all(self,sql,params=(),*,max_rows):
                self.reads.append((sql,max_rows));return [{'collection_id':key} for key in ids]
            def fetch_one(self,sql,params=()):
                if sql==subject._PENDING_LOCK:return {'id':OWNER}
                self.headers.append(params['collection']);return {'id':params['collection']}
            def execute(self,sql,params=()):
                if sql.startswith('DELETE FROM public.saved_collection_members'):
                    # A concurrent Saved remove commits for the last collection
                    # after the cleanup's ID snapshot, before that header lock.
                    return 0 if params['collection']==ids[-1] else 1
                if sql==subject._MEMBER_COUNT:self.revisions.append((params['collection'],params['count']))
                return 0
        tx=Pending();removed=subject._pending(tx,{'owner':OWNER,'at':AT})
        self.assertEqual(removed,101)
        self.assertEqual(tx.reads,[(subject._PENDING_COLLECTIONS,100000)])
        self.assertNotIn('LIMIT',subject._PENDING_COLLECTIONS)
        self.assertNotIn('::text',subject._PENDING_COLLECTIONS)
        self.assertEqual(tx.headers,ids)
        self.assertEqual(tx.revisions[-1],(ids[-1],0))


class ClosedDriverTests(unittest.TestCase):
    def setUp(self):
        from db.base.postgres import PostgresConfig,PostgresDatabase
        from test_direct_pg_transactions import Connection,Pool,DSN
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),pool_factory=lambda config:self.pool,admission=lambda name:None).open()
        self.addCleanup(self.database.close)
    def empty_page(self):
        from test_direct_pg_transactions import Result
        self.connection.results.extend((Result(rows=(dict(roles_valid=True,relations_valid=True,privileges_valid=True),)),Result(rows=(dict(target='deleted_channels',after_id=None,limit=100,at=AT,cutoff=FLOOR),)),Result(),Result(),Result(has_columns=False,rowcount=0)))
    def test_closed_entry_preserves_native_clock_and_one_transaction(self):
        self.empty_page();result=self.database.cleanup_hot_deleted_channels(target='deleted_channels')
        self.assertEqual((result['as_of'],result['cutoff']),(AT,FLOOR))
        self.assertEqual((result['scanned'],result['deleted'],result['complete']),(0,0,True))
        self.assertEqual((self.pool.borrowed,self.connection.commits),(1,1))
        from db.readiness.deleted_channel_retention_capabilities import READINESS_SQL
        self.assertIn(READINESS_SQL,self.connection.queries[0][0])
        self.assertIn(subject._HEADER,self.connection.queries[1][0])
    def test_closed_maintenance_gate_denies_ordinary_sql_and_arbitrary_callback(self):
        import os,hot_runtime
        from unittest.mock import Mock
        from db.base.postgres import Operation,_runtime_admission
        self.database._admission=_runtime_admission
        controller=Mock();controller.require_collection_ready.side_effect=hot_runtime.HotRuntimeUnavailable('hot_runtime_not_ready')
        env={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres','LIC_DATABASE_ID':'91f5d9f3-9230-4ee6-b170-155d4c3c1719','LIC_HOT_RUNTIME_SECRET':'internal_runtime_secret_0123456789'}
        with patch.dict(os.environ,env,clear=True),patch.object(hot_runtime,'_local_controller',controller):
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):self.database.cleanup_hot_deleted_channels(target='deleted_channels')
            with hot_runtime.maintenance_access():
                self.empty_page();self.database.cleanup_hot_deleted_channels(target='deleted_channels')
                with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                    with self.database.transaction(Operation('cleanup_hot_deleted_channels')):pass
                for argument in ({'callback':lambda tx:None},{'statement':'DELETE FROM channels'}):
                    with self.assertRaises(TypeError):self.database.cleanup_hot_deleted_channels(target='deleted_channels',**argument)
        self.assertEqual(self.pool.borrowed,1)

if __name__=='__main__':unittest.main()