"""Scoped scheduled owner and advisory keyset tests; no database execution."""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from db.videos import video_scheduled_claims as subject
from db.videos.video_collection import VideoCollectionRepository
from db.base.postgres import DatabaseUnavailable, _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_video_scheduled_claims import Tx, RUN

CHANNEL='UC'+'a'*22
OTHER='UC'+'b'*22


class ScopedTx(Tx):
    def fetch_one(self,sql,params=()):
        if sql == subject._MIXED_WINDOW:
            self.events.append((sql,params))
            return dict(ids=self.ids,scan_exhausted=getattr(self,'window_exhausted',True))
        if sql in subject._SCOPED_WINDOW.values():
            self.events.append((sql,params))
            return {'ids':self.ids}
        return super().fetch_one(sql,params)


class ReadDatabase:
    def __init__(self,rows):self.rows=rows;self.calls=[];self.operations=[]
    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation)
        yield self
    def fetch_all(self,sql,params,*,max_rows):
        self.calls.append((sql,params,max_rows))
        return list(self.rows)


class ScopedClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tx=ScopedTx()
        self.fences=[]
        def fence(tx,scopes):
            self.assertFalse(tx.cursor)
            self.fences.append(scopes)
            tx.events.append(('final_fence',))
        guard=patch.object(subject,'assert_channel_scopes',side_effect=fence)
        guard.start();self.addCleanup(guard.stop)

    def claim(self,kind='long',**kwargs):
        return subject.claim_due_video_collection_batch(None,50,'worker',RUN,
            transaction=self.tx,scope=dict(channel_id=CHANNEL,video_type=kind),**kwargs)

    def seed(self,rows):
        self.tx.rows=rows;self.tx.ids=[row['video_id'] for row in rows]

    def test_scoped_raw_window_precedes_same_cursor_and_final_fence(self):
        self.seed([dict(video_id='abcdefghijk',channel_id=CHANNEL,video_type='long')])
        self.assertEqual(len(self.claim()['rows']),1)
        self.assertIn((subject._SCOPED_WINDOW[False],(CHANNEL,'long')),self.tx.events)
        self.assertEqual(self.tx.events[-2][0],'cursor_close')
        self.assertEqual(self.tx.events[-1][0],'final_fence')
        self.assertEqual(self.fences,[[dict(channel_id=CHANNEL,control_epoch=7)]])

    def test_moved_channel_or_format_is_neither_adopted_nor_paused(self):
        self.seed([dict(video_id='foreignchan',channel_id=OTHER,video_type='long'),
                   dict(video_id='foreignkind',channel_id=CHANNEL,video_type='short'),
                   dict(video_id='owned000001',channel_id=CHANNEL,video_type='long')])
        result=self.claim()
        self.assertEqual([row['video_id'] for row in result['rows']],['owned000001'])
        self.assertEqual(result['inspected_count'],3)
        self.assertFalse(any(row[0]==subject._PAUSE for row in self.tx.events))
        self.assertEqual(sum(row[0]==subject._CLAIM for row in self.tx.events),1)

    def test_null_identity_scope_retains_identity_refresh_owner(self):
        self.seed([dict(video_id='unknown0001',channel_id=CHANNEL,video_type=None)])
        result=self.claim('unknown')
        self.assertEqual(result['rows'][0]['collection_mode'],'identity_refresh')
        self.assertEqual(result['rows'][0]['video_type'],'unknown')
        self.assertIn((subject._SCOPED_WINDOW[True],(CHANNEL,)),self.tx.events)

    def test_invalid_scope_fails_before_transaction_or_cursor(self):
        for scope in ({},dict(channel_id=CHANNEL,video_type=None),dict(channel_id='bad',video_type='long'),
                      dict(channel_id=CHANNEL,video_type='long',extra=True)):
            with self.assertRaises(ValueError):
                subject.claim_due_video_collection_batch(None,1,'worker',RUN,transaction=self.tx,scope=scope)
        self.assertEqual(self.tx.events,[])

    def test_scoped_unknown_commit_is_not_retried(self):
        self.seed([dict(video_id='abcdefghijk',channel_id=CHANNEL,video_type='long')])
        class Database:
            calls=0
            @contextmanager
            def transaction(inner,operation):
                inner.calls+=1
                self.assertTrue(operation.disable_jit)
                yield self.tx
                raise DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        database=Database()
        with self.assertRaises(DatabaseUnavailable):
            subject.claim_due_video_collection_batch(database,1,'worker',RUN,
                scope=dict(channel_id=CHANNEL,video_type='long'))
        self.assertEqual(database.calls,1)

    def test_facade_preserves_scope_and_rejects_foreign_response(self):
        repo=VideoCollectionRepository(Mock())
        scope=dict(channel_id=CHANNEL,video_type='long')
        batch=dict(rows=[],inspected_count=0,paused_count=0,scan_exhausted=True)
        with patch.object(subject,'claim_due_video_collection_batch',return_value=batch) as action:
            self.assertEqual(repo.claim_due_batch(limit=1,worker_id='worker',run_id=RUN,scope=scope),batch)
            self.assertEqual(action.call_args.kwargs['scope'],scope)
        row=dict(video_id='abcdefghijk',channel_id=OTHER,video_type='long',collection_mode='counters',
                 lease_token=RUN,lease_expires_at='native',next_due_at='native')
        with patch.object(subject,'claim_due_video_collection_batch',return_value=dict(
                rows=[row],inspected_count=1,paused_count=0,scan_exhausted=True)):
            with self.assertRaises(RuntimeError):repo.claim_due_batch(limit=1,worker_id='w',run_id=RUN,scope=scope)

    def test_mixed_window_uses_one_cursor_and_one_final_fence(self):
        requested=[dict(channel_id=CHANNEL,video_type='long'),
                   dict(channel_id=OTHER,video_type='short'),
                   dict(channel_id=CHANNEL,video_type='unknown')]
        self.seed([dict(video_id='long0000001',channel_id=CHANNEL,video_type='long'),
                   dict(video_id='short000001',channel_id=OTHER,video_type='short'),
                   dict(video_id='unknown0001',channel_id=CHANNEL,video_type=None),
                   dict(video_id='long0000002',channel_id=CHANNEL,video_type='long')])
        self.tx.mode.update(long0000001='metadata', long0000002='metadata')
        result=subject.claim_due_video_collection_batch(None,50,'worker',RUN,
            transaction=self.tx,scopes=requested)
        self.assertEqual([row['video_id'] for row in result['rows']],self.tx.ids)
        self.assertEqual([row['video_type'] for row in result['rows']],['long','short','unknown','long'])
        self.assertEqual(set(result),{'rows','inspected_count','paused_count','scan_exhausted'})
        windows=[event for event in self.tx.events if event[0]==subject._MIXED_WINDOW]
        self.assertEqual(len(windows),1)
        self.assertEqual(windows[0][1],dict(channels=[CHANNEL,OTHER,CHANNEL],
            types=['long','short','unknown'],scope_limit=333,operation=None,run_id=RUN))
        self.assertEqual(sum(event[0]=='cursor_open' for event in self.tx.events),1)
        self.assertEqual(self.tx.events[-2][0],'cursor_close')
        self.assertEqual(self.tx.events[-1][0],'final_fence')
        self.assertEqual(self.fences,[[dict(channel_id=CHANNEL,control_epoch=7),
                                      dict(channel_id=OTHER,control_epoch=7)]])

    def test_mixed_cursor_rejects_moved_channel_or_type_before_pause(self):
        self.seed([dict(video_id='foreignchan',channel_id=OTHER,video_type='long'),
                   dict(video_id='foreignkind',channel_id=CHANNEL,video_type='short'),
                   dict(video_id='owned000001',channel_id=OTHER,video_type='short')])
        self.tx.eligible['foreignkind']=False
        result=subject.claim_due_video_collection_batch(None,50,'worker',RUN,
            transaction=self.tx,scopes=[dict(channel_id=CHANNEL,video_type='long'),
                                       dict(channel_id=OTHER,video_type='short')])
        self.assertEqual([row['video_id'] for row in result['rows']],['owned000001'])
        self.assertEqual(result['inspected_count'],3)
        self.assertFalse(any(event[0]==subject._PAUSE for event in self.tx.events))

    def test_mixed_saturated_raw_scope_requires_revisit_below_global_cap(self):
        self.seed([dict(video_id='owned000001',channel_id=CHANNEL,video_type='long')])
        self.tx.window_exhausted=False
        result=subject.claim_due_video_collection_batch(None,50,'worker',RUN,
            transaction=self.tx,scopes=[dict(channel_id=CHANNEL,video_type='long')])
        self.assertEqual((result['inspected_count'],len(result['rows'])),(1,1))
        self.assertFalse(result['scan_exhausted'])

    def test_mixed_short_window_at_claim_limit_requires_revisit(self):
        self.seed([dict(video_id='owned000001',channel_id=CHANNEL,video_type='long'),
                   dict(video_id='owned000002',channel_id=OTHER,video_type='short')])
        result=subject.claim_due_video_collection_batch(None,1,'worker',RUN,
            transaction=self.tx,scopes=[dict(channel_id=CHANNEL,video_type='long'),
                                       dict(channel_id=OTHER,video_type='short')])
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['inspected_count'],2)
        self.assertFalse(result['scan_exhausted'])

    def test_mixed_invalid_scope_window_fails_before_transaction(self):
        scope=dict(channel_id=CHANNEL,video_type='long')
        for scopes in ([],{},[None],[scope,scope],[scope]*51,('bad',)):
            with self.subTest(scopes=scopes):
                with self.assertRaises(ValueError):
                    subject.claim_due_video_collection_batch(None,50,'worker',RUN,
                        transaction=self.tx,scopes=scopes)
        with self.assertRaises(ValueError):
            subject.claim_due_video_collection_batch(None,50,'worker',RUN,
                transaction=self.tx,scope=scope,scopes=[scope])
        self.assertEqual(self.tx.events,[])

    def test_mixed_facade_accepts_only_requested_pairs_and_unique_ids(self):
        requested=[dict(channel_id=CHANNEL,video_type='long'),
                   dict(channel_id=OTHER,video_type='unknown')]
        first=dict(video_id='long0000001',channel_id=CHANNEL,video_type='long',collection_mode='metadata',
            lease_token=RUN,lease_expires_at='native',next_due_at='native')
        second=dict(first,video_id='unknown0001',channel_id=OTHER,video_type='unknown',collection_mode='identity_refresh')
        batch=dict(rows=[first,second],inspected_count=2,paused_count=0,scan_exhausted=True)
        repo=VideoCollectionRepository(Mock())
        with patch.object(subject,'claim_due_video_collection_batch',return_value=batch) as action:
            result=repo.claim_due_batch(limit=50,worker_id='worker',run_id=RUN,scopes=tuple(requested))
            self.assertEqual(action.call_args.kwargs['scopes'],requested)
            self.assertEqual([(row.channel_id,row.video_type) for row in result['rows']],
                             [(CHANNEL,'long'),(OTHER,'unknown')])
        for rows in ([dict(first,channel_id=OTHER)], [first,first],
                     [dict(first,collection_mode='counters'),second]):
            with patch.object(subject,'claim_due_video_collection_batch',return_value=dict(batch,rows=rows)):
                with self.assertRaises(RuntimeError):
                    repo.claim_due_batch(limit=50,worker_id='worker',run_id=RUN,scopes=requested)

    def test_mixed_unknown_ack_does_not_retry_or_return_progress(self):
        self.seed([dict(video_id='owned000001',channel_id=CHANNEL,video_type='long')])
        class Database:
            calls=0
            @contextmanager
            def transaction(inner,operation):
                inner.calls+=1
                self.assertTrue(operation.disable_jit)
                yield self.tx
                raise DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
        database=Database()
        with self.assertRaises(DatabaseUnavailable) as raised:
            subject.claim_due_video_collection_batch(database,50,'worker',RUN,
                scopes=[dict(channel_id=CHANNEL,video_type='long')])
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(database.calls,1)
        self.assertEqual(len(self.fences),1)

    def test_facade_accepts_mixed_stats_and_identity_refresh_for_retained_types(self):
        repo=VideoCollectionRepository(Mock())
        scopes=[dict(channel_id=CHANNEL,video_type=kind) for kind in ('long','short','stream')]
        for operation,mode in (('counters','counters'),('metadata','identity_refresh')):
            with self.subTest(mode=mode):
                rows=[dict(video_id=kind,channel_id=CHANNEL,video_type=kind,collection_mode=mode,
                    lease_token=RUN,lease_expires_at='native',next_due_at='native')
                    for kind in ('long','short','stream')]
                batch=dict(rows=rows,inspected_count=3,paused_count=0,scan_exhausted=True)
                with patch.object(subject,'claim_due_video_collection_batch',return_value=batch):
                    result=repo.claim_due_batch(limit=50,worker_id='worker',run_id=RUN,
                        scopes=scopes,operation=operation)
                self.assertEqual([row.collection_mode for row in result['rows']],[mode]*3)

    def test_mixed_sql_has_bound_arrays_and_reviewed_shape(self):
        self.assertTrue(_reviewed_statement(subject._MIXED_WINDOW,read=True,max_rows=1))
        query=PostgresQuery(Transformer())
        query.convert(subject._MIXED_WINDOW,dict(channels=[CHANNEL,OTHER],
            types=['long','unknown'],scope_limit=500,operation=None,run_id=RUN))
        self.assertEqual(len(query.params),5)
        self.assertIn(b'ORDER BY turn,due_at,scope_order,video_id',query.query)
        self.assertNotIn(b'OFFSET',query.query)

    def test_physical_empty_page_advances_and_reads_no_lease(self):
        database=ReadDatabase([dict(channel_id=CHANNEL,eligible=False),dict(channel_id=OTHER,eligible=False)])
        page=subject.due_candidate_page(database,video_type='long',run_id=RUN,limit=2)
        self.assertEqual(page,dict(rows=[],next_after=OTHER,inspected_count=2,scan_exhausted=False))
        self.assertTrue(database.operations[0].read_only)
        self.assertTrue(database.operations[0].disable_jit)
        self.assertEqual(database.operations[0].statement_timeout_ms,5000)
        sql=database.calls[0][0]
        self.assertNotIn('FOR UPDATE',sql)
        self.assertNotIn('OFFSET',sql)
        self.assertIn('WITH page AS MATERIALIZED',sql)
        self.assertIn('last_claim_run_id IS DISTINCT',sql)
        self.assertIn('NOT page.is_hidden',sql)
        self.assertEqual(database.calls[0][2],2)

    def test_candidate_result_is_source_free_and_facade_uses_same_owner(self):
        database=ReadDatabase([dict(channel_id=OTHER,eligible=True,operation='metadata',ready_count=1,due_at='now')])
        page=VideoCollectionRepository(database).due_candidate_page(video_type='unknown',run_id=RUN,after=CHANNEL,limit=2)
        self.assertEqual(page['rows'],[dict(channel_id=OTHER,video_type='unknown',operation='metadata',ready_count=1,due_at='now')])
        self.assertTrue(page['scan_exhausted'])
        self.assertIn('state.video_type IS NULL',database.calls[0][0])

    def test_keyset_validation_and_nonmonotonic_result_fail_closed(self):
        database=ReadDatabase([])
        for kwargs in ({'limit':0},{'limit':101},{'limit':True},{'after':'bad'},{'run_id':'bad'},{'video_type':'live'}):
            args=dict(video_type='long',run_id=RUN);args.update(kwargs)
            with self.assertRaises(ValueError):subject.due_candidate_page(database,**args)
        self.assertEqual(database.operations,[])
        for rows in ([dict(channel_id=CHANNEL,eligible=True)], [dict(channel_id=OTHER,eligible=None)]):
            with self.assertRaises(ValueError):
                subject.due_candidate_page(ReadDatabase(rows),video_type='long',run_id=RUN,after=CHANNEL)

    def test_new_sql_uses_reviewed_read_shape_and_bound_parameters(self):
        for unknown,sql in subject._CANDIDATE_PAGES.items():
            self.assertTrue(_reviewed_statement(sql,read=True,max_rows=100))
            query=PostgresQuery(Transformer())
            query.convert(sql,dict(after=None,limit=16,run_id=RUN,video_type='unknown' if unknown else 'long',
                operation=None,metadata_after=None,counters_after=None,metadata_enabled=True,counters_enabled=True))
            self.assertEqual(len(query.params),9 if not unknown else 8)
        for unknown,sql in subject._SCOPED_WINDOW.items():
            query=PostgresQuery(Transformer())
            query.convert(sql,(CHANNEL,) if unknown else (CHANNEL,'long'))
            self.assertIn(b'LIMIT 1000',query.query)


if __name__=='__main__':unittest.main()
