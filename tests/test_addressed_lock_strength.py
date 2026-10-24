"""Prepared lock-strength parity boundaries, offline only."""
from pathlib import Path
import sys,unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tools')]
from db.quota import quota_write_normalization as quota
from db.runs import sync_run_write_normalization as sync
TARGET='55000000-0000-4000-8000-000000000001'

class LockStrengthTests(unittest.TestCase):
 def test_nonkey_fields_use_original_nonkey_lock(self):
  for fields in ({'duration_ms':2},{'retry_of_request_attempt_id':None},{'phase':None,'quota_units':7}):
   self.assertIn('FOR NO KEY UPDATE OF ledger',quota._proposal(fields,insert=False).as_string())
  for fields in ({'errors':2},{'summary_json':None},{'status':'error','finished_at':None}):
   self.assertIn('FOR NO KEY UPDATE OF run',sync._addressed_proposal(fields).as_string())
 def test_explicit_primary_key_always_retains_stronger_lock(self):
  for value in (None,0,True,'same','new'):
   q=quota._proposal({'request_attempt_id':value,'duration_ms':2},insert=False).as_string()
   s=sync._addressed_proposal({'id':value,'errors':2}).as_string()
   self.assertIn('FOR UPDATE OF ledger',q);self.assertNotIn('FOR NO KEY',q)
   self.assertIn('FOR UPDATE OF run',s);self.assertNotIn('FOR NO KEY',s)
 def test_hot_and_scrub_explicit_baseline_locks_are_unchanged(self):
  self.assertIn('FOR UPDATE OF run',sync._HOT_LOCK);self.assertIn('FOR UPDATE OF run NOWAIT',sync._SCRUB_LOCK)
 def test_original_parameters_and_predicates_bind_without_new_input(self):
  for values in ({'duration_ms':2},{'request_attempt_id':TARGET}):
   q=quota._proposal(values,insert=False);PostgresQuery(Transformer()).convert(q,tuple(values.values())+(TARGET,))
   self.assertIn("ledger.status='started'",q.as_string())
  for values in ({'errors':2},{'id':5500}):
   q=sync._addressed_proposal(values);PostgresQuery(Transformer()).convert(q,tuple(values.values())+(5500,))
   self.assertIn('WHERE run.id=%s::bigint',q.as_string())

if __name__=='__main__':unittest.main()
