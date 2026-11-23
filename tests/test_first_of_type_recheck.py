"""Discovery activation: durable page budgets and count settlement, without count-based deferral."""
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import Mock, patch
import sys
import unittest
sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'application'), str(Path(__file__).parent)]
from collection.budgets import PageBudget
from collection.dispatch import Completion, Skipped
from collection.inventory_admission import InventoryDiscoveryAdmission
from collection.inventory_dispatch import InventoryPageUnit, InventoryPageReservations
from collection.inventory import run_typed_inventory_claim, run_typed_inventory_batch
from discovery_admission import DiscoveryAdmissionDenied, DiscoveryAdmissionUnsafe
from db.inventory.typed_inventory import TypedInventoryRepository
from db.inventory import inventory_priority, inventory_claims, inventory_candidates
from test_shared_inventory_engine import Repository, claim, page, CHANNEL, NOW
import collection_shutdown


class Budget:
    def __init__(self, full=3, event=3):
        self.full, self.event = full, event
        self.calls, self.owner = [], get_ident()
        self.denied = False
    def reserve(self, token, *, bucket, full, event):
        assert get_ident() == self.owner
        self.calls.append((token, bucket, full, event))
        if self.denied or (full and not self.full) or (event and not self.event):
            return {'allowed':False, 'reason':'window_budget'}
        self.full -= int(full); self.event -= int(event)
        return {'allowed':True}
    def begin_dispatch(self, token):
        assert get_ident() == self.owner
        return True
    def note_exhausted(self, bucket):
        assert get_ident() == self.owner
        self.denied = True


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.stop = patch.object(collection_shutdown, '_requested', Event())
        self.stop.start(); self.addCleanup(self.stop.stop)
        self.repo = Repository()
        self.repo.database = Mock(spec=['drain_hot_collection_claims'])
        self.repo.database.drain_hot_collection_claims.return_value = dict(status='drained',remaining=0,
            playlist_requested=1,playlist_released=1,video_requested=0,video_released=0,
            operator_requested=0,operator_released=0)
        self.repo.discovery_admission_scope = Mock(return_value={'full':True,'event':False})
        self.budget = Budget()
        self.admission = InventoryDiscoveryAdmission(self.budget)

    def run_page(self, owned=None, payload=None):
        return run_typed_inventory_claim(repository=self.repo, claim=owned or claim(),
            fetch_page=Mock(return_value=payload or page()), page_budget=PageBudget(1),
            discovery_admission=self.admission, now=lambda:NOW)

    def test_disabled_preparation_and_automatic_first_format_spend_full(self):
        for kind in ('long', 'short'):
            result = self.run_page(claim(kind))
            self.assertEqual(result['provider_attempts'], 1)
        self.assertEqual([(x[2],x[3]) for x in self.budget.calls], [(True,False)]*2)
        self.assertEqual((self.budget.full,self.budget.event),(1,3))

    def test_every_continuation_uses_same_durable_window_budget_across_passes(self):
        first = self.run_page(payload=page('next'))
        second = self.run_page(claim(active_scan_id=first['scan_id'],resume_page_token='next'), page('last'))
        self.run_page(claim(active_scan_id=second['scan_id'],resume_page_token='last'))
        with self.assertRaises(DiscoveryAdmissionDenied): self.run_page()
        self.assertEqual(len(self.repo.receipts),3)
        self.assertEqual(self.budget.full,0)
        self.assertEqual(len({x[0] for x in self.budget.calls}),4)

    def test_signal_full_debits_both_atomically_without_duplicate_http(self):
        self.repo.discovery_admission_scope.return_value={'full':True,'event':True}
        self.budget.event=1
        self.run_page()
        with self.assertRaises(DiscoveryAdmissionDenied): self.run_page()
        self.assertEqual(len(self.repo.receipts),1)
        self.assertEqual((self.budget.full,self.budget.event),(2,0))

    def test_count_head_spends_event_but_quiet_head_uses_neither(self):
        self.repo.discovery_admission_scope.return_value={'full':False,'event':True}
        self.run_page(claim(scan_kind='discovery'))
        self.repo.discovery_admission_scope.return_value={'full':False,'event':False}
        self.run_page(claim(scan_kind='discovery'))
        self.assertEqual([(x[2],x[3]) for x in self.budget.calls],[(False,True)])
        self.assertEqual(len(self.repo.receipts),2)

    def test_refusal_releases_exact_lease_without_provider_or_source_failure(self):
        self.budget.full=0
        fetched,settled=Mock(return_value=page()),Mock()
        unit=InventoryPageUnit(self.repo,key='one',lane='auto',channel=CHANNEL,video_type='long',
            claim_page=lambda:claim(),fetch_page=fetched,reservations=InventoryPageReservations(PageBudget(1)),
            require_ready=lambda:None,on_claimed=lambda value:None,on_settled=settled,
            discovery_admission=self.admission,now=lambda:NOW)
        self.assertIsInstance(unit.prepare(),Skipped)
        fetched.assert_not_called()
        self.assertEqual(self.repo.events,['begin'])
        self.assertEqual(settled.call_args.args[0]['status'],'discovery_deferred')
        self.assertEqual(unit.reservations.budget.attempts,0)
        self.assertFalse(unit.summary['yield_requested'])

    def test_grant_expiring_while_waiting_never_becomes_source_failure_or_http_charge(self):
        fetched=Mock(side_effect=DiscoveryAdmissionDenied({'reason':'grant_expired'}))
        unit=InventoryPageUnit(self.repo,key='expiry',lane='auto',channel=CHANNEL,video_type='long',
            claim_page=lambda:claim(),fetch_page=fetched,reservations=InventoryPageReservations(PageBudget(1)),
            require_ready=lambda:None,on_claimed=lambda value:None,on_settled=lambda value:None,
            discovery_admission=self.admission,now=lambda:NOW)
        unit.prepare()
        response=unit.fetch(Event())
        unit.settle(Completion('response',value=response,fetch_started=True))
        self.assertEqual(unit.summary['status'],'discovery_deferred')
        self.assertEqual(unit.summary['discovery_wait_reason'],'grant_expired')
        self.assertEqual(unit.reservations.budget.attempts,0)
        self.assertEqual(self.repo.receipts,[])
        self.assertEqual(self.budget.full,2)  # issued capacity is never refunded

    def test_unknown_final_admission_read_stops_without_provider_attempt_or_source_failure(self):
        from quota_buckets import QuotaStateUnavailable
        unit=InventoryPageUnit(self.repo,key='unknown',lane='auto',channel=CHANNEL,video_type='long',
            claim_page=lambda:claim(),fetch_page=Mock(side_effect=QuotaStateUnavailable()),
            reservations=InventoryPageReservations(PageBudget(1)),require_ready=lambda:None,
            on_claimed=lambda value:None,on_settled=lambda value:None,
            discovery_admission=self.admission,now=lambda:NOW)
        unit.prepare()
        unit.settle(Completion('response',value=unit.fetch(Event()),fetch_started=True))
        self.assertEqual(unit.reservations.budget.attempts,0)
        self.assertEqual(self.repo.events,['begin'])
        self.repo.database.drain_hot_collection_claims.assert_called_once()
        self.assertTrue(unit.summary['quota_control_unknown'])
        self.assertEqual(unit.summary['stopped_reason'],'quota_state_unavailable')
        self.assertFalse(unit.summary['yield_requested'])
        self.assertFalse(unit.summary.get('settlement_unknown'))

    def test_unknown_budget_write_cannot_dispatch_or_fall_back(self):
        self.budget.reserve=Mock(side_effect=OSError('unknown'))
        with self.assertRaises(DiscoveryAdmissionUnsafe):self.run_page()
        with self.assertRaises(DiscoveryAdmissionUnsafe):self.run_page()
        self.assertEqual(self.repo.receipts,[])
        self.budget.reserve.assert_called_once()

    def test_sequential_learned_ban_defers_exact_owner_without_counting_http(self):
        from quota_buckets import remembered_refusal
        owned=claim()
        self.repo.claim_pass=Mock(return_value=[owned])
        page_budget=PageBudget(1)
        result=run_typed_inventory_batch(repository=self.repo,worker_id='known-ban',limit=1,
            prepare=False,page_budget=page_budget,discovery_admission=self.admission,now=lambda:NOW,
            fetch_page=Mock(side_effect=remembered_refusal('general')))
        self.assertEqual((result['errors'],result['processed'],result['provider_attempts']),(0,0,0))
        self.assertTrue(result['general_quota_exhausted'])
        self.assertFalse(result.get('settlement_unknown'))
        self.assertEqual(result['quota_deferred'],1)
        self.assertEqual(page_budget.attempts,0)
        self.assertEqual(self.repo.receipts,[])
        self.assertEqual(self.repo.events[1][0:2],('defer',[owned]))
        self.assertFalse(self.budget.denied)  # already persisted; no new refusal write

    def test_controlled_sequential_work_without_discovery_adapter_also_spends_no_attempt(self):
        from quota_buckets import remembered_refusal
        self.repo.claim_pass=Mock(return_value=[claim()])
        page_budget=PageBudget(1)
        with patch('quota_buckets.control_active',return_value=True):
            result=run_typed_inventory_batch(repository=self.repo,worker_id='controlled-ban',limit=1,
                prepare=False,page_budget=page_budget,now=lambda:NOW,
                fetch_page=Mock(side_effect=remembered_refusal('general')))
        self.assertEqual((result['errors'],result['provider_attempts'],result['quota_deferred']),(0,0,1))
        self.assertEqual(page_budget.attempts,0)

    def test_sequential_unknown_control_has_no_source_failure_and_requires_exact_drain(self):
        from quota_buckets import QuotaStateUnavailable
        self.repo.claim_pass=Mock(return_value=[claim()])
        result=run_typed_inventory_batch(repository=self.repo,worker_id='unknown-control',limit=1,
            prepare=False,discovery_admission=self.admission,now=lambda:NOW,
            fetch_page=Mock(side_effect=QuotaStateUnavailable()))
        self.assertEqual((result['errors'],result['provider_attempts']),(1,0))
        self.assertTrue(result['quota_control_unknown'])
        self.assertEqual(result['stopped_reason'],'quota_state_unavailable')
        self.assertFalse(result.get('settlement_unknown'))
        self.assertEqual(self.repo.events,['begin'])
        self.repo.database.drain_hot_collection_claims.assert_called_once()

    def test_unknown_control_with_unknown_drain_keeps_page_credit_and_owner_fenced(self):
        from quota_buckets import QuotaStateUnavailable
        self.repo.database.drain_hot_collection_claims.side_effect=OSError('lost drain acknowledgement')
        unit=InventoryPageUnit(self.repo,key='unknown-drain',lane='auto',channel=CHANNEL,video_type='long',
            claim_page=lambda:claim(),fetch_page=Mock(side_effect=QuotaStateUnavailable()),
            reservations=InventoryPageReservations(PageBudget(1)),require_ready=lambda:None,
            on_claimed=lambda value:None,on_settled=Mock(),discovery_admission=self.admission,now=lambda:NOW)
        unit.prepare()
        with self.assertRaises(OSError):unit.settle(Completion('response',value=unit.fetch(Event()),fetch_started=True))
        self.assertEqual(unit.reservations.reserved,1)
        unit.on_settled.assert_not_called()
        self.assertEqual(self.repo.receipts,[])

    def test_captured_scan_generations_not_new_channel_token_choose_the_debit(self):
        db=Mock(); db.read_row.return_value={'full':True,'event':False}
        repository=TypedInventoryRepository(db); owned=claim()
        self.assertEqual(repository.discovery_admission_scope(owned,scan_id='captured'),{'full':True,'event':False})
        sql=db.read_row.call_args.args[1]
        self.assertIn('s.count_reconcile_token',sql)
        self.assertNotIn('public.channels',sql)
        self.assertIn('p.lease_token=%s::uuid',sql)
        self.assertIn('s.full_reconcile_token',sql)

    def test_cutover_removes_legacy_writers_without_rewriting_early_due(self):
        self.assertFalse(hasattr(inventory_priority,'arm_missing_playlist_rechecks'))
        self.assertFalse(hasattr(TypedInventoryRepository,'settle_missing_playlist_rechecks'))
        self.assertIn('d.next_due_at<=now()',inventory_claims._CANDIDATE_PREDICATE)
        self.assertIn(inventory_claims._SIGNAL_DUE,inventory_candidates._PAGE)
        self.assertNotIn('make_interval(secs=>%(gap)s)',inventory_claims._SIGNAL_DUE)


if __name__=='__main__':unittest.main()
