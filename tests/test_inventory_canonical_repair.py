"""Repair binds approved plan/identity and rolls back every failed postcondition."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.inventory import inventory_canonical_repair as h,canonical_repair_effects as effects
from db.base.postgres import DatabaseError,DatabaseServerError,_reviewed_statement

class Tx:
 def __init__(self,stage=None,count=1):self.stage=stage;self.count=count;self.events=[];self.poison=False
 @contextmanager
 def borrow(self):
  try:yield self
  except BaseException:self.poison=True;raise
 def fetch_one(self,sql,params=()):
  self.events.append(sql)
  if sql==h._INPUT:return dict(expected_count=self.count,fingerprint='a'*32)
  if sql==h._FINGERPRINT:return dict(valid=self.stage!='fingerprint')
  if sql==h._CANDIDATE_IDS:return dict(video_ids=[] if self.stage=='drift' else ['video'],video_types=['short'])
  if sql==h._QUEUE_COUNT:return dict(count=1)
  if sql==h._UNRESOLVED:return dict(count=1 if self.stage=='unresolved' else 0)
  if sql==h._REMAINS:return dict(present=self.stage=='remains')
  raise AssertionError('unexpected read')
 def lock_typed_inventory_repair(self):self.events.append('locks')
 def returning_all(self,sql,params,*,max_rows):
  self.events.append(sql);assert sql==h._UPDATE_EFFECT and params==(['video'],['short']) and max_rows==5000
  return [] if self.stage=='updated' else [dict(old_text='NATIVE_OLD',new_text='NATIVE_NEW')]
class DB:
 def __init__(self,tx,unknown=False):self.tx=tx;self.commits=0;self.operations=[];self.unknown=unknown
 @contextmanager
 def transaction(self,operation):
  self.operations.append(operation)
  with self.tx.borrow():yield self.tx
  self.commits+=1
  if self.unknown:raise DatabaseError('database_connection_failed',outcome_unknown=True)

class RepairTests(unittest.TestCase):
 def invoke(self,tx,db=None,plan=None,borrow=False):
  db=db or DB(tx)
  def preview(*args,**kwargs):
   self.assertIs(kwargs['transaction'],tx);self.assertEqual(args,(db,0));tx.events.append('preview');return plan or dict(status='ready',repairable_count=1,candidate_fingerprint='a'*32)
  def effect(transaction,receipts):
   self.assertIs(transaction,tx);self.assertEqual(receipts,[dict(old_text='NATIVE_OLD',new_text='NATIVE_NEW')]);tx.events.append('effects')
  with patch.object(effects,'after_canonical_repair_statement',side_effect=effect),patch.object(h,'inspect_typed_inventory_canonical_repair',side_effect=preview):value=h.repair_typed_inventory_canonical_types(db,1,'a'*32,transaction=tx if borrow else None)
  return value,db
 def test_validation_lock_plan_snapshot_update_and_postconditions_order(self):
  tx=Tx();value,db=self.invoke(tx)
  self.assertEqual(tx.events,[h._INPUT,h._FINGERPRINT,'locks','preview',h._CANDIDATE_IDS,h._QUEUE_COUNT,h._UPDATE_EFFECT,'effects',h._UNRESOLVED,h._REMAINS])
  self.assertEqual(value,dict(status='completed',schema_version=2,updated=1,collection_state_rows_removed=1,canonical_channel_mutation=False,videos_deleted=0,snapshots_deleted=0,youtube_requests=0,runtime_activation=False,product_exposure_changed=False));self.assertEqual(db.commits,1)
 def test_input_limits_precede_lock_or_preview(self):
  for count in (None,0,5001):
   tx=Tx(count=count)
   with self.assertRaises(DatabaseServerError) as caught:self.invoke(tx)
   self.assertEqual(caught.exception.message,'typed_inventory_canonical_repair_expected_count_invalid');self.assertEqual(tx.events,[h._INPUT])
  tx=Tx('fingerprint')
  with self.assertRaises(DatabaseServerError):self.invoke(tx)
  self.assertNotIn('locks',tx.events)
 def test_readiness_and_fingerprint_or_count_mismatch_never_write(self):
  for plan in (dict(status='blocked',repairable_count=1,candidate_fingerprint='a'*32),dict(status='ready',repairable_count=2,candidate_fingerprint='a'*32),dict(status='ready',repairable_count=1,candidate_fingerprint='b'*32)):
   tx=Tx();db=DB(tx)
   with self.assertRaises(DatabaseServerError):self.invoke(tx,db,plan)
   self.assertEqual(tx.events[-1],'preview');self.assertEqual(db.commits,0);self.assertTrue(tx.poison)
 def test_each_postcondition_failure_poison_and_no_commit(self):
  for stage,suffix in (('drift','candidate_drift'),('updated','update_count_mismatch'),('unresolved','postcondition_failed'),('remains','collection_state_remains')):
   tx=Tx(stage);db=DB(tx)
   with self.subTest(stage=stage),self.assertRaises(DatabaseServerError) as caught:self.invoke(tx,db)
   self.assertEqual(caught.exception.message,'typed_inventory_canonical_repair_'+suffix);self.assertEqual(db.commits,0);self.assertTrue(tx.poison)
 def test_borrowed_transaction_owns_no_second_connection_or_commit(self):
  tx=Tx();_,db=self.invoke(tx,borrow=True);self.assertEqual(db.operations,[]);self.assertEqual(db.commits,0)
 def test_unknown_commit_not_replayed(self):
  tx=Tx();db=DB(tx,unknown=True)
  with self.assertRaises(DatabaseError) as caught:self.invoke(tx,db)
  self.assertTrue(caught.exception.outcome_unknown);self.assertEqual(len(db.operations),1);self.assertEqual(db.commits,1)
 def test_exact_registered_timeouts_and_domain_errors(self):
  _,db=self.invoke(Tx());spec=h.get_operation('repair_typed_inventory_canonical_types');op=db.operations[0]
  self.assertEqual((op.statement_timeout_ms,op.lock_timeout_ms,op.error_codes),(spec.statement_timeout_ms,spec.lock_timeout_ms,spec.error_codes));self.assertFalse(op.read_only)
 def test_driver_guards_read_and_update_queries(self):
  for query in (h._CANDIDATE_IDS,h._QUEUE_COUNT,h._UNRESOLVED,h._REMAINS):_reviewed_statement(query,read=True,max_rows=1)
  _reviewed_statement(h._UPDATE_EFFECT,read=True,max_rows=5000,returning=True)
if __name__=='__main__':unittest.main()
