"""Actual owner routing through the closed driver; no database connection."""
from pathlib import Path
import re,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.reviews import video_type_review_decisions as decision,video_type_review_queries as reads
from db.retention import inventory_retention as retention
from db.inventory import manual_decision_inventory_effects as effects
from db.base.postgres import DatabaseServerError,PostgresDatabase,PostgresConfig,Operation,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from test_direct_pg_transactions import Connection,Pool,Result,DSN
CHANNEL='UC0000000000000000000001';VIDEO='aaaaaaaaaaa';CASE='86000000-0000-4000-8000-000000000001';REQUEST='86000000-0000-4000-8000-000000000002'
ARGS=dict(case_id=CASE,request_id=REQUEST,expected_revision=1,video_type='long',reason='chosen')
CANONICAL_BEFORE='UPDATE public.video_inventory SET video_type=%(video_type)s::text,updated_at=%(decided_at)s::timestamptz\n WHERE video_id=%(video_id)s::text AND video_type IS DISTINCT FROM %(video_type)s::text'
MEMBER_SCRUB_BEFORE='UPDATE public.video_inventory_memberships SET\n first_seen_at=CASE WHEN %(first_valid)s THEN first_seen_at ELSE NULL END,\n first_seen_scan_id=CASE WHEN %(first_valid)s THEN first_seen_scan_id ELSE NULL END,\n last_present_at=CASE WHEN %(positive_valid)s THEN last_present_at ELSE NULL END,\n source_published_at=CASE WHEN %(positive_valid)s THEN source_published_at ELSE NULL END\n WHERE video_id=%(video_id)s AND channel_id=%(channel_id)s AND video_type=%(video_type)s\n AND ((NOT %(first_valid)s AND (first_seen_at IS NOT NULL OR first_seen_scan_id IS NOT NULL))\n OR (NOT %(positive_valid)s AND (last_present_at IS NOT NULL OR source_published_at IS NOT NULL)))'
def one(**row):
 if isinstance(row.get('value'),dict) and set(row['value'])==reads.CASE_FIELDS:row['source_expires_at']=None
 return Result((row,))
def write(n=1):return Result(has_columns=False,rowcount=n)
class Routing(unittest.TestCase):
 def setUp(self):
  self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
  self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open();self.addCleanup(self.database.close)
 def queue(self,*rows):self.connection.results.extend(rows)
 def decision(self,result=True):
  self.queue(one(**ARGS,invalid=False),one(video_id=VIDEO,channel_id=CHANNEL,revision=1,decision_type=None),one(acquired=True),Result(),
   one(video_id=VIDEO,channel_id=CHANNEL,value='QUEUE'),one(live=False),one(video_id=VIDEO,channel_id=CHANNEL,video_type='short',effect_old='NATIVE_OLD'),
   one(video_id=VIDEO,channel_id=CHANNEL,revision=1,decision_type=None),one(blocked=False),one(review_status='needs_review'),one(epoch=17,decided_at='clock',revision=2),write(),write(),
   one(value='NATIVE_NEW') if result else Result(),one(value=dict.fromkeys(reads.CASE_FIELDS)),one(channel_id=CHANNEL,owner_permission='accepted',is_hidden=False,control_epoch=17))
 def test_decision_receipt_runs_effect_before_projection_fence_and_commit(self):
  self.decision();seen=[]
  def effect(tx,**args):
   self.assertIn(decision._CANONICAL_EFFECT,self.connection.queries[-1][0]);self.assertEqual(self.connection.commits,0);seen.append(args)
  with patch.object(effects,'after_manual_decision_inventory',side_effect=effect):value=decision.decide_video_type_review(self.database,**ARGS)
  self.assertEqual(seen,[dict(old_text='NATIVE_OLD',new_text='NATIVE_NEW')]);self.assertEqual(value['outcome'],'ready')
  self.assertIn('FOR SHARE',self.connection.queries[-1][0]);self.assertEqual(self.connection.commits,2);self.assertEqual(self.pool.borrowed,1) # savepoint release + owner commit in this recording fake
 def test_decision_zero_returning_emits_no_event_and_retains_original_no_count_gate(self):
  self.decision(False)
  with patch.object(effects,'after_manual_decision_inventory') as effect:self.assertEqual(decision.decide_video_type_review(self.database,**ARGS)['outcome'],'ready')
  effect.assert_not_called();self.assertEqual(self.connection.commits,2);self.assertEqual(self.pool.borrowed,1)
 def test_decision_busy_effect_rolls_back_savepoint_before_busy_projection(self):
  self.decision()
  with patch.object(effects,'after_manual_decision_inventory',side_effect=DatabaseServerError('busy',code='55P03')):
   value=decision.decide_video_type_review(self.database,**ARGS)
  self.assertEqual(value['outcome'],'busy');self.assertEqual(self.connection.rollbacks,1);self.assertEqual(self.connection.commits,1)
  self.assertNotIn('FOR SHARE',self.connection.queries[-1][0])
 def test_decision_effect_constraint_failure_rolls_back_whole_owner_without_retry(self):
  self.decision()
  with patch.object(effects,'after_manual_decision_inventory',side_effect=DatabaseServerError('constraint',code='23514')):
   with self.assertRaises(DatabaseServerError):decision.decide_video_type_review(self.database,**ARGS)
  self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
 def member_prefix(self,source_valid,returned=True):
  # RR14 uses one native preflight; later ownership locks/effect receipts stay exact.
  self.queue(one(noop=False),one(acquired=True),one(channel_id=CHANNEL),Result(),Result(),one(channel_id=CHANNEL,value='LOCKED_INV'),one(at='fresh_clock'),Result(),one(value='LOCKED_MEMBER'),one(source_valid=source_valid,first_valid=False,positive_valid=True),one(value='ACTUAL_NEW_OR_DELETE') if returned else Result())
 def run_member(self):
  with self.database.transaction(Operation('retention_owner')) as tx:return retention._candidate(tx,dict(video_id=VIDEO,channel_id=CHANNEL,video_type='long',cutoff='cutoff'),'memberships')
 def test_retention_delete_passes_actual_deleted_row_at_original_statement(self):
  self.member_prefix(False)
  with patch.object(retention,'after_membership_source_write') as effect:value=self.run_member()
  self.assertEqual(value,(1,0,0,True));self.assertEqual(effect.call_args.kwargs,dict(operation='delete',old_text='ACTUAL_NEW_OR_DELETE'));self.assertEqual(self.connection.commits,1)
 def test_retention_scrub_uses_locked_old_and_actual_new(self):
  self.member_prefix(True)
  with patch.object(retention,'after_membership_source_write') as effect:value=self.run_member()
  self.assertEqual(value,(0,1,0,True));self.assertEqual(effect.call_args.kwargs,dict(operation='update',old_text='LOCKED_MEMBER',new_text='ACTUAL_NEW_OR_DELETE'))
 def test_retention_zero_row_scrub_emits_no_event(self):
  self.member_prefix(True,False)
  with patch.object(retention,'after_membership_source_write') as effect:value=self.run_member()
  self.assertEqual(value,(0,0,0,True));effect.assert_not_called()
 def test_retention_synthetic_effect_failure_does_not_commit_prior_mutation(self):
  self.member_prefix(True)
  with patch.object(retention,'after_membership_source_write',side_effect=DatabaseServerError('scope',code='23514')):
   with self.assertRaises(DatabaseServerError):self.run_member()
  self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
