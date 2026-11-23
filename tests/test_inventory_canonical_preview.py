"""Repair preview decision keeps exact caps, blockers and one native snapshot."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_canonical_preview as h
from db.base.postgres import DatabaseServerError,_reviewed_statement


def row(**changes):
 value=dict(canonical_unsupported=1,explicit_conflicts=0,typed_to_typed=0,repairable_count=1,long_to_short=1,
 long_to_stream=0,null_to_short=0,null_to_stream=0,collection_state_rows=1,candidate_active_lease_rows=0,
 current_video_rows=1,snapshot_video_rows=1,candidate_fingerprint='native_fingerprint',runtime_enabled_rows=0,
 product_exposure_rows=0,running_scan_rows=0,collection_trigger_ready=True,examples=[{'video_id':'kept'}]);value.update(changes);return value

class PreviewTests(unittest.TestCase):
 def test_ready_and_nothing_keep_all_counts_and_native_fingerprint(self):
  value=h._result(row());self.assertEqual(value['status'],'ready');self.assertEqual(value['candidate_fingerprint'],'native_fingerprint')
  self.assertEqual((value['schema_version'],value['max_atomic_repairs'],value['collection_state_rows_to_remove'],value['current_video_rows_retained'],value['video_ids_with_snapshots_retained']),(1,5000,1,1,1))
  self.assertFalse(value['canonical_channel_mutation']);self.assertEqual(value['examples'],[{'video_id':'kept'}])
  self.assertEqual(h._result(row(canonical_unsupported=0,repairable_count=0))['status'],'nothing_to_repair')
 def test_each_safety_blocker_has_precedence_over_empty(self):
  for key in ('explicit_conflicts','runtime_enabled_rows','product_exposure_rows','running_scan_rows','candidate_active_lease_rows'):
   with self.subTest(key=key):self.assertEqual(h._result(row(canonical_unsupported=0,repairable_count=0,**{key:1}))['status'],'blocked')
  self.assertEqual(h._result(row(canonical_unsupported=2))['status'],'blocked')
  self.assertEqual(h._result(row(collection_trigger_ready=False))['status'],'blocked')
 def test_count_boundary_is_exact_and_typed_mismatch_not_double_counted(self):
  self.assertEqual(h._result(row(canonical_unsupported=5000,repairable_count=5000))['status'],'ready')
  self.assertEqual(h._result(row(canonical_unsupported=5001,repairable_count=5001))['status'],'blocked')
  value=h._result(row(typed_to_typed=3));self.assertEqual(value['typed_to_typed_mismatches'],3);self.assertEqual(value['status'],'ready')
 def test_native_cast_then_one_aggregate_statement_no_rpc(self):
  class Tx:
   calls=[]
   @contextmanager
   def borrow(self):yield self
   def fetch_one(self,sql,params):
    self.calls.append((sql,params));return {'example_limit':25} if sql==h._INPUT else row()
  tx=Tx();db=Mock(spec=['transaction','call']);result=h.inspect_typed_inventory_canonical_repair(db,'25',transaction=tx)
  self.assertEqual(result['status'],'ready');self.assertEqual(tx.calls,[(h._INPUT,('25',)),(h._READ,dict(example_limit=25))]);db.transaction.assert_not_called();db.call.assert_not_called()
 def test_invalid_native_limit_never_reads_domain_data(self):
  for limit in (None,-1,26):
   tx=Mock();tx.fetch_one.return_value={'example_limit':limit}
   @contextmanager
   def borrow():yield tx
   tx.borrow=borrow
   with self.assertRaises(DatabaseServerError) as caught:h.inspect_typed_inventory_canonical_repair(None,limit,transaction=tx)
   self.assertEqual(caught.exception.message,'typed_inventory_canonical_repair_example_limit_invalid');tx.fetch_one.assert_called_once_with(h._INPUT,(limit,))
 def test_read_statement_uses_actual_driver_guard(self):
  _reviewed_statement(h._READ,read=True,max_rows=1)
  self.assertNotIn('inspect_typed_inventory_canonical_repair(',h._READ)
if __name__=='__main__':unittest.main()
