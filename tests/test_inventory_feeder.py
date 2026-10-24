"""Actual inventory feeder/unit/coordinator; only repository/provider are doubles."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident, Thread
from unittest.mock import Mock, patch
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from collection.budgets import PageBudget
from collection.dispatch import Ack, Coordinator, DispatchBlocked, Prepared, Work
from collection.inventory_feeder import AutoInventoryCursor, AutoInventoryFeeder
from db.inventory.inventory_candidates import InventoryCandidateCursor
from test_collection_dispatch import ManualExecutor, Clock
from test_shared_inventory_engine import Repository as PageRepository, claim, page, NOW
import collection_shutdown


def channel(number):return 'UC'+str(number).zfill(22)


class Repository(PageRepository):
    def __init__(self, scopes, *, physical=()):
        super().__init__();self.owner=get_ident()
        self.states={}
        for index,scope in enumerate(dict.fromkeys([*physical,*scopes]),1):
            self.states[scope]=dict(ordinal=index,eligible=scope in scopes,due=True,leased=False,
                scan=None,token=None,transition=None,epoch=7)
        self.pages=[];self.claims=[];self.locked=set()
        self.unknown_claim=self.unknown_settle=self.wrong_scope=False
        self.database=Mock(spec=['drain_hot_collection_claims','abort_hot_collection_claims'])
        self.database.drain_hot_collection_claims.return_value=dict(status='drained',remaining=0,
            playlist_requested=1,playlist_released=1,video_requested=0,video_released=0,
            operator_requested=0,operator_released=0)

    def check(self):assert get_ident()==self.owner,'repository called from provider thread'

    def candidate_page(self, **request):
        self.check();self.pages.append(request)
        ceiling=request['ceiling']
        if ceiling is None:ceiling=max((r['ordinal'] for r in self.states.values()),default=0)
        physical=sorted(((InventoryCandidateCursor(r['ordinal'],0,*scope),scope,r)
            for scope,r in self.states.items() if r['ordinal']<=ceiling),key=lambda item:item[0].key)
        physical=[row for row in physical if request['after'] is None or row[0].key>request['after'].key][:request['limit']]
        proofs={(p['channel_id'],p['video_type']):p for p in request['continuations']}
        candidates=[]
        for cursor,scope,row in physical:
            proof=proofs.get(scope)
            continuation=(proof is not None and proof['scan_id']==row['scan']
                and proof['last_transition_id']==row['transition'] and proof['control_epoch']==row['epoch'])
            if row['eligible'] and not row['leased'] and (row['due'] or continuation):
                candidates.append(dict(channel_id=scope[0],video_type=scope[1],control_epoch=row['epoch'],due=row['due']))
        return dict(rows=candidates,next_after=physical[-1][0] if physical else request['after'],ceiling=ceiling,
            inspected_count=len(physical),scan_exhausted=len(physical)<request['limit'])

    def _claim(self, scope, request, *, continuation):
        self.check();self.claims.append(dict(scope=scope,continuation=continuation,**request))
        row=self.states[scope]
        if scope in self.locked or row['leased'] or not row['eligible']:return []
        if not continuation and not row['due']:return []
        row['leased']=True
        row['ordinal']=max(r['ordinal'] for r in self.states.values())+1
        if self.unknown_claim:raise OSError('synthetic unknown claim COMMIT')
        value=claim(scope[1],channel_id=scope[0],lease_channel_epoch=row['epoch'],
            active_scan_id=row['scan'],resume_page_token=row['token'])
        if self.wrong_scope:value=replace(value,channel_id=channel(9999))
        return [value]

    def claim_channels(self, ids, **request):
        assert len(ids)==1 and len(request['video_types'])==1 and request['only_due'] is True
        scope=(ids[0],request['video_types'][0])
        assert request['expected_channel_scopes']==[dict(channel_id=ids[0],control_epoch=self.states[scope]['epoch'])]
        return self._claim(scope,request,continuation=False)

    def claim_pass(self, **request):
        assert request['include_due'] is False and request['limit']==1 and len(request['continuations'])==1
        proof=request['continuations'][0];scope=(proof['channel_id'],proof['video_type'])
        row=self.states[scope]
        if (proof['scan_id'],proof['last_transition_id'],proof['control_epoch'])!=(row['scan'],row['transition'],row['epoch']):
            self.check();self.claims.append(dict(scope=scope,continuation=True,**request));return []
        return self._claim(scope,request,continuation=True)

    def begin_scan(self, owned, **options):
        self.check();reply=super().begin_scan(owned,**options)
        self.states[(owned.channel_id,owned.video_type)]['scan']=reply['scan_id']
        return reply

    def settle_page(self, owned, **options):
        self.check();reply=super().settle_page(owned,**options)
        row=self.states[(owned.channel_id,owned.video_type)]
        row.update(leased=False,due=False,scan=reply['scan_id'],token=reply['resume_page_token'],
            transition=reply['transition_id'],eligible=reply['status']=='running')
        if self.unknown_settle:raise OSError('synthetic unknown page COMMIT')
        return reply

    def fail_scan(self,*args,**kwargs):self.check();return super().fail_scan(*args,**kwargs)
    def defer_claims_after_daily_quota(self,*args,**kwargs):self.check();return super().defer_claims_after_daily_quota(*args,**kwargs)


class FeederTests(unittest.TestCase):
    def setUp(self):
        stop=patch.object(collection_shutdown,'_requested',Event());stop.start();self.addCleanup(stop.stop)
        self.sent=[]

    def create(self, repo, *, limit=10, scan_limit=1000, concurrency=2, repeat=1, cursor=None, run_id='stable-inventory-pass'):
        counts={}
        def provider(playlist,**options):
            self.sent.append((playlist,options))
            counts[playlist]=counts.get(playlist,0)+1
            return page('next-'+str(counts[playlist]) if counts[playlist]<repeat else None)
        clock=Clock();executor=ManualExecutor();coordinator=Coordinator(executor,concurrency,clock=clock)
        feeder=AutoInventoryFeeder(repo,page_budget=PageBudget(limit),cursor=cursor or AutoInventoryCursor(),candidate_scan_limit=scan_limit,
            run_id=run_id,worker_id='worker',fetch_page=provider,require_ready=lambda:None,
            concurrency=concurrency,now=lambda:NOW,clock=clock)
        return feeder,coordinator,executor

    def drive(self, feeder, coordinator, executor, maximum=2000):
        for _ in range(maximum):
            feeder.offer_to(coordinator)
            coordinator.step()
            for index,(future,*_) in enumerate(executor.jobs):
                if not future.done():executor.run(index)
            state=coordinator.snapshot()
            self.assertLessEqual(state.ready,2*coordinator.concurrency)
            self.assertLessEqual(state.held,coordinator.concurrency)
            self.assertLessEqual(len(feeder._owned),3*coordinator.concurrency)
            if feeder.exhausted and not state.ready and not state.held:return
        self.fail('bounded fake inventory pass did not finish')

    def test_full_page_observer_is_owner_only_and_after_durable_receipt(self):
        repo=Repository([(channel(1),'long')]);feeder,owner,executor=self.create(repo,limit=1)
        observed=[]
        def on_page(result):
            self.assertEqual(get_ident(),repo.owner)
            self.assertEqual(len(repo.receipts),1)
            observed.append(dict(result))
        feeder.on_page=on_page
        self.drive(feeder,owner,executor)
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0]['channel_id'],channel(1))
        self.assertIn('channel_scope',observed[0])
        self.assertIn('new_video_ids',observed[0])
        self.assertNotIn('channel_id',feeder.summary['results'][0])

    def test_page_observer_unknown_is_not_replayed_or_cursor_released(self):
        repo=Repository([(channel(1),'long')]);feeder,owner,executor=self.create(repo,limit=1)
        observer=Mock(side_effect=RuntimeError('unknown handoff'))
        feeder.on_page=observer
        feeder.offer_to(owner);owner.step();executor.run(0)
        with self.assertRaises(DispatchBlocked):owner.step()
        with self.assertRaises(DispatchBlocked):owner.step()
        with self.assertRaises(RuntimeError):feeder.finish_pass(owner)
        observer.assert_called_once()
        self.assertEqual((len(repo.receipts),owner.snapshot().held),(1,1))

    def test_offer_limit_and_global_stop_preserve_unclaimed_budget(self):
        repo=Repository([(channel(i),'long') for i in range(1,8)])
        feeder,owner,executor=self.create(repo,limit=3)
        self.assertEqual(feeder.offer_to(owner,limit=0),0)
        self.assertFalse(repo.pages)
        for value in (True,-1,101,1.5):
            with self.assertRaises(ValueError):feeder.offer_to(owner,limit=value)
        self.assertEqual(feeder.offer_to(owner,limit=1),1)
        feeder.should_stop=lambda:True
        owner.step()
        self.assertFalse(repo.claims)
        self.assertFalse(self.sent)
        self.assertEqual(feeder.claims_remaining,3)

    def followup(self, result, *, video_repo=None, ready=lambda:None, fallback_required=False):
        from collection.availability import AddressedAvailabilityUnit
        from test_availability_dispatch import Repository as VideoRepository
        from test_initial_video_parallel import service,video
        from types import SimpleNamespace
        video_repo=video_repo or VideoRepository()
        original_claim=video_repo.claim
        def scoped_claim():
            value=original_claim()
            value['rows']=[replace(row,channel_id=result['channel_id']) for row in value['rows']]
            return value
        video_repo.claim=scoped_claim
        video_repo.load_video_channel_map=lambda ids:dict.fromkeys(ids,result['channel_id'])
        if not isinstance(video_repo.ensure_video_ids,Mock):
            video_repo.ensure_video_ids=lambda ids,**options:None
        def source(ids,reason):
            items=[video(value) for value in ids]
            for item in items:
                item['snippet']['channelId']=result['channel_id']
                if fallback_required:item['statistics'].pop('viewCount')
            return items
        collector=service(video_repo,source)
        collector.fetch_metadata=source
        collector.fetch_counters=lambda ids,reason:dict(items=[dict(id=value,views=2,like_count=1,comment_count=1) for value in ids],summary=dict(failed_ids=[]))
        return AddressedAvailabilityUnit(SimpleNamespace(database=object()),collector,
            key='recheck',channel_id=result['channel_id'],expected_epoch=7,kind='recheck',
            discovery_result=dict(status='no_new_videos'),trigger_allowed=False,
            require_ready=ready,on_settled=lambda reply:None)

    def test_hot_before_followup_prepare_does_not_unacknowledge_page(self):
        from hot_runtime import HotRuntimeUnavailable
        repo=Repository([(channel(1),'long')]);feeder,owner,executor=self.create(repo,limit=1)
        feeder.on_page=lambda result:self.followup(result,ready=Mock(side_effect=HotRuntimeUnavailable()))
        self.drive(feeder,owner,executor)
        self.assertEqual((len(repo.receipts),feeder.budget.attempts,feeder.summary['completed']),(1,1,1))
        self.assertEqual(owner.snapshot().held,0)
        repo.database.drain_hot_collection_claims.assert_not_called()

    def test_unknown_followup_claim_preserves_known_page_and_no_inventory_retry(self):
        from test_availability_dispatch import Repository as VideoRepository
        videos=VideoRepository();videos.ensure_video_ids=Mock(side_effect=OSError('unknown queue preparation'))
        repo=Repository([(channel(1),'long')]);feeder,owner,executor=self.create(repo,limit=1)
        feeder.on_page=lambda result:self.followup(result,video_repo=videos)
        with patch('collection.availability.load_availability_recheck_candidate_ids',return_value=['abcdefghijk']), \
                patch.dict('os.environ',{'LIC_DEPLOYMENT_PROFILE':'self-hosted'}):
            feeder.offer_to(owner);owner.step();executor.run(0)
            with self.assertRaises(DispatchBlocked):owner.step()
            with self.assertRaises(DispatchBlocked):owner.step()
        self.assertEqual((len(repo.receipts),feeder.budget.attempts,feeder.summary['completed']),(1,1,1))
        self.assertEqual(owner.snapshot().held,1)
        self.assertEqual(len(repo.claims),1)
        videos.ensure_video_ids.assert_called_once()
        repo.database.drain_hot_collection_claims.assert_not_called()

    def test_unknown_followup_settlement_never_retries_or_refunds_inventory_page(self):
        from test_availability_dispatch import Repository as VideoRepository
        videos=VideoRepository();videos.fail_commit=True
        repo=Repository([(channel(1),'long')]);feeder,owner,executor=self.create(repo,limit=1)
        feeder.on_page=lambda result:self.followup(result,video_repo=videos,fallback_required=True)
        with patch('collection.availability.load_availability_recheck_candidate_ids',return_value=['abcdefghijk']), \
                patch.dict('os.environ',{'LIC_DEPLOYMENT_PROFILE':'self-hosted'}):
            feeder.offer_to(owner);owner.step();executor.run(0);owner.step()
            executor.run(1);owner.step();executor.run(2)
            with self.assertRaises(DispatchBlocked):owner.step()
            with self.assertRaises(DispatchBlocked):owner.step()
        self.assertEqual((len(repo.receipts),len(repo.claims),feeder.budget.attempts),(1,1,1))
        self.assertEqual((videos.claimed,owner.snapshot().held),(1,1))
        repo.database.drain_hot_collection_claims.assert_not_called()

    def test_other_scope_settles_while_known_page_waits_for_followup(self):
        repo=Repository([(channel(1),'long'),(channel(2),'short')])
        feeder,owner,executor=self.create(repo,limit=2,concurrency=2)
        feeder.on_page=lambda result:self.followup(result,fallback_required=True) if result['video_type']=='long' else None
        with patch('collection.availability.load_availability_recheck_candidate_ids',return_value=['abcdefghijk']), \
                patch.dict('os.environ',{'LIC_DEPLOYMENT_PROFILE':'self-hosted'}):
            feeder.offer_to(owner);owner.step()
            executor.run(0);owner.step()  # Long page ACK, recheck remains unrun.
            executor.run(1);owner.step()  # Independent Short page may finish.
            self.assertEqual((len(repo.receipts),owner.snapshot().held),(2,1))
            self.assertEqual(feeder.budget.attempts,2)
            executor.run(2);owner.step();executor.run(3);owner.step()
            self.assertFalse(owner.keys())

    def test_cancel_followup_after_page_ack_only_drains_current_video_claim(self):
        repo=Repository([(channel(1),'long')]);feeder,owner,executor=self.create(repo,limit=1)
        feeder.on_page=lambda result:self.followup(result)
        with patch('collection.availability.load_availability_recheck_candidate_ids',return_value=['abcdefghijk']), \
                patch('collection.initial_video_hydration._drain_unsent') as drain, \
                patch.dict('os.environ',{'LIC_DEPLOYMENT_PROFILE':'self-hosted'}):
            feeder.offer_to(owner);owner.step();executor.run(0);owner.step()
            self.assertEqual((len(repo.receipts),len(executor.jobs)),(1,2))
            self.assertEqual(owner.snapshot().held,1)
            owner.cancel(owner.keys()[0]);owner.step()
            drain.assert_called_once()
            self.assertEqual(len(drain.call_args.args[1]),1)
        self.assertEqual((len(repo.receipts),feeder.budget.attempts),(1,1))
        self.assertEqual(owner.snapshot().held,0)
        repo.database.drain_hot_collection_claims.assert_not_called()

    def test_three_formats_share_one_page_claim_and_run_budget(self):
        scopes=[(channel(1),kind) for kind in ('long','short','stream')]
        repo=Repository(scopes);feeder,coordinator,executor=self.create(repo,limit=3,concurrency=3)
        self.drive(feeder,coordinator,executor)
        self.assertEqual((feeder.budget.attempts,feeder.claims_processed,len(self.sent)),(3,3,3))
        self.assertEqual({r['scope'][1] for r in repo.claims},{'long','short','stream'})
        self.assertEqual({r['run_id'] for r in repo.claims},{'stable-inventory-pass'})
        self.assertEqual(feeder.reservations.reserved,0)

    def test_empty_physical_prefix_advances_and_last_allowed_row_is_processed(self):
        all_scopes=[(channel(i),'long') for i in range(1,402)]
        repo=Repository([all_scopes[-1]],physical=all_scopes)
        feeder,coordinator,executor=self.create(repo,limit=1,scan_limit=401)
        self.drive(feeder,coordinator,executor)
        self.assertEqual((feeder.candidate_inspected,feeder.budget.attempts),(401,1))
        self.assertEqual(repo.claims[0]['scope'],all_scopes[-1])
        self.assertTrue(all(1<=p['limit']<=4 for p in repo.pages))

    def test_locked_prefix_does_not_spend_claim_or_attempt_allowance(self):
        scopes=[(channel(i),'long') for i in range(1,9)]
        repo=Repository(scopes);repo.locked=set(scopes[:-1])
        feeder,coordinator,executor=self.create(repo,limit=1,scan_limit=100)
        self.drive(feeder,coordinator,executor)
        self.assertEqual((feeder.claims_processed,feeder.budget.attempts),(1,1))
        self.assertEqual(repo.claims[-1]['scope'],scopes[-1])

    def test_continuation_reenters_fair_order_after_small_channel(self):
        scopes=[(channel(i),'long') for i in range(1,4)]
        repo=Repository(scopes);feeder,coordinator,executor=self.create(repo,limit=4,concurrency=1,repeat=10)
        self.drive(feeder,coordinator,executor)
        self.assertEqual([r['scope'] for r in repo.claims],scopes+[scopes[0]])
        self.assertEqual([r['continuation'] for r in repo.claims],[False,False,False,True])
        self.assertTrue(repo.claims[-1]['continuations'])
        self.assertGreater(feeder.candidate_inspected,3)
        self.assertEqual(feeder.claims_processed,4)

    def test_ready_capacity_does_not_create_private_backlog(self):
        repo=Repository([(channel(i),'long') for i in range(1,12)])
        feeder,coordinator,executor=self.create(repo,limit=10)
        self.assertEqual(feeder.offer_to(coordinator),4)
        self.assertEqual(feeder.offer_to(coordinator),0)
        self.assertEqual((len(repo.pages),len(feeder._owned)),(1,4))
        coordinator.step();feeder.offer_to(coordinator)
        self.assertEqual((coordinator.snapshot().ready,len(feeder._owned)),(4,6))

    def test_duplicate_own_ready_scope_is_not_offered_twice(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo)
        feeder.offer_to(coordinator)
        with patch.object(repo,'candidate_page') as read:
            self.assertEqual(feeder.offer_to(coordinator),0)  # Physical EOF waits for this own descriptor.
        read.assert_not_called()
        self.assertEqual(len(feeder._owned),1)

    def test_operator_playlist_exclusion_skips_without_claim(self):
        scope=(channel(1),'long');repo=Repository([scope]);feeder,coordinator,executor=self.create(repo,limit=1)
        coordinator.offer(Work('operator','operator',scope[0],scope[1],lambda:Prepared(lambda cancel:None,200),
            lambda completion:Ack(),exclusion_key='playlist:'+scope[0]+':'+scope[1]))
        self.assertEqual(feeder.offer_to(coordinator),0)
        self.assertFalse(repo.claims)
        self.assertFalse(feeder._owned)
        self.assertEqual(feeder.candidate_inspected,1)

    def test_operator_interleaves_without_resetting_any_pass_counter(self):
        scopes=[(channel(i),'short') for i in range(1,8)]
        repo=Repository(scopes);feeder,coordinator,executor=self.create(repo,limit=5,scan_limit=15)
        observed=[]
        coordinator.offer(Work('operator','operator',channel(99),'channel',
            lambda:Prepared(lambda cancel:observed.append(feeder.budget.attempts),200),lambda c:Ack()))
        self.drive(feeder,coordinator,executor)
        self.assertEqual(feeder.budget.attempts,5)
        self.assertEqual(len(observed),1)
        self.assertLess(observed[0],5)
        self.assertEqual({r['run_id'] for r in repo.claims},{feeder.run_id})
        self.assertLessEqual(feeder.candidate_inspected,15)

    def test_unknown_claim_keeps_both_reservations_without_provider_or_retry(self):
        repo=Repository([(channel(1),'long')]);repo.unknown_claim=True
        feeder,coordinator,executor=self.create(repo,limit=1)
        feeder.offer_to(coordinator)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual((feeder.claims_processed,feeder.claims_remaining,feeder.reservations.reserved),(0,0,1))
        self.assertTrue(feeder.paused)
        self.assertEqual(coordinator.snapshot().held,1)
        self.assertFalse(self.sent)
        self.assertEqual(feeder.offer_to(coordinator),0)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual(len(repo.claims),1)

    def test_unknown_settlement_never_emits_continuation_or_retries(self):
        repo=Repository([(channel(1),'long')]);repo.unknown_settle=True
        feeder,coordinator,executor=self.create(repo,limit=2,repeat=10)
        feeder.offer_to(coordinator);coordinator.step();executor.run(0)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual((feeder.budget.attempts,feeder.summary['processed']),(1,0))
        self.assertFalse(feeder._proofs)
        self.assertTrue(feeder.paused)
        self.assertEqual(feeder.offer_to(coordinator),0)
        self.assertEqual((len(repo.receipts),len(self.sent)),(1,1))

    def test_wrong_claim_scope_cannot_enter_provider(self):
        repo=Repository([(channel(1),'long')]);repo.wrong_scope=True
        feeder,coordinator,executor=self.create(repo)
        feeder.offer_to(coordinator)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertFalse(self.sent)
        self.assertEqual(feeder.reservations.reserved,1)

    def test_cancel_unsent_ready_uses_no_db_or_budget_and_clears_scope(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo)
        feeder.offer_to(coordinator)
        self.assertEqual(feeder.cancel_ready(coordinator),1)
        self.assertEqual((coordinator.snapshot().ready,feeder.budget.attempts,feeder.reservations.reserved),(0,0,0))
        self.assertFalse(repo.claims)
        self.assertFalse(feeder._owned)

    def test_cancel_prepared_uses_existing_known_token_drain_and_no_refund_of_claim(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo,limit=1)
        feeder.offer_to(coordinator);coordinator.step()
        key=next(iter(feeder._owned.values()))['key'];coordinator.cancel(key);coordinator.step()
        repo.database.drain_hot_collection_claims.assert_called_once()
        self.assertEqual((feeder.claims_processed,feeder.budget.attempts,feeder.reservations.reserved),(1,0,0))
        self.assertFalse(feeder._owned)
        self.assertTrue(feeder.paused)
        self.assertFalse(self.sent)

    def test_zero_scan_or_page_budget_performs_no_candidate_or_claim_calls(self):
        for limit,scan in ((0,100),(10,0)):
            repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo,limit=limit,scan_limit=scan)
            self.assertEqual(feeder.offer_to(coordinator),0)
            self.assertTrue(feeder.exhausted)
            self.assertFalse(repo.pages)
            self.assertFalse(repo.claims)

    def test_result_samples_and_proofs_are_bounded_without_source_tokens(self):
        scopes=[(channel(i),'long') for i in range(1,503)]
        repo=Repository(scopes);feeder,coordinator,executor=self.create(repo,limit=501,scan_limit=1100,concurrency=8,repeat=10)
        self.drive(feeder,coordinator,executor)
        self.assertEqual((len(feeder._proofs),len(feeder.summary['results']),feeder.summary['results_omitted']),(500,500,1))
        self.assertTrue(all(set(row)=={'video_type','status','provider_attempts','page_items'} for row in feeder.summary['results']))
        self.assertNotIn('scan_id',str(feeder.summary))
        self.assertNotIn('transition_id',str(feeder.summary))
        self.assertNotIn('resume_page_token',str(feeder.summary))

    def test_scan_cap_continues_tail_across_fresh_passes_and_budgets(self):
        physical=[(channel(i),'long') for i in range(1,122)]
        repo=Repository([physical[-1]],physical=physical);cursor=AutoInventoryCursor()
        starts=[]
        for number in range(4):
            feeder,coordinator,executor=self.create(repo,limit=1,scan_limit=40,cursor=cursor,run_id=f'pass-{number}')
            before=len(repo.pages)
            self.drive(feeder,coordinator,executor)
            starts.append(repo.pages[before]['after'])
            self.assertLessEqual(feeder.candidate_inspected,40)
            if number<3:
                self.assertEqual(feeder.pass_stop_reason,'candidate_scan_limit')
                self.assertFalse(cursor.sweep_exhausted)
                self.assertEqual((feeder.claims_processed,feeder.budget.attempts),(0,0))
            feeder.finish_pass(coordinator)
        self.assertEqual([value.dispatch_ordinal if value else None for value in starts],[None,40,80,120])
        self.assertEqual([value['scope'] for value in repo.claims],[physical[-1]])
        self.assertEqual(repo.claims[0]['run_id'],'pass-3')
        self.assertEqual(len(self.sent),1)

    def test_locked_prefix_advances_across_passes_without_claim_reservation_leak(self):
        scopes=[(channel(i),'stream') for i in range(1,10)]
        repo=Repository(scopes);repo.locked=set(scopes[:-1]);cursor=AutoInventoryCursor()
        for number in range(3):
            feeder,coordinator,executor=self.create(repo,limit=1,scan_limit=3,cursor=cursor)
            self.drive(feeder,coordinator,executor)
            self.assertEqual(feeder.reservations.reserved,0)
            self.assertEqual(feeder.claims_processed,int(number==2))
            feeder.finish_pass(coordinator)
        self.assertEqual([entry['scope'] for entry in repo.claims],scopes)
        self.assertEqual(len(self.sent),1)

    def test_eof_resets_only_next_sweep_and_new_ordinals_wait_for_that_sweep(self):
        scopes=[(channel(i),'long') for i in range(1,6)]
        repo=Repository([],physical=scopes);cursor=AutoInventoryCursor()
        first,coordinator,executor=self.create(repo,limit=1,scan_limit=2,cursor=cursor)
        self.drive(first,coordinator,executor);first.finish_pass(coordinator)
        scope=(channel(90),'short')
        repo.states[scope]=dict(ordinal=90,eligible=True,due=True,leased=False,scan=None,token=None,transition=None,epoch=7)
        second,coordinator,executor=self.create(repo,limit=1,scan_limit=10,cursor=cursor)
        self.drive(second,coordinator,executor)
        self.assertTrue(cursor.sweep_exhausted)
        self.assertEqual(second.pass_stop_reason,'sweep_exhausted')
        self.assertEqual(second.candidate_inspected,3)
        self.assertFalse(repo.claims)
        self.assertEqual(cursor._ceiling,5)
        second.finish_pass(coordinator)
        third,coordinator,executor=self.create(repo,limit=1,scan_limit=10,cursor=cursor)
        self.assertIsNone(cursor._after)
        self.assertIsNone(cursor._ceiling)
        self.drive(third,coordinator,executor)
        self.assertEqual([row['scope'] for row in repo.claims],[scope])

    def test_cursor_has_one_active_owner_and_closed_feeder_cannot_reenter(self):
        repo=Repository([]);cursor=AutoInventoryCursor()
        first,coordinator,executor=self.create(repo,cursor=cursor)
        with self.assertRaisesRegex(RuntimeError,'cursor_pass_active'):self.create(repo,cursor=cursor)
        self.drive(first,coordinator,executor);first.finish_pass(coordinator)
        with self.assertRaisesRegex(RuntimeError,'pass_closed'):first.offer_to(coordinator)
        second,_,_=self.create(repo,cursor=cursor)
        self.assertIs(cursor._active,second)
        self.assertEqual((second.budget.attempts,second.candidate_inspected,second.claims_processed),(0,0,0))

    def test_finish_refuses_ready_held_and_unknown_work(self):
        for unknown in (False,True):
            with self.subTest(unknown=unknown):
                repo=Repository([(channel(1),'long')]);repo.unknown_claim=unknown
                feeder,coordinator,executor=self.create(repo,limit=1)
                feeder.offer_to(coordinator)
                with self.assertRaisesRegex(RuntimeError,'release_unconfirmed'):feeder.finish_pass(coordinator)
                if unknown:
                    with self.assertRaises(DispatchBlocked):coordinator.step()
                else:coordinator.step()
                with self.assertRaisesRegex(RuntimeError,'release_unconfirmed'):feeder.finish_pass(coordinator)
                self.assertIs(feeder.cursor._active,feeder)

    def test_finish_ignores_other_lane_known_work_but_not_global_uncertainty(self):
        repo=Repository([]);feeder,coordinator,executor=self.create(repo,scan_limit=0)
        coordinator.offer(Work('operator','operator',channel(99),'channel',
            lambda:Prepared(lambda cancel:None,200),lambda completion:Ack()))
        feeder.offer_to(coordinator)
        self.assertTrue(feeder.exhausted)
        feeder.finish_pass(coordinator)
        self.assertTrue(coordinator.contains('operator'))
        repo=Repository([]);feeder,coordinator,executor=self.create(repo,scan_limit=0)
        def unknown():raise RuntimeError('synthetic claim COMMIT unknown')
        coordinator.offer(Work('unknown','operator',channel(99),'channel',unknown,lambda completion:Ack()))
        feeder.offer_to(coordinator)
        with self.assertRaises(DispatchBlocked):coordinator.step()
        with self.assertRaisesRegex(RuntimeError,'release_unconfirmed'):feeder.finish_pass(coordinator)

    def test_full_other_lane_ready_queue_does_not_block_finished_auto_pass(self):
        repo=Repository([]);feeder,coordinator,executor=self.create(repo,scan_limit=0)
        for index in range(4):
            coordinator.offer(Work(f'operator-{index}','operator',channel(99+index),'channel',
                lambda:Prepared(lambda cancel:None,200),lambda completion:Ack()))
        feeder.offer_to(coordinator)
        self.assertTrue(feeder.exhausted)
        feeder.finish_pass(coordinator)
        self.assertEqual(coordinator.snapshot().ready,4)
        self.assertFalse(repo.pages)

    def test_external_ready_cancellation_is_pruned_without_reclaiming_cursor_position(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo)
        feeder.offer_to(coordinator);position=feeder.cursor._after
        key=next(iter(feeder._owned.values()))['key']
        coordinator.cancel(key);feeder.pause('scheduled_handoff');feeder.finish_pass(coordinator)
        self.assertFalse(feeder._owned)
        self.assertFalse(feeder._offered)
        self.assertEqual(feeder.cursor._after,position)
        self.assertFalse(repo.claims)

    def test_private_continuation_proofs_do_not_move_to_next_pass(self):
        repo=Repository([(channel(1),'long')]);cursor=AutoInventoryCursor()
        first,coordinator,executor=self.create(repo,limit=1,repeat=10,cursor=cursor)
        self.drive(first,coordinator,executor)
        self.assertEqual(len(first._proofs),1)
        first.finish_pass(coordinator)
        self.assertFalse(first._proofs)
        self.assertEqual(set(vars(cursor)),{'_owner','_active','_after','_ceiling','_exhausted'})
        second,coordinator,executor=self.create(repo,limit=1,cursor=cursor)
        self.drive(second,coordinator,executor)
        self.assertEqual((second.claims_processed,second.budget.attempts),(0,0))
        self.assertEqual(len(repo.claims),1)
        self.assertFalse(second._proofs)

    def test_empty_exact_continuation_claim_never_falls_back_to_ordinary_due(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo,limit=3,repeat=10)
        with patch.object(repo,'claim_pass',return_value=[]) as continuation:
            self.drive(feeder,coordinator,executor)
        continuation.assert_called_once()
        self.assertFalse(continuation.call_args.kwargs['include_due'])
        self.assertEqual((len(repo.claims),len(self.sent)),(1,1))
        self.assertEqual(feeder.reservations.reserved,0)

    def test_malformed_candidate_does_not_advance_cursor_or_claim_and_pauses(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo)
        malformed=dict(rows=[],next_after=None,ceiling=1,inspected_count=1,scan_exhausted=True)
        with patch.object(repo,'candidate_page',return_value=malformed):
            with self.assertRaisesRegex(ValueError,'candidate_invalid'):feeder.offer_to(coordinator)
        self.assertIsNone(feeder.cursor._after)
        self.assertIsNone(feeder.cursor._ceiling)
        self.assertEqual(feeder.candidate_inspected,0)
        self.assertFalse(repo.claims)
        self.assertTrue(feeder.paused)
        with self.assertRaisesRegex(RuntimeError,'release_unconfirmed'):feeder.finish_pass(coordinator)

    def test_running_summary_without_durable_transition_keeps_credit_and_no_proof(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo,repeat=10)
        original=repo.settle_page
        def broken(*args,**options):
            result=original(*args,**options);result['transition_id']='invalid';return result
        with patch.object(repo,'settle_page',side_effect=broken):
            feeder.offer_to(coordinator);coordinator.step();executor.run(0)
            with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual(coordinator.snapshot().held,1)
        self.assertFalse(feeder._proofs)
        self.assertEqual(feeder.summary['processed'],0)
        self.assertEqual(feeder.budget.attempts,1)

    def test_real_provider_thread_never_owns_repository_or_feeder_callbacks(self):
        repo=Repository([(channel(1),'long')]);feeder,_,_=self.create(repo,limit=1)
        owner=get_ident();fetch_threads=[]
        def provider(*args,**options):fetch_threads.append(get_ident());return page()
        feeder.fetch_page=provider
        with ThreadPoolExecutor(max_workers=1) as executor:
            coordinator=Coordinator(executor,2,clock=feeder.clock)
            feeder.offer_to(coordinator);coordinator.step()
        # Executor exit joins the actual fetch thread before settlement/cleanup.
        coordinator.step();feeder.offer_to(coordinator);feeder.finish_pass(coordinator)
        self.assertEqual(len(fetch_threads),1)
        self.assertNotEqual(fetch_threads[0],owner)
        self.assertEqual((feeder.budget.attempts,feeder.summary['processed']),(1,1))

    def test_wrong_thread_and_coordinator_are_refused(self):
        repo=Repository([(channel(1),'long')]);feeder,coordinator,executor=self.create(repo)
        errors=[]
        def wrong():
            try:feeder.offer_to(coordinator)
            except Exception as error:errors.append(type(error))
        worker=Thread(target=wrong);worker.start();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[RuntimeError])
        with self.assertRaises(ValueError):feeder.offer_to(Coordinator(ManualExecutor(),1))
        feeder.offer_to(coordinator)
        with self.assertRaises(ValueError):feeder.offer_to(Coordinator(ManualExecutor(),2))


if __name__=='__main__':unittest.main()
