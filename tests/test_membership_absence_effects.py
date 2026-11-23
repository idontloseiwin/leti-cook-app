"""Closed ABSENT protocol and exact native receipt routing; offline only."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock,patch
import re,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tools')]
from db.base import postgres as pg
from db.inventory import membership_absence_effects as effect,inventory_scan_pages as owner
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_channel_membership_cascade import ServerError
PARAMS=dict(channel_id='UC0000000000000000000001',video_type='long',scan_id='00000000-0000-0000-0000-000000000001',scan_boundary_at='2026-10-06',source_checked_at='2026-10-06')
class Cursor:
 def __init__(self,connection,name):self.connection,self.name=connection,name;self.description=None;self.rowcount=connection.updated
 def __enter__(self):return self
 def __exit__(self,*args):self.connection.events.append(('close',self.name))
 def execute(self,statement,parameters):
  self.connection.events.append(('execute',self.name,statement,parameters))
  if self.name is None and self.connection.write_error:raise self.connection.write_error
 def fetchmany(self,count):
  self.connection.events.append(('fetch',self.name,count));rows=self.connection.rows[self.name]
  value=rows.popleft() if rows else []
  if isinstance(value,BaseException):raise value
  return value
class Connection:
 def __init__(self,locks,old,updated):
  self.rows={'lic_inventory_absence_locks':deque(locks),'lic_inventory_absence_old':deque(old)};self.updated=updated;self.events=[];self.write_error=None;self.rollbacks=0
 def cursor(self,**options):self.events.append(('cursor',options));return Cursor(self,options.get('name'))
 def execute(self,statement,parameters=()):self.events.append(('setting',statement,parameters))
 @contextmanager
 def transaction(self):
  try:yield
  except BaseException:self.rollbacks+=1;raise

def create(locks=None,old=None,updated=1,**options):
 connection=Connection([[{}],[]] if locks is None else locks,[[{}],[]] if old is None else old,updated);gate=Mock()
 args=dict(total_timeout_ms=5000,statement_timeout_ms=5000);args.update(options)
 return pg.Transaction(connection,gate,**args),connection,gate
class Protocol(unittest.TestCase):
 def test_exhaust_locks_declare_old_one_update_then_first_old_fetch(self):
  tx,c,gate=create(locks=[[{}]*50,[{}],[]],old=[[{}]*50,[{}],[]],updated=51)
  with tx.inventory_absence_changes(PARAMS) as batches:self.assertEqual(sum(map(len,batches)),51)
  significant=[event[:2] for event in c.events if event[0] in ('fetch','execute')]
  self.assertEqual(significant,[('execute','lic_inventory_absence_locks')]+[('fetch','lic_inventory_absence_locks')]*3+[('execute','lic_inventory_absence_old'),('execute',None)]+[('fetch','lic_inventory_absence_old')]*3)
  writes=[event for event in c.events if event[:2]==('execute',None)];self.assertEqual(writes[0][2].as_string(),owner._ABSENT);self.assertEqual(writes[0][3],PARAMS)
  named=[event[1] for event in c.events if event[0]=='cursor' and event[1]]
  self.assertEqual([item['scrollable'] for item in named],[False,False]);self.assertTrue(all(not item['withhold'] for item in named))
  self.assertTrue(all(call.args==(None,) for call in gate.call_args_list));self.assertFalse(tx._failed);self.assertFalse(tx._scheduled_cursor_active)
 def test_zero_rows_still_original_update_and_old_exhaustion(self):
  tx,c,_=create([[]],[[]],0)
  with tx.inventory_absence_changes(PARAMS) as batches:self.assertEqual(list(batches),[])
  self.assertEqual(len([event for event in c.events if event[:2]==('execute',None)]),1)
 def test_incomplete_consumption_poisoned_even_after_last_nonempty_buffer(self):
  for consume in (False,True):
   tx,c,_=create()
   with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_incomplete'):
    with tx.inventory_absence_changes(PARAMS) as batches:
     if consume:next(batches)
   self.assertTrue(tx._failed);self.assertFalse(tx._scheduled_cursor_active)
 def test_receipt_drift_never_yields_effects(self):
  tx,c,_=create(updated=0);entered=False
  with self.assertRaises(pg.DatabaseError):
   with tx.inventory_absence_changes(PARAMS):entered=True
  self.assertFalse(entered);self.assertFalse(any(event[:2]==('fetch','lic_inventory_absence_old') for event in c.events))
 def test_snapshot_cardinality_drift_poisoned(self):
  tx,c,_=create(old=[[]])
  with self.assertRaisesRegex(pg.DatabaseError,'database_cursor_incomplete'):
   with tx.inventory_absence_changes(PARAMS) as batches:list(batches)
  self.assertTrue(tx._failed)
 def test_late_lock_or_update_failure_prevents_effects_and_maps_native(self):
  for stage in ('lock','update'):
   tx,c,_=create(locks=[[{}],ServerError()] if stage=='lock' else None)
   if stage=='update':c.write_error=ServerError()
   entered=False
   with self.assertRaises(pg.DatabaseServerError) as caught:
    with tx.inventory_absence_changes(PARAMS):entered=True
   self.assertEqual(caught.exception.code,'55P03');self.assertFalse(entered);self.assertTrue(tx._failed)
   self.assertFalse(any(event[:2]==('fetch','lic_inventory_absence_old') for event in c.events))
 def test_invalid_capability_is_pre_dispatch(self):
  for kw in (dict(read_only=True),dict(rpc_name='cleanup_hot_inventory'),dict(total_timeout_ms=None)):
   tx,c,_=create(**kw)
   with self.assertRaises(pg.DatabaseError):
    with tx.inventory_absence_changes(PARAMS):pass
   self.assertEqual(c.events,[])
  for args in (None,{},dict(PARAMS,sql='SELECT 1')):
   tx,c,_=create()
   with self.assertRaises(pg.DatabaseError):
    with tx.inventory_absence_changes(args):pass
   self.assertEqual(c.events,[])
 def test_overflow_deadline_and_savepoint_recovery(self):
  tx,c,_=create(locks=[[{}]*51])
  with self.assertRaises(pg.DatabaseError):
   with tx.savepoint():
    with tx.inventory_absence_changes(PARAMS):pass
  self.assertFalse(tx._failed);self.assertEqual(c.rollbacks,1)
  tx,c,_=create()
  with self.assertRaises(pg.DatabaseServerError) as caught:
   with tx.inventory_absence_changes(PARAMS) as batches:tx._deadline=0;next(batches)
  self.assertEqual(caught.exception.code,'57014');self.assertTrue(tx._failed)
 def test_admission_before_every_cursor_or_fetch_and_update(self):
  for fail_at in range(1,15):
   tx,c,gate=create();gate.side_effect=[None]*(fail_at-1)+[RuntimeError('denied')]
   with self.assertRaises(RuntimeError):
    with tx.inventory_absence_changes(PARAMS) as batches:list(batches)
   self.assertTrue(tx._failed)
 def test_no_total_cutoff_and_no_cursor_escape(self):
  rows=[[{}]*50]*20+[[{}]*13,[]];tx,c,_=create(rows,rows,1013)
  with tx.inventory_absence_changes(PARAMS) as batches:self.assertEqual(sum(map(len,batches)),1013)
  with self.assertRaises(pg.DatabaseError):next(batches)
 def test_original_statement_and_fixed_predicates_pass_actual_boundary(self):
  lock,old,update=effect._statements();self.assertEqual(update,owner._ABSENT)
  predicate=owner._ABSENT.split('    WHERE ',1)[1]
  self.assertTrue(lock.endswith(predicate+' FOR NO KEY UPDATE'));self.assertTrue(old.endswith(predicate))
  self.assertNotIn('FOR ',old);self.assertNotIn('LIMIT',lock);self.assertNotIn('SKIP LOCKED',lock)
  for query,read in ((lock,True),(old,True),(update,False),(effect._NEW,True)):
   names=re.findall(r'%\(([^)]+)\)s',query);params={name:None for name in names}
   wrapped=pg._reviewed_statement(query,read=read,max_rows=50 if read else 0)
   PostgresQuery(Transformer()).convert(wrapped.as_string(),params)
class Fake:
 def __init__(self,batches,current):self.batches=batches;self.current=current;self.calls=[];self.failed=False
 @contextmanager
 def borrow(self):
  try:yield self
  except BaseException:self.failed=True;raise
 @contextmanager
 def inventory_absence_changes(self,params):self.calls.append(('source',params));yield iter(self.batches)
 def fetch_all(self,query,args,**options):self.calls.append(('actual_new',query,args,options));return self.current

def previous(video='v'):
 return dict(video_id=video,channel_id=PARAMS['channel_id'],video_type='long',old_text='NATIVE_OLD')
def incoming(video='v',exact=True):
 return dict(video_id=video,channel_id=PARAMS['channel_id'],video_type='long',new_text='NATIVE_NEW',exact=exact)
class Routing(unittest.TestCase):
 def test_native_exact_new_before_only_normal_review_and_original_count(self):
  tx=Fake([[previous('b'),previous('a')]],[incoming('a'),incoming('b')])
  with patch.object(effect,'refresh_review_cases_batch') as refresh:self.assertEqual(effect.mark_absent_memberships(tx,dict(PARAMS,irrelevant='untouched')),2)
  self.assertEqual([call.args for call in refresh.call_args_list],[(tx,['b','a'])]);self.assertTrue(all(not call.kwargs for call in refresh.call_args_list));self.assertFalse(tx.failed)
  self.assertEqual(tx.calls[1][3],dict(max_rows=50))
 def test_missing_changed_duplicate_or_wrong_new_aborts_before_effect(self):
  for rows in ([],[incoming(exact=False)],[incoming(),incoming()],[incoming('other')]):
   tx=Fake([[previous()]],rows)
   with patch.object(effect,'refresh_review_cases_batch') as refresh:
    with self.assertRaises(pg.DatabaseError):effect.mark_absent_memberships(tx,PARAMS)
    refresh.assert_not_called()
   self.assertTrue(tx.failed)
 def test_native_old_not_decoded_and_owner_boundary_stays_before_release(self):
  source=Path(effect.__file__).read_text();self.assertNotIn('json.loads',source);self.assertIn("'updated_at',now()",effect._NEW)
  body=Path(owner.__file__).read_text().split('    source, scan = _owned',1)[1]
  self.assertLess(body.index('_SCAN_UPDATE'),body.index('mark_absent_memberships(tx'))
  self.assertLess(body.index('mark_absent_memberships(tx'),body.index('_RELEASE'))
if __name__=='__main__':unittest.main()