class Source(unittest.TestCase):
 def test_new_sql_is_admitted_and_bound_by_actual_driver(self):
  for query,returning in ((decision._INVENTORY,False),(decision._CANONICAL_EFFECT,True),(retention._MEMBER_DELETE_EFFECT,True),(retention._MEMBER_SCRUB_EFFECT,True),(retention._WITHDRAW_TYPE_EFFECT,True)):
   names=re.findall(r'%\(([^)]+)\)s',query);params={name:None for name in names} if names else (None,)*query.count('%s')
   sql=_reviewed_statement(query,read=True,max_rows=1,returning=returning);PostgresQuery(Transformer()).convert(sql.as_string(),params)
 def test_original_targets_casts_and_predicates_are_exact_prefixes(self):
  # R04B additionally clears the identity-only marker on confirmed restoration.
  self.assertEqual(decision._CANONICAL_EFFECT.split(' RETURNING',1)[0],CANONICAL_BEFORE.replace(',updated_at=',',skeleton_since=NULL,updated_at='))
  self.assertEqual(retention._MEMBER_SCRUB_EFFECT.split(' RETURNING',1)[0],MEMBER_SCRUB_BEFORE)
  self.assertNotIn('updated_at=',retention._WITHDRAW_TYPE_EFFECT)
 def test_expiry_preserves_identity_queue_and_decision_after_child_proof(self):
  source=(ROOT/'application/db/retention/inventory_retention.py').read_text();body=source.split('    if _fact(tx,p,_FRESH)',1)[1]
  self.assertLess(body.index('_CHILDREN'),body.index('_RECOVERY_QUEUE'))
  self.assertLess(body.index('DELETE FROM public.videos'),body.index('_RECOVERY_QUEUE'))
  self.assertNotIn('detach_review_source',source)
  self.assertNotIn('DELETE FROM public.video_inventory',body)
  self.assertNotIn('DELETE FROM public.video_collection_state',body)
 def test_latent_unreferenced_writer_methods_are_retired(self):
  from db.inventory.inventory_maintenance import InventoryMaintenanceRepository
  for name in ('upsert_inventory','repair_untyped_inventory'):self.assertFalse(hasattr(InventoryMaintenanceRepository,name))
  for path in (ROOT/'application').rglob('*.py'):
   for name in ('upsert_inventory','repair_untyped_inventory'):self.assertNotIn('.'+name+'(',path.read_text())
if __name__=='__main__':unittest.main()
