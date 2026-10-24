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
        self.header();self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=True,expires_at=AT),one(active=True),one(value=EXPIRED))
        value=self.run_page()
        self.assertEqual((value['deferred'],value['oldest_remaining_expired_source_at']),(1,EXPIRED))
        self.assertFalse(any(query.startswith('DELETE') for query,params in self.connection.queries))
    def test_busy_queue_rolls_back_only_candidate_and_reports_lag(self):
        self.header();self.queue(Result(rows=(item(),)),one(eligible=True),Result(execute_error=ServerFailure('55P03')),one(value=EXPIRED))
        value=self.run_page();self.assertEqual(value['deferred'],1);self.assertEqual(self.connection.commits,1)
    def test_missing_or_locked_evidence_is_deferred_after_queue_lock(self):
        self.header();self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=False,expires_at=None),Result(rows=()),one(value=EXPIRED))
        value=self.run_page();self.assertEqual(value['deferred'],1)
        statements=[query for query,params in self.connection.queries]
        self.assertLess(next(i for i,q in enumerate(statements) if 'video_collection_state' in q),next(i for i,q in enumerate(statements) if 'SKIP LOCKED' in q))
    def test_concurrent_refresh_wins_after_locked_recheck(self):
        self.header();self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=False,expires_at=None),one(value={'id':1}),one(at=AT),one(eligible=False))
        value=self.run_page();self.assertEqual((value['deleted'],value['deferred']),(0,0))
        self.assertFalse(any(query.startswith('DELETE') for query,params in self.connection.queries))
    def test_exact_page_keeps_cursor_and_deletes_only_locked_evidence(self):
        row=item();self.header(limit=1);self.queue(Result(rows=(row,)),one(eligible=True),one(has_token=False,expires_at=None),one(value={'id':1}),one(at=AT),one(eligible=True),count(1))
        value=self.run_page(limit=1);self.assertEqual((value['deleted'],value['complete'],value['next_cursor']),(1,False,row['cursor']))
        self.assertEqual(self.pool.borrowed,1);self.assertTrue(self.connection.queries[-1][0].startswith('DELETE FROM public.video_snapshots'))
    def test_review_advisory_conflict_does_not_lock_video_queue(self):
        self.header('review_evidence');self.queue(Result(rows=(item(row_data={'case_id':'00000000-0000-4000-8000-000000000001','video_id':VIDEO}),)),one(eligible=True),one(acquired=False),one(value=EXPIRED))
        value=self.run_page('review_evidence');self.assertEqual(value['deferred'],1)
        self.assertFalse(any('video_collection_state WHERE' in query for query,params in self.connection.queries))
    def test_nonfinite_deferred_clock_counts_invalid_without_native_datetime_decode(self):
        self.header();self.queue(Result(rows=(item(work_at='-infinity',expired=False,invalid_clock=True),)),one(eligible=True),one(has_token=True,expires_at=AT),one(active=True))
        value=self.run_page();self.assertEqual(value['invalid_clock_remaining'],1);self.assertIsNone(value['oldest_remaining_expired_source_at'])
    def test_native_failure_after_lock_aborts_all_work_without_retry(self):
        self.header();self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=False,expires_at=None),one(value={'id':1}),one(at=AT),one(eligible=True),Result(execute_error=ServerFailure('23503')))
        with self.assertRaises(DatabaseServerError):self.run_page()
        self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
    def test_channel_snapshot_has_no_video_lease(self):
        self.header('channel_snapshots');self.queue(Result(rows=(item(video_id=None),)),one(eligible=True),one(value={'channel_id':'synthetic','recorded_at':EXPIRED}),one(at=AT),one(eligible=True),count(1))
        self.assertEqual(self.run_page('channel_snapshots')['deleted'],1)
        self.assertFalse(any('video_collection_state WHERE' in query for query,params in self.connection.queries))
    def test_lease_expired_after_lock_is_rechecked_with_new_native_clock(self):
        self.header();self.queue(Result(rows=(item(),)),one(eligible=True),one(has_token=True,expires_at=AT),one(active=False),one(value={'id':1}),one(at=AT),one(eligible=True),count(1))
        self.assertEqual(self.run_page()['deleted'],1)
        queries=self.connection.queries
        lock_index=next(i for i,(query,params) in enumerate(queries)
                        if 'AS has_token' in query and 'FOR UPDATE NOWAIT' in query)
        self.assertNotIn('clock_timestamp()',queries[lock_index][0])
        self.assertIn('SELECT %s::timestamptz>clock_timestamp() AS active',queries[lock_index+1][0])
        self.assertEqual(queries[lock_index+1][1],(AT,))

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
