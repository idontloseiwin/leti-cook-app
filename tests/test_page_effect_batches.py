"""Finite batch branches, actual SQL admission and native expression inverses."""
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import inspect,re,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.inventory import page_effect_batches as page,review_effect_batches as review
from db.base import postgres as pg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

class Tx:
 def __init__(self,answers=()):self.answers=list(answers);self.calls=[];self.failed=False
 @contextmanager
 def borrow(self):
  try:yield self
  except BaseException:self.failed=True;raise
 def run(self,kind,q,args,max_rows=None):
  self.calls.append((kind,q,args,max_rows))
  if q==page._RESUME_RETURNED:return 0
  if q==page._QUEUE_LOCK and not self.answers:return []
  if not self.answers:raise AssertionError('unexpected SQL')
  answer=self.answers.pop(0)
  if isinstance(answer,BaseException):raise answer
  return answer
 def fetch_all(self,q,args,max_rows):return self.run('read',q,args,max_rows)
 def returning_all(self,q,args,max_rows):return self.run('returning',q,args,max_rows)
 def execute(self,q,args):return self.run('write',q,args)

def fact(video,index=1,**extra):
 return dict(ordinality=index,video_id=video,channel_id='c',video_type='short',types=['short'],channels=['c'],blocked=None,present=False,evidence_expires_at='2026-10-15T00:00:00+00:00',**extra)
def member(video,index=1,*,insert=True,withdrawal=False,eligible=False):
 return dict(ordinality=index,operation='insert' if insert else 'update',video_id=video,old_video_id=None if insert else video,withdrawal=withdrawal,identity_changed=insert,eligible=eligible)
def event(insert=True):return dict(operation='insert' if insert else 'update',old_text=None if insert else 'NATIVE_OLD',new_text='NATIVE_NEW')
def restored(video,index=1,*,manual=False,skip=False,kind='short'):
 return dict(ordinality=index,video_id=video,channel_id='c',video_type=kind,published_at='native_clock',published_acquired_at='source_clock',exact=True,manual=manual,review_skip=skip)
def receipt(video):return dict(video_id=video,old_text='NATIVE_OLD',new_text='NATIVE_NEW')

class Reviews(unittest.TestCase):
 def test_empty_and_invalid_have_no_io(self):
  for fn in (review.refresh_review_cases_batch,review.withdraw_review_cases_batch):
   tx=Tx();fn(tx,[]);self.assertEqual(tx.calls,[])
   for bad in (None,(),['v']*2,[str(n) for n in range(51)],[True]):
    with self.assertRaises(ValueError):fn(tx,bad)
   self.assertEqual(tx.calls,[])
 def test_one_and_fifty_constant_reads_without_case(self):
  for n in (1,50):
   ids=[str(i) for i in range(n)];tx=Tx([[{}]*n,[fact(v,i) for i,v in enumerate(ids,1)]])
   review.refresh_review_cases_batch(tx,ids);self.assertEqual(len(tx.calls),2);self.assertEqual([c[3] for c in tx.calls],[n,n])
 def test_conflict_and_existing_case_exact_write_set(self):
  rows=[fact(str(i),i+1) for i in range(5)]
  rows[0]['types']=['long','short'];rows[1]['blocked']='channel_conflict';rows[2]['present']=True;rows[3]['video_type']=None
  tx=Tx([[{}]*5,rows,4]);review.refresh_review_cases_batch(tx,[str(i) for i in range(5)])
  plans=tx.calls[-1][2][0].obj;self.assertEqual([r['video_id'] for r in plans],['0','1','2','3']);self.assertTrue(all(r['removed'] is False for r in plans));self.assertEqual(len(tx.calls),3)
 def test_missing_inventory_is_noop_after_lock(self):
  tx=Tx([[{}],[]]);review.refresh_review_cases_batch(tx,['gone']);self.assertEqual(len(tx.calls),2)
 def test_duplicate_facts_or_wrong_write_count_fails_borrow(self):
  row=fact('v');row['present']=True
  for answers in ([[{}],[row,row]],[[{}],[row],0],[[{}],[row],True]):
   tx=Tx(answers)
   with self.assertRaises(pg.DatabaseError):review.refresh_review_cases_batch(tx,['v'])
   self.assertTrue(tx.failed)
 def test_withdrawal_native_current_revision_and_count(self):
  tx=Tx([[{}]*50,49]);review.withdraw_review_cases_batch(tx,[str(i) for i in range(50)]);self.assertEqual(len(tx.calls),2)
  self.assertIn('revision=c.revision+',review._WITHDRAW);self.assertNotIn('last_seen_at=',review._WITHDRAW)

