"""Readiness catalog guards: shared facts, strict shapes and exact rights; offline only."""
from pathlib import Path
import re,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import capabilities as c,intrinsic_capabilities as g
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_capabilities import FactsDatabase,reviewed_chain_facts
GOOD=dict(roles_valid=True,functions_valid=True,triggers_valid=True)
def quoted(s,i):
 q=s[i];i+=1
 while i<len(s):
  if s[i]=='\\':i+=2;continue
  if s[i]==q:
   if i+1<len(s) and s[i+1]==q:i+=2;continue
   return i+1
  i+=1
 raise AssertionError('quote')
def close(s,i):
 stack=[s[i]];j=i+1
 while j<len(s):
  if s[j] in "'\"":j=quoted(s,j);continue
  if s[j] in '([':stack.append(s[j])
  elif s[j] in ')]':
   assert stack.pop()=={')':'(',']':'['}[s[j]]
   if not stack:return j
  j+=1
 raise AssertionError('brackets')
def slots(sql):
 opening=re.search(r'SELECT\s+ARRAY\[',sql).end()-1;ending=close(sql,opening);text=sql[opening+1:ending]
 out=[];start=i=0
 while i<len(text):
  if text[i] in "'\"":i=quoted(text,i);continue
  if text[i] in '([':i=close(text,i)+1;continue
  if text[i]==',':out.append(text[start:i].strip());start=i+1
  i+=1
 out.append(text[start:].strip());return ([] if out==[''] else out),sql[ending+1:]

