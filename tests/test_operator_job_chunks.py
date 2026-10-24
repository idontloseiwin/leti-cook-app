"""Focused operator chunk preparation boundaries; no database/provider execution."""
from pathlib import Path
import json,re,sys,unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_chunks as helper
from db.jobs.operator_job_inventory_evidence import _NativeObject
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
CHANNEL='UC0000000000000000000001';JOB='f9100000-0000-4000-8000-000000000001';TOKEN='f9200000-0000-4000-8000-000000000001'
VIDEO='aaaaaaaaaaa';AT='2026-10-06T00:00:00+00:00'
def one(**value):return Result((value,))
def write(count=1):return Result(has_columns=False,rowcount=count)
def job(**changes):return dict(dict(id=JOB,channel_id=CHANNEL,job_type='channel_current_stats_backfill',status='running',
 phase='video_targets',lease_token=TOKEN,lease_channel_epoch=7,accepted_epoch=7,initial_scope_version=0,
 initial_video_types=['long'],targets_frozen_at=None,summary_json={},quota_units_reserved=0,max_quota_units=100),**changes)
def encoded(value):return json.dumps(value,separators=(',',':'))
def held(value,invalid=False):return one(value=encoded(value),invalid=invalid)
def returned(value):return one(value=encoded(value))
def owned(query):
    for prefix,suffix in (('SELECT * FROM (',') AS lic_bounded_result LIMIT 2'),('WITH lic_changed AS (',') SELECT * FROM lic_changed LIMIT 2')):
        if query.startswith(prefix) and query.endswith(suffix):return query[len(prefix):-len(suffix)]
    return query
def args(**changes):return dict(dict(job_id=JOB,lease_token=TOKEN,limit=50,lease_seconds=1200),**changes)

