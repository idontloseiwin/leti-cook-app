"""Schema24 exact progress catalog boundary, upgrade history and read admission."""
from pathlib import Path
import re,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import capabilities,collection_completion_capabilities as checks
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

class Database:
 def __init__(self,row):self.row=row;self.calls=[]
 def read_row(self,*args):self.calls.append(args);return self.row

class CompletionCapabilityTests(unittest.TestCase):
 def test_one_bounded_catalog_query_and_strict_shape(self):
  good=dict(roles_valid=True,relations_valid=True,privileges_valid=True)
  database=Database(good)
  self.assertEqual(checks.assert_collection_completion_ready(database),dict(ready=True,schema_version=24))
  self.assertEqual(database.calls,[('collection_completion_readiness',checks.READINESS_SQL)])
  _reviewed_statement(checks.READINESS_SQL,read=True,max_rows=1)
  PostgresQuery(Transformer()).convert(checks.READINESS_SQL,())
  for key in good:
   with self.assertRaisesRegex(DatabaseError,'collection_completion_acl_invalid') as caught:checks.validate_readiness({**good,key:False})
   self.assertEqual(caught.exception.code,'P0001');self.assertFalse(caught.exception.outcome_unknown)
  for row in (None,{},[],{**good,'roles_valid':1},{**good,'relations_valid':None},{**good,'extra':True}):
   with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):checks.validate_readiness(row)
 def test_exact_runtime_three_control_four_update_columns_no_activation(self):
  self.assertEqual(checks.RUNTIME_UPDATE_COLUMNS,('reconcile_queue_cursor','reconcile_inventory_cursor','updated_at'))
  self.assertEqual(checks.CONTROL_UPDATE_COLUMNS,('reconciliation_required','last_reconciled_at','updated_at','invalidated_at'))
  for table,columns in (('typed_video_collection_runtime',checks.RUNTIME_UPDATE_COLUMNS),('video_collection_control',checks.CONTROL_UPDATE_COLUMNS)):
   self.assertEqual(re.findall(r"\('"+table+r"','([^']+)','UPDATE'\)",checks.READINESS_SQL),list(columns))
   self.assertIn("('"+table+"',ARRAY['SELECT']::text[])",checks.READINESS_SQL)
  for token in ('relrowsecurity',"<>'postgres'",'NOT rolcanlogin','NOT rolsuper','aclexplode(a.attacl)','acl.is_grantable','IS DISTINCT FROM'):
   self.assertIn(token,checks.READINESS_SQL)
  self.assertNotIn('::regclass',checks.READINESS_SQL)
 def test_fixed_hot_name_rejects_each_false_fact(self):
  from test_capabilities import FactsDatabase
  self.assertEqual(capabilities.MAINTENANCE_CAPABILITY_QUERIES['collection_completion'],checks.READINESS_SQL)
  for key in ('roles_valid','relations_valid','privileges_valid'):
   database=FactsDatabase();database.overrides['collection_completion']={name:name!=key for name in ('roles_valid','relations_valid','privileges_valid')}
   with self.assertRaisesRegex(DatabaseError,'collection_completion_acl_invalid'):capabilities.get_hot_runtime_contract(database)
   self.assertEqual(database.calls[-1],('maintenance','collection_completion'))
 def test_current_python_rights(self):
  for source in (capabilities._ASSERT_TYPED_VIDEO_COLLECTION_SCHEMA_SQL,):self.assertIn(checks.READINESS_SQL.strip(),source)
  current=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
  self.assertIn(checks.READINESS_SQL.strip(),current)
  self.assertNotIn('SELECT public.assert_typed_video_collection_schema();',current)
 def test_dispatch_sequence_requires_only_usage(self):
  schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
  for source in (capabilities._ASSERT_INVENTORY_SWITCH_SCHEMA_SQL,):
   for token in ("a.privilege_type<>'USAGE'",'a.is_grantable',"'USAGE') IS DISTINCT FROM true","'SELECT,UPDATE')",'s.seqmin=1 AND NOT s.seqcycle'):
    self.assertIn(token,source)
  self.assertIn('GRANT USAGE ON SEQUENCE operator_private.inventory_dispatch_seq TO service_role;',schema)
  grants=re.findall(r'GRANT\s+([^;]+?)\s+ON\s+SEQUENCE\s+operator_private\.inventory_dispatch_seq\s+TO\s+service_role\s*;',schema,re.I)
  self.assertEqual([' '.join(value.upper().split()) for value in grants],['USAGE'])
if __name__=='__main__':unittest.main()
