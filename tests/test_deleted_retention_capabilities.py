"""Exact schema22 cleanup rights, fail-closed boundary and immutable upgrades."""
from pathlib import Path
import sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import capabilities,deleted_channel_retention_capabilities as private,video_failure_capabilities as queue
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

class Database:
 def __init__(self,row):self.row=row;self.calls=[]
 def read_row(self,*args):self.calls.append(args);return self.row

class DeletedRetentionCapabilitiesTests(unittest.TestCase):
 def test_private_read_is_one_closed_catalog_statement_and_strict_facts(self):
  good=dict(roles_valid=True,relations_valid=True,privileges_valid=True)
  database=Database(good)
  self.assertEqual(private.assert_deleted_channel_retention_ready(database),dict(ready=True,schema_version=22))
  self.assertEqual(database.calls,[('deleted_channel_retention_readiness',private.READINESS_SQL)])
  _reviewed_statement(private.READINESS_SQL,read=True,max_rows=1)
  PostgresQuery(Transformer()).convert(private.READINESS_SQL,())
  for key in good:
   with self.assertRaisesRegex(DatabaseError,'deleted_channel_retention_acl_invalid') as caught:private.validate_readiness({**good,key:False})
   self.assertEqual(caught.exception.code,'P0001');self.assertFalse(caught.exception.outcome_unknown)
  for row in (None,{},[],{**good,'roles_valid':1},{**good,'relations_valid':None},{**good,'extra':True}):
   with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):private.validate_readiness(row)
 def test_private_schema_owner_rls_roles_and_effective_raw_acl_fail_closed(self):
  query=private.READINESS_SQL
  for token in ("('operator_job_retry_requests',ARRAY['SELECT','INSERT','DELETE']::text[])","to_regclass('operator_private.'",'relrowsecurity',"<>'postgres'",'NOT rolcanlogin','NOT rolsuper','aclexplode(a.attacl)','acl.is_grantable',"acldefault('n',n.nspowner)","acl.privilege_type<>'USAGE'","has_schema_privilege(roles.service_oid,n.oid,'CREATE')",'IS DISTINCT FROM'):
   self.assertIn(token,query)
  self.assertNotIn('::regclass',query);self.assertNotRegex(query,r'operator_private\.(?:assert_|get_)')
 def test_new_hot_name_uses_same_fixed_query_and_fails_before_admission(self):
  self.assertEqual(capabilities.MAINTENANCE_CAPABILITY_QUERIES['deleted_channels'],private.READINESS_SQL)
  from test_capabilities import FactsDatabase
  for key in ('roles_valid','relations_valid','privileges_valid'):
   database=FactsDatabase();database.overrides['deleted_channels']={name:name!=key for name in ('roles_valid','relations_valid','privileges_valid')}
   with self.assertRaisesRegex(DatabaseError,'deleted_channel_retention_acl_invalid'):capabilities.get_hot_runtime_contract(database)
   self.assertEqual(database.calls[-1],('maintenance','deleted_channels'))
 def test_queue_column_lists_are_exact(self):
  self.assertEqual(queue.UPDATE_COLUMNS[20:25],('video_type','typed_source_published_at','typed_source_published_acquired_at','typed_stream_actual_end_at','typed_source_stream_end_acquired_at'))
  self.assertEqual((len(queue.UPDATE_COLUMNS),len(queue.INSERT_COLUMNS)),(32,18))
  self.assertNotIn('created_at',queue.UPDATE_COLUMNS)
 def test_current_rights_and_no_stored_readiness_call(self):
  self.assertIn(queue.READINESS_SQL.strip(),capabilities._ASSERT_TYPED_INVENTORY_CANONICAL_REPAIR_SCHEMA_SQL)
  current_schema=(ROOT/'schema/schema.sql').read_text()
  self.assertNotIn('SELECT public.assert_typed_inventory_canonical_repair_schema();',current_schema)
if __name__=='__main__':unittest.main()
