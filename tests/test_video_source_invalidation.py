"""Focused prepared queue-owned source invalidation protocol; no database."""
from pathlib import Path
from contextlib import contextmanager
from collections import deque
import ast,hashlib,inspect,json,re,sys,unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.videos import video_source_invalidation as effect
from db.videos import video_counter_completion as counter,video_metadata_completion as metadata
from db.base.postgres import DatabaseError,_reviewed_statement

class Tx:
 def __init__(self,*answers):self.answers=deque(answers);self.events=[]
 @contextmanager
 def borrow(self):
  self.events.append('borrow')
  try:yield self
  except BaseException:self.events.append('poison');raise
  finally:self.events.append('exit')
 def fetch_one(self,sql,params=()):
  self.events.append((sql,params));value=self.answers.popleft()
  if isinstance(value,BaseException):raise value
  return value
 def execute(self,sql,params=()):return self.fetch_one(sql,params)

REVISIONS_PATH=ROOT/'tests/fixtures/source_invalidation_schema28.json'
REVISIONS_BYTES=REVISIONS_PATH.read_bytes()
assert hashlib.sha256(REVISIONS_BYTES).hexdigest()=='6c32ee378789c10c58fca5a5738df2352d6794ff4b5dfd264710fe7ddad8e6a1'
REVISIONS=json.loads(REVISIONS_BYTES)['modules']

