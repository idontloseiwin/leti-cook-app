"""Finite HOT control entries cannot lend admission to arbitrary SQL owners."""
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
import os,sys,unittest,psycopg
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
import test_direct_pg_transactions as fixtures
from db.base import postgres as pg
from db.hot import hot_collection_termination as helper
import hot_runtime as runtime
class HotControlEntries(unittest.TestCase):
 def setUp(self):
  self.env=patch.dict(os.environ,{'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres','LIC_DATABASE_ID':'f9900000-0000-4000-8000-000000000001','LIC_HOT_RUNTIME_SECRET':'synthetic_test_secret_0123456789abcdef'},clear=True);self.env.start();self.addCleanup(self.env.stop)
  self.events=[];self.conn=fixtures.Connection(self.events);self.pool=fixtures.Pool(self.conn,self.events)
  self.db=pg.PostgresDatabase(pg.PostgresConfig(fixtures.DSN),pool_factory=Mock(return_value=self.pool));self.db.open();self.addCleanup(self.db.close)
 def actions(self):
  return (('abort_hot_collection_claims',lambda:self.db.abort_hot_collection_claims(video_claims=[],playlist_claims=[],operator_claims=[]),helper.ABORT_ERRORS),
   ('drain_hot_collection_claims',lambda:self.db.drain_hot_collection_claims(video_claims=[],playlist_claims=[],operator_claims=[]),helper.ABORT_ERRORS),
   ('interrupt_hot_sync_run',lambda:self.db.interrupt_hot_sync_run(run_id=1,started_at='2026-10-06T01:00:00Z'),helper.INTERRUPT_ERRORS))
 def test_fixed_capability_budget_and_borrowed_transaction(self):
  for name,call,errors in self.actions():
   with patch.object(self.db,'_transaction',wraps=self.db._transaction) as owner,patch.object(helper,name,return_value={'ok':True}) as action,runtime.collection_control_access():
    self.assertEqual(call(),{'ok':True})
   operation=owner.call_args.args[0]
   self.assertEqual((operation.name,operation.statement_timeout_ms,operation.total_timeout_ms,operation.lock_timeout_ms),(name,5000,5000,250))
   self.assertEqual(owner.call_args.kwargs,dict(rpc_name=name,allowed=errors));self.assertEqual(operation.error_codes,errors)
   self.assertFalse(operation.read_only);self.assertIsInstance(action.call_args.args[0],pg.Transaction)
   self.assertFalse(action.call_args.args[0]._active)
 def test_arbitrary_transaction_name_cannot_borrow_control_permission(self):
  with runtime.collection_control_access():
   for name,_,_ in self.actions():
    with self.assertRaises(runtime.HotDatabaseAdmissionDenied) as caught:
     with self.db.transaction(pg.Operation(name)):self.fail('admitted arbitrary body')
    self.assertEqual(caught.exception.code,'hot_runtime_control_scope_denied')
  self.assertEqual(self.pool.borrowed,0)
 def test_pool_wait_is_followed_by_exact_control_recheck(self):
  for name,call,_ in self.actions():
   seen=[]
   def gate(value):
    seen.append(value)
    if len(seen)==2:raise runtime.HotDatabaseAdmissionDenied('hot_runtime_not_ready')
   with patch.object(self.db,'_admission',side_effect=gate),patch.object(helper,name) as action:
    with self.assertRaises(runtime.HotDatabaseAdmissionDenied):call()
   self.assertEqual(seen,[name,name]);action.assert_not_called()
 def test_unknown_commit_released_pool_and_not_retried(self):
  for name,call,_ in self.actions():
   self.conn.commit_error=psycopg.OperationalError('synthetic')
   def dispatched(tx,*args):tx.execute('UPDATE public.sync_runs SET errors=errors');return {'ok':True}
   self.conn.results.append(fixtures.Result(has_columns=False,rowcount=0))
   with patch.object(helper,name,side_effect=dispatched) as action,runtime.collection_control_access():
    with self.assertRaises(pg.DatabaseUnavailable) as caught:call()
   self.assertTrue(caught.exception.outcome_unknown);action.assert_called_once()
   self.assertEqual(self.pool.borrowed,self.pool.returned)
 def test_closed_entries_need_explicit_control_scope_when_runtime_blocked(self):
  blocked=SimpleNamespace(require_collection_ready=Mock(side_effect=runtime.HotRuntimeUnavailable('hot_runtime_not_ready')))
  with patch.object(runtime,'_local_controller',blocked):
   for name,call,_ in self.actions():
    with patch.object(helper,name) as action,self.assertRaises(runtime.HotDatabaseAdmissionDenied) as caught:call()
    self.assertEqual(caught.exception.code,'hot_runtime_not_ready');action.assert_not_called()
  self.assertEqual(self.pool.borrowed,0)
 def test_no_arbitrary_callback_or_budget_arguments(self):
  with self.assertRaises(TypeError):self.db.abort_hot_collection_claims(video_claims=[],playlist_claims=[],operator_claims=[],callback=lambda:None)
  with self.assertRaises(TypeError):self.db.interrupt_hot_sync_run(run_id=1,started_at='now',statement_timeout_ms=60000)
  self.assertEqual(self.pool.borrowed,0)
if __name__=='__main__':unittest.main()
