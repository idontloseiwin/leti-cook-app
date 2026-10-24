"""Focused offline native estimate-effect protocol; native66 remains separate."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
import inspect,re,sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.channel import channel_inventory_estimate_normalization as effect
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement

STAMP='2026-10-06T12:00:00+00:00'
OPAQUE='{"huge":'+('9'*5001)+',"scaled":1.000}'
STATE=dict(counts=OPAQUE,statuses='null',clocks='["long"]')

def head(kinds=None):
 return [dict(invalid=False),dict(invalid=False,kinds=['long']),
  dict(kinds=kinds or ['long'],**STATE,common_input=None),*[dict(STATE) for _ in (kinds or ['long'])]]
def count(**changes):return dict(dict(invalid=False,overflow=False,count='null',status='error'),**changes)
def diagnosed(clock=None,error=None,raw=None):return dict(clock=clock,error=error,raw=raw)

class Transaction:
 def __init__(self,*answers):self.answers=deque(answers);self.calls=[];self.events=[]
 @contextmanager
 def borrow(self):
  self.events.append('borrow')
  try:yield self
  except BaseException:self.events.append('poison');raise
 def fetch_one(self,statement,parameters=()):
  self.calls.append((statement,parameters));value=self.answers.popleft()
  if isinstance(value,BaseException):raise value
  return value

class EffectTests(unittest.TestCase):
 def call(self,tx,**changes):
  args=dict(incoming_text=OPAQUE,inserted=True);args.update(changes)
  return effect.normalize_inventory_estimate(tx,**args)
 def test_sql_null_and_untargeted_paths_dispatch_nothing(self):
  for args in (dict(incoming_text=None,inserted=True),dict(incoming_text='null',inserted=False,targeted=False)):
   tx=Transaction();self.assertEqual(self.call(tx,**args),args['incoming_text']);self.assertEqual(tx.calls,[]);self.assertEqual(tx.events,[])
 def test_unchanged_invalid_jsonb_escapes_before_shape_validation(self):
  tx=Transaction(dict(same=True));self.assertEqual(self.call(tx,incoming_text='null',previous_text='null',inserted=False,targeted=True),'null')
  self.assertEqual(tx.calls,[(effect._EQUAL,('null','null'))]);self.assertEqual(tx.events,['borrow'])
 def test_shape_and_type_rejection_precede_any_clock_cast(self):
  for values in ([dict(invalid=True)],[dict(invalid=False),dict(invalid=True,kinds=None)]):
   tx=Transaction(*values)
   with self.assertRaisesRegex(DatabaseServerError,'channel_inventory_estimate_invalid'):self.call(tx)
   self.assertEqual(len(tx.calls),len(values));self.assertEqual(tx.events,['borrow','poison'])
 def test_error_null_clock_skips_window_but_preserves_native_accumulators(self):
  tx=Transaction(*head(),count(),dict(clock=None),diagnosed(),dict(keep=False,conflict=None),dict(STATE),diagnosed(),dict(value=OPAQUE))
  self.assertEqual(self.call(tx),OPAQUE)
  self.assertNotIn(effect._SOURCE_CLOCK,[s for s,p in tx.calls]);self.assertNotIn(effect._OLD_FINITE,[s for s,p in tx.calls])
  self.assertEqual(tx.calls[-1][1],(['long'],OPAQUE,'null','["long"]',None));self.assertFalse(tx.answers)
 def test_count_shape_and_range_error_win_before_invalid_timestamp(self):
  for invalid,overflow in ((True,False),(False,True)):
   tx=Transaction(*head(),count(invalid=invalid,overflow=overflow))
   with self.assertRaisesRegex(DatabaseServerError,'channel_inventory_estimate_invalid'):self.call(tx)
   self.assertEqual(tx.calls[-1][0],effect._COUNT)
 def test_incoming_timestamp_native_error_is_not_caught(self):
  error=DatabaseServerError('database_operation_failed',code='22008');tx=Transaction(*head(),count(),error)
  with self.assertRaises(DatabaseServerError) as caught:self.call(tx)
  self.assertIs(caught.exception,error);self.assertEqual(tx.calls[-1][0],effect._INPUT_CLOCK);self.assertEqual(tx.events[-1],'poison')
 def test_nonerror_null_clock_still_runs_fresh_window_validation(self):
  tx=Transaction(*head(),count(status='playlist_not_found'),dict(clock=None),dict(invalid=True))
  with self.assertRaises(DatabaseServerError) as caught:self.call(tx)
  self.assertEqual((caught.exception.code,caught.exception.message),('22023','hot_source_clock_out_of_window'))
  self.assertEqual(tx.calls[-1],(effect._SOURCE_CLOCK,(None,)))
 def test_old_clock_swallows_only_retained_two_states(self):
  for code in ('22007','22008'):
   tx=Transaction(diagnosed(error=code,raw='bad'));self.assertIsNone(effect._old_clock(tx,'native','long'));self.assertEqual(len(tx.calls),1)
  error=DatabaseServerError('native',code='22003');tx=Transaction(diagnosed(error='22003',raw='huge'),error)
  with self.assertRaises(DatabaseServerError) as caught:effect._old_clock(tx,'native','long')
  self.assertIs(caught.exception,error);self.assertEqual(tx.calls[-1],(effect._CAST_CLOCK,('huge',)))
 def test_stale_clock_keep_precedes_conflict_and_has_no_jsonb_apply(self):
  tx=Transaction(*head(),count(),dict(clock=None),diagnosed(STAMP),dict(clock=STAMP),dict(keep=True,conflict=True),
   diagnosed(STAMP),dict(unknown=False,common=STAMP),dict(value=OPAQUE))
  self.assertEqual(self.call(tx),OPAQUE);self.assertNotIn(effect._APPLY,[s for s,p in tx.calls]);self.assertEqual(tx.calls[-1][1][-1],STAMP)
 def test_equal_clock_conflict_follows_native_source_and_old_clock_facts(self):
  tx=Transaction(*head(),count(status='ok',count='4'),dict(clock=STAMP),dict(invalid=False),diagnosed(STAMP),dict(clock=STAMP),dict(keep=False,conflict=True))
  with self.assertRaisesRegex(DatabaseServerError,'channel_inventory_estimate_clock_conflict'):self.call(tx)
  self.assertEqual([s for s,p in tx.calls][-4:],[effect._SOURCE_CLOCK,effect._OLD_CLOCK,effect._OLD_FINITE,effect._DECIDE])
 def test_any_unknown_final_format_makes_common_json_null(self):
  tx=Transaction(*head(['long','short','stream']),count(),dict(clock=None),diagnosed(),dict(keep=False,conflict=None),dict(STATE),
   diagnosed(),diagnosed('infinity'),dict(unknown=True,common=None),diagnosed(STAMP),dict(unknown=False,common=STAMP),dict(value=OPAQUE))
  self.assertEqual(self.call(tx),OPAQUE);self.assertIsNone(tx.calls[-1][1][-1]);self.assertEqual(tx.calls[-1][1][0],['long','short','stream'])
 def test_bad_native_result_poisoning_never_returns_partial_document(self):
  for result in ({'invalid':None},{'invalid':False,'extra':1}):
   tx=Transaction(result)
   with self.assertRaises(DatabaseError):self.call(tx)
   self.assertEqual(tx.events,['borrow','poison'])
 def test_native_inputs_are_strict_and_jsonb_is_never_python_decoded(self):
  for args in (dict(incoming_text={}),dict(previous_text='{}'),dict(inserted=1),dict(targeted=1)):
   tx=Transaction()
   with self.assertRaises(ValueError):self.call(tx,**args)
   self.assertFalse(tx.calls)
  source=inspect.getsource(effect);self.assertNotIn('json.loads',source);self.assertNotIn('Decimal',source);self.assertNotIn('.execute(',source)
 def test_sql_binds_closed_read_statements_and_retains_jsonb_operators(self):
  statements=[v for k,v in vars(effect).items() if k.startswith('_') and isinstance(v,str) and v.lstrip().startswith(('SELECT','WITH'))]
  self.assertEqual(len(statements),15)
  for statement in statements:
   params=(None,)*len(re.findall('%s',statement));PostgresQuery(Transformer()).convert(statement,params)
   _reviewed_statement(statement,read=True,max_rows=1)
  self.assertIn('IS NOT DISTINCT FROM',effect._EQUAL);self.assertIn("count::text !~ '^[0-9]{1,10}$'",effect._COUNT)
  self.assertIn("counts->kind='0'::jsonb",effect._DECIDE);self.assertIn('counts||jsonb_build_object',effect._APPLY)
  self.assertNotIn('clock_timestamp',effect._COMMON);self.assertIn('clock_timestamp',effect._OLD_FINITE)

if __name__=='__main__':unittest.main()