class Page(unittest.TestCase):
 def setUp(self):
  automatic=patch('db.inventory.automatic_type_reconciliation.reconcile_automatic_video_types',return_value=[])
  typed=patch.object(page,'ensure_typed_collection_state_in_transaction',return_value=0)
  self.automatic=automatic.start();self.typed=typed.start()
  self.addCleanup(automatic.stop);self.addCleanup(typed.stop)
 def test_new_long_initializer_then_review_including_typed_and_profiles(self):
  tx=Tx([[dict(ordinality=1,video_id='a',exact=True,video_type='long'),dict(ordinality=2,video_id='b',exact=True,video_type='short')]]);order=[]
  with patch.object(page,'_ordinary_long',side_effect=lambda t,ids:order.append(('queue',ids))),patch.object(page,'refresh_review_cases_batch',side_effect=lambda t,ids:order.append(('review',ids))):
   page.after_page_inventory_inserts(tx,[dict(video_id=v,effect_new='native') for v in ('a','b')])
  self.assertEqual(order,[('queue',['a']),('review',['a','b'])]);self.assertEqual(len(tx.calls),1)
 def test_new_receipt_duplicates_fail_before_sql(self):
  tx=Tx()
  with self.assertRaises(pg.DatabaseError):page.after_page_inventory_inserts(tx,[dict(video_id='a',effect_new='native')]*2)
  self.assertFalse(tx.calls)
 def test_fifty_insert_memberships_have_one_facts_and_one_review_dispatch(self):
  ids=[str(i) for i in range(50)];tx=Tx([[member(v,i) for i,v in enumerate(ids,1)]])
  with patch.object(page,'refresh_review_cases_batch') as refresh,patch.object(page,'withdraw_review_cases_batch') as withdraw:
   page.after_page_membership_changes(tx,[event() for _ in ids])
  refresh.assert_called_once_with(tx,ids);withdraw.assert_called_once_with(tx,[]);self.assertEqual(len(tx.calls),3)
  self.assertEqual(tx.calls[-1][1],page._RESUME_RETURNED)
 def test_withdrawal_never_restores_or_runs_normal_review_for_that_id(self):
  tx=Tx([[member('w',insert=False,withdrawal=True,eligible=None),member('n',2,insert=False)]])
  with patch.object(page,'refresh_review_cases_batch') as refresh,patch.object(page,'withdraw_review_cases_batch') as withdraw:
   page.after_page_membership_changes(tx,[event(False),event(False)])
  withdraw.assert_called_once_with(tx,['w']);refresh.assert_called_once_with(tx,['n']);self.assertEqual(len(tx.calls),3)
 def test_live_restoration_excluded_after_queue_lock(self):
  tx=Tx([[member('v',eligible=True)],[dict(video_id='v',value='QUEUE')],[dict(video_id='v',live=True)]])
  with patch.object(page,'refresh_review_cases_batch') as refresh:page.after_page_membership_changes(tx,[event()])
  self.assertEqual([c[1] for c in tx.calls],[page._MEMBER_FACTS,page._QUEUE_LOCK,page._RESTORE_LIVE,page._QUEUE_LOCK,page._RESUME_RETURNED]);refresh.assert_called_once_with(tx,['v'])
 def test_actual_returned_restores_keep_source_order_and_nested_before_outer(self):
  tx=Tx([[member('b',eligible=True),member('a',2,eligible=True)],[],[dict(video_id=v,live=False) for v in ('b','a')],[receipt('a'),receipt('b')]])
  order=[]
  with patch.object(page,'_restored_inventory',side_effect=lambda t,rows:order.append(('nested',[r['video_id'] for r in rows]))),patch.object(page,'refresh_review_cases_batch',side_effect=lambda t,ids:order.append(('outer',ids))):page.after_page_membership_changes(tx,[event(),event()])
  self.assertEqual(order,[('nested',['b','a']),('outer',['b','a'])])
 def test_raced_unmatched_restore_emits_only_outer_review(self):
  tx=Tx([[member('v',eligible=True)],[],[dict(video_id='v',live=False)],[]])
  with patch.object(page,'_restored_inventory') as nested,patch.object(page,'refresh_review_cases_batch') as outer:page.after_page_membership_changes(tx,[event()])
  nested.assert_not_called();outer.assert_called_once_with(tx,['v'])
 def test_restore_receipt_preserves_early_queue_return_but_not_review(self):
  tx=Tx([[restored('v',manual=True)],[dict(video_id='v')]])
  with patch.object(page,'_manual_queues') as manual,patch.object(page,'_ordinary_long') as ordinary,patch.object(page,'refresh_review_cases_batch') as review_call:page._restored_inventory(tx,[receipt('v')])
  manual.assert_called_once_with(tx,[]);ordinary.assert_called_once_with(tx,[]);review_call.assert_called_once_with(tx,['v']);self.assertEqual(len(tx.calls),2)
 def test_manual_and_ordinary_restore_sets_and_review_skip(self):
  tx=Tx([[restored('m',manual=True,skip=True),restored('l',2,kind='long'),restored('s',3)],[],2]);order=[]
  with patch.object(page,'_manual_queues',side_effect=lambda t,r:order.append(('manual',[x['video_id'] for x in r]))),patch.object(page,'_ordinary_long',side_effect=lambda t,ids:order.append(('ordinary',ids))),patch.object(page,'refresh_review_cases_batch',side_effect=lambda t,ids:order.append(('review',ids))):page._restored_inventory(tx,[receipt(v) for v in ('m','l','s')])
  self.assertEqual(order,[('manual',['m']),('ordinary',['l']),('review',['l','s'])]);self.assertEqual(tx.calls[-1][2],(['l','s'],))
 def test_manual_live_lease_fails_before_policy_or_mutation(self):
  tx=Tx([[dict(video_id='v',value='QUEUE')],[dict(video_id='v',live=True)]])
  with self.assertRaises(pg.DatabaseServerError) as caught:page._manual_queues(tx,[dict(video_id='v',channel_id='c',video_type='short',published_at=None)])
  self.assertEqual(caught.exception.code,'55P03');self.assertEqual(len(tx.calls),2)
 def test_manual_typed_missing_policy_uses_real_strict_decoder(self):
  tx=Tx([[],[dict(video_id='v',published_at='native',published_acquired_at='source_clock',stream_end=None,stream_end_acquired_at=None,has_current=True,availability_status='public')],[dict(video_id='v',identity_facts=None)],1,1])
  page._manual_queues(tx,[dict(video_id='v',channel_id='c',video_type='short',published_at=None)])
  self.assertEqual(tx.calls[-1][2][0].obj[0]['pause'],'typed_identity_not_eligible');self.assertEqual(len(tx.calls),5)
 def test_manual_long_incident_does_not_override_current_profile_policy(self):
  tx=Tx([[],[dict(video_id='v',published_at=None,published_acquired_at=None,stream_end=None,stream_end_acquired_at=None,has_current=True,availability_status='private')],[dict(video_id='v',open_incident=True,policy_facts=None)],1,1])
  page._manual_queues(tx,[dict(video_id='v',channel_id='c',video_type='long',published_at=None)])
  self.assertEqual(tx.calls[-1][2][0].obj[0]['pause'],'availability_unsupported')
 def test_queue_initialize_precedes_pause_and_no_reason_writes(self):
  tx=Tx([[dict(video_id='b',reason='channel_hidden'),dict(video_id='a',reason=None)],1]);order=[]
  with patch.object(page,'ensure_video_collection_state_in_transaction',side_effect=lambda t,ids:order.append(ids)):page._ordinary_long(tx,['a','b'])
  self.assertEqual(order,[['a','b']]);self.assertEqual(tx.calls[-1][2][0].obj,[dict(video_id='b',reason='channel_hidden')])

