"""Typed writer cutovers keep <=50 batches, exact receipt retry and owner fences."""
import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.videos import video_stream_completion as stream,video_typed_metadata_completion as metadata
from db.videos.video_collection import VideoCollectionRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied

class TypedCompletionConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.repo=VideoCollectionRepository(self.db)
  self.cases=tuple((stream,'complete_stream_lifecycle_probe',self.repo.complete_stream_lifecycle,kind)
                   for kind in ('long','short','stream','unknown'))+(
              (metadata,'complete_typed_video_metadata_collection',self.repo.complete_typed_metadata,'short'),
              (metadata,'complete_typed_video_metadata_collection',self.repo.complete_typed_metadata,'stream'))
 def rows(self,kind,count=1):
  return [dict(video_id=str(i),video_type=kind,lease_token='lease',collection_attempt_id='attempt') for i in range(count)]
 def test_exact_direct_writer_and_empty_input(self):
  for module,name,call,kind in self.cases:
   rows=self.rows(kind)
   with patch.object(module,name,return_value=1) as action:
    self.assertEqual(call(rows),1);action.assert_called_once_with(self.db,rows)
    action.reset_mock();self.assertEqual(call([]),0);action.assert_not_called()
  self.db.call.assert_not_called()
 def test_owner_type_validation_before_dispatch(self):
  with self.assertRaises(ValueError):self.repo.complete_stream_lifecycle(self.rows('invalid_type'))
  with self.assertRaises(ValueError):self.repo.complete_typed_metadata(self.rows('long'))
  with self.assertRaises(ValueError):self.repo.complete_typed_metadata(self.rows('short')+self.rows('stream'))
  self.db.transaction.assert_not_called();self.db.call.assert_not_called()
 def test_publication_batch_can_mix_types_without_changing_owner(self):
  rows=[dict(video_id=str(index),video_type=kind,lease_token='lease',collection_attempt_id='attempt')
        for index,kind in enumerate(('long','short','stream','unknown'))]
  with patch.object(stream,'complete_stream_lifecycle_probe',return_value=4) as action:
   self.assertEqual(self.repo.complete_stream_lifecycle(rows),4)
   action.assert_called_once_with(self.db,rows)
 def test_fifty_row_transactions_preserve_all_identities(self):
  for module,name,call,kind in self.cases:
   rows=self.rows(kind,51)
   with patch.object(module,name,side_effect=lambda database,batch:len(batch)) as action:
    self.assertEqual(call(rows),51)
    self.assertEqual([len(c.args[1]) for c in action.call_args_list],[50,1])
    self.assertEqual([row for c in action.call_args_list for row in c.args[1]],rows)
 def test_unknown_ack_uses_exactly_one_identical_replay(self):
  for module,name,call,kind in self.cases:
   error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
   with patch.object(module,name,side_effect=[error,1]) as action:
    self.assertEqual(call(self.rows(kind)),1);self.assertEqual(action.call_count,2)
    self.assertEqual(action.call_args_list[0],action.call_args_list[1])
   with patch.object(module,name,side_effect=error) as action:
    with self.assertRaises(DatabaseUnavailable):call(self.rows(kind))
    self.assertEqual(action.call_count,2)
 def test_first_denial_aborts_only_its_original_claim(self):
  for module,name,call,kind in self.cases:
   with patch.object(module,name,side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}) as abort:
    with self.assertRaises(HotDatabaseAdmissionDenied):call(self.rows(kind))
    action.assert_called_once();abort.assert_called_once_with(videos=[dict(video_id='0',video_type=kind,lease_token='lease')])
 def test_retry_denial_does_not_abort_possibly_committed_work(self):
  for module,name,call,kind in self.cases:
   error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
   with patch.object(module,name,side_effect=[error,HotDatabaseAdmissionDenied()]) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(HotDatabaseAdmissionDenied) as caught:call(self.rows(kind))
    self.assertTrue(caught.exception.outcome_may_be_committed);self.assertEqual(action.call_count,2);abort.assert_not_called()
 def test_incomplete_count_remains_an_error(self):
  for module,name,call,kind in self.cases:
   with patch.object(module,name,return_value=0) as action:
    with self.assertRaises(RuntimeError):call(self.rows(kind))
    action.assert_called_once()

if __name__=='__main__':unittest.main()
