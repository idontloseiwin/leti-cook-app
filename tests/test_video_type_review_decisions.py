"""Manual review native casts, bounded reads, branch order and atomic recovery."""
from channel_policy_fixtures import native_policy_facts, native_eligible_facts, native_pause_facts
from pathlib import Path
import sys,unittest
from unittest.mock import patch
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.reviews import video_type_review_decisions as decision,video_type_review_queries as reads
from db.inventory import manual_decision_inventory_effects as effects
from db.base.postgres import DatabaseError,DatabaseServerError,Operation,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
CASE='f8900000-0000-4000-8000-000000000001'
REQUEST='f8900000-0000-4000-8000-000000000002'
CHANNEL='UC0000000000000000000001';VIDEO='rv000000001'
ARGS=dict(case_id=CASE,request_id=REQUEST,expected_revision=1,video_type='long',reason='chosen')
def one(**row):
 if isinstance(row.get('value'),dict) and set(row['value'])==reads.CASE_FIELDS:row['source_expires_at']=None
 return Result((row,))
def write():return Result(has_columns=False,rowcount=1)
def projection():return dict.fromkeys(reads.CASE_FIELDS)

class DirectTests(unittest.TestCase):
 def test_type_decision_preserves_existing_episode_and_separate_counter_deadline(self):
  self.assertIn('s.availability_first_missing_at IS NOT NULL',decision._QUEUE_UPDATE)
  self.assertIn("s.paused_reason='availability_retry_expired'",decision._QUEUE_UPDATE)
  self.assertIn('THEN s.next_due_at',decision._QUEUE_UPDATE)
  self.assertIn('THEN s.paused_reason',decision._QUEUE_UPDATE)
  self.assertNotIn('stats_due_at=',decision._QUEUE_UPDATE)
  self.assertNotIn('availability_first_missing_at=',decision._QUEUE_UPDATE)
  self.assertNotIn('availability_resume_observed_at=',decision._QUEUE_UPDATE)
 def setUp(self):
  self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
  self.database=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
  self.addCleanup(self.database.close)
  def captured_effect(tx,*,old_text,new_text):
   # Owner-boundary test: helper semantics and honest total budget have separate coverage.
   self.assertIs(tx._connection,self.connection)
   self.assertEqual((old_text,new_text),('NATIVE_OLD','NATIVE_NEW'))
   self.assertIn(decision._CANONICAL_EFFECT,self.connection.queries[-1][0])
  effect=patch.object(effects,'after_manual_decision_inventory',side_effect=captured_effect)
  self.effect=effect.start();self.addCleanup(effect.stop)
 def queue(self,*rows):self.connection.results.extend(rows)
 def call(self,**kw):return decision.decide_video_type_review(self.database,**{**ARGS,**kw})
 def start(self,video=VIDEO,receipt=None,acquired=True):
  self.queue(one(**ARGS,invalid=False),one(video_id=video,channel_id=CHANNEL,revision=1,decision_type=None))
  if video is not None:self.queue(one(acquired=acquired))
  if acquired:self.queue(Result() if receipt is None else one(**receipt))
 def held(self,canonical='long',prior=None,revision=1,blocked=False):
  self.canonical=canonical;self.prior=prior
  self.queue(one(video_id=VIDEO,channel_id=CHANNEL,value='{}'),one(live=False),
    one(video_id=VIDEO,channel_id=CHANNEL,video_type=canonical,effect_old='NATIVE_OLD'),
    one(video_id=VIDEO,channel_id=CHANNEL,revision=revision,decision_type=prior),one(blocked=blocked))
 def writes(self,manual=False,typed=False):
  if self.prior is None:self.queue(one(review_status='needs_review'))
  self.queue(one(epoch=17,decided_at='2026-10-06 00:00:00+00',revision=2),write(),write(),
   one(value='NATIVE_NEW') if self.canonical!=ARGS['video_type'] else Result())
  if manual:
   self.queue(one(present=True),one(video_id=VIDEO,channel_id=CHANNEL,value='{}'),one(live=False),
      one(published_at=None,published_acquired_at=None,stream_end=None,stream_end_acquired_at=None,has_current=True,availability_status='public'))
   if typed:self.queue(one(eligible=True))
   else:self.queue(one(open_incident=False,policy_facts=native_pause_facts(None)))
   self.queue(write(),write())
  self.queue(one(value=projection()),one(channel_id=CHANNEL,owner_permission='accepted',is_hidden=False,control_epoch=17))
 def test_changed_type_writes_case_history_inventory_then_final_scope_without_double_queue_effect(self):
  self.start();self.held(canonical='short');self.writes()
  result=self.call();sql=[q for q,p in self.connection.queries]
  self.assertEqual(result['mutation'],dict(request_id=REQUEST,replayed=False,applied_revision=2))
  self.assertEqual([q for q in sql if q.startswith(('UPDATE','INSERT')) or decision._CANONICAL_EFFECT in q],
   [decision._DECISION,decision._HISTORY,_reviewed_statement(decision._CANONICAL_EFFECT,read=True,returning=True,max_rows=1).as_string()])
  self.effect.assert_called_once()
  self.assertEqual(len([q for q in sql if 'FOR UPDATE' in q]),3)
  for actual,expected in zip([q for q in sql if 'FOR UPDATE' in q],(decision._QUEUE,decision._INVENTORY,decision._CASE_LOCK)):self.assertIn(expected,actual)
  self.assertIn('FOR SHARE',sql[-1]);self.assertEqual(len(sql),16);self.assertEqual(self.pool.borrowed,1)
 def test_new_decision_waits_for_automatic_recheck_without_source_writes(self):
  for status in ('awaiting_refresh','confirmed','channel_conflict','history'):
   self.start();self.held();self.queue(one(review_status=status),one(value=projection()))
   start=len(self.connection.queries)
   self.assertEqual(self.call()['outcome'],'source_unavailable')
   self.assertFalse(any(q.startswith(('UPDATE','INSERT')) for q,p in self.connection.queries[start:]))
 def test_replay_remains_before_current_evidence_eligibility(self):
  self.start(receipt=dict(revision=2,conflict=False));self.queue(one(value=projection()))
  result=self.call();self.assertTrue(result['mutation']['replayed'])
  self.assertNotIn(decision._ELIGIBILITY,[q for q,p in self.connection.queries])
 def test_null_to_type_routes_actual_event_to_borrowed_effect_owner_once(self):
  self.start();self.held(canonical=None);self.writes();self.call();self.effect.assert_called_once()
  self.assertNotIn(decision._QUEUE_INSERT,[q for q,p in self.connection.queries])
 def test_first_same_type_invokes_borrowed_manual_queue_once_and_has_finite_budget(self):
  self.start();self.held();self.writes(manual=True);self.call()
  sql=[q for q,p in self.connection.queries]
  self.assertEqual(sql.count(decision._QUEUE_INSERT),1);self.assertEqual(sql.count(decision._QUEUE_UPDATE),1)
  self.assertEqual(len(sql),23);self.assertEqual(self.pool.borrowed,1);self.effect.assert_not_called()
 def test_reason_only_same_type_keeps_queue(self):
  self.start();self.held(prior='long');self.writes();self.call();self.effect.assert_not_called()
  self.assertNotIn(decision._QUEUE_UPDATE,[q for q,p in self.connection.queries])
 def test_native_cast_failure_precedes_domain_or_database_reads(self):
  self.queue(Result(execute_error=ServerFailure('22P02')))
  with self.assertRaises(DatabaseError) as e:self.call(case_id=None,request_id='bad')
  self.assertEqual(e.exception.code,'22P02');self.assertEqual(len(self.connection.queries),1)
 def test_domain_error_preserves_sqlstate_and_does_not_read_case(self):
  self.queue(one(**ARGS,invalid=True))
  with self.assertRaisesRegex(DatabaseError,'invalid_video_type_review_decision') as e:self.call()
  self.assertEqual(e.exception.code,'22023');self.assertEqual(len(self.connection.queries),1)
 def test_missing_case_returns_null_without_locks(self):
  self.queue(one(**ARGS,invalid=False),Result())
  self.assertEqual(self.call(),dict(outcome='not_found',case=None,mutation=None))
  self.assertEqual(len(self.connection.queries),2)
 def test_advisory_contention_precedes_receipt(self):
  self.start(acquired=False);self.queue(one(value=projection()))
  self.assertEqual(self.call()['outcome'],'busy')
  self.assertNotIn(decision._RECEIPT,[q for q,p in self.connection.queries])
 def test_receipt_replays_even_detached_before_queue_or_scope(self):
  self.start(video=None,receipt=dict(revision=9,conflict=False));self.queue(one(value=projection()))
  result=self.call()
  self.assertEqual(result['mutation'],dict(request_id=REQUEST,replayed=True,applied_revision=9))
  self.assertEqual(len(self.connection.queries),4)
 def test_reused_request_conflict_precedes_detached_source(self):
  self.start(video=None,receipt=dict(revision=9,conflict=True));self.queue(one(value=projection()))
  self.assertEqual(self.call()['outcome'],'request_conflict')
 def test_detached_without_receipt_returns_source_unavailable(self):
  self.start(video=None);self.queue(one(value=projection()))
  self.assertEqual(self.call()['outcome'],'source_unavailable')
 def test_active_queue_returns_busy_without_inventory_locks(self):
  self.start();self.queue(one(video_id=VIDEO,channel_id=CHANNEL,value='{}'),one(live=True),one(value=projection()))
  self.assertEqual(self.call()['outcome'],'busy')
  self.assertNotIn(decision._INVENTORY,[q for q,p in self.connection.queries])
 def test_each_nowait_contention_rolls_back_savepoint_before_busy_projection(self):
  for prefix in ([],[one(video_id=VIDEO,channel_id=CHANNEL,value='{}'),one(live=False)],
                 [one(video_id=VIDEO,channel_id=CHANNEL,value='{}'),one(live=False),one(video_id=VIDEO,channel_id=CHANNEL,video_type='long')]):
   self.start();self.queue(*prefix,Result(execute_error=ServerFailure('55P03')),one(value=projection()))
   before=self.connection.rollbacks;self.assertEqual(self.call()['outcome'],'busy')
   self.assertEqual(self.connection.rollbacks,before+1)
 def test_cross_channel_conflict_precedes_revision(self):
  self.start();self.held(revision=8,blocked=True);self.queue(one(value=projection()))
  self.assertEqual(self.call()['outcome'],'blocked')
 def test_revision_mismatch_performs_no_writes(self):
  self.start();self.held(revision=8);self.queue(one(value=projection()))
  self.assertEqual(self.call()['outcome'],'changed')
  self.assertFalse(any(q.startswith(('INSERT','UPDATE')) for q,p in self.connection.queries))
 def test_limit_trigger_error_rolls_back_all_and_is_not_busy(self):
  self.start();self.held();self.queue(one(review_status='needs_review'),one(epoch=17,decided_at='fixed',revision=2),write(),
   Result(execute_error=ServerFailure('P0001','user_work_limit_reached')))
  with self.assertRaisesRegex(DatabaseError,'user_work_limit_reached'):self.call()
  self.assertEqual(self.connection.commits,0)
 def test_late_scope_error_is_never_converted_to_busy(self):
  self.start();self.held(canonical='short');self.writes()
  with patch.object(decision,'assert_channel_scope',side_effect=DatabaseServerError('scope',code='55P03')):
   with self.assertRaises(DatabaseError) as e:self.call()
  self.assertEqual(e.exception.code,'55P03');self.assertEqual(self.connection.rollbacks,1)
 def test_unknown_error_is_not_busy_or_retried(self):
  self.start();self.queue(Result(execute_error=psycopg.OperationalError('private')))
  with self.assertRaises(DatabaseError) as e:self.call()
  self.assertTrue(e.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
 def test_borrowed_validation_failure_poisons_enclosing_transaction(self):
  self.queue(write(),one(**ARGS,invalid=True))
  with self.assertRaisesRegex(DatabaseError,'database_transaction_aborted'):
   with self.database.transaction(Operation('manual_owner')) as tx:
    tx.execute('DELETE FROM public.folders')
    try:decision.decide_video_type_review(None,**ARGS,transaction=tx)
    except DatabaseServerError:pass
  self.assertEqual(self.connection.commits,0)
 def test_readers_are_bounded_and_return_plain_native_json(self):
  case=projection();history=dict.fromkeys(reads.HISTORY_FIELDS)
  self.queue(one(value=case));self.assertEqual(reads.get_video_type_review_case(self.database,CASE),dict(case,source_expires_at=None))
  self.queue(one(state='all',page_limit=2,after_id=None),Result((dict(value=case,source_expires_at=None),dict(value=case,source_expires_at=None))))
  self.assertEqual(len(reads.list_video_type_review_cases(self.database,'all',2)),2)
  self.queue(one(case_id=CASE,page_limit=1,before_revision=None),one(value=history))
  self.assertEqual(reads.list_video_type_review_history(self.database,CASE,1),[history])
  self.queue(one(value=True));self.assertTrue(reads.has_open_video_type_reviews(self.database))
  self.assertEqual(len(self.connection.queries),6)
  self.assertIn(3,self.connection.fetch_sizes)
 def test_invalid_page_casts_first_then_domain_gate(self):
  self.queue(Result(execute_error=ServerFailure('22P02')))
  with self.assertRaises(DatabaseError) as e:reads.list_video_type_review_cases(self.database,'invalid',0,'bad')
  self.assertEqual(e.exception.code,'22P02')
  for state,limit in ((None,1),('invalid',1),('all',None),('all',0),('all',102)):
   self.queue(one(state=state,page_limit=limit,after_id=None))
   with self.assertRaises(DatabaseError) as e:reads.list_video_type_review_cases(self.database,state,limit)
   self.assertEqual(e.exception.code,'22023')
 def test_history_page_native_bigint_then_domain_and_null_case_empty(self):
  for limit,before in ((None,None),(0,None),(102,None),(1,0)):
   self.queue(one(case_id=None,page_limit=limit,before_revision=before))
   with self.assertRaises(DatabaseError) as e:reads.list_video_type_review_history(self.database,None,limit,before)
   self.assertEqual(e.exception.code,'22023')
  self.queue(one(case_id=None,page_limit=101,before_revision=None),Result())
  self.assertEqual(reads.list_video_type_review_history(self.database,None,101),[])
 def test_readers_reject_malformed_projection_and_excess_cardinality(self):
  self.queue(one(value={}))
  with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):reads.get_video_type_review_case(self.database,CASE)
  self.queue(one(value=1))
  with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):reads.has_open_video_type_reviews(self.database)
  self.queue(one(state='all',page_limit=1,after_id=None),Result((dict(value=projection()),dict(value=projection()))))
  with self.assertRaises(DatabaseError):reads.list_video_type_review_cases(self.database,'all',1)
 def test_fixed_sql_is_bound_and_no_business_rpc_or_clock_emulator(self):
  args={**ARGS,'video_id':VIDEO,'channel_id':CHANNEL,'revision':2,'decided_at':'fixed','pause':None,'has_current':True,'published_at':None,'published_acquired_at':None,'stream_end':None,'stream_end_acquired_at':None}
  for module in (reads,decision):
   for name,text in vars(module).items():
    if name.startswith('_') and isinstance(text,str) and text.startswith(('SELECT','WITH','UPDATE','INSERT')):
     returning=module is decision and name=='_CANONICAL_EFFECT'
     read=returning or not text.startswith(('UPDATE','INSERT'));_reviewed_statement(text,read=read,returning=returning,max_rows=101 if name in ('_LIST','_HISTORY') and module is reads else 1 if read else 0)
     params=args if '%(' in text else (None,)*text.count('%s')
     if module is reads:params={**args,'state':'all','page_limit':2,'after_id':None,'before_revision':None} if '%(' in text else params
     PostgresQuery(Transformer()).convert(text,params)
  for path in ('video_type_review_decisions.py','video_type_review_queries.py'):
   text=(ROOT/'application/db/reviews'/path).read_text()
   for forbidden in ('database.call(', 'public.decide_video_type_review(', 'public.apply_manual_video_type_queue(', 'public.get_video_type_review_case(', 'datetime.now(', 'time.sleep('):
    self.assertNotIn(forbidden,text)

if __name__=='__main__':unittest.main()