class EffectTests(unittest.TestCase):
 def run_effect(self,tx,ids=('a',)):
  with effect.source_clock_invalidation(tx,ids):tx.events.append('source')
 def test_snapshot_remains_native_and_source_is_between_capture_and_facts(self):
  opaque='[{"checked_at":"0001-01-01T00:00:00+00:00 BC"}]'
  tx=Tx(dict(count=1,snapshot_text=opaque),dict(present=1,required=False));self.run_effect(tx)
  self.assertEqual(tx.events,['borrow',(effect._CAPTURE,(['a'],)),'source',(effect._FACTS,(opaque,)),'exit'])
 def test_invalidation_single_write_accepts_only_zero_or_one_receipt(self):
  for count in (0,1):
   tx=Tx(dict(count=1,snapshot_text='native'),dict(present=1,required=True),count);self.run_effect(tx)
   self.assertEqual(tx.events[-2],(effect._INVALIDATE,()))
   self.assertEqual(sum(e==(effect._INVALIDATE,()) for e in tx.events),1)
 def test_empty_replay_yields_without_any_database_query(self):
  tx=Tx();self.run_effect(tx,[]);self.assertEqual(tx.events,['borrow','source','exit'])
 def test_bound_and_unique_text_identity_precede_borrow(self):
  for ids in (None,'a',{},[1],['a','a'],list(map(str,range(1001)))):
   tx=Tx()
   with self.assertRaises(ValueError):self.run_effect(tx,ids)
   self.assertFalse(tx.events)
 def test_thousand_ids_use_constant_two_read_statements(self):
  ids=list(map(str,range(1000)));tx=Tx(dict(count=1000,snapshot_text='native'),dict(present=1000,required=False));self.run_effect(tx,ids)
  self.assertEqual(len([e for e in tx.events if isinstance(e,tuple)]),2)
 def test_malformed_capture_prevents_source(self):
  for row in (None,{},dict(count=True,snapshot_text='x'),dict(count=0,snapshot_text='x'),dict(count=1,snapshot_text={}),dict(count=1,snapshot_text='x',extra=1)):
   tx=Tx(row)
   with self.assertRaises(DatabaseError):self.run_effect(tx)
   self.assertNotIn('source',tx.events);self.assertEqual(tx.events[-2: ],['poison','exit'])
 def test_source_error_never_runs_facts_or_write_and_poisons(self):
  tx=Tx(dict(count=1,snapshot_text='x'));error=RuntimeError('original_count_error')
  with self.assertRaises(RuntimeError) as caught:
   with effect.source_clock_invalidation(tx,['a']):raise error
  self.assertIs(caught.exception,error);self.assertEqual(tx.events[-2:],['poison','exit']);self.assertFalse(tx.answers)
 def test_malformed_post_state_poison_prevents_partial_success(self):
  for row in (None,{},dict(present=True,required=False),dict(present=0,required=False),dict(present=1,required=1),dict(present=1,required=False,extra=1)):
   tx=Tx(dict(count=1,snapshot_text='x'),row)
   with self.assertRaises(DatabaseError):self.run_effect(tx)
   self.assertEqual(tx.events[-2:],['poison','exit'])
 def test_malformed_write_receipt_poisons_without_retry(self):
  for count in (True,None,-1,2):
   tx=Tx(dict(count=1,snapshot_text='x'),dict(present=1,required=True),count)
   with self.assertRaises(DatabaseError):self.run_effect(tx)
   self.assertFalse(tx.answers);self.assertEqual(tx.events[-2:],['poison','exit'])
 def test_native_failure_preserved_and_no_retry(self):
  for index in (0,1,2):
   error=DatabaseError('test_native_failure');answers=[dict(count=1,snapshot_text='x'),dict(present=1,required=True),1];answers[index]=error;tx=Tx(*answers[:index+1])
   with self.assertRaises(DatabaseError) as caught:self.run_effect(tx)
   self.assertIs(caught.exception,error);self.assertFalse(tx.answers);self.assertEqual(tx.events[-2:],['poison','exit'])
 def test_exact_native_predicate_and_closed_statements(self):
  for sql,read,params in ((effect._CAPTURE,True,(['a'],)),(effect._FACTS,True,('[]',)),(effect._INVALIDATE,False,())):
   PostgresQuery(Transformer()).convert(sql,params);_reviewed_statement(sql,read=read,max_rows=1)
  self.assertIn('previous.source_existed AND',effect._FACTS)
  self.assertEqual(effect._FACTS.count('IS DISTINCT FROM'),2)
  self.assertIn('state.lease_token IS NOT NULL AND state.lease_expires_at>now()',effect._FACTS)
  self.assertNotIn('clock_timestamp',inspect.getsource(effect));self.assertNotIn('json.loads',inspect.getsource(effect))
  self.assertIn('WHERE singleton=true AND reconciliation_required=false',effect._INVALIDATE)
 def test_two_completion_deltas_preserve_original_bulk_sql_and_call_order(self):
  pins={'video_counter_completion.py': '6e437e6a9a78bb2da3051e6851fafa7cb7018130f967ca01919ace1de4363bc5', 'video_metadata_completion.py': '9bebaf11de16f412570d6b9c294a060996be6cddf1b7bf7c9474a69361e962a4'}
  for module,name,statement,rows in ((counter,'complete_video_counter_collection','_UPDATE_SOURCE','pending.obj'),(metadata,'complete_video_metadata_collection','_UPSERT',"owned['rows']")):
   new=Path(module.__file__).read_text(encoding='utf-8')
   # Reverse only the accepted schema28 all-type cadence/episode delta. Exact
   # blocks are immutable fixtures; the original full-module pins stay below.
   for revision in REVISIONS[Path(module.__file__).name]:
    self.assertEqual(new.count(revision['after']),1)
    new=new.replace(revision['after'],revision['before'])
   # C08 moved db modules into domain packages; undo only that import-path change.
   new=re.sub(r'from \.\.\w+\.','from .',new)
   import_line='from .video_source_invalidation import source_clock_invalidation\n'
   self.assertEqual(new.count(import_line),1);new=new.replace(import_line,'')
   original="        if tx.execute("+statement+",(pending,))!=owned['count']:_reject()\n"
   wrapped="        with source_clock_invalidation(tx,[row['video_id'] for row in "+rows+"]):\n    "+original
   self.assertEqual(new.count(wrapped),1);new=new.replace(wrapped,original)
   # The only independent accepted change is the metadata-only registry rename.
   self.assertEqual(new.count('from .operation_contracts import get_operation'),1)
   self.assertEqual(new.count("get_operation('"),1)
   new=new.replace('from .operation_contracts import get_operation','from .function_contracts import get_function').replace("get_operation('","get_function('")
   # Normalize only the reviewed snapshot transport delta, not source DML.
   imported='from .collection_policy import cadence_parameter, _SQL as _CADENCE_SQL,_decode_cadence_rows'
   self.assertEqual(new.count(imported),1)
   new=new.replace(imported,imported.replace('cadence_parameter, ',''))
   bound='(_POLICY_INPUT,(cadence_parameter(tx),pending))'
   self.assertEqual(new.count(bound),1)
   new=new.replace(bound,'(_POLICY_INPUT,(pending,))')
   # R01 moved source retention from 28 to 29 days; test_retention_window pins it.
   # F6 validates independent General and Stats observations; no new window.
   self.assertEqual(new.count("interval '696 hours'"),2 if module is metadata else 1)
   new=new.replace("interval '696 hours'","interval '672 hours'")
   # P3 replaces only the 21-day availability ceiling with the shared 28-day
   # bound. Reverse these three exact deltas; retain the historical module pins.
   self.assertEqual(module.MAX_INTERVAL_SECONDS,2419200)
   for current,historical in (
    ('from collection_policy import MAX_INTERVAL_SECONDS,availability_class,select_long_policy_at_age,PolicyError',
     'from collection_policy import availability_class,select_long_policy_at_age,PolicyError'),
    ('input.availability_checked_at+make_interval(secs=>%s::integer)',
     "input.availability_checked_at+interval '504 hours'"),
    ('tx.execute(_COMPLETE,(Jsonb(schedules),MAX_INTERVAL_SECONDS))',
     'tx.execute(_COMPLETE,(Jsonb(schedules),))'),
   ):
    self.assertEqual(new.count(current),1)
    new=new.replace(current,historical)
   # Counter still restores the exact pre-68 module. Metadata additionally pins
   # F6's reviewed two-clock SQL, cadence anchors and non-regression fences;
   # test_video_source_clocks guards those semantics. No other inverse changes.
   self.assertEqual(hashlib.sha256(new.encode()).hexdigest(),pins[Path(module.__file__).name])
   source=inspect.getsource(getattr(module,name));self.assertLess(source.index('_LOCK'),source.index('source_clock_invalidation'));self.assertLess(source.index('source_clock_invalidation'),source.index('_COMPLETE'))

if __name__=='__main__':unittest.main()
