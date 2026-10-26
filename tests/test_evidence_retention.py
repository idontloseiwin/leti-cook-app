"""Evidence cleanup locks, clock preservation, cursor bounds and failure atomicity."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.retention.evidence_retention import _page,_TARGETS
from db.base.postgres import DatabaseServerError,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
AT='2026-10-05T12:00:00+00:00';CUTOFF='2026-09-07T12:00:00+00:00';EXPIRED='2026-09-01T00:00:00+00:00'
VIDEO='aaaaaaaaaaa'
# Mutable evidence keeps the per-row protocol; immutable evidence runs it per page.
ROW='availability_events'

def one(**values):return Result(rows=(values,))
def count(value):return Result(has_columns=False,rowcount=value)
def item(**changes):
    return dict(dict(row_data={'id':1,'video_id':VIDEO,'recorded_at':EXPIRED},work_at=EXPIRED,work_key='00000000000000000001',video_id=VIDEO,
        expired=True,invalid_clock=False,cursor='{"target": "video_snapshots", "at": "2026-09-01T00:00:00+00:00", "key": "00000000000000000001"}'),**changes)


class EvidenceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.trace=[];self.connection=Connection(self.trace);self.pool=Pool(self.connection,self.trace);self.gates=[]
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=self.gates.append,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*results):self.connection.results.extend(results)
    def header(self,target='video_snapshots',after_id=None,limit=100):
        self.queue(one(target=target,after_id=after_id,limit=limit),one(at=AT,cutoff=CUTOFF))
    def run_page(self,target='video_snapshots',**kwargs):return self.database.cleanup_hot_evidence(target=target,**kwargs)
    def test_empty_page_has_single_commit_and_finite_gate(self):
        self.header();self.queue(Result(rows=()))
        value=self.run_page()
        self.assertEqual((value['scanned'],value['complete'],value['next_cursor']),(0,True,None))
        self.assertEqual(value['telemetry_scope'],'scanned_page');self.assertIsNone(value['remaining'])
        self.assertTrue(value['global_remaining_unknown']);self.assertEqual(self.connection.commits,1)
        self.assertEqual(self.pool.borrowed,1);self.assertEqual(set(self.gates),{'cleanup_hot_evidence'})
    def test_native_integer_overflow_precedes_domain_validation(self):
        self.queue(Result(execute_error=ServerFailure('22003')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_page('bad',limit=2147483648)
        self.assertEqual(caught.exception.code,'22003');self.assertEqual(len(self.connection.queries),1)
    def test_cursor_validation_precedes_unknown_target(self):
        self.header('unknown',after_id='bad-json');self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_page('unknown',after_id='bad-json')
        self.assertEqual(caught.exception.message,'hot_cleanup_invalid_cursor');self.assertEqual(self.connection.commits,0)
    def test_wrong_target_and_oversized_page_fail_before_page_query(self):
        for target,limit,message in [('unknown',100,'hot_cleanup_invalid_target'),('video_snapshots',501,'hot_cleanup_invalid_limit')]:
            self.header(target,limit=limit)
            with self.assertRaises(DatabaseServerError) as caught:self.run_page(target,limit=limit)
            self.assertEqual(caught.exception.message,message)
        self.assertFalse(any('FROM public.' in query for query,params in self.connection.queries))
    def test_cancelled_cursor_is_not_disguised_as_bad_user_input(self):
        self.header(after_id='{}');self.queue(Result(execute_error=ServerFailure('57014')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_page(after_id='{}')
        self.assertEqual(caught.exception.code,'57014')
    def test_noop_proof_precedes_any_owner_lock(self):
        self.header('availability_events');self.queue(Result(rows=(item(),)),one(eligible=False))
        value=self.run_page('availability_events')
        self.assertEqual((value['scanned'],value['deferred'],value['deleted']),(1,0,0))
        self.assertFalse(any('FOR UPDATE' in query for query,params in self.connection.queries))
    def test_live_lease_defers_and_preserves_expired_source_clock(self):
        self.header(ROW);self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=True,expires_at=AT),one(active=True),one(value=EXPIRED))
        value=self.run_page(ROW)
        self.assertEqual((value['deferred'],value['oldest_remaining_expired_source_at']),(1,EXPIRED))
        self.assertFalse(any(query.startswith('DELETE') for query,params in self.connection.queries))
    def test_busy_queue_rolls_back_only_candidate_and_reports_lag(self):
        self.header(ROW);self.queue(Result(rows=(item(),)),one(eligible=True),Result(execute_error=ServerFailure('55P03')),one(value=EXPIRED))
        value=self.run_page(ROW);self.assertEqual(value['deferred'],1);self.assertEqual(self.connection.commits,1)
    def test_missing_or_locked_evidence_is_deferred_after_queue_lock(self):
        self.header(ROW);self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=False,expires_at=None),Result(rows=()),one(value=EXPIRED))
        value=self.run_page(ROW);self.assertEqual(value['deferred'],1)
        statements=[query for query,params in self.connection.queries]
        self.assertLess(next(i for i,q in enumerate(statements) if 'video_collection_state' in q),next(i for i,q in enumerate(statements) if 'SKIP LOCKED' in q))
    def test_concurrent_refresh_wins_after_locked_recheck(self):
        self.header(ROW);self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=False,expires_at=None),one(value={'id':1}),one(at=AT),one(eligible=False))
        value=self.run_page(ROW);self.assertEqual((value['deleted'],value['deferred']),(0,0))
        self.assertFalse(any(query.startswith('DELETE') for query,params in self.connection.queries))
    def test_exact_page_keeps_cursor_and_deletes_only_locked_evidence(self):
        row=item();self.header(ROW,limit=1);self.queue(Result(rows=(row,)),one(eligible=True),one(has_token=False,expires_at=None),one(value={'id':1}),one(at=AT),one(eligible=True),one(expired=True),count(1))
        value=self.run_page(ROW,limit=1);self.assertEqual((value['deleted'],value['complete'],value['next_cursor']),(1,False,row['cursor']))
        self.assertEqual(self.pool.borrowed,1);self.assertTrue(self.connection.queries[-1][0].startswith('DELETE FROM public.video_availability_events'))
    def test_review_advisory_conflict_does_not_lock_video_queue(self):
        self.header('review_evidence');self.queue(Result(rows=(item(row_data={'case_id':'00000000-0000-4000-8000-000000000001','video_id':VIDEO}),)),one(eligible=True),one(acquired=False),one(value=EXPIRED))
        value=self.run_page('review_evidence');self.assertEqual(value['deferred'],1)
        self.assertFalse(any('video_collection_state WHERE' in query for query,params in self.connection.queries))
    def test_nonfinite_deferred_clock_counts_invalid_without_native_datetime_decode(self):
        self.header(ROW);self.queue(Result(rows=(item(work_at='-infinity',expired=False,invalid_clock=True),)),one(eligible=True),one(has_token=True,expires_at=AT),one(active=True))
        value=self.run_page(ROW);self.assertEqual(value['invalid_clock_remaining'],1);self.assertIsNone(value['oldest_remaining_expired_source_at'])
    def test_native_failure_after_lock_aborts_all_work_without_retry(self):
        self.header(ROW);self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=False,expires_at=None),one(value={'id':1}),one(at=AT),one(eligible=True),one(expired=True),Result(execute_error=ServerFailure('23503')))
        with self.assertRaises(DatabaseServerError):self.run_page(ROW)
        self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
    def test_channel_snapshot_has_no_video_lease(self):
        self.header('channel_snapshots');self.queue(Result(rows=(item(video_id=None),)),Result(rows=(dict(ord=1,eligible=True),)),
            Result(rows=(dict(ord=1,value={'channel_id':'synthetic','recorded_at':EXPIRED}),)),one(at=AT),count(1))
        self.assertEqual(self.run_page('channel_snapshots')['deleted'],1)
        self.assertFalse(any('video_collection_state' in query for query,params in self.connection.queries))
    def test_lease_expired_after_lock_is_rechecked_with_new_native_clock(self):
        self.header(ROW);self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=True,expires_at=AT),one(active=False),one(value={'id':1}),one(at=AT),one(eligible=True),one(expired=True),count(1))
        self.assertEqual(self.run_page(ROW)['deleted'],1)
        queries=self.connection.queries
        lock_index=next(i for i,(query,params) in enumerate(queries)
                        if 'AS has_token' in query and 'FOR UPDATE NOWAIT' in query)
        self.assertNotIn('clock_timestamp()',queries[lock_index][0])
        self.assertIn('SELECT %s::timestamptz>clock_timestamp() AS active',queries[lock_index+1][0])
        self.assertEqual(queries[lock_index+1][1],(AT,))

    # Immutable evidence: the per-row protocol, one statement per step for the page.
    def snapshots(self,count_=3):
        return [item(row_data={'id':n,'video_id':VIDEO if n<3 else 'bbbbbbbbbbb','recorded_at':EXPIRED},
                     work_key=str(n).zfill(20),video_id=VIDEO if n<3 else 'bbbbbbbbbbb') for n in range(1,count_+1)]
    def proof(self,*flags):return Result(rows=tuple(dict(ord=n,eligible=flag) for n,flag in enumerate(flags,1)))
    def domain(self):return [query for query,params in self.connection.queries]

    def test_snapshot_page_deletes_in_one_statement_after_page_locks(self):
        rows=self.snapshots();self.header()
        self.queue(Result(rows=tuple(rows)),self.proof(True,True,True),
            Result(rows=(dict(video_id=VIDEO,present=True,has_token=False,expires_at=None),dict(video_id='bbbbbbbbbbb',present=False,has_token=None,expires_at=None))),
            Result(rows=tuple(dict(ord=n,value={'id':n}) for n in (1,2,3))),one(at=AT),count(3))
        value=self.run_page()
        self.assertEqual((value['scanned'],value['deleted'],value['deferred']),(3,3,0))
        statements=self.domain()
        self.assertEqual(len(statements),8)  # arguments, clock, page; then proof, queue locks, row locks, clock, delete
        self.assertEqual(sum(query.startswith('DELETE FROM public.video_snapshots') for query in statements),1)
        self.assertIn('FOR UPDATE SKIP LOCKED',statements[4]);self.assertIn('video_collection_state',statements[4])
        delete=self.connection.queries[-1]
        self.assertEqual(delete[1]['now'],AT);self.assertEqual([row['id'] for row in delete[1]['rows'].obj],[1,2,3])
        self.assertIn('t.recorded_at<%(cutoff)s::timestamptz',delete[0])  # rechecked on the locked rows

    def test_snapshot_of_a_live_lease_or_held_queue_row_is_deferred(self):
        for lease,statements,results in (('live',7,(Result(rows=(dict(video_id=VIDEO,present=True,has_token=True,expires_at=AT),)),Result(rows=(dict(video_id=VIDEO,active=True),)))),
                                         ('held',6,(Result(rows=(dict(video_id=VIDEO,present=True,has_token=None,expires_at=None),)),))):
            with self.subTest(lease=lease):
                self.setUp();rows=self.snapshots(2);self.header()
                self.queue(Result(rows=tuple(rows)),self.proof(True,True),*results,one(value=EXPIRED))
                value=self.run_page()
                self.assertEqual((value['deleted'],value['deferred'],value['oldest_remaining_expired_source_at']),(0,2,EXPIRED))
                self.assertEqual(len(self.domain()),statements)  # only the oldest deferred clock follows the queue locks

    def test_snapshot_lease_clock_is_sampled_after_the_queue_locks(self):
        rows=self.snapshots(1);self.header()
        self.queue(Result(rows=tuple(rows)),self.proof(True),Result(rows=(dict(video_id=VIDEO,present=True,has_token=True,expires_at=AT),)),
            Result(rows=(dict(video_id=VIDEO,active=False),)),Result(rows=(dict(ord=1,value={'id':1}),)),one(at=AT),count(1))
        self.assertEqual(self.run_page()['deleted'],1)
        statements=self.domain()
        lock=next(i for i,query in enumerate(statements) if 'video_collection_state' in query)
        self.assertNotIn('clock_timestamp()',statements[lock])
        self.assertIn('expires_at::timestamptz>clock_timestamp() AS active',statements[lock+1])
        self.assertEqual(self.connection.queries[lock+1][1]['leases'].obj,[dict(video_id=VIDEO,expires_at=AT)])

    def test_snapshot_already_locked_or_gone_is_deferred_and_others_deleted(self):
        rows=self.snapshots();self.header()
        self.queue(Result(rows=tuple(rows)),self.proof(True,False,True),
            Result(rows=(dict(video_id=VIDEO,present=True,has_token=False,expires_at=None),dict(video_id='bbbbbbbbbbb',present=True,has_token=False,expires_at=None))),
            Result(rows=(dict(ord=2,value={'id':3}),)),one(at=AT),count(1),one(value=EXPIRED))
        value=self.run_page()
        self.assertEqual((value['deleted'],value['deferred']),(1,1))  # row 2 failed the proof, row 1 was held
        self.assertEqual(value['priority_references'],[dict(target='video_snapshots',key=['1'])])
        self.assertEqual([row['id'] for row in self.connection.queries[-2][1]['rows'].obj],[3])

    def test_snapshot_noop_proof_precedes_any_lock(self):
        self.header();self.queue(Result(rows=tuple(self.snapshots(2))),self.proof(False,False))
        value=self.run_page();self.assertEqual((value['scanned'],value['deleted'],value['deferred']),(2,0,0))
        self.assertFalse(any('FOR UPDATE' in query for query in self.domain()))

    def test_snapshot_delete_failure_aborts_the_page(self):
        self.header();self.queue(Result(rows=tuple(self.snapshots(1))),self.proof(True),
            Result(rows=(dict(video_id=VIDEO,present=False,has_token=None,expires_at=None),)),
            Result(rows=(dict(ord=1,value={'id':1}),)),one(at=AT),Result(execute_error=ServerFailure('23503')))
        with self.assertRaises(DatabaseServerError):self.run_page()
        self.assertEqual(self.connection.commits,0)

    def test_incident_noop_proof_uses_one_shared_clock(self):
        self.header('availability_incidents');self.queue(Result(rows=(item(),)),one(eligible=False))
        self.run_page('availability_incidents')
        query=self.connection.queries[-1][0]
        self.assertEqual(query.count('clock_timestamp()'),1)
        self.assertIn('clock AS MATERIALIZED',query)
        self.assertGreater(query.count('clock.at'),1)

    def test_fixed_projection_orders_native_clock_not_text_alias(self):
        for name,spec in _TARGETS.items():
            with self.subTest(target=name):
                query=_page(spec)
                self.assertIn('ORDER BY candidates.work_at,candidates.work_key',query)
                self.assertIn('LIMIT %(limit)s::integer',query)
                self.assertEqual('UNION ALL' in query,spec.immutable)
        with self.assertRaises(TypeError):_TARGETS['untrusted']='bad'

if __name__=='__main__':unittest.main()
