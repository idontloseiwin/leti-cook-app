"""Saved tombstone cleanup clocks, locking and finite maintenance admission."""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.base.postgres import DatabaseServerError,DatabaseError,PostgresConfig,PostgresDatabase,Operation
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
import hot_runtime
TARGET='deleted_saved_objects'
IDENTITY='c0000000-0000-4000-8000-000000000061'
AT='2026-10-05T12:00:00+00:00'
CUTOFF='2026-09-05T12:00:00+00:00'
EXPIRED='2026-09-01T12:00:00+00:00'

def one(**value):return Result(rows=(value,))
def count(value):return Result(has_columns=False,rowcount=value)
def item(**changes):return dict(kind='collection',id=IDENTITY,deleted_at=EXPIRED,invalid_clock=False,expired=True,**changes)

class CleanupTests(unittest.TestCase):
 def setUp(self):
  self.trace=[];self.connection=Connection(self.trace);self.pool=Pool(self.connection,self.trace);self.gates=[]
  self.database=PostgresDatabase(PostgresConfig(DSN),admission=self.gates.append,pool_factory=lambda config:self.pool).open()
  self.addCleanup(self.database.close)
 def queue(self,*results):self.connection.results.extend(results)
 def header(self,limit=100,after_id=None):self.queue(one(target=TARGET,after_id=after_id,limit=limit),one(at=AT,cutoff=CUTOFF))
 def run_page(self,**kwargs):return self.database.cleanup_deleted_saved_objects(target=TARGET,**kwargs)
 def test_empty_page_is_complete_with_one_known_commit(self):
  self.header();self.queue(Result(rows=()))
  result=self.run_page()
  self.assertEqual(result,dict(status='ok',target=TARGET,as_of=AT,cutoff=CUTOFF,scanned=0,deleted=0,scrubbed=0,
    deferred=0,complete=True,next_cursor=None,oldest_remaining_expired_source_at=None,invalid_clock_remaining=0,priority_complete=True,priority_references=[]))
  self.assertEqual(self.connection.commits,1);self.assertEqual(self.pool.borrowed,1)
  self.assertEqual(set(self.gates),{'cleanup_deleted_saved_objects'})
 def test_expired_row_rechecked_under_lock_before_receipt_delete(self):
  self.header(limit=1);self.queue(Result(rows=(item(),)),one(expired=True),count(2),count(1))
  value=self.run_page(limit=1)
  self.assertEqual((value['deleted'],value['complete'],value['next_cursor']),(1,False,IDENTITY))
  statements=[q for q,p in self.connection.queries]
  self.assertIn('FOR UPDATE NOWAIT',statements[3]);self.assertIn('saved_collection_commands',statements[4])
  self.assertIn('deleted_at IS NOT NULL',statements[5])
  # Recording connection counts savepoint release and outer commit separately.
  self.assertEqual(self.connection.commits,2);self.assertEqual(self.pool.borrowed,1)
 def test_concurrent_restore_or_disappearance_never_deletes_receipts(self):
  for locked in (one(expired=False),Result(rows=())):
   self.header();self.queue(Result(rows=(item(),)),locked)
   self.assertEqual(self.run_page()['deleted'],0)
  self.assertFalse(any(q.startswith('DELETE') for q,p in self.connection.queries))
 def test_busy_row_defers_with_native_oldest_and_page_continues(self):
  self.header();self.queue(Result(rows=(item(),)),Result(execute_error=ServerFailure('55P03')),one(value=EXPIRED))
  value=self.run_page()
  self.assertEqual((value['deferred'],value['oldest_remaining_expired_source_at']),(1,EXPIRED))
  self.assertEqual(self.connection.commits,1)
  self.assertIn('min(value::timestamptz)',self.connection.queries[-1][0])
 def test_invalid_clock_and_fresh_tombstones_do_not_acquire_row_lock(self):
  self.header();self.queue(Result(rows=(dict(item(),invalid_clock=True),dict(item(),expired=False))))
  result=self.run_page();self.assertEqual(result['invalid_clock_remaining'],1);self.assertEqual(result['scanned'],2)
  self.assertEqual(len(self.connection.queries),3)
 def test_invalid_cursor_native_error_is_safe_domain_error(self):
  self.header(after_id='not_uuid');self.queue(Result(execute_error=ServerFailure('22P02')))
  with self.assertRaises(DatabaseServerError) as caught:self.run_page(after_id='not_uuid')
  self.assertEqual((caught.exception.code,caught.exception.message),('P0001','hot_operational_cursor_invalid'))
  self.assertEqual(self.connection.commits,0)
 def test_native_limit_overflow_precedes_wrong_target(self):
  self.queue(Result(execute_error=ServerFailure('22003')))
  with self.assertRaises(DatabaseServerError) as caught:self.database.cleanup_deleted_saved_objects(target='bad',limit=2147483648)
  self.assertEqual(caught.exception.code,'22003');self.assertEqual(len(self.connection.queries),1)
 def test_invalid_target_or_limit_aborts_before_table_query(self):
  for args,code in ((dict(target='wrong',after_id=None,limit=100),'hot_operational_target_invalid'),(dict(target=TARGET,after_id=None,limit=None),'hot_operational_limit_invalid')):
   self.queue(one(**args),one(at=AT,cutoff=CUTOFF))
   with self.assertRaises(DatabaseServerError) as caught:self.run_page()
   self.assertEqual(caught.exception.message,code)
  self.assertFalse(any('public.saved_objects' in q for q,p in self.connection.queries))
 def test_late_error_rolls_back_everything_without_retry(self):
  self.header();self.queue(Result(rows=(item(),)),one(expired=True),count(1),Result(execute_error=ServerFailure('23503')))
  with self.assertRaises(DatabaseServerError):self.run_page()
  self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
 def test_finite_gate_cannot_be_borrowed_by_an_operation_name_or_sql(self):
  self.database._admission=__import__('db.base.postgres',fromlist=['_runtime_admission'])._runtime_admission
  env={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres','LIC_DATABASE_ID':'91f5d9f3-9230-4ee6-b170-155d4c3c1719','LIC_HOT_RUNTIME_SECRET':'internal_runtime_secret_0123456789'}
  controller=Mock();controller.require_collection_ready.side_effect=hot_runtime.HotRuntimeUnavailable('hot_runtime_not_ready')
  with patch.dict(os.environ,env,clear=True),patch.object(hot_runtime,'_local_controller',controller):
   with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):self.run_page()
   with hot_runtime.maintenance_access():
    self.header();self.queue(Result(rows=()));self.run_page()
    with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
     with self.database.transaction(Operation('cleanup_deleted_saved_objects')):pass
    with self.assertRaises(TypeError):self.database.cleanup_deleted_saved_objects(target=TARGET,statement='DELETE FROM channels')
  self.assertEqual(self.pool.borrowed,1)

if __name__=='__main__':unittest.main()
