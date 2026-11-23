"""Native policy-row projection transport contracts for direct readers."""
from pathlib import Path
import copy,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.channel.channel_read_policy import FACT_SQL,channel_mode_row,channel_eligible_row,channel_pause_row
from db.base.postgres import DatabaseError
from channel_policy_fixtures import native_policy_facts,native_eligible_facts,native_pause_facts

class PolicyRowTests(unittest.TestCase):
 def test_native_fixture_states_and_full_fact_vocabulary(self):
  for state,mode,eligible,pause in (('active','active',True,None),('metadata_only','active',True,None),
   ('operator_hold','operator_hold',False,'channel_inventory_not_ready'),('hidden','hidden',False,'channel_hidden'),
   ('manual_required','operator_hold',False,'channel_inventory_manual_required'),('initial_inventory','initial_inventory',True,'channel_inventory_not_ready')):
   with self.subTest(state=state):
    facts=native_policy_facts(state);self.assertEqual(set(facts),set(FACT_SQL));row={'policy_facts':facts}
    self.assertEqual(channel_mode_row(row)['collection_mode'],mode);self.assertIs(channel_eligible_row(row)['eligible'],eligible);self.assertEqual(channel_pause_row(row)['pause_reason'],pause)
 def test_absent_row_and_absent_scalar_keep_distinct_shapes(self):
  for project in (channel_mode_row,channel_eligible_row,channel_pause_row):self.assertIsNone(project(None))
  self.assertEqual(channel_mode_row({'policy_facts':None},field='mode'),{'mode':'operator_hold'})
  self.assertEqual(channel_eligible_row({'policy_facts':None},field='valid'),{'valid':False})
  self.assertEqual(channel_pause_row({'open_incident':False,'policy_facts':None},field='pause'),{'open_incident':False,'pause':None})
 def test_hidden_null_is_not_false_or_python_truthiness(self):
  facts=native_policy_facts();facts['hidden']=None;facts['pause_hidden']=True
  self.assertEqual(channel_mode_row({'policy_facts':facts})['collection_mode'],'active')
  self.assertIs(channel_eligible_row({'policy_facts':facts})['eligible'],False)
  self.assertEqual(channel_pause_row({'policy_facts':facts})['pause_reason'],'channel_hidden')
 def test_missing_control_preserves_channel_hidden_pause(self):
  facts=native_policy_facts();facts.update(control_present=False,hidden=None,pause_hidden=True,permission_accepted=None,permission_not_accepted=None,permission_manual=None)
  self.assertEqual(channel_pause_row({'policy_facts':facts})['pause_reason'],'channel_hidden')
  self.assertIs(channel_eligible_row({'policy_facts':facts})['eligible'],False)
 def test_native_other_values_and_input_are_unchanged(self):
  row={'control_epoch':9007199254740993,'checked_at':'0001-01-01 BC','published_at':'10000-01-01 UTC','optional':None,'policy_facts':native_policy_facts()};before=copy.deepcopy(row)
  result=channel_mode_row(row);self.assertEqual(row,before);self.assertEqual(result,{**{k:v for k,v in row.items() if k!='policy_facts'},'collection_mode':'active'})
 def test_malformed_transport_facts_fail_closed(self):
  for row in ([],{}, {'policy_facts':{}},{'policy_facts':dict(native_policy_facts(),hidden=1)},{'policy_facts':dict(native_policy_facts(),unexpected=False)}):
   with self.subTest(row=row),self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):channel_mode_row(row)

if __name__=='__main__':unittest.main()
