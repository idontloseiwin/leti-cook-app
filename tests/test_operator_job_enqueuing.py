"""Focused prepared enqueue/estimate boundaries; no database or provider I/O."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from pathlib import Path
import sys,unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_enqueuing as helper
from channel_workflow_fixtures import workflow_results
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,Operation,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
CHANNEL='UC0000000000000000000001';JOB='f9100000-0000-4000-8000-000000000001';AT='2026-10-06T00:00:00+00:00'
def one(**value):return Result((value,))
def write(count=1):return Result(has_columns=False,rowcount=count)
def facts(counts=(51,0,2),*,restart=False,statuses=None,absent=None,total=0):
    return dict(channel_id=CHANNEL,restart=restart,video_count=total,enqueue_allowed=True,
        facts=[dict(ordinal=i+1,kind=kind,count=count,status=(statuses or {}).get(kind,'ok'),
        absent=(absent or {}).get(kind,False),error=None) for i,(kind,count) in enumerate(zip(helper._TYPES,counts))])
def eligibility(initial=False):
    return (one(requested_at=AT),one(valid=True),one(hidden=False),
            one(present=True) if initial else one(policy_facts=native_policy_facts('active')))

def stats_args(**changes):return dict(dict(channel_id=CHANNEL,max_items=None,max_quota_units=100,max_target_attempts=3,expected_epoch=7),**changes)
def initial_args(**changes):return dict(dict(channel_id=CHANNEL,expected_items=53,expected_pages=4,expected_quota_units=6,
    max_items=None,max_pages=None,max_quota_units=None,max_attempts=3,confirmed=False,expected_epoch=7,as_of=AT),**changes)

class EnqueueTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda _:self.pool).open()
        self.addCleanup(self.database.close)
    def queue(self,*values):self.connection.results.extend(values)
    def stats(self,**changes):return helper.enqueue_channel_current_stats_backfill(self.database,CHANNEL,None,100,**changes)
    def initial(self,**changes):return helper.enqueue_channel_initial_inventory_job(self.database,CHANNEL,53,4,6,None,None,None,**changes)
    def accept(self,kind,permission='accepted'):
        self.queue(one(job_type=kind,leased=False),one(is_hidden=False,control_epoch=7,owner_permission=permission))
        if kind=='channel_current_stats_backfill':self.queue(one(policy_facts=native_policy_facts('active')))
        self.queue(one(control_epoch=7 if permission=='accepted' else 8),write())
    def stats_success(self,count=3):
        self.queue(one(**stats_args()),one(channel_id=CHANNEL),write(0),Result(),*eligibility(),one(id=JOB),write(count),one(target_count=count,quota=2*((count+49)//50)),write())
        self.accept('channel_current_stats_backfill');self.queue(one(job={'id':JOB,'target_count':count}))
    def initial_success(self):
        self.queue(one(**initial_args()),one(channel_id=CHANNEL),one(restart=False),one(policy_facts=native_policy_facts('operator_hold')),write(0),Result(),one(**facts()),*eligibility(True),one(id=JOB),*workflow_results('enqueue_pending'),one(channel_id=CHANNEL,length=24),write(0),write(),write())
        self.accept('channel_initial_long_inventory','manual_required');self.queue(one(job={'id':JOB,'accepted_epoch':8}))
    def test_estimate_one_read_only_query_and_exact_three_format_arithmetic(self):
        self.queue(one(**facts()));result=helper.estimate_channel_initial_inventory_job(self.database,CHANNEL)
        self.assertEqual((result['estimated_items'],result['estimated_pages'],result['estimated_hydration_units'],result['estimated_quota_units']),(53,4,2,6))
        self.assertEqual(result['items_lower_bound'],51);self.assertTrue(result['auto_hydration_allowed'])
        self.assertEqual(len(self.connection.queries),1)
        self.assertIn('SET TRANSACTION READ ONLY',str(self.connection.setup));self.assertEqual(self.connection.commits,1)
    def test_unknown_and_absent_estimates_preserve_lower_bound_and_status(self):
        self.queue(one(**facts((51,None,None),statuses={'short':'playlist_not_found','stream':'error'},absent={'short':True})))
        result=helper.estimate_channel_initial_inventory_job(self.database,CHANNEL)
        self.assertEqual(result['counts'],{'long':51,'short':None,'stream':None})
        self.assertEqual(result['statuses'],{'long':'ok','short':'playlist_not_found','stream':'error'})
        self.assertEqual(result['items_lower_bound'],51);self.assertIsNone(result['estimated_pages']);self.assertTrue(result['confirmation_required'])
        self.queue(one(**facts((0,None,None),statuses={'short':'playlist_not_found','stream':'playlist_not_found'},absent={'short':True,'stream':True})))
        result=helper.estimate_channel_initial_inventory_job(self.database,CHANNEL)
        self.assertEqual((result['estimated_items'],result['estimated_pages'],result['estimated_quota_units']),(0,3,3))
    def test_overflow_restart_and_youtube_total_force_existing_confirmation(self):
        for captured in (facts((2147483647,1,0)),facts(total=10001),facts(restart=True)):
            self.queue(one(**captured));result=helper.estimate_channel_initial_inventory_job(self.database,CHANNEL)
            self.assertTrue(result['confirmation_required']);self.assertFalse(result['auto_hydration_allowed'])
        self.assertIsNone(result['counts'].get('not_a_type'))
    def test_estimate_native_error_precedence_and_invisible_channel(self):
        captured=facts();captured['facts'][0]['error']='22023';captured['facts'][1]['error']='22009'
        self.queue(one(**captured))
        with self.assertRaises(DatabaseServerError) as caught:helper.estimate_channel_initial_inventory_job(self.database,CHANNEL)
        self.assertEqual(caught.exception.code,'22023');self.assertEqual(caught.exception.message,'database_operation_failed')
        self.queue(Result())
        with self.assertRaisesRegex(DatabaseError,'operator_job_channel_not_visible'):helper.estimate_channel_initial_inventory_job(self.database,None)
    def test_estimate_clock_parser_and_fallback_conditions_remain_native(self):
        self.assertIn("pg_input_error_info(clock_text,'timestamptz')",helper._ESTIMATE)
        self.assertIn("status='ok' AND error_code='22003'",helper._ESTIMATE)
        self.assertIn("estimate ? 'checked_at_by_type'",helper._ESTIMATE)
        self.assertIn("'^[0-9]{1,10}$'",helper._ESTIMATE)
        self.assertIn("status='playlist_not_found' THEN estimate->'checked_at_by_type'->>kind",helper._ESTIMATE)
        self.assertIn('ORDER BY ordinal',helper._ESTIMATE)
    def test_stats_target_insert_is_one_set_statement_for_one_or_thousand(self):
        for count in (1,1000):
            start=len(self.connection.queries);self.stats_success(count);result=self.stats(expected_epoch=7)
            self.assertEqual(result,dict(created=True,job={'id':JOB,'target_count':count}))
            sql=[q for q,_ in self.connection.queries[start:]];self.assertEqual(len(sql),18)
            self.assertEqual(sql.count(helper._TARGETS),1);self.assertIn('inventory.video_type=\'long\'',helper._TARGETS)
            self.assertNotIn('LIMIT',helper._TARGETS);self.assertIn('FOR UPDATE',sql[12]);self.assertIn('FOR UPDATE',sql[13])
    def test_stats_zero_targets_preserves_completed_job_creation(self):
        self.stats_success(0);self.assertTrue(self.stats(expected_epoch=7)['created'])
        self.assertIn("'completed','video_targets',0",helper._INSERT_STATS)
        self.assertEqual(self.connection.queries[11][1]['target_count'],0)
    def test_stats_native_cast_precedes_validation_and_validation_order(self):
        self.queue(Result(execute_error=ServerFailure('22P02')))
        with self.assertRaises(DatabaseServerError) as caught:self.stats()
        self.assertEqual(caught.exception.code,'22P02')
        for args,code in ((stats_args(expected_epoch=None,channel_id=None),'channel_collection_scope_invalid'),
                (stats_args(channel_id=' '),'operator_job_channel_required'),(stats_args(max_items=0,max_quota_units=0),'operator_job_max_items_required'),
                (stats_args(max_quota_units=0,max_target_attempts=0),'operator_job_max_quota_units_required'),
                (stats_args(max_target_attempts=21),'operator_job_max_target_attempts_invalid')):
            self.queue(one(**args))
            with self.assertRaisesRegex(DatabaseError,code):self.stats()
    def test_existing_stats_replay_fences_without_targets_or_acceptance(self):
        self.queue(one(**stats_args()),one(channel_id=CHANNEL),write(),one(job={'id':JOB,'job_type':'channel_current_stats_backfill'}),one(channel_id=CHANNEL,is_hidden=False,owner_permission='accepted',control_epoch=7))
        result=self.stats();self.assertFalse(result['created']);self.assertEqual(len(self.connection.queries),5)
        self.assertIn('FOR SHARE',self.connection.queries[-1][0]);self.assertNotIn(helper._TARGETS,[q for q,_ in self.connection.queries])
    def test_conflicting_job_rolls_back_stale_suspension(self):
        self.queue(one(**stats_args()),one(channel_id=CHANNEL),write(),one(job={'job_type':'channel_initial_long_inventory'}))
        with self.assertRaisesRegex(DatabaseError,'operator_job_channel_has_active_job'):self.stats()
        self.assertEqual(self.connection.commits,0)
    def test_stats_caps_fail_after_actual_insert_and_native_arithmetic_before_acceptance(self):
        self.queue(one(**stats_args(max_items=1)),one(channel_id=CHANNEL),write(),Result(),*eligibility(),one(id=JOB),write(2),one(target_count=2,quota=2))
        with self.assertRaisesRegex(DatabaseError,'database_operation_failed'):self.stats()
        self.assertEqual(len(self.connection.queries),11);self.assertEqual(self.connection.commits,0)
        self.assertIn('%s::integer+49',helper._COUNTS)
    def test_initial_fixed_prepare_order_and_one_transaction(self):
        self.initial_success();result=self.initial(expected_epoch=7)
        self.assertEqual(result,dict(created=True,job={'id':JOB,'accepted_epoch':8}))
        self.assertEqual(len(self.connection.queries),25)
        prepared=[params for sql,params in self.connection.queries if sql==helper._PREPARE]
        self.assertEqual([p[1] for p in prepared],['long','short','stream'])
        self.assertEqual([p[2][:4] for p in prepared],['UULF','UUSH','UULV'])
        self.assertEqual((self.pool.borrowed,self.connection.commits),(1,1))
        self.assertEqual(self.connection.queries[6][1]['as_of'],AT)
        summary=self.connection.queries[11][1]['summary'].obj
        self.assertEqual(summary['full_inventory_sources'],{});self.assertFalse(summary['confirmed'])
    def test_initial_limit_and_attempt_checks_before_channel_lock(self):
        for args,code in ((initial_args(expected_epoch=None,max_items=1),'channel_collection_scope_invalid'),
                (initial_args(max_items=0,max_attempts=0),'full_initial_limits_must_be_null'),
                (initial_args(max_attempts=0),'operator_job_max_target_attempts_invalid')):
            self.queue(one(**args))
            with self.assertRaisesRegex(DatabaseError,code):self.initial()
        self.assertEqual(len(self.connection.queries),3)
    def test_restart_confirmation_precedes_stale_job_mutation_and_estimate(self):
        self.queue(one(**initial_args()),one(channel_id=CHANNEL),one(restart=True))
        with self.assertRaisesRegex(DatabaseError,'operator_job_confirmation_required'):self.initial()
        self.assertEqual(len(self.connection.queries),3)
    def test_initial_status_gate_precedes_existing_job_and_estimate(self):
        self.queue(one(**initial_args()),one(channel_id=CHANNEL),one(restart=False),one(policy_facts=native_policy_facts('active')))
        with self.assertRaisesRegex(DatabaseError,'operator_job_initial_inventory_status_not_eligible'):self.initial()
        self.assertEqual(len(self.connection.queries),4)
    def test_changed_estimate_precedes_confirmation_and_does_not_insert(self):
        self.queue(one(**initial_args(expected_items=1)),one(channel_id=CHANNEL),one(restart=False),one(policy_facts=native_policy_facts('operator_hold')),write(),Result(),one(**facts(total=10001)))
        with self.assertRaisesRegex(DatabaseError,'operator_job_estimate_changed'):self.initial()
        self.assertEqual(len(self.connection.queries),7)
    def test_acceptance_lost_epoch_rolls_back_every_prior_write(self):
        self.stats_success();self.connection.results[13]=one(is_hidden=False,control_epoch=8,owner_permission='accepted')
        with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_lost'):self.stats()
        self.assertEqual((self.connection.commits,self.connection.rollbacks),(0,1))
    def test_borrowed_error_poison_and_no_unknown_commit_retry(self):
        self.queue(one(**stats_args(expected_epoch=None)))
        with self.assertRaises(DatabaseError):
            with self.database.transaction(Operation('enqueue_test')) as tx:
                with self.assertRaises(DatabaseError):helper.enqueue_channel_current_stats_backfill(None,CHANNEL,None,100,transaction=tx)
        self.stats_success();self.connection.commit_error=psycopg.OperationalError('private')
        with self.assertRaises(DatabaseUnavailable) as caught:self.stats()
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,2)
    def test_every_fixed_statement_adapts_parameters_without_query_builder(self):
        for name,value in vars(helper).items():
            if name.startswith('_') and isinstance(value,str) and value.lstrip().startswith(('SELECT','WITH','UPDATE','INSERT')) and name not in ('_RESTART_EXPRESSION',):
                # Missing values are accepted here; only psycopg placeholder compilation is checked.
                import re
                names=re.findall(r'%\(([^)]+)\)s',value);parameters={key:None for key in names} if names else tuple(None for _ in re.findall('%s',value))
                PostgresQuery(Transformer()).convert(value,parameters)
        source=(ROOT/'application/db/jobs/operator_job_enqueuing.py').read_text()
        self.assertNotIn('.call(',source);self.assertNotIn('CREATE TEMP',source)

if __name__=='__main__':unittest.main()
