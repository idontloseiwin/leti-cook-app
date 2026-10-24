"""Public inventory read/priority/page/failure cutovers retain existing contracts."""
from pathlib import Path
from unittest.mock import Mock,patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_runtime_queries as read,inventory_priority as priority
from db.jobs import operator_job_inventory_page_settlement as page,operator_job_inventory_failure as failure
from db.inventory.typed_inventory import TypedInventoryRepository
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied
ID='f8300000-0000-4000-8000-000000000001'

class RuntimeConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.typed=TypedInventoryRepository(self.db);self.jobs=OperatorJobRepository(self.db)
 def test_hydration_trim_mapping_and_unchanged_telemetry(self):
  expected=dict(schema_version=1,channel_id='UCchannel',status='ok',formats=[])
  with patch.object(read,'get_channel_initial_typed_hydration',return_value=expected) as action,patch('db.inventory.typed_inventory.emit_event') as emit:
   self.assertEqual(self.typed.load_channel_initial_hydration(' UCchannel '),expected)
  action.assert_called_once_with(self.db,channel_id='UCchannel')
  self.assertEqual([c.kwargs['status'] for c in emit.call_args_list],['started','success'])
  self.assertEqual([c.args[3] for c in emit.call_args_list],['rpc:get_channel_initial_typed_hydration']*2)
  self.db.call.assert_not_called()
 def test_hydration_empty_and_bad_mapping_remain_errors(self):
  with patch.object(read,'get_channel_initial_typed_hydration',return_value=[]) as action:
   with self.assertRaises(ValueError):self.typed.load_channel_initial_hydration(' ')
   action.assert_not_called()
   with self.assertRaises(TypeError):self.typed.load_channel_initial_hydration('UCchannel')
   action.assert_called_once()
 def test_priority_preserves_500_row_batches_and_captured_generations(self):
  channels=['UC'+str(i).zfill(22) for i in range(501)];scopes=[dict(channel_id=c,control_epoch=i+1) for i,c in enumerate(channels)]
  with patch.object(priority,'prioritize_typed_inventory_channels',side_effect=[1500,3]) as action:
   self.assertEqual(self.typed.prioritize_channels(channels,expected_channel_scopes=scopes),1503)
  self.assertEqual(action.call_count,2)
  for call,start,end in zip(action.call_args_list,(0,500),(500,501)):
   self.assertEqual(call.args,(self.db,));self.assertEqual(call.kwargs,dict(channel_ids=channels[start:end],expected_channel_scopes=scopes[start:end]))
  self.db.call.assert_not_called()
 def test_priority_dedup_empty_and_limit_are_still_public(self):
  with patch.object(priority,'prioritize_typed_inventory_channels',return_value=2) as action:
   self.assertEqual(self.typed.prioritize_channels([]),0);action.assert_not_called()
   self.assertEqual(self.typed.prioritize_channels([' UCa ','UCa']),2)
   action.assert_called_once_with(self.db,channel_ids=['UCa'],expected_channel_scopes=None)
   action.reset_mock()
   with self.assertRaises(ValueError):self.typed.prioritize_channels([str(i) for i in range(1001)])
   action.assert_not_called()
 def test_priority_unknown_commit_does_not_repeat_or_continue_later_batch(self):
  error=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
  with patch.object(priority,'prioritize_typed_inventory_channels',side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(DatabaseUnavailable) as caught:self.typed.prioritize_channels([str(i) for i in range(501)])
   self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
 def invoke_page(self):
  return self.jobs.settle_initial_inventory_chunk(ID,ID,reserved_units=-1,pages_processed='2',items_processed='3',inserted=-4,updated='5',next_page_token='',summary={'receipt':'kept'})
 def invoke_failure(self):return self.jobs.fail_initial_inventory(ID,ID,error_code='x'*130,playlist_not_found=1)
 def test_page_normalizations_and_receipt_are_unchanged(self):
  with patch.object(page,'settle_initial_inventory_job_chunk',return_value={'id':ID}) as action:
   self.assertEqual(self.invoke_page(),{'id':ID})
   action.assert_called_once_with(self.db,job_id=ID,lease_token=ID,reserved_units=0,pages_processed=2,items_processed=3,inserted=0,updated=5,next_page_token=None,summary={'receipt':'kept'})
  self.db.call.assert_not_called()
 def test_failure_normalizations_and_default_code_are_unchanged(self):
  with patch.object(failure,'fail_initial_inventory_job_chunk',return_value={'id':ID}) as action:
   self.assertEqual(self.invoke_failure(),{'id':ID})
   action.assert_called_once_with(self.db,job_id=ID,lease_token=ID,error_code='x'*120,playlist_not_found=True)
   action.reset_mock();self.jobs.fail_initial_inventory(ID,ID,error_code='')
   action.assert_called_once_with(self.db,job_id=ID,lease_token=ID,error_code='initial_inventory_error',playlist_not_found=False)
  self.db.call.assert_not_called()
 def operations(self):return ((page,'settle_initial_inventory_job_chunk',self.invoke_page),(failure,'fail_initial_inventory_job_chunk',self.invoke_failure))
 def test_page_and_failure_keep_no_abort_on_definite_admission_denial(self):
  for module,name,call in self.operations():
   with patch.object(module,name,side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(HotDatabaseAdmissionDenied):call()
    action.assert_called_once();abort.assert_not_called()
 def test_page_and_failure_do_not_retry_unknown_commit(self):
  for module,name,call in self.operations():
   error=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
   with patch.object(module,name,side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(DatabaseUnavailable) as caught:call()
    self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
 def test_page_and_failure_invalid_results_remain_errors(self):
  for module,name,call in self.operations():
   with patch.object(module,name,return_value=None) as action:
    with self.assertRaises(TypeError):call()
    action.assert_called_once()
if __name__=='__main__':unittest.main()