class SQL(unittest.TestCase):
 def test_all_twenty_statements_use_actual_driver_and_psycopg_binding(self):
  statements={review:('_ADVISORY','_FACTS','_UPSERT','_WITHDRAW'),page:('_MEMBER_FACTS','_QUEUE_LOCK','_RESTORE_LIVE','_MANUAL_LIVE','_RESTORE','_RESTORED_FACTS','_RESTORE_QUEUE','_DELETE_QUEUE','_PAUSE_FACTS','_PAUSE','_MANUAL_SOURCE','_MANUAL_LONG','_MANUAL_TYPED','_MANUAL_INSERT','_MANUAL_UPDATE','_NEW_INVENTORY','_RESUME_RETURNED')}
  for module,names in statements.items():
   for name in names:
    with self.subTest(name=name):
     query=getattr(module,name);returned=name in ('_RESTORE','_RESTORE_QUEUE');read=query.lstrip().startswith('SELECT') or returned
     sql=pg._reviewed_statement(query,read=read,max_rows=50 if read else 0,returning=returned);PostgresQuery(Transformer()).convert(sql.as_string(),(None,)*query.count('%s'))
 def test_native_conflict_expression_inverse(self):
  self.assertEqual(review._UPSERT.split(' ON CONFLICT',1)[1],review.original._UPSERT.split(' ON CONFLICT',1)[1].replace('%(removed)s::boolean','false'))
  self.assertEqual(page._MANUAL_INSERT.split(' ON CONFLICT',1)[1],page.manual._QUEUE_INSERT.split(' ON CONFLICT',1)[1])
  self.assertIn('clock_timestamp()',page._MANUAL_UPDATE);self.assertIn('lease_expires_at<=clock_timestamp()',page._RESTORE_QUEUE)
 def test_native_sources_not_python_decoded_or_clock_captured(self):
  for module in (page,review):
   source=inspect.getsource(module);self.assertNotIn('json.loads',source);self.assertNotIn('datetime',source)
  self.assertIn('clock_timestamp()',review._FACTS);self.assertIn('clock_timestamp()',page._MEMBER_FACTS)
  self.assertIn('FOR UPDATE OF s NOWAIT',page._QUEUE_LOCK);self.assertIn('FOR NO KEY UPDATE OF i',page._RESTORE)
if __name__=='__main__':unittest.main()
