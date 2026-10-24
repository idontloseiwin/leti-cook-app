"""Finite mode/count policy guard cases; no server or provider requests."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from channel_sync_policy import channel_policy_from_native_facts,channel_sync_eligibility
from channel_inventory_estimate import (COUNT_PRECLOCK_FACTS,inventory_count_from_native_facts,
    InventoryEstimateCountError)
from db.channel import channel_read_policy as sql
from db.reads import operational_read_queries as readers

def facts(**changes):
    result={name:False for name in sql.FACT_SQL}
    result.update(control_present=True,permission_accepted=True,linked=True,types_long=True,
        status_completed=True,errors_zero=True,phase_initial_stats=True)
    return dict(result,**changes)

class ModeTests(unittest.TestCase):
 def test_long_success_and_skip_priority_before_error_count(self):
    self.assertEqual(channel_policy_from_native_facts(facts())['mode'],'active')
    result=channel_policy_from_native_facts(facts(initial_skipped=True,errors_zero=False))
    self.assertEqual(result,dict(mode='active',typed_runtime_eligible=True,pause_reason=None))
 def test_terminal_no_long_is_empty_inventory_not_a_channel_hold(self):
    for linked in (False,True):
      sample=facts(linked=linked,initial_skipped=True,errors_zero=False)
      with self.subTest(linked=linked):
        result=channel_policy_from_native_facts(sample)
        self.assertEqual(result,dict(mode='active',typed_runtime_eligible=True,pause_reason=None))
        public=channel_sync_eligibility(dict(collection_mode=result['mode'],collection_permission='accepted',collection_control_epoch=7))
        self.assertTrue(public.allow_discovery and public.allow_video_collection and public.allow_typed_inventory)
 def test_skipped_marker_does_not_promote_unfinished_or_blocked_all_type_initial(self):
    sample=facts(types_long=False,types_all=True,type_count_three=True,scope_full=True,
                 initial_skipped=True,status_completed=False,phase_initial_stats=False)
    self.assertEqual(channel_policy_from_native_facts(sample)['mode'],'initial_inventory')
    for change in (dict(status_blocked=True),dict(status_error=True),dict(status_suspended=True),
                   dict(status_completed=True,frozen_null=True),dict(permission_not_accepted=True),dict(hidden=True)):
      with self.subTest(change=change):
        self.assertNotEqual(channel_policy_from_native_facts(dict(sample,**change))['mode'],'active')
 def test_full_initial_without_long_finishes_active_but_owner_holds_still_win(self):
    sample=facts(types_long=False,types_all=True,type_count_three=True,scope_full=True,
                 initial_skipped=True,phase_initial_stats=False,phase_verify=True)
    self.assertEqual(channel_policy_from_native_facts(sample),
                     dict(mode='active',typed_runtime_eligible=True,pause_reason=None))
    for changes,mode in ((dict(hidden=True,pause_hidden=True),'hidden'),
                         (dict(permission_not_accepted=True,permission_accepted=False),'operator_hold')):
      with self.subTest(changes=changes):
        result=channel_policy_from_native_facts(dict(sample,**changes))
        self.assertEqual(result['mode'],mode)
        self.assertFalse(result['typed_runtime_eligible'])
 def test_full_target_and_failure_checks_precede_completed_selection(self):
    for changes in (dict(scope_full=True,frozen_null=True),dict(scope_full=True,status_error=True),dict(epoch_null=True),dict(job_channel_distinct=True)):
        self.assertEqual(channel_policy_from_native_facts(facts(initial_skipped=True,**changes))['mode'],'operator_hold')
 def test_null_permission_comparison_remains_unknown(self):
    result=channel_policy_from_native_facts(facts(permission_not_accepted=None,permission_accepted=None))
    self.assertEqual(result['mode'],'active');self.assertFalse(result['typed_runtime_eligible'])
 def test_missing_control_hidden_default_and_no_job_status(self):
    result=channel_policy_from_native_facts(facts(control_present=False,hidden=None,pause_hidden=True))
    self.assertEqual(result,dict(mode='operator_hold',typed_runtime_eligible=False,pause_reason='channel_hidden'))
    self.assertEqual(channel_policy_from_native_facts(facts(linked=False,initial_completed=True))['mode'],'active')
 def test_initial_runtime_permission_does_not_change_public_flag(self):
    result=channel_policy_from_native_facts(facts(status_completed=False))
    self.assertEqual(result['mode'],'initial_inventory');self.assertTrue(result['typed_runtime_eligible'])
    public=channel_sync_eligibility(dict(collection_mode='initial_inventory',collection_permission='accepted',collection_control_epoch=1))
    self.assertFalse(public.allow_typed_inventory)
 def test_native_boolean_shape_rejects_truthy_integer(self):
    with self.assertRaises(ValueError):channel_policy_from_native_facts(facts(hidden=1))
 def test_exact_array_semantics_stay_native(self):
    self.assertIn("IS NOT DISTINCT FROM ARRAY['long']::text[]",sql.FACT_SQL['types_long'])
    self.assertEqual(sql.FACT_SQL['type_count_three'],'cardinality(job.initial_video_types)=3')
    self.assertEqual(channel_policy_from_native_facts(facts(types_long=False,types_all=False))['mode'],'operator_hold')
 def test_case_emission_is_closed_and_null_safe(self):
    self.assertNotIn(' = ANY(',sql.CHANNEL_MODE_CASE)
    self.assertIn('facts.permission_not_accepted IS TRUE',sql.CHANNEL_MODE_CASE)
    self.assertIn('facts.types_long IS FALSE AND facts.types_all IS FALSE',sql.CHANNEL_MODE_CASE)
    with self.assertRaises(AssertionError):sql._case_sql([type('Bad',(),{'terms':(('injected;sql',True),),'result':'active'})()], 'operator_hold')

class CountTests(unittest.TestCase):
 def base(self,**changes):
    result={name:True for name in COUNT_PRECLOCK_FACTS}
    return dict(result,count=2147483647,clock_error=None,clock_valid=True,**changes)
 def test_each_preclock_rejection_suppresses_later_error(self):
    for key in COUNT_PRECLOCK_FACTS:
      for value in (False,None):
        sample=self.base();sample.update({key:value,'clock_error':'22009'})
        self.assertIsNone(inventory_count_from_native_facts(sample))
 def test_exact_soft_clock_whitelist_and_unhandled_error(self):
    for code in ('22007','22008','22003'):
      sample=self.base();sample['clock_error']=code;self.assertIsNone(inventory_count_from_native_facts(sample))
    sample=self.base();sample['clock_error']='22009'
    with self.assertRaises(InventoryEstimateCountError) as raised:inventory_count_from_native_facts(sample)
    self.assertEqual(raised.exception.sqlstate,'22009')
 def test_zero_maximum_and_unknown_clocks(self):
    for value in (0,2147483647):
      sample=self.base();sample['count']=value;self.assertEqual(inventory_count_from_native_facts(sample),value)
    for value in (False,None):
      sample=self.base();sample['clock_valid']=value;self.assertIsNone(inventory_count_from_native_facts(sample))
 def test_clock_scope_fallback_and_three_original_expression_sites(self):
    self.assertEqual(readers.CHANNEL_COVERAGE_SQL.count(sql.LONG_ESTIMATE_COUNT_SQL),3)
    self.assertIn("CASE WHEN estimate ? 'checked_at_by_type' THEN estimate->'checked_at_by_type'->>kind",sql.LONG_ESTIMATE_COUNT_SQL)
    self.assertIn("statement_timestamp()",sql.LONG_ESTIMATE_COUNT_SQL)
    self.assertNotIn('clock_timestamp()',sql.LONG_ESTIMATE_COUNT_SQL)
    self.assertIn("ELSE clock_text::timestamptz END",sql.LONG_ESTIMATE_COUNT_SQL)
    self.assertIn('greatest(base.expected_long_count - base.videos_in_db, 0)',readers.CHANNEL_COVERAGE_SQL)
    self.assertIn('round((base.videos_in_db::numeric / base.expected_long_count::numeric) * 100, 2)',readers.CHANNEL_COVERAGE_SQL)
    self.assertIn('coalesce(visibility.is_hidden, true) = false',readers.CHANNEL_COVERAGE_SQL)

if __name__=='__main__':unittest.main()
