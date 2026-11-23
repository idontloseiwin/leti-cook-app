"""Focused offline protocol tests for prepared operator-job claims/release."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.jobs import operator_job_lifecycle as lifecycle
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement
from channel_workflow_fixtures import stage_answers,stage_sql

ID='f8100000-0000-4000-8000-000000000001';CHANNEL='UC0000000000000000000001'

def args(**changes):return dict(channel_id=CHANNEL,worker='worker',lease_seconds=1200,**changes)
def candidate():return dict(channel_id=CHANNEL,control_epoch=17,is_hidden=False,collection_mode='active',job_id=ID)
def job(**changes):
    result=dict(id=ID,channel_id=CHANNEL,accepted_epoch=17,job_type='channel_initial_long_inventory',phase='inventory',
                initial_video_types=['long'],inventory_video_type='long');result.update(changes);return {'value':result}

class Database:
    def __init__(self,*answers):self.answers=deque(answers);self.calls=[];self.events=[];self.operations=[]
    @contextmanager
    def transaction(self,operation):
        self.operations.append(operation)
        try:yield self
        except BaseException:self.events.append('rollback');raise
        else:self.events.append('commit')
    @contextmanager
    def borrow(self):
        self.events.append('borrow')
        try:yield self
        except BaseException:self.events.append('poison');raise
    def answer(self,kind,sql,parameters):
        self.calls.append((kind,sql,parameters));result=self.answers.popleft()
        if isinstance(result,BaseException):raise result
        return result
    def fetch_one(self,sql,parameters=()):
        result=self.answer('one',sql,parameters)
        if sql==lifecycle._CHANNEL and result is not None:
            result=dict(result);result['policy_facts']=native_policy_facts(result.pop('collection_mode'))
        return result
    def returning_one(self,sql,parameters=()):return self.answer('returning',sql,parameters)
    def execute(self,sql,parameters=()):return self.answer('execute',sql,parameters)

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(lifecycle,'assert_channel_scope');self.fence=self.patch.start();self.addCleanup(self.patch.stop)
    def test_claim_locks_channel_then_exact_job_and_fences_after_inventory_write(self):
        held=job();db=Database(args(),candidate(),held,held,*stage_answers('claim_running'))
        self.assertEqual(lifecycle.claim_operator_job(db,' worker '),held['value'])
        self.assertEqual([item[1] for item in db.calls],[lifecycle._CLAIM_INPUT,lifecycle._CANDIDATE,lifecycle._JOB,lifecycle._LEASE,*stage_sql('claim_running')])
        self.assertEqual(db.calls[0][2],(None,' worker ',1200));self.fence.assert_called_once_with(db,CHANNEL,17)
        self.assertEqual(db.events,['borrow','commit']);self.assertEqual(db.operations[0].name,'claim_operator_job')
    def test_initial_claim_preserves_addressed_order_and_trimmed_channel_binding(self):
        held=job();db=Database(args(),candidate(),held,held,*stage_answers('claim_running'))
        lifecycle.claim_channel_initial_hydration_job(db,' '+CHANNEL+' ','worker')
        self.assertEqual(db.calls[0][2],(' '+CHANNEL+' ','worker',1200))
        self.assertEqual([row[1] for row in db.calls[1:3]],[lifecycle._CHANNEL,lifecycle._INITIAL_JOB])
        self.assertIn('btrim(%s::text)',lifecycle._CHANNEL)
    def test_verify_forces_sixty_seconds_and_does_not_write_inventory_status(self):
        held=job(phase='verify');db=Database(args(),candidate(),held,held)
        lifecycle.claim_operator_job(db,'worker',3600)
        self.assertEqual(db.calls[-1][2]['lease_seconds'],60);self.assertEqual(len(db.calls),4)
    def test_three_type_short_phase_does_not_restart_long_status(self):
        held=job(initial_video_types=['long','short','stream'],inventory_video_type='short');db=Database(args(),candidate(),held,held)
        lifecycle.claim_operator_job(db,'worker');self.assertEqual(len(db.calls),4);self.fence.assert_called_once()
    def test_stats_job_never_inherits_verify_sixty_second_special_case(self):
        held=job(job_type='channel_current_stats_backfill',phase='video_targets');db=Database(args(),candidate(),held,held)
        lifecycle.claim_operator_job(db,'worker');self.assertEqual(db.calls[-1][2]['lease_seconds'],1200)
    def test_native_cast_errors_precede_domain_checks(self):
        error=DatabaseServerError('database_operation_failed',code='22P02');db=Database(error)
        with self.assertRaises(DatabaseError) as caught:lifecycle.claim_channel_initial_hydration_job(db,None,None,'bad')
        self.assertIs(caught.exception,error);self.assertEqual(len(db.calls),1)
    def test_channel_worker_lease_error_order_and_explicit_null(self):
        for values,expected in ((dict(channel_id=None,worker=None,lease_seconds=None),'operator_job_channel_required'),
                (dict(channel_id=CHANNEL,worker='',lease_seconds=None),'operator_job_worker_required'),
                (dict(channel_id=CHANNEL,worker='worker',lease_seconds=None),'operator_job_lease_seconds_invalid'),
                (dict(channel_id=CHANNEL,worker='worker',lease_seconds=59),'operator_job_lease_seconds_invalid')):
            db=Database(values)
            with self.assertRaisesRegex(DatabaseServerError,expected):lifecycle.claim_channel_initial_hydration_job(db,CHANNEL,'worker',None)
            self.assertEqual(len(db.calls),1);self.assertEqual(db.events,['rollback'])
    def test_absent_or_skipped_locked_candidate_commits_null_without_fence(self):
        for answers in ((args(),None),(args(),candidate(),None)):
            db=Database(*answers);self.assertIsNone(lifecycle.claim_operator_job(db,'worker'));self.assertEqual(db.events,['commit'])
        self.fence.assert_not_called()
    def test_changed_epoch_after_channel_lock_does_not_claim_or_retry_next_job(self):
        db=Database(args(),candidate(),job(accepted_epoch=18))
        self.assertIsNone(lifecycle.claim_operator_job(db,'worker'));self.assertEqual(len(db.calls),3);self.fence.assert_not_called()
    def test_final_scope_loss_rolls_back_and_borrowed_scope_is_poisoned(self):
        held=job();db=Database(args(),candidate(),held,held,*stage_answers('claim_running'))
        self.fence.side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')
        with self.assertRaises(DatabaseError):lifecycle.claim_operator_job(None,'worker',transaction=db)
        self.assertEqual(db.events,['borrow','borrow','poison']);self.assertFalse(db.operations)
    def test_release_is_native_token_guarded_update_without_an_extra_control_lock(self):
        values=dict(job_id=ID,lease_token=None,error_code=' ');db=Database(values,job(status='suspended'))
        self.assertEqual(lifecycle.release_operator_job(db,ID,None,' ')['status'],'suspended')
        self.assertEqual([c[1] for c in db.calls],[lifecycle._RELEASE_INPUT,lifecycle._RELEASE]);self.fence.assert_not_called()
        self.assertIn('IS NOT DISTINCT FROM %(lease_token)s::uuid',lifecycle._RELEASE)
        self.assertIn("clock_timestamp()+interval '5 minutes'",lifecycle._RELEASE)
        self.assertEqual(lifecycle._RELEASE.count("job.requested_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()"),3)
    def test_release_lost_lease_raises_with_no_replay(self):
        db=Database(dict(job_id=ID,lease_token=None,error_code=None),None)
        with self.assertRaisesRegex(DatabaseServerError,'operator_job_lease_lost'):lifecycle.release_operator_job(db,ID,None,None)
        self.assertEqual(len(db.calls),2);self.assertEqual(db.events,['rollback'])
    def test_unknown_commit_ack_is_propagated_once(self):
        class Lost(Database):
            @contextmanager
            def transaction(self,operation):
                with super().transaction(operation) as tx:yield tx
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
        db=Lost(dict(job_id=ID,lease_token=ID,error_code='error'),job(status='queued'))
        with self.assertRaises(DatabaseError) as caught:lifecycle.release_operator_job(db,ID,ID,'error')
        self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(len(db.operations),1);self.assertEqual(db.events,['commit'])
    def test_all_statements_parameterized_and_bounded_no_retained_function_calls(self):
        params=dict(job_id=ID,channel_id=CHANNEL,control_epoch=17,worker='worker',lease_seconds=300,error_code=None,lease_token=ID,error_summary=lifecycle._RELEASE_SUMMARY)
        for sql,values in ((lifecycle._CLAIM_INPUT,(CHANNEL,'worker',1200)),(lifecycle._CANDIDATE,()),
                (lifecycle._CHANNEL,(CHANNEL,)),(lifecycle._JOB,params),(lifecycle._INITIAL_JOB,params),
                (lifecycle._RELEASE_INPUT,(ID,None,None))):
            _reviewed_statement(sql,read=True,max_rows=1);PostgresQuery(Transformer()).convert(sql,values)
        for sql in (lifecycle._LEASE,lifecycle._RELEASE):
            _reviewed_statement(sql,read=True,max_rows=1,returning=True);PostgresQuery(Transformer()).convert(sql,params)
        _reviewed_statement(lifecycle._START_INVENTORY,read=False,max_rows=0);PostgresQuery(Transformer()).convert(lifecycle._START_INVENTORY,(CHANNEL,))
        source=(ROOT/'application/db/jobs/operator_job_lifecycle.py').read_text(encoding='utf-8')
        self.assertNotIn('database.call(',source);self.assertNotIn('operator_private.',source)


if __name__=='__main__':unittest.main()
