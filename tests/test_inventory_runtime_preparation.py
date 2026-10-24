"""Global preparation preserves per-slot interleaving and complete pause scope."""
from contextlib import contextmanager
from pathlib import Path
import sys,unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_runtime_preparation as h
from db.base.postgres import DatabaseServerError,DatabaseError,_reviewed_statement

class Transaction:
 def __init__(self,limit=2,enable=True,channels=None,slots=None,paused=False):
  self.limit=limit;self.enable=enable;self.channels=channels if channels is not None else [dict(channel_id='UCa',control_epoch=9),dict(channel_id='UCb',control_epoch=11)]
  self.slots=iter(slots if slots is not None else [None]*6);self.events=[];self.poison=False;self.paused=paused;self.inserts=iter([1]*6)
 def fetch_one(self,query,args=()):
  self.events.append((query,args))
  if query==h._INPUT:return dict(limit=self.limit,enable=self.enable)
  if query==h._PAUSE_LOCK:return dict(channel_ids=['old'] if self.paused else None,video_types=['long'] if self.paused else None,scan_ids=[None] if self.paused else None)
  if query==h._SLOT:return next(self.slots)
  if query==h._REMAINING_PREPARE:return dict(value=7)
  if query==h._REMAINING_ENABLE:return dict(value=8)
  raise AssertionError('unexpected query')
 def fetch_all(self,query,args=(),*,max_rows):
  self.events.append((query,args,max_rows))
  if query==h._PAUSE_PROJECTION:return [dict(matched=1)]
  if query==h._CANDIDATES:return self.channels
  raise AssertionError('unexpected query')
 def execute(self,query,args=()):
  self.events.append((query,args))
  if query==h._INSERT:return next(self.inserts)
  if query in (h._ENABLE,h._PAUSE_SCANS,h._PAUSE_PLAYLISTS):return 1
  raise AssertionError('unexpected write')
 @contextmanager
 def borrow(self):
  try:yield self
  except BaseException:self.poison=True;raise
class Database:
 def __init__(self,tx,unknown=False):self.tx=tx;self.unknown=unknown;self.operations=[];self.commits=0
 @contextmanager
 def transaction(self,operation):
  self.operations.append(operation)
  with self.tx.borrow():yield self.tx
  self.commits+=1
  if self.unknown:raise DatabaseError('database_connection_failed',outcome_unknown=True)

