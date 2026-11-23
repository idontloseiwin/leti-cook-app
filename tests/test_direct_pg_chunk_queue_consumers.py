"""Chunk/queue cutovers preserve public coercion, ownership and one dispatch."""
import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.jobs import operator_job_chunks as chunks,operator_job_chunk_settlement as settlement
from db.videos import video_queue_maintenance as queue
from db.jobs.operator_jobs import OperatorJobRepository
from db.videos.video_collection import VideoCollectionRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied
ID=UUID('f8500000-0000-4000-8000-000000000001')

class ChunkQueueConsumers(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.jobs=OperatorJobRepository(self.db);self.videos=VideoCollectionRepository(self.db)
 def pairs(self):return ((self.jobs.prepare_chunk,'prepare_operator_job_chunk'),(self.jobs.prepare_initial_hydration_chunk,'prepare_initial_hydration_job_chunk'))
 def test_chunk_public_projection_and_exact_owned_parameters(self):
  for call,name in self.pairs():
   with self.subTest(name=name),patch.object(chunks,name,return_value=dict(job={'id':str(ID)},video_ids=['a','a','b'],reserved_units='2',deferred=1,scope='all',targets=[])) as action:
    self.assertEqual(call(ID,ID,limit='7',lease_seconds='900'),dict(job={'id':str(ID)},video_ids=['a','b'],reserved_units=2,deferred=True))
    action.assert_called_once_with(self.db,job_id=str(ID),lease_token=str(ID),limit=7,lease_seconds=900)
  self.db.call.assert_not_called()
 def test_chunk_empty_optional_projection_unchanged(self):
  for call,name in self.pairs():
   with patch.object(chunks,name,return_value={'job':{}}):
    self.assertEqual(call(ID,ID),dict(job={},video_ids=[],reserved_units=0,deferred=False))
 def test_chunk_invalid_result_does_not_retry_or_hide_error(self):
  for call,name in self.pairs():
   for result in ({'job':None},{'job':{},'video_ids':['a',None]},None):
    with patch.object(chunks,name,return_value=result) as action:
     with self.assertRaises(TypeError):call(ID,ID)
     action.assert_called_once()
 def test_chunk_unknown_commit_does_not_retry_or_release_owner(self):
  for call,name in self.pairs():
   with patch.object(chunks,name,side_effect=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(DatabaseUnavailable) as caught:call(ID,ID)
    self.assertTrue(caught.exception.outcome_unknown);action.assert_called_once();abort.assert_not_called()
  self.db.call.assert_not_called()
 def test_chunk_definite_denial_releases_only_exact_owner(self):
  for call,name in self.pairs():
   with patch.object(chunks,name,side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}) as abort:
    with self.assertRaises(HotDatabaseAdmissionDenied):call(ID,ID)
    action.assert_called_once();abort.assert_called_once_with(operators=[{'job_id':str(ID),'lease_token':str(ID)}])
 def test_queue_cleans_ids_but_preserves_captured_epochs_without_read(self):
  scopes=[{'channel_id':'channel','control_epoch':7}]
  with patch.object(queue,'enqueue_video_collection_ids',return_value=2) as action:
   self.assertEqual(self.videos.enqueue_video_ids([' a ','a','b',None],channel_scopes=scopes),2)
   action.assert_called_once_with(self.db,video_ids=['a','b'],channel_scopes=scopes)
  self.db.call.assert_not_called();self.db.transaction.assert_not_called()
 def test_queue_bounds_before_dispatch(self):
  with patch.object(queue,'enqueue_video_collection_ids') as action:
   self.assertEqual(self.videos.enqueue_video_ids([],channel_scopes=[]),0)
   with self.assertRaises(ValueError):self.videos.enqueue_video_ids([str(i) for i in range(1001)],channel_scopes=[])
   action.assert_not_called()
 def test_queue_unknown_ack_no_retry_or_abort(self):
  with patch.object(queue,'enqueue_video_collection_ids',side_effect=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(DatabaseUnavailable):self.videos.enqueue_video_ids(['a'],channel_scopes=[])
   action.assert_called_once();abort.assert_not_called()
  self.db.call.assert_not_called()
 def test_queue_definite_denial_does_not_release_unowned_video(self):
  with patch.object(queue,'enqueue_video_collection_ids',side_effect=HotDatabaseAdmissionDenied()),patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(HotDatabaseAdmissionDenied):self.videos.enqueue_video_ids(['a'],channel_scopes=[])
   abort.assert_not_called()
class SettlementConsumers(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden');self.repo=OperatorJobRepository(self.db)
 def pairs(self):return ((self.repo.settle_chunk,'settle_operator_job_chunk'),(self.repo.settle_initial_hydration_chunk,'settle_initial_hydration_job_chunk'))
 def invoke(self,call,**extra):
  values=dict(observed_quota_units=-1,error_code='x'*130,summary={'kept':True});values.update(extra)
  return call(ID,ID,**values)
 def test_native_result_and_existing_public_coercions(self):
  for call,name in self.pairs():
   with patch.object(settlement,name,return_value={'id':'id','status':'queued'}) as action:
    self.assertEqual(self.invoke(call),{'id':'id','status':'queued'})
    action.assert_called_once_with(self.db,job_id=str(ID),lease_token=str(ID),observed_quota_units=0,error_code='x'*120,summary={'kept':True})
  self.db.call.assert_not_called()
 def test_unknown_ack_does_not_retry_or_release_committed_job(self):
  for call,name in self.pairs():
   with patch.object(settlement,name,side_effect=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(DatabaseUnavailable):self.invoke(call)
    action.assert_called_once();abort.assert_not_called()
  self.db.call.assert_not_called()
 def test_definite_denial_releases_exact_job_owner(self):
  for call,name in self.pairs():
   with patch.object(settlement,name,side_effect=HotDatabaseAdmissionDenied()),patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}) as abort:
    with self.assertRaises(HotDatabaseAdmissionDenied):self.invoke(call)
    abort.assert_called_once_with(operators=[{'job_id':str(ID),'lease_token':str(ID)}])
 def test_invalid_result_and_invalid_summary_remain_visible(self):
  for call,name in self.pairs():
   with patch.object(settlement,name,return_value=None) as action:
    with self.assertRaises(TypeError):self.invoke(call)
    action.assert_called_once();action.reset_mock()
    with self.assertRaises(TypeError):self.invoke(call,summary=None)
    action.assert_not_called()
if __name__=='__main__':unittest.main()
