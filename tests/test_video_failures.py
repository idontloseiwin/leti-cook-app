"""Prepared queue failure policy/atomic protocol, no network or live DB."""
from collections import deque
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from collection_policy import failure_policy
from db.videos import video_failures as queries
from db.base.postgres import DatabaseServerError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

NOW=datetime(2026,10,5,12,tzinfo=timezone.utc)
TOKEN=UUID('c0000000-0000-4000-8000-000000000071')
CHANNEL='UC0000000000000000000001'

def payload(**change):
    row=dict(video_id='aaaaaaaaaaa',video_type='long',lease_token=str(TOKEN),
             error_type='fetch_failed',retry_after_seconds=None)
    row.update(change);return row

def parsed(**change):
    row=dict(payload(),ordinal=1,lease_token=TOKEN,trimmed_id='aaaaaaaaaaa',trimmed_error='fetch_failed')
    row.update(change);return row

def locked(**change):
    row=dict(video_id='aaaaaaaaaaa',video_type='long',channel_id=CHANNEL,lease_token=TOKEN,
             lease_channel_epoch=42,consecutive_failures=4,transaction_clock=NOW,ordinal=1)
    row.update(change);return row

class Database:
    def __init__(self,*rows):self.rows=deque(rows);self.calls=[];self.events=[];self.operations=[]
    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation)
        try:yield self
        except BaseException:self.events.append('rollback');raise
        else:self.events.append('commit')
    @contextmanager
    def borrow(self):
        self.events.append('borrow')
        yield self
    def answer(self,mode,sql,params,maximum=None):
        self.calls.append((mode,sql,params,maximum))
        result=self.rows.popleft()
        if isinstance(result,BaseException):raise result
        return result
    def fetch_one(self,sql,params=()):return self.answer('one',sql,params,1)
    def fetch_all(self,sql,params=(),*,max_rows):return self.answer('all',sql,params,max_rows)
    def execute(self,sql,params=()):return self.answer('write',sql,params)

class PolicyTests(unittest.TestCase):
    def test_normal_backoff_and_manual_retry_boundaries(self):
        for failures,expected in ((0,30),(1,60),(11,61440),(100,61440)):
            actual=failure_policy(now=NOW,consecutive_failures=failures,error_type=' failed ',retry_after_seconds=None)
            self.assertEqual(actual.retry_at,NOW+timedelta(seconds=expected))
            self.assertEqual(actual.consecutive_failures,failures+1)
            self.assertEqual(actual.error_type,'failed')
        self.assertEqual(failure_policy(now=NOW,consecutive_failures=0,error_type='error',retry_after_seconds=86400).retry_at,NOW+timedelta(days=1))
    def test_stats_miss_schedules_general_without_growing_failure_backoff(self):
        value = failure_policy(now=NOW, consecutive_failures=12,
            error_type='youtube_stats_counters_unavailable', retry_after_seconds=30)
        self.assertEqual(value.retry_at, NOW + timedelta(seconds=30))
        self.assertEqual(value.consecutive_failures, 12)

    def test_typed_floor_clamp_and_default(self):
        for retry,failures,seconds in ((None,0,3600),(None,4,57600),(None,100,57600),(-1,4,3600),(90000,0,86400)):
            with self.subTest(retry=retry,failures=failures):
                value=failure_policy(now=NOW,consecutive_failures=failures,error_type=' ',retry_after_seconds=retry,typed=True)
                self.assertEqual(value.retry_at,NOW+timedelta(seconds=seconds))
                self.assertEqual(value.error_type,'collection_error')
    def test_quota_reset_does_not_spend_attempt_and_typed_ignores_manual_retry(self):
        for typed in (False,True):
            value=failure_policy(now=NOW,consecutive_failures=5,error_type=' youtube_quota_exhausted ',retry_after_seconds=30,typed=typed)
            self.assertEqual(value.consecutive_failures,5)
            self.assertEqual(value.retry_at,datetime(2026,10,6,7,tzinfo=timezone.utc))
        requested=failure_policy(now=NOW,consecutive_failures=0,error_type='youtube_quota_exhausted',retry_after_seconds=86400)
        self.assertEqual(requested.retry_at,NOW+timedelta(days=1))
        alias=failure_policy(now=NOW,consecutive_failures=0,error_type='youtube_daily_quota_exceeded_error',retry_after_seconds=None)
        self.assertEqual(alias.consecutive_failures,1)
    def test_native_ascii_space_trim_and_120_char_limit(self):
        value=failure_policy(now=NOW,consecutive_failures=0,error_type='\t'+('x'*130),retry_after_seconds=None)
        self.assertEqual(value.error_type,'\t'+'x'*119)

