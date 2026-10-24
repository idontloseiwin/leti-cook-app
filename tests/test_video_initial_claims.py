"""Initial job ownership and source validation precede its bounded video leases."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from contextlib import contextmanager
from datetime import datetime,timezone
from uuid import UUID
from pathlib import Path
import sys,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.videos import video_initial_claims as h
from db.base.postgres import DatabaseError,DatabaseServerError

class Transaction:
 def __init__(self,stage=None,count=1,rows=None):
  self.stage=stage;self.count=count;self.events=[];self.poison=False
  self.rows=rows if rows is not None else [dict(video_id='video',channel_id='UCchannel',video_type='long',collection_mode='metadata',lease_token='token',lease_expires_at='expiry',next_due_at='due',lease_channel_epoch=9,input_position=1)]
 def fetch_one(self,sql,args):
  self.events.append((sql,args))
  if sql==h._INPUT:return dict(job_id='job',video_ids=['video'] if self.count else [],worker_id=' worker ',run_id='run',lease_seconds=900,id_count=self.count,invalid=self.stage=='input')
  if sql==h._FIND:return None if self.stage=='missing' else dict(channel_id='UCchannel')
  if sql==h._CHANNEL:return None if self.stage=='channel' else dict(channel_id='UCchannel')
  if sql==h._ELIGIBLE:return dict(policy_facts=native_eligible_facts(self.stage!='ineligible'))
  if sql==h._JOB:return None if self.stage=='vanished' else dict(value='{}',epoch=9,expires='infinity',invalid=self.stage=='owner')
  if sql==h._SOURCE:return dict(invalid=self.stage=='source')
  if sql==h._TARGETS:return dict(invalid=self.stage=='targets')
  raise AssertionError('unexpected read')
 def execute(self,sql,args):self.events.append((sql,args));assert sql==h._INSERT;return 0
 def returning_all(self,sql,args,max_rows):
  self.events.append((sql,args));assert sql==h._CLAIM;assert max_rows==50;return self.rows
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

class InitialClaimTests(unittest.TestCase):
 def run_case(self,tx,db=None,borrow=False):
  db=db or Database(tx)
  with patch.object(h,'assert_full_initial_scope',side_effect=lambda *args:tx.events.append(('source_proof',args[1:]))),patch.object(h,'assert_channel_scopes',side_effect=lambda *args:tx.events.append(('fence',args[1]))):
   result=h.claim_initial_video_collection_batch(db,'job',['video'],' worker ','run',transaction=tx if borrow else None)
  return result,db
 def test_ordered_ownership_before_single_atomic_claim_and_final_fence(self):
  tx=Transaction();result,db=self.run_case(tx)
  self.assertEqual([v[0] for v in tx.events],[h._INPUT,h._FIND,h._CHANNEL,h._ELIGIBLE,h._JOB,h._SOURCE,'source_proof',h._TARGETS,h._INSERT,h._CLAIM,'fence'])
  self.assertEqual(tx.events[6][1],('{}',True));self.assertEqual(tx.events[-1][1],[dict(channel_id='UCchannel',control_epoch=9)])
  self.assertEqual(set(result[0]),{'video_id','channel_id','video_type','collection_mode','lease_token','lease_expires_at','next_due_at'});self.assertEqual(db.commits,1)
 def test_errors_do_not_acquire_video_lease(self):
  cases={'input':'claim_input_invalid','missing':'job_missing','channel':'channel_ineligible','ineligible':'channel_ineligible','vanished':'job_lease_lost','owner':'job_lease_lost','targets':'target_scope_invalid'}
  for stage,suffix in cases.items():
   with self.subTest(stage=stage):
    tx=Transaction(stage);db=Database(tx)
    with self.assertRaises(DatabaseServerError) as caught:self.run_case(tx,db)
    self.assertEqual(caught.exception.message,'initial_video_collection_'+suffix)
    self.assertFalse(any(e[0] in (h._INSERT,h._CLAIM) for e in tx.events));self.assertEqual(db.commits,0);self.assertTrue(tx.poison)
 def test_expired_source_is_distinct_from_job_lease(self):
  tx=Transaction('source')
  with self.assertRaises(DatabaseServerError) as caught:self.run_case(tx)
  self.assertEqual(caught.exception.message,'operator_job_source_scope_expired');self.assertEqual(tx.events[-1][0],h._SOURCE)
 def test_empty_still_validates_ownership_and_frozen_sources_without_fence(self):
  tx=Transaction(count=0);result,_=self.run_case(tx)
  self.assertEqual(result,[]);self.assertEqual(tx.events[-1],('source_proof',('{}',True)))
  with self.assertRaises(DatabaseServerError):self.run_case(Transaction('owner',count=0))
 def test_all_leased_zero_claim_still_checks_generation(self):
  tx=Transaction(rows=[]);result,_=self.run_case(tx);self.assertEqual(result,[]);self.assertEqual(tx.events[-1][0],'fence')
 def test_final_generation_loss_poison_prevents_commit(self):
  tx=Transaction();db=Database(tx)
  with patch.object(h,'assert_full_initial_scope'),patch.object(h,'assert_channel_scopes',side_effect=DatabaseServerError('channel_collection_scope_lost',code='P0001')):
   with self.assertRaises(DatabaseServerError):h.claim_initial_video_collection_batch(db,'job',['video'],'worker','run')
  self.assertTrue(tx.poison);self.assertEqual(db.commits,0)
 def test_full_source_failure_precedes_empty_return(self):
  tx=Transaction(count=0);db=Database(tx)
  with patch.object(h,'assert_full_initial_scope',side_effect=DatabaseServerError('full_initial_targets_not_frozen',code='P0001')):
   with self.assertRaises(DatabaseServerError):h.claim_initial_video_collection_batch(db,'job',[],'worker','run')
  self.assertTrue(tx.poison);self.assertEqual(db.commits,0)
 def test_borrowed_transaction_never_opens_another_connection(self):
  tx=Transaction();result,db=self.run_case(tx,borrow=True);self.assertEqual(len(result),1);self.assertEqual(db.operations,[])
 def test_unknown_commit_never_reclaims_or_retries(self):
  tx=Transaction();db=Database(tx,unknown=True)
  with self.assertRaises(DatabaseError) as caught:self.run_case(tx,db)
  self.assertTrue(caught.exception.outcome_unknown);self.assertEqual((len(db.operations),db.commits),(1,1))
 def test_registered_timeout_and_errors_preserved(self):
  _,db=self.run_case(Transaction());spec=h.get_operation('claim_initial_video_collection_batch');op=db.operations[0]
  self.assertEqual((op.name,op.statement_timeout_ms,op.lock_timeout_ms,op.total_timeout_ms,op.error_codes),(spec.name,spec.statement_timeout_ms,spec.lock_timeout_ms,spec.statement_timeout_ms,spec.error_codes))
 def test_native_uuid_and_timestamp_fraction_match_table_contract(self):
  tx=Transaction();tx.rows[0].update(lease_token=UUID('00000000-0000-4000-8000-000000000001'),lease_expires_at=datetime(2026,10,6,1,2,3,123450,tzinfo=timezone.utc))
  result,_=self.run_case(tx)
  self.assertEqual(result[0]['lease_token'],'00000000-0000-4000-8000-000000000001');self.assertEqual(result[0]['lease_expires_at'],'2026-10-06T01:02:03.123450+00:00')
 def test_both_statements_pass_real_driver_single_statement_guard(self):
  from db.base.postgres import _reviewed_statement
  _reviewed_statement(h._INSERT,read=False,max_rows=None)
  _reviewed_statement(h._CLAIM,read=True,max_rows=50,returning=True)
if __name__=='__main__':unittest.main()
