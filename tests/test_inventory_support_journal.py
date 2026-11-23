"""Real page adapter/threads: passive evidence never supplies an owner ACK."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import pipeline_trace
import support_journal as journal
import test_inventory_dispatch as fixture
from collection.dispatch import Ack, Skipped
from collection.inventory import run_typed_inventory_claim
from discovery_admission import DiscoveryAdmissionDenied
from test_shared_inventory_engine import Repository, claim, page, CHANNEL, VIDEO, NOW


class InventoryJournalTests(unittest.TestCase):
    def setUp(self):
        journal.close()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(journal.close)
        self.path = Path(folder.name) / 'journal.jsonl'
        journal.initialize(environ={}, path=self.path, key=b'j' * 32)
        self.owner = fixture.InventoryDispatchTests()
        self.owner.setUp()
        self.addCleanup(self.owner.doCleanups)

    def events(self):
        self.assertTrue(journal._emitter.flush(1))
        return journal.snapshot()['events']

    def outcomes(self):
        return [(item['operation'], item['status']) for item in self.events()]

    def dispatch(self, unit):
        return self.owner.settle_response(unit, self.owner.response(unit.prepare()))

    def head_repository(self):
        repo = self.owner.repo
        repo.head_observations = True
        repo.automatic_page_plan = Mock(return_value=dict(flags=dict(full=False, event=False),
            head_due=True, head_signal=True, progressed=False))
        repo.prepare_head = Mock()
        repo.settle_head = Mock(return_value=dict(status='head_observed', coverage='partial',
            provider_attempts=1, pages_processed=1, page_items=1, inserted=1,
            already_matching=0, conflicts=0, new_video_ids=[VIDEO]))
        return repo

    def test_count_full_page_links_source_provider_writer_and_ack_without_private_state(self):
        owned = claim(resume_page_token='PRIVATE-PAGE-TOKEN')
        def provider(*args, **kwargs):
            pipeline_trace.event('http_started', 'started', request_attempt_id='b' * 32)
            pipeline_trace.event('http_finished', 'received', request_attempt_id='b' * 32)
            return page()
        pipeline_trace.event('channel_count_change', 'confirmed', channel_id=CHANNEL)
        unit = self.owner.unit(claim_page=lambda: owned, fetch_page=provider)
        self.assertIsInstance(self.dispatch(unit), Ack)
        events = self.events()
        self.assertEqual([(e['operation'], e['status']) for e in events], [
            ('signal', 'accepted'), ('claim', 'accepted'), ('claim', 'prepared'),
            ('provider', 'started'), ('provider', 'received'), ('writer', 'started'),
            ('writer', 'confirmed'), ('claim', 'confirmed')])
        self.assertEqual({e['channel_ref'] for e in events}, {journal.reference('channel', CHANNEL)})
        self.assertEqual(len({e['work_ref'] for e in events[1:]}), 1)
        self.assertEqual({e['operation_id'] for e in events[2:]}, {unit.prepared.identity['transition_id'].replace('-', '')})
        encoded = json.dumps(events)
        for secret in (CHANNEL, VIDEO, owned.lease_token, 'PRIVATE-PAGE-TOKEN', owned.playlist_id):
            self.assertNotIn(secret, encoded)
        self.assertEqual((self.owner.budget.attempts, self.owner.reservations.reserved), (1, 0))

    def test_head_has_known_writer_receipt_then_descriptor_ack(self):
        repo = self.head_repository()
        unit = self.owner.unit()
        self.assertIsInstance(self.dispatch(unit), Ack)
        self.assertEqual(self.outcomes(), [('claim', 'accepted'), ('claim', 'prepared'),
            ('writer', 'started'), ('writer', 'confirmed'), ('claim', 'confirmed')])
        self.assertEqual(repo.events, [])
        repo.settle_head.assert_called_once()

    def test_unknown_head_write_never_reports_success_or_releases_owner_credit(self):
        repo = self.head_repository()
        repo.settle_head.side_effect = OSError('PRIVATE WRITE PAYLOAD')
        unit = self.owner.unit()
        with self.assertRaises(OSError):
            self.dispatch(unit)
        outcomes = self.outcomes()
        self.assertIn(('writer', 'unknown'), outcomes)
        self.assertIn(('claim', 'unknown'), outcomes)
        self.assertNotIn(('writer', 'confirmed'), outcomes)
        self.assertNotIn(('claim', 'confirmed'), outcomes)
        self.assertNotIn('PRIVATE WRITE PAYLOAD', self.path.read_text())
        self.owner.progress.assert_not_called()
        self.assertEqual(self.owner.budget.attempts, 1)

    def test_malformed_full_receipt_is_unknown_not_confirmed(self):
        settle = self.owner.repo.settle_page
        def wrong_receipt(*args, **kwargs):
            return {**settle(*args, **kwargs), 'lease_token': 'PRIVATE WRONG TOKEN'}
        self.owner.repo.settle_page = wrong_receipt
        with self.assertRaisesRegex(RuntimeError, 'receipt identity mismatch'):
            self.dispatch(self.owner.unit())
        self.assertIn(('writer', 'unknown'), self.outcomes())
        self.assertNotIn(('writer', 'confirmed'), self.outcomes())
        self.assertNotIn(('claim', 'confirmed'), self.outcomes())
        self.owner.progress.assert_not_called()

    def test_lost_progress_ack_does_not_erase_known_page_or_invent_ack(self):
        self.owner.progress.side_effect = OSError('PRIVATE ACK ERROR')
        with self.assertRaises(OSError):
            self.dispatch(self.owner.unit())
        outcomes = self.outcomes()
        self.assertIn(('writer', 'confirmed'), outcomes)
        self.assertIn(('claim', 'unknown'), outcomes)
        self.assertNotIn(('claim', 'confirmed'), outcomes)
        self.assertEqual(len(self.owner.repo.saved), 1)
        self.assertNotIn('PRIVATE ACK ERROR', self.path.read_text())

    def test_budget_wait_requires_confirmed_drain_and_never_becomes_page_success(self):
        admission = Mock()
        admission.admit.side_effect = DiscoveryAdmissionDenied(dict(reason='window_budget'))
        unit = self.owner.unit(discovery_admission=admission)
        self.assertIsInstance(unit.prepare(), Skipped)
        self.assertEqual(self.outcomes(), [('claim', 'accepted'), ('claim', 'confirmed'), ('wait', 'waiting')])
        self.assertEqual(self.events()[-1]['reason'], 'budget')
        self.owner.repo.database.drain_hot_collection_claims.assert_called_once()
        self.assertEqual(self.owner.budget.attempts, 0)

    def test_unknown_release_does_not_claim_deferred_ack(self):
        admission = Mock()
        admission.admit.side_effect = DiscoveryAdmissionDenied(dict(reason='window_budget'))
        self.owner.repo.database.drain_hot_collection_claims.side_effect = OSError('PRIVATE DRAIN ERROR')
        with self.assertRaises(Exception):
            self.owner.unit(discovery_admission=admission).prepare()
        self.assertEqual(self.outcomes(), [('claim', 'accepted'), ('claim', 'unknown')])
        self.assertEqual((self.owner.budget.attempts, self.owner.reservations.reserved), (0, 1))

    def test_source_work_refs_separate_formats_and_omit_noncanonical_identity(self):
        for kind in ('long', 'short'):
            journal.inventory_event('wait', 'waiting', channel_id=CHANNEL, video_type=kind,
                cause='PRIVATE REASON', operation_id='PRIVATE OPERATION')
        journal.inventory_event('claim', 'accepted', channel_id='PRIVATE CHANNEL', video_type='long')
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(len({e['work_ref'] for e in events}), 2)
        self.assertNotIn('PRIVATE', json.dumps(events))

    def test_journal_sink_failure_cannot_change_page_or_ack(self):
        with patch.object(journal._emitter, 'emit', side_effect=OSError('PRIVATE SINK ERROR')):
            self.assertIsInstance(self.dispatch(self.owner.unit()), Ack)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.owner.budget.attempts, 1)
        self.owner.progress.assert_called_once()

    def test_sequential_compatibility_has_same_owner_boundaries_and_unknown_fence(self):
        repo = Repository()
        result = run_typed_inventory_claim(repository=repo, claim=claim(), fetch_page=lambda *a, **k: page(), now=lambda: NOW)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.outcomes(), [('claim', 'accepted'), ('claim', 'prepared'),
            ('writer', 'started'), ('writer', 'confirmed'), ('claim', 'confirmed')])
        known = len(self.events())
        repo.settle_page = Mock(side_effect=OSError('PRIVATE SYNC WRITE'))
        with self.assertRaises(OSError):
            run_typed_inventory_claim(repository=repo, claim=claim(), fetch_page=lambda *a, **k: page(), now=lambda: NOW)
        late = [(e['operation'], e['status']) for e in self.events()[known:]]
        self.assertIn(('writer', 'unknown'), late)
        self.assertIn(('claim', 'unknown'), late)
        self.assertNotIn(('writer', 'confirmed'), late)
        self.assertNotIn(('claim', 'confirmed'), late)


if __name__ == '__main__':
    unittest.main()
