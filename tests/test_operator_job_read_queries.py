"""Job status projections preserve one bounded read and never mutate stored rows."""
from pathlib import Path
import sys,unittest
from datetime import datetime,timezone
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.jobs import operator_job_read_queries as reads
from db.base.postgres import DatabaseError,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,DSN

class ReadTests(unittest.TestCase):
 def setUp(self):
  self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
  self.db=PostgresDatabase(PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
  self.addCleanup(self.db.close)
 def job(self,**values):return dict(channel_id='channel',accepted_epoch=31,id='f8100000-0000-4000-8000-000000000001',job_type='channel_current_stats_backfill',status='queued',**values)
 def test_current_and_initial_use_exact_parameterized_family(self):
  for action,kind in ((reads.get_operator_job_status,'channel_current_stats_backfill'),(reads.get_channel_initial_inventory_job,'channel_initial_long_inventory')):
   job=self.job(summary_json={'count':9007199254740993});job['status']='completed'
   self.connection.results.extend((Result((dict(value=job),)),Result((dict(suspended=False),))))
   self.assertEqual(action(self.db," x' "),job)
   statement,params=self.connection.queries[-2];self.assertEqual(params,(" x' ",kind))
   self.assertNotIn("x'",statement);self.assertIn('LIMIT 1',statement)
 def test_committed_job_lookup_uses_exact_identity_not_latest(self):
  job=self.job(summary_json={'work':7})
  self.connection.results.extend((Result((dict(value=job),)),Result((dict(suspended=False),))))
  result=reads.get_operator_job_by_id(self.db,job['id'],channel_id=job['channel_id'],job_type=job['job_type'])
  self.assertEqual(result,job)
  statement,params=self.connection.queries[-2]
  self.assertEqual(params,(job['id'],job['channel_id'],job['job_type']))
  self.assertIn('FROM readable.operator_jobs',statement)
  self.assertIn('job.id=%s::uuid',statement)
  self.assertNotIn('ORDER BY',statement)
  self.assertFalse(any(q.startswith(('INSERT','UPDATE','DELETE')) for q,p in self.connection.queries))
 def test_suspension_is_an_output_copy_with_other_fields_preserved(self):
  original=dict(channel_id='channel',job_type='channel_current_stats_backfill',status='running',lease_token='owned',lease_owner='worker',lease_expires_at='later',lease_channel_epoch=31,
    accepted_epoch=31,summary_json={'work':7},id='job',last_error_code=None,last_error_summary=None)
  before=dict(original);self.connection.results.extend((Result((dict(value=original),)),Result((dict(suspended=True),))))
  result=reads.get_operator_job_status(self.db,'channel')
  self.assertEqual(result,{**original,**reads._SUSPENDED});self.assertEqual(original,before)
  self.assertFalse(any(q.startswith(('INSERT','UPDATE','DELETE')) for q,p in self.connection.queries))
 def test_missing_latest_remains_none(self):
  self.connection.results.append(Result())
  self.assertIsNone(reads.get_channel_initial_inventory_job(self.db,None))
 def test_active_list_uses_native_clamp_and_existing_scalar_normalization(self):
  identity=UUID('f8100000-0000-4000-8000-000000000001')
  stamp=datetime(2026,10,6,tzinfo=timezone.utc)
  for limit in (None,-1,0,1,101):
   self.connection.results.append(Result((dict(id=identity,requested_at=stamp,target_count=9007199254740993),)))
   row=reads.list_active_operator_jobs(self.db,limit)[0]
   self.assertEqual(row['id'],str(identity));self.assertEqual(row['target_count'],9007199254740993)
   self.assertEqual(self.connection.queries[-1][1],(limit,))
  self.assertIn('greatest(least(coalesce(%s::integer,20),100),0)',reads._LIST)
 def test_more_than_100_returned_rows_fails_closed(self):
  self.connection.results.append(Result(tuple(dict(id=i) for i in range(101))))
  with self.assertRaises(DatabaseError):reads.list_active_operator_jobs(self.db,100)
 def test_invalid_projection_is_rejected(self):
  for row in (dict(value={},suspended=None),dict(value=[],suspended=False),dict(value={},suspended=False,extra=1)):
   self.connection.results.append(Result((row,)))
   with self.assertRaises(DatabaseError):reads.get_operator_job_status(self.db,'channel')
 def test_reviewed_statements_are_reads_with_no_business_rpc(self):
  for statement in (reads._LATEST,reads._LIST,reads._EFFECTIVE):
   _reviewed_statement(statement,read=True,max_rows=100)
   self.assertNotIn('operator_job_effective_value',statement);self.assertNotIn('FOR UPDATE',statement)
  self.assertEqual(reads._LATEST.count(reads._ELIGIBLE),1);self.assertEqual(reads._EFFECTIVE.count(reads._ELIGIBLE),1)

 def test_final_control_check_uses_captured_row_not_a_reselected_job(self):
  for eligible in (False,True):
   job=self.job(summary_json={'opaque':'kept'});before=dict(job)
   self.connection.results.extend((Result((dict(value=job),)),Result((dict(suspended=not eligible),))))
   result=reads.get_operator_job_status(self.db,'channel')
   self.assertEqual(result,before if eligible else {**before,**reads._SUSPENDED})
   query,params=self.connection.queries[-1]
   self.assertIn(reads._EFFECTIVE,query);self.assertNotIn('FROM public.operator_jobs',query)
   self.assertEqual(params,tuple(before[key] for key in reads._CAPTURED_FIELDS))
 def test_invalid_final_control_shape_fails_closed(self):
  self.connection.results.extend((Result((dict(value=self.job()),)),Result((dict(suspended=None),))))
  with self.assertRaises(DatabaseError):reads.get_operator_job_status(self.db,'channel')

if __name__=='__main__':unittest.main()