class ReadinessGuards(unittest.TestCase):
 def setUp(self):reviewed_chain_facts(self)
 def test_mixed_checks_keep_private_rights_views_and_target_guard(self):
  full=c._QUERIES['assert_full_initial_schema'];self.assertIn('public.guard_hot_operator_target_scope()',full);self.assertNotIn('public.enqueue_channel_initial_inventory_job(',full)
  pool=c._QUERIES['assert_video_collection_common_pool_schema'];self.assertIn('open_video_availability',pool);self.assertIn('has_table_privilege',pool);self.assertNotIn('complete_typed_video_missing',pool)
  typed=c._QUERIES['assert_typed_video_collection_schema'];self.assertNotIn('typed_video_collection_candidates',typed)
  for text in ('direct_completion_rights','direct_typed_receipt_rights',"('video_collection_control','invalidated_at','UPDATE')"):
   self.assertIn(text,typed)
  jobs=c._QUERIES['assert_operator_jobs_ready']
  for text in ('operator_private.operator_job_retry_requests','accepted_epoch','lease_channel_epoch','request_control_epoch'):self.assertIn(text,jobs)
 def test_every_shared_false_fact_rejects_before_any_domain_query(self):
  for key in GOOD:
   db=FactsDatabase();db.overrides['final_schema']={**GOOD,key:False}
   with self.assertRaisesRegex(DatabaseError,'database_operation_failed') as caught:c.assert_typed_inventory_runtime_schema(db)
   self.assertEqual(caught.exception.code,'P0001');self.assertEqual(len(db.calls),2);self.assertEqual([call[0] for call in db.calls],['capability_schema_release','capability_final_schema'])
 def test_shared_shape_is_strict_and_no_integer_truth_coercion(self):
  for row in (None,{},[],{**GOOD,'extra':True},{**GOOD,'functions_valid':1},{**GOOD,'roles_valid':None}):
   with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):g.validate_readiness(row)
  g.validate_readiness(GOOD)
 def test_shared_fact_once_each_call_no_cross_call_cache(self):
  db=FactsDatabase();c.assert_typed_inventory_runtime_schema(db);self.assertEqual(sum(v[0]=='capability_final_schema' for v in db.calls),1)
  c.assert_typed_inventory_runtime_schema(db);self.assertEqual(sum(v[0]=='capability_final_schema' for v in db.calls),2)
 def test_no_permissive_schema_or_unknown_processing_hooks(self):
  sql=g.READINESS_SQL
  self.assertNotIn("obj_description",sql);self.assertNotIn("schema_migrations",sql)
  self.assertIn('(SELECT count(*) FROM functions)=19',sql);self.assertIn('(SELECT count(*) FROM triggers)=42',sql)
  self.assertIn("n.nspname IN ('public','operator_private')",sql);self.assertIn('NOT t.tgisinternal',sql)
  for field in ('proowner','proargnames','proconfig','prosrc','prosecdef','prolang','provolatile','proretset','proparallel','is_grantable','tgattr','tgenabled','tgtype','tgqual','tgfoid','tgargs','tgconstraint','tgoldtable','tgnewtable','procost','prorows','prosupport','provariadic'):self.assertIn(field,sql)
  self.assertNotIn('schema_migrations ',sql);self.assertNotIn('FROM operator_private.schema_migrations',sql)
 def test_same_strict_fact_drives_hot_alongside_schedule_compatibility(self):
  self.assertIn(g.READINESS_SQL,c._HOT_RUNTIME_SQL);self.assertIn('clock_timestamp()',c._HOT_RUNTIME_SQL)
  self.assertNotIn('cleanup_hot_',c._HOT_RUNTIME_SQL);self.assertEqual(len(c.MAINTENANCE_CAPABILITY_QUERIES),11)
 def test_actual_guard_and_binding_for_new_shared_statement(self):
  reviewed=_reviewed_statement(g.READINESS_SQL,read=True,max_rows=1)
  a=PostgresQuery(Transformer());b=PostgresQuery(Transformer());a.convert(g.READINESS_SQL,());b.convert(reviewed.as_string(),());self.assertEqual(a.params,b.params)
 def test_cadence_empty_local_slots_do_not_erase_other_cadence_checks(self):
  self.assertEqual(c._ERRORS['assert_video_collection_cadence_schema'],())
  self.assertEqual(slots(c._QUERIES['assert_video_collection_cadence_schema'])[0],[])
  for name in ('assert_typed_inventory_quota_defer_schema','assert_typed_video_collection_schema','assert_video_collection_queue_schema'):
   self.assertIn('public.video_collection_cadence',c._QUERIES[name]);self.assertTrue(c._ERRORS[name])
 def test_only_exact_two_chain_hooks_are_deferred_constraints(self):
  sql=g.READINESS_SQL
  rows=sql.split('expected_triggers(',1)[1].split(')), roles AS (',1)[0]
  rows=rows[rows.index('AS (VALUES')+10:]+')'
  parsed=re.findall(r"^\('([^']+)','([^']+)','([^']+)',([0-9]+),ARRAY\[([^\]]*)\]::text\[\],(true|false)\)",rows,re.M)
  self.assertEqual(len(parsed),36)
  # The two inventory-chain guards, and the once-per-commit user-work size check.
  expected={('public.operator_job_inventory_chains','inventory_chain_children_consistency_guard',29),
            ('public.operator_jobs','inventory_chain_parent_consistency_guard',21),
            ('operator_private.user_work_bytes_pending','user_work_bytes_commit',5)}
  deferred={(table,name,int(bits)) for table,name,signature,bits,columns,flag in parsed if flag=='true'}
  self.assertEqual(deferred,expected)
  self.assertEqual(len({(table,name) for table,name,*_ in parsed}),36)
  for table,name,signature,bits,columns,flag in parsed:
   if flag=='true':
    self.assertEqual(signature,'operator_private.check_user_work_bytes()' if name=='user_work_bytes_commit' else 'public.guard_inventory_chain_consistency()')
    self.assertEqual(columns,'')
  for expression in ("constraint_hook.contype IS DISTINCT FROM 't'",
      'constraint_hook.conrelid IS DISTINCT FROM t.tgrelid',
      'constraint_hook.conname IS DISTINCT FROM t.tgname',
      'constraint_hook.condeferrable IS DISTINCT FROM true',
      'constraint_hook.condeferred IS DISTINCT FROM true',
      't.tgconstraint=0 OR NOT t.tgdeferrable OR NOT t.tginitdeferred',
      'ELSE (t.tgconstraint<>0 OR t.tgdeferrable OR t.tginitdeferred) END'):
   self.assertIn(expression,sql)

if __name__=='__main__':unittest.main()
