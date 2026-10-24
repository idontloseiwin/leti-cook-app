"""Public job preparation retains projections and never retries unknown writes."""
import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.jobs import operator_job_inventory_preparation as helper
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied
ID=UUID('f8500000-0000-4000-8000-000000000001')
class PreparationConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
 def invoke(self):return OperatorJobRepository(self.db).prepare_initial_inventory_chunk(ID,ID,page_limit=5)
 def test_exact_dispatch_and_result_projection(self):
  result=dict(job={'id':str(ID)},action='inventory',page_limit=1,reserved_units=1,video_type='short',
    transition_id=str(ID),scan_id=None,resume_page_token='synthetic',evidence={'complete':True},reason='discarded_by_existing_contract')
  with patch.object(helper,'prepare_initial_inventory_job_chunk',return_value=result) as action:
   self.assertEqual(self.invoke(),{k:v for k,v in result.items() if k!='reason'})
   action.assert_called_once_with(self.db,job_id=str(ID),lease_token=str(ID),page_limit=5,lease_seconds=1200)
  self.db.call.assert_not_called()
 def test_existing_missing_optional_defaults(self):
  with patch.object(helper,'prepare_initial_inventory_job_chunk',return_value={'job':{}}):
   self.assertEqual(self.invoke(),dict(job={},action='blocked',page_limit=0,resume_page_token=None,reserved_units=0,video_type='long',transition_id=None,scan_id=None,evidence=None))
 def test_unknown_commit_is_propagated_once_without_abort(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(helper,'prepare_initial_inventory_job_chunk',side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(DatabaseUnavailable) as caught:self.invoke()
   self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
 def test_existing_no_abort_admission_contract(self):
  with patch.object(helper,'prepare_initial_inventory_job_chunk',side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(HotDatabaseAdmissionDenied) as caught:self.invoke()
   self.assertTrue(caught.exception.outcome_may_be_committed);action.assert_called_once();abort.assert_not_called()
 def test_malformed_job_or_evidence_fails_after_one_dispatch(self):
  for value in ({'job':None},{'job':{},'evidence':1},None):
   with patch.object(helper,'prepare_initial_inventory_job_chunk',return_value=value) as action:
    with self.assertRaises(TypeError):self.invoke()
    action.assert_called_once()
if __name__=='__main__':unittest.main()
