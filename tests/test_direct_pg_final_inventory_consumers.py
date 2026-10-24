"""Final inventory cutovers preserve public contracts and dispatch once."""
from pathlib import Path
from unittest.mock import Mock,patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_canonical_preview as preview,inventory_canonical_repair as repair
from db.inventory import inventory_runtime_preparation as preparation
from db.jobs import operator_job_verification as verification
from db.inventory.typed_inventory import TypedInventoryRepository
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseUnavailable
ID='f9800000-0000-4000-8000-000000000001'

class FinalInventoryConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
  self.typed=TypedInventoryRepository(self.db);self.jobs=OperatorJobRepository(self.db)
 def operations(self):
  return ((preview,'inspect_typed_inventory_canonical_repair',lambda:self.typed.inspect_canonical_repair(example_limit='2'),dict(example_limit=2)),
   (repair,'repair_typed_inventory_canonical_types',lambda:self.typed.repair_canonical_types(expected_count='3',expected_fingerprint=' '+('A'*32)+' '),dict(expected_count=3,expected_fingerprint='a'*32)),
   (preparation,'prepare_typed_inventory_runtime',lambda:self.typed.prepare_runtime(limit='10',enable=1),dict(limit=10,enable=True)),
   (verification,'verify_initial_hydration_job',lambda:self.jobs.verify_initial_hydration(ID,ID),dict(job_id=ID,lease_token=ID)))
 def test_existing_normalization_routes_only_to_python(self):
  for module,name,call,expected in self.operations():
   with self.subTest(name=name),patch.object(module,name,return_value={'status':'ok'}) as action:
    self.assertEqual(call(),{'status':'ok'});action.assert_called_once_with(self.db,**expected)
  self.db.call.assert_not_called()
 def test_public_invalid_inputs_never_open_transaction(self):
  for call in (lambda:self.typed.inspect_canonical_repair(example_limit=26),
    lambda:self.typed.repair_canonical_types(expected_count=0,expected_fingerprint='a'*32),
    lambda:self.typed.repair_canonical_types(expected_count=1,expected_fingerprint='not-a-hash'),
    lambda:self.typed.prepare_runtime(limit=0,enable=True)):
   with self.assertRaises(ValueError):call()
  self.db.transaction.assert_not_called();self.db.call.assert_not_called()
 def test_invalid_mapping_result_still_rejected(self):
  for module,name,call,_ in self.operations():
   with self.subTest(name=name),patch.object(module,name,return_value=None) as action:
    with self.assertRaises(TypeError):call()
    action.assert_called_once()
 def test_unknown_commit_does_not_retry_or_abort(self):
  error=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)
  for module,name,call,_ in self.operations():
   with self.subTest(name=name),patch.object(module,name,side_effect=error) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
    with self.assertRaises(DatabaseUnavailable) as caught:call()
    self.assertIs(caught.exception,error);action.assert_called_once();abort.assert_not_called()
 def test_typed_paths_keep_operation_telemetry(self):
  for module,name,call,_ in self.operations()[:3]:
   with patch.object(module,name,return_value={}),patch('db.inventory.typed_inventory.emit_event') as emit:call()
   self.assertEqual([c.args[3] for c in emit.call_args_list],['rpc:'+name]*2)
   self.assertEqual([c.kwargs['status'] for c in emit.call_args_list],['started','success'])
if __name__=='__main__':unittest.main()
