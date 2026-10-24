"""Assignment parse preflight is finite, nonexecuting, bounded and fail closed."""
from pathlib import Path
import sys,unittest
from unittest.mock import Mock,patch
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.base import operational_assignment_validation as native
from db.base.postgres import Transaction,DatabaseError,DatabaseServerError,_reviewed_statement
from test_direct_pg_transactions import Connection,Result,ServerFailure
from db.runs import sync_run_insert_normalization as inserts,sync_run_write_normalization as updates
from db.quota import quota_write_normalization as quota
from test_operator_job_lifecycle import Database

class AssignmentValidationTests(unittest.TestCase):
 def setUp(self):
  self.connection=Connection([]);self.gate=Mock();self.tx=Transaction(self.connection,self.gate,total_timeout_ms=8000)
 def result(self,rows=None):
  self.connection.results.append(Result(rows=tuple(rows if rows is not None else [{'QUERY PLAN':[{'Plan':{'Node Type':'ModifyTable'}}]}])))
 def test_five_templates_keep_original_assignments_without_destination_casts(self):
  for family in sorted(native.FAMILIES):
   with self.subTest(family=family):
    query,params=native.assignment_plan(family,{'errors' if family.startswith('sync') else 'quota_units':True},7 if family in {'sync_update','quota_finish'} else None)
    query=query.as_string();self.assertTrue(query.startswith('EXPLAIN (FORMAT JSON, ANALYZE FALSE, VERBOSE FALSE, COSTS FALSE) '))
    self.assertNotIn('%s::integer',query);self.assertNotIn('WHERE false',query);self.assertIs(params[0],True)
    self.assertEqual(' RETURNING id' in query,family=='sync_start')
    if family=='sync_update':self.assertTrue(query.endswith('WHERE id=%s'))
    if family=='quota_finish':self.assertTrue(query.endswith("WHERE request_attempt_id=%s::uuid AND status='started'"))
 def test_bound_values_json_null_and_original_order_are_preserved(self):
  values={'status':'secret-value','summary_json':{'x':1},'errors':2}
  query,params=native.assignment_plan('sync_update',values,'private-id')
  self.assertNotIn('secret-value',query.as_string());self.assertIsInstance(params[1],Jsonb)
  self.assertEqual(params[1].obj,{'x':1});self.assertEqual(params[::2],('secret-value',2))
  self.assertEqual(params[-1],'private-id');self.assertIsNone(native.assignment_plan('sync_start',{'summary_json':None},None)[1][0])
 def test_unknown_column_is_sent_to_native_parse_but_identifiers_are_quoted(self):
  query,_=native.assignment_plan('sync_start',{'unknown_field':1},None);self.assertIn('"unknown_field"',query.as_string())
  for family,values,identity in [('other',{'errors':1},None),('sync_start',{'errors':1},9),('sync_update',{},9),('sync_start',{'bad-name':1},None)]:
   with self.assertRaises(ValueError):native.assignment_plan(family,values,identity)
 def test_no_generic_explain_or_sql_template_input(self):
  with self.assertRaises(DatabaseError):_reviewed_statement('EXPLAIN SELECT 1',read=True,max_rows=1)
  with self.assertRaises(ValueError):native.assignment_plan('SELECT 1',{'errors':1},None)
 def test_discards_one_plan_and_preserves_admission_deadline(self):
  self.result();self.tx._check_budget=Mock()
  self.assertIsNone(self.tx.validate_operational_assignments('sync_start',{'errors':1}))
  self.assertEqual(self.gate.call_args_list,[unittest.mock.call(None),unittest.mock.call(None)])
  self.tx._check_budget.assert_called_once_with(configure=True);self.assertEqual(self.connection.fetch_sizes,[2])
  self.assertEqual(len(self.connection.queries),1);self.assertFalse(self.tx._failed)
 def test_readonly_rpc_and_unbounded_transactions_reject_before_dispatch(self):
  for options in ({'read_only':True,'total_timeout_ms':8000},{'rpc_name':'persist_youtube_api_request_events','total_timeout_ms':8000},{}):
   tx=Transaction(self.connection,self.gate,**options)
   with self.assertRaises(DatabaseError):tx.validate_operational_assignments('sync_start',{'errors':1})
   self.assertTrue(tx._failed)
  self.assertEqual(self.connection.queries,[]);self.gate.assert_not_called()
 def test_native_error_keeps_sqlstate_sanitizes_detail_and_poisons(self):
  self.connection.results.append(Result(execute_error=ServerFailure('42804')))
  with self.assertRaises(DatabaseServerError) as caught:self.tx.validate_operational_assignments('sync_update',{'errors':True},identity=999)
  self.assertEqual(caught.exception.code,'42804');self.assertNotIn('private',str(caught.exception));self.assertTrue(self.tx._failed)
  with self.assertRaises(DatabaseError):self.tx.validate_operational_assignments('sync_start',{'errors':1})
  self.assertEqual(len(self.connection.queries),1)
 def test_malformed_or_unbounded_plan_result_fails_closed(self):
  for rows in ([],[{}],[{'QUERY PLAN':[]}],[{'QUERY PLAN':{}}],[{'QUERY PLAN':[{}],'extra':1}],[{'QUERY PLAN':[{}]}]*2):
   self.setUp();self.result(rows)
   with self.assertRaises(DatabaseError):self.tx.validate_operational_assignments('quota_start',{'quota_units':1})
   self.assertTrue(self.tx._failed)
 def test_expired_or_wrong_owner_stops_before_dispatch(self):
  self.tx._deadline=0
  with self.assertRaises(DatabaseServerError):self.tx.validate_operational_assignments('sync_start',{'errors':1})
  self.assertEqual(self.connection.queries,[])
  self.setUp();self.tx._active=False
  with self.assertRaises(DatabaseError):self.tx.validate_operational_assignments('sync_start',{'errors':1})
  self.assertEqual(self.connection.queries,[])
 def test_input_copy_preserves_caller_mapping(self):
  values={'errors':1};self.result();self.tx.validate_operational_assignments('sync_update',values,identity=8)
  self.assertEqual(values,{'errors':1})
 def test_all_five_owners_stop_before_capture_defaults_locks_or_writes(self):
  actions=[('sync_start',None,lambda tx:inserts.start_sync_run(tx,{'errors':True})),
   ('sync_finish_insert',None,lambda tx:inserts.finish_insert_sync_run(tx,{'errors':True})),
   ('sync_update',9,lambda tx:updates.update_sync_run(tx,9,{'errors':True})),
   ('quota_start',None,lambda tx:quota.start_quota_attempt(tx,{'quota_units':True})),
   ('quota_finish','id',lambda tx:quota.finish_quota_attempt(tx,'id',{'duration_ms':True}))]
  for family,identity,invoke in actions:
   with self.subTest(family=family):
    tx=Database();tx.validate_operational_assignments=Mock(side_effect=DatabaseServerError('database_operation_failed',code='42804'))
    with self.assertRaises(DatabaseServerError):invoke(tx)
    self.assertEqual(tx.calls,[]);self.assertEqual(tx.events,['borrow','poison'])
    self.assertEqual(tx.validate_operational_assignments.call_args.args[0],family)
    self.assertEqual(tx.validate_operational_assignments.call_args.kwargs.get('identity'),identity)

if __name__=='__main__':unittest.main()
