"""Public scan binding keeps acknowledgement semantics after direct cutover."""
import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.jobs import operator_job_inventory_binding as binding
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied
ID=UUID('f8500000-0000-4000-8000-000000000001')

class BindingConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
 def invoke(self):
  return OperatorJobRepository(self.db).bind_initial_inventory_scan(ID,ID,
      playlist_lease_token=ID,scan_id=ID,transition_id=ID)
 def test_direct_binding_preserves_all_identities_and_result_projection(self):
  result={'status':'bound','dispatch_allowed':False,'replayed':True}
  with patch.object(binding,'bind_initial_inventory_scan',return_value=result) as action:
   self.assertEqual(self.invoke(),result)
   action.assert_called_once_with(self.db,job_id=str(ID),job_lease_token=str(ID),
       playlist_lease_token=str(ID),scan_id=str(ID),transition_id=str(ID))
  self.db.call.assert_not_called()
 def test_unknown_ack_is_not_automatically_repeated_or_aborted(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(binding,'bind_initial_inventory_scan',side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(DatabaseUnavailable) as caught:self.invoke()
   self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
  self.db.call.assert_not_called()
 def test_denied_old_binding_preserves_existing_no_abort_contract(self):
  with patch.object(binding,'bind_initial_inventory_scan',side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(HotDatabaseAdmissionDenied):self.invoke()
   action.assert_called_once();abort.assert_not_called()
  self.db.call.assert_not_called()
 def test_malformed_result_rejected_after_one_direct_attempt(self):
  for result in (None,1,[]):
   with patch.object(binding,'bind_initial_inventory_scan',return_value=result) as action:
    with self.assertRaises(TypeError):self.invoke()
    action.assert_called_once()
  self.db.call.assert_not_called()

if __name__=='__main__':unittest.main()
