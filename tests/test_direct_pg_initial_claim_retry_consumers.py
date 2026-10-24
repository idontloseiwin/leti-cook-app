"""Initial job public validation survives the direct Python cutover."""
from pathlib import Path
from unittest.mock import Mock,patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.videos import video_initial_claims as claims
from db.jobs import operator_job_retry as retry
from db.videos.video_collection import VideoCollectionRepository
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseUnavailable
CHANNEL='UC'+'a'*22;VIDEO='a'*11;ID='f9900000-0000-4000-8000-000000000001'
class InitialJobConsumers(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.videos=VideoCollectionRepository(self.db);self.jobs=OperatorJobRepository(self.db)
 def claim(self,ids=None,**changes):
  args=dict(job_id=' '+ID+' ',channel_id=CHANNEL,worker_id=' worker ',run_id=' '+ID+' ',lease_seconds='900');args.update(changes)
  return self.videos.claim_initial_video_ids([' '+VIDEO+' ',VIDEO] if ids is None else ids,**args)
 def retried(self,**changes):
  args=dict(expected_epoch=7,retry_request_id=ID,max_items='20',max_pages=None,max_quota_units='30',confirmed=1);args.update(changes)
  return self.jobs.retry_initial_inventory(ID,**args)
 def test_claim_normalization_and_single_dispatch(self):
  with patch.object(claims,'claim_initial_video_collection_batch',return_value=[]) as action:
   self.assertEqual(self.claim(),[]);action.assert_called_once_with(self.db,job_id=ID,video_ids=[VIDEO],worker_id='worker',run_id=ID,lease_seconds=900)
  self.db.call.assert_not_called()
 def test_empty_and_invalid_claims_do_not_dispatch(self):
  with patch.object(claims,'claim_initial_video_collection_batch') as action:
   self.assertEqual(self.claim([]),[])
   for ids,args in (([str(i) for i in range(51)],{}),([VIDEO],dict(lease_seconds=True)),([VIDEO],dict(channel_id=''))):
    with self.assertRaises(ValueError):self.claim(ids,**args)
   action.assert_not_called()
 def test_returned_claim_cannot_escape_channel_or_counter_mode(self):
  row=dict(video_id=VIDEO,channel_id=CHANNEL,video_type='long',collection_mode='metadata',lease_token=ID,lease_expires_at='later',next_due_at='due')
  for changed in ({'channel_id':'other'},{'collection_mode':'counters'},{'video_id':'other'}):
   with patch.object(claims,'claim_initial_video_collection_batch',return_value=[{**row,**changed}]),self.assertRaises(RuntimeError):self.claim()
 def test_retry_normalization_preserves_nullable_caps_and_generation(self):
  with patch.object(retry,'retry_channel_initial_inventory_job_v5',return_value={'status':'queued'}) as action:
   self.assertEqual(self.retried(),{'status':'queued'})
   action.assert_called_once_with(self.db,job_id=ID,retry_request_id=ID,max_items=20,max_pages=None,max_quota_units=30,confirmed=True,expected_epoch=7)
   action.reset_mock()
   with self.assertRaises(ValueError):self.retried(expected_epoch=True)
   action.assert_not_called()
 def test_unknown_ack_is_never_automatically_replayed(self):
  error=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
  for module,name,call in ((claims,'claim_initial_video_collection_batch',self.claim),(retry,'retry_channel_initial_inventory_job_v5',self.retried)):
   with patch.object(module,name,side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(DatabaseUnavailable) as caught:call()
    self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
if __name__=='__main__':unittest.main()
