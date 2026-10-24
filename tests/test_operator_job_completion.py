"""Offline ordering, native-value boundaries and ownership for F8 completion."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_completion as completion
from db.base.postgres import DatabaseError, DatabaseServerError, _reviewed_statement
from test_operator_job_lifecycle import Database, ID, CHANNEL

NOW=datetime(2026,3,8,9,30,tzinfo=timezone.utc)
TOKEN='f8100000-0000-4000-8000-000000000002'


def arguments(**changes):
    value=dict(job_id=ID,lease_token=TOKEN,reserved_units=3,observed_quota_units=2,status='completed',summary_is_null=False)
    value.update(changes);return value


def job(**changes):
    value=dict(id=ID,channel_id=CHANNEL,job_type='channel_initial_long_inventory',phase='initial_stats',
        status='running',lease_token=TOKEN,lease_channel_epoch=17,accepted_epoch=17,error_count=0,max_target_attempts=3)
    value.update(changes);return {'value':value}


def settle(db,**changes):
    params=dict(job_id=ID,lease_token=TOKEN,reserved_units=3,observed_quota_units=2,status='completed',summary={'stats':'kept'})
    params.update(changes);return completion.settle_initial_inventory_stats(db,**params)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(completion,'assert_channel_scope');self.fence=self.patch.start();self.addCleanup(self.patch.stop)

    def test_success_casts_then_locks_then_native_counter_then_update_and_fence(self):
        updated=job(status='completed',lease_token=None,lease_channel_epoch=None)
        db=Database(arguments(),job(),dict(error_count=0,as_of=NOW),updated)
        self.assertEqual(settle(db),updated['value'])
        self.assertEqual([call[1] for call in db.calls],[completion._STATS_INPUT,completion._JOB,completion._COUNTER,completion._SETTLE])
        self.assertEqual(db.calls[2][2],(0,0));self.assertEqual(db.calls[3][2]['next_status'],'completed')
        self.assertIsNone(db.calls[3][2]['error_code']);self.assertIsNone(db.calls[3][2]['error_summary'])
        self.fence.assert_called_once_with(db,CHANNEL,17);self.assertEqual(db.events,['commit'])
        operation=db.operations[0];self.assertEqual((operation.statement_timeout_ms,operation.lock_timeout_ms,operation.total_timeout_ms),(60000,0,60000))

    def test_all_success_statuses_and_null_unknown_status_keep_exact_policy(self):
        for status,increment,next_status in [('completed',0,'completed'),('skipped_no_recent_videos',0,'completed'),
                ('skipped_too_many_recent_videos',0,'completed'),(None,1,'queued'),('COMPLETED',1,'queued'),('error',1,'queued')]:
            with self.subTest(status=status):
                db=Database(arguments(status=status),job(),dict(error_count=increment,as_of=NOW),job(status=next_status))
                settle(db,status=status)
                self.assertEqual(db.calls[2][2],(0,increment));self.assertEqual(db.calls[-1][2]['next_status'],next_status)

    def test_quota_defer_keeps_error_count_and_uses_existing_pacific_reset_only_when_queued(self):
        for count,expected in ((2,'queued'),(3,'error')):
            db=Database(arguments(status='quota_deferred'),job(error_count=count),dict(error_count=count,as_of=NOW),job())
            with patch.object(completion,'youtube_quota_reset_at',wraps=completion.youtube_quota_reset_at) as reset:
                settle(db,status='quota_deferred')
            params=db.calls[-1][2];self.assertEqual(db.calls[2][2],(count,0));self.assertEqual(params['next_status'],expected)
            self.assertEqual(params['error_code'],'initial_stats_quota_exhausted')
            if expected=='queued':
                reset.assert_called_once_with(NOW);self.assertEqual(params['reset_at'],datetime(2026,3,9,7,tzinfo=timezone.utc))
            else:reset.assert_not_called();self.assertIsNone(params['reset_at'])

    def test_rate_defers_one_minute_without_daily_reset_or_attempt(self):
        db=Database(arguments(status='rate_limited'),job(error_count=3),
            dict(error_count=3,as_of=NOW),job(status='queued'))
        with patch.object(completion,'youtube_quota_reset_at') as reset:
            settle(db,status='rate_limited')
        reset.assert_not_called()
        self.assertEqual(db.calls[2][2],(3,0))
        params=db.calls[-1][2]
        self.assertEqual((params['next_status'],params['error_code']),('queued','provider_rate_limited'))
        self.assertTrue(params['rate_limited'])
        self.assertFalse(params['quota_deferred'])
        self.assertIn("THEN now()+interval '1 minute'",completion._SETTLE)

    def test_failure_at_attempt_cap_retains_exhausted_copy(self):
        db=Database(arguments(status='failed'),job(error_count=2),dict(error_count=3,as_of=NOW),job())
        settle(db,status='failed');params=db.calls[-1][2]
        self.assertEqual(params['next_status'],'error');self.assertEqual(params['error_summary'],'Initial stats exhausted its bounded retry policy.')

    def test_native_argument_failure_precedes_lock_and_invalid_domain(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:settle(db,job_id='bad',reserved_units=-1)
        self.assertIs(caught.exception,error);self.assertEqual(len(db.calls),1);self.assertEqual(db.events,['rollback'])

    def test_lease_validation_precedes_quota_validation_and_accepts_null_token_equality(self):
        db=Database(arguments(reserved_units=None),None)
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):settle(db,reserved_units=None)
        for key in ('reserved_units','observed_quota_units'):
            for value in (None,-1):
                db=Database(arguments(**{key:value}),job())
                with self.assertRaisesRegex(DatabaseError,'operator_job_initial_stats_settlement_invalid'):settle(db,**{key:value})
                self.assertEqual(len(db.calls),2)
        db=Database(arguments(lease_token=None),job(lease_token=None),dict(error_count=0,as_of=NOW),job())
        settle(db,lease_token=None);self.assertEqual(db.events,['commit'])

    def test_all_lease_identity_predicates_fail_before_counter(self):
        for field,value in [('job_type','channel_current_stats_backfill'),('phase','verify'),('status','queued'),('lease_token',ID)]:
            db=Database(arguments(),job(**{field:value}))
            with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):settle(db)
            self.assertEqual(len(db.calls),2)

    def test_counter_overflow_and_update_errors_precede_late_lease_generation_check(self):
        for at in ('counter','update'):
            error=DatabaseServerError('database_operation_failed',code='22003')
            answers=[arguments(),job(lease_channel_epoch=None),error] if at=='counter' else [arguments(),job(lease_channel_epoch=None),dict(error_count=0,as_of=NOW),error]
            db=Database(*answers)
            with self.assertRaises(DatabaseError) as caught:settle(db)
            self.assertIs(caught.exception,error);self.assertEqual(db.events,['rollback'])
        self.fence.assert_not_called()

    def test_generation_mismatch_is_late_and_rolls_back_borrowed_transaction(self):
        db=Database(arguments(),job(lease_channel_epoch=18),dict(error_count=0,as_of=NOW),job())
        with self.assertRaisesRegex(DatabaseError,'operator_job_lease_lost'):settle(None,transaction=db)
        self.assertEqual(len(db.calls),4);self.assertEqual(db.events,['borrow','poison']);self.fence.assert_not_called()

    def test_final_scope_failure_rolls_back_without_replay(self):
        db=Database(arguments(),job(),dict(error_count=0,as_of=NOW),job())
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaises(DatabaseError):settle(db)
        self.assertEqual(db.events,['rollback']);self.assertEqual(len(db.operations),1)

    def test_jsonb_shape_is_left_native_including_explicit_json_null(self):
        for summary in (None,{},['array'],7,'text',Jsonb(None)):
            db=Database(arguments(),job(),dict(error_count=0,as_of=NOW),job());settle(db,summary=summary)
            value=db.calls[0][2][-1];self.assertIs(db.calls[-1][2]['summary'],value)
            if summary is None:self.assertIsNone(value)
            elif isinstance(summary,Jsonb):self.assertIs(value,summary)
            else:self.assertEqual(value.obj,summary)

    def test_cas_native_casts_then_domain_validation_before_lock(self):
        for epoch in (None,0,-1):
            db=Database(dict(job_id=ID,channel_id=CHANNEL,error_count=0,expected_epoch=epoch))
            with self.assertRaisesRegex(DatabaseError,'channel_collection_scope_invalid'):
                completion.mark_completed_initial_hydration_failed(db,ID,CHANNEL,0,epoch)
            self.assertEqual(len(db.calls),1)
        error=DatabaseServerError('database_operation_failed',code='22003');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:completion.mark_completed_initial_hydration_failed(db,ID,None,None,2**63)
        self.assertIs(caught.exception,error)

    def test_cas_changes_only_status_then_fences_and_returns_one(self):
        args=dict(job_id=ID,channel_id=CHANNEL,error_count=2,expected_epoch=17)
        db=Database(args,job(status='completed',phase='verify',error_count=2),1)
        self.assertEqual(completion.mark_completed_initial_hydration_failed(db,ID,CHANNEL,2,17),1)
        self.assertEqual(db.calls[-1],('execute',completion._MARK_FAILED,(ID,)))
        self.fence.assert_called_once_with(db,CHANNEL,17);self.assertEqual(db.events,['commit'])

    def test_cas_every_mismatch_is_zero_with_no_mutation_or_fence(self):
        args=dict(job_id=ID,channel_id=CHANNEL,error_count=2,expected_epoch=17)
        values=[None]+[job(status='completed',phase='verify',error_count=2,**change) for change in
            ({'job_type':'channel_current_stats_backfill'},{'channel_id':'other'},{'accepted_epoch':18})]
        values.extend([job(status='running',phase='verify',error_count=2),job(status='completed',phase='initial_stats',error_count=2),job(status='completed',phase='verify',error_count=1)])
        for held in values:
            db=Database(args,held);self.assertEqual(completion.mark_completed_initial_hydration_failed(db,ID,CHANNEL,2,17),0)
            self.assertEqual(len(db.calls),2);self.assertEqual(db.events,['commit'])
        self.fence.assert_not_called()

    def test_unknown_commit_is_propagated_once_for_both_writers(self):
        class Lost(Database):
            @contextmanager
            def transaction(self,operation):
                with super().transaction(operation) as tx:yield tx
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
        for action in ('stats','cas'):
            db=Lost(arguments(),job(),dict(error_count=0,as_of=NOW),job()) if action=='stats' else Lost(
                dict(job_id=ID,channel_id=CHANNEL,error_count=0,expected_epoch=17),job(status='completed',phase='verify'),1)
            with self.assertRaises(DatabaseError) as caught:
                settle(db) if action=='stats' else completion.mark_completed_initial_hydration_failed(db,ID,CHANNEL,0,17)
            self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(len(db.operations),1);self.assertEqual(db.events,['commit'])

    def test_native_statements_are_parameterized_and_no_retained_rpc_dependency(self):
        values={**arguments(),'summary':Jsonb({'x':1}),'next_status':'queued','error_count':1,'quota_deferred':False,'rate_limited':False,
                'reset_at':None,'error_code':'initial_stats_failed','error_summary':'retry'}
        for sql,params in ((completion._STATS_INPUT,(ID,TOKEN,3,2,None,Jsonb([]))),
            (completion._CAS_INPUT,(ID,CHANNEL,1,17)),(completion._JOB,(ID,)),(completion._COUNTER,(0,1))):
            _reviewed_statement(sql,read=True,max_rows=1);PostgresQuery(Transformer()).convert(sql,params)
        _reviewed_statement(completion._SETTLE,read=True,max_rows=1,returning=True)
        PostgresQuery(Transformer()).convert(completion._SETTLE,values)
        _reviewed_statement(completion._MARK_FAILED,read=False,max_rows=0)
        self.assertIn('job.quota_units_reserved-%(reserved_units)s::integer+%(observed_quota_units)s::integer',completion._SETTLE)
        source=(ROOT/'application/db/jobs/operator_job_completion.py').read_text()
        self.assertNotIn('database.call(',source);self.assertNotIn('operator_private.',source)


if __name__=='__main__':unittest.main()
