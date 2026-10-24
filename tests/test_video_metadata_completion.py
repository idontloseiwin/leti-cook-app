"""Offline Long metadata completion order, profile semantics and batch ownership."""
from pathlib import Path
import json
import sys
import unittest
import psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_metadata_completion as helper,video_source_invalidation as invalidation
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_collection_policy import rows as cadence_rows,NOW
from test_video_counter_completion import CHANNEL,TOKEN,ATTEMPT,one


def payload(video='vm000000001',**values):
    return dict(dict(video_id=video,channel_id=CHANNEL,lease_token=TOKEN,collection_attempt_id=ATTEMPT,
        title='',views=9007199254740993,like_count=None,comment_count=0,recorded_at=NOW.isoformat(),
        published_at='10000-01-01T00:00:00+00:00',youtube_topic_categories=[],tags=None,
        availability_status='public',profile_collected=True),**values)
def source(video='vm000000001',**values):
    return dict(dict(video_id=video,channel_id=CHANNEL,lease_token=TOKEN,recorded_at=NOW.isoformat(),
        metadata_observed_at=NOW.isoformat(),metadata_age_seconds=values.get("age_seconds",0),
        profile_collected=True,availability_checked_at=NOW.isoformat(),availability_status='public',
        published_invalid=False,age_seconds=0,stats_now=False,metadata_now=False,
        as_of_valid=True,availability_now=False),**values)

class MetadataCompletionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*values):self.connection.results.extend(values)
    def setup_success(self,count=1,*,owned_count=None,guard=None,changes=None,cadence=None,invalidation_required=False):
        owned_count=count if owned_count is None else owned_count
        entries=[payload(f'vm{i:09d}') for i in range(count)];pending=entries[:owned_count]
        self.queue(one(valid=True),one(count=count),one(valid=True),one(valid=True),
            Result(tuple(dict(video_id=r['video_id']) for r in pending)),one(valid=True),one(valid=True),one(stale=False),
            one(count=owned_count,rows=pending,scopes=[dict(channel_id=CHANNEL,control_epoch=7)] if pending else []))
        if not owned_count:self.queue(*(one(valid=True) for _ in range(4)));return entries
        self.queue(one(count=owned_count,snapshot_text=json.dumps([dict(video_id=r['video_id'],source_existed=True,checked_at=None,metadata_checked_at=None) for r in pending])))
        self.queue(Result(has_columns=False,rowcount=0 if guard=='source' else owned_count))
        if guard=='source':return entries
        self.queue(one(present=owned_count,required=invalidation_required))
        if invalidation_required:self.queue(Result(has_columns=False,rowcount=1))
        self.queue(Result(has_columns=False,rowcount=0 if guard=='snapshot' else owned_count))
        if guard=='snapshot':return entries
        self.queue(one(sources=[source(r['video_id'],**(changes or {})) for r in pending],cadence=cadence_rows() if cadence is None else cadence))
        self.queue(Result(has_columns=False,rowcount=0 if guard=='queue' else owned_count))
        if guard=='queue':return entries
        self.queue(one(valid=True),one(valid=True),one(channel_id=CHANNEL,control_epoch=7,is_hidden=False,owner_permission='accepted'))
        return entries
    def run_helper(self,entries):return helper.complete_video_metadata_collection(self.database,entries)
    def test_atomic_source_upsert_snapshot_queue_and_final_fence(self):
        entries=self.setup_success();self.assertEqual(self.run_helper(entries),1)
        sql=[q for q,_ in self.connection.queries]
        self.assertEqual(len(sql),18);self.assertIn(helper._LOCK,sql[4])
        self.assertEqual(sql[9:13],[f'SELECT * FROM ({invalidation._CAPTURE}) AS lic_bounded_result LIMIT 2',helper._UPSERT,f'SELECT * FROM ({invalidation._FACTS}) AS lic_bounded_result LIMIT 2',helper._SNAPSHOTS]);self.assertEqual(sql[14],helper._COMPLETE)
        self.assertEqual(self.connection.queries[14][1][1],2419200)
        self.assertIn(helper._AVAILABILITY_CLOCKS,sql[16]);self.assertIn('FOR SHARE',sql[-1])
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(1,0,1))
    def test_required_invalidation_precedes_snapshot_and_retains_single_commit(self):
        entries=self.setup_success(invalidation_required=True);self.assertEqual(self.run_helper(entries),1)
        statements=[sql for sql,_ in self.connection.queries]
        self.assertEqual(statements[9:14],[f'SELECT * FROM ({invalidation._CAPTURE}) AS lic_bounded_result LIMIT 2',helper._UPSERT,f'SELECT * FROM ({invalidation._FACTS}) AS lic_bounded_result LIMIT 2',invalidation._INVALIDATE,helper._SNAPSHOTS])
        self.assertEqual(statements.count(invalidation._INVALIDATE),1)
        self.assertEqual((self.connection.commits,self.connection.rollbacks,self.pool.borrowed),(1,0,1))
    def test_one_and_thousand_rows_use_eighteen_queries_and_one_cadence_snapshot(self):
        for count in (1,1000):
            start=len(self.connection.queries);entries=self.setup_success(count)
            self.assertEqual(self.run_helper(entries),count)
            sql=[q for q,_ in self.connection.queries[start:]]
            self.assertEqual(len(sql),18);self.assertEqual(sum(helper._POLICY_INPUT in q for q in sql),1)
            self.assertEqual(len(self.connection.queries[-4][1][0].obj),count)
    def test_optional_profile_array_shape_precedes_native_casts_and_count(self):
        self.queue(one(valid=False))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload(lease_token='bad',youtube_topic_categories=None)]*1001)
        self.assertEqual(caught.exception.code,'P0001');self.assertEqual(len(self.connection.queries),1)
        self.assertIn("raw.value ? 'youtube_topic_categories'",helper._PROFILE_ARRAY)
    def test_empty_runs_shape_then_native_count_and_nonarrays_do_not_dispatch(self):
        self.queue(one(valid=True),one(count=0));self.assertEqual(self.run_helper([]),0)
        for value in (None,{},'private'):
            with self.assertRaises(DatabaseServerError):self.run_helper(value)
        self.assertEqual(len(self.connection.queries),2)
    def test_native_record_casts_precede_cap_without_transfer_of_oversized_records(self):
        self.queue(one(valid=True),Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()]*1001)
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),2)
        self.assertIn('caption_available boolean',helper._COUNT);self.assertNotIn('LIMIT',helper._COUNT)
    def test_over_cap_and_invalid_base_fields_precede_profile_semantics(self):
        self.queue(one(valid=True),one(count=1001))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload()]*1001)
        self.queue(one(valid=True),one(count=1),one(valid=False))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload(views=-1,youtube_topic_categories=['bad'])])
        self.assertEqual(len(self.connection.queries),5)
    def test_profile_validation_is_native_and_precedes_queue_lock(self):
        self.queue(one(valid=True),one(count=1),one(valid=True),one(valid=False))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload(youtube_topic_categories=['bad'])])
        self.assertEqual(len(self.connection.queries),4)
        self.assertIn('char_length(default_audio_language)>64',helper._PROFILE_VALID)
        self.assertIn('cardinality(youtube_topic_categories)>50',helper._PROFILE_VALID)
        self.assertIn('[^?#[:space:]]+',helper._PROFILE_VALID)
    def test_only_new_matching_token_and_channel_are_prelocked(self):
        self.queue(one(valid=True),one(count=1),one(valid=True),one(valid=True),Result(execute_error=ServerFailure('55P03')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload(recorded_at='infinity')])
        self.assertEqual(caught.exception.code,'55P03');self.assertEqual(len(self.connection.queries),5)
        self.assertIn('state.channel_id=input.channel_id',helper._LOCK)
        self.assertIn('state.lease_token=input.lease_token',helper._LOCK)
        self.assertIn('WHERE NOT EXISTS',helper._LOCK);self.assertNotIn('SKIP LOCKED',helper._LOCK)
    def test_recorded_then_availability_clock_fail_before_stale(self):
        for index in (5,6):
            entries=self.setup_success();self.connection.results[index]=one(valid=False)
            start=len(self.connection.queries)
            with self.assertRaises(DatabaseServerError) as caught:self.run_helper(entries)
            self.assertEqual((caught.exception.code,caught.exception.message),('22023','hot_source_clock_out_of_window'))
            self.assertEqual(len(self.connection.queries)-start,index+1);self.connection.results.clear()
    def test_receipt_only_replay_preserves_two_repeated_final_clock_pairs_and_skips_writes(self):
        entries=self.setup_success(2,owned_count=0);self.assertEqual(self.run_helper(entries),2)
        sql=[q for q,_ in self.connection.queries]
        self.assertEqual(len(sql),13);self.assertFalse(any(q.startswith(('UPDATE','INSERT')) for q in sql))
        self.assertEqual(sum(helper._AVAILABILITY_CLOCKS in q for q in sql),3)
        self.assertNotIn('FOR SHARE',' '.join(sql))
    def test_each_count_guard_rolls_back_and_stops_later_work(self):
        for guard,count in (('source',11),('snapshot',13),('queue',15)):
            entries=self.setup_success(guard=guard);start=len(self.connection.queries)
            with self.assertRaises(DatabaseServerError):self.run_helper(entries)
            self.assertEqual(len(self.connection.queries)-start,count)
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,3))
    def test_source_payload_retains_exact_bigint_extended_publication_and_native_arrays(self):
        entries=self.setup_success();self.run_helper(entries);value=self.connection.queries[10][1][0].obj[0]
        self.assertEqual(value['views'],9007199254740993);self.assertEqual(value['published_at'],'10000-01-01T00:00:00+00:00')
        self.assertIsNone(value['tags']);self.assertIn('coalesce(input.tags,ARRAY[]::text[])',helper._UPSERT)
        self.assertIn('input.youtube_topic_categories',helper._UPSERT)
    def test_missing_profile_clears_fields_instead_of_reclocking_prior_values(self):
        entries=self.setup_success(changes={'profile_collected':False});self.run_helper(entries)
        schedule=self.connection.queries[14][1][0].obj[0];self.assertFalse(schedule['profile_collected'])
        for column in ('youtube_topic_categories','default_audio_language','default_language','caption_available'):
            self.assertIn(column+'=excluded.'+column,helper._UPSERT)
        self.assertIn('CASE WHEN input.profile_collected THEN input.default_language END',helper._UPSERT)
        self.assertIn('profile_metadata_version=CASE WHEN input.profile_collected THEN 1 ELSE 0 END',helper._COMPLETE)
    def test_policy_rules_are_captured_once_and_both_due_times_remain_native(self):
        rules=cadence_rows()
        for rule in rules:
            if rule['video_type']=='long':rule['cadence_seconds']=43
        entries=self.setup_success(cadence=rules);self.run_helper(entries);schedule=self.connection.queries[14][1][0].obj[0]
        self.assertEqual((schedule['stats_seconds'],schedule['metadata_seconds']),(43,43))
        self.assertIn('extract(epoch FROM (recorded_at-published_at))::bigint',helper._POLICY_INPUT)
        self.assertIn('make_interval(secs=>%s::integer)',helper._COMPLETE)
    def test_unsupported_availability_keeps_legacy_null_ignoring_next_due_even_while_paused(self):
        entries=self.setup_success(changes={'availability_status':'private'});self.run_helper(entries)
        schedule=self.connection.queries[14][1][0].obj[0]
        self.assertEqual(schedule['paused_reason'],'availability_unsupported');self.assertIsNone(schedule['stats_seconds'])
        self.assertIn('next_due_at=least(schedule.stats_due_at,schedule.metadata_due_at,',helper._COMPLETE)
        self.assertNotIn('next_due_at=CASE',helper._COMPLETE)
    def test_empty_cadence_keeps_both_nominal_schedules_null_and_policy_version_one(self):
        entries=self.setup_success(cadence=[]);self.run_helper(entries);schedule=self.connection.queries[14][1][0].obj[0]
        self.assertIsNone(schedule['stats_seconds']);self.assertIsNone(schedule['metadata_seconds']);self.assertEqual(schedule['policy_version'],1)
    def test_final_clock_and_epoch_failures_roll_back_all_prior_mutations(self):
        for index,value in ((16,one(valid=False)),(17,one(channel_id=CHANNEL,control_epoch=8,is_hidden=False,owner_permission='accepted'))):
            entries=self.setup_success();self.connection.results[index]=value
            with self.assertRaises(DatabaseServerError):self.run_helper(entries)
            self.connection.results.clear()
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,2))
    def test_borrowed_error_poison_and_unknown_commit_never_replay(self):
        with self.assertRaises(DatabaseError) as caught:
            with self.database.transaction(Operation('metadata_completion_test')) as tx:
                with self.assertRaises(DatabaseServerError):helper.complete_video_metadata_collection(None,None,transaction=tx)
        self.assertEqual(caught.exception.message,'database_transaction_aborted')
        entries=self.setup_success();self.connection.commit_error=psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseUnavailable) as caught:self.run_helper(entries)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,2)
    def test_no_business_rpc_or_availability_wrapper_effects(self):
        code=(ROOT/'application/db/videos/video_metadata_completion.py').read_text(encoding='utf-8-sig')
        self.assertNotIn('.call(',code);self.assertNotIn('UPDATE public.video_availability_incidents',code)
        self.assertNotIn('public.video_collection_due_at(',code)

if __name__=='__main__':unittest.main()
