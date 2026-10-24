"""Long completion cutover preserves batching, idempotent retry and owner denial."""
import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.videos import video_counter_completion as counters,video_profile_completion as profile
from db.videos.video_collection import VideoCollectionRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied

class LongCompletionConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.repo=VideoCollectionRepository(self.db)
  self.rows=[{'video_id':'fixture0001','lease_token':'lease','collection_attempt_id':'attempt'}]
  self.cases=((counters,'complete_video_counter_collection',self.repo.complete_counters),
              (profile,'complete_video_profile_metadata_collection',self.repo.complete_metadata))
 def test_unchanged_rows_reach_only_one_named_direct_writer(self):
  for module,name,call in self.cases:
   with self.subTest(name=name),patch.object(module,name,return_value=1) as action:
    self.assertEqual(call(self.rows),1);action.assert_called_once_with(self.db,self.rows)
  self.db.call.assert_not_called()
 def test_empty_input_never_opens_a_transaction(self):
  for module,name,call in self.cases:
   with patch.object(module,name) as action:self.assertEqual(call([]),0);action.assert_not_called()
  self.db.transaction.assert_not_called();self.db.call.assert_not_called()
 def test_1000_row_boundary_stays_two_bounded_transactions(self):
  rows=[dict(self.rows[0],video_id=str(index)) for index in range(1001)]
  for module,name,call in self.cases:
   with patch.object(module,name,side_effect=lambda database,batch:len(batch)) as action:
    self.assertEqual(call(rows),1001)
    self.assertEqual([len(c.args[1]) for c in action.call_args_list],[1000,1])
    self.assertEqual([row for c in action.call_args_list for row in c.args[1]],rows)
  self.db.call.assert_not_called()
 def test_unknown_ack_retries_identical_body_exactly_once(self):
  for module,name,call in self.cases:
   error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
   with patch.object(module,name,side_effect=[error,1]) as action:
    self.assertEqual(call(self.rows),1);self.assertEqual(action.call_count,2)
    self.assertEqual(action.call_args_list[0],action.call_args_list[1])
  self.db.call.assert_not_called()
 def test_second_unknown_ack_propagates_without_third_try(self):
  for module,name,call in self.cases:
   error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
   with patch.object(module,name,side_effect=error) as action:
    with self.assertRaises(DatabaseUnavailable):call(self.rows)
    self.assertEqual(action.call_count,2)
 def test_first_denial_aborts_only_its_claim_and_is_not_retried(self):
  for module,name,call in self.cases:
   with patch.object(module,name,side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}) as abort:
    with self.assertRaises(HotDatabaseAdmissionDenied):call(self.rows)
    action.assert_called_once();abort.assert_called_once_with(videos=[{'video_id':'fixture0001','video_type':'long','lease_token':'lease'}])
 def test_denial_after_unknown_ack_never_aborts_possibly_completed_work(self):
  for module,name,call in self.cases:
   error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
   denial=HotDatabaseAdmissionDenied()
   with patch.object(module,name,side_effect=[error,denial]) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(HotDatabaseAdmissionDenied) as caught:call(self.rows)
    self.assertTrue(caught.exception.outcome_may_be_committed);self.assertEqual(action.call_count,2);abort.assert_not_called()
 def test_incomplete_count_remains_a_failure(self):
  for module,name,call in self.cases:
   with patch.object(module,name,return_value=0) as action:
    with self.assertRaises(RuntimeError):call(self.rows)
    action.assert_called_once()

if __name__=='__main__':unittest.main()
