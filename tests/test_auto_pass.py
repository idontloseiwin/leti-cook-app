"""Bounded common-pass identities/counters and fake candidate sweeps."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys,time,unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.common_video_dispatch import CommonBatchUnit
from collection.dispatch import Ack,Coordinator,Prepared,Work
from collection.provider_steps import ProviderStep
import collection_shutdown

RUN='fa700000-0000-4000-8000-000000000001'
def channel(n):return 'UC'+str(n).zfill(22)
def batch(n,inspected=None,exhausted=True):
    return dict(rows=[SimpleNamespace(video_id=str(i),channel_id=channel(1),video_type='long') for i in range(n)],
        inspected_count=n if inspected is None else inspected,paused_count=0,scan_exhausted=exhausted)


def candidate_state(*args, **kwargs):
    state = AutoPassState(*args, **kwargs)
    state.bind_candidate_cursor(CommonCandidateCursor())
    return state


class AutoPassTests(unittest.TestCase):
    def test_stable_uuid_and_integer_progress_identity_are_distinct(self):
        state=AutoPassState(101,run_id=RUN,sync_run_id=42)
        for i,size in enumerate((50,50,1)):
            args=state.begin_claim(str(i))
            self.assertEqual(args,dict(limit=size,run_id=RUN))
            state.on_claimed(str(i),batch(size))
            state.on_settled(str(i),{})
        self.assertEqual((state.remaining,state.claimed,state.sync_run_id),(0,101,42))
        self.assertIsNone(state.begin_claim('done'))

    def test_scan_cap_matches_current_formula_and_empty_claims_count(self):
        state=AutoPassState(100,run_id=RUN)
        self.assertEqual(state.scan_limit,2000)
        for key in ('first','second'):
            state.begin_claim(key)
            state.on_claimed(key,batch(0,1000,False))
        self.assertEqual((state.inspected,state.claimed,state.held),(2000,0,0))
        self.assertIsNone(state.begin_claim('third'))
        self.assertEqual(AutoPassState(1000000).scan_limit,2000000)

    def test_unknown_claim_reservation_stays_held_without_retry(self):
        state=AutoPassState(80,concurrency=2)
        state.begin_claim('a');state.claim_failed('a')
        self.assertEqual((state.held,state.remaining),(1,30))
        self.assertTrue(state.paused)
        self.assertIsNone(state.begin_claim('b'))
        with self.assertRaises(ValueError):state.begin_claim('a')

    def test_concurrent_callback_claim_is_forbidden_and_held_credits_bounded(self):
        state=AutoPassState(200,concurrency=1)
        state.begin_claim('a')
        with self.assertRaises(RuntimeError):state.begin_claim('b')
        state.on_claimed('a',batch(50))
        with self.assertRaises(RuntimeError):state.begin_claim('b')
        state.on_settled('a',{})
        self.assertEqual(state.begin_claim('b')['limit'],50)

    def test_uncertain_settlement_pauses_with_credit_retained(self):
        state=AutoPassState(100)
        state.begin_claim('a');state.on_claimed('a',batch(50))
        with self.assertRaises(RuntimeError):state.on_settled('a',{'settlement_unknown':True})
        self.assertEqual(state.held,1)
        self.assertTrue(state.paused)
        with self.assertRaises(RuntimeError):state.on_settled('a',{})

    def test_known_handoff_settlement_stops_new_work_without_refunding_claims(self):
        state=candidate_state(100)
        state.begin_claim('a');state.on_claimed('a',batch(50))
        state.on_settled('a',{'yield_requested':True})
        self.assertEqual((state.held,state.claimed),(0,50))
        self.assertIsNone(state.begin_claim('b'))
        self.assertIsNone(state.candidate_request(1))

    def test_bad_envelope_retains_unknown_credit(self):
        state=AutoPassState(1)
        state.begin_claim('a')
        with self.assertRaises(ValueError):state.on_claimed('a',batch(2))
        self.assertTrue(state.paused)
        self.assertEqual(state.held,1)

    def test_candidate_pages_round_robin_formats_and_empty_cursor_advances(self):
        state=candidate_state(1000,concurrency=2,run_id=RUN)
        for kind in ('long','short','stream','unknown','long'):
            request=state.candidate_request(4)
            self.assertEqual(request['video_type'],kind)
            expected=channel(4) if kind=='long' and request['after'] else None
            self.assertEqual(request['after'],expected)
            after=channel(8) if expected else channel(4)
            state.accept_candidate_page(dict(rows=[],next_after=after,inspected_count=4,scan_exhausted=False))
        self.assertEqual(state.inspected,0)
        self.assertEqual(state.candidate_inspected,20)

    def test_more_than_500_channels_and_late_small_channel_without_prefix_cap(self):
        state=candidate_state(30000,concurrency=8)
        population=[channel(i) for i in range(1,601)]
        seen=set();maximum=0;reads=0
        for operation in ('metadata','counters'):
            while True:
                request=state.candidate_request(16,operation=operation)
                if request is None:break
                reads+=1
                if reads==10:population.append(channel(777))
                physical=[x for x in population if x>(request['after'] or '')][:request['limit']]
                # The first40 channels model hidden/locked/no eligible work.
                rows=[dict(channel_id=x,video_type=request['video_type'],operation=operation,
                    ready_count=1,due_at=1) for x in physical if x>channel(40)]
                accepted=state.accept_candidate_page(dict(rows=rows,next_after=physical[-1] if physical else request['after'],
                    inspected_count=len(physical),scan_exhausted=len(physical)<request['limit']))
                maximum=max(maximum,len(accepted))
                seen.update(row['channel_id'] for row in accepted)
                # Only this operation's known claim advances its frontier.
                for scope in accepted:state.candidate_claimed(scope,operation=operation)
                self.assertLess(reads,1000)
        self.assertIn(channel(777),seen)
        self.assertEqual(len(seen),561)
        self.assertLessEqual(maximum,16)
        self.assertEqual(len(state._after),8)
        self.assertTrue(state._cursor().sweep_exhausted)
        self.assertEqual(state.held,0)

    def test_candidate_scan_budget_is_finite_but_does_not_charge_video_attempts(self):
        state=candidate_state(50,concurrency=50)
        for i in range(20):
            request=state.candidate_request(100)
            self.assertEqual(request['limit'],50)
            start=int((request['after'] or channel(0))[2:])
            state.accept_candidate_page(dict(rows=[],next_after=channel(start+50),inspected_count=50,scan_exhausted=False))
        self.assertIsNone(state.candidate_request(100))
        self.assertEqual((state.claimed,state.inspected,state.remaining),(0,0,50))

    def test_only_unknown_counter_type_stays_exhausted_through_reset_and_wrap(self):
        cursor=CommonCandidateCursor()
        empty={('counters','unknown')}
        self.assertEqual(cursor._exhausted,empty)
        cursor._exhausted=set(cursor._after)
        cursor._reset_sweep()
        self.assertEqual(cursor._exhausted,empty)
        state=AutoPassState(100);state.bind_candidate_cursor(cursor)
        for kind in ('long','short','stream'):
            request=state.candidate_request(1,operation='counters')
            self.assertEqual(request['video_type'],kind)
            state.accept_candidate_page(dict(rows=[],next_after=None,inspected_count=0,scan_exhausted=True))
        self.assertIsNone(state.candidate_request(1,operation='counters'))
        self.assertTrue(state.wrap_candidate_window('counters',scopes=[]))
        self.assertEqual(cursor._exhausted,empty)
        self.assertEqual(state.candidate_request(1,operation='counters')['video_type'],'long')

    def test_no_infinite_wrap_without_progress(self):
        state=candidate_state(100)
        for _ in range(4):
            request=state.seed_request(1)
            state.accept_seed_page(dict(rows=[],next_after=request['after'],inspected_count=0,scan_exhausted=True))
        self.assertIsNone(state.seed_request(1))

    def test_next_sweep_keeps_same_run_and_raw_budgets(self):
        state=candidate_state(100,run_id=RUN)
        for _ in range(4):
            request=state.seed_request(1)
            state.accept_seed_page(dict(rows=[],next_after=None,inspected_count=0,scan_exhausted=True))
        self.assertTrue(state._cursor().sweep_exhausted)
        state.begin_claim('a');state.on_claimed('a',batch(1));state.on_settled('a',{})
        self.assertEqual(state.seed_request(1)['run_id'],RUN)
        self.assertEqual((state.claimed,state.inspected),(1,1))

    def test_nonmonotonic_candidate_page_is_not_accepted(self):
        state=candidate_state(100)
        state.candidate_request(1)
        with self.assertRaises(ValueError):state.accept_candidate_page(dict(rows=[],next_after=None,inspected_count=1,scan_exhausted=False))
        with self.assertRaises(RuntimeError):state.candidate_request(1)

    def test_state_is_coordinator_thread_only(self):
        state=AutoPassState(100)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(RuntimeError):pool.submit(state.begin_claim,'a').result()

    def test_actual_coordinator_interleaves_operator_without_resetting_common_budget(self):
        state=AutoPassState(3,concurrency=2,run_id=RUN)
        calls=[]
        handoff=patch.object(collection_shutdown,'_requested',Event())
        handoff.start();self.addCleanup(handoff.stop)
        def steps(claims,**kwargs):
            response=yield ProviderStep(lambda ids,reason: calls.append(('fetch',ids)),tuple(r.video_id for r in claims),'test')
            response.unwrap()
            return {'updated':len(claims)}
        service=SimpleNamespace(common_claim_steps=steps,finish_common_claims=lambda claims,summary:summary)
        units=[]
        for key in ('a','b','c'):
            def claim(key=key):
                args=state.begin_claim(key);calls.append(('claim',args['run_id']))
                return batch(1)
            units.append(CommonBatchUnit(service,key=key,lane='auto',channel=channel(1),video_type='long',
                claim_batch=claim,require_ready=lambda:None,on_claimed=partial(state.on_claimed,key),
                on_settled=partial(state.on_settled,key)))
        operator=Work('operator','operator',channel(2),'channel',
            lambda:Prepared(lambda cancel:calls.append(('operator',RUN)),time.monotonic()+10),lambda completion:Ack())
        with ThreadPoolExecutor(max_workers=2) as executor:
            coordinator=Coordinator(executor,concurrency=2)
            coordinator.offer(units[0].work);coordinator.offer(operator);coordinator.offer(units[1].work)
            end=time.monotonic()+5
            while coordinator.snapshot().held or coordinator.snapshot().ready:
                self.assertLess(time.monotonic(),end);coordinator.step();coordinator.wait(.01)
            coordinator.offer(units[2].work)
            while coordinator.snapshot().held or coordinator.snapshot().ready:
                self.assertLess(time.monotonic(),end);coordinator.step();coordinator.wait(.01)
        self.assertEqual((state.claimed,state.inspected,state.settled_units,state.remaining),(3,3,3,0))
        self.assertEqual([value for kind,value in calls if kind=='claim'],[RUN]*3)
        self.assertEqual(sum(kind=='operator' for kind,_ in calls),1)


if __name__=='__main__':unittest.main()
