"""Initial playlist preparation keeps channel/playlist/final fence ownership."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_initial_preparation as h
from db.base.postgres import DatabaseError,DatabaseServerError

class Transaction:
 def __init__(self,*,invalid=False,exists=True,scope=True,counts=(1,1,1)):
  self.invalid=invalid;self.exists=exists;self.scope=scope;self.counts=iter(counts);self.events=[];self.poison=False
 def fetch_one(self,sql,args):
  self.events.append((sql,args))
  if sql==h._INPUT:return {'channel_id':'UCchannel','invalid':self.invalid}
  if sql==h._LOCK:return {'channel_id':'UCchannel'} if self.exists else None
  if sql==h._SCOPE:return None if self.scope is None else {'control_epoch':9,'policy_facts':native_eligible_facts(self.scope)}
  raise AssertionError('unexpected read')
 def execute(self,sql,args):
  self.events.append((sql,args));assert sql==h._INSERT;return next(self.counts)
 @contextmanager
 def borrow(self):
  try:yield self
  except BaseException:self.poison=True;raise
class Database:
 def __init__(self,tx,unknown=False):self.tx=tx;self.attempts=0;self.commits=0;self.unknown=unknown;self.operations=[]
 @contextmanager
 def transaction(self,operation):
  self.attempts+=1;self.operations.append(operation)
  with self.tx.borrow():yield self.tx
  self.commits+=1
  if self.unknown:raise DatabaseError('database_connection_failed',outcome_unknown=True)
class InitialPreparationTests(unittest.TestCase):
 def run_case(self,tx,db=None):
  db=db or Database(tx)
  with patch.object(h,'assert_channel_scope',side_effect=lambda *args:tx.events.append(('fence',args[1:]))):result=h.prepare_channel_initial_typed_inventory(db,' UCchannel ')
  return result,db
 def test_three_ordered_insertions_and_final_fence(self):
  tx=Transaction();result,db=self.run_case(tx)
  self.assertEqual(result,h._result('UCchannel','ok',3));self.assertEqual(db.commits,1)
  self.assertEqual([e[0] for e in tx.events],[h._INPUT,h._LOCK,h._SCOPE,h._INSERT,h._INSERT,h._INSERT,'fence'])
  self.assertEqual([e[1][1:3] for e in tx.events[3:6]],[('long','UULF'),('short','UUSH'),('stream','UULV')]);self.assertEqual(tx.events[-1],('fence',('UCchannel',9)))
 def test_existing_rows_count_only_successful_insert(self):
  for counts in ((0,0,0),(1,0,1),(0,1,0)):
   result,_=self.run_case(Transaction(counts=counts));self.assertEqual(result,h._result('UCchannel','ok',sum(counts)))
 def test_missing_channel_never_reads_scope_or_writes(self):
  tx=Transaction(exists=False);result,_=self.run_case(tx);self.assertEqual(result,h._result('UCchannel','not_found'));self.assertEqual(len(tx.events),2)
 def test_ineligible_or_missing_control_never_writes(self):
  for scope in (None,False):
   tx=Transaction(scope=scope);result,_=self.run_case(tx);self.assertEqual(result,h._result('UCchannel','ineligible'));self.assertEqual(len(tx.events),3)
 def test_native_validation_precedes_channel_lookup(self):
  tx=Transaction(invalid=True)
  with self.assertRaises(DatabaseServerError) as caught:self.run_case(tx)
  self.assertEqual(caught.exception.message,'initial_typed_channel_id_invalid');self.assertEqual(len(tx.events),1);self.assertTrue(tx.poison)
 def test_final_epoch_loss_poison_and_no_commit(self):
  tx=Transaction();db=Database(tx)
  with patch.object(h,'assert_channel_scope',side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')):
   with self.assertRaises(DatabaseServerError):h.prepare_channel_initial_typed_inventory(db,'UCchannel')
  self.assertTrue(tx.poison);self.assertEqual(db.commits,0)
 def test_borrowed_context_does_not_open_connection(self):
  tx=Transaction();db=Database(tx)
  with patch.object(h,'assert_channel_scope'):h.prepare_channel_initial_typed_inventory(db,'UCchannel',transaction=tx)
  self.assertEqual(db.attempts,0)
 def test_unknown_commit_no_automatic_retry(self):
  tx=Transaction();db=Database(tx,unknown=True)
  with self.assertRaises(DatabaseError) as caught:self.run_case(tx,db)
  self.assertTrue(caught.exception.outcome_unknown);self.assertEqual((db.attempts,db.commits),(1,1))
 def test_same_registered_timeout_and_errors(self):
  _,db=self.run_case(Transaction());spec=h.get_operation('prepare_channel_initial_typed_inventory');op=db.operations[0]
  self.assertEqual((op.name,op.statement_timeout_ms,op.lock_timeout_ms,op.error_codes),(spec.name,spec.statement_timeout_ms,spec.lock_timeout_ms,spec.error_codes))
 def test_no_stored_policy_or_mutation_of_existing_rows(self):
  self.assertNotIn('public.typed_inventory_channel_eligible',h._SCOPE);self.assertIn('projection.policy_facts',h._SCOPE);self.assertNotIn('public.channel_collection_control',h._SCOPE)
  self.assertIn('ON CONFLICT(channel_id,video_type) DO NOTHING',h._INSERT);self.assertNotIn('UPDATE',h._INSERT)
if __name__=='__main__':unittest.main()
