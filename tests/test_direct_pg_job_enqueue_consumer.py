"""Job enqueue cutover keeps coercion, generations and single dispatch."""
from pathlib import Path
import sys,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.jobs.operator_jobs import OperatorJobRepository
from db.jobs import operator_job_enqueuing as helper
from db.base.postgres import DatabaseUnavailable
from hot_runtime import HotDatabaseAdmissionDenied

class JobEnqueueConsumers(unittest.TestCase):
 def setUp(self):
  self.db=Mock(spec=['call','transaction']);self.db.call.side_effect=AssertionError('RPC forbidden');self.repo=OperatorJobRepository(self.db)
 def test_estimate_uses_trimmed_channel_without_other_coercion(self):
  payload={'estimated_items':None,'counts':{'long':0}}
  with patch.object(helper,'estimate_channel_initial_inventory_job',return_value=payload) as action:
   self.assertEqual(self.repo.estimate_initial_inventory(' channel '),payload);action.assert_called_once_with(self.db,'channel')
  self.db.call.assert_not_called()
 def test_stats_exact_limits_epoch_and_existing_projection(self):
  with patch.object(helper,'enqueue_channel_current_stats_backfill',return_value={'created':1,'job':{'id':'id'},'internal':'unchanged'}) as action:
   self.assertEqual(self.repo.enqueue_current_stats(' channel ',expected_epoch=7,max_items=None,max_quota_units='100',max_target_attempts='3'),{'created':True,'job':{'id':'id'}})
   action.assert_called_once_with(self.db,channel_id='channel',max_items=None,max_quota_units=100,max_target_attempts=3,expected_epoch=7)
  self.db.call.assert_not_called()
 def initial(self,**changes):
  args=dict(expected_epoch=7,expected_items='12',expected_pages='3',expected_quota_units=None,max_items=None,max_pages=None,max_quota_units=None,max_attempts='3',confirmed=True)
  args.update(changes);return self.repo.enqueue_initial_inventory(' channel ',**args)
 def test_initial_keeps_explicit_nulls_and_expected_values(self):
  with patch.object(helper,'enqueue_channel_initial_inventory_job',return_value={'created':False,'job':{'id':'id'}}) as action:
   self.assertEqual(self.initial(),{'created':False,'job':{'id':'id'}})
   action.assert_called_once_with(self.db,channel_id='channel',expected_items=12,expected_pages=3,expected_quota_units=None,
    max_items=None,max_pages=None,max_quota_units=None,max_attempts=3,confirmed=True,expected_epoch=7)
 def test_invalid_generation_rejected_before_dispatch(self):
  for epoch in (None,0,-1,'7',True):
   with self.assertRaises(ValueError):self.initial(expected_epoch=epoch)
  self.db.transaction.assert_not_called();self.db.call.assert_not_called()
 def test_unknown_ack_has_no_automatic_retry_or_fallback(self):
  for name,call in (('enqueue_channel_initial_inventory_job',self.initial),('enqueue_channel_current_stats_backfill',lambda:self.repo.enqueue_current_stats('channel',expected_epoch=7,max_items=None,max_quota_units=100))):
   with patch.object(helper,name,side_effect=DatabaseUnavailable('database_connection_failed',outcome_unknown=True)) as action:
    with self.assertRaises(DatabaseUnavailable) as caught:call()
    self.assertTrue(caught.exception.outcome_unknown);action.assert_called_once()
  self.db.call.assert_not_called()
 def test_new_job_admission_denial_does_not_abort_some_existing_owner(self):
  with patch.object(helper,'enqueue_channel_initial_inventory_job',side_effect=HotDatabaseAdmissionDenied()) as action,patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
   with self.assertRaises(HotDatabaseAdmissionDenied):self.initial()
   action.assert_called_once();abort.assert_not_called()
 def test_invalid_helper_shape_remains_visible(self):
  with patch.object(helper,'enqueue_channel_initial_inventory_job',return_value={'created':True,'job':None}):
   with self.assertRaises(TypeError):self.initial()
if __name__=='__main__':unittest.main()
