"""Offline publication receipt protocol using the real bounded transaction driver."""
from pathlib import Path
import hashlib,sys,unittest
import psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_stream_completion as helper
from collection_schedule import CollectionSchedules
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
from test_collection_policy import rows as cadence_rows,NOW
TOKEN='ca000000-0000-4000-8000-000000000002';ATTEMPT='cc000000-0000-4000-8000-000000000002'
CHANNEL='UC0000000000000000000001'
def one(**values):return Result((values,))
def payload(video='ss000000001',**values):
    return dict(dict(video_id=video,channel_id=CHANNEL,video_type='stream',lease_token=TOKEN,
        collection_attempt_id=ATTEMPT,recorded_at=NOW.isoformat(),stream_actual_end_at=None,
        availability_status='unlisted',privacy_status='unlisted',upload_status='uploaded',
        title='Future publication',published_at=NOW.isoformat(),duration=None,
        publication_status='upcoming',scheduled_start_at=None),**values)

class StreamCompletionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),cadence_snapshot=CollectionSchedules().cadence_snapshot(),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*values):self.connection.results.extend(values)
    def setup_success(self,count=1,*,owned_count=None,pending_count=None,cadence=None,matches=None):
        owned_count=count if owned_count is None else owned_count
        pending_count=count if pending_count is None else pending_count
        rows=[payload(f'ss{i:09d}') for i in range(count)];owned=rows[:owned_count]
        cadence=cadence_rows() if cadence is None else cadence
        matches=([i for i,r in enumerate(cadence) if r['video_type']=='stream' and r['min_age_seconds']==0] if matches is None else matches)
        scopes=[dict(channel_id=CHANNEL,control_epoch=7)] if owned else []
        self.queue(one(count=count),one(valid=True),Result(tuple(dict(video_id=r['video_id']) for r in rows)),one(valid=True),
            one(mismatch=False),one(indices=list(range(1,pending_count+1))),one(stale=False),
            Result(tuple(dict(video_id=r['video_id']) for r in owned)),one(regression=False),one(mismatch=False),
            one(scopes=scopes),Result(),one(sources=[dict(video_id=r['video_id'],fallback_at=None,matches=matches) for r in owned],cadence=cadence),
            *([one(count=owned_count,snapshot_text='[]')] if owned else []),
            Result(has_columns=False,rowcount=owned_count),
            *([one(present=owned_count,required=False)] if owned else []),
            Result(),Result(has_columns=False,rowcount=0),
            Result(has_columns=False,rowcount=owned_count),one(valid=True))
        if scopes:self.queue(one(channel_id=CHANNEL,control_epoch=7,is_hidden=False,owner_permission='accepted'))
        return rows
    def run_helper(self,rows):return helper.complete_stream_lifecycle_probe(self.database,rows)
    def test_exact_receipt_stage_order_and_single_atomic_queue_owner(self):
        rows=self.setup_success();self.assertEqual(self.run_helper(rows),1)
        sql=[v[0] for v in self.connection.queries]
        for index,fragment in ((2,helper._LOCK),(4,helper._MISMATCH),(6,helper._STALE),(7,helper._INSERT),
            (8,helper._REGRESSION),(9,helper._MISMATCH),(10,helper._SCOPES),(11,helper._INCIDENT_LOCK),
            (12,helper._POLICY),(14,helper._AVAILABILITY),(16,helper._RESTORE),(17,helper._EVENTS),(18,helper._COMPLETE)):
            self.assertIn(fragment,sql[index])
        self.assertIn('FOR SHARE',sql[-1]);self.assertEqual((len(sql),self.pool.borrowed,self.connection.commits),(21,1,1))
        self.assertEqual(self.connection.queries[7][1]['indices'],[1])
        self.assertEqual(self.connection.queries[8][1]['owned_ids'],[rows[0]['video_id']])
    def test_one_and_fifty_inputs_have_constant_twenty_one_statements(self):
        for count in (1,50):
            start=len(self.connection.queries);rows=self.setup_success(count)
            self.assertEqual(self.run_helper(rows),count);self.assertEqual(len(self.connection.queries)-start,21)
            self.assertEqual(len(self.connection.queries[-3][1]['schedules'].obj),count)
        self.assertIn(51,self.connection.fetch_sizes)
    def test_empty_and_nonarray_contract(self):
        self.queue(one(count=0));self.assertEqual(self.run_helper([]),0)
        for value in (None,{},'private'):
            with self.assertRaises(DatabaseServerError) as caught:self.run_helper(value)
            self.assertEqual(caught.exception.message,'stream_lifecycle_completion_requires_array')
        self.assertEqual(len(self.connection.queries),1)
    def test_native_casts_precede_fifty_cap_and_no_oversized_array_is_returned(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()]*51)
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
        self.queue(one(count=51))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload()]*51)
        self.assertEqual(caught.exception.message,'stream_lifecycle_completion_input_invalid')
        self.assertIn('AS MATERIALIZED',helper._COUNT);self.assertNotIn('LIMIT',helper._COUNT)
    def test_invalid_shape_duplicates_and_nonstream_end_reject_before_lock(self):
        self.queue(one(count=1),one(valid=False))
        with self.assertRaises(DatabaseServerError):self.run_helper([payload(stream_actual_end_at=NOW.isoformat())])
        self.assertEqual(len(self.connection.queries),2)
        self.assertIn("video_type NOT IN ('long','short','stream','unknown')",helper._VALID)
        self.assertIn('stream_actual_end_at IS NOT NULL',helper._VALID);self.assertIn('GROUP BY video_id',helper._VALID)
    def test_waiting_queue_lock_precedes_hot_clock_and_receipts(self):
        self.queue(one(count=1),one(valid=True),Result(execute_error=ServerFailure('55P03')))
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper([payload(recorded_at='infinity')])
        self.assertEqual(caught.exception.code,'55P03');self.assertEqual(len(self.connection.queries),3)
        self.assertNotIn('SKIP LOCKED',helper._LOCK)
    def test_hot_clock_failure_precedes_receipt_mismatch(self):
        rows=self.setup_success();self.connection.results[3]=one(valid=False)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.code,'22023');self.assertEqual(len(self.connection.queries),4)
    def test_early_receipt_mismatch_precedes_stale_and_any_mutation(self):
        rows=self.setup_success();self.connection.results[4]=one(mismatch=True)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'stream_lifecycle_completion_receipt_mismatch')
        self.assertEqual(len(self.connection.queries),5)
    def test_stale_owner_or_known_stream_end_precedes_receipt_insert(self):
        rows=self.setup_success();self.connection.results[6]=one(stale=True)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'stream_lifecycle_completion_stale_or_ineligible')
        self.assertEqual(len(self.connection.queries),7)
        self.assertIn('state.typed_stream_actual_end_at IS NOT NULL',helper._STALE)
        self.assertIn(helper._ADMITTED,helper._STALE)
        self.assertIn("state.video_type IS DISTINCT FROM nullif(input.video_type,'unknown')",helper._STALE)
        self.assertIn("control.owner_permission='accepted'",helper._STALE)
        self.assertNotIn('public.typed_video_collection_candidates',helper._STALE)
    def test_new_owned_clock_regression_rolls_back_receipt_before_race_check(self):
        rows=self.setup_success();self.connection.results[8]=one(regression=True)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'typed_completion_observation_clock_regression')
        self.assertEqual(len(self.connection.queries),9);self.assertEqual(self.connection.commits,0)
    def test_post_insert_receipt_mismatch_rolls_back_before_policy_or_queue(self):
        rows=self.setup_success();self.connection.results[9]=one(mismatch=True)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'stream_lifecycle_completion_receipt_mismatch')
        self.assertEqual(len(self.connection.queries),10);self.assertEqual(self.connection.commits,0)
    def test_exact_replay_and_concurrent_receipt_loser_have_no_owned_scope_or_mutation(self):
        for pending in (0,1):
            start=len(self.connection.queries);rows=self.setup_success(owned_count=0,pending_count=pending,cadence=[])
            self.assertEqual(self.run_helper(rows),1)
            self.assertEqual(len(self.connection.queries)-start,18)
            self.assertEqual(self.connection.queries[-2][1]['owned_ids'],[])
            self.assertEqual(self.connection.queries[-2][1]['schedules'].obj,[])
    def test_restored_live_incident_keeps_actual_status_and_replay_safe_event(self):
        rows=self.setup_success()
        restored=dict(video_id=rows[0]['video_id'],channel_id=CHANNEL,restored_at=NOW.isoformat(),
                      restored_status='unlisted',latest_status='unknown_unavailable')
        self.connection.results[16]=Result((restored,))
        self.connection.results[17]=Result(has_columns=False,rowcount=1)
        self.assertEqual(self.run_helper(rows),1)
        self.assertEqual(self.connection.queries[17][1][0].obj,[restored])
        self.assertIn('incident.restored_at::text AS restored_at',helper._RESTORE)
        self.assertIn('FOR UPDATE OF incident',helper._INCIDENT_LOCK)
    def test_native_fingerprint_covers_raw_extra_fields_and_never_roundtrips_payload(self):
        rows=self.setup_success();rows[0]['extra']={'nested':9007199254740993}
        self.run_helper(rows)
        self.assertIn('md5(raw.payload::text)',helper._INPUT)
        self.assertIs(self.connection.queries[7][1]['rows'].obj,rows)
        self.assertIn('payload_fingerprint IS DISTINCT FROM input.payload_fingerprint',helper._MISMATCH)
        self.assertNotIn('created_at',helper._INSERT)
    def test_cadence_and_native_calendar_matches_share_snapshot_and_python_jitter(self):
        rows=self.setup_success();self.run_helper(rows)
        source=self.connection.queries[18][1]['schedules'].obj[0]
        rule=next(r for r in cadence_rows() if r['video_type']=='stream' and r['min_age_seconds']==0)
        jitter=int(hashlib.md5(rows[0]['video_id'].encode()).hexdigest()[:8],16)%(rule['jitter_window_seconds']+1)
        self.assertEqual(source['seconds'],rule['cadence_seconds']-rule['jitter_window_seconds']+jitter)
        self.assertIn('cadence AS MATERIALIZED',helper._POLICY)
        self.assertIn('sources.anchor<sources.recorded_at-make_interval(days=>',helper._POLICY)
        self.assertIn('sources.anchor>=sources.recorded_at-make_interval(days=>',helper._POLICY)
        self.assertIn('make_interval(secs=>schedule.seconds)',helper._COMPLETE)
    def test_missing_cadence_rolls_back_receipt_before_queue_update(self):
        rows=self.setup_success(cadence=[],matches=[])
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'typed_video_collection_cadence_missing')
        self.assertEqual(len(self.connection.queries),13);self.assertEqual(self.connection.commits,0)
    def test_settlement_count_and_final_clock_fence_fail_atomically(self):
        rows=self.setup_success();self.connection.results[18]=Result(has_columns=False,rowcount=0)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'typed_completion_owned_settlement_count_mismatch')
        self.assertEqual(len(self.connection.queries),19);self.assertEqual(self.connection.commits,0)
    def test_final_source_clock_failure_rolls_back_after_settlement_before_scope(self):
        rows=self.setup_success();self.connection.results[19]=one(valid=False)
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.code,'22023')
        self.assertEqual(len(self.connection.queries),20);self.assertEqual(self.connection.commits,0)
    def test_policy_priority_uses_version_then_minimum_from_captured_native_matches(self):
        rules=[r for r in cadence_rows() if r['video_type']=='stream']
        first=dict(rules[0],policy_version=2,cadence_seconds=123,jitter_window_seconds=0)
        second=dict(rules[1],policy_version=2,cadence_seconds=456,jitter_window_seconds=0)
        schedules=helper._schedules(dict(cadence=[first,second],sources=[dict(video_id='ss000000001',fallback_at=None,matches=[0,1])]))
        self.assertEqual(schedules[0]['seconds'],456)
        self.assertIsNone(schedules[0]['fallback_at'])

    def test_final_channel_epoch_mismatch_rolls_back_receipt_and_queue(self):
        rows=self.setup_success();self.connection.results[-1]=one(channel_id=CHANNEL,control_epoch=8,is_hidden=False,owner_permission='accepted')
        with self.assertRaises(DatabaseServerError) as caught:self.run_helper(rows)
        self.assertEqual(caught.exception.message,'channel_collection_scope_lost');self.assertEqual(self.connection.commits,0)
    def test_caught_borrowed_python_failure_poisons_parent_transaction(self):
        with self.assertRaises(DatabaseError) as caught:
            with self.database.transaction(Operation('stream_completion_test')) as tx:
                with self.assertRaises(DatabaseServerError):helper.complete_stream_lifecycle_probe(None,None,transaction=tx)
        self.assertEqual(caught.exception.message,'database_transaction_aborted');self.assertEqual(self.connection.commits,0)
    def test_unknown_commit_is_not_retried(self):
        rows=self.setup_success();self.connection.commit_error=psycopg.OperationalError('private')
        with self.assertRaises(DatabaseUnavailable) as caught:self.run_helper(rows)
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
    def test_waiting_profile_renews_its_observation_only_without_counter_snapshot(self):
        import inspect
        code=inspect.getsource(helper)
        self.assertNotIn('.call(',code);self.assertNotIn('public.typed_video_collection_due_at(',code)
        self.assertNotIn('public.typed_video_collection_identity_eligible(',code)
        self.assertIn('availability_status=excluded.availability_status',helper._AVAILABILITY)
        self.assertIn('restored_status=input.availability_status',helper._RESTORE)
        self.assertIn('video_availability_events',helper._EVENTS)
        self.assertIn('availability_first_missing_at=NULL',helper._COMPLETE)
        self.assertIn('availability_schedule_type=NULL',helper._COMPLETE)
        self.assertIn('availability_resume_observed_at>input.recorded_at',helper._COMPLETE)
        self.assertIn('metadata_checked_at=excluded.metadata_checked_at',helper._AVAILABILITY)
        self.assertIn('publication_status=excluded.publication_status',helper._AVAILABILITY)
        self.assertIn('scheduled_start_at=excluded.scheduled_start_at',helper._AVAILABILITY)
        self.assertIn('views=NULL,like_count=NULL,comment_count=NULL,checked_at=NULL',helper._AVAILABILITY)
        self.assertIn('stats_source_checked_at=NULL',helper._COMPLETE)
        self.assertNotIn('INSERT INTO public.video_snapshots',code)
        for field in ('attempt_count=','last_claim_run_id=','typed_source_published_at=','typed_stream_actual_end_at='):
            self.assertNotIn(field,helper._COMPLETE)

    def test_all_publication_statements_have_bound_native_parameters(self):
        from psycopg._queries import PostgresQuery
        from psycopg.adapt import Transformer
        from psycopg.types.json import Jsonb
        params=dict(rows=Jsonb([payload()]),indices=[1],owned_ids=['ss000000001'],
            cadence=Jsonb([]),identity_seconds=86400,schedules=Jsonb([]),max_interval_seconds=2419200)
        for name in ('_COUNT','_VALID','_LOCK','_MISMATCH','_PENDING','_STALE','_INSERT',
                     '_REGRESSION','_SCOPES','_INCIDENT_LOCK','_AVAILABILITY','_RESTORE','_POLICY','_COMPLETE'):
            with self.subTest(name=name):
                PostgresQuery(Transformer()).convert(getattr(helper,name),params)

    def test_known_future_date_shortens_due_but_past_date_does_not_loop(self):
        self.assertIn('input.scheduled_start_at>input.recorded_at',helper._DUE)
        self.assertIn('input.recorded_at+make_interval(secs=>%(max_interval_seconds)s::integer)',helper._DUE)
        self.assertIn('metadata_due_at='+helper._DUE,helper._COMPLETE)
        self.assertIn('next_due_at='+helper._DUE,helper._COMPLETE)

    def test_unknown_identity_uses_existing_recheck_without_assigning_type(self):
        result=helper._schedules(dict(cadence=[],sources=[dict(video_id='unknown',
            fallback_at=None,identity_seconds=86400,matches=[])]))
        self.assertEqual(result,[dict(video_id='unknown',fallback_at=None,seconds=86400)])
        self.assertIn("nullif(input.video_type,'unknown')",helper._INSERT)
        self.assertNotIn('SET video_type=',helper._COMPLETE)

    def test_long_uses_own_metadata_rules_and_typed_metadata_keeps_calendar_rules(self):
        self.assertIn("rule.video_type=sources.video_type",helper._POLICY)
        self.assertIn("sources.video_type='long'",helper._POLICY)
        self.assertIn("sources.video_type IN ('short','stream')",helper._POLICY)
        self.assertIn('rule.min_age_seconds<=sources.age_seconds',helper._POLICY)
        self.assertIn('video.metadata_checked_at',helper._REGRESSION)

if __name__=='__main__':unittest.main()
