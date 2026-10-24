"""Real owner/service/scanner and worker threads; repository/provider doubles only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event,get_ident
from unittest.mock import Mock,patch
import sys,time,unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from collection.budgets import PageBudget
from collection.dispatch import Ack,Completion,Coordinator,DispatchBlocked,DuplicateWork,Prepared,Skipped
from collection.inventory_dispatch import InventoryPageUnit,InventoryPageReservations
from collection import inventory_dispatch as adapter
from collection.inventory_steps import InventoryPageResponse
from hot_runtime import HotRuntimeUnavailable
import collection_shutdown
from test_shared_inventory_engine import Repository,claim,page,CHANNEL,NOW,VIDEO


class GuardedRepository(Repository):
    def __init__(self):
        super().__init__();self.owner=get_ident();self.saved=[]
    def check(self):
        assert get_ident()==self.owner,'database call in worker'
    def begin_scan(self,*a,**kw):
        self.check();return super().begin_scan(*a,**kw)
    def known_membership_ids(self,*a,**kw):
        self.check();return super().known_membership_ids(*a,**kw)
    def settle_page(self,*a,**kw):
        self.check();self.saved.append(kw);return super().settle_page(*a,**kw)
    def fail_scan(self,*a,**kw):
        self.check();return super().fail_scan(*a,**kw)
    def defer_claims_after_daily_quota(self,*a,**kw):
        self.check();return super().defer_claims_after_daily_quota(*a,**kw)


class OwnerBudget(PageBudget):
    def __init__(self,limit):
        super().__init__(limit);self.owner=get_ident()
    def try_debit(self):
        assert get_ident()==self.owner,'shared budget debit in worker'
        return super().try_debit()


class InventoryDispatchTests(unittest.TestCase):
    def setUp(self):
        stopped=patch.object(collection_shutdown,'_requested',Event());stopped.start();self.addCleanup(stopped.stop)
        self.repo=GuardedRepository();self.budget=OwnerBudget(2)
        self.repo.database=Mock(spec=['drain_hot_collection_claims','abort_hot_collection_claims'])
        self.repo.database.drain_hot_collection_claims.return_value=self.drain_receipt()
        self.reservations=InventoryPageReservations(self.budget)
        self.calls=[];self.progress=Mock();self.claimed=Mock()
    @staticmethod
    def drain_receipt(**changes):
        receipt=dict(status='drained',remaining=0,playlist_requested=1,playlist_released=1,
            video_requested=0,video_released=0,operator_requested=0,operator_released=0)
        receipt.update(changes);return receipt
    def unit(self,kind='long',**options):
        owned=claim(kind)
        def provider(*a,**kw):
            self.assertNotEqual(get_ident(),self.repo.owner)
            self.calls.append((a,kw));return page()
        args=dict(key=kind,lane='auto',channel=CHANNEL,video_type=kind,
            claim_page=lambda:owned,fetch_page=provider,reservations=self.reservations,
            require_ready=Mock(),on_claimed=self.claimed,on_settled=self.progress,
            now=lambda:NOW)
        args.update(options)
        return InventoryPageUnit(self.repo,**args)
    def response(self,prepared):
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(prepared.fetch,Event()).result(timeout=5)
    def settle_response(self,unit,response):
        return unit.settle(Completion('response',value=response,fetch_started=True))
    def test_inventory_lease_bounds_match_existing_native_claim_owner(self):
        for value in (True, 30, 59, 3601):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.unit(lease_seconds=value)
        for value in (60, 3600):
            self.assertEqual(self.unit(lease_seconds=value).lease_seconds, value)
        self.assertEqual(self.repo.events, [])
        self.claimed.assert_not_called()

    def test_three_formats_keep_begin_bind_fetch_scanner_and_receipt_on_owner(self):
        for kind in ('long','short','stream'):
            with self.subTest(kind=kind):
                self.setUp()
                def bind(*args):
                    self.repo.check();self.repo.events.append('bind');return True
                unit=self.unit(kind,before_dispatch=bind)
                prepared=unit.prepare();self.assertEqual(self.repo.events,['begin','bind'])
                self.assertEqual((self.budget.attempts,self.reservations.reserved),(0,1))
                response=self.response(prepared)
                self.assertEqual((self.budget.attempts,self.repo.events),(0,['begin','bind']))
                self.assertIsInstance(self.settle_response(unit,response),Ack)
                self.assertEqual(self.repo.events,['begin','bind','settle'])
                self.assertEqual(unit.summary['canonical_video_ids'],[VIDEO])
                self.assertEqual((self.budget.attempts,self.reservations.reserved),(1,0))
                self.progress.assert_called_once();self.claimed.assert_called_once()
    def test_reservations_prevent_overspend_and_unsent_releases_capacity(self):
        self.reservations=InventoryPageReservations(OwnerBudget(1))
        first=self.unit();second_claim=Mock(return_value=claim('short'))
        second=self.unit('short',claim_page=second_claim)
        first.prepare();self.assertIsInstance(second.prepare(),Skipped);second_claim.assert_not_called()
        self.assertIsInstance(first.settle(Completion('cancelled')),Ack)
        self.repo.database.drain_hot_collection_claims.assert_called_once_with(
            video_claims=[],operator_claims=[],playlist_claims=[dict(channel_id=CHANNEL,
                video_type='long',lease_token=first.claim.lease_token)])
        self.repo.database.abort_hot_collection_claims.assert_not_called()
        self.assertEqual((self.reservations.budget.attempts,self.reservations.reserved),(0,0))
        self.assertIsInstance(self.unit('stream').prepare(),Prepared)
    def test_zero_budget_does_not_claim_begin_or_bind(self):
        self.reservations=InventoryPageReservations(OwnerBudget(0))
        claim_page,bind=Mock(),Mock();unit=self.unit(claim_page=claim_page,before_dispatch=bind)
        self.assertIsInstance(unit.prepare(),Skipped)
        claim_page.assert_not_called();bind.assert_not_called();self.assertEqual(self.repo.events,[])
    def test_source_clock_is_response_clock_not_later_settlement(self):
        received=NOW+timedelta(seconds=3);clock=Mock(side_effect=[NOW,received])
        unit=self.unit(now=clock);prepared=unit.prepare();response=self.response(prepared)
        clock.side_effect=lambda:NOW+timedelta(hours=5)
        self.settle_response(unit,response)
        self.assertEqual(self.repo.saved[0]['source_checked_at'],received)
        self.assertEqual(clock.call_count,2)
    def test_failed_response_clock_uses_conservative_prefetch_clock(self):
        unit=self.unit(now=Mock(side_effect=[NOW,RuntimeError('clock unavailable')]))
        self.settle_response(unit,self.response(unit.prepare()))
        self.assertEqual(self.repo.saved[0]['source_checked_at'],NOW)
    def test_invalid_page_and_provider_failure_use_original_failure_owner(self):
        for payload in ({'items':[{}]},TimeoutError('private payload')):
            with self.subTest(kind=type(payload).__name__):
                self.setUp();provider=Mock(side_effect=payload) if isinstance(payload,Exception) else Mock(return_value=payload)
                unit=self.unit(fetch_page=provider)
                self.assertIsInstance(self.settle_response(unit,self.response(unit.prepare())),Ack)
                self.assertEqual(unit.summary['status'],'error')
                self.assertEqual(self.repo.events[-1][0],'fail')
                self.assertEqual(self.budget.attempts,1)
    def test_discovery_boundary_validation_stays_on_owner(self):
        self.repo.known_membership_ids=lambda *a,**kw:(self.repo.check() or {VIDEO})
        unit=self.unit(claim_page=lambda:claim(scan_kind='discovery'))
        self.settle_response(unit,self.response(unit.prepare()))
        self.assertTrue(unit.summary['boundary_found']);self.assertEqual(unit.summary['page_items'],1)
    def test_replayed_bind_holds_claim_and_reservation_without_dispatch(self):
        unit=self.unit(before_dispatch=lambda *args:False)
        with self.assertRaisesRegex(RuntimeError,'prepare_unconfirmed'):unit.prepare()
        self.assertEqual(self.calls,[]);self.assertEqual(self.reservations.reserved,1)
        self.assertEqual(self.budget.attempts,0)
        with self.assertRaisesRegex(RuntimeError,'prepare_repeated'):unit.prepare()
    def test_unknown_begin_never_releases_or_replays(self):
        self.repo.begin_scan=Mock(side_effect=OSError('unknown begin commit'));unit=self.unit()
        with self.assertRaises(OSError):unit.prepare()
        with self.assertRaisesRegex(RuntimeError,'prepare_repeated'):unit.prepare()
        self.repo.begin_scan.assert_called_once();self.assertEqual(self.reservations.reserved,1)
    def test_unconfirmed_or_failed_drain_retains_credit_and_reservation(self):
        for result in (self.drain_receipt(status='aborted'),self.drain_receipt(remaining=1),
                       self.drain_receipt(remaining=True),OSError('lost drain ack')):
            with self.subTest(kind=type(result).__name__):
                self.setUp();unit=self.unit()
                drain=self.repo.database.drain_hot_collection_claims
                if isinstance(result,Exception):drain.side_effect=result
                else:drain.return_value=result
                with ThreadPoolExecutor(max_workers=1) as executor:
                    coordinator=Coordinator(executor,1);coordinator.offer(unit.work)
                    # Arrange a known HOT denial after acknowledged begin.
                    unit.require_ready.side_effect=[None,HotRuntimeUnavailable()]
                    with self.assertRaises(DispatchBlocked):
                        coordinator.step()
                    self.assertEqual(coordinator.snapshot().held,1)
                    with self.assertRaises(DispatchBlocked):coordinator.step()
                drain.assert_called_once();self.progress.assert_not_called()
                self.assertEqual((self.reservations.reserved,self.budget.attempts),(1,0))
    def test_verified_token_absence_allows_zero_newly_released_count(self):
        self.repo.database.drain_hot_collection_claims.return_value=self.drain_receipt(playlist_released=0)
        unit=self.unit();unit.prepare()
        self.assertIsInstance(unit.settle(Completion('expired')),Ack)
        self.assertEqual((self.reservations.reserved,self.budget.attempts),(0,0))
        self.assertEqual(unit.summary['stopped_reason'],'expired')
    def test_unknown_attempt_flag_keeps_reservation_without_charge(self):
        unit=self.unit();unit.prepare()
        with self.assertRaisesRegex(RuntimeError,'attempt_unknown'):
            self.settle_response(unit,InventoryPageResponse(attempted=None))
        self.assertEqual((self.reservations.reserved,self.budget.attempts),(1,0))
        self.assertEqual(self.repo.events,['begin']);self.progress.assert_not_called()
    def test_invalid_scope_is_rejected_before_claim(self):
        for changes in (dict(channel='bad'),dict(video_type='unknown')):
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'config_invalid'):
                self.unit(**changes)
        self.assertEqual(self.repo.events,[])
    def test_unknown_write_holds_credit_and_charges_actual_attempt_once(self):
        self.repo.settle_page=Mock(side_effect=OSError('unknown write'));unit=self.unit()
        with ThreadPoolExecutor(max_workers=1) as executor:
            coordinator=Coordinator(executor,1);coordinator.offer(unit.work);coordinator.step()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                try:coordinator.step()
                except DispatchBlocked:break
                coordinator.wait(.01)
            else:self.fail('unknown write not blocked')
            self.assertEqual(coordinator.snapshot().held,1)
            with self.assertRaises(DispatchBlocked):coordinator.step()
        self.assertEqual(self.budget.attempts,1);self.repo.settle_page.assert_called_once()
        self.progress.assert_not_called()
    def test_unknown_progress_never_replays_confirmed_page(self):
        self.progress.side_effect=OSError('progress ack lost');unit=self.unit();response=self.response(unit.prepare())
        with self.assertRaises(OSError):self.settle_response(unit,response)
        with self.assertRaisesRegex(RuntimeError,'settle_repeated'):self.settle_response(unit,response)
        self.assertEqual(len(self.repo.saved),1);self.assertEqual(self.budget.attempts,1)
    def test_cancel_after_started_response_settles_page_without_abort(self):
        unit=self.unit();response=self.response(unit.prepare())
        result=unit.settle(Completion('response',value=response,fetch_started=True,cancel_requested=True))
        self.assertIsInstance(result,Ack)
        self.repo.database.drain_hot_collection_claims.assert_not_called()
        self.assertEqual(len(self.repo.saved),1)
    def test_daily_quota_current_and_explicitly_cancelled_sibling_use_deferral_owner(self):
        class YouTubeDailyQuotaExceededError(RuntimeError):pass
        first=self.unit(fetch_page=Mock(side_effect=YouTubeDailyQuotaExceededError()))
        second=self.unit('short');prepared=first.prepare();second.prepare()
        self.assertIsInstance(self.settle_response(first,self.response(prepared)),Ack)
        self.assertEqual(first.summary['status'],'quota_deferred')
        self.assertEqual(second.phase,'prepared') # no inferred sibling acknowledgement
        self.assertIsInstance(second.settle(Completion('cancelled')),Ack)
        self.repo.database.drain_hot_collection_claims.assert_not_called()
        self.assertEqual(second.summary['provider_attempts'],0)
        self.assertEqual([event[0] for event in self.repo.events if isinstance(event,tuple)],['defer','defer'])
        self.assertEqual(self.budget.attempts,1);self.assertEqual(self.reservations.reserved,0)
        third_claim=Mock();self.assertIsInstance(self.unit('stream',claim_page=third_claim).prepare(),Skipped)
        third_claim.assert_not_called()
    def test_sibling_withdrawn_after_quota_stop_defers_instead_of_failing_list(self):
        # RR-23: a started sibling whose request the limiter withdraws after the
        # quota stop received no page. It keeps its checkpoint and waits.
        from provider_admission import AdmissionCancelled
        class YouTubeDailyQuotaExceededError(RuntimeError):pass
        first=self.unit(fetch_page=Mock(side_effect=YouTubeDailyQuotaExceededError()))
        second=self.unit('short',fetch_page=Mock(side_effect=AdmissionCancelled('provider_request_cancelled')))
        prepared_first=first.prepare();prepared_second=second.prepare()
        self.assertIsInstance(self.settle_response(first,self.response(prepared_first)),Ack)
        result=self.settle_response(second,self.response(prepared_second))
        self.assertIsInstance(result,Ack)
        self.assertEqual(second.summary['status'],'quota_deferred')
        self.assertEqual(second.summary['provider_attempts'],0)
        self.assertEqual([e[0] for e in self.repo.events if isinstance(e,tuple)],['defer','defer'])
        self.assertEqual(self.repo.saved,[])
        self.repo.database.drain_hot_collection_claims.assert_not_called()
        self.assertEqual(self.budget.attempts,1);self.assertEqual(self.reservations.reserved,0)
    def test_request_withdrawn_without_quota_stop_releases_claim_without_failure(self):
        from provider_admission import AdmissionCancelled
        unit=self.unit(fetch_page=Mock(side_effect=AdmissionCancelled('provider_admission_cancelled')))
        result=self.settle_response(unit,self.response(unit.prepare()))
        self.assertIsInstance(result,Ack)
        self.assertEqual(unit.summary['status'],'cancelled')
        self.assertEqual(unit.summary['provider_attempts'],0)
        self.assertFalse([e for e in self.repo.events if isinstance(e,tuple) and e[0]=='fail'])
        self.assertEqual(self.repo.saved,[])
        self.repo.database.drain_hot_collection_claims.assert_called_once()
        self.assertEqual(self.budget.attempts,0);self.assertEqual(self.reservations.reserved,0)
    def test_unconfirmed_quota_deferral_keeps_credit_and_stops_new_reservations(self):
        class YouTubeDailyQuotaExceededError(RuntimeError):pass
        self.repo.defer_claims_after_daily_quota=Mock(return_value={'status':'deferred','active_scan_state_preserved':True,'requested':1,'released':0,'rejected':1})
        unit=self.unit(fetch_page=Mock(side_effect=YouTubeDailyQuotaExceededError()))
        with self.assertRaisesRegex(RuntimeError,'quota_receipt_unconfirmed'):
            self.settle_response(unit,self.response(unit.prepare()))
        with self.assertRaisesRegex(RuntimeError,'settle_repeated'):unit.settle(Completion('cancelled'))
        self.progress.assert_not_called();self.assertIsNotNone(self.reservations.quota)
        self.assertEqual(self.budget.attempts,1)
    def test_scope_mismatch_does_not_adopt_or_abort_foreign_claim(self):
        unit=self.unit(claim_page=lambda:claim('short'))
        with self.assertRaisesRegex(RuntimeError,'scope_mismatch'):unit.prepare()
        self.repo.database.drain_hot_collection_claims.assert_not_called()
        self.assertEqual(self.repo.events,[])
    def test_playlist_exclusion_prevents_next_page_before_ack(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            coordinator=Coordinator(executor,2);coordinator.offer(self.unit().work)
            with self.assertRaises(DuplicateWork):coordinator.offer(self.unit(key='second-page').work)

if __name__=='__main__':unittest.main(verbosity=2)
