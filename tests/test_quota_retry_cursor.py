"""Actual isolated driver cursor ownership and failure envelope; offline only."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys,unittest
from unittest.mock import Mock,patch
import psycopg
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tools')]
from db.base import postgres as pg
from db.quota.quota_retry_detachment import _CHILDREN
from hot_admission import real_admission
import hot_runtime
PARENT='60000000-0000-4000-8000-000000000001'

class ServerError(psycopg.Error):
 @property
 def sqlstate(self):return '55P03'
 @property
 def diag(self):return SimpleNamespace(message_primary='private server detail')

class Cursor:
 def __init__(self,owner):self.owner=owner
 def __enter__(self):return self
 def __exit__(self,*args):
  self.owner.closed+=1
  if self.owner.close_error:raise self.owner.close_error
 def execute(self,statement,parameters):
  self.owner.queries.append((statement,parameters))
  if self.owner.execute_error:raise self.owner.execute_error
 def fetchmany(self,count):
  self.owner.fetch_sizes.append(count)
  value=self.owner.rows.popleft() if self.owner.rows else []
  if isinstance(value,BaseException):raise value
  return value

class Connection:
 def __init__(self,*rows):
  self.rows=deque(rows);self.queries=[];self.settings=[];self.cursor_options=[];self.fetch_sizes=[]
  self.closed=0;self.rollbacks=0;self.close_error=self.execute_error=None
 def execute(self,statement,parameters=()):self.settings.append((statement,parameters))
 def cursor(self,**options):self.cursor_options.append(options);return Cursor(self)
 @contextmanager
 def transaction(self):
  try:yield
  except BaseException:self.rollbacks+=1;raise

def create(*rows,**kwargs):
 connection=Connection(*rows);gate=Mock();values=dict(rpc_name='cleanup_hot_operational_copies',total_timeout_ms=5000,statement_timeout_ms=5000)
 values.update(kwargs);return pg.Transaction(connection,gate,**values),connection,gate

class CursorTests(unittest.TestCase):
 def test_fixed_snapshot_one_row_fetch_and_exhaustion(self):
  tx,c,gate=create([{'id':1}],[{'id':2}],[])
  with tx.quota_retry_children(parent_id=PARENT) as rows:
   self.assertEqual(list(rows),[{'id':1},{'id':2}]);self.assertEqual(list(rows),[])
  self.assertEqual(c.queries,[(_CHILDREN,(PARENT,PARENT))]);self.assertEqual(c.fetch_sizes,[1,1,1])
  self.assertEqual(c.cursor_options,[dict(name='lic_quota_retry_children',scrollable=False,withhold=False,row_factory=pg.dict_row)])
  self.assertEqual(c.closed,1);self.assertFalse(tx._scheduled_cursor_active)
  self.assertTrue(all(call.args==('cleanup_hot_operational_copies',) for call in gate.call_args_list))
  self.assertTrue(all(0<int(params[0][:-2])<=5000 for _,params in c.settings))
 def test_capabilities_denied_before_cursor_or_sql(self):
  for kwargs in (dict(read_only=True),dict(rpc_name=None),dict(rpc_name='cleanup_hot_evidence'),dict(total_timeout_ms=None)):
   with self.subTest(kwargs=kwargs):
    tx,c,_=create(**kwargs)
    with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_capability_invalid'):
     with tx.quota_retry_children(parent_id=PARENT):pass
    self.assertTrue(tx._failed);self.assertEqual(c.queries,[]);self.assertEqual(c.cursor_options,[])
  for value in (None,1,'bad',PARENT+'x'):
   tx,c,_=create()
   with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_capability_invalid'):
    with tx.quota_retry_children(parent_id=value):pass
   self.assertEqual(c.cursor_options,[])
 def test_no_extra_sql_callback_or_batch_size(self):
  tx,c,_=create()
  for name in ('sql','callback','batch_size'):
   with self.assertRaises(TypeError):tx.quota_retry_children(parent_id=PARENT,**{name:None})
  self.assertEqual(c.cursor_options,[])
 def test_stream_cannot_escape_context(self):
  tx,c,_=create([{'id':1}])
  with tx.quota_retry_children(parent_id=PARENT) as rows:pass
  with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_owner_invalid'):next(rows)
  self.assertTrue(tx._failed);self.assertEqual(c.fetch_sizes,[])
 def test_transaction_owner_bound_before_open_and_during_fetch(self):
  for attribute in ('_active','_pid','_thread'):
   tx,c,_=create([{'id':1}]);setattr(tx,attribute,False if attribute=='_active' else -1)
   with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_owner_invalid'):
    with tx.quota_retry_children(parent_id=PARENT):pass
   self.assertEqual(c.cursor_options,[])
   tx,c,_=create([{'id':1}])
   with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_owner_invalid'):
    with tx.quota_retry_children(parent_id=PARENT) as rows:
     setattr(tx,attribute,False if attribute=='_active' else -1);next(rows)
   self.assertEqual(c.fetch_sizes,[]);self.assertEqual(c.closed,1)
 def test_reentrant_and_other_active_cursor_denied_and_poisoned(self):
  tx,c,_=create()
  with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_capability_invalid'):
   with tx.quota_retry_children(parent_id=PARENT):
    with tx.quota_retry_children(parent_id=PARENT):pass
  self.assertEqual(len(c.cursor_options),1);self.assertEqual(c.closed,1);self.assertTrue(tx._failed);self.assertFalse(tx._scheduled_cursor_active)
  tx,c,_=create();tx._scheduled_cursor_active=True
  with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_capability_invalid'):
   with tx.quota_retry_children(parent_id=PARENT):pass
  self.assertTrue(tx._scheduled_cursor_active);self.assertEqual(c.cursor_options,[])
 def test_deadline_before_open_and_between_fetches(self):
  tx,c,_=create();tx._deadline=0
  with self.assertRaises(pg.DatabaseServerError) as caught:
   with tx.quota_retry_children(parent_id=PARENT):pass
  self.assertEqual(caught.exception.code,'57014');self.assertEqual(c.cursor_options,[])
  tx,c,_=create([{'id':1}],[{'id':2}])
  with self.assertRaises(pg.DatabaseServerError) as caught:
   with tx.quota_retry_children(parent_id=PARENT) as rows:
    next(rows);tx._deadline=0;next(rows)
  self.assertEqual(caught.exception.code,'57014');self.assertEqual(c.fetch_sizes,[1]);self.assertEqual(c.closed,1)
 def test_admission_rechecked_before_dispatch_and_each_fetch(self):
  for fail_at in (1,2,3,4):
   tx,c,gate=create([{'id':1}]);gate.side_effect=[None]*(fail_at-1)+[RuntimeError('denied')]
   with self.assertRaisesRegex(RuntimeError,'denied'):
    with tx.quota_retry_children(parent_id=PARENT) as rows:next(rows)
   self.assertTrue(tx._failed);self.assertEqual(c.fetch_sizes,[])
   self.assertEqual(len(c.queries),0 if fail_at<=2 else 1)
 def test_real_admission_name_kept_for_maintenance_cursor(self):
  with real_admission():
   tx,c,_=create([{'id':1}],[]);tx._admission=pg._runtime_admission
   with hot_runtime.maintenance_access():
    with tx.quota_retry_children(parent_id=PARENT) as rows:self.assertEqual(list(rows),[{'id':1}])
   tx,c,_=create();tx._admission=pg._runtime_admission
   with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
    with tx.quota_retry_children(parent_id=PARENT):pass
   self.assertEqual(c.cursor_options,[])
 def test_row_overflow_poisoned_and_cursor_closed(self):
  tx,c,_=create([{'id':1},{'id':2}])
  with self.assertRaisesRegex(pg.DatabaseError,'database_result_limit_exceeded'):
   with tx.quota_retry_children(parent_id=PARENT) as rows:next(rows)
  self.assertTrue(tx._failed);self.assertEqual(c.fetch_sizes,[1]);self.assertEqual(c.closed,1)
 def test_native_fetch_failure_rollback_resets_only_explicit_savepoint(self):
  tx,c,_=create(ServerError())
  with self.assertRaises(pg.DatabaseServerError) as caught:
   with tx.savepoint():
    with tx.quota_retry_children(parent_id=PARENT) as rows:next(rows)
  self.assertEqual(caught.exception.code,'55P03');self.assertFalse(tx._failed)
  self.assertEqual(c.rollbacks,1);self.assertEqual(c.closed,1);self.assertFalse(tx._scheduled_cursor_active)
  with tx.quota_retry_children(parent_id=PARENT) as rows:self.assertEqual(list(rows),[])
  self.assertEqual(len(c.queries),2)
 def test_close_failure_poisoned_no_replay(self):
  tx,c,_=create();c.close_error=RuntimeError('close failure')
  with self.assertRaisesRegex(RuntimeError,'close failure'):
   with tx.quota_retry_children(parent_id=PARENT):pass
  self.assertTrue(tx._failed);self.assertFalse(tx._scheduled_cursor_active);self.assertEqual(len(c.queries),1)
 def test_no_total_row_cutoff(self):
  tx,c,_=create(*([[{'id':i}] for i in range(513)]+[[]]))
  with tx.quota_retry_children(parent_id=PARENT) as rows:self.assertEqual(len(list(rows)),513)
  self.assertEqual(c.fetch_sizes,[1]*514)

if __name__=='__main__':unittest.main()
