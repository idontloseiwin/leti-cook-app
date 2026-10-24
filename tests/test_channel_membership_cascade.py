"""Actual closed cursor plus finite FK drain ownership; no server."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import re,sys,unittest,psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tools')]
from db.base import postgres as pg
from db.channel import channel_membership_cascade as effect
from db.retention import deleted_channel_retention as owner
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
CHANNEL='UC0000000000000000000001'
class ServerError(psycopg.Error):
 @property
 def sqlstate(self):return '55P03'
 @property
 def diag(self):return SimpleNamespace(message_primary='private server detail')
class Cursor:
 def __init__(self,owner):self.owner=owner
 def __enter__(self):return self
 def __exit__(self,*args):self.owner.closed+=1
 def execute(self,statement,parameters):self.owner.queries.append((statement,parameters))
 def fetchmany(self,count):
  self.owner.fetch_sizes.append(count);value=self.owner.rows.popleft() if self.owner.rows else []
  if isinstance(value,BaseException):raise value
  return value
class Connection:
 def __init__(self,*rows):self.rows=deque(rows);self.queries=[];self.settings=[];self.cursor_options=[];self.fetch_sizes=[];self.closed=0;self.rollbacks=0
 def execute(self,statement,parameters=()):self.settings.append((statement,parameters))
 def cursor(self,**options):self.cursor_options.append(options);return Cursor(self)
 @contextmanager
 def transaction(self):
  try:yield
  except BaseException:self.rollbacks+=1;raise

def create(*rows,**kwargs):
 connection=Connection(*rows);gate=Mock();values=dict(rpc_name='cleanup_hot_deleted_channels',total_timeout_ms=5000,statement_timeout_ms=5000)
 values.update(kwargs);return pg.Transaction(connection,gate,**values),connection,gate
class CursorTests(unittest.TestCase):
 def test_single_snapshot_fixed50_buffers_and_maintenance_admission(self):
  tx,c,gate=create([{'video_id':1}],[{'video_id':2}],[])
  with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:self.assertEqual(list(batches),[[{'video_id':1}],[{'video_id':2}]]);self.assertEqual(list(batches),[])
  self.assertEqual(c.queries,[(effect._MEMBERS,([CHANNEL],))]);self.assertEqual(c.fetch_sizes,[50,50,50]);self.assertEqual(c.closed,1)
  self.assertEqual(c.cursor_options,[dict(name='lic_channel_membership_cascade',scrollable=False,withhold=False,row_factory=pg.dict_row)])
  self.assertTrue(all(call.args==('cleanup_hot_deleted_channels',) for call in gate.call_args_list));self.assertFalse(tx._scheduled_cursor_active)
 def test_capability_and_parameters_fail_before_cursor(self):
  for kwargs in (dict(read_only=True),dict(rpc_name=None),dict(rpc_name='cleanup_hot_inventory'),dict(total_timeout_ms=None)):
   tx,c,_=create(**kwargs)
   with self.assertRaises(pg.DatabaseError):
    with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]):pass
   self.assertEqual(c.queries,[]);self.assertEqual(c.cursor_options,[])
  for ids in (None,CHANNEL,[],[None],[CHANNEL]*101):
   tx,c,_=create()
   with self.assertRaises(pg.DatabaseError):
    with tx.channel_membership_cascade_rows(channel_ids=ids):pass
   self.assertEqual(c.queries,[])
 def test_no_arbitrary_sql_or_buffer_size(self):
  tx,c,_=create()
  for name in ('sql','callback','batch_size'):
   with self.assertRaises(TypeError):tx.channel_membership_cascade_rows(channel_ids=[CHANNEL],**{name:None})
 def test_context_escape_and_nested_cursor_fail(self):
  tx,c,_=create([])
  with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:list(batches)
  with self.assertRaises(pg.DatabaseError):next(batches)
  tx,c,_=create()
  with self.assertRaises(pg.DatabaseError):
   with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]):
    with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]):pass
  self.assertEqual(len(c.queries),1);self.assertTrue(tx._failed)
 def test_partial_iteration_poisoned_before_commit(self):
  tx,c,_=create([{}],[])
  with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_incomplete'):
   with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:next(batches)
  self.assertTrue(tx._failed);self.assertEqual(c.closed,1)
 def test_deadline_between_buffers_poisoned_and_closed(self):
  tx,c,_=create([{}],[{}])
  with self.assertRaises(pg.DatabaseServerError) as caught:
   with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:next(batches);tx._deadline=0;next(batches)
  self.assertEqual(caught.exception.code,'57014');self.assertEqual(c.fetch_sizes,[50]);self.assertEqual(c.closed,1)
 def test_admission_before_open_and_each_fetch(self):
  for fail_at in (1,2,3,4):
   tx,c,gate=create([{}]);gate.side_effect=[None]*(fail_at-1)+[RuntimeError('denied')]
   with self.assertRaises(RuntimeError):
    with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:next(batches)
   self.assertEqual(c.fetch_sizes,[]);self.assertTrue(tx._failed)
 def test_cursor_overflow_never_silently_truncates(self):
  tx,c,_=create([{}]*51)
  with self.assertRaises(pg.DatabaseError):
   with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:next(batches)
  self.assertEqual(c.closed,1);self.assertTrue(tx._failed)
 def test_no_total_cutoff(self):
  tx,c,_=create(*([[{}]*50]*20+[[{}]*13,[]]))
  with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:self.assertEqual(sum(map(len,batches)),1013)
  self.assertEqual(c.fetch_sizes,[50]*22)
 def test_native_failure_maps_and_only_explicit_savepoint_recovers(self):
  tx,c,_=create(ServerError())
  with self.assertRaises(pg.DatabaseServerError) as caught:
   with tx.savepoint():
    with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:next(batches)
  self.assertEqual(caught.exception.code,'55P03');self.assertFalse(tx._failed);self.assertEqual(c.rollbacks,1)
 def test_real_maintenance_admission_is_retained(self):
  import hot_runtime
  from hot_admission import real_admission
  with real_admission():
   tx,c,_=create([]);tx._admission=pg._runtime_admission
   with hot_runtime.maintenance_access():
    with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]) as batches:self.assertEqual(list(batches),[])
   tx,c,_=create();tx._admission=pg._runtime_admission
   with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
    with tx.channel_membership_cascade_rows(channel_ids=[CHANNEL]):pass
   self.assertEqual(c.queries,[])
class Fake:
 def __init__(self,batches,answers):self.batches=batches;self.answers=deque(answers);self.calls=[];self.failed=False
 @contextmanager
 def borrow(self):
  try:yield self
  except BaseException:self.failed=True;raise
 @contextmanager
 def channel_membership_cascade_rows(self,**params):self.calls.append(('cursor',params));yield iter(self.batches)
 def answer(self,kind,query,params):
  self.calls.append((kind,query,params));result=self.answers.popleft()
  if isinstance(result,BaseException):raise result
  return result
 def returning_all(self,query,params,**kwargs):return self.answer('returning',query,params)
 def fetch_all(self,query,params,**kwargs):return self.answer('all',query,params)
 def fetch_one(self,query,params):return self.answer('one',query,params)
 def execute(self,query,params):return self.answer('execute',query,params)
def held(video='v',kind='long'):return dict(video_id=video,channel_id=CHANNEL,video_type=kind)
class Drain(unittest.TestCase):
 def test_actual_receipts_deduplicate_only_idempotent_withdrawal(self):
  tx=Fake([[held(kind='long'),held(kind='short')]],[ [dict(video_id='v',old_text='NATIVE_A'),dict(video_id='v',old_text='NATIVE_B')], [dict(acquired=None)],1,dict(present=False)])
  self.assertIsNone(effect.drain_channel_memberships(tx,[CHANNEL]));self.assertEqual(tx.calls[3][2],(['v'],));self.assertEqual(tx.calls[4][1],effect._WITHDRAW)
  self.assertFalse(any('DELETE FROM public.channels' in str(c) for c in tx.calls));self.assertFalse(tx.failed)
  self.assertIn('evidence_expires_at=NULL',tx.calls[4][1]);self.assertNotIn('decision_type=',tx.calls[4][1])
 def test_missing_delete_receipt_aborts_before_withdrawal(self):
  tx=Fake([[held()]],[[]])
  with self.assertRaises(pg.DatabaseError):effect.drain_channel_memberships(tx,[CHANNEL])
  self.assertEqual(len(tx.calls),3);self.assertTrue(tx.failed)
 def test_remaining_child_fail_closed_before_parent_delete(self):
  tx=Fake([],[dict(present=True)])
  with self.assertRaises(pg.DatabaseError):effect.drain_channel_memberships(tx,[CHANNEL])
  self.assertTrue(tx.failed)
 def test_empty_parent_set_has_no_cursor(self):
  tx=Fake([],[]);effect.drain_channel_memberships(tx,[]);self.assertEqual(tx.calls,[])
 def test_all_new_statements_pass_real_driver_and_binding(self):
  for query,mode in ((effect._MEMBERS,'read'),(effect._DELETE,'returning'),(effect._ADVISORY,'read'),(effect._WITHDRAW,'write'),(effect._REMAINS,'read'),(owner._INCIDENT_DELETE,'returning')):
   names=re.findall(r'%\(([^)]+)\)s',query);params={name:None for name in names} if names else (None,)*query.count('%s')
   sql=pg._reviewed_statement(query,read=mode!='write',max_rows=50 if mode!='write' else 0,returning=mode=='returning');PostgresQuery(Transformer()).convert(sql.as_string(),params)
 def test_original_owner_order_and_canonical_empty_proof_remain(self):
  source=Path(owner.__file__).read_text();body=source.split('def cleanup_hot_deleted_channels',1)[1]
  self.assertIn('EXISTS(SELECT 1 FROM public.video_inventory WHERE channel_id=',owner._BUSY)
  self.assertLess(body.index('_FENCE'),body.index('drain_channel_memberships'));self.assertLess(body.index('drain_channel_memberships'),body.index('DELETE FROM public.channels'))
  self.assertNotIn('LIMIT',effect._MEMBERS);self.assertNotIn('SKIP LOCKED',effect._MEMBERS);self.assertNotIn('NOWAIT',effect._MEMBERS)
  source=Path(effect.__file__).read_text();self.assertEqual(source.count('with transaction.channel_membership_cascade_rows('),2)
  self.assertLess(source.index('expected+=len(held)'),source.index('receipts=transaction.returning_all'))
if __name__=='__main__':unittest.main()
