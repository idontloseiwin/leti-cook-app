"""Disposable PG17 count cutover: stored deadlines, no-WebSub and late tokens.

Provider results are supplied only through production source receipt owners.
Never collected by the ordinary unittest discovery command.
"""
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from uuid import uuid4
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).parent)]
import research_query_pg as native
from db.channel.channel_sync import ChannelSyncRepository
from db.discovery_counts import settle_count_heads
from db.inventory.typed_inventory import TypedInventoryRepository


class FirstOfTypePostgres(unittest.TestCase):
    setUpClass=classmethod(native.NativeQueryTests.setUpClass.__func__)
    tearDownClass=classmethod(native.NativeQueryTests.tearDownClass.__func__)
    refresh=classmethod(native.NativeQueryTests.refresh.__func__)

    def scalar(self, statement, parameters=()):
        row=self.admin.execute(statement,parameters).fetchone()
        return None if row is None else row[0]

    def now(self):return self.scalar('SELECT clock_timestamp()')

    def setUp(self):
        self.channel='UC'+uuid4().hex[:22]
        self.channel_owner=ChannelSyncRepository(self.db)
        self.channel_owner.insert_channel(dict(channel_id=self.channel,title='Discovery synthetic source',
            user_input='https://www.youtube.com/channel/'+self.channel,
            initial_long_inventory_status='completed',initial_28d_stats_status='completed',video_count=10,
            channel_metadata_status='ok',channel_metadata_checked_at=(self.now()-timedelta(hours=1)).isoformat()),
            permission='accepted')
        self.epoch=self.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(self.channel,))
        self.scope=dict(channel_id=self.channel,control_epoch=self.epoch)
        self.typed=TypedInventoryRepository(self.db)
        self.typed.prepare_runtime(limit=1000,enable=True)
        self.admin.execute("""UPDATE public.channel_playlist_state SET status='ready',
            inventory_status='playlist_not_found',last_error_code='typed_playlist_not_found',
            source_checked_at=clock_timestamp()-interval '1 hour',
            next_due_at=clock_timestamp()+interval '21 days',source_next_after=clock_timestamp()+interval '21 days'
            WHERE channel_id=%s""",(self.channel,))
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')

    def count(self, count):
        at=self.now().isoformat()
        self.assertEqual(self.channel_owner.update_metadata_batch([dict(self.scope,values=dict(video_count=count,
            channel_metadata_status='ok',channel_metadata_checked_at=at,channel_video_count_changed_at=at))]),
            {self.channel:'updated'})
        return self.scalar('SELECT count_reconcile_token FROM public.channels WHERE channel_id=%s',(self.channel,))

    def claim(self, kind):
        return self.typed.claim_channels([self.channel],video_types=[kind],only_due=True,
            expected_channel_scopes=[self.scope],worker_id='discovery-source-regression',run_id=str(uuid4()))

    def begin(self, owned):
        scan_id=owned.active_scan_id or str(uuid4())
        self.typed.begin_scan(owned,scan_id=scan_id,scan_boundary_at=self.now())
        return scan_id

    def missing(self, owned, scan_id):
        return self.typed.fail_scan(owned,scan_id=scan_id,transition_id=str(uuid4()),
            expected_page_token=None,outcome='playlist_not_found',error_code='typed_playlist_not_found',
            source_checked_at=self.now())

    def test_without_websub_new_count_reads_first_format_immediately_after_old_404(self):
        self.assertEqual(self.scalar('SELECT count(*) FROM public.websub_subscriptions WHERE channel_id=%s',(self.channel,)),0)
        self.assertEqual(self.claim('short'),[])
        self.count(11)
        owned=self.claim('short')
        self.assertEqual(len(owned),1)
        self.assertEqual(owned[0].scan_kind,'initial_inventory')
        scan=self.begin(owned[0])
        self.assertEqual(self.typed.discovery_admission_scope(owned[0],scan_id=scan),{'full':True,'event':True})
        self.missing(owned[0],scan)
        self.assertEqual(self.claim('short'),[])

    def test_inactive_cutover_preserves_previously_early_source_deadline(self):
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=false WHERE singleton')
        self.admin.execute("""UPDATE public.channel_playlist_state SET next_due_at=clock_timestamp()-interval '1 second',
            source_next_after=clock_timestamp()+interval '1 hour' WHERE channel_id=%s AND video_type='short'""",(self.channel,))
        before=self.scalar("SELECT next_due_at FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.channel,))
        self.count(11)
        self.assertEqual(self.scalar("SELECT next_due_at FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.channel,)),before)
        owned=self.claim('short')[0];scan=self.begin(owned)
        self.assertEqual(self.typed.discovery_admission_scope(owned,scan_id=scan),{'full':True,'event':False})

    def test_late_count_generation_cannot_be_settled_by_older_source_receipt(self):
        old=self.count(11)
        from concurrent.futures import ThreadPoolExecutor
        from collection.inventory import run_typed_inventory_claim
        long=self.claim('long')[0]
        def provider(*args,**kwargs):
            # A real concurrent accepted metadata writer runs while the old
            # source HTTP callback is outstanding, outside every DB transaction.
            with ThreadPoolExecutor(max_workers=1) as executor:
                newer=executor.submit(self.count,12).result(timeout=5)
            self.assertNotEqual(old,newer)
            return {'items':[]}
        run_typed_inventory_claim(repository=self.typed,claim=long,fetch_page=provider)
        short=self.claim('short')[0];self.missing(short,self.begin(short))
        self.assertEqual(settle_count_heads(self.db,self.channel,video_types=('long','short')),
                         {'settled':False,'reason':'heads_pending'})
        self.assertIsNone(self.scalar('SELECT count_reconcile_settled_token FROM public.channels WHERE channel_id=%s',(self.channel,)))
        self.assertEqual(len(self.claim('long')),1)

    def test_disabled_preparation_pages_and_continuations_use_actual_full_cap(self):
        from dataclasses import replace
        from collection.inventory import run_typed_inventory_batch
        from collection.inventory_admission import InventoryDiscoveryAdmission
        from db.discovery_budget import DiscoveryBudgetRepository
        from discovery_policy import DiscoverySettings
        self.admin.execute('UPDATE public.discovery_runtime_control SET enabled=false WHERE singleton')
        self.admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now(),last_error_code=NULL WHERE channel_id=%s AND video_type='short'",(self.channel,))
        settings=replace(DiscoverySettings(),full_general_window_limit=2)
        project='discovery-source-'+uuid4().hex
        budget=DiscoveryBudgetRepository(self.db,project_id=project,settings=settings,interval_minutes=15)
        calls=[]
        def provider(*args,**kwargs):
            calls.append(kwargs.get('page_token'))
            return {'items':[{'contentDetails':{'videoId':'fxfull%05d'%len(calls),
                'videoPublishedAt':self.now().isoformat()}}], 'nextPageToken':'next-'+str(len(calls))}
        result=run_typed_inventory_batch(repository=self.typed,fetch_page=provider,limit=3,
            worker_id='discovery-preparation-budget',explicit_channel_ids=[self.channel],video_types=('short',),
            expected_channel_scopes=[self.scope],prepare=False,discovery_admission=InventoryDiscoveryAdmission(budget))
        self.assertEqual(calls,[None,'next-1'])
        self.assertTrue(result['discovery_deferred'])
        self.assertEqual(self.admin.execute('SELECT full_used,event_used FROM public.discovery_budget_days WHERE project_id=%s',(project,)).fetchone(),(2,0))
        self.assertEqual(self.scalar("SELECT resume_page_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(self.channel,)),'next-2')

    def test_empty_heads_conservatively_request_full_without_a_notification_tally(self):
        self.count(12)
        for kind in ('long','short'):
            owned=self.claim(kind)[0];self.missing(owned,self.begin(owned))
        result=settle_count_heads(self.db,self.channel,video_types=('long','short'))
        self.assertTrue(result['settled']);self.assertTrue(result['full_requested'])
        self.assertEqual(result['new_count'],0)
        self.assertIsNone(self.scalar("SELECT full_reconcile_required_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='stream'",(self.channel,)))
        # Full debt derived from this same count is not fresh source evidence
        # and cannot erase either source's just-observed missing-playlist delay.
        self.assertEqual(self.claim('long'),[])
        self.assertEqual(self.claim('short'),[])


if __name__=='__main__':unittest.main(verbosity=2)
