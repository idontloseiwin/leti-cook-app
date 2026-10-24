"""Scheduled claim orchestration against a fixed recording transaction."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from contextlib import contextmanager
from collections import deque
from pathlib import Path
import sys,unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_scheduled_claims as subject
from db.base.postgres import DatabaseError,DatabaseServerError
from collection_schedule import CollectionSchedules
RUN='fa700000-0000-4000-8000-000000000001'

class Tx:
    def __init__(self):
        self.events=[];self.ids=[];self.rows=[];self.enabled=True;self.pause={};self.eligible={};self.mode={};self.failed=False;self.cursor=False;self.update_error=None
        self.handoff=set();self.reverse_returned=False;self.prefixes=[];self.fetched=[]
        self.recovery={};self.cadence_snapshot=CollectionSchedules().cadence_snapshot()
    @contextmanager
    def borrow(self):
        try:yield self
        except BaseException:self.failed=True;raise
    @contextmanager
    def video_scheduled_candidates(self,ids,run,*,video_type):
        self.events.append(('cursor_open',ids,video_type));self.cursor=True
        class Prefixes:
            def __init__(inner):inner.rows=deque(self.rows);inner.buffer=deque();inner.size=10
            def locked_prefix(inner,limit):
                if not inner.buffer:
                    fetched=[inner.rows.popleft() for _ in range(min(inner.size,len(inner.rows)))]
                    self.fetched.extend(row['video_id'] for row in fetched)
                    inner.buffer.extend(fetched);inner.size=50
                rows=[inner.buffer.popleft() for _ in range(min(limit,len(inner.buffer)))]
                self.prefixes.append([row['video_id'] for row in rows])
                return rows
        try:yield Prefixes()
        finally:self.cursor=False;self.events.append(('cursor_close',))
    def fetch_one(self,sql,params=()):
        self.events.append((sql,params))
        if sql==subject._ARGS:return dict(video_type=params[0],limit=params[1],worker=params[2].strip(' ') if params[2] is not None else None,run_id=params[3],lease_seconds=params[4])
        if sql==subject._RUNTIME:return {'enabled':self.enabled}
        if sql in subject._WINDOW.values():return {'ids':self.ids}
        raise AssertionError(sql)
    def fetch_all(self,sql,params=(),*,max_rows):
        self.events.append((sql,params))
        assert len(params['video_ids'])<=max_rows<=50
        rows=[]
        for identity,channel,kind in zip(params['video_ids'],params['channel_ids'],params['video_types']):
            if sql==subject._LONG_FACTS:
                row=dict(policy_facts=native_pause_facts(self.pause.get(channel)),
                         collection_mode=self.mode.get(identity,'counters'),
                         initial_handoff=identity in self.handoff,eligible=self.eligible.get(identity,True))
            elif sql==subject._RECOVERY_FACTS:row=dict(eligible=self.recovery.get(identity,self.eligible.get(identity,True)))
            elif sql==subject._TYPED_FACTS:
                row=dict(collection_mode=self.mode.get(identity,'metadata'),identity_facts=dict(typed_type=True,channel_runtime=True,playlist_enabled=self.eligible.get(identity,True),
                    inventory_ready=True,scan_complete=True,foreign_present=False,manual_valid=False,automatic_valid=False,
                    exact_present=True,conflicting_present_type=False))
            else:raise AssertionError(sql)
            rows.append(dict(video_id=identity,**row))
        return list(reversed(rows)) if self.reverse_returned else rows
    def returning_all(self,sql,params=(),*,max_rows):
        self.events.append((sql,params))
        if self.update_error:raise self.update_error
        assert len(params['video_ids'])<=max_rows<=50
        if sql in (subject._PAUSE,subject._PROMOTE_METADATA):return [dict(video_id=identity) for identity in params['video_ids']]
        assert sql==subject._CLAIM
        rows=[]
        for identity in params['video_ids']:
            state=next(row for row in self.rows if row['video_id']==identity)
            rows.append(dict(value={**state,'lease_token':'token','lease_expires_at':'native_expiry','next_due_at':'native_due'},
                             epoch=7,collection_mode=self.mode.get(identity,'counters' if state['video_type']=='long' else 'metadata')))
        return list(reversed(rows)) if self.reverse_returned else rows

class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tx=Tx();self.fences=[]
        def fence(tx,scopes):self.assertFalse(tx.cursor);self.fences.append(scopes);tx.events.append(('fence',scopes))
        self.patch=patch.object(subject,'assert_channel_scopes',side_effect=fence);self.patch.start();self.addCleanup(self.patch.stop)
    def rows(self,*types):
        self.tx.rows=[dict(video_id=str(i),channel_id='channel',video_type=kind) for i,kind in enumerate(types)]
        self.tx.ids=[row['video_id'] for row in self.tx.rows]
    def invoke(self,typed=None,table=False,**changes):
        args=dict(limit=50,worker_id=' worker ',run_id=RUN,lease_seconds=300,transaction=self.tx);args.update(changes)
        if typed is None:return subject.claim_due_video_collection_batch(None,**args)
        method=subject.claim_due_typed_video_collection if table else subject.claim_due_typed_video_collection_batch
        return method(None,typed,**args)
    def test_zero_limit_and_disabled_type_skip_window_cursor_and_fence(self):
        for typed in (None,'short'):
            result=self.invoke(typed,limit=0);self.assertEqual(result,dict(rows=[],inspected_count=0,paused_count=0,scan_exhausted=True))
        self.tx.enabled=False;self.invoke('stream')
        self.assertEqual(self.fences,[]);self.assertFalse(any(e[0]=='cursor_open' for e in self.tx.events))
    def test_invalid_bounds_fail_before_runtime_or_cursor(self):
        for kwargs in ({'limit':None},{'limit':-1},{'limit':51},{'worker_id':' '},{'run_id':None},{'lease_seconds':29},{'lease_seconds':3601}):
            self.tx=Tx()
            with self.assertRaises(DatabaseServerError) as error:self.invoke(**kwargs)
            self.assertEqual(error.exception.message,'common_video_collection_claim_input_invalid');self.assertEqual(len(self.tx.events),1);self.assertTrue(self.tx.failed)
        with self.assertRaises(DatabaseServerError) as error:self.invoke('long')
        self.assertEqual(error.exception.message,'typed_video_collection_claim_input_invalid')
    def test_mixed_rows_preserve_order_modes_and_deduplicate_final_scopes(self):
        self.rows('long','short','stream');self.tx.mode={'0':'hydrate','1':'metadata','2':'stream_lifecycle'}
        result=self.invoke()
        self.assertEqual([row['video_id'] for row in result['rows']],['0','1','2'])
        self.assertEqual([row['collection_mode'] for row in result['rows']],['hydrate','metadata','stream_lifecycle'])
        self.assertTrue(all('priority' not in row for row in result['rows']))
        self.assertEqual(self.fences,[[dict(channel_id='channel',control_epoch=7)]])
        self.assertEqual(self.tx.events[-2][0],'cursor_close');self.assertEqual(self.tx.events[-1][0],'fence')
    def test_untyped_identity_uses_same_owner_without_guessing_a_format(self):
        self.rows(None,'long')
        result=self.invoke()
        self.assertEqual([(r['video_type'],r['collection_mode']) for r in result['rows']],
                         [('unknown','identity_refresh'),('long','counters')])
        self.assertEqual(self.fences,[[dict(channel_id='channel',control_epoch=7)]])
    def test_lost_type_proof_uses_general_identity_without_changing_retained_type(self):
        self.rows('long','short','stream')
        self.tx.eligible=dict.fromkeys(self.tx.ids,False)
        self.tx.recovery=dict.fromkeys(self.tx.ids,True)
        result=self.invoke()
        self.assertEqual([(row['video_type'],row['collection_mode']) for row in result['rows']],
            [('long','identity_refresh'),('short','identity_refresh'),('stream','identity_refresh')])
        self.assertEqual(result['paused_count'],0)
        self.assertFalse(any(event[0]==subject._PAUSE for event in self.tx.events))
    def test_long_resolution_gate_is_limited_to_existing_exact_conflict_rule(self):
        # Native PostgreSQL proves membership states. This guards the SQL boundary:
        # ordinary Long must not acquire a new all-type resolution requirement.
        from db.videos.video_queue_initialization import LONG_SHORT_CONFLICT_SQL
        conflict=LONG_SHORT_CONFLICT_SQL.replace('%(video_id)s::text','inventory.video_id').replace(
            '%(channel_id)s::text','inventory.channel_id')
        expected="AND (NOT ("+conflict+")\n OR EXISTS(SELECT 1 FROM public.video_inventory_type_resolution proof"
        self.assertIn(expected,subject._LONG_FACTS)
        self.assertEqual(subject._LONG_FACTS.count(conflict),1)
        self.assertIn("proof.resolution_status='resolved' AND proof.canonical_video_type='long'",subject._LONG_FACTS)
    def test_ordinary_long_admission_preserves_scheduled_mode_without_recovery_probe(self):
        for mode in ('counters','metadata'):
            with self.subTest(mode=mode):
                self.tx=Tx();self.rows('long')
                self.tx.mode['0']=mode;self.tx.eligible['0']=True;self.tx.recovery['0']=False
                result=self.invoke()
                self.assertEqual([row['collection_mode'] for row in result['rows']],[mode])
                self.assertFalse(any(event[0] in (subject._RECOVERY_FACTS,subject._PAUSE,
                    subject._PROMOTE_METADATA) for event in self.tx.events))
    def test_counter_claims_are_valid_for_each_of_the_three_types(self):
        self.rows('long','short','stream')
        self.tx.mode=dict.fromkeys(self.tx.ids,'counters')
        result=self.invoke()
        self.assertEqual([row['collection_mode'] for row in result['rows']],['counters']*3)
        self.assertEqual([row['video_type'] for row in result['rows']],['long','short','stream'])
    def test_expired_missing_episode_stops_locally_before_any_claim(self):
        at=datetime(2026,10,15,12,tzinfo=timezone.utc)
        for kind in ('long','short','stream',None):
            with self.subTest(kind=kind):
                self.tx=Tx();self.rows(kind)
                self.tx.rows[0].update(availability_first_missing_at=at-timedelta(days=29),
                    availability_schedule_type=kind,availability_resume_observed_at=None,observed_at=at)
                result=self.invoke()
                self.assertEqual((result['rows'],result['paused_count']),([],1))
                pauses=[event[1] for event in self.tx.events if event[0]==subject._PAUSE]
                self.assertEqual(pauses[0]['pause_reasons'],['availability_retry_expired'])
                self.assertFalse(any(event[0]==subject._CLAIM for event in self.tx.events))
    def test_explicit_reappearance_can_check_an_expired_episode_once(self):
        at=datetime(2026,10,15,12,tzinfo=timezone.utc)
        self.rows('short');self.tx.mode['0']='metadata'
        self.tx.rows[0].update(availability_first_missing_at=at-timedelta(days=35),
            availability_schedule_type='short',availability_resume_observed_at=at,observed_at=at)
        result=self.invoke()
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['collection_mode'],'metadata')
        self.assertEqual(result['paused_count'],0)
    def test_future_missing_episode_is_rejected_without_a_provider_claim(self):
        at=datetime(2026,10,15,12,tzinfo=timezone.utc)
        self.rows('long')
        self.tx.rows[0].update(availability_first_missing_at=at+timedelta(seconds=1),
            availability_schedule_type='long',availability_resume_observed_at=None,observed_at=at)
        with self.assertRaises(DatabaseServerError) as raised:self.invoke()
        self.assertEqual(raised.exception.message,'video_unavailable_episode_clock_invalid')
        self.assertFalse(any(event[0]==subject._CLAIM for event in self.tx.events))
    def test_newer_return_signal_survives_hold_after_old_episode_was_closed(self):
        at=datetime(2026,10,15,12,tzinfo=timezone.utc)
        self.rows('short')
        self.tx.rows[0].update(availability_first_missing_at=None,availability_schedule_type=None,
            availability_resume_observed_at=at,observed_at=at,next_due_at=at,paused_reason=None)
        before=dict(self.tx.rows[0])
        self.tx.eligible['0']=False;self.tx.recovery['0']=False
        held=self.invoke()
        self.assertEqual((held['rows'],held['paused_count']),([],0))
        self.assertEqual(self.tx.rows[0],before)
        self.assertFalse(any(event[0] in (subject._CLAIM,subject._PAUSE) for event in self.tx.events))
        self.tx.recovery['0']=True
        resumed=self.invoke()
        self.assertEqual(len(resumed['rows']),1)
        self.assertEqual(resumed['rows'][0]['collection_mode'],'identity_refresh')
    def test_episode_hold_preserves_deadline_until_channel_is_allowed_again(self):
        at=datetime(2026,10,15,12,tzinfo=timezone.utc)
        for kind in ('long','short','stream'):
            with self.subTest(kind=kind):
                self.tx=Tx();self.rows(kind)
                self.tx.rows[0].update(availability_first_missing_at=at-timedelta(days=2),
                    availability_schedule_type=kind,observed_at=at,next_due_at=at,
                    paused_reason=None)
                before=dict(self.tx.rows[0])
                self.tx.pause['channel']='channel_hidden'
                self.tx.eligible['0']=False;self.tx.recovery['0']=False
                held=self.invoke()
                self.assertEqual((held['rows'],held['paused_count']),([],0))
                self.assertEqual(self.tx.rows[0],before)
                self.assertFalse(any(event[0] in (subject._CLAIM,subject._PAUSE,
                    subject._PROMOTE_METADATA) for event in self.tx.events))
                self.tx.pause.clear();self.tx.recovery['0']=True
                resumed=self.invoke()
                self.assertEqual(len(resumed['rows']),1)
                self.assertEqual(resumed['rows'][0]['collection_mode'],'identity_refresh')
    def test_ineligible_identity_is_not_leased_or_permanently_paused(self):
        self.rows(None);self.tx.eligible['0']=False
        result=self.invoke()
        self.assertEqual((result['rows'],result['paused_count']),([],0))
        self.assertFalse(any(e[0] in (subject._CLAIM,subject._PAUSE) for e in self.tx.events))
    def test_inspected_count_precedes_filtered_cursor_and_claim_limit(self):
        self.rows('long','long','long');self.tx.ids=[str(i) for i in range(1000)]
        result=self.invoke(limit=1)
        self.assertEqual((result['inspected_count'],len(result['rows']),result['scan_exhausted']),(1000,1,False))
        self.assertEqual(sum(e[0]==subject._LONG_FACTS for e in self.tx.events),1)
    def test_full_raw_window_without_claims_is_not_exhausted(self):
        self.tx.ids=[str(i) for i in range(1000)]
        result=self.invoke();self.assertFalse(result['scan_exhausted']);self.assertEqual(result['rows'],[]);self.assertEqual(self.fences,[[]])
    def test_reaching_claim_limit_forces_nonexhausted_even_on_short_window(self):
        self.rows('long');result=self.invoke(limit=1)
        self.assertEqual(result['inspected_count'],1);self.assertFalse(result['scan_exhausted'])
    def test_invalid_long_inventory_does_not_override_channel_hold(self):
        self.rows('long');self.tx.pause['channel']='channel_hidden';self.tx.eligible['0']=False
        result=self.invoke();self.assertEqual((result['paused_count'],result['rows']),(1,[]))
        self.assertEqual([event[1]['pause_reasons'] for event in self.tx.events if event[0]==subject._PAUSE],[['channel_hidden']])
        self.assertEqual(self.fences,[[]])
    def test_typed_invalid_rows_pause_then_valid_rows_can_fill_limit(self):
        self.rows('short','short');self.tx.eligible['0']=False
        result=self.invoke('short',limit=1)
        self.assertEqual((result['paused_count'],result['rows'][0]['video_id']),(1,'1'))
        self.assertEqual(result['rows'][0]['priority'],0);self.assertFalse(result['scan_exhausted'])
    def test_typed_wrapper_returns_only_rows_through_same_protocol(self):
        self.rows('stream');self.tx.mode['0']='stream_lifecycle'
        result=self.invoke('stream',table=True)
        self.assertIsInstance(result,list);self.assertEqual(result[0]['collection_mode'],'stream_lifecycle');self.assertEqual(result[0]['priority'],0)
    def test_nullable_native_fact_is_rejected_by_original_exists_admission(self):
        # The original identity function returns EXISTS, never SQL NULL. A NULL
        # runtime_enabled fact is excluded by its view's WHERE predicate.
        self.rows('short');self.tx.eligible['0']=None
        result=self.invoke('short')
        self.assertEqual(result['rows'],[]);self.assertEqual(result['paused_count'],1)
        self.assertEqual([event[1]['pause_reasons'] for event in self.tx.events if event[0]==subject._PAUSE],[['typed_identity_not_eligible']])
    def test_claim_error_aborts_without_fence_or_retry(self):
        self.rows('long');self.tx.update_error=DatabaseServerError('database_operation_failed',code='22003')
        with self.assertRaises(DatabaseServerError):self.invoke()
        self.assertTrue(self.tx.failed);self.assertEqual(self.fences,[])
        self.assertEqual(sum(e[0]==subject._CLAIM for e in self.tx.events),1)
    def test_own_transaction_keeps_original_budget_and_unknown_ack(self):
        self.rows('long')
        class Database:
            attempts=0
            @contextmanager
            def transaction(inner,operation):
                inner.attempts+=1;inner.operation=operation
                yield self.tx
                raise DatabaseError('database_connection_failed',outcome_unknown=True)
        database=Database()
        with self.assertRaises(DatabaseError) as error:subject.claim_due_video_collection_batch(database,1,'worker',RUN)
        self.assertTrue(error.exception.outcome_unknown);self.assertEqual(database.attempts,1)
        self.assertEqual(database.operation.total_timeout_ms,8000)

if __name__=='__main__':unittest.main()
