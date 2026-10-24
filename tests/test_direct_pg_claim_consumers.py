"""Owned claim/release and receipt-backed failure consumers after native parity."""
from datetime import datetime,timezone
from pathlib import Path
import sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.videos import video_claims,video_scheduled_claims,video_queue_ensure
from db.jobs import operator_job_lifecycle,operator_job_completion
from db.inventory import inventory_scan_steps,inventory_scan_pages
from db.videos.video_collection import VideoCollectionRepository
from db.jobs.operator_jobs import OperatorJobRepository
from db.inventory.typed_inventory import TypedInventoryRepository,TypedInventoryClaim,InventoryTokenCycleError
from db.base.postgres import DatabaseUnavailable,DatabaseServerError
from hot_runtime import HotDatabaseAdmissionDenied
CHANNEL='UC'+'a'*22
VIDEO='a'*11
ID='c0000000-0000-4000-8000-000000000061'
SCOPE=[{'channel_id':CHANNEL,'control_epoch':1}]

class ClaimConsumerTests(unittest.TestCase):
 def setUp(self):
  self.database=Mock(spec=['call','transaction']);self.database.call.side_effect=AssertionError('RPC forbidden')
 def test_both_addressed_paths_keep_validation_and_captured_scope(self):
  repository=VideoCollectionRepository(self.database)
  for method,name in ((repository.claim_video_ids,'claim_video_collection_ids'),(repository.claim_video_metadata_ids,'claim_video_metadata_collection_ids')):
   with patch.object(video_claims,name,return_value=[]) as action:
    self.assertEqual(method([' '+VIDEO+' ',VIDEO],channel_scopes=SCOPE,video_channels={VIDEO:CHANNEL},worker_id=' worker ',run_id=ID),[])
    action.assert_called_once_with(self.database,video_ids=[VIDEO],worker_id='worker',run_id=ID,channel_scopes=SCOPE,lease_seconds=300)
  self.database.call.assert_not_called()
 def test_addressed_unknown_ack_does_not_retry_or_abort(self):
  repository=VideoCollectionRepository(self.database)
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(video_claims,'claim_video_collection_ids',side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(DatabaseUnavailable) as caught:
    repository.claim_video_ids([VIDEO],channel_scopes=SCOPE,video_channels={VIDEO:CHANNEL},worker_id='worker',run_id=ID)
   self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
 def test_claim_scope_rejects_before_helper(self):
  with patch.object(video_claims,'claim_video_collection_ids') as action:
   with self.assertRaises(ValueError):VideoCollectionRepository(self.database).claim_video_ids([VIDEO],channel_scopes=[],video_channels={VIDEO:CHANNEL},worker_id='worker',run_id=ID)
   action.assert_not_called()
 def test_job_methods_dispatch_typed_arguments(self):
  repo=OperatorJobRepository(self.database)
  with patch.object(operator_job_lifecycle,'claim_operator_job',return_value=None) as action:
   self.assertIsNone(repo.claim(' worker ',lease_seconds=120));action.assert_called_once_with(self.database,worker_id='worker',lease_seconds=120)
  with patch.object(operator_job_lifecycle,'claim_channel_initial_hydration_job',return_value={'id':ID}) as action:
   self.assertEqual(repo.claim_initial_hydration(' '+CHANNEL+' ',' worker ')['id'],ID)
   action.assert_called_once_with(self.database,channel_id=CHANNEL,worker_id='worker',lease_seconds=1200)
  with patch.object(operator_job_lifecycle,'release_operator_job',return_value={'status':'queued'}) as action:
   self.assertEqual(repo.release(ID,ID,error_code='')['status'],'queued')
   action.assert_called_once_with(self.database,job_id=ID,lease_token=ID,error_code='operator_worker_error')
  self.database.call.assert_not_called()
 def test_job_claim_unknown_ack_has_no_automatic_retry(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(operator_job_lifecycle,'claim_operator_job',side_effect=error) as action:
   with self.assertRaises(DatabaseUnavailable):OperatorJobRepository(self.database).claim('worker')
   action.assert_called_once();self.database.call.assert_not_called()
 def claim(self):
  return TypedInventoryClaim(channel_id=CHANNEL,video_type='long',playlist_id='UU'+'a'*22,
    lease_token=ID,lease_channel_epoch=1,lease_expires_at='2026-10-06T01:00:00Z',no_new_streak=0,
    active_scan_id=ID,resume_page_token=None,scan_kind='discovery')
 def fail(self):
  return TypedInventoryRepository(self.database).fail_scan(self.claim(),scan_id=ID,transition_id=ID,
   expected_page_token=None,outcome='error',error_code='fetch_failed',source_checked_at=datetime(2026,10,6,tzinfo=timezone.utc))
 def test_scan_failure_uses_stable_receipt_identity_on_existing_retry(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(inventory_scan_steps,'fail_typed_inventory_scan',side_effect=[error,{'status':'error'}]) as action:
   self.assertEqual(self.fail(),{'status':'error'});self.assertEqual(action.call_count,2)
   self.assertEqual(action.call_args_list[0],action.call_args_list[1])
   self.assertEqual(action.call_args.kwargs['transition_id'],ID)
   self.assertEqual(action.call_args.kwargs['source_checked_at'],'2026-10-06T00:00:00+00:00')
  self.database.call.assert_not_called()
 def test_scan_denial_does_not_retry_and_preserves_owned_abort(self):
  error=HotDatabaseAdmissionDenied()
  with patch.object(inventory_scan_steps,'fail_typed_inventory_scan',side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}) as abort:
   with self.assertRaises(HotDatabaseAdmissionDenied):self.fail()
   action.assert_called_once();self.assertTrue(error.control_abort_attempted);abort.assert_called_once()
 def test_scan_bad_receipt_shape_rejected(self):
  with patch.object(inventory_scan_steps,'fail_typed_inventory_scan',return_value=None) as action:
   with self.assertRaises(TypeError):self.fail()
   action.assert_called_once()

 def settle(self,items=None):
  return TypedInventoryRepository(self.database).settle_page(self.claim(),scan_id=ID,transition_id=ID,
   expected_page_token=None,next_page_token=None,page_status='completed',
   items=items if items is not None else [{'video_id':' '+VIDEO+' ','published_at':None},{'video_id':VIDEO}],
   source_checked_at=datetime(2026,10,6,tzinfo=timezone.utc))
 def test_page_settlement_normalizes_and_replays_only_same_identity(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(inventory_scan_pages,'settle_typed_inventory_page',side_effect=[error,{'status':'completed','replayed':True}]) as action:
   self.assertTrue(self.settle()['replayed']);self.assertEqual(action.call_count,2)
   self.assertEqual(action.call_args_list[0],action.call_args_list[1])
   action.assert_called_with(self.database,channel_id=CHANNEL,video_type='long',lease_token=ID,scan_id=ID,transition_id=ID,
    expected_page_token=None,next_page_token=None,page_status='completed',items=[{'video_id':VIDEO,'published_at':None}],
    source_checked_at='2026-10-06T00:00:00+00:00')
  self.database.call.assert_not_called()
 def test_page_token_cycle_preserves_domain_exception(self):
  error=DatabaseServerError('inventory_token_cycle',code='P0001')
  with patch.object(inventory_scan_pages,'settle_typed_inventory_page',side_effect=error) as action:
   with self.assertRaises(InventoryTokenCycleError):self.settle()
   self.assertEqual(action.call_count,2)
 def test_page_admission_denial_never_retries(self):
  with patch.object(inventory_scan_pages,'settle_typed_inventory_page',side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}):
   with self.assertRaises(HotDatabaseAdmissionDenied):self.settle()
   action.assert_called_once()
 def test_page_limit_precedes_transaction(self):
  with patch.object(inventory_scan_pages,'settle_typed_inventory_page') as action:
   with self.assertRaises(ValueError):self.settle([{'video_id':str(index).zfill(11)} for index in range(51)])
   action.assert_not_called()
 def test_job_completion_keeps_normalization_and_result_contract(self):
  repo=OperatorJobRepository(self.database)
  with patch.object(operator_job_completion,'settle_initial_inventory_stats',return_value={'status':'queued'}) as action:
   result=repo.settle_initial_stats(ID,ID,reserved_units=-1,observed_quota_units=2,status='',summary={'x':1})
   self.assertEqual(result,{'status':'queued'})
   action.assert_called_once_with(self.database,job_id=ID,lease_token=ID,reserved_units=0,observed_quota_units=2,status='error',summary={'x':1})
  with patch.object(operator_job_completion,'mark_completed_initial_hydration_failed',return_value=1) as action:
   self.assertIsNone(repo.mark_completed_initial_hydration_failed(ID,CHANNEL,error_count=3,expected_epoch=1))
   action.assert_called_once_with(self.database,job_id=ID,channel_id=CHANNEL,error_count=3,expected_epoch=1)
  self.database.call.assert_not_called()
 def test_job_settlement_unknown_ack_is_not_automatically_repeated(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(operator_job_completion,'settle_initial_inventory_stats',side_effect=error) as action:
   with self.assertRaises(DatabaseUnavailable):OperatorJobRepository(self.database).settle_initial_stats(ID,ID,
    reserved_units=0,observed_quota_units=0,status='ok',summary={})
   action.assert_called_once();self.database.call.assert_not_called()

 def test_scheduled_claims_preserve_raw_window_counters_and_typed_arguments(self):
  repo=VideoCollectionRepository(self.database)
  result={'rows':[],'inspected_count':1000,'paused_count':2,'scan_exhausted':False}
  with patch.object(video_scheduled_claims,'claim_due_video_collection_batch',return_value=result) as action:
   self.assertEqual(repo.claim_due_batch(limit=10,worker_id=' worker ',run_id=' '+ID+' '),result)
   action.assert_called_once_with(self.database,limit=10,worker_id='worker',run_id=ID,lease_seconds=300)
  with patch.object(video_scheduled_claims,'claim_due_typed_video_collection_batch',return_value=result) as action:
   self.assertEqual(repo.claim_due_typed_batch(video_type=' SHORT ',limit=10,worker_id=' worker ',run_id=ID),result)
   action.assert_called_once_with(self.database,video_type='short',limit=10,worker_id='worker',run_id=ID,lease_seconds=300)
  self.database.call.assert_not_called()
 def test_scheduled_claim_zero_and_limits_precede_dispatch(self):
  repo=VideoCollectionRepository(self.database)
  with patch.object(video_scheduled_claims,'claim_due_video_collection_batch') as action:
   self.assertEqual(repo.claim_due_batch(limit=0,worker_id='worker',run_id=ID)['rows'],[])
   with self.assertRaises(ValueError):repo.claim_due_batch(limit=51,worker_id='worker',run_id=ID)
   action.assert_not_called()
 def test_scheduled_claim_unknown_ack_propagates_once(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  repo=VideoCollectionRepository(self.database)
  for name,method,extra in [('claim_due_video_collection_batch',repo.claim_due_batch,{}),
                          ('claim_due_typed_video_collection_batch',repo.claim_due_typed_batch,{'video_type':'short'})]:
   with patch.object(video_scheduled_claims,name,side_effect=error) as action:
    with self.assertRaises(DatabaseUnavailable):method(limit=1,worker_id='worker',run_id=ID,**extra)
    action.assert_called_once();self.database.call.assert_not_called()
 def test_scheduled_claim_impossible_counts_rejected(self):
  with patch.object(video_scheduled_claims,'claim_due_video_collection_batch',return_value={'rows':[],'inspected_count':1,'paused_count':2,'scan_exhausted':False}) as action:
   with self.assertRaises(RuntimeError):VideoCollectionRepository(self.database).claim_due_batch(limit=1,worker_id='worker',run_id=ID)
   action.assert_called_once()

 def test_ensure_preserves_1000_row_batches_and_each_captured_scope(self):
  repo=VideoCollectionRepository(self.database)
  ids=[str(i).zfill(11) for i in range(1001)];other='UC'+'b'*22
  mapping={video:(CHANNEL if i<1000 else other) for i,video in enumerate(ids)}
  scopes=SCOPE+[{'channel_id':other,'control_epoch':2}]
  with patch.object(video_queue_ensure,'ensure_video_collection_state',side_effect=[1000,1]) as action:
   self.assertEqual(repo.ensure_video_ids(ids+[ids[0]],channel_scopes=scopes,video_channels=mapping),1001)
   self.assertEqual(action.call_count,2)
   self.assertEqual(action.call_args_list[0].kwargs,dict(video_ids=ids[:1000],channel_scopes=SCOPE))
   self.assertEqual(action.call_args_list[1].kwargs,dict(video_ids=ids[1000:],channel_scopes=scopes[1:]))
  self.database.call.assert_not_called()
 def test_ensure_unknown_ack_does_not_repeat_initialization(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(video_queue_ensure,'ensure_video_collection_state',side_effect=error) as action:
   with self.assertRaises(DatabaseUnavailable) as caught:
    VideoCollectionRepository(self.database).ensure_video_ids([VIDEO],channel_scopes=SCOPE,video_channels={VIDEO:CHANNEL})
   self.assertIs(caught.exception,error);action.assert_called_once();self.database.call.assert_not_called()
 def test_ensure_empty_and_invalid_scope_stop_before_helper(self):
  repo=VideoCollectionRepository(self.database)
  with patch.object(video_queue_ensure,'ensure_video_collection_state') as action:
   self.assertEqual(repo.ensure_video_ids([],channel_scopes=[],video_channels={}),0)
   with self.assertRaises(ValueError):repo.ensure_video_ids([VIDEO],channel_scopes=[],video_channels={VIDEO:CHANNEL})
   action.assert_not_called()

if __name__=='__main__':unittest.main()
