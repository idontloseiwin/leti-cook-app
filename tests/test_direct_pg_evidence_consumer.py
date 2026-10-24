from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.hot.hot_retention import HotRetentionRepository,LocalTestRetentionRepository,RetentionError,CleanupPage
from db.base.postgres import DatabaseUnavailable

TARGETS=('video_snapshots','channel_snapshots','typed_video_collection_receipts','availability_events','availability_incidents','review_evidence')
def receipt():
    return dict(as_of='2026-10-06T00:00:00+00:00',cutoff='2026-09-07T00:00:00+00:00',
        scanned=1,deleted=1,scrubbed=0,deferred=0,complete=True,next_cursor=None,
        oldest_remaining_expired_source_at=None,invalid_clock_remaining=0)

class EvidenceConsumerTests(unittest.TestCase):
    def test_every_target_uses_closed_action_and_page_validation(self):
        for target in TARGETS:
            database=Mock(spec=['cleanup_hot_evidence','call'])
            database.cleanup_hot_evidence.return_value=receipt()
            page=HotRetentionRepository(database).cleanup_page(target,limit=2,after_id='cursor')
            self.assertIsInstance(page,CleanupPage)
            database.cleanup_hot_evidence.assert_called_once_with(target=target,after_id='cursor',limit=2)
            database.call.assert_not_called()
    def test_lost_ack_is_sanitized_and_never_retried(self):
        database=Mock(spec=['cleanup_hot_evidence','call'])
        database.cleanup_hot_evidence.side_effect=DatabaseUnavailable('private provider data',code='08006',outcome_unknown=True)
        with self.assertRaises(RetentionError) as error:HotRetentionRepository(database).cleanup_page(TARGETS[0])
        self.assertEqual(str(error.exception),'cleanup_outcome_unknown')
        database.cleanup_hot_evidence.assert_called_once();database.call.assert_not_called()
    def test_local_validation_precedes_driver(self):
        database=Mock(spec=['cleanup_hot_evidence','call'])
        for kwargs in ({'limit':101},{'limit':False},{'after_id':''}):
            with self.assertRaises(ValueError):HotRetentionRepository(database).cleanup_page(TARGETS[0],**kwargs)
        database.cleanup_hot_evidence.assert_not_called()
    def test_native_receipt_invalid_cutoff_is_rejected(self):
        database=Mock(spec=['cleanup_hot_evidence','call'])
        database.cleanup_hot_evidence.return_value={**receipt(),'cutoff':'2026-09-09T00:00:00+00:00'}
        with self.assertRaises(RetentionError):HotRetentionRepository(database).cleanup_page(TARGETS[0])
        database.cleanup_hot_evidence.assert_called_once()
    def test_explicit_local_synthetic_adapter_keeps_own_dispatch(self):
        repository=LocalTestRetentionRepository(psql='psql',database='typed_audit_retention',port=5433)
        with patch.object(repository,'_call',return_value=receipt()) as action:
            repository.cleanup_page('video_snapshots')
        action.assert_called_once_with('cleanup_hot_evidence',dict(p_target='video_snapshots',p_after_id=None,p_limit=100))

if __name__=='__main__':unittest.main()