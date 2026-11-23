"""A denied full allowance cannot turn into an unbounded or unowned head."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'application'),str(Path(__file__).parent)]
from collection.inventory import run_typed_inventory_claim
from collection.inventory_admission import InventoryDiscoveryAdmission
from discovery_admission import DiscoveryAdmissionDenied, DiscoveryAdmissionUnsafe
from typed_playlist import typed_playlist_id
from youtube_errors import YouTubePlaylistNotFoundError
from youtube_playlist import fetch_playlist_items_page
from test_first_of_type_recheck import Budget
from test_shared_inventory_engine import Repository, claim, page, NOW


class HeadAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.repo=Repository()
        self.repo.head_observations=True
        self.plan=dict(flags=dict(full=True,event=False),head_due=True,head_signal=True,progressed=False)
        self.repo.automatic_page_plan=Mock(side_effect=lambda value:self.plan)
        self.repo.prepare_head=Mock()
        self.repo.settle_head=Mock(return_value=dict(status='head_observed',coverage='partial',provider_attempts=1,
            pages_processed=1,page_items=1,inserted=1,already_matching=0,conflicts=0,new_video_ids=[]))
        self.repo.discovery_admission_scope=Mock(return_value=dict(full=True,event=False))
        self.fetch=Mock(return_value=page())
        self.budget=Budget(full=0)

    def run_page(self, owned=None, *, controlled=True):
        return run_typed_inventory_claim(repository=self.repo,claim=owned or claim(),fetch_page=self.fetch,
            discovery_admission=InventoryDiscoveryAdmission(self.budget) if controlled else None,now=lambda:NOW)

    def test_exhausted_full_window_still_admits_exact_count_head_before_scan_begin(self):
        result=self.run_page()
        self.assertEqual((result['status'],result['coverage']),('head_observed','partial'))
        self.assertEqual(self.repo.events,[])
        self.repo.prepare_head.assert_called_once()
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(True,False),(False,True)])
        self.assertEqual((self.budget.full,self.budget.event),(0,2))

    def test_only_a_verified_http_404_on_the_requested_list_reaches_head_settlement_as_proof(self):
        owned=claim('short')
        playlist=typed_playlist_id(owned.channel_id,'short')
        def from_http(status, requested):
            response=Mock(status_code=status)
            response.json.return_value={'error':{'errors':[{'reason':'playlistNotFound'}]}}
            return fetch_playlist_items_page(requested, youtube_api_get=Mock(return_value=response),
                api_key=None,attempt_recorder=None,timeout_seconds=1,emit_event=Mock(),perf_counter=lambda:0)
        cases=(
            (lambda *a,**kw:from_http(404,playlist),'playlist_not_found',playlist),
            (lambda *a,**kw:from_http(404,typed_playlist_id(owned.channel_id,'long')),'playlist_not_found',None),
            (Mock(side_effect=YouTubePlaylistNotFoundError('legacy')),'playlist_not_found',None),
            (lambda *a,**kw:from_http(403,playlist),'error',None),
        )
        for fetch,status,expected in cases:
            with self.subTest(status=status,proof=expected):
                result=run_typed_inventory_claim(repository=self.repo,claim=owned,fetch_page=fetch,
                    discovery_admission=InventoryDiscoveryAdmission(Budget(full=0)),now=lambda:NOW,
                    playlist_not_found_errors=(YouTubePlaylistNotFoundError,))
                self.assertEqual(result['status'],status)
                self.assertEqual(self.repo.settle_head.call_args.kwargs['confirmed_missing_playlist'],expected)

    def test_v4_inactive_growth_uses_head_without_invented_admission(self):
        self.run_page(controlled=False)
        self.assertEqual(self.budget.calls,[])
        self.assertEqual(self.repo.events,[])
        self.fetch.assert_called_once()

    def test_progressed_full_uses_page_one_without_replacing_scan_or_cursor(self):
        self.plan['progressed']=True
        owned=claim(active_scan_id='f6300000-0000-4000-8000-000000000001',resume_page_token='saved-page')
        result=self.run_page(owned)
        self.assertEqual(result['scan_id'],owned.active_scan_id)
        self.assertIsNone(self.fetch.call_args.kwargs['page_token'])
        self.assertEqual(owned.resume_page_token,'saved-page')
        self.assertEqual(self.repo.events,[])
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(False,True)])

    def test_full_debt_alone_never_creates_head_request(self):
        self.plan.update(head_due=False,head_signal=False)
        with self.assertRaises(DiscoveryAdmissionDenied):self.run_page()
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()
        self.assertEqual(self.repo.events,[])

    def test_full_capacity_is_reserved_before_creating_new_scan(self):
        self.budget.full=1
        reserve=self.budget.reserve
        def ordered(*args,**kwargs):
            self.repo.events.append('admit')
            return reserve(*args,**kwargs)
        self.budget.reserve=ordered
        self.run_page()
        self.assertEqual(self.repo.events[:2],['admit','begin'])
        self.repo.prepare_head.assert_not_called()

    def test_provider_ban_never_falls_back_to_head(self):
        self.budget.reserve=Mock(return_value=dict(allowed=False,reason='provider_quota'))
        with self.assertRaises(DiscoveryAdmissionDenied):self.run_page()
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()

    def test_unknown_admission_never_means_spent_full_capacity(self):
        self.budget.reserve=Mock(side_effect=OSError('unknown'))
        with self.assertRaises(DiscoveryAdmissionUnsafe):self.run_page()
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()

    def test_inactive_count_head_with_existing_coverage_uses_event_capacity(self):
        self.plan['flags']=dict(full=False,event=False)
        self.run_page(claim(scan_kind='discovery'))
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(False,True)])

    def test_exhausted_event_capacity_cannot_be_bypassed_by_head_fallback(self):
        self.budget.event=0
        with self.assertRaises(DiscoveryAdmissionDenied):self.run_page()
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()

    def planned(self, **changes):
        """A covered list with a waiting count signal and no urgent allowance left."""
        self.plan=dict(flags=dict(full=False,event=False),head_due=True,head_signal=True,
                       head_scheduled=True,progressed=False)
        self.plan.update(changes)
        self.budget.event=0
        return claim(scan_kind='discovery')

    def test_due_periodic_check_runs_once_without_serving_the_waiting_signal(self):
        result=self.run_page(self.planned())
        self.assertEqual((result['status'],result['coverage']),('head_observed','partial'))
        self.assertIs(self.repo.prepare_head.call_args.kwargs['serve_signal'],False)
        self.fetch.assert_called_once()
        self.assertIsNone(self.fetch.call_args.kwargs['page_token'])
        self.assertEqual(self.repo.events,[])
        # The only reservation asked for was refused: nothing was charged.
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(False,True)])
        self.assertEqual((self.budget.full,self.budget.event),(0,0))

    def test_signal_alone_gets_no_head_once_the_urgent_allowance_is_used_up(self):
        for scheduled in (False,None,'true',1):
            with self.subTest(scheduled=scheduled):
                owned=self.planned(head_scheduled=scheduled)
                if scheduled is None:del self.plan['head_scheduled']
                with self.assertRaises(DiscoveryAdmissionDenied):self.run_page(owned)
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()
        self.assertEqual(self.repo.events,[])

    def test_admitted_urgent_head_still_serves_the_signal(self):
        owned=self.planned()
        self.budget.event=1
        self.run_page(owned)
        self.assertNotIn('serve_signal',self.repo.prepare_head.call_args.kwargs)
        self.assertEqual(self.budget.event,0)

    def test_provider_ban_on_the_urgent_head_runs_no_periodic_check(self):
        owned=self.planned()
        self.budget.reserve=Mock(return_value=dict(allowed=False,reason='provider_quota'))
        with self.assertRaises(DiscoveryAdmissionDenied):self.run_page(owned)
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()

    def test_running_full_scan_goes_on_when_its_urgent_head_is_refused(self):
        self.plan['progressed']=True
        self.budget.full,self.budget.event=1,0
        owned=claim(active_scan_id='f6300000-0000-4000-8000-000000000001',resume_page_token='saved-page')
        result=self.run_page(owned)
        self.assertEqual(result['scan_id'],owned.active_scan_id)
        self.assertEqual(self.fetch.call_args.kwargs['page_token'],'saved-page')
        self.assertEqual(self.repo.events,['begin','settle'])
        self.repo.prepare_head.assert_not_called()
        self.repo.discovery_admission_scope.assert_not_called()
        # Head refused, then the page under the scan's own allowance.
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(False,True),(True,False)])
        self.assertEqual(len({r[0] for r in self.budget.calls}),1)
        self.assertEqual((self.budget.full,self.budget.event),(0,0))

    def test_running_full_scan_waits_when_its_own_allowance_is_used_up_too(self):
        self.plan['progressed']=True
        self.budget.event=0
        owned=claim(active_scan_id='f6300000-0000-4000-8000-000000000001',resume_page_token='saved-page')
        with self.assertRaises(DiscoveryAdmissionDenied):self.run_page(owned)
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()
        self.assertEqual(self.repo.events,[])

    def test_signal_caused_full_scan_page_is_not_released_from_the_urgent_allowance(self):
        self.plan.update(progressed=True,flags=dict(full=True,event=True))
        self.budget.full,self.budget.event=1,0
        owned=claim(active_scan_id='f6300000-0000-4000-8000-000000000001',resume_page_token='saved-page')
        with self.assertRaises(DiscoveryAdmissionDenied):self.run_page(owned)
        self.fetch.assert_not_called()
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(False,True),(True,True)])
        self.assertEqual(self.budget.full,1)

    def test_running_discovery_scan_goes_on_when_its_urgent_head_is_refused(self):
        self.planned(progressed=True,head_scheduled=False)
        owned=claim(scan_kind='discovery',active_scan_id='f6300000-0000-4000-8000-000000000001',
                    resume_page_token='saved-page')
        result=self.run_page(owned)
        self.assertEqual(result['scan_id'],owned.active_scan_id)
        self.assertEqual(self.fetch.call_args.kwargs['page_token'],'saved-page')
        self.assertEqual(self.repo.events,['begin','settle'])
        self.repo.prepare_head.assert_not_called()
        # The refused urgent head is the only reservation: an ordinary scan page is not charged.
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(False,True)])
        self.assertEqual((self.budget.full,self.budget.event),(0,0))

    def test_signal_bound_discovery_scan_page_waits_for_the_urgent_allowance(self):
        self.planned(progressed=True,flags=dict(full=False,event=True))
        owned=claim(scan_kind='discovery',active_scan_id='f6300000-0000-4000-8000-000000000001',
                    resume_page_token='saved-page')
        with self.assertRaises(DiscoveryAdmissionDenied):self.run_page(owned)
        self.repo.prepare_head.assert_not_called()
        self.fetch.assert_not_called()
        self.assertEqual(self.repo.events,[])
        self.assertEqual([(r[2],r[3]) for r in self.budget.calls],[(False,True),(False,True)])

    def test_head_provider_receives_existing_trace_channel_identity(self):
        from pipeline_trace import capture
        seen=[]
        self.fetch.side_effect=lambda *args,**kwargs:(seen.append(dict(capture())) or page())
        self.run_page()
        self.assertEqual(seen[0]['channel_id'],claim().channel_id)
        self.assertEqual(seen[0]['reason'],'head')
        self.assertTrue(seen[0]['operation_id'])

    def test_explicit_initial_page_without_bind_callback_never_becomes_a_head(self):
        result=run_typed_inventory_claim(repository=self.repo,claim=claim(),fetch_page=self.fetch,
            automatic=False,now=lambda:NOW)
        self.assertEqual(result['status'],'completed')
        self.repo.automatic_page_plan.assert_not_called()
        self.repo.prepare_head.assert_not_called()
        self.assertEqual(self.repo.events,['begin','settle'])


if __name__=='__main__':unittest.main()
