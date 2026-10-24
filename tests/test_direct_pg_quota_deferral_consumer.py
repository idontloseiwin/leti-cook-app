"""Exact receipt replay and input bounds at the public quota-deferral boundary."""
from datetime import datetime,timezone
from pathlib import Path
import sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_quota_deferral as helper
from db.inventory.typed_inventory import TypedInventoryRepository,TypedInventoryClaim
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied
ID='fd000000-0000-4000-8000-000000000001'
CHANNEL='UC'+'a'*22
NOW=datetime(2026,10,6,tzinfo=timezone.utc)
def claim(kind='long'):
 return TypedInventoryClaim(CHANNEL,kind,'playlist',ID,7,'2026-10-06T01:00:00+00:00',0,None,None,'discovery')
class QuotaDeferralConsumerTests(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden')
 def invoke(self,claims=None,transition=' '+ID+' '):
  return TypedInventoryRepository(self.db).defer_claims_after_daily_quota(
      claims if claims is not None else [claim()],transition_id=transition,source_checked_at=NOW)
 def test_three_types_preserve_lease_identity_clock_and_result(self):
  result={'released':3,'replayed':False}
  with patch.object(helper,'defer_typed_inventory_claims_after_daily_quota',return_value=result) as action:
   self.assertEqual(self.invoke([claim(k) for k in ('long','short','stream')]),result)
   action.assert_called_once_with(self.db,transition_id=ID,claims=[
       dict(channel_id=CHANNEL,video_type=k,lease_token=ID) for k in ('long','short','stream')],source_checked_at=NOW.isoformat())
  self.db.call.assert_not_called()
 def test_unknown_ack_repeats_only_identical_transition_payload_once(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(helper,'defer_typed_inventory_claims_after_daily_quota',side_effect=[error,{'replayed':True}]) as action:
   self.assertTrue(self.invoke()['replayed']);self.assertEqual(action.call_count,2)
   self.assertEqual(action.call_args_list[0],action.call_args_list[1])
  self.db.call.assert_not_called()
 def test_second_failure_propagates_without_third_attempt(self):
  error=DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)
  with patch.object(helper,'defer_typed_inventory_claims_after_daily_quota',side_effect=error) as action:
   with self.assertRaises(DatabaseUnavailable):self.invoke()
   self.assertEqual(action.call_count,2)
 def test_admission_denial_does_not_retry_owned_release(self):
  with patch.object(helper,'defer_typed_inventory_claims_after_daily_quota',side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort',return_value={}):
   with self.assertRaises(HotDatabaseAdmissionDenied):self.invoke()
   action.assert_called_once()
 def test_invalid_input_precedes_any_dispatch(self):
  with patch.object(helper,'defer_typed_inventory_claims_after_daily_quota') as action:
   for values,transition,error in (([],ID,ValueError),([claim(),claim()],ID,ValueError),([object()],ID,TypeError),([claim()],' ',ValueError)):
    with self.subTest(values=values),self.assertRaises(error):self.invoke(values,transition)
   action.assert_not_called()
 def test_bad_result_is_not_accepted(self):
  with patch.object(helper,'defer_typed_inventory_claims_after_daily_quota',return_value=None) as action:
   with self.assertRaises(TypeError):self.invoke()
   action.assert_called_once()
if __name__=='__main__':unittest.main()
