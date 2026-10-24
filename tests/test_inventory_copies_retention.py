"""Offline native boundaries and per-row ownership of inventory copy cleanup."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.retention import inventory_copies_retention as cleanup
from db.inventory.inventory_scan_clock import SCAN_IS_HOT_SQL
from db.base.postgres import DatabaseServerError,PostgresConfig,PostgresDatabase
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN
AT='2026-10-06T12:00:00+00:00';OLD='2026-09-01T12:00:00+00:00';FLOOR='2026-09-07T12:00:00+00:00'
SCAN='f9600000-0000-4000-8000-000000000001'
CHANNEL='UC0000000000000000000001'

def one(**values):return Result(rows=(values,))
def count(value=1):return Result(has_columns=False,rowcount=value)
def candidate(target='playlist_state'):
    return dict(a=SCAN if target=='inventory_scans' else 'scan_transition' if target=='inventory_receipts' else CHANNEL,
                b='' if target=='inventory_scans' else SCAN if target=='inventory_receipts' else 'long',cursor='native_cursor')
def state(active=None,completed=None):return one(value='{}',active_scan_id=active,last_completed_scan_id=completed)
def receipt(**changes):return one(**dict(dict(scan_id=None,has_hash=False,expired_marker=False,created_hot=True,source_hot=False,value_clock=OLD),**changes))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),pool_factory=lambda config:self.pool,admission=lambda name:None).open()
        self.addCleanup(self.database.close)
    def queue(self,*items):self.connection.results.extend(items)
    def header(self,target='playlist_state',limit=100,cursor=None):
        self.queue(one(target=target,limit=limit,cursor=cursor,cursor_length=len(cursor) if cursor is not None else None,at=AT,cutoff=FLOOR))
    def run_page(self,target='playlist_state',**kwargs):
        return self.database.cleanup_hot_inventory_copies(target=target,**kwargs)
    def page(self,target='playlist_state',limit=100):
        self.header(target,limit);self.queue(Result(rows=(candidate(target),)),one(at=AT))
    def test_empty_native_header_and_page_commit_once(self):
        self.header();self.queue(Result())
        value=self.run_page()
        self.assertEqual((value['as_of'],value['cutoff'],value['complete'],value['next_cursor']),(AT,FLOOR,True,None))
        self.assertEqual(self.connection.commits,1);self.assertEqual(self.pool.borrowed,1)
        self.assertIn('clock_timestamp()',self.connection.queries[0][0])
    def test_target_before_limit_before_cursor(self):
        for target,limit,cursor,code in [('bad',None,'bad','target'),('playlist_state',0,'bad','limit'),('playlist_state',100,'','cursor')]:
            self.header(target,limit,cursor)
            with self.assertRaises(DatabaseServerError) as error:self.run_page(target,limit=limit,after_id=cursor)
            self.assertEqual(error.exception.message,'hot_inventory_copies_'+code+'_invalid')
        self.assertEqual(len(self.connection.queries),3)
    def test_cursor_remaps_only_invalid_text_representation(self):
        for sqlstate in ('22P02','22023','57014'):
            self.header('inventory_scans',cursor='bad');self.queue(Result(execute_error=ServerFailure(sqlstate)))
            with self.assertRaises(DatabaseServerError) as error:self.run_page('inventory_scans',after_id='bad')
            self.assertEqual(error.exception.code,'P0001' if sqlstate=='22P02' else sqlstate)
    def test_full_page_retains_cursor_without_lookahead(self):
        self.page(limit=1);self.queue(Result())
        result=self.run_page(limit=1)
        self.assertFalse(result['complete']);self.assertEqual(result['next_cursor'],'native_cursor')
        self.assertEqual(self.connection.queries[1][1]['limit'],1)
    def test_playlist_noop_keeps_required_child_lock_order_without_lease_read(self):
        self.page();self.queue(state(SCAN),Result(rows=({'scan_id':SCAN},)),one(noop=True))
        self.assertEqual(self.run_page()['scrubbed'],0)
        queries=[query for query,params in self.connection.queries]
        self.assertIn('channel_playlist_state',queries[3]);self.assertIn('FOR UPDATE NOWAIT',queries[3])
        self.assertIn('ORDER BY scan_id FOR UPDATE NOWAIT',queries[4])
        self.assertFalse(any(query==cleanup._LEASE or query.startswith('UPDATE') for query in queries))
    def playlist_change(self):
        self.page();self.queue(state(SCAN),Result(rows=({'scan_id':SCAN},)),one(noop=False,active_valid=False,completed_valid=False,source_valid=False,value_clock=OLD),one(busy=False),one(pinned=False))
    def test_playlist_invalid_active_scrub_and_owner_fields_in_one_savepoint(self):
        self.playlist_change();self.queue(count(),count())
        result=self.run_page();self.assertEqual((result['scrubbed'],result['deleted'],result['deferred']),(1,0,0))
        updates=[(query,params) for query,params in self.connection.queries if query.startswith('UPDATE')]
        self.assertEqual(len(updates),2);self.assertIn('token_history_verified=false',updates[0][0])
        self.assertIn('lease_token=NULL,lease_owner=NULL,lease_run_id=NULL',updates[1][0])
        self.assertEqual(updates[1][1]['scan_at'],AT)
    def test_partial_candidate_failure_rolls_back_writes_and_counters(self):
        self.playlist_change();self.queue(count(),Result(execute_error=ServerFailure('55P03')),one(oldest=OLD,invalid=0))
        result=self.run_page();self.assertEqual((result['scrubbed'],result['deleted'],result['deferred']),(0,0,1))
        self.assertEqual(self.connection.rollbacks,1);self.assertEqual(self.connection.commits,1)
        self.assertEqual(result['oldest_remaining_expired_source_at'],OLD)
        self.assertEqual(self.connection.queries[-1][1][0].obj,[dict(value_clock=OLD,upper_at=AT,proof_invalid=True)])
    def test_live_playlist_lease_checked_after_locks_defers_without_release(self):
        self.page();self.queue(state(),Result(),one(noop=False,active_valid=False,completed_valid=False,source_valid=False,value_clock=OLD),one(busy=True),one(oldest=OLD,invalid=0))
        self.assertEqual(self.run_page()['deferred'],1)
        self.assertFalse(any(query.startswith('UPDATE') for query,params in self.connection.queries))
        self.assertIn('clock_timestamp()',self.connection.queries[-2][0])
    def test_busy_playlist_fresh_snapshot_is_not_invalid_or_expired(self):
        self.page();self.queue(Result(execute_error=ServerFailure('55P03')),
            one(noop=True,value_clock=AT,upper_at=AT))
        result=self.run_page()
        self.assertEqual((result['deferred'],result['unknown_expiry_remaining'],result['invalid_clock_remaining']),(1,0,0))
        self.assertIsNone(result['oldest_remaining_expired_source_at'])
        self.assertEqual(result['priority_references'],[])  # no new expiry cause
        self.assertEqual(self.connection.rollbacks,1)
        self.assertFalse(any(q.startswith(('UPDATE','DELETE')) for q,p in self.connection.queries))
        observation=self.connection.queries[-1][0]
        self.assertIn('clock_timestamp()',observation)
        self.assertNotIn('FOR UPDATE',observation)

    def test_busy_playlist_unobserved_is_unknown_but_observed_bad_clock_is_not(self):
        for clock,invalid,oldest in ((OLD,0,OLD),(None,1,None),(AT,1,None),('2026-10-07T12:00:00+00:00',1,None)):
            with self.subTest(clock=clock):
                self.page();self.queue(Result(execute_error=ServerFailure('55P03')),
                    one(noop=False,value_clock=clock,upper_at=AT),one(oldest=oldest,invalid=invalid))
                result=self.run_page()
                self.assertEqual(result['unknown_expiry_remaining'],0)
                self.assertEqual((result['invalid_clock_remaining'],result['oldest_remaining_expired_source_at']),(invalid,oldest))
        self.page();self.queue(Result(execute_error=ServerFailure('55P03')),Result())
        result=self.run_page()
        self.assertEqual((result['unknown_expiry_remaining'],result['invalid_clock_remaining']),(1,0))
        self.assertIsNone(result['oldest_remaining_expired_source_at'])

    def test_mixed_old_and_fresh_busy_rows_register_only_old_cause(self):
        from db.hot.hot_retention import CleanupPage,PriorityResult
        from hot_retention import PriorityRetrySet
        other='UC0000000000000000000002'
        self.header();self.queue(Result(rows=(candidate(),dict(a=other,b='long',cursor='other'))))
        self.queue(one(at=AT),Result(execute_error=ServerFailure('55P03')),
                   one(noop=False,value_clock=OLD,upper_at=AT))
        self.queue(one(at=AT),Result(execute_error=ServerFailure('55P03')),
                   one(noop=True,value_clock=AT,upper_at=AT),one(oldest=OLD,invalid=0))
        page=CleanupPage.parse('playlist_state',self.run_page(),100)
        self.assertEqual(page.deferred,2)
        self.assertEqual([r.key for r in page.priority_references],[(CHANNEL,'long')])
        registry=PriorityRetrySet();registry.observe(page,0)
        generation,requested=registry.request()
        self.assertEqual(tuple(registry.references),page.priority_references)
        receipt=PriorityResult.parse(dict(as_of=AT,checked=[r.wire() for r in requested],remaining=[],
                                         complete=True,deleted=0,scrubbed=1),requested)
        self.assertTrue(registry.acknowledge(generation,requested,receipt,1))
        self.assertFalse(registry.unresolved)  # fresh busy neighbor cannot inherit OLD

    def test_busy_playlist_observation_failure_aborts_page_instead_of_ack(self):
        self.page();self.queue(Result(execute_error=ServerFailure('55P03')),
            Result(execute_error=ServerFailure('57014')))
        with self.assertRaises(DatabaseServerError):self.run_page()
        self.assertEqual(self.connection.commits,0)

    def test_playlist_observation_binds_exact_identity_without_payload_output(self):
        from psycopg._queries import PostgresQuery
        from psycopg.adapt import Transformer
        from db.base.postgres import _reviewed_statement
        sql=cleanup._PLAYLIST_OBSERVATION
        _reviewed_statement(sql,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(sql,dict(a=CHANNEL,b='long',cutoff=FLOOR))
        self.assertIn('channel_id=%(a)s AND video_type=%(b)s',sql)
        self.assertNotIn('%(state)s',sql)
        self.assertNotIn('%(scan_at)s',sql)
        self.assertNotIn('FOR UPDATE',sql)

    def test_receipt_current_noop_never_locks_owners(self):
        self.page('inventory_receipts');self.queue(receipt(source_hot=True))
        self.assertEqual(self.run_page('inventory_receipts')['scrubbed'],0)
        self.assertFalse(any('FOR UPDATE' in query for query,params in self.connection.queries))
    def test_receipt_parent_scan_then_receipt_lock_and_hash_invalidation(self):
        self.page('inventory_receipts');self.queue(receipt(scan_id=SCAN,has_hash=True),one(channel_id=CHANNEL,video_type='long'),Result(),Result(),receipt(scan_id=SCAN,has_hash=True),one(pinned=False),count(),count())
        self.assertEqual(self.run_page('inventory_receipts')['scrubbed'],1)
        queries=[query for query,params in self.connection.queries]
        lock_queries=[query for query in queries if 'FOR UPDATE' in query]
        self.assertEqual(len(lock_queries),3)
        self.assertIn('channel_playlist_state',lock_queries[0]);self.assertIn('channel_video_inventory_scans',lock_queries[1]);self.assertIn('typed_inventory_receipts',lock_queries[2])
        writes=[query for query in queries if query.startswith('UPDATE')]
        self.assertIn('token_history_verified=false',writes[0]);self.assertIn('claims=NULL',writes[1])
    def test_receipt_advisory_miss_preserves_observed_old_clock(self):
        self.page('inventory_receipts');self.queue(receipt(),one(acquired=False),one(oldest=OLD,invalid=0))
        result=self.run_page('inventory_receipts');self.assertEqual((result['deferred'],result['invalid_clock_remaining'],result['unknown_expiry_remaining']),(1,0,0))
        self.assertEqual(result['oldest_remaining_expired_source_at'],OLD)
        self.assertEqual(self.connection.queries[-1][1][0].obj[0]['value_clock'],OLD)
        self.assertIn('781631247',self.connection.queries[-2][0])
    def test_receipt_expired_created_at_is_deleted_not_rescrubbed(self):
        self.page('inventory_receipts');self.queue(receipt(created_hot=False),one(acquired=True),receipt(created_hot=False),one(pinned=False),count())
        result=self.run_page('inventory_receipts');self.assertEqual((result['deleted'],result['scrubbed']),(1,0))
        self.assertTrue(self.connection.queries[-1][0].startswith('DELETE'))
    def test_scan_hot_noop_precedes_parent_lock(self):
        self.page('inventory_scans');self.queue(one(hot=True))
        self.assertEqual(self.run_page('inventory_scans')['deferred'],0)
        self.assertFalse(any('FOR UPDATE' in query for query,params in self.connection.queries))
    def test_scan_live_owner_preserves_clock_read_before_locked_scan(self):
        self.page('inventory_scans');self.queue(one(hot=False,channel_id=CHANNEL,video_type='long',value_clock=OLD),state(),one(busy=True),one(oldest=OLD,invalid=0))
        result=self.run_page('inventory_scans');self.assertEqual((result['invalid_clock_remaining'],result['unknown_expiry_remaining']),(0,0))
        self.assertEqual(result['oldest_remaining_expired_source_at'],OLD)
        self.assertFalse(any('FOR UPDATE NOWAIT' in query and 'FROM public.channel_video_inventory_scans scan' in query for query,params in self.connection.queries))
    def scan_change(self):
        self.page('inventory_scans');self.queue(one(hot=False,channel_id=CHANNEL,video_type='long',value_clock=OLD),Result(),one(hot=False,value_clock=OLD),one(pinned=False))
    def test_scan_reference_keeps_scrub_and_counts_defer(self):
        self.scan_change();self.queue(count(),one(owner_reference=True),one(oldest=OLD,invalid=0))
        result=self.run_page('inventory_scans');self.assertEqual((result['scrubbed'],result['deferred'],result['deleted']),(1,1,0))
        self.assertFalse(any(query.startswith('DELETE') for query,params in self.connection.queries))
    def test_scan_unreferenced_deleted_after_bounded_checks(self):
        self.scan_change();self.queue(count(),one(owner_reference=False),one(retained_reference=False),count())
        result=self.run_page('inventory_scans');self.assertEqual((result['scrubbed'],result['deleted'],result['deferred']),(1,1,0))
    def test_nonlock_failure_aborts_whole_page_without_retry(self):
        self.scan_change();self.queue(Result(execute_error=ServerFailure('23514')))
        with self.assertRaises(DatabaseServerError):self.run_page('inventory_scans')
        self.assertEqual(self.connection.commits,0);self.assertEqual(self.pool.borrowed,1)
    def test_scan_predicate_is_fixed_native_finite_clock_expression(self):
        self.assertIn("scan.source_checked_at IS NULL AND scan.status='running'",SCAN_IS_HOT_SQL)
        self.assertIn('scan.pages_completed=0 AND scan.items_seen=0 AND scan.new_memberships_found=0',SCAN_IS_HOT_SQL)
        self.assertIn('scan.last_transition_id IS NULL AND scan.completed_at IS NULL',SCAN_IS_HOT_SQL)
        self.assertEqual(SCAN_IS_HOT_SQL.count("interval '696 hours'"),3)
        self.assertNotIn('inventory_scan_is_hot(',SCAN_IS_HOT_SQL)
        self.assertNotIn('clock_timestamp()',SCAN_IS_HOT_SQL)


if __name__=='__main__':unittest.main()
