"""Offline bounded operational retention clocks, locks and native payloads."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.retention import operational_retention as cleanup
from db.base.postgres import DatabaseServerError,PostgresConfig,PostgresDatabase,Operation,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
AT='2026-10-06T12:00:00+00:00';OLD='2026-09-01T12:00:00+00:00';FLOOR='2026-09-08T12:00:00+00:00'
ID='f9700000-0000-4000-8000-000000000001';SCAN='f9700000-0000-4000-8000-000000000002'
CHANNEL='UC0000000000000000000001'

def one(**values):
 if 'oldest' in values:values.setdefault('unsafe_positions',[0] if values['oldest'] is not None or values.get('invalid') else [])
 return Result(rows=(values,))
def count(value=1):return Result(has_columns=False,rowcount=value)
def job(**changes):return one(**dict(dict(id=ID,channel_id=CHANNEL,inventory_scan_id=None,initial_scope_version=0,summary='{}',source_at=OLD,last_error_code=None,lease_expires_at=None,has_lease=False),**changes))
def quota(**changes):return one(**dict(dict(source_at=AT,expired=False,phase='raw_phase',endpoint='raw_endpoint',quota_bucket='raw_bucket',error_type='RawError'),**changes))

class CleanupTests(unittest.TestCase):
 def setUp(self):
  self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
  self.database=PostgresDatabase(PostgresConfig(DSN),pool_factory=lambda config:self.pool,admission=lambda name:None).open()
  self.addCleanup(self.database.close)
 def queue(self,*items):self.connection.results.extend(items)
 def run_page(self,target='operator_jobs',**kwargs):
  return self.database.cleanup_hot_operational_copies(target=target,**kwargs)
 def header(self,target='operator_jobs',limit=100,cursor=None):
  self.queue(one(target=target,limit=limit,cursor=cursor,cursor_length=len(cursor) if cursor is not None else None,at=AT,cutoff=FLOOR))
 def page(self,target='operator_jobs',limit=100,**job_changes):
  self.header(target,limit)
  if target not in ('operator_job_targets','sync_runs','quota_attempts'):self.queue(one(value=None))
  if target=='operator_job_targets':
   # Next job with targets, its one lock-free check (needs work), its targets, no further job.
   self.queue(one(id=ID),job(**job_changes),one(present=False),one(hot=False))
   self.queue(Result(rows=(dict(id=ID,video='aaaaaaaaaaa',cursor='native_cursor'),)),Result())
   return
  self.queue(Result(rows=(dict(id='61' if target=='sync_runs' else ID,video=None,cursor='native_cursor'),)))
 def expired_job(self,target='operator_jobs',**changes):
  self.page(target,**changes);self.queue(job(**changes),one(present=False),one(hot=False),Result(),job(**changes),one(stale=True),one(present=False),one(busy=False))
 def test_empty_page_native_clock_bound_and_cursor(self):
  self.header();self.queue(one(value=None),Result())
  value=self.run_page()
  self.assertTrue(value['complete']);self.assertIsNone(value['next_cursor'])
  self.assertEqual((value['as_of'],value['cutoff']),(AT,FLOOR))
  self.assertEqual(self.pool.borrowed,1)
 def test_limit_then_cursor_length_then_target_validation(self):
  for target,limit,cursor,code in [('bad',None,'','limit'),('bad',100,'','cursor'),('bad',100,None,'target')]:
   self.header(target,limit,cursor)
   with self.assertRaises(DatabaseServerError) as caught:self.run_page(target,limit=limit,after_id=cursor)
   self.assertEqual(caught.exception.message,'hot_operational_'+code+'_invalid')
  self.assertEqual(len(self.connection.queries),3)
 def test_every_data_exception_in_cursor_maps_but_lock_timeout_does_not(self):
  for code in ('22P02','22003','22023','57014'):
   self.header('sync_runs',cursor='bad');self.queue(Result(execute_error=ServerFailure(code)))
   with self.assertRaises(DatabaseServerError) as caught:self.run_page('sync_runs',after_id='bad')
   self.assertEqual(caught.exception.code,'P0001' if code.startswith('22') else code)
 def test_target_cursor_is_native_json_and_uuid(self):
  self.header('operator_job_targets',cursor='["'+ID+'","v"]');self.queue(one(valid=True,job=ID,video='v'),one(value=ID),Result(),Result())
  self.assertTrue(self.run_page('operator_job_targets',after_id='["'+ID+'","v"]')['complete'])
  resumed=[p for q,p in self.connection.queries if 'WHERE job_id=%(job)s::uuid' in q][0]
  self.assertEqual((resumed['job'],resumed['after_video']),(ID,'v'))
  self.assertEqual([p for q,p in self.connection.queries if 'job_id>%(after)s::uuid' in q],[{'after':ID}])
 def test_a_job_that_needs_no_work_is_skipped_with_all_its_targets(self):
  other='f9700000-0000-4000-8000-000000000009'
  self.header('operator_job_targets')
  self.queue(one(id=ID),job(source_at=AT),one(present=False),one(hot=True))  # fresh job: no target is read
  self.queue(one(id=other),job(id=other,source_at=AT),one(present=False),one(hot=True),Result())
  value=self.run_page('operator_job_targets')
  self.assertEqual((value['scanned'],value['complete'],value['next_cursor']),(0,True,None))
  self.assertFalse(any('WHERE job_id=%(job)s::uuid' in q for q,p in self.connection.queries))
 def test_a_page_checks_at_most_its_share_of_jobs(self):
  self.header('operator_job_targets')
  for _ in range(cleanup.JOBS_PER_PAGE):self.queue(one(id=ID),job(source_at=AT),one(present=False),one(hot=True))
  value=self.run_page('operator_job_targets')
  self.assertEqual((value['scanned'],value['complete'],value['next_cursor']),(0,False,'["'+ID+'",null]'))
 def test_runs_and_api_records_read_only_rows_past_the_cutoff_or_from_the_future_oldest_first(self):
  for target,table,key in (('sync_runs','sync_runs','id'),('quota_attempts','youtube_api_request_attempts','request_attempt_id')):
   self.connection.queries.clear();self.header(target);self.queue(Result())
   self.assertTrue(self.run_page(target)['complete'])
   sql,params=self.connection.queries[-1]
   self.assertIn('FROM public.'+table+' WHERE started_at<%(cutoff)s::timestamptz',sql)
   self.assertIn('FROM public.'+table+' WHERE started_at>%(at)s::timestamptz',sql)
   self.assertIn('ORDER BY started_at,'+key,sql);self.assertNotIn('ORDER BY '+key+' ',sql)
   self.assertEqual((params['cutoff'],params['at']),(FLOOR,AT))
 def test_ordered_cursor_is_a_native_clock_and_key_pair(self):
  cursor='["'+OLD+'","61"]'
  self.header('sync_runs',cursor=cursor);self.queue(one(valid=True,clock=OLD,id='61'),one(clock=OLD,id='61'),Result())
  self.assertTrue(self.run_page('sync_runs',after_id=cursor)['complete'])
  self.assertEqual((self.connection.queries[-1][1]['after_clock'],self.connection.queries[-1][1]['after_id']),(OLD,'61'))
  self.header('quota_attempts',cursor='[1]');self.queue(one(valid=False,clock=None,id=None))
  with self.assertRaises(DatabaseServerError) as caught:self.run_page('quota_attempts',after_id='[1]')
  self.assertEqual(caught.exception.message,'hot_operational_cursor_invalid')
 def test_full_page_does_not_look_ahead_or_lose_cursor(self):
  self.page(limit=1);self.queue(Result())
  value=self.run_page(limit=1)
  self.assertFalse(value['complete']);self.assertEqual(value['next_cursor'],'native_cursor')
 def test_job_hot_noop_has_no_lock_or_mutation(self):
  self.page();self.queue(job(source_at=AT),one(present=False),one(hot=True))
  self.assertEqual(self.run_page()['scrubbed'],0)
  self.assertFalse(any('FOR UPDATE' in q for q,p in self.connection.queries))
 def test_job_channel_lock_precedes_locked_read_and_scrub(self):
  self.expired_job();self.queue(count())
  value=self.run_page();self.assertEqual((value['scrubbed'],value['deleted'],value['deferred']),(1,0,0))
  locks=[q for q,p in self.connection.queries if 'FOR UPDATE' in q]
  self.assertIn('public.channels',locks[0]);self.assertIn('public.operator_jobs',locks[1])
  self.assertTrue(self.connection.queries[-1][1]['stale'])
  self.assertIn('inventory_scan_id=NULL',self.connection.queries[-1][0])
 def test_locked_refresh_wins_before_lease_or_update(self):
  self.page();self.queue(job(),one(present=False),one(hot=False),Result(),job(source_at=AT),one(stale=False),one(present=False))
  self.assertEqual(self.run_page()['scrubbed'],0)
  self.assertFalse(any(q==cleanup._LEASE or q.startswith('UPDATE') for q,p in self.connection.queries))
 def test_live_lease_has_distinct_invalid_clock_telemetry(self):
  self.page();self.queue(job(source_at=AT,inventory_scan_id=SCAN),one(expired=True),one(present=False),one(hot=True),Result(),job(source_at=AT,inventory_scan_id=SCAN),one(stale=False),one(expired=True),one(present=False),one(busy=True),one(oldest=None,invalid=1))
  value=self.run_page();self.assertEqual((value['deferred'],value['invalid_clock_remaining']),(1,1))
  self.assertEqual(self.connection.queries[-1][1][0].obj,[dict(source_at=AT,reason='lease',clock_observed=True,position=0)])
 def test_ack_only_scrub_does_not_wait_for_lease(self):
  self.page()
  for locked in (False,True):
   if locked:self.queue(Result())
   self.queue(job(source_at=AT))
   if locked:self.queue(one(stale=False))
   self.queue(one(present=True),one(scan_id=None,transition_id=SCAN,outcome='quota'),one(expired=True))
   if not locked:self.queue(one(hot=True))
  self.queue(count())
  self.assertEqual(self.run_page()['scrubbed'],1)
  self.assertIn("summary_json-'inventory_ack'",self.connection.queries[-1][0])
  self.assertFalse(any(q==cleanup._LEASE for q,p in self.connection.queries))
 def test_full_sources_native_error_precedes_ack_and_owner_lock(self):
  self.page();self.queue(job(initial_scope_version=1),Result(execute_error=ServerFailure('22023')))
  with self.assertRaises(DatabaseServerError):self.run_page()
  self.assertIn('jsonb_each',self.connection.queries[-1][0])
  self.assertFalse(any('FOR UPDATE' in q for q,p in self.connection.queries))
 def test_unfenced_target_defer_keeps_unfiltered_source_clock(self):
  self.expired_job('operator_job_targets',source_at='-infinity');self.queue(one(oldest='-infinity',invalid=0))
  value=self.run_page('operator_job_targets');self.assertEqual(value['oldest_remaining_expired_source_at'],'-infinity')
  self.assertEqual(self.connection.queries[-1][1][0].obj,[dict(source_at='-infinity',reason='unfenced_target',clock_observed=True,position=0)])
  self.assertIn("reason='unfenced_target' THEN source_at",cleanup._TELEMETRY)
 def targets(self,*videos,**changes):
  # One job that needs work, several of its targets on the page, then its full check.
  self.header('operator_job_targets')
  self.queue(one(id=ID),job(**changes),one(present=False),one(hot=False))
  self.queue(Result(rows=tuple(dict(id=ID,video=v,cursor='native_'+v) for v in videos)),Result())
  self.queue(job(**changes),one(present=False),one(hot=False),Result(),job(**changes),one(stale=True),one(present=False),one(busy=False))
 def test_a_job_decided_under_its_lock_deletes_its_other_targets_together(self):
  self.targets('aaaaaaaaaaa','bbbbbbbbbbb','ccccccccccc',last_error_code='source_scope_expired')
  self.queue(Result(),count(),Result(rows=(dict(video_id='bbbbbbbbbbb'),)),count(),one(oldest=OLD,invalid=0))
  value=self.run_page('operator_job_targets')
  self.assertEqual((value['scanned'],value['deleted'],value['deferred']),(3,2,1))
  self.assertEqual(value['priority_references'],[dict(target='operator_job_targets',key=[ID,'ccccccccccc'])])
  self.assertEqual(sum('public.channels' in q and 'FOR UPDATE' in q for q,p in self.connection.queries),1)
  locked,deleted=self.connection.queries[-3],self.connection.queries[-2]
  self.assertIn('FOR UPDATE SKIP LOCKED',locked[0]);self.assertEqual(locked[1]['videos'],['bbbbbbbbbbb','ccccccccccc'])
  self.assertTrue(deleted[0].startswith('DELETE FROM public.operator_job_targets'));self.assertEqual(deleted[1]['videos'],['bbbbbbbbbbb'])
  self.assertEqual(self.connection.queries[-1][1][0].obj,[dict(source_at=OLD,reason='lock',clock_observed=True,position=0)])
 def test_a_leased_job_defers_its_other_targets_without_another_check(self):
  self.header('operator_job_targets')
  self.queue(one(id=ID),job(),one(present=False),one(hot=False))
  self.queue(Result(rows=tuple(dict(id=ID,video=v,cursor='native_'+v) for v in ('aaaaaaaaaaa','bbbbbbbbbbb'))),Result())
  self.queue(job(),one(present=False),one(hot=False),Result(),job(),one(stale=True),one(present=False),one(busy=True),one(oldest=OLD,invalid=0,unsafe_positions=[0,1]))
  value=self.run_page('operator_job_targets')
  self.assertEqual((value['deferred'],value['deleted']),(2,0))
  self.assertEqual([x['reason'] for x in self.connection.queries[-1][1][0].obj],['lease','lease'])
  self.assertEqual(sum(cleanup._LEASE in q for q,p in self.connection.queries),1)
 def test_target_deleted_only_after_header_fence_and_row_lock(self):
  self.expired_job('operator_job_targets',last_error_code='source_scope_expired');self.queue(Result(),count())
  self.assertEqual(self.run_page('operator_job_targets')['deleted'],1)
  self.assertIn('operator_job_targets',self.connection.queries[-2][0]);self.assertIn('FOR UPDATE NOWAIT',self.connection.queries[-2][0])
 def test_row_lock_failure_discards_counters_but_retains_source_for_oldest(self):
  self.expired_job();self.queue(Result(execute_error=ServerFailure('55P03')),one(oldest=OLD,invalid=0))
  value=self.run_page();self.assertEqual((value['scrubbed'],value['deferred']),(0,1))
  self.assertEqual(self.connection.queries[-1][1][0].obj,[dict(source_at=OLD,reason='lock',clock_observed=True,position=0)])
  self.assertEqual(self.connection.rollbacks,1)
  self.assertEqual(value['unknown_expiry_remaining'],0)  # its clock was read before the lock
 def test_a_row_locked_before_its_clock_is_read_counts_as_unknown_age(self):
  self.page();self.queue(Result(execute_error=ServerFailure('55P03')),one(oldest=None,invalid=0))
  value=self.run_page();self.assertEqual((value['deferred'],value['unknown_expiry_remaining']),(1,1))
  self.assertEqual(self.connection.queries[-1][1][0].obj,[dict(source_at=None,reason='lock',clock_observed=False,position=0)])
 def test_mixed_old_and_unobserved_busy_rows_keep_only_observed_ordinary_cause(self):
  other='f9700000-0000-4000-8000-000000000099'
  self.header();self.queue(one(value=None),Result(rows=(dict(id=ID,video=None,cursor=ID),dict(id=other,video=None,cursor=other))))
  self.queue(job(),one(present=False),one(hot=False),Result(execute_error=ServerFailure('55P03')),
             Result(execute_error=ServerFailure('55P03')),one(oldest=OLD,invalid=0,unsafe_positions=[0]))
  value=self.run_page()
  self.assertEqual(value['deferred'],2)
  self.assertEqual(value['unknown_expiry_remaining'],1)
  self.assertEqual(value['priority_references'],[dict(target='operator_jobs',key=[ID])])
  self.assertEqual([x['position'] for x in self.connection.queries[-1][1][0].obj],[0,1])
 def test_observed_null_clock_then_busy_is_invalid_not_unknown(self):
  self.expired_job(source_at=None)
  self.queue(Result(execute_error=ServerFailure('55P03')),one(oldest=None,invalid=1))
  value=self.run_page()
  self.assertEqual((value['deferred'],value['unknown_expiry_remaining'],value['invalid_clock_remaining']),(1,0,1))
  self.assertEqual(self.connection.queries[-1][1][0].obj,[dict(source_at=None,reason='lock',clock_observed=True,position=0)])
  self.assertIn('clock_observed AND (source_at IS NULL',cleanup._TELEMETRY)
 def test_expired_sync_copy_rechecks_expiry_in_delete(self):
  self.page('sync_runs');self.queue(one(source_at=OLD,expired=True),count(0))
  self.assertEqual(self.run_page('sync_runs')['deleted'],0)
  self.assertTrue(self.connection.queries[-1][0].startswith('DELETE'))
  self.assertIn('started_at<',self.connection.queries[-1][0])
  self.assertFalse(any('FOR UPDATE' in q for q,p in self.connection.queries))
 def test_expired_quota_parent_refreshed_before_lock_keeps_children(self):
  self.page('quota_attempts');self.queue(one(source_at=OLD,expired=True),Result())
  self.assertEqual(self.run_page('quota_attempts')['deleted'],0)
  self.assertIn('FOR UPDATE',self.connection.queries[-1][0])
  self.assertIn('started_at<',self.connection.queries[-1][0])
  self.assertFalse(any(q.startswith(('UPDATE','DELETE')) for q,p in self.connection.queries))
 def test_locked_summary_reprojects_raw_huge_and_tiny_numbers(self):
  for number in ('1'+'0'*5000,'0.'+'0'*399+'1'):
   self.page('sync_runs');self.queue(one(source_at=AT,expired=False,summary='{"processed":1,"raw":"x"}'),one(same=False),one(columns_text='{}',summary_text='{"processed":'+number+',"raw":"y"}'),one(columns_text='{}',summary_text='{"processed":'+number+'}',changed=True),count())
   self.assertEqual(self.run_page('sync_runs')['scrubbed'],1)
   self.assertIn(number,self.connection.queries[-1][1][0])
   self.assertEqual(self.connection.queries[-1][1][2],'{"processed":'+number+'}')
   self.assertTrue(any('FOR UPDATE' in q and 'NOWAIT' in q for q,params in self.connection.queries))
   self.assertFalse(self.connection.queries[-1][0].startswith('DELETE'))
 def test_safe_summary_noop_uses_native_jsonb_equality(self):
  self.page('sync_runs');self.queue(one(source_at=AT,expired=False,summary='{"processed":1.000}'),one(same=True))
  self.assertEqual(self.run_page('sync_runs')['scrubbed'],0)
  self.assertIn('IS NOT DISTINCT FROM',self.connection.queries[-1][0])
 def test_quota_locked_values_win_and_fixed_label_policy_is_reused(self):
  self.page('quota_attempts');self.queue(quota(),quota(phase='initial_inventory',endpoint='videos',quota_bucket='general',error_type=None),count(0))
  self.assertEqual(self.run_page('quota_attempts')['scrubbed'],0)
  params=self.connection.queries[-1][1]
  self.assertEqual([params[k] for k in ('phase','endpoint','quota_bucket','error_type')],['initial_inventory','videos','general',None])
 def test_quota_fresh_safe_labels_skip_lock(self):
  self.page('quota_attempts');self.queue(quota(phase='initial_inventory',endpoint='videos',quota_bucket='general',error_type=None))
  self.assertEqual(self.run_page('quota_attempts')['scrubbed'],0)
  self.assertFalse(any('FOR UPDATE' in q for q,p in self.connection.queries))
 def test_nonlock_write_failure_aborts_whole_transaction(self):
  self.expired_job();self.queue(Result(execute_error=ServerFailure('23514')))
  with self.assertRaises(DatabaseServerError):self.run_page()
  self.assertEqual(self.pool.borrowed,1);self.assertEqual(self.connection.rollbacks,2)
 def test_closed_gate_allows_only_fixed_cleanup_during_blocked_maintenance(self):
  import os,hot_runtime
  from unittest.mock import Mock,patch
  from db.base.postgres import _runtime_admission
  self.database._admission=_runtime_admission
  controller=Mock();controller.require_collection_ready.side_effect=hot_runtime.HotRuntimeUnavailable('hot_runtime_not_ready')
  env={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres','LIC_DATABASE_ID':'91f5d9f3-9230-4ee6-b170-155d4c3c1719','LIC_HOT_RUNTIME_SECRET':'internal_runtime_secret_0123456789'}
  with patch.dict(os.environ,env,clear=True),patch.object(hot_runtime,'_local_controller',controller):
   with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):self.run_page()
   with hot_runtime.maintenance_access():
    self.header();self.queue(one(value=None),Result());self.run_page()
    with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
     with self.database.transaction(Operation('cleanup_hot_operational_copies')):pass
    with self.assertRaises(TypeError):self.database.cleanup_hot_operational_copies(target='operator_jobs',callback=lambda tx:None)
    with self.assertRaises(TypeError):self.database.cleanup_hot_operational_copies(target='operator_jobs',statement='DELETE FROM channels')
  self.assertEqual(self.pool.borrowed,1)

 def test_all_sql_is_finite_parameterized_driver_grammar(self):
  statements=[cleanup._HEADER,*cleanup._PAGE.values(),cleanup._JOB,cleanup._LINKED,cleanup._FULL_SOURCES,cleanup._ACK,cleanup._ACK_SCAN,cleanup._ACK_RECEIPT,cleanup._SOURCE_HOT,cleanup._SOURCE_STALE,cleanup._LEASE,cleanup._SCRUB_JOB,cleanup._RUN,cleanup._QUOTA,*cleanup._DELETE.values(),cleanup._SCRUB_QUOTA,cleanup._TELEMETRY]
  for statement in statements:
   _reviewed_statement(statement,read=statement.startswith(('SELECT','WITH')),max_rows=500 if statement.startswith(('SELECT','WITH')) else 0)
   self.assertNotIn('public.hot_operational_',statement)
   self.assertNotIn('operator_private.inventory_scan_is_hot',statement)

if __name__=='__main__':unittest.main()
