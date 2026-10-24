"""Bounded manual decisions include the current-evidence read before writes."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
import sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tools'),str(ROOT/'tests')]
from db.inventory import manual_decision_inventory_effects as effect
from db.reviews import video_type_review_decisions as decision
from db.base import postgres as pg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_direct_pg_transactions import Connection,Pool,Result,DSN
from test_inventory_owner_effect_routing import ARGS,VIDEO,CHANNEL,one,write

class Counted:
 """Count domain client executions, excluding driver configuration and controls."""
 def __init__(self,probe,budget):self.probe=probe;self.budget=budget
 @contextmanager
 def transaction(self,operation):
  count=0
  def mark():
   nonlocal count
   count+=1
   if count>self.budget:raise AssertionError('review_statement_budget')
  with self.probe.database.transaction(operation) as tx:
   class Borrowed:
    def __getattr__(self,name):return getattr(tx,name)
    def fetch_one(self,sql,params=()):mark();return tx.fetch_one(sql,params)
    def fetch_all(self,sql,params=(),*,max_rows):mark();return tx.fetch_all(sql,params,max_rows=max_rows)
    def execute(self,sql,params=()):mark();return tx.execute(sql,params)
    def returning_one(self,sql,params=()):mark();return tx.returning_one(sql,params)
    def returning_all(self,sql,params=(),*,max_rows):mark();return tx.returning_all(sql,params,max_rows=max_rows)
   try:yield Borrowed()
   finally:
    self.probe.max_domain_statements=max(getattr(self.probe,'max_domain_statements',0),count)

class Tx:
 def __init__(self,restore=False,count=1):self.count=count;self.writes=[];self.facts=dict(video_id=VIDEO,channel_id=CHANNEL,video_type='long',published_at='native-clock',published_acquired_at='source-clock',restore_type=restore,exact=True,review_skip=True)
 @contextmanager
 def borrow(self):yield self
 def fetch_one(self,sql,args):return dict(self.facts)
 def execute(self,sql,args):self.writes.append((sql,args));return self.count
class Effects(unittest.TestCase):
 def test_original_native_assignment_and_real_driver_binding(self):
  self.assertIs(effect._RESTORE_TYPE,__import__('db.inventory.inventory_incident_queue_effects',fromlist=['_RESTORE_TYPE'])._RESTORE_TYPE)
  wrapped=pg._reviewed_statement(effect._FACTS,read=True,max_rows=1);PostgresQuery(Transformer()).convert(wrapped.as_string(),('null','null'))
 def test_changed_nonnull_uses_original_manual_owner_once(self):
  tx=Tx()
  with patch.object(effect,'_apply_manual_video_type_queue_after_decision') as apply:effect.after_manual_decision_inventory(tx,old_text='old',new_text='new')
  apply.assert_called_once_with(tx,dict(video_id=VIDEO,channel_id=CHANNEL,video_type='long'));self.assertEqual(tx.writes,[])
 def test_native_restore_receipt_short_circuits_manual_even_if_case_has_decision(self):
  tx=Tx(True)
  with patch.object(effect,'_apply_manual_video_type_queue_after_decision') as apply:effect.after_manual_decision_inventory(tx,old_text='old',new_text='new')
  apply.assert_not_called();self.assertEqual(len(tx.writes),1)
 def test_missing_restore_falls_through_manual(self):
  tx=Tx(True,0)
  with patch.object(effect,'_apply_manual_video_type_queue_after_decision') as apply:effect.after_manual_decision_inventory(tx,old_text='old',new_text='new')
  self.assertEqual(apply.call_count,1)
 def test_unproved_review_skip_or_changed_identity_fails_before_writes(self):
  for key in ('exact','review_skip'):
   tx=Tx();tx.facts[key]=False
   with self.assertRaises(pg.DatabaseError):effect.after_manual_decision_inventory(tx,old_text='old',new_text='new')
   self.assertEqual(tx.writes,[])
 def test_native_failure_and_restore_shape_preserved(self):
  for count in (True,2,-1):
   with self.assertRaises(pg.DatabaseError):effect.after_manual_decision_inventory(Tx(True,count),old_text='old',new_text='new')
  with patch.object(effect,'_apply_manual_video_type_queue_after_decision',side_effect=pg.DatabaseServerError('busy',code='55P03')):
   with self.assertRaises(pg.DatabaseServerError):effect.after_manual_decision_inventory(Tx(),old_text='old',new_text='new')
 def test_invalid_native_text_is_rejected_before_borrow(self):
  for old,new in [(None,'new'),('old',None),(1,'new'),('old',{})]:
   tx=Mock()
   with self.assertRaises(ValueError):effect.after_manual_decision_inventory(tx,old_text=old,new_text=new)
   tx.borrow.assert_not_called()
 def test_missing_or_malformed_case_proof_is_rejected_before_mutation(self):
  for value in [False,None,'true',1]:
   tx=Tx(True);tx.facts['review_skip']=value
   with patch.object(effect,'_apply_manual_video_type_queue_after_decision') as apply:
    with self.assertRaises(pg.DatabaseError):effect.after_manual_decision_inventory(tx,old_text='old',new_text='new')
   apply.assert_not_called();self.assertEqual(tx.writes,[])
 def test_original_manual_wrapper_keeps_decision_gate(self):
  for admitted in (False,True):
   tx=Tx();tx.fetch_one=Mock(return_value={'present':admitted})
   with patch.object(decision,'_apply_manual_video_type_queue_after_decision') as tail:
    if admitted:decision.apply_manual_video_type_queue(tx,video_id=VIDEO,channel_id=CHANNEL,video_type='long')
    else:
     with self.assertRaises(pg.DatabaseServerError) as caught:decision.apply_manual_video_type_queue(tx,video_id=VIDEO,channel_id=CHANNEL,video_type='long')
     self.assertEqual(caught.exception.message,'manual_video_type_queue_decision_missing')
    self.assertEqual(tail.call_count,int(admitted));tx.fetch_one.assert_called_once_with(decision._QUEUE_DECISION,dict(video_id=VIDEO,channel_id=CHANNEL,video_type='long'))
 def test_actual_decision_route_fits_23_statements_including_evidence_check(self):
  from db.reviews import video_type_review_queries as reads
  for old_type,initial_queue,restore_count in [('short',True,None),(None,True,1),(None,False,0)]:
   events=[];conn=Connection(events);pool=Pool(conn,events)
   db=pg.PostgresDatabase(pg.PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:pool).open()
   def q(*results):conn.results.extend(results)
   q(one(**ARGS,invalid=False),one(video_id=VIDEO,channel_id=CHANNEL,revision=1,decision_type=None),one(acquired=True),Result())
   q(one(video_id=VIDEO,channel_id=CHANNEL,value='QUEUE') if initial_queue else Result())
   if initial_queue:q(one(live=False))
   q(one(video_id=VIDEO,channel_id=CHANNEL,video_type=old_type,effect_old='NATIVE_OLD'),one(video_id=VIDEO,channel_id=CHANNEL,revision=1,decision_type=None),one(blocked=False),one(review_status='needs_review'),one(epoch=17,decided_at='clock',revision=2),write(),write(),one(value='NATIVE_NEW'))
   q(one(video_id=VIDEO,channel_id=CHANNEL,video_type='long',published_at=None,published_acquired_at=None,restore_type=old_type is None,exact=True,review_skip=True))
   if old_type is None:q(write(restore_count))
   if restore_count!=1:
    q(one(video_id=VIDEO,channel_id=CHANNEL,value='QUEUE'),one(live=False),one(published_at=None,published_acquired_at=None,stream_end=None,stream_end_acquired_at=None,has_current=False,availability_status=None),one(policy_facts={},open_incident=False),write(),write())
   q(one(value=dict.fromkeys(reads.CASE_FIELDS)),one(channel_id=CHANNEL,owner_permission='accepted',is_hidden=False,control_epoch=17))
   probe=SimpleNamespace(database=db,max_domain_statements=0)
   try:
    with patch.object(decision,'channel_pause_row',return_value=dict(pause=None,open_incident=False)):
     answer=decision.decide_video_type_review(Counted(probe,23),**ARGS)
    self.assertEqual(answer['outcome'],'ready');self.assertLessEqual(probe.max_domain_statements,23)
    sql=[query for query,_ in conn.queries]
    eligibility=pg._reviewed_statement(decision._ELIGIBILITY,read=True,max_rows=1).as_string()
    self.assertEqual(sql.count(eligibility),1)
    self.assertLess(sql.index(eligibility),sql.index(decision._DECISION))
    if restore_count!=1:self.assertEqual(probe.max_domain_statements,23)
    self.assertEqual(len(conn.results),0);self.assertEqual(len(conn.queries),probe.max_domain_statements);self.assertEqual(pool.borrowed,1);self.assertEqual(conn.rollbacks,0)
   finally:db.close()
if __name__=='__main__':unittest.main()
