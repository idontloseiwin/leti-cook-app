"""Public job read methods keep input/output contracts and use named Python reads."""
from pathlib import Path
import sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.jobs.operator_jobs import OperatorJobRepository
from db.jobs import operator_job_read_queries as reads
from db.base.postgres import DatabaseUnavailable
class JobReadConsumers(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.repo=OperatorJobRepository(self.db)
 def test_latest_uses_trimmed_input_and_named_read(self):
  for name,call in (('get_operator_job_status',self.repo.get_channel_job),('get_channel_initial_inventory_job',self.repo.get_initial_inventory_job)):
   for value in (None,{'id':'job','status':'suspended'}):
    with patch.object(reads,name,return_value=value) as action:
     self.assertEqual(call(' channel '),value);action.assert_called_once_with(self.db,'channel')
  self.db.call.assert_not_called()
 def test_empty_channel_rejected_before_transaction(self):
  for call in (self.repo.get_channel_job,self.repo.get_initial_inventory_job):
   with self.assertRaises(ValueError):call(' ')
  self.db.transaction.assert_not_called();self.db.call.assert_not_called()
 def test_list_retains_public_int_conversion_and_projection(self):
  with patch.object(reads,'list_active_operator_jobs',return_value=[{'id':'job'}]) as action:
   self.assertEqual(self.repo.list_active(limit='3'),[{'id':'job'}]);action.assert_called_once_with(self.db,3)
  with patch.object(reads,'list_active_operator_jobs',return_value=[4]):
   with self.assertRaises(TypeError):self.repo.list_active()
 def test_read_error_does_not_fall_back_to_rpc(self):
  with patch.object(reads,'get_operator_job_status',side_effect=DatabaseUnavailable('database_unavailable')) as action:
   with self.assertRaises(DatabaseUnavailable):self.repo.get_channel_job('channel')
   action.assert_called_once()
  self.db.call.assert_not_called()
if __name__=='__main__':unittest.main()
