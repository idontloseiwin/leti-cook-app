"""Final readiness map/strict catalog guard; offline only."""
from pathlib import Path
import ast,hashlib,json,re,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import capabilities as c,intrinsic_capabilities as g
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_capabilities import FactsDatabase,reviewed_chain_facts
REVIEW=ROOT/'tests/fixtures/final_readiness90'
PINS=json.loads((REVIEW/'pins.json').read_text())
for name,digest in PINS.items():
 assert hashlib.sha256((REVIEW/name).read_bytes()).hexdigest()==digest, name
RAW=json.loads((REVIEW/'F10-final-capability-topology-raw.json').read_text())
PLAN=json.loads((REVIEW/'F10-final-capability-topology-proposal.json').read_text())
READER_LOOKUPS=json.loads((REVIEW/'reader_catalog_lookup_contract.json').read_text())
RECOVERY=json.loads((ROOT/'tests/fixtures/identity_recovery_contract.json').read_text())
CADENCE=json.loads((REVIEW/'cadence_28d_contract.json').read_text())
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

class FinalReadiness(unittest.TestCase):
 def setUp(self):reviewed_chain_facts(self)
 def test_keep_slots_exact_with_explicit_identity_receipt_revision(self):
  kept=removed=narrow=0
  for name,q in RAW['queries'].items():
   actual,_=slots(c._QUERIES[name]);desired=PLAN['queries'][name]['slots'];index=0;errors=[]
   for original,rule in zip(q['slots'],desired):
    if rule['action']=='remove':removed+=1;continue
    self.assertEqual(c._ERRORS[name][index],original['error']);errors.append(original['error'])
    revision=(READER_LOOKUPS['slots'] | CADENCE['slots']).get(f"{name}:{original['index']}")
    if revision is not None:
     self.assertEqual(actual[index],revision['after'],(name,original['index']))
    if rule['action']=='keep':
     expected=(RECOVERY['receipt_schema_predicate'] if (name,original['index'])==('assert_typed_video_collection_schema',3) else original['sql'])
     if revision is not None:
      self.assertEqual(expected,revision['before']);expected=revision['after']
     if (name,original['index'])==('assert_typed_video_collection_schema',3):
      # Schema31 broadens only lifecycle receipt types. Preserve all earlier
      # nullable/nonnull/IS TRUE requirements and add explicit lifecycle facts.
      anchor="              and pg_catalog.pg_get_constraintdef(c.oid) like '%%identity_refresh%%'\n"
      self.assertEqual(expected.count(anchor),1)
      extra=''.join("              and pg_catalog.pg_get_constraintdef(c.oid) like '%%"+value+"%%'\n"
                    for value in ('stream_probe','long','short','stream'))
      expected=expected.replace(anchor,anchor+extra)
     if (name,original['index'])==('assert_typed_inventory_canonical_repair_schema',8):
      for old_column,new_column,privilege in (
       ('typed_source_published_at','typed_source_published_acquired_at','INSERT'),
       ('typed_source_published_at','typed_source_published_acquired_at','UPDATE'),
       ('typed_stream_actual_end_at','typed_source_stream_end_acquired_at','UPDATE')):
       anchor=f" ('video_collection_state','{old_column}','{privilege}'),"
       self.assertEqual(expected.count(anchor),1)
       expected=expected.replace(anchor,anchor+f"\n ('video_collection_state','{new_column}','{privilege}'),")
      # Schema28 adds only the three episode fields to both required grants.
      # The exact migration grants are independently pinned by schema validation.
      episode_columns=('availability_first_missing_at','availability_schedule_type','availability_resume_observed_at')
      for privilege,anchor_column,ending in (('INSERT','typed_collection_status',','),('UPDATE','typed_collection_checked_at','')):
       anchor=f" ('video_collection_state','{anchor_column}','{privilege}'){ending}"
       self.assertEqual(expected.count(anchor),1)
       rows=[f" ('video_collection_state','{column}','{privilege}')" for column in episode_columns]
       replacement=anchor+"\n"+",\n".join(rows)+"," if ending else anchor+",\n"+",\n".join(rows)
       expected=expected.replace(anchor,replacement)
     self.assertEqual(actual[index],expected,(name,original['index']));kept+=1
    else:narrow+=1
    index+=1
   self.assertEqual(index,len(actual));self.assertEqual(tuple(errors),c._ERRORS[name])
  self.assertEqual((kept,removed,narrow),(76,34,7))
 def test_cadence_revision_changes_only_approved_old_refresh_defaults(self):
  self.assertFalse(set(CADENCE['slots']) & set(READER_LOOKUPS['slots']))
  self.assertEqual(set(CADENCE['slots']),{'assert_typed_inventory_quota_defer_schema:4','assert_typed_video_collection_schema:4'})
  for revision in CADENCE['slots'].values():
   self.assertEqual(revision['before'].count('cadence_seconds = 1814400'),1)
   self.assertEqual(revision['after'],revision['before'].replace('cadence_seconds = 1814400','cadence_seconds = 2419200'))
  self.assertEqual(set(CADENCE['values']),{'assert_typed_video_collection_schema'})
  revision=CADENCE['values']['assert_typed_video_collection_schema']
  self.assertEqual(revision['before'].count("'old_cadence_days', 21"),1)
  self.assertEqual(revision['after'],revision['before'].replace("'old_cadence_days', 21","'old_cadence_days', 28"))
 def test_reader_catalog_revision_is_limited_to_exact_private_lookups(self):
  self.assertEqual(set(READER_LOOKUPS['slots']),{
   'assert_full_initial_schema:3','assert_inventory_switch_schema:2',
   'assert_inventory_switch_schema:3','assert_operator_jobs_ready:0',
   'assert_typed_inventory_canonical_repair_schema:4'})
  for name in ('assert_full_initial_schema','assert_inventory_switch_schema',
               'assert_operator_jobs_ready','assert_typed_inventory_canonical_repair_schema'):
   self.assertNotRegex(c._QUERIES[name],r"to_reg(?:class|procedure|namespace)\(\s*'operator_private")
  inventory=c._QUERIES['assert_inventory_switch_schema']
  self.assertIn("function.proargtypes='23 1009 1009 25 25 23 3802 16'::pg_catalog.oidvector",inventory)
  self.assertIn("namespace.nspname='operator_private' AND function.proname='claim_inventory_states'",inventory)
  self.assertEqual(set(READER_LOOKUPS['ctes']),{'assert_typed_inventory_canonical_repair_schema:observed_1'})
  for key,revision in READER_LOOKUPS['ctes'].items():
   name,ident=key.split(':');query=c._QUERIES[name]
   match=re.search(r'\b'+ident+r'\s+AS\s+(?:MATERIALIZED\s+)?\(',query,re.I);start=match.end()-1
   self.assertEqual(query[start+1:close(query,start)],revision['after'])
 def test_all_live_value_suffixes_fields_and_dependencies_exact(self):
  for name,q in RAW['queries'].items():
   _,value=slots(c._QUERIES[name]);expected=q['value_sql']
   if name in CADENCE['values']:
    self.assertEqual(expected,CADENCE['values'][name]['before']);expected=CADENCE['values'][name]['after']
   self.assertEqual(value,expected,name);self.assertEqual(tuple(q['fields']),c._FIELDS[name])
   for ident in q['value_ctes']:
    m=re.search(r'\b'+ident+r'\s+AS\s+(?:MATERIALIZED\s+)?\(',c._QUERIES[name],re.I);self.assertIsNotNone(m,(name,ident))
    start=m.end()-1;self.assertEqual(c._QUERIES[name][start+1:close(c._QUERIES[name],start)],q['ctes'][ident])
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
 def test_no_permissive_old_schema_or_unknown_processing_hooks(self):
  sql=g.READINESS_SQL
  self.assertNotIn("obj_description",sql);self.assertNotIn("schema_migrations",sql)
  self.assertIn('(SELECT count(*) FROM functions)=13',sql);self.assertIn('(SELECT count(*) FROM triggers)=25',sql)
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
 def test_guard_constants_pin_eight_functions_and16_native_registrations(self):
  self.assertEqual(len(PLAN['retained_integrity_guards']),8)
  self.assertEqual(sum(len(f['trigger_definitions']) for f in PLAN['retained_integrity_guards']),16)
  for f in PLAN['retained_integrity_guards']:
   # Frozen eight-function plan is historical; these two exact approved body revisions are additive.
   revisions={'public.guard_manual_video_type_decision()':RECOVERY['manual_guard_md5'],
              'public.guard_hot_operator_scope()':'eda8054b15f84795ee8637fbcd457d44'}
   digest=revisions.get(f['function'],f['normalized_prosrc_md5'])
   self.assertIn(f["function"],g.READINESS_SQL);self.assertIn(digest,g.READINESS_SQL)
   for d in f['trigger_definitions']:self.assertIn(d.split()[2],g.READINESS_SQL)

 def test_only_exact_two_chain_hooks_are_deferred_constraints(self):
  sql=g.READINESS_SQL
  rows=sql.split('expected_triggers(',1)[1].split(')), roles AS (',1)[0]
  rows=rows[rows.index('AS (VALUES')+10:]+')'
  parsed=re.findall(r"^\('([^']+)','([^']+)','([^']+)',([0-9]+),ARRAY\[([^\]]*)\]::text\[\],(true|false)\)",rows,re.M)
  self.assertEqual(len(parsed),25)
  expected={('public.operator_job_inventory_chains','inventory_chain_children_consistency_guard',29),
            ('public.operator_jobs','inventory_chain_parent_consistency_guard',21)}
  deferred={(table,name,int(bits)) for table,name,signature,bits,columns,flag in parsed if flag=='true'}
  self.assertEqual(deferred,expected)
  self.assertEqual(len({(table,name) for table,name,*_ in parsed}),25)
  for table,name,signature,bits,columns,flag in parsed:
   if flag=='true':
    self.assertEqual(signature,'public.guard_inventory_chain_consistency()');self.assertEqual(columns,'')
  for expression in ("constraint_hook.contype IS DISTINCT FROM 't'",
      'constraint_hook.conrelid IS DISTINCT FROM t.tgrelid',
      'constraint_hook.conname IS DISTINCT FROM t.tgname',
      'constraint_hook.condeferrable IS DISTINCT FROM true',
      'constraint_hook.condeferred IS DISTINCT FROM true',
      't.tgconstraint=0 OR NOT t.tgdeferrable OR NOT t.tginitdeferred',
      'ELSE (t.tgconstraint<>0 OR t.tgdeferrable OR t.tginitdeferred) END'):
   self.assertIn(expression,sql)


if __name__=='__main__':unittest.main()
