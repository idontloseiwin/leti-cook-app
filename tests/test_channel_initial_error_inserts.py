"""Offline initial-error create/default and restore no-effect owner contracts."""
from collections import deque,UserDict
from contextlib import contextmanager
from pathlib import Path
import hashlib,re,sys,unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.channel import channel_initial_error_inserts as writer
from db.user_work import user_work_restore_bind as restore
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement


def render(value):return value.as_string() if hasattr(value,'as_string') else value

class Transaction:
 def __init__(self,*answers):self.answers=deque(answers);self.calls=[];self.events=[]
 @contextmanager
 def borrow(self):
  self.events.append('borrow')
  try:yield self
  except BaseException:self.events.append('poison');raise
 def answer(self,kind,sql,parameters):
  self.calls.append((kind,render(sql),parameters));value=self.answers.popleft()
  if isinstance(value,BaseException):raise value
  return value
 def fetch_one(self,s,p=()):return self.answer('one',s,p)
 def returning_one(self,s,p=()):return self.answer('returning',s,p)
 def execute(self,s,p=()):return self.answer('execute',s,p)

class InsertTests(unittest.TestCase):
 def test_native_columns_defaults_nullability_match_schema(self):
  body=re.search(r'CREATE TABLE public.channels \((.*?)\n\);',(ROOT/'schema/schema.sql').read_text(),re.S).group(1)
  lines=[line.strip().rstrip(',') for line in body.splitlines() if line.strip() and not line.strip().startswith('CONSTRAINT')]
  self.assertEqual([line.split()[0] for line in lines],[name for name,_ in writer.COLUMN_TYPES]);self.assertEqual(len(lines),39)
  self.assertEqual({line.split()[0] for line in lines if 'NOT NULL' in line},writer._NOT_NULL)
  actual={line.split()[0]:line.split(' DEFAULT ',1)[1].replace(' NOT NULL','') for line in lines if ' DEFAULT ' in line}
  self.assertEqual(actual,writer._DEFAULTS)
 def test_live_ordinal_is_the_only_production_guard_change(self):
  self.assertIn('row_number() OVER (ORDER BY a.attnum) AS position',writer._SCHEMA_PIN)
  self.assertIn('d.adnum=a.attnum',writer._SCHEMA_PIN)
  self.assertIn('a.attnum>0 AND NOT a.attisdropped',writer._SCHEMA_PIN)
  source=(ROOT/'application/db/channel/channel_initial_error_inserts.py').read_bytes()
  # C08 moved db modules into domain packages; undo only that import-path change.
  source=re.sub(rb'from \.\.\w+\.',b'from .',source)
  inverse=source.replace(b'row_number() OVER (ORDER BY a.attnum) AS position',b'a.attnum').replace(b'actual a ON a.position=e.position',b'actual a ON a.attnum=e.position')
  self.assertEqual(hashlib.sha256(inverse).hexdigest(),'435fa7d2e37c8ccfafc7121ec14f3f06108a869679e347a75eb2a01cd52a40a9')
 def test_omitted_uuid_captured_once_and_explicit_null_is_not_defaulted(self):
  omitted=render(writer._proposal_statement(('channel_id',)))
  explicit=render(writer._proposal_statement(('channel_id','initial_long_inventory_error_count')))
  self.assertEqual(omitted.count('gen_random_uuid()'),1);self.assertIn('AS MATERIALIZED',omitted)
  self.assertIn('input."initial_long_inventory_error_count"',explicit)
  self.assertNotIn('coalesce',omitted)
  self.assertIn('NULL::timestamptz',omitted)
 def test_mapping_to_jsonb_adapter_and_original_srf_casts_are_kept(self):
  values=UserDict(channel_id='channel',initial_long_inventory_status='completed')
  tx=Transaction({'matches':True},{'columns_text':'native','estimate_text':None},{'channel_id':'channel'})
  with patch.object(writer,'normalize_initial_error_fields',return_value='normalized'):
   self.assertEqual(writer.insert_collection_channel(tx,values),{'channel_id':'channel'})
  self.assertIsInstance(tx.calls[1][2][0],Jsonb);self.assertIs(type(tx.calls[1][2][0].obj),dict)
  self.assertEqual(tx.calls[1][2][0].obj,dict(values))
  self.assertIn('jsonb_populate_record(NULL::public.channels,%s::jsonb) input',tx.calls[1][1])
 def test_catalog_rejection_precedes_default_capture_normalization_and_insert(self):
  tx=Transaction({'matches':False})
  with patch.object(writer,'normalize_initial_error_fields',side_effect=AssertionError('effect')):
   with self.assertRaises(DatabaseError) as caught:writer.insert_collection_channel(tx,{'channel_id':'c'})
  self.assertEqual(caught.exception.code,'P0001');self.assertEqual(len(tx.calls),1)
  self.assertEqual(tx.events,['borrow','poison'])
 def test_normalize_runs_before_single_final_insert_and_preserves_receipt(self):
  tx=Transaction({'matches':True},{'columns_text':'native','estimate_text':None},{'channel_id':'native-id'})
  with patch.object(writer,'normalize_initial_error_fields',return_value='normalized') as normalize:
   self.assertEqual(writer.insert_collection_channel(tx,{'channel_id':'c'}),{'channel_id':'native-id'})
  normalize.assert_called_once_with(tx,proposed_columns_text='native',inserted=True)
  self.assertEqual([kind for kind,_,_ in tx.calls],['one','one','returning'])
  self.assertEqual(tx.calls[2][2],(None,'normalized'))
  self.assertIn('RETURNING channel_id',tx.calls[2][1])
 def test_final_insert_all_columns_once_does_not_repeat_defaults(self):
  final=render(writer._CREATE_INSERT)
  self.assertNotIn('gen_random_uuid',final);self.assertNotIn('now()',final);self.assertNotIn('clock_timestamp',final)
  columns=final.split('(',1)[1].split(')',1)[0]
  self.assertEqual(re.findall(r'"([a-z_0-9]+)"',columns),[name for name,_ in writer.COLUMN_TYPES])
  self.assertNotIn('UPDATE',final);self.assertNotIn('ON CONFLICT',final)
 def test_estimate_sql_null_and_json_null_transport_remain_distinct(self):
  for estimate in (None,'null','{"huge":'+'9'*5001+'}'):
   tx=Transaction({'matches':True},{'columns_text':'native','estimate_text':estimate},{'channel_id':'c'})
   with patch.object(writer,'normalize_initial_error_fields',return_value='normalized'):
    writer.insert_collection_channel(tx,{'channel_id':'c'})
   self.assertEqual(tx.calls[-1][2],(estimate,'normalized'))
 def test_native_cast_and_normalizer_errors_do_not_retry_or_insert(self):
  failure=DatabaseServerError('database_operation_failed',code='22003')
  tx=Transaction({'matches':True},failure)
  with self.assertRaises(DatabaseServerError):writer.insert_collection_channel(tx,{'channel_id':'c'})
  self.assertEqual(len(tx.calls),2)
  tx=Transaction({'matches':True},{'columns_text':'native','estimate_text':None})
  with patch.object(writer,'normalize_initial_error_fields',side_effect=failure):
   with self.assertRaises(DatabaseServerError):writer.insert_collection_channel(tx,{'channel_id':'c'})
  self.assertEqual(len(tx.calls),2)
 def test_malformed_catalog_and_native_shape_never_write(self):
  for answer in ({},{'matches':1},{'matches':None}):
   tx=Transaction(answer)
   with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):writer.insert_collection_channel(tx,{'channel_id':'c'})
   self.assertEqual(len(tx.calls),1)
  tx=Transaction({'matches':True},{'columns_text':{},'estimate_text':None})
  with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):writer.insert_collection_channel(tx,{'channel_id':'c'})
  self.assertEqual(len(tx.calls),2)
 def test_restore_uses_byte_exact_original_sql_and_parameter_tuple(self):
  self.assertEqual(writer._RESTORE_INSERT,restore._CHANNEL_INSERT)
  parameters=tuple(range(11));tx=Transaction(1)
  with patch.object(writer,'normalize_initial_error_fields',side_effect=AssertionError('no-effect path')):
   self.assertEqual(writer.insert_restored_channel(tx,parameters),1)
  self.assertEqual(tx.calls,[('execute',restore._CHANNEL_INSERT,parameters)])
  self.assertEqual(tx.events,['borrow'])
 def test_restore_keeps_volatile_clock_native_and_does_not_sample_control_clock(self):
  self.assertEqual(writer._RESTORE_INSERT.count('clock_timestamp()'),1)
  self.assertIn('CASE WHEN %s::boolean THEN coalesce(%s::timestamptz,clock_timestamp()) END',writer._RESTORE_INSERT)
  self.assertNotIn('channel_collection_state',writer._RESTORE_INSERT)
  self.assertNotIn('initial_long_inventory_error_count',writer._RESTORE_INSERT)
  self.assertIn("data->>'initial_long_inventory_status' NOT IN('pending','manual_required')",restore._DATA_SHAPE)
 def test_statements_pass_current_driver_and_exact_parameter_counts(self):
  vectors=[(writer._SCHEMA_PIN,writer._SCHEMA_PARAMETERS,True),
   (writer._proposal_statement(('channel_id',)),(Jsonb({'channel_id':'c'}),),True),
   (writer._CREATE_INSERT,(None,'native'),False),
   (writer._RESTORE_INSERT,tuple(None for _ in range(11)),False)]
  for statement,parameters,read in vectors:
   returning=statement is writer._CREATE_INSERT
   text=_reviewed_statement(statement,read=read or returning,max_rows=1 if read or returning else None,returning=returning)
   query=PostgresQuery(Transformer());query.convert(text,parameters)
   self.assertEqual(len(query.params),len(parameters))

if __name__=='__main__':unittest.main()
