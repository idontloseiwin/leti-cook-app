"""Exercise the existing synchronous entrypoint through shared pass accounting."""
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
import collection.auto_pass as bookkeeping
import collection_shutdown
from video_collection_service import run_claimed_common_video_collection

RUN = 'fa700000-0000-4000-8000-000000000001'


def batch(count, *, inspected=None, paused=0, exhausted=True):
    return dict(rows=[SimpleNamespace(video_id=str(i)) for i in range(count)],
                inspected_count=count if inspected is None else inspected,
                paused_count=paused, scan_exhausted=exhausted)


class SharedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.states = []
        original = bookkeeping.AutoPassState

        def capture(*args, **kwargs):
            state = original(*args, **kwargs)
            self.states.append(state)
            return state

        for guard in (patch.object(bookkeeping, 'AutoPassState', side_effect=capture),
                      patch.object(collection_shutdown, '_requested', Event())):
            guard.start()
            self.addCleanup(guard.stop)
        self.repository = Mock()
        self.repository.reconcile_typed.return_value = {'affected_count': 0}
        self.service = Mock()
        self.service.process_common_claims.side_effect = (
            lambda rows, **kw: {'updated': len(rows), 'requested': len(rows)})

    def run_pass(self, limit):
        return run_claimed_common_video_collection(self.repository, self.service,
                   limit=limit, worker_id='offline', run_id=RUN)

    def test_same_state_owns_50_plus_1_and_stable_run(self):
        self.repository.claim_due_batch.side_effect = [batch(50), batch(1)]
        result = self.run_pass(51)
        self.assertEqual([c.kwargs['limit'] for c in self.repository.claim_due_batch.call_args_list], [50, 1])
        self.assertEqual({c.kwargs['run_id'] for c in self.repository.claim_due_batch.call_args_list}, {RUN})
        self.assertEqual((result['claimed'], result['updated'], result['inspected']), (51, 51, 51))
        self.assertEqual((len(self.states), self.states[0].held, self.states[0].remaining), (1, 0, 0))

    def test_empty_scans_spend_scan_allowance_but_no_provider_attempt(self):
        self.repository.claim_due_batch.side_effect = [
            batch(0, inspected=1000, paused=500, exhausted=False),
            batch(0, inspected=1000, paused=300, exhausted=False)]
        result = self.run_pass(100)
        self.assertEqual((result['inspected'], result['paused'], result['claimed']), (2000, 800, 0))
        self.assertEqual(result['stopped_reason'], 'scan_budget')
        self.assertEqual(self.states[0].remaining, 100)
        self.service.process_common_claims.assert_not_called()

    def test_unknown_claim_never_retries_or_refunds_reservation(self):
        self.repository.claim_due_batch.side_effect = OSError('synthetic lost ACK')
        result = self.run_pass(100)
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(result['stopped_reason'], 'claim_failed')
        self.assertEqual((self.states[0].held, self.states[0].remaining), (1, 50))
        self.repository.claim_due_batch.assert_called_once()
        self.service.process_common_claims.assert_not_called()

    def test_unknown_settlement_preserves_recorded_progress_and_holds_credit(self):
        self.repository.claim_due_batch.return_value = batch(50)
        self.service.process_common_claims.return_value = None
        self.service.process_common_claims.side_effect = lambda *a, **kw: {
            'updated': 3, 'settlement_unknown': True, 'stopped_reason': 'settlement_unknown'}
        result = self.run_pass(100)
        self.assertEqual((result['claimed'], result['updated']), (50, 3))
        self.assertTrue(result['settlement_unknown'])
        self.assertTrue(self.states[0].paused)
        self.assertEqual(self.states[0].held, 1)
        self.repository.claim_due_batch.assert_called_once()

    def test_malformed_claim_is_a_fixed_unknown_outcome(self):
        self.repository.claim_due_batch.return_value = batch(51)
        result = self.run_pass(100)
        self.assertEqual(result['stopped_reason'], 'claim_failed')
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(self.states[0].held, 1)
        self.service.process_common_claims.assert_not_called()


if __name__ == '__main__':
    unittest.main()