class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fence_patch=patch('db.channel.channel_control.assert_channel_scopes',create=True)
        self.fence=self.fence_patch.start();self.addCleanup(self.fence_patch.stop)
    def test_common_atomic_release_and_final_epoch_fence(self):
        db=Database([parsed()],[locked()],1)
        self.assertEqual(queries.fail_video_collection_batch(db,[payload()]),1)
        self.assertEqual(db.events,['commit'])
        self.assertEqual(db.operations[0].total_timeout_ms,8000)
        update=db.calls[-1][2][0].obj[0]
        self.assertEqual(update['failures'],5)
        self.assertEqual(update['retry_at'],(NOW+timedelta(seconds=480)).isoformat())
        self.fence.assert_called_once_with(db,[{'channel_id':CHANNEL,'control_epoch':42}])
        self.assertNotIn('stats_due_at=',db.calls[-1][1])
    def test_stats_handoff_changes_due_and_releases_lease_in_same_write(self):
        db=Database([parsed(error_type='youtube_stats_counters_unavailable')],
                    [locked(consecutive_failures=12)],1)
        self.assertEqual(queries.fail_video_collection_batch(db,
            [payload(error_type='youtube_stats_counters_unavailable',retry_after_seconds=30)]),1)
        update=db.calls[-1][2][0].obj[0]
        self.assertTrue(update['check_availability'])
        self.assertEqual(update['retry_at'],(NOW+timedelta(seconds=30)).isoformat())
        self.assertEqual(update['failures'],12)
        self.assertIn('metadata_due_at=CASE WHEN input.check_availability THEN now()',db.calls[-1][1])
        self.assertIn('lease_token=NULL',db.calls[-1][1])
        self.assertEqual(db.events,['commit'])
        self.fence.assert_called_once()

    def test_typed_native_retry_cast_happens_after_row_locks(self):
        db=Database({'video_type':'short'},[locked(video_type='short')],[parsed(video_type='short',error_type=None,retry_after_seconds=None)],1)
        self.assertEqual(queries.fail_typed_video_collection_claims(db,'short',[payload(video_type='short')]),1)
        self.assertIn('FOR UPDATE OF state',db.calls[1][1])
        self.assertIn('retry_after_seconds integer',db.calls[2][1])
        self.assertIn('stats_due_at=NULL,metadata_due_at=NULL',db.calls[-1][1])
        self.assertEqual(db.calls[-1][2][0].obj[0]['error_type'],'collection_error')
    def test_long_native_record_count_precedes_row_limit(self):
        failure=DatabaseServerError('database_operation_failed',code='22P02')
        db=Database(failure)
        with self.assertRaises(DatabaseServerError) as caught:queries.fail_video_collection_claims(db,[payload()]*1001)
        self.assertIs(caught.exception,failure)
        self.assertEqual(len(db.calls),1)
        self.assertIn('count(*)',db.calls[0][1])
    def test_common_duplicate_invalid_before_queue_lock(self):
        db=Database([parsed(),parsed(ordinal=2)])
        with self.assertRaisesRegex(DatabaseServerError,'common_video_collection_fail_row_invalid'):
            queries.fail_video_collection_batch(db,[payload(),payload()])
        self.assertEqual(len(db.calls),1)
        self.assertEqual(db.events,['rollback'])
    def test_partial_owned_write_rolls_back_before_final_fence(self):
        db=Database([parsed(),parsed(ordinal=2,video_id='bbbbbbbbbbb',trimmed_id='bbbbbbbbbbb')],[locked()],1)
        with self.assertRaisesRegex(DatabaseServerError,'common_video_collection_fail_stale_ownership'):
            queries.fail_video_collection_batch(db,[payload(),payload(video_id='bbbbbbbbbbb')])
        self.assertEqual(db.events,['rollback'])
        self.fence.assert_not_called()
    def test_scope_loss_rolls_back_full_release(self):
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        db=Database([parsed()],[locked()],1)
        with self.assertRaises(DatabaseServerError):queries.fail_video_collection_batch(db,[payload()])
        self.assertEqual(db.events,['rollback'])
    def test_empty_typed_null_type_preserves_sql_null_not_in_semantics(self):
        db=Database({'video_type':None})
        self.assertEqual(queries.fail_typed_video_collection_claims(db,None,[]),0)
        self.assertEqual(len(db.calls),1)
        self.fence.assert_not_called()
    def test_legacy_long_writer_keeps_exact_token_across_video_types(self):
        for kind in ('short','stream'):
            db=Database({'count':1},[parsed(video_type=kind)],[locked(video_type=kind)],1)
            self.assertEqual(queries.fail_video_collection_claims(db,[payload(video_type=kind,retry_after_seconds=30)]),1)
            self.assertEqual(db.calls[2][2]['mode'],'long')
            self.assertNotIn('stats_due_at=NULL',db.calls[-1][1])

    def test_native_integer_overflow_in_write_rolls_back_before_fence(self):
        failure=DatabaseServerError('database_operation_failed',code='22003')
        db=Database([parsed()],[locked(consecutive_failures=2147483647)],failure)
        with self.assertRaises(DatabaseServerError) as caught:
            queries.fail_video_collection_batch(db,[payload()])
        self.assertIs(caught.exception,failure)
        self.assertEqual(db.calls[-1][2][0].obj[0]['failures'],2147483648)
        self.assertEqual(db.events,['rollback']);self.fence.assert_not_called()

    def test_queries_use_bound_inputs_and_driver_grammar(self):
        for query,read,params in ((queries._PARSE,True,([],)),(queries._LOCK,True,{'rows':[],'mode':'common','video_type':None}),
                                  (queries._UPDATE,False,([],)),(queries._UPDATE_COMMON,False,([],)),(queries._UPDATE_TYPED,False,([],))):
            _reviewed_statement(query,read=read,max_rows=1000 if read else 0)
            PostgresQuery(Transformer()).convert(query,params)
            self.assertNotIn('public.fail_',query)
        self.assertNotIn('lease_expires_at',queries._LOCK)

if __name__=='__main__':unittest.main()
