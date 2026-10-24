"""Initial preparation keeps public validation/telemetry and one dispatch."""
from pathlib import Path
import sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_initial_preparation as helper
from db.inventory.typed_inventory import TypedInventoryRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied

class InitialPreparationConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden');self.repo=TypedInventoryRepository(self.db)
 def test_trim_mapping_and_telemetry_keep_public_contract(self):
  expected=helper._result('UCchannel','ok',3)
  with patch.object(helper,'prepare_channel_initial_typed_inventory',return_value=expected) as call,patch('db.inventory.typed_inventory.emit_event') as emit:
   self.assertEqual(self.repo.prepare_channel_initial_inventory(' UCchannel '),expected)
  call.assert_called_once_with(self.db,channel_id='UCchannel');self.db.call.assert_not_called()
  self.assertEqual([x.kwargs['status'] for x in emit.call_args_list],['started','success'])
  self.assertEqual([x.args[3] for x in emit.call_args_list],['rpc:prepare_channel_initial_typed_inventory']*2)
 def test_empty_rejected_before_helper(self):
  with patch.object(helper,'prepare_channel_initial_typed_inventory') as call:
   with self.assertRaises(ValueError):self.repo.prepare_channel_initial_inventory(' ')
  call.assert_not_called();self.db.call.assert_not_called()
 def test_unknown_commit_propagates_once_without_abort(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(helper,'prepare_channel_initial_typed_inventory',side_effect=error) as call,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(DatabaseUnavailable) as caught:self.repo.prepare_channel_initial_inventory('UCchannel')
  self.assertIs(caught.exception,error);call.assert_called_once();abort.assert_not_called();self.db.call.assert_not_called()
 def test_unowned_admission_denial_does_not_abort_some_other_owner(self):
  with patch.object(helper,'prepare_channel_initial_typed_inventory',side_effect=HotDatabaseAdmissionDenied()) as call,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(HotDatabaseAdmissionDenied):self.repo.prepare_channel_initial_inventory('UCchannel')
  call.assert_called_once();abort.assert_not_called()
 def test_invalid_result_rejected_after_one_dispatch(self):
  for result in (None,[],5):
   with self.subTest(result=result),patch.object(helper,'prepare_channel_initial_typed_inventory',return_value=result) as call:
    with self.assertRaises(TypeError):self.repo.prepare_channel_initial_inventory('UCchannel')
    call.assert_called_once()
if __name__=='__main__':unittest.main()