class RuntimePreparationTests(unittest.TestCase):
 def run_case(self,tx,db=None):
  db=db or Database(tx)
  with patch.object(h,'assert_channel_scopes',side_effect=lambda _,scopes:tx.events.append(('fence',scopes))):result=h.prepare_typed_inventory_runtime(db,tx.limit,tx.enable)
  return result,db
 def test_each_channel_locks_then_writes_long_short_stream_in_native_order(self):
  tx=Transaction();result,db=self.run_case(tx)
  self.assertEqual(result,dict(selected_channels=2,inventory_states_inserted=6,discovery_states_inserted=6,discovery_states_enabled=0,remaining_to_prepare=7,remaining_to_enable=8,enable_requested=True))
  steps=tx.events[3:-3]
  self.assertEqual([s[0] for s in steps],[h._SLOT,h._INSERT]*6)
  self.assertEqual([s[1] for s in steps[::2]],[(c,t) for c in ('UCa','UCb') for t in ('long','short','stream')])
  self.assertEqual([s[1][2] for s in steps[1::2]],['UULF','UUSH','UULV']*2)
  self.assertEqual([s[0] for s in tx.events[-3:]],[h._REMAINING_PREPARE,h._REMAINING_ENABLE,'fence'])
  self.assertEqual(tx.events[-1][1],tx.channels);self.assertEqual((db.commits,len(db.operations)),(1,1))
 def test_concurrent_insert_conflict_is_not_promoted_by_a_stale_missing_decision(self):
  tx=Transaction(channels=[dict(channel_id='UCa',control_epoch=9)],slots=[None]*3);tx.inserts=iter([0,1,0])
  result,_=self.run_case(tx);self.assertEqual(result['inventory_states_inserted'],1)
  self.assertNotIn(h._ENABLE,[e[0] for e in tx.events]);self.assertIn('ON CONFLICT(channel_id,video_type) DO NOTHING',h._INSERT)
 def test_existing_active_scan_and_ready_row_are_preserved(self):
  slots=[dict(active_scan_id='active',runtime_enabled=False,status='paused'),dict(active_scan_id=None,runtime_enabled=True,status='ready'),dict(active_scan_id=None,runtime_enabled=False,status='paused')]
  tx=Transaction(channels=[dict(channel_id='UCa',control_epoch=9)],slots=slots)
  result,_=self.run_case(tx);self.assertEqual(result['discovery_states_enabled'],1)
  self.assertEqual([e[1] for e in tx.events if e[0]==h._ENABLE],[('UCa','stream')]);self.assertEqual(result['inventory_states_inserted'],0)
 def test_enable_false_never_changes_existing_slots(self):
  tx=Transaction(enable=False,channels=[dict(channel_id='UCa',control_epoch=9)],slots=[dict(active_scan_id=None,runtime_enabled=False,status='paused')]*3)
  result,_=self.run_case(tx);self.assertFalse(result['enable_requested']);self.assertEqual(result['discovery_states_enabled'],0)
  self.assertNotIn(h._ENABLE,[e[0] for e in tx.events])
 def test_pause_freezes_complete_set_before_projection_scans_and_playlists(self):
  tx=Transaction(limit=1,channels=[],paused=True);self.run_case(tx)
  self.assertEqual([e[0] for e in tx.events[1:6]],[h._PAUSE_LOCK,h._PAUSE_PROJECTION,h._PAUSE_SCANS,h._PAUSE_PLAYLISTS,h._CANDIDATES])
  self.assertEqual(tx.events[2][2],30000);self.assertNotIn('LIMIT',h._PAUSE_LOCK.upper());self.assertNotIn('FOR UPDATE',h._PAUSE_PROJECTION.upper())
  self.assertIn('for update of discovery',h._PAUSE_LOCK)
 def test_blocked_full_initial_exception_remains_in_native_pause_selection(self):
  self.assertIn("job.status in('blocked','error','suspended')",h._PAUSE_LOCK)
  self.assertIn('job.accepted_epoch=control.control_epoch',h._PAUSE_LOCK)
  self.assertIn('job.initial_scope_version=1',h._PAUSE_LOCK)
 def test_invalid_input_order_precedes_any_pause(self):
  for limit,enable,message in ((0,None,'typed_inventory_prepare_limit_invalid'),(None,True,'typed_inventory_prepare_limit_invalid'),(1001,True,'typed_inventory_prepare_limit_invalid'),(1,None,'typed_inventory_prepare_enable_required')):
   tx=Transaction(limit=limit,enable=enable)
   with self.assertRaisesRegex(DatabaseServerError,message):self.run_case(tx)
   self.assertEqual(len(tx.events),1);self.assertTrue(tx.poison)
 def test_empty_candidates_still_take_two_fresh_counts_then_empty_fence(self):
  tx=Transaction(channels=[]);result,_=self.run_case(tx)
  self.assertEqual(result['selected_channels'],0);self.assertEqual(tx.events[-1],('fence',[]))
  self.assertEqual(len(tx.events),6)
 def test_final_generation_failure_poison_prevents_commit(self):
  tx=Transaction();db=Database(tx)
  with patch.object(h,'assert_channel_scopes',side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')):
   with self.assertRaises(DatabaseServerError):h.prepare_typed_inventory_runtime(db,2,True)
  self.assertEqual(db.commits,0);self.assertTrue(tx.poison)
 def test_unknown_commit_no_replay(self):
  tx=Transaction();db=Database(tx,True)
  with self.assertRaises(DatabaseError) as caught:self.run_case(tx,db)
  self.assertTrue(caught.exception.outcome_unknown);self.assertEqual((db.commits,len(db.operations)),(1,1))
 def test_parent_context_owns_all_steps_and_budget(self):
  tx=Transaction(channels=[]);db=Database(tx)
  with patch.object(h,'assert_channel_scopes'):h.prepare_typed_inventory_runtime(db,2,True,transaction=tx)
  self.assertEqual(db.operations,[])
 def test_registered_timeout_covers_entire_protocol_not_each_loop(self):
  _,db=self.run_case(Transaction());spec=h.get_operation('prepare_typed_inventory_runtime');op=db.operations[0]
  self.assertEqual((op.total_timeout_ms,op.statement_timeout_ms,op.lock_timeout_ms,op.error_codes),(spec.statement_timeout_ms,spec.statement_timeout_ms,spec.lock_timeout_ms,spec.error_codes))
 def test_all_fixed_statements_pass_real_driver_and_psycopg_binding(self):
  selected=dict(channel_ids=['UCa'],video_types=['long'],scan_ids=[None])
  vectors=[(h._INPUT,(2,True),True),(h._PAUSE_LOCK,(),True),(h._PAUSE_PROJECTION,selected,True),(h._PAUSE_SCANS,selected,False),(h._PAUSE_PLAYLISTS,selected,False),(h._CANDIDATES,(True,2),True),(h._SLOT,('UCa','long'),True),(h._INSERT,('UCa','long','UULF','UCa',True,True,True),False),(h._ENABLE,('UCa','long'),False),(h._REMAINING_PREPARE,(),True),(h._REMAINING_ENABLE,(),True)]
  for sql,args,read in vectors:
   _reviewed_statement(sql,read=read,max_rows=30000 if read else None);PostgresQuery(Transformer()).convert(sql,args)
   self.assertNotIn('public.typed_inventory_channel_eligible(',sql)
 def test_no_stored_business_rpc_or_automatic_retry(self):
  source=Path(h.__file__).read_text();self.assertNotIn('database.call(',source);self.assertNotIn('time.sleep(',source)
if __name__=='__main__':unittest.main()
