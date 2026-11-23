"""Disposable PG17 proof of independent head demand and retained full debt."""
from datetime import timedelta
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock
import sys
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).parent)]
import discovery_executor_pg as fixture
from collection.inventory import run_typed_inventory_claim, run_typed_inventory_batch
from collection.inventory_admission import InventoryDiscoveryAdmission
from db.channel.channel_sync import ChannelSyncRepository
from db.discovery_budget import DiscoveryBudgetRepository
from db.discovery_runtime import runtime_status
from db.inventory.typed_inventory import TypedInventoryRepository
from db.hot.hot_collection_control import HotCollectionControlRepository
from discovery_policy import DiscoverySettings
from discovery_admission import DiscoveryAdmissionDenied


class HeadSeparationPostgres(unittest.TestCase):
    setUpClass=classmethod(fixture.DiscoveryDispatchPostgres.setUpClass.__func__)
    tearDownClass=classmethod(fixture.DiscoveryDispatchPostgres.tearDownClass.__func__)
    refresh=classmethod(fixture.DiscoveryDispatchPostgres.refresh.__func__)
    scalar=fixture.DiscoveryDispatchPostgres.scalar
    now=fixture.DiscoveryDispatchPostgres.now
    channel=fixture.DiscoveryDispatchPostgres.channel
    cover=fixture.DiscoveryDispatchPostgres.cover

    def setUp(self):
        self.owned=fixture.OwnerDatabase(self.db)
        self.target=self.channel()
        self.typed=TypedInventoryRepository(self.owned)
        self.typed.prepare_runtime(limit=1000,enable=True)
        self.admin.execute("""UPDATE public.channel_playlist_state SET runtime_enabled=false,status='paused',
            lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL
            WHERE channel_id<>%s""",(self.target,))
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=false WHERE singleton')
        self.epoch=self.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(self.target,))
        self.scopes=[dict(channel_id=self.target,control_epoch=self.epoch)]
        self.settings=replace(DiscoverySettings(),full_general_window_limit=1)
        self.budget=DiscoveryBudgetRepository(self.db,project_id='heads-'+str(uuid4()),settings=self.settings,interval_minutes=10)
        self.adapter=InventoryDiscoveryAdmission(self.budget)

    def claim_source(self):
        rows=self.typed.claim_channels([self.target],video_types=['short'],worker_id='head-native',run_id=str(uuid4()),
            expected_channel_scopes=self.scopes,only_due=True)
        self.assertEqual(len(rows),1)
        return rows[0]

    def payload(self,identity=None,*,next_token=None):
        identity=identity or uuid4().hex[:10]+'A'
        payload={'items':[{'contentDetails':{'videoId':identity,'videoPublishedAt':(self.now()-timedelta(minutes=1)).isoformat()}}]}
        if next_token:payload['nextPageToken']=next_token
        return identity,payload

    def grow(self,count=11):
        at=self.now().isoformat()
        ChannelSyncRepository(self.owned).update_metadata_batch([dict(channel_id=self.target,control_epoch=self.epoch,
            values=dict(video_count=count,channel_metadata_checked_at=at,channel_metadata_status='ok',channel_video_count_changed_at=at))])
        return self.scalar('SELECT count_reconcile_token FROM public.channels WHERE channel_id=%s',(self.target,))

    def missing(self):
        owned=self.claim_source();scan=str(uuid4())
        self.typed.begin_scan(owned,scan_id=scan,scan_boundary_at=self.now())
        self.typed.fail_scan(owned,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
            outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=self.now())

    def spent(self):
        self.assertTrue(self.budget.reserve(str(uuid4()),bucket='general',full=True,event=False)['allowed'])

    def drain(self,owned):
        HotCollectionControlRepository(self.owned).drain(playlists=[dict(channel_id=owned.channel_id,
            video_type=owned.video_type,lease_token=owned.lease_token)])

    def test_owner_full_check_joins_and_advances_weekly_proof_once(self):
        from db.inventory.manual_check import request_check, ManualCheckRefused
        from db.reads.channel_explorer import ChannelExplorerRepository
        source = (self.target, 'short')
        self.cover(*source)
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton")
        self.admin.execute("UPDATE public.channel_playlist_state SET head_next_due_at=now()+interval '1 day',next_due_at=now()+interval '6 days' WHERE channel_id=%s AND video_type=%s", source)
        header = ChannelExplorerRepository(self.db)
        def status():
            return header.load_list_inventory(self.target, limit=100)['library_formats']['short']
        before = status()
        self.assertEqual(before['manual_check']['estimated_general'], 1)
        token = str(uuid4())
        def submit(identity, previous=None):
            return request_check(self.db, *source, self.epoch, previous, identity)
        self.assertFalse(submit(token)['joined'])
        self.assertEqual(submit(str(uuid4()))['request']['id'], token)
        claim = self.claim_source()
        self.assertEqual(claim.scan_kind, 'full_reconciliation')
        unbudgeted = Mock()
        with self.assertRaises(DiscoveryAdmissionDenied):
            run_typed_inventory_claim(repository=self.typed,claim=claim,fetch_page=unbudgeted)
        unbudgeted.assert_not_called()
        calls = []
        def fetch(*args, **kwargs):
            calls.append(kwargs['page_token'])
            joined = submit(str(uuid4()))
            self.assertTrue(joined['joined'])
            self.assertEqual(joined['request']['state'], 'running')
            return self.payload()[1]
        result = run_typed_inventory_claim(repository=self.typed,claim=claim,fetch_page=fetch,discovery_admission=self.adapter)
        self.assertEqual((calls,result['coverage']), ([None],'complete'))
        after = status()
        self.assertGreater(after['full_completed_at'],before['full_completed_at'])
        receipt = after['manual_check']['request']
        self.assertEqual((receipt['id'],receipt['state'],receipt['pages'],receipt['general']),(token,'complete',1,1))
        self.assertTrue(submit(token)['joined'])
        with self.assertRaises(ManualCheckRefused):
            submit(str(uuid4()))
        self.assertEqual(submit(str(uuid4()), previous=token)['request']['state'], 'queued')

    def test_owner_joins_existing_scan_and_keeps_intent_when_budget_is_spent(self):
        from db.inventory.manual_check import request_check
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton")
        claim = self.claim_source()
        scan = str(uuid4())
        self.typed.begin_scan(claim,scan_id=scan,scan_boundary_at=self.now())
        token = str(uuid4())
        response = request_check(self.db,self.target,'short',self.epoch,None,token)
        self.assertEqual((response['joined'],response['request']['scan']), (True,scan))
        self.spent()
        self.admin.execute("UPDATE public.channel_playlist_state SET head_next_due_at=now()+interval '1 day' WHERE channel_id=%s AND video_type='short'", (self.target,))
        with self.assertRaises(DiscoveryAdmissionDenied):
            run_typed_inventory_claim(repository=self.typed,claim=claim,fetch_page=Mock(side_effect=AssertionError),
                                      discovery_admission=self.adapter)
        self.typed.record_manual_budget_wait(claim,'window_budget',self.now()+timedelta(minutes=5))
        repeated = request_check(self.db,self.target,'short',self.epoch,None,str(uuid4()))
        self.assertEqual((repeated['request']['wait'],repeated['request']['general']),('window_budget',0))
        self.drain(claim)
        # Model the next ten-minute window, then use a fresh repository and the ordinary due claim.
        self.admin.execute("UPDATE public.discovery_budget_days SET window_start=now()-interval '11 minutes',window_end=now()-interval '1 minute' WHERE project_id=%s",(self.budget.project,))
        self.admin.execute("""UPDATE public.channel_playlist_state SET next_due_at=now(),
            manual_list_check=manual_list_check||jsonb_build_object('retry_at',now()-interval '1 second')
            WHERE channel_id=%s AND video_type='short'""",(self.target,))
        self.typed = TypedInventoryRepository(self.owned)
        result = run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),
            fetch_page=lambda *args,**kwargs:self.payload()[1],discovery_admission=self.adapter)
        self.assertEqual(result['status'],'completed')
        saved = self.short('manual_list_check')
        self.assertEqual((saved['id'],saved['state'],saved['general']), (token,'complete',1))

    def test_two_concurrent_confirmations_create_one_intent(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from db.inventory.manual_check import request_check
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton")
        start = Barrier(2)
        def submit(identity):
            start.wait(timeout=5)
            return request_check(self.db,self.target,'short',self.epoch,None,identity)
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(submit,[str(uuid4()),str(uuid4())]))
        self.assertEqual(len({reply['request']['id'] for reply in replies}),1)
        self.assertEqual(sum(reply['joined'] for reply in replies),1)

    def test_manual_first_page_absence_and_http_quota_have_separate_receipts(self):
        from db.inventory.manual_check import request_check
        from db.reads.channel_explorer import ChannelExplorerRepository
        self.cover(self.target, 'short')
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton")
        repo = ChannelExplorerRepository(self.db)
        def status():
            return repo.load_list_inventory(self.target,limit=100)['library_formats']['short']
        previous = status()['full_completed_at']
        request_id = str(uuid4())
        request_check(self.db,self.target,'short',self.epoch,None,request_id)
        claim = self.claim_source()
        scan = str(uuid4())
        self.typed.begin_scan(claim,scan_id=scan,scan_boundary_at=self.now())
        receipt = dict(transition_id=str(uuid4()), source_checked_at=self.now(), charged_scan=scan)
        self.typed.defer_claims_after_daily_quota([claim], **receipt)
        self.typed.defer_claims_after_daily_quota([claim], **receipt)
        waiting = status()['manual_check']
        self.assertTrue(waiting['quota_retry_at'])
        self.assertEqual((waiting['request']['state'],waiting['request']['general']),('running',1))
        # Move only the test source's retry gate forward to simulate the next admitted pass.
        self.admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now(),source_next_after=now() WHERE channel_id=%s AND video_type='short'",(self.target,))
        retry = self.claim_source()
        failure = dict(scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
                       outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=self.now())
        self.typed.fail_scan(retry, **failure)
        self.typed.fail_scan(retry, **failure)
        after = status()
        self.assertTrue(after['absent_checked_at'])
        self.assertEqual(after['full_completed_at'],previous)
        self.assertEqual(after['manual_check']['estimated_general'],1)
        record = after['manual_check']['request']
        self.assertEqual((record['state'],record['general'],record['pages']),('absent',2,0))

    def test_manual_owner_http_uses_real_write_guard_and_returns_durable_job(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tempfile import TemporaryDirectory
        import time
        from self_hosted_auth import SelfHostedAuthMiddleware
        from db.self_hosted_auth import OwnerAuthRepository
        from web.explorer import create_explorer_router
        from web.middleware import HttpReadScopeMiddleware
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton")
        origin = 'http://127.0.0.1:8765'
        with TemporaryDirectory() as folder:
            owner = OwnerAuthRepository.initialize(Path(folder)/'owner.sqlite3','fixture',int(time.time()))
            owner.issue_setup('test-code',int(time.time()))
            owner.claim_setup(lambda x:x=='test-code','local-session',int(time.time()))
            owner.complete_setup('local-session','local',None,lambda:None,None,int(time.time()))
            app = FastAPI()
            app.include_router(create_explorer_router(database=self.db))
            app.add_middleware(SelfHostedAuthMiddleware,repository=owner,origin=origin)
            app.add_middleware(HttpReadScopeMiddleware)
            with TestClient(app,base_url=origin,headers={'Origin':origin}) as client:
                path = '/channels/'+self.target+'/explorer/list-check/short'
                payload = dict(epoch=self.epoch,previous=None,request_id=str(uuid4()))
                self.assertEqual(client.post(path,json=payload).status_code,403)
                response = client.post(path,json=payload,headers={'X-Saved-Work':'1'})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['request']['state'],'queued')
                projected = client.get('/channels/'+self.target+'/explorer/list-inventory')
                self.assertEqual(projected.status_code,200,projected.text)
                self.assertEqual(projected.json()['library']['formats']['short']['manual_check']['request']['id'],payload['request_id'])

    def test_owner_full_walk_prices_two_pages_and_preserves_date_after_later_failure(self):
        from db.inventory.manual_check import request_check
        from db.reads.channel_explorer import ChannelExplorerRepository
        self.cover(self.target,'short')
        self.admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton")
        repo = ChannelExplorerRepository(self.db)
        def status():
            return repo.load_list_inventory(self.target,limit=100)['library_formats']['short']
        adapter = InventoryDiscoveryAdmission(DiscoveryBudgetRepository(self.db,project_id=str(uuid4()),
            settings=replace(self.settings,full_general_window_limit=6),interval_minutes=10))
        previous = status()['full_completed_at']
        token = str(uuid4())
        request_check(self.db,self.target,'short',self.epoch,None,token)
        def page(next_token=None):
            return run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),
                fetch_page=lambda *args,**kwargs:self.payload(next_token=next_token)[1],discovery_admission=adapter)
        self.assertEqual(page('second')['status'],'running')
        self.assertEqual(status()['full_completed_at'],previous)
        self.assertEqual(page()['status'],'completed')
        complete = status()
        self.assertEqual(complete['manual_check']['estimated_general'],2)
        self.assertEqual(complete['manual_check']['request']['general'],2)
        request_check(self.db,self.target,'short',self.epoch,token,str(uuid4()))
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic transport failure')
        run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=fail,discovery_admission=adapter)
        after = status()
        self.assertEqual(after['full_completed_at'],complete['full_completed_at'])
        self.assertIsNone(after['absent_checked_at'])
        record = after['manual_check']['request']
        self.assertIn(record['state'],('failed','running'))
        self.assertEqual((record['general'],record['pages']), (1,0))

    def test_v4_inactive_growth_checks_missing_first_format_without_initial_proof(self):
        self.missing();token=self.grow();identity,payload=self.payload()
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=lambda *a,**kw:payload)
        self.assertEqual((result['status'],result['coverage']),('head_observed','partial'))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.channel_video_inventory_scans WHERE channel_id=%s AND status='completed' AND coverage='complete'",(self.target,)),0)
        self.assertEqual(self.scalar("SELECT count_reconcile_served_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)),token)
        self.assertIsNotNone(self.scalar("SELECT full_reconcile_required_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)))
        self.assertIsNone(self.scalar('SELECT event_used FROM public.discovery_budget_days WHERE project_id=%s',(self.budget.project,)))

    def test_v5_inactive_growth_bills_event_head_when_full_allowance_is_spent(self):
        self.missing();self.grow();self.spent();identity,payload=self.payload()
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=lambda *a,**kw:payload,
            discovery_admission=self.adapter)
        self.assertEqual(result['status'],'head_observed')
        self.assertEqual(self.admin.execute('SELECT event_used,full_used FROM public.discovery_budget_days WHERE project_id=%s',
            (self.budget.project,)).fetchone(),(1,1))
        owned=self.claim_source()
        plan=self.typed.automatic_page_plan(owned)
        self.assertFalse(plan['head_due']);self.assertFalse(plan['head_signal'])
        forbidden=Mock(side_effect=AssertionError('repeated head without demand'))
        try:
            with self.assertRaises(DiscoveryAdmissionDenied):
                run_typed_inventory_claim(repository=self.typed,claim=owned,fetch_page=forbidden,discovery_admission=self.adapter)
        finally:self.drain(owned)
        forbidden.assert_not_called()
        self.assertEqual(self.scalar("SELECT count(*) FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND result->>'channel_id'=%s",(self.target,)),1)

    def test_progressed_full_keeps_identity_cursor_receipts_and_membership_scan_tags(self):
        self.cover(self.target,'short')
        self.admin.execute("UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),next_due_at=now() WHERE channel_id=%s AND video_type='short'",(self.target,))
        first_id,first=self.payload(next_token='page-two')
        first_result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=lambda *a,**kw:first,
            discovery_admission=self.adapter)
        scan=first_result['scan_id']
        before=self.scalar('SELECT to_jsonb(s) FROM public.channel_video_inventory_scans s WHERE scan_id=%s',(scan,))
        tag=self.scalar('SELECT last_seen_scan_id FROM public.video_inventory_memberships WHERE video_id=%s',(first_id,))
        self.grow();identity,payload=self.payload();seen=[]
        def head(playlist,**options):seen.append(options['page_token']);return payload
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=head,discovery_admission=self.adapter)
        self.assertEqual(result['status'],'head_observed');self.assertEqual(seen,[None])
        self.assertEqual(self.scalar('SELECT to_jsonb(s) FROM public.channel_video_inventory_scans s WHERE scan_id=%s',(scan,)),before)
        self.assertEqual(self.scalar('SELECT last_seen_scan_id FROM public.video_inventory_memberships WHERE video_id=%s',(first_id,)),tag)
        self.assertEqual(self.admin.execute("SELECT active_scan_id::text,resume_page_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)).fetchone(),(scan,'page-two'))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.typed_inventory_receipts WHERE scan_id=%s AND receipt_scope='scan_transition'",(scan,)),1)

    def test_older_head_response_cannot_serve_concurrent_count_generation(self):
        self.cover(self.target,'short');old=self.grow();identity,payload=self.payload();new=[]
        def head(*args,**kwargs):new.append(self.grow(12));return payload
        run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=head,discovery_admission=self.adapter)
        self.assertNotEqual(old,new[0])
        self.assertEqual(self.scalar("SELECT count_reconcile_served_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)),old)
        owned=self.claim_source()
        try:self.assertTrue(self.typed.automatic_page_plan(owned)['head_signal'])
        finally:self.drain(owned)

    def test_runtime_probe_wakes_due_head_despite_spent_full_capacity(self):
        self.cover(self.target,'short')
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')
        self.admin.execute("UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),next_due_at=now() WHERE channel_id=%s AND video_type='short'",(self.target,))
        self.grow();self.spent()
        status=runtime_status(self.owned,project_id=self.budget.project,settings=self.settings,interval_minutes=10,video_types=('short',))
        self.assertTrue(status['runnable'],status)

    def test_failed_count_head_waits_for_retry_or_a_new_count_generation(self):
        self.cover(self.target,'short')
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')
        self.grow()
        def failed(*args,**kwargs):raise RuntimeError('synthetic source failure')
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=failed,
            discovery_admission=self.adapter)
        self.assertEqual(result['status'],'error')
        options=dict(video_types=['short'],worker_id='head-native',run_id=str(uuid4()),
            expected_channel_scopes=self.scopes,only_due=True)
        self.assertEqual(self.typed.claim_channels([self.target],**options),[])
        self.grow(12)
        owned=self.claim_source()
        try:self.assertTrue(self.typed.automatic_page_plan(owned)['head_signal'])
        finally:self.drain(owned)

    def test_blocked_count_head_cannot_hide_a_due_scheduled_head(self):
        self.cover(self.target,'short');self.cover(self.target,'long')
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')
        token=self.grow();self.spent()
        # The Short has event-driven head demand. The long source has only its
        # independent scheduled head plus full debt; its head needs no local cap.
        self.admin.execute("""UPDATE public.channel_playlist_state SET count_reconcile_served_token=%s,
            count_reconcile_served_epoch=%s,head_signal_token=NULL,head_next_due_at=now(),
            full_reconcile_required_token=gen_random_uuid(),next_due_at=now()
            WHERE channel_id=%s AND video_type='long'""",(token,self.epoch,self.target))
        self.admin.execute('UPDATE public.discovery_budget_days SET event_used=event_limit WHERE project_id=%s',
            (self.budget.project,))
        status=runtime_status(self.owned,project_id=self.budget.project,settings=self.settings,interval_minutes=10,
            video_types=('long','short'))
        self.assertTrue(status['runnable'],status)

    def test_due_scheduled_head_does_not_spend_event_capacity_for_full_debt(self):
        self.cover(self.target,'short');self.spent()
        self.admin.execute("UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),next_due_at=now(),head_next_due_at=now() WHERE channel_id=%s AND video_type='short'",(self.target,))
        identity,payload=self.payload()
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=lambda *a,**kw:payload,
            discovery_admission=self.adapter)
        self.assertEqual(result['status'],'head_observed')
        self.assertEqual(self.admin.execute('SELECT event_used,full_used FROM public.discovery_budget_days WHERE project_id=%s',
            (self.budget.project,)).fetchone(),(0,1))

    # ---- the urgent allowance is used up: the list's ordinary work does not wait with it

    def enable(self):
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,'
                           'first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')

    def roomy(self):
        """An allowance in which a full scan can take more than one page per window."""
        budget=DiscoveryBudgetRepository(self.db,project_id='heads-'+str(uuid4()),settings=DiscoverySettings(),
                                         interval_minutes=10)
        return budget,InventoryDiscoveryAdmission(budget)

    def events_used_up(self,budget):
        if self.scalar('SELECT count(*) FROM public.discovery_budget_days WHERE project_id=%s',(budget.project,))==0:
            self.assertTrue(budget.reserve(str(uuid4()),bucket='general',full=True,event=False)['allowed'])
        self.admin.execute('UPDATE public.discovery_budget_days SET event_used=event_limit WHERE project_id=%s',
                           (budget.project,))

    def used(self,budget):
        return self.admin.execute('SELECT event_used,full_used,event_limit FROM public.discovery_budget_days '
                                  'WHERE project_id=%s',(budget.project,)).fetchone()

    def short(self,column):
        return self.scalar('SELECT '+column+" FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",
                           (self.target,))

    def refused(self,adapter):
        """One claim that must be refused without any request, then released as the dispatcher does."""
        owned=self.claim_source()
        forbidden=Mock(side_effect=AssertionError('a refused urgent check made a request'))
        try:
            with self.assertRaises(DiscoveryAdmissionDenied):
                run_typed_inventory_claim(repository=self.typed,claim=owned,fetch_page=forbidden,
                                          discovery_admission=adapter)
        finally:self.drain(owned)
        forbidden.assert_not_called()

    def periodic_due(self):
        self.admin.execute("""UPDATE public.channel_playlist_state SET head_last_attempt=jsonb_set(head_last_attempt,
            '{next_due_at}',to_jsonb(now()-interval '1 second')) WHERE channel_id=%s AND video_type='short'""",
            (self.target,))

    def test_waiting_signal_follows_the_periodic_pace_of_the_list_when_urgent_allowance_is_used_up(self):
        self.cover(self.target,'short');self.enable()
        token=self.grow();self.events_used_up(self.budget)
        served=self.short('count_reconcile_served_token')
        _,_,limit=self.used(self.budget)
        heads="SELECT count(*) FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND result->>'channel_id'=%s"
        # The start was read by the covering scan a moment ago: its next periodic time is ahead.
        self.assertGreater(self.scalar("SELECT (head_last_attempt->>'next_due_at')::timestamptz-now() FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)),timedelta(0))
        self.refused(self.adapter)
        # The refusal moved the scan clock; even a scan clock long overdue is not a reason for a free read.
        self.admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now()-interval '1 hour' WHERE channel_id=%s AND video_type='short'",(self.target,))
        self.refused(self.adapter)
        self.assertEqual(self.scalar(heads,(self.target,)),0)
        # The list's own periodic time comes: one read of the start, not charged, the signal not served.
        self.periodic_due()
        identity,payload=self.payload();seen=[]
        def head(playlist,**options):seen.append(options['page_token']);return payload
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=head,
                                         discovery_admission=self.adapter)
        self.assertEqual((result['status'],result['coverage'],result['inserted']),('head_observed','partial',1))
        self.assertEqual(seen,[None])
        self.assertEqual(self.used(self.budget)[0],limit)
        self.assertEqual(self.short('count_reconcile_served_token'),served)
        self.assertIsNotNone(self.short('head_signal_token'))
        self.assertIsNone(self.scalar("SELECT result->>'count_reconcile_token' FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND result->>'channel_id'=%s",(self.target,)))
        self.assertEqual(self.scalar(heads,(self.target,)),1)
        self.assertGreater(self.scalar("SELECT (head_last_attempt->>'next_due_at')::timestamptz-now() FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)),timedelta(0))
        # Until the next periodic time nothing is free: not a repeat, not a new count generation,
        # not the scan clock a refusal keeps moving.
        self.refused(self.adapter)
        newer=self.grow(12);self.assertNotEqual(newer,token)
        self.refused(self.adapter)
        self.admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now()-interval '1 hour' WHERE channel_id=%s AND video_type='short'",(self.target,))
        self.refused(self.adapter)
        self.assertEqual(self.scalar(heads,(self.target,)),1)
        # The allowance is back: the urgent check is made, charged and serves the signal.
        self.admin.execute('UPDATE public.discovery_budget_days SET event_used=0 WHERE project_id=%s',(self.budget.project,))
        _,payload=self.payload()
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=lambda *a,**kw:payload,
                                         discovery_admission=self.adapter)
        self.assertEqual(result['status'],'head_observed')
        self.assertEqual(self.used(self.budget)[0],1)
        self.assertEqual(self.short('count_reconcile_served_token'),newer)
        self.assertIsNone(self.short('head_signal_token'))

    def progressed_full(self,adapter):
        """A full check of the Short list with its first page done and the second saved."""
        self.cover(self.target,'short')
        self.admin.execute("UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),next_due_at=now() WHERE channel_id=%s AND video_type='short'",(self.target,))
        _,first=self.payload(next_token='page-two')
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=lambda *a,**kw:first,
                                         discovery_admission=adapter)
        self.assertEqual(result['status'],'running')
        return result['scan_id']

    def test_full_scan_under_way_takes_its_next_page_when_the_urgent_check_is_refused(self):
        budget,adapter=self.roomy()
        scan=self.progressed_full(adapter)
        self.grow();self.events_used_up(budget)
        before=self.used(budget)
        _,second=self.payload(next_token='page-three');seen=[]
        def page(playlist,**options):seen.append(options['page_token']);return second
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=page,
                                         discovery_admission=adapter)
        self.assertEqual((result['status'],result['scan_id']),('running',scan))
        self.assertEqual(seen,['page-two'])
        self.assertEqual(self.admin.execute("SELECT active_scan_id::text,resume_page_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.target,)).fetchone(),(scan,'page-three'))
        # The page was charged to the scan's own allowance; the urgent one stays used up and the signal waits.
        self.assertEqual(self.used(budget),(before[0],before[1]+1,before[2]))
        self.assertIsNotNone(self.short('head_signal_token'))
        self.assertEqual(self.scalar("SELECT count(*) FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND result->>'channel_id'=%s",(self.target,)),0)

    def test_failed_check_of_the_start_does_not_pause_a_scan_under_way(self):
        budget,adapter=self.roomy()
        scan=self.progressed_full(adapter)
        self.grow()
        def failed(*args,**kwargs):raise RuntimeError('synthetic source failure')
        result=run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=failed,
                                         discovery_admission=adapter)
        self.assertEqual(result['status'],'error')
        self.assertEqual(self.short("head_last_attempt->>'outcome'"),'error')
        self.assertGreater(self.short('head_next_due_at'),self.now())
        # The start is not read again until its retry time, but the saved page is not the start.
        owned=self.claim_source()
        plan=self.typed.automatic_page_plan(owned)
        self.assertFalse(plan['head_due']);self.assertTrue(plan['progressed'])
        _,second=self.payload(next_token='page-three');seen=[]
        def page(playlist,**options):seen.append(options['page_token']);return second
        result=run_typed_inventory_claim(repository=self.typed,claim=owned,fetch_page=page,discovery_admission=adapter)
        self.assertEqual((result['status'],result['scan_id']),('running',scan))
        self.assertEqual(seen,['page-two'])


    def due_for_measurement(self):
        self.periodic_due()
        self.admin.execute("""UPDATE public.channel_playlist_state SET next_due_at=now(),
            head_next_due_at=now(), source_next_after=now() WHERE channel_id=%s AND video_type='short'""",
            (self.target,))

    def measured_page(self, adapter, payload, *, missing=False):
        from list_observation import http_start
        from youtube_errors import YouTubePlaylistNotFoundError
        def fetch(playlist, **kwargs):
            http_start('general', 1, None)
            if missing:
                raise YouTubePlaylistNotFoundError('synthetic', confirmed_playlist_id=playlist)
            return payload
        return run_typed_inventory_claim(repository=self.typed,claim=self.claim_source(),fetch_page=fetch,
                 discovery_admission=adapter,playlist_not_found_errors=(YouTubePlaylistNotFoundError,))

    def test_report_separates_paid_empty_heads_and_weekly_coverage(self):
        from db.inventory.list_check_report import report
        self.cover(self.target, 'short')
        self.enable()
        _, adapter = self.roomy()
        start = self.now()
        self.due_for_measurement()
        self.measured_page(adapter, {'items': []})
        from db.discovery import operation
        with self.db.transaction(operation('test_list_period')) as tx:
            period = tx.cadence_snapshot.inventory_by_type.short.reconciliation_seconds
        self.admin.execute("""UPDATE public.channel_video_inventory_scans SET started_at=now()-make_interval(secs=>%s)
            WHERE channel_id=%s AND video_type='short' AND coverage='complete'""", (period+60,self.target))
        self.due_for_measurement()
        self.measured_page(adapter, {'items': []})
        value = report(self.db, start=start, end=self.now(), formats=('long','short'))
        groups = {g['reason']: g for g in value['groups']}
        self.assertEqual(set(groups), {'scheduled_head','scheduled_full'}, value)
        for g in groups.values():
            self.assertEqual((g['format'],g['general'],g['new_videos'],g['general_per_find']), ('short',1,0,None))
        self.assertEqual(groups['scheduled_full']['unchanged_full'], 1)

    def test_report_separates_repeat_absence_and_appearance(self):
        from db.inventory.list_check_report import report
        self.enable()
        _, adapter = self.roomy()
        self.missing()
        start = self.now()
        self.due_for_measurement()
        self.measured_page(adapter, None, missing=True)
        self.due_for_measurement()
        self.measured_page(adapter, self.payload()[1])
        value = report(self.db, start=start, end=self.now(), formats=('long','short'))
        self.assertEqual(len(value['groups']), 1, value)
        g = value['groups'][0]
        self.assertEqual((g['reason'],g['still_absent'],g['appeared'],g['new_videos'],g['general']),
                         ('absent_recheck',1,1,1,2))
        self.assertIsNotNone(g['mean_appearance_check_gap_seconds'])

    def test_manual_join_keeps_original_reason_across_window_boundary(self):
        from db.inventory.list_check_report import report
        from db.inventory.manual_check import request_check
        self.cover(self.target, 'short')
        self.enable()
        _, adapter = self.roomy()
        self.admin.execute("""UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),
            next_due_at=now() WHERE channel_id=%s AND video_type='short'""", (self.target,))
        r = self.measured_page(adapter, self.payload(next_token='two')[1])
        self.assertEqual(r['status'], 'running')
        start = self.now()
        job = request_check(self.db, self.target, 'short', self.epoch, None, str(uuid4()))
        self.assertTrue(job['joined'])
        r = self.measured_page(adapter, self.payload()[1])
        self.assertEqual(r['coverage'], 'complete')
        value = report(self.db, start=start, end=self.now(), formats=('long','short'))
        self.assertEqual(len(value['groups']), 1)
        group = value['groups'][0]
        self.assertEqual((group['reason'],group['settled_pages'],group['shared_pages'],group['general'],
                          group['deep_finds']), ('signal',1,1,1,1))

    def test_report_shared_manual_walk_matches_ledger_and_finds(self):
        import json
        from db.inventory.list_check_report import report
        from db.inventory.manual_check import request_check
        from list_observation import http_start
        self.cover(self.target, 'short')
        self.enable()
        _, adapter = self.roomy()
        first, page1 = self.payload(next_token='two')
        second, page2 = self.payload()
        self.admin.execute("INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind) VALUES(%s,%s,%s,'entry')",
                           (uuid4().hex*2, self.target, first))
        start = self.now()
        request_check(self.db, self.target, 'short', self.epoch, None, str(uuid4()))
        calls = []
        def fetch(playlist, *, page_token, **kwargs):
            previous = None
            calls.append(page_token)
            for attempt in range(1 if page_token else 2):
                http_start('general', 1, previous)
                previous = self.scalar("""INSERT INTO public.youtube_api_request_attempts(project_id,phase,endpoint,
                    quota_bucket,quota_units,retry_of_request_attempt_id) VALUES('test','typed_inventory_short',
                    'playlistItems','general',1,%s) RETURNING request_attempt_id""", (previous,))
            return page2 if page_token else page1
        for _ in range(2):
            result = run_typed_inventory_claim(repository=self.typed, claim=self.claim_source(), fetch_page=fetch,
                                                discovery_admission=adapter)
        self.assertEqual(calls, [None, 'two'])
        self.assertEqual(result['coverage'], 'complete')
        end = self.now()
        def read(start, end):
            return report(self.db, start=start, end=end, formats=('long','short'))
        value = read(start, end)
        json.dumps(value, allow_nan=False)
        self.assertEqual(value['ledger_minus_attributed_general'], 0)
        self.assertEqual(value['ledger'][0]['general'], 3)
        self.assertEqual(len(value['groups']), 1, value['groups'])
        group = value['groups'][0]
        self.assertEqual((group['reason'],group['format'],group['general'],group['http_retries']),
                         ('manual','short',3,1))
        self.assertEqual((group['settled_pages'],group['checks_finished'],group['full_coverages']), (2,1,1))
        self.assertEqual((group['new_videos'],group['already_announced'],group['deep_finds']), (2,1,1))
        self.assertEqual(group['general_per_find'], 1.5)
        self.assertEqual(read(start,end)['groups'], value['groups'])
        self.assertEqual(read(end,self.now())['groups'], [])
        for identity in (self.target, first, second):
            self.assertNotIn(identity, json.dumps(value))

if __name__=='__main__':unittest.main(verbosity=2)
