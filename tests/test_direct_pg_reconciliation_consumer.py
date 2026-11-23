"""Accepted typed reconciliation preserves public validation, telemetry and admission."""
from pathlib import Path
import sys, unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.videos import video_typed_reconciliation as helper
from db.videos.video_collection import VideoCollectionRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied

class ReconciliationConsumerTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock(spec=['call', 'transaction'])
        self.db.call.side_effect = AssertionError('RPC forbidden')
        self.repo = VideoCollectionRepository(self.db)

    def test_exact_public_type_limit_mapping_and_telemetry(self):
        for kind in ('short', 'stream'):
            expected = {'enabled': True, 'video_type': kind, 'remaining_count': None}
            with patch.object(helper, 'reconcile_typed_video_collection_state', return_value=expected) as action, patch('db.videos.video_collection.emit_event') as emit:
                self.assertEqual(self.repo.reconcile_typed(kind, '7'), expected)
            action.assert_called_once_with(self.db, video_type=kind, limit=7)
            self.assertEqual([call.kwargs['status'] for call in emit.call_args_list], ['started', 'success'])
            self.assertEqual([call.args[3] for call in emit.call_args_list], ['rpc:reconcile_typed_video_collection_state'] * 2)
        self.db.call.assert_not_called()

    def test_invalid_type_and_limit_reject_before_dispatch(self):
        for kind, limit in (('long', 1), ('other', 1), ('short', 0), ('short', 1001)):
            with self.subTest(kind=kind, limit=limit), patch.object(helper, 'reconcile_typed_video_collection_state') as action:
                with self.assertRaises(ValueError):
                    self.repo.reconcile_typed(kind, limit)
                action.assert_not_called()
        self.db.call.assert_not_called()

    def test_unknown_ack_has_one_attempt_and_no_unowned_abort(self):
        error = DatabaseUnavailable('database_connection_failed', code='08006', outcome_unknown=True)
        with patch.object(helper, 'reconcile_typed_video_collection_state', side_effect=error) as action, patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
            with self.assertRaises(DatabaseUnavailable) as caught:
                self.repo.reconcile_typed('short')
            self.assertIs(caught.exception, error)
            action.assert_called_once()
            abort.assert_not_called()

    def test_admission_denial_does_not_invent_a_claim_owner(self):
        with patch.object(helper, 'reconcile_typed_video_collection_state', side_effect=HotDatabaseAdmissionDenied()) as action, patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
            with self.assertRaises(HotDatabaseAdmissionDenied):
                self.repo.reconcile_typed('stream')
            action.assert_called_once()
            abort.assert_not_called()

    def test_invalid_result_remains_visible_after_one_dispatch(self):
        for result in (None, [], 3):
            with patch.object(helper, 'reconcile_typed_video_collection_state', return_value=result) as action:
                with self.assertRaises(TypeError):
                    self.repo.reconcile_typed('short')
                action.assert_called_once()

if __name__ == '__main__':
    unittest.main()
