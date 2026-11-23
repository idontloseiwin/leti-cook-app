"""Repository cutover: fixed cleanup dispatch, admission failure and unknown commit."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.hot.hot_retention import HotRetentionRepository,RetentionError,CleanupPage
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied

FAMILIES={
 'cleanup_hot_inventory':('memberships','inventory'),
 'cleanup_hot_deleted_channels':('deleted_channels',),
 'cleanup_hot_current_source':('video_counters','video_metadata','video_availability','channel_profile','channel_country','channel_inventory_estimate'),
 'cleanup_hot_inventory_copies':('playlist_state','inventory_scans','inventory_receipts'),
 'cleanup_hot_operational_copies':('operator_jobs','operator_job_targets','sync_runs','quota_attempts'),
}
def receipt():
 return dict(as_of='2026-10-06T00:00:00+00:00',cutoff='2026-09-07T00:00:00+00:00',
  scanned=1,deleted=0,scrubbed=1,deferred=0,complete=False,next_cursor='cursor',
  oldest_remaining_expired_source_at=None,invalid_clock_remaining=0)

class RetentionConsumerTests(unittest.TestCase):
 def database(self):
  value=Mock(spec=[*FAMILIES,'call']);value.call.side_effect=AssertionError('RPC forbidden')
  return value
 def test_migrated_targets_use_fixed_action_and_validated_page(self):
  for function,targets in FAMILIES.items():
   for target in targets:
    with self.subTest(target=target):
     database=self.database();action=getattr(database,function);action.return_value=receipt()
     page=HotRetentionRepository(database).cleanup_page(target,after_id='before',limit=3)
     self.assertIsInstance(page,CleanupPage);self.assertEqual(page.scrubbed,1)
     self.assertEqual(page.next_cursor,'cursor');self.assertNotIn('next_cursor',page.summary())
     action.assert_called_once_with(target=target,after_id='before',limit=3);database.call.assert_not_called()
 def test_unknown_commit_and_denial_never_retry_or_fallback(self):
  for function,targets in FAMILIES.items():
   for error in (DatabaseUnavailable('private error',code='08006',outcome_unknown=True),HotDatabaseAdmissionDenied()):
    database=self.database();action=getattr(database,function);action.side_effect=error
    with self.assertRaisesRegex(RetentionError,'^cleanup_outcome_unknown$') as caught:
     HotRetentionRepository(database).cleanup_page(targets[0])
    self.assertIsNone(caught.exception.__cause__);action.assert_called_once();database.call.assert_not_called()
 def test_local_bounds_precede_every_action(self):
  for function,targets in FAMILIES.items():
   database=self.database()
   for kwargs in ({'limit':True},{'limit':101},{'after_id':''},{'after_id':'x'*2049}):
    with self.assertRaises(ValueError):HotRetentionRepository(database).cleanup_page(targets[0],**kwargs)
   self.assertFalse(database.mock_calls)
 def test_bad_receipt_is_rejected_after_single_execution(self):
  for function,targets in FAMILIES.items():
   database=self.database();action=getattr(database,function);action.return_value={**receipt(),'cutoff':'2026-09-09T00:00:00+00:00'}
   with self.assertRaisesRegex(RetentionError,'invalid_cleanup_result'):
    HotRetentionRepository(database).cleanup_page(targets[0])
   action.assert_called_once();database.call.assert_not_called()

if __name__=='__main__':unittest.main()
