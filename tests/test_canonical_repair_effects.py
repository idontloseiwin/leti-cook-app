"""Finite repair effects; original owner locks/receipts/postconditions stay owned."""
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import re,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.base import postgres as pg
from db.inventory import canonical_repair_effects as effect,inventory_canonical_repair as owner
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
class Fake:
 def __init__(self,answers):self.answers=deque(answers);self.calls=[];self.failed=False
 @contextmanager
 def borrow(self):
  try:yield self
  except BaseException:self.failed=True;raise
 def answer(self,kind,query,args,options):
  self.calls.append((kind,query,args,options));value=self.answers.popleft()
  if isinstance(value,BaseException):raise value
  return value
 def fetch_all(self,query,args,**options):return self.answer('read',query,args,options)
 def returning_all(self,query,args,**options):return self.answer('returning',query,args,options)
 def execute(self,query,args):return self.answer('write',query,args,{})
def facts(video='v',restore=False,manual=False,index=1):return dict(ordinality=index,video_id=video,channel_id='channel',video_type='short',published_at=None,published_acquired_at=None,restore_type=restore,manual=manual,exact=True)
def receipt():return dict(old_text='NATIVE_OLD',new_text='NATIVE_NEW')
def review_fact(video='v',types=None,present=False):return dict(video_id=video,channel_id='channel',video_type='short',published_at=None,published_acquired_at=None,types=['short'] if types is None else types,channels=['channel'],blocked=None,present=present,evidence_expires_at='2026-10-20T00:00:00Z')
class Repair(unittest.TestCase):
 def test_all_fixed_sql_passes_actual_driver_and_psycopg_binding(self):
  pairs=((effect._FACTS,'read'),(effect._RESTORE,'returning'),(effect._DELETE,'write'),(effect._MANUAL_LOCK,'read'),(effect._MANUAL_FACTS,'read'),(effect._MANUAL_INSERT,'write'),(effect._MANUAL_UPDATE,'write'),(effect._REVIEW_DECISION,'read'),(effect._ADVISORY,'read'),(effect._REVIEW_FACTS,'read'),(effect._REVIEW_UPSERT,'write'),(owner._UPDATE_EFFECT,'returning'))
  for query,mode in pairs:
   with self.subTest(sql=query[:40]):
    named=re.findall(r'%\(([^)]+)\)s',query);params={name:None for name in named} if named else (None,)*query.count('%s')
    bound=pg._reviewed_statement(query,read=mode!='write',max_rows=1000 if mode!='write' else 0,returning=mode=='returning')
    PostgresQuery(Transformer()).convert(bound.as_string(),params)
 def test_restore_receipt_short_circuits_manual_or_delete_but_not_review(self):
  tx=Fake([[facts(restore=True,manual=True)],[dict(video_id='v')],[dict(video_id='v',present=True)]])
  effect.after_canonical_repair_statement(tx,[receipt()])
  self.assertEqual([call[1] for call in tx.calls],[effect._FACTS,effect._RESTORE,effect._REVIEW_DECISION]);self.assertFalse(tx.failed)
 def test_missing_restore_falls_through_original_delete_then_review(self):
  tx=Fake([[facts(restore=True)],[],0,[dict(video_id='v',present=False)],[dict(acquired=None)],[review_fact()]])
  effect.after_canonical_repair_statement(tx,[receipt()])
  self.assertEqual([call[1] for call in tx.calls],[effect._FACTS,effect._RESTORE,effect._DELETE,effect._REVIEW_DECISION,effect._ADVISORY,effect._REVIEW_FACTS])
 def test_existing_case_refresh_keeps_normal_clock_branch_even_without_conflict(self):
  tx=Fake([[facts()],1,[dict(video_id='v',present=False)],[dict(acquired=None)],[review_fact(present=True)],1])
  effect.after_canonical_repair_statement(tx,[receipt()]);plan=tx.calls[-1][2][0].obj[0]
  self.assertEqual(tx.calls[-1][1],effect._REVIEW_UPSERT);self.assertIs(plan['removed'],False);self.assertEqual(plan['types'],['short']);self.assertEqual(plan['evidence_expires_at'],'2026-10-20T00:00:00Z')
  self.assertIn('clock_timestamp()',effect._REVIEW_UPSERT);self.assertNotIn('input.removed',effect._REVIEW_UPSERT.split('ON CONFLICT',1)[1])
 def test_manual_queue_keeps_typed_identity_and_busy_native_branch(self):
  tx=Fake([[facts(manual=True)],[dict(video_id='v',live=True)]])
  with self.assertRaises(pg.DatabaseServerError) as caught:effect.after_canonical_repair_statement(tx,[receipt()])
  self.assertEqual(caught.exception.code,'55P03');self.assertEqual(caught.exception.message,'manual_video_type_collection_busy');self.assertTrue(tx.failed)
  self.assertEqual(len(tx.calls),2)
 def test_manual_queue_creation_update_then_review_skip(self):
  tx=Fake([[facts(manual=True)],[],[dict(video_id='v',published_at=None,published_acquired_at=None,stream_end=None,stream_end_acquired_at=None,has_current=False,availability_status=None,identity_facts=None)],1,1,[dict(video_id='v',present=True)]])
  effect.after_canonical_repair_statement(tx,[receipt()]);insert=tx.calls[3]
  self.assertEqual(insert[1],effect._MANUAL_INSERT);self.assertEqual(insert[2][0].obj[0]['pause'],'typed_identity_not_eligible')
  self.assertEqual(tx.calls[4][1],effect._MANUAL_UPDATE);self.assertEqual(tx.calls[5][1],effect._REVIEW_DECISION)
 def test_exact_receipt_shape_and_native_effect_validation_precede_writes(self):
  for value in ([],[dict(facts(),exact=False)],[dict(facts(),video_type='long')],[dict(facts(),ordinality=2)]):
   tx=Fake([value])
   with self.assertRaises(pg.DatabaseError):effect.after_canonical_repair_statement(tx,[receipt()])
   self.assertEqual(len(tx.calls),1);self.assertTrue(tx.failed)
 def test_duplicate_video_across_batches_fails_atomically(self):
  first=[facts(str(i),index=i+1) for i in range(1000)];tx=Fake([first,1000,[dict(video_id=str(i),present=True) for i in range(1000)],[facts('0')]])
  with self.assertRaises(pg.DatabaseError):effect.after_canonical_repair_statement(tx,[receipt() for _ in range(1001)])
  self.assertTrue(tx.failed);self.assertEqual(len(tx.calls),4)
 def test_thousand_rows_remain_three_queries_on_delete_and_decision_skip(self):
  rows=[facts(str(i),index=i+1) for i in range(1000)];tx=Fake([rows,1000,[dict(video_id=str(i),present=True) for i in range(1000)]])
  effect.after_canonical_repair_statement(tx,[receipt() for _ in rows]);self.assertEqual(len(tx.calls),3)
  self.assertEqual(tx.calls[0][3],dict(max_rows=1000));self.assertEqual(len(tx.calls[1][2][0]),1000)
 def test_empty_input_and_original5000_bound(self):
  tx=Fake([]);effect.after_canonical_repair_statement(tx,[]);self.assertEqual(tx.calls,[])
  with self.assertRaises(ValueError):effect.after_canonical_repair_statement(tx,[receipt()]*5001)
 def test_source_update_keeps_set_predicate_single_statement_and_postconditions(self):
  expected=owner._UPDATE.replace(' FROM unnest(', ' FROM public.video_inventory effect_previous,unnest(',1)+" AND effect_previous.video_id=inventory.video_id RETURNING to_jsonb(effect_previous)::text AS old_text,to_jsonb(inventory)::text AS new_text"
  self.assertEqual(owner._UPDATE_EFFECT,expected)
  body=Path(owner.__file__).read_text().split('def repair_typed_inventory_canonical_types',1)[1]
  self.assertLess(body.index('lock_typed_inventory_repair'),body.index('_UPDATE_EFFECT'));self.assertLess(body.index('after_canonical_repair_statement(tx'),body.index('_UNRESOLVED'));self.assertLess(body.index('_UNRESOLVED'),body.index('_REMAINS'))
  self.assertIn('updated!=count',body);self.assertIn('max_rows=5000',body)
if __name__=='__main__':unittest.main()
