"""Focused unwired INSERT default/cast/receipt guards; native parity is separate."""
import hashlib,json,re,sys,unittest
from pathlib import Path
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.runs import sync_run_insert_normalization as writer
from db.base.postgres import DatabaseError,DatabaseServerError,DatabaseUnavailable,_reviewed_statement
from test_operator_job_lifecycle import Database as BaseDatabase

class Database(BaseDatabase):
 def validate_operational_assignments(self,family,values,*,identity=None):
  self.assignment_validation=(family,dict(values),identity)


GOOD={'matches':True}
def row(summary='{}',**columns):return {'summary_text':summary,'columns_text':json.dumps({'id':41,**columns})}
def text(statement):return statement.as_string() if hasattr(statement,'as_string') else statement

class InsertNormalizationTests(unittest.TestCase):
 def test_defaults_and_physical_order_match_authoritative_table(self):
  source=(ROOT/'schema/schema.sql').read_text()
  definition=re.search(r'CREATE TABLE public.sync_runs \((.*?)\n\);',source,re.S)[1]
  columns=re.findall(r'^    (\w+) ',definition,re.M)
  self.assertEqual(columns,[name for name,_ in writer.COLUMN_TYPES])
  self.assertEqual(set(writer._DEFAULTS),set(columns))
  self.assertIn("ALTER TABLE ONLY public.sync_runs ALTER COLUMN id SET DEFAULT nextval('public.sync_runs_id_seq'::regclass);",source)
  for name,kind in writer.COLUMN_TYPES:
   definition_line=next(line.strip().rstrip(',') for line in definition.splitlines() if line.lstrip().startswith(name+' '))
   if name=='id':continue
   if name in ('finished_at','duration_seconds'):
    self.assertNotIn('DEFAULT',definition_line);self.assertNotIn('NOT NULL',definition_line)
   else:self.assertIn('DEFAULT '+writer._DEFAULTS[name]+' NOT NULL',definition_line)
  self.assertEqual([len(value) for value in writer._SCHEMA_PARAMETERS],[20]*5)
 def test_start_native_defaults_then_normalize_before_insert_and_return_id(self):
  tx=Database(GOOD,row('{"errors":2,"unknown":"private"}',errors=2),{'id':41})
  self.assertEqual(writer.start_sync_run(tx,{'run_type':'manual'}),41)
  self.assertEqual([mode for mode,_,_ in tx.calls],['one','one','returning'])
  self.assertEqual(tx.calls[0][1],writer._SCHEMA_PIN)
  proposal=text(tx.calls[1][1]);self.assertEqual(proposal.count('nextval('),1);self.assertEqual(proposal.count('now()'),2)
  self.assertIn('proposed AS MATERIALIZED',proposal);self.assertIn('ROW(nextval(',proposal)
  final=text(tx.calls[2][1]);self.assertNotIn('nextval(',final);self.assertNotIn('now()',final);self.assertTrue(final.endswith(' RETURNING id'))
  self.assertEqual(json.loads(tx.calls[2][2][0]),{'summary_schema_version':3,'column_fields':1})
  self.assertEqual(tx.calls[2][2][1],row('{"errors":2,"unknown":"private"}',errors=2)['columns_text'])
  self.assertEqual((tx.events,tx.operations),(['borrow'],[]))
 def test_finish_insert_has_no_returning_and_retains_rowcount(self):
  tx=Database(GOOD,row(),1)
  self.assertEqual(writer.finish_insert_sync_run(tx,{'status':'error'}),1)
  self.assertEqual(tx.calls[-1][0],'execute');self.assertNotIn('RETURNING',text(tx.calls[-1][1]))
 def test_explicit_null_and_supplied_identity_do_not_use_omission_defaults(self):
  values={'summary_json':None,'id':None,'started_at':None,'created_at':None}
  tx=Database(GOOD,row(None),DatabaseServerError('database_operation_failed',code='23502'))
  with self.assertRaises(DatabaseServerError):writer.start_sync_run(tx,values)
  proposal=text(tx.calls[1][1]);self.assertNotIn('nextval(',proposal);self.assertNotIn('now()',proposal)
  self.assertIn('supplied."id"',proposal);self.assertEqual(tx.calls[1][2],(None,None,None,None))
  self.assertEqual(json.loads(tx.calls[2][2][0]),{'summary_schema_version':3,'column_fields':0})
  self.assertEqual(tx.events,['borrow','poison'])
 def test_input_parameter_order_json_adapter_and_casts_remain_explicit(self):
  values={'errors':1.5,'summary_json':{'errors':1},'status':'running','id':73}
  tx=Database(GOOD,row('{"errors":1}',errors=2),{'id':73})
  writer.start_sync_run(tx,values)
  params=tx.calls[1][2];self.assertEqual(params[0],1.5);self.assertIsInstance(params[1],Jsonb);self.assertEqual(params[1].obj,{'errors':1});self.assertEqual(params[2:],('running',73))
  query=text(tx.calls[1][1]);self.assertIn('%s::integer AS "errors",%s::jsonb AS "summary_json",%s::text AS "status",%s::bigint AS "id"',query)
  self.assertNotIn('jsonb_populate_record',query)
 def test_original_insert_envelope_rejects_before_borrow_or_defaults(self):
  for values,message in (({},'database_insert_row_invalid'),({f'f{i}':i for i in range(257)},'database_insert_row_invalid'),({'bad-name':1},'database_identifier_invalid')):
   tx=Database()
   with self.assertRaisesRegex(ValueError,message):writer.start_sync_run(tx,values)
   self.assertEqual((tx.calls,tx.events),([],[]))
 def test_catalog_pin_is_bounded_metadata_and_fails_before_sequence(self):
  for contract in ({'matches':False},{},{'matches':1},None,{'matches':True,'extra':True}):
   tx=Database(contract)
   with self.assertRaises(DatabaseError):writer.start_sync_run(tx,{'errors':1})
   self.assertEqual(len(tx.calls),1);self.assertEqual(tx.events,['borrow','poison'])
  self.assertIn('FROM public.sync_runs WHERE false',writer._SCHEMA_PIN)
  for token in ('a.attidentity','a.attgenerated','pg_catalog.pg_get_expr','(SELECT count(*) FROM actual)=20','relrowsecurity'):
   self.assertIn(token,writer._SCHEMA_PIN)
 def test_native_unknown_column_parse_and_error_have_no_retry(self):
  error=DatabaseServerError('database_operation_failed',code='42703');tx=Database(GOOD,error)
  with self.assertRaises(DatabaseServerError) as caught:writer.start_sync_run(tx,{'unknown_field':1})
  self.assertIs(caught.exception,error);self.assertIn('run."unknown_field"',text(tx.calls[1][1]));self.assertEqual(len(tx.calls),2)
 def test_malformed_proposal_and_json_null_stop_before_insert(self):
  for proposed in (None,{'summary_text':'{}'},row('null')):
   tx=Database(GOOD,proposed)
   with self.assertRaises(DatabaseError):writer.start_sync_run(tx,{'errors':1})
   self.assertEqual(len(tx.calls),2);self.assertEqual(tx.events,['borrow','poison'])
 def test_huge_native_tokens_pass_untouched_to_final_projection(self):
  huge='8'*5001;tx=Database(GOOD,{'summary_text':'{"requested":'+huge+',"unknown":"private"}','columns_text':'{"id":41}'},{'id':41})
  writer.start_sync_run(tx,{'status':'running'})
  self.assertIn(huge,tx.calls[-1][2][0]);self.assertNotIn('private',tx.calls[-1][2][0])
 def test_timestamps_borrow_only_supplied_transaction(self):
  tx=Database(GOOD,row(),{'id':41})
  with patch.object(writer,'_final_summary',return_value='{}') as normalize:writer.start_sync_run(tx,{'errors':1})
  normalize.assert_called_once_with(tx,row());self.assertEqual(tx.operations,[])
 def test_start_retains_no_row_and_strict_id_shape(self):
  for result in (None,{'id':None}):
   self.assertIsNone(writer.start_sync_run(Database(GOOD,row(),result),{'errors':1}))
  for result in ({},{'id':True},{'id':'41'},{'id':41,'other':True}):
   tx=Database(GOOD,row(),result)
   with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):writer.start_sync_run(tx,{'errors':1})
 def test_final_native_error_and_unknown_ack_propagate_without_retry(self):
  for error in (DatabaseServerError('database_operation_failed',code='23505'),DatabaseUnavailable('database_connection_failed',code='08006',outcome_unknown=True)):
   tx=Database(GOOD,row(),error)
   with self.assertRaises(type(error)) as caught:writer.start_sync_run(tx,{'errors':1})
   self.assertIs(caught.exception,error);self.assertEqual(len(tx.calls),3);self.assertEqual(tx.operations,[])
 def test_all_statements_use_existing_driver_guard_and_native_binding(self):
  values={'errors':2,'summary_json':None}
  for query,params,read,returning in ((writer._SCHEMA_PIN,writer._SCHEMA_PARAMETERS,True,False),(writer._proposal_statement(values),(2,None),True,False),(writer._START_INSERT,('{}','{}'),True,True),(writer._FINISH_INSERT,('{}','{}'),False,False)):
   _reviewed_statement(query,read=read,max_rows=1 if read else 0,returning=returning)
   PostgresQuery(Transformer()).convert(query,params)

if __name__=='__main__':unittest.main()
