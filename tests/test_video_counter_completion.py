"""Offline atomic counter completion using the real finite transaction driver."""
from contextlib import contextmanager
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch
import psycopg
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_counter_completion as helper,video_source_invalidation as invalidation
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_collection_policy import rows as cadence_rows,NOW

VIDEO='vc000000001';CHANNEL='UC0000000000000000000001'
TOKEN='cc000000-0000-4000-8000-000000000001'
ATTEMPT='cc000000-0000-4000-8000-000000000002'
def one(**values):return Result((values,))
def payload(video=VIDEO,**values):
    return dict(dict(video_id=video,lease_token=TOKEN,collection_attempt_id=ATTEMPT,
        views=9007199254740993,like_count=None,comment_count=0,recorded_at=NOW.isoformat()),**values)
def source(video=VIDEO,**values):
    return dict(dict(video_id=video,lease_token=TOKEN,recorded_at=NOW.isoformat(),metadata_due_at=None,
        video_type='long',policy_version=1,
        availability_checked_at='10000-01-01T00:00:00+00:00',availability_status='public',published_invalid=False,
        age_seconds=86400,stats_now=False,as_of_valid=True,availability_now=True),**values)

class CounterCompletionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*values):self.connection.results.extend(values)
    def setup_success(self,count=1,*,pending_count=None,source_changes=None,cadence=None,guard=None,invalidation_required=False):
        pending_count=count if pending_count is None else pending_count
        entries=[payload(f'vc{i:09d}') for i in range(count)]
        pending=entries[:pending_count]
        sources=[source(row['video_id'],**(source_changes or {})) for row in pending]
        scopes=[dict(channel_id=CHANNEL,control_epoch=7)] if pending else []
        self.queue(one(count=count),one(valid=True),Result(tuple(dict(video_id=r['video_id']) for r in entries)),
            one(valid=True),one(stale=False),one(rows=pending),one(count=pending_count,scopes=scopes))
        if pending:self.queue(one(count=pending_count,snapshot_text=json.dumps([dict(video_id=r['video_id'],source_existed=True,checked_at=None,metadata_checked_at=None) for r in pending])))
        self.queue(Result(has_columns=False,rowcount=0 if guard=='source' else pending_count))
        if guard=='source':return entries
        if pending:
            self.queue(one(present=pending_count,required=invalidation_required))
            if invalidation_required:self.queue(Result(has_columns=False,rowcount=1))
        self.queue(Result(has_columns=False,rowcount=0 if guard=='snapshot' else pending_count))
        if guard=='snapshot':return entries
        self.queue(one(sources=sources,cadence=cadence_rows() if cadence is None else cadence))
        self.queue(Result(has_columns=False,rowcount=0 if guard=='queue' else pending_count))
        if guard=='queue':return entries
        self.queue(one(valid=True))
        if pending:self.queue(one(channel_id=CHANNEL,control_epoch=7,is_hidden=False,owner_permission='accepted'))
        return entries
    def run_helper(self,entries):return helper.complete_video_counter_collection(self.database,entries)
    def test_atomic_source_receipt_and_queue_updates_then_final_fence(self):
        entries=self.setup_success();self.assertEqual(self.run_helper(entries),1)
        statements=[sql for sql,_ in self.connection.queries]
        self.assertEqual(len(statements),15)
        self.assertIn(helper._LOCK,statements[2]);self.assertIn(helper._CLOCKS,statements[3])
        self.assertEqual(statements[7:11],[f'SELECT * FROM ({invalidation._CAPTURE}) AS lic_bounded_result LIMIT 2',helper._UPDATE_SOURCE,f'SELECT * FROM ({invalidation._FACTS}) AS lic_bounded_result LIMIT 2',helper._INSERT_SNAPSHOTS])
        self.assertEqual(self.connection.queries[12][1][1],2419200)
        self.assertEqual(statements[12],helper._COMPLETE);self.assertIn(helper._CLOCKS,statements[13]);self.assertIn('FOR SHARE',statements[-1])
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(1,0,1))
        self.assertEqual(self.connection.queries[8][1][0].obj[0]['views'],9007199254740993)
        self.assertIsNone(self.connection.queries[8][1][0].obj[0]['like_count'])
    def test_required_invalidation_precedes_snapshot_and_retains_single_commit(self):
        entries=self.setup_success(invalidation_required=True);self.assertEqual(self.run_helper(entries),1)
        statements=[sql for sql,_ in self.connection.queries]
        self.assertEqual(statements[7:12],[f'SELECT * FROM ({invalidation._CAPTURE}) AS lic_bounded_result LIMIT 2',helper._UPDATE_SOURCE,f'SELECT * FROM ({invalidation._FACTS}) AS lic_bounded_result LIMIT 2',invalidation._INVALIDATE,helper._INSERT_SNAPSHOTS])
        self.assertEqual(statements.count(invalidation._INVALIDATE),1)
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(1,0,1))
    def test_one_and_thousand_rows_have_exactly_the_same_query_count(self):
        for count in (1,1000):
            start=len(self.connection.queries);entries=self.setup_success(count)
            self.assertEqual(self.run_helper(entries),count)
            self.assertEqual(len(self.connection.queries)-start,15)
            self.assertEqual(len(self.connection.queries[-3][1][0].obj),count)
        self.assertEqual(self.connection.fetch_sizes.count(1001),1)
    def test_empty_returns_after_native_cast_count_and_nonarrays_do_not_dispatch(self):
        self.queue(one(count=0));self.assertEqual(self.run_helper([]),0)
        for value in (None,{},'private'):
            with self.assertRaises(DatabaseServerError) as caught:self.run_helper(value)
            self.assertEqual(caught.exception.code,'P0001')
        self.assertEqual(len(self.connection.queries),1)
    def test_oversized_array_count_is_native_and_fails_without_transfer_or_writes(self):
        self.queue(one(count=1001))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload()]*1001)
        self.assertEqual(len(self.connection.queries),1)
        self.assertIn('jsonb_to_recordset',helper._COUNT);self.assertIn('views bigint',helper._COUNT)
        self.assertNotIn('LIMIT',helper._COUNT)
    def test_native_cast_error_precedes_row_cap_and_domain_validation(self):
        self.queue(Result(execute_error=ServerFailure('22P02','private invalid UUID')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()]*1001)
        self.assertEqual((caught.exception.code,caught.exception.message),('22P02','database_operation_failed'))
        self.assertEqual(len(self.connection.queries),1);self.assertEqual(self.connection.commits,0)
    def test_invalid_duplicate_input_precedes_queue_lock(self):
        self.queue(one(count=2),one(valid=False))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload(),payload()])
        self.assertEqual(len(self.connection.queries),2)
        self.assertIn('GROUP BY video_id HAVING count(*)>1',helper._VALID)
        self.assertNotIn('GROUP BY collection_attempt_id',helper._VALID)
    def test_waiting_queue_lock_precedes_hot_clock_check(self):
        self.queue(one(count=1),one(valid=True),Result(execute_error=ServerFailure('55P03')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload(recorded_at='infinity')])
        self.assertEqual(caught.exception.code,'55P03');self.assertEqual(len(self.connection.queries),3)
        self.assertNotIn('SKIP LOCKED',helper._LOCK)
    def test_native_hot_clock_failure_precedes_stale_receipt_check(self):
        self.queue(one(count=1),one(valid=True),one(video_id=VIDEO),one(valid=False))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()])
        self.assertEqual((caught.exception.code,caught.exception.message),('22023','hot_source_clock_out_of_window'))
        self.assertEqual(len(self.connection.queries),4)
        self.assertIn('clock_timestamp()',helper._CLOCKS);self.assertIn('bool_and',helper._CLOCKS)
    def test_receipt_replay_has_no_owned_writes_or_scope_lock(self):
        entries=self.setup_success(2,pending_count=0,cadence=[])
        self.assertEqual(self.run_helper(entries),2)
        self.assertEqual(len(self.connection.queries),12)
        self.assertEqual(self.connection.queries[7][1][0].obj,[])
        self.assertEqual(self.connection.queries[10][1][0].obj,[])
        self.assertIn('state.video_id IS NULL OR',helper._STALE)
        self.assertIn('snapshot.collection_attempt_id=input.collection_attempt_id',helper._STALE)
    def test_stale_failure_precedes_pending_selection_and_mutations(self):
        self.queue(one(count=1),one(valid=True),one(video_id=VIDEO),one(valid=True),one(stale=True))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload()])
        self.assertEqual(len(self.connection.queries),5)
    def test_each_count_guard_rolls_back_without_later_work(self):
        for guard,wanted in (('source',9),('snapshot',11),('queue',13)):
            with self.subTest(guard=guard):
                start=len(self.connection.queries);entries=self.setup_success(guard=guard)
                with self.assertRaises(DatabaseServerError):self.run_helper(entries)
                self.assertEqual(len(self.connection.queries)-start,wanted)
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,3))
    def test_final_hot_clock_error_rolls_back_all_three_mutations_before_scope(self):
        entries=self.setup_success();self.connection.results.pop();self.connection.results[-1]=one(valid=False)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(entries)
        self.assertEqual(caught.exception.code,'22023');self.assertEqual(len(self.connection.queries),14)
        self.assertEqual(self.connection.commits,0)
    def test_final_scope_failure_rolls_back_source_snapshot_and_settlement(self):
        entries=self.setup_success();self.connection.results[-1]=one(channel_id=CHANNEL,control_epoch=8,is_hidden=False,owner_permission='accepted')
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(entries)
        self.assertEqual(caught.exception.message,'channel_collection_scope_lost');self.assertEqual(self.connection.commits,0)
    def test_cadence_and_sources_share_snapshot_native_age_and_timestamp_arithmetic(self):
        cadence=cadence_rows();cadence[0]['cadence_seconds']=43
        entries=self.setup_success(source_changes={'age_seconds':0},cadence=cadence);self.run_helper(entries)
        schedule=self.connection.queries[12][1][0].obj[0]
        self.assertEqual(schedule['stats_seconds'],43)
        self.assertEqual(schedule['availability_checked_at'],'10000-01-01T00:00:00+00:00')
        self.assertIn('cadence AS MATERIALIZED',helper._POLICY_INPUT)
        self.assertIn('extract(epoch FROM (recorded_at-published_at))::bigint',helper._POLICY_INPUT)
        self.assertIn('make_interval(secs=>input.stats_seconds::double precision)',helper._COMPLETE)
        self.assertIn('make_interval(secs=>%s::integer)',helper._COMPLETE)
    def test_empty_cadence_keeps_null_stats_and_native_null_ignoring_least(self):
        entries=self.setup_success(cadence=[]);self.run_helper(entries)
        schedule=self.connection.queries[12][1][0].obj[0]
        self.assertIsNone(schedule['stats_seconds']);self.assertEqual(schedule['policy_version'],1)
        self.assertIn('least(schedule.stats_due_at,schedule.metadata_due_at,state.availability_resume_observed_at)',helper._COMPLETE)
    def test_each_video_type_uses_its_counter_band_without_renewing_metadata(self):
        from dataclasses import replace
        snapshot=CollectionSchedules().cadence_snapshot()
        rules=tuple(replace(rule,cadence_seconds={'long':41,'short':43,'stream':47}[rule.video_type])
                    if rule.collection_kind=='counters' and rule.min_age_seconds==0
                    and rule.availability_class=='public' else rule for rule in snapshot.rules)
        for kind,wanted in (('long',41),('short',43),('stream',47)):
            with self.subTest(kind=kind):
                entries=self.setup_success(source_changes={'video_type':kind,'age_seconds':0,
                    'metadata_due_at':'2026-10-06T12:00:00+00:00'},
                    cadence=cadence_rows(replace(snapshot,rules=rules)))
                self.assertEqual(self.run_helper(entries),1)
                schedule=self.connection.queries[-3][1][0].obj[0]
                self.assertEqual(schedule['stats_seconds'],wanted)
                self.assertEqual(schedule['metadata_due_at'],'2026-10-06T12:00:00+00:00')
                self.assertEqual(schedule['video_type'],kind)
                self.assertIsNone(schedule['paused_reason'])
        self.assertNotIn('metadata_source_checked_at=',helper._COMPLETE)
        self.assertNotIn('typed_collection_checked_at=',helper._COMPLETE)
    def test_stats_completion_has_typed_identity_and_finished_stream_fences(self):
        self.assertIn('candidate.channel_id=state.channel_id',helper._STALE)
        self.assertIn('candidate.video_type=state.video_type',helper._STALE)
        self.assertIn('video.stream_actual_end_at IS NULL',helper._STALE)
        self.assertIn('video.stream_actual_end_at>input.recorded_at',helper._STALE)
        self.assertIn('state.video_type IS NULL',helper._STALE)
    def test_unclassified_counter_source_is_rejected_instead_of_using_long_policy(self):
        entries=self.setup_success(source_changes={'video_type':None})
        with self.assertRaises(DatabaseServerError):self.run_helper(entries)
        self.assertEqual(self.connection.commits,0)
    def test_unsupported_availability_pauses_and_has_no_policy_match(self):
        entries=self.setup_success(source_changes={'availability_status':'private'});self.run_helper(entries)
        schedule=self.connection.queries[12][1][0].obj[0]
        self.assertEqual(schedule['paused_reason'],'availability_unsupported');self.assertFalse(schedule['availability_supported'])
    def test_policy_native_error_rolls_back_earlier_source_and_snapshot(self):
        entries=self.setup_success();self.connection.results[11]=Result(execute_error=ServerFailure('22003'))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(entries)
        self.assertEqual(caught.exception.code,'22003');self.assertEqual(self.connection.commits,0)
    def test_caught_borrowed_domain_rejection_poison_prevents_commit(self):
        with self.assertRaises(DatabaseError) as caught:
            with self.database.transaction(Operation('counter_completion_test')) as tx:
                with self.assertRaises(DatabaseServerError):helper.complete_video_counter_collection(None,None,transaction=tx)
        self.assertEqual(caught.exception.message,'database_transaction_aborted');self.assertEqual(self.connection.commits,0)
    def test_unknown_commit_is_never_retried(self):
        entries=self.setup_success();self.connection.commit_error=psycopg.OperationalError('private details')
        with self.assertRaises(DatabaseUnavailable) as caught:self.run_helper(entries)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
        self.assertEqual(len(self.connection.queries),15)
    def test_prepared_helper_has_no_business_rpc_or_provider_calls(self):
        import inspect
        code=inspect.getsource(helper)
        self.assertNotIn('.call(',code);self.assertNotIn('requests.',code)
        self.assertNotIn('public.video_collection_policy_value(',code)
        self.assertNotIn('public.assert_hot_source_clock(',code)

if __name__=='__main__':unittest.main()