class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda _:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*values):self.connection.results.extend(values)
    def invoke(self,**values):return helper.prepare_operator_job_chunk(self.database,JOB,TOKEN,**values)
    def fence(self,epoch=7):self.queue(one(channel_id=CHANNEL,is_hidden=False,owner_permission='accepted',control_epoch=epoch))
    def start(self,value=None,*,pending=1,invalid=False,limit=50):
        value=value or job();self.queue(one(**args(limit=limit)),held(value,invalid),write(0),write(0),write(0),one(count=pending));return value
    def success(self,count=1):
        value=self.start(limit=count);ids=[str(i).zfill(11) for i in range(count)]
        self.queue(one(ids=ids),one(reserved=2*((count+49)//50),blocked=False),write(count),returned({**value,'quota_units_reserved':2*((count+49)//50)}),one(value=[dict(video_id=v,video_type='long') for v in ids]));self.fence();return ids
    def test_selected_batch_has_constant_query_count_and_exact_return_shape(self):
        for count in (1,250):
            before=len(self.connection.queries);ids=self.success(count);result=self.invoke(limit=count)
            self.assertEqual(set(result),{'job','video_ids','scope','targets','reserved_units','deferred'})
            self.assertEqual(result['video_ids'],ids);self.assertEqual(result['reserved_units'],2*((count+49)//50));self.assertFalse(result['deferred'])
            queries=self.connection.queries[before:];self.assertEqual(len(queries),12)
            self.assertEqual([owned(q) for q,_ in queries[2:5]],[helper._REFRESHED,helper._UNAVAILABLE,helper._RETRIES])
            self.assertIn('FOR UPDATE OF target SKIP LOCKED LIMIT',queries[6][0]);self.assertEqual(queries[6][1]['limit'],count)
            self.assertIn('FOR SHARE',queries[-1][0])
        self.assertEqual(self.connection.commits,2)
    def test_completed_return_omits_deferred_targets_scope_and_preserves_late_fence(self):
        value=self.start(pending=0);self.queue(returned({**value,'status':'completed','lease_channel_epoch':None}));self.fence()
        result=self.invoke();self.assertEqual(set(result),{'job','video_ids','reserved_units'});self.assertEqual(result['video_ids'],[])
        self.assertEqual(owned(self.connection.queries[-2][0]),helper._COMPLETE);self.assertEqual(self.connection.queries[-1][1],([CHANNEL],))
    def test_empty_select_defers_and_quota_block_has_distinct_shape(self):
        value=self.start();self.queue(one(ids=[]),returned({**value,'status':'queued','lease_channel_epoch':None}));self.fence()
        result=self.invoke();self.assertEqual(set(result),{'job','video_ids','reserved_units','deferred'});self.assertTrue(result['deferred'])
        before=len(self.connection.queries);value=self.start();self.queue(one(ids=[VIDEO]),one(reserved=2,blocked=True),returned({**value,'status':'blocked','lease_channel_epoch':None}));self.fence()
        result=self.invoke();self.assertEqual(set(result),{'job','video_ids','reserved_units'});self.assertEqual(result['reserved_units'],0)
        self.assertNotIn(helper._ATTEMPTS,[owned(q) for q,_ in self.connection.queries[before:]])
    def test_native_cast_and_bound_validation_precede_job_lock(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.invoke()
        self.assertEqual(caught.exception.code,'22P02')
        for values,code in ((args(limit=None,lease_seconds=0),'operator_job_chunk_limit_invalid'),(args(limit=251),'operator_job_chunk_limit_invalid'),
                (args(lease_seconds=None),'operator_job_lease_seconds_invalid'),(args(lease_seconds=59),'operator_job_lease_seconds_invalid'),(args(lease_seconds=3601),'operator_job_lease_seconds_invalid')):
            self.queue(one(**values));before=len(self.connection.queries)
            with self.assertRaisesRegex(DatabaseError,code):self.invoke()
            self.assertEqual(len(self.connection.queries)-before,1)
    def test_native_nullable_lease_expression_is_not_tightened_by_python(self):
        value=self.start(pending=0,invalid=None);self.queue(returned({**value,'status':'completed'}));self.fence()
        self.assertEqual(self.invoke()['job']['status'],'completed')
        self.assertIn('j.lease_expires_at<=now()',helper._LOCK);self.assertNotIn('coalesce',helper._LOCK)
        self.queue(one(**args()),held(job(),True))
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.invoke()
    def test_initial_phase_error_precedes_full_scope_proof(self):
        self.queue(one(**args()),held(job(job_type='channel_initial_long_inventory',phase='inventory')))
        with patch.object(helper,'assert_full_initial_scope') as proof,self.assertRaisesRegex(DatabaseError,'operator_job_initial_phase_invalid'):self.invoke()
        proof.assert_not_called();self.assertEqual(len(self.connection.queries),2)
    def test_late_bad_epoch_rolls_back_status_mutation_before_return(self):
        value=self.start(job(accepted_epoch=8),pending=0);self.queue(returned({**value,'status':'completed','lease_channel_epoch':None}))
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):self.invoke()
        self.assertEqual(owned(self.connection.queries[-1][0]),helper._COMPLETE);self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_native_quota_overflow_wins_before_late_epoch_and_attempt_increment(self):
        self.start(job(accepted_epoch=8));self.queue(one(ids=[VIDEO]),Result(execute_error=ServerFailure('22003')))
        with self.assertRaises(DatabaseServerError) as caught:self.invoke()
        self.assertEqual(caught.exception.code,'22003');self.assertEqual(owned(self.connection.queries[-1][0]),helper._QUOTA)
        self.assertIn("CASE WHEN j.job_type<>'channel_initial_long_inventory'",helper._QUOTA)
        self.assertEqual(self.connection.commits,0)
    def test_final_channel_loss_rolls_back_reservation_and_attempts(self):
        self.success();self.connection.results[-1]=one(channel_id=CHANNEL,is_hidden=False,owner_permission='accepted',control_epoch=8)
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):self.invoke()
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_full_typed_filter_order_chunk_capture_and_summary_snapshot(self):
        value=job(job_type='channel_initial_long_inventory',phase='hydrate_videos',initial_scope_version=1,initial_video_types=['long','short','stream'],targets_frozen_at=AT)
        self.queue(one(**args()),held(value),one(ids=[VIDEO]),write(),write(),write(),write(),write(),one(count=1),one(ids=[VIDEO]),returned({**value,'summary_json':{'full_hydration_chunk':[VIDEO]}}),one(reserved=2,blocked=False),write(),returned(value),one(value={'long':{'current_profile_ready':True}}),returned(value),one(value=[dict(video_id=VIDEO,video_type='long')]))
        self.fence()
        with patch.object(helper,'assert_full_initial_scope') as proof:result=self.invoke()
        proof.assert_called_once();self.assertEqual(proof.call_args.args[2],True);self.assertEqual(result['scope'],'all')
        queries=[owned(q) for q,_ in self.connection.queries]
        self.assertEqual(queries[2:8],[helper._FULL_CHUNK,helper._IDENTITY,helper._REFRESHED,helper._UNAVAILABLE,helper._AWAITING,helper._RETRIES])
        self.assertLess(queries.index(helper._CHUNK),queries.index(helper._QUOTA));self.assertLess(queries.index(helper._SUMMARY),queries.index(helper._TARGETS))
        self.assertNotIn('ANY(%(chunk)s',helper._RETRIES)
    def test_full_retry_marker_prevents_premature_completion_on_zero_pending(self):
        value=job(job_type='channel_initial_long_inventory',phase='hydrate_videos',initial_video_types=['long','short','stream'],initial_scope_version=1,targets_frozen_at=AT,summary_json={'full_retry_after':'a','full_retry_requested_at':AT})
        self.queue(one(**args()),held(value),one(after_id='a',cutoff=AT),one(ids=[VIDEO]),write(),returned(value),one(ids=[]),write(),write(),write(),write(),write(),one(count=0),one(ids=[]),returned(value),returned({**value,'status':'queued'}),one(value={}),returned({**value,'status':'queued'}));self.fence()
        with patch.object(helper,'assert_full_initial_scope'):result=self.invoke()
        self.assertTrue(result['deferred']);self.assertNotIn(helper._COMPLETE,[owned(q) for q,_ in self.connection.queries])
        self.assertEqual([owned(q) for q,_ in self.connection.queries[2:6]],[helper._REPAIR_CLOCK,helper._REPAIR_IDS,helper._REPAIR_TARGETS,helper._REPAIR_JOB])
        self.assertIn('completed_at<=%(cutoff)s::timestamptz',helper._REPAIR_IDS)
    def test_repair_noop_precedes_invalid_cutoff_cast_and_reads_nothing(self):
        value=_NativeObject(encoded(job(summary_json={'full_retry_after':'a','full_retry_requested_at':'bad'})))
        with self.database.transaction(Operation('chunk_test')) as tx:self.assertIs(helper.repair_full_initial_target_chunk(tx,value,50),value)
        self.assertEqual(self.connection.queries,[])
    def test_initial_wrapper_locks_twice_then_transitions_completion_to_verify(self):
        value=job(job_type='channel_initial_long_inventory',phase='hydrate_videos')
        self.queue(one(**args()),held(value),held(value),write(),write(),write(),one(count=0),returned({**value,'status':'completed','lease_channel_epoch':None}),returned({**value,'status':'queued','phase':'verify','lease_channel_epoch':None}));self.fence()
        with patch.object(helper,'assert_full_initial_scope'):result=helper.prepare_initial_hydration_job_chunk(self.database,JOB,TOKEN)
        self.assertEqual((result['job']['status'],result['job']['phase']),('queued','verify'))
        queries=[owned(q) for q,_ in self.connection.queries];self.assertEqual(queries.count(helper._LOCK),2);self.assertEqual(queries[-2],helper._VERIFY)
        self.assertEqual(self.pool.borrowed,1)
    def test_initial_wrapper_wrong_type_rejects_before_provisional(self):
        self.queue(one(**args()),held(job()))
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):helper.prepare_initial_hydration_job_chunk(self.database,JOB,TOKEN)
        self.assertEqual(len(self.connection.queries),2)
    def test_raw_summary_numeric_lexeme_survives_native_composite_parameters(self):
        value=job(summary_json={'huge':0});raw=encoded(value).replace('"huge":0','"huge":1e1000')
        self.queue(one(**args()),one(value=raw,invalid=False),write(),write(),write(),one(count=0),one(value=raw));self.fence();self.invoke()
        self.assertTrue(all('1e1000' in params['job'] for query,params in self.connection.queries if query in (helper._REFRESHED,helper._UNAVAILABLE,helper._RETRIES)))
    def test_borrowed_failure_poison_and_unknown_ack_never_retries(self):
        self.queue(one(**args(limit=None)))
        with self.assertRaises(DatabaseError):
            with self.database.transaction(Operation('chunk_test')) as tx:
                with self.assertRaises(DatabaseError):helper.prepare_operator_job_chunk(None,JOB,TOKEN,transaction=tx)
        self.success();self.connection.commit_error=psycopg.OperationalError('private')
        with self.assertRaises(DatabaseUnavailable) as caught:self.invoke()
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,2)
    def test_native_summary_boolean_failure_precedes_epoch_check(self):
        value=job(job_type='channel_initial_long_inventory',phase='hydrate_videos',initial_video_types=['long','short','stream'],accepted_epoch=8)
        self.queue(one(**args()),held(value),write(),write(),write(),write(),write(),one(count=0),returned(value),Result(execute_error=ServerFailure('22P02')))
        with patch.object(helper,'assert_full_initial_scope'),self.assertRaises(DatabaseServerError) as caught:self.invoke()
        self.assertEqual(caught.exception.code,'22P02');self.assertEqual(owned(self.connection.queries[-1][0]),helper._FORMATS)
        self.assertEqual(self.connection.commits,0)
    def test_initial_hydration_accepts_400_but_manual_limit_remains_250(self):
        value=job(job_type='channel_initial_long_inventory',phase='hydrate_videos')
        for count in (251,400):
            self.queue(one(**args(limit=count)),held(value),held(value),write(),write(),write(),one(count=0),
                returned({**value,'status':'completed'}),returned({**value,'status':'queued','phase':'verify'}))
            self.fence()
            with patch.object(helper,'assert_full_initial_scope'):
                result=helper.prepare_initial_hydration_job_chunk(self.database,JOB,TOKEN,limit=count)
            self.assertEqual(result['job']['phase'],'verify')
        self.queue(one(**args(limit=401)))
        with self.assertRaisesRegex(DatabaseError,'operator_job_chunk_limit_invalid'):
            helper.prepare_initial_hydration_job_chunk(self.database,JOB,TOKEN,limit=401)
        self.queue(one(**args(limit=400)))
        with self.assertRaisesRegex(DatabaseError,'operator_job_chunk_limit_invalid'):
            self.invoke(limit=400)
        self.assertIn("CASE WHEN j.job_type='channel_initial_long_inventory' AND j.initial_scope_version=1 THEN 1 ELSE 2 END",helper._QUOTA)

    def test_fixed_sql_parameter_compilation_and_no_rpc_or_provider(self):
        for name,value in vars(helper).items():
            if name.startswith('_') and isinstance(value,str) and value.lstrip().startswith(('SELECT','UPDATE')) and name not in ('_FILTER',):
                names=re.findall(r'%\(([^)]+)\)s',value);values={key:None for key in names} if names else tuple(None for _ in re.findall('%s',value))
                PostgresQuery(Transformer()).convert(value,values)
        source=(ROOT/'application/db/jobs/operator_job_chunks.py').read_text()
        self.assertNotIn('.call(',source);self.assertNotIn('CREATE TEMP',source);self.assertNotIn('datetime',source)

if __name__=='__main__':unittest.main()
