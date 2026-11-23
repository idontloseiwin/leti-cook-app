"""Offline bounds, clock order, NOWAIT rollback and admission for root inventory cleanup."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.retention import inventory_retention as cleanup
from db.base.postgres import DatabaseError,DatabaseServerError,PostgresConfig,PostgresDatabase,_reviewed_statement
from test_direct_pg_transactions import Connection,Pool,Result,ServerFailure,DSN

AT='2026-10-06T12:00:00+00:00';LATER='2026-10-06T12:00:01+00:00'
FLOOR='2026-09-08T12:00:00+00:00';OLD='2026-09-01T12:00:00+00:00'
CHANNEL='UC0000000000000000000001';VIDEO='aaaaaaaaaaa'
def one(**values):return Result(rows=(values,))
def count(n=1):return Result(has_columns=False,rowcount=n)
def row(noop=False,video=VIDEO):return dict(video_id=video,channel_id=CHANNEL,video_type='long',cursor=video,noop=noop)
def inventory(channel=CHANNEL):return one(channel_id=channel,value='{}')
def member():return one(value='{}')


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=Connection(self.events);self.pool=Pool(self.connection,self.events)
        self.database=PostgresDatabase(PostgresConfig(DSN),pool_factory=lambda config:self.pool,admission=lambda name:None).open()
        self.addCleanup(self.database.close)
        for name in ('after_inventory_source_write','after_membership_source_write'):
            effect=patch.object(cleanup,name);setattr(self,name,effect.start());self.addCleanup(effect.stop)
    def queue(self,*items):self.connection.results.extend(items)
    def header(self,target='memberships',limit=100,cursor=None):
        self.queue(one(target=target,limit=limit,cursor=cursor,cursor_length=len(cursor) if cursor is not None else None,at=AT,cutoff=FLOOR))
    def run_page(self,target='memberships',**kwargs):return self.database.cleanup_hot_inventory(target=target,**kwargs)
    def start(self,target='memberships'):
        self.header(target);self.queue(Result(rows=(row(),)),one(noop=False))
    def owners(self,*,channel=CHANNEL,queue=False):
        self.queue(one(acquired=True),one(channel_id=CHANNEL),Result())
        self.queue(member(),one(busy=False)) if queue else self.queue(Result())
        self.queue(inventory(channel),one(at=LATER),Result())
    def test_empty_page_commits_once_and_budget_keeps_native_clock(self):
        self.header();self.queue(Result());result=self.run_page()
        self.assertEqual((result['complete'],result['scanned'],result['next_cursor']),(True,0,None))
        self.assertEqual((result['as_of'],result['cutoff']),(AT,FLOOR));self.assertEqual(self.connection.commits,1)
        self.assertIn('clock_timestamp()',self.connection.queries[0][0]);self.assertEqual(self.connection.queries[1][1]['size'],100)
        self.header('inventory');self.queue(Result());self.run_page('inventory')
        self.assertEqual(self.connection.queries[-1][1]['size'],1000)  # the list is read a thousand keys a page
    def test_target_limit_and_cursor_precedence(self):
        for target,limit,cursor,code in [('bad',None,'','target'),('inventory',0,'','limit'),('memberships',100,'','cursor')]:
            self.header(target,limit,cursor)
            with self.assertRaises(DatabaseServerError) as caught:self.run_page(target,limit=limit,after_id=cursor)
            self.assertEqual(caught.exception.message,'hot_inventory_'+code+'_invalid')
        self.assertEqual(len(self.connection.queries),3)
    def test_cursor_native_json_error_remaps_only22p02(self):
        for code in ('22P02','22023','57014'):
            self.header(cursor='bad');self.queue(Result(execute_error=ServerFailure(code)))
            with self.assertRaises(DatabaseServerError) as caught:self.run_page(after_id='bad')
            self.assertEqual(caught.exception.code,'P0001' if code=='22P02' else code)
    def test_inventory_cursor_is_unparsed_text_and_membership_cursor_is_branch_clock_and_key(self):
        self.header('inventory',cursor='any text');self.queue(Result());self.run_page('inventory',after_id='any text')
        self.assertEqual(self.connection.queries[-1][1]['after_video'],'any text')
        cursor='[1,"'+OLD+'","v","c","long"]'
        self.header(cursor=cursor);self.queue(one(valid=True,branch='1',clock=OLD,video='v',channel='c',type='long'),one(value=OLD),Result())
        self.run_page(after_id=cursor);params=self.connection.queries[-1][1]
        self.assertEqual((params['after_branch'],params['after_clock'],params['after_video'],params['after_channel'],params['after_type']),
                         (1,OLD,'v','c','long'))
        for code in ('22007','22008'):  # a clock that is not a timestamp is an invalid cursor
            self.header(cursor=cursor);self.queue(one(valid=True,branch='0',clock='x',video='v',channel='c',type='long'),
                                                  Result(execute_error=ServerFailure(code)))
            with self.assertRaises(DatabaseServerError) as caught:self.run_page(after_id=cursor)
            self.assertEqual(caught.exception.message,'hot_inventory_cursor_invalid')
    def test_membership_page_selects_only_rows_past_the_cutoff_or_from_the_future(self):
        self.header();self.queue(Result());self.run_page()
        sql=self.connection.queries[-1][0]
        self.assertIn(cleanup._MEMBER_EARLIEST+'<%(cutoff)s::timestamptz',sql)
        self.assertIn(cleanup._MEMBER_LATEST+'>%(upper)s::timestamptz AND '+cleanup._MEMBER_EARLIEST+'>=%(cutoff)s::timestamptz',sql)
        self.assertIn('ORDER BY '+cleanup._MEMBER_EARLIEST+','+cleanup._MEMBER_KEY,sql)
        self.assertNotIn('LEFT JOIN public.video_inventory_memberships',sql)
    def test_ten_key_pages_advance_without_scanning_or_owner_queries(self):
        self.header('inventory',limit=1);self.queue(Result(rows=tuple(row(True,str(i)) for i in range(10))))
        result=self.run_page('inventory',limit=1);self.assertEqual((result['scanned'],result['complete'],result['next_cursor']),(0,False,'9'))
        self.assertEqual(len(self.connection.queries),2)
    def test_scanned_limit_stops_before_next_mutable_key_but_after_noop_keys(self):
        self.header('inventory',limit=1);self.queue(Result(rows=(row(False,'1'),row(True,'2'),row(False,'3'))))
        with patch.object(cleanup,'_candidate',return_value=(0,0,0,False)) as candidate:result=self.run_page('inventory',limit=1)
        candidate.assert_called_once();self.assertEqual((result['scanned'],result['complete'],result['next_cursor']),(1,False,'2'))
    def test_mixed_list_keys_need_empty_third_probe_after_full_second_page(self):
        def key(number):return 'v'+str(number).zfill(10)
        def selected(number):
            return dict(video_id=key(number),channel_id=CHANNEL,video_type='long',noop=number not in (2,4,6),cursor=key(number))
        cursor=None;pages=[]
        with patch.object(cleanup,'_candidate',return_value=(1,0,0,False)) as candidate:
            for numbers in (range(1,21),range(6,26),()):
                self.header('inventory',limit=2,cursor=cursor)
                self.queue(Result(rows=tuple(selected(number) for number in numbers)))
                page=self.run_page('inventory',limit=2,after_id=cursor);pages.append(page);cursor=page['next_cursor']
        self.assertEqual([p['next_cursor'] for p in pages],[key(5),key(25),None])
        self.assertEqual([p['complete'] for p in pages],[False,False,True])
        self.assertEqual([p['scanned'] for p in pages],[2,1,0]);self.assertEqual([p['deleted'] for p in pages],[2,1,0])
        self.assertEqual(candidate.call_count,3)
        after=[p['after_video'] for sql,p in self.connection.queries if cleanup._INVENTORY_PAGE in sql]
        self.assertEqual(after,[None,key(5),key(25)])
    def test_row_refreshed_after_selection_noops_before_advisory_or_lease(self):
        self.header();self.queue(Result(rows=(row(),)),one(noop=True))
        result=self.run_page();self.assertEqual((result['scanned'],result['deferred']),(1,0))
        self.assertFalse(any('advisory' in sql or 'FOR UPDATE' in sql for sql,p in self.connection.queries))
        self.assertIn('clock_timestamp()',self.connection.queries[-1][0])
    def test_member_delete_owner_order_and_native_recheck(self):
        self.start();self.owners();self.queue(member(),one(source_valid=False,first_valid=False,positive_valid=False),one(value='NATIVE_DELETED'),Result())
        result=self.run_page();self.assertEqual((result['deleted'],result['scrubbed']),(1,0))
        self.assertEqual(self.after_membership_source_write.call_args.kwargs,dict(operation='delete',old_text='NATIVE_DELETED'))
        queries=[sql for sql,p in self.connection.queries];locks=[sql for sql in queries if 'FOR UPDATE' in sql]
        self.assertEqual(len(locks),5)
        for sql,name in zip(locks,('channel_playlist_state','video_collection_state','video_inventory iv','video_type_review_cases','video_inventory_memberships')):
            self.assertIn(name,sql);self.assertIn('NOWAIT',sql)
        facts=[p for sql,p in self.connection.queries if cleanup._MEMBER_FACTS in sql]
        self.assertEqual([p['upper'] for p in facts],[LATER]);self.assertTrue(all(p['cutoff']==FLOOR for p in facts))
    def test_member_scrub_changes_only_stale_first_positive_fields_and_samples_survivor(self):
        self.start();self.owners();self.queue(member(),one(source_valid=True,first_valid=False,positive_valid=True),one(value='NATIVE_NEW'),
            one(video_id=VIDEO,channel_id=CHANNEL,video_type='long',oldest=None,invalid=False))
        result=self.run_page();self.assertEqual((result['deleted'],result['scrubbed'],result['invalid_clock_remaining']),(0,1,0))
        writes=[(sql,p) for sql,p in self.connection.queries if sql.startswith('UPDATE') or cleanup._MEMBER_SCRUB_EFFECT in sql]
        self.assertEqual(len(writes),1);self.assertEqual(writes[0][0],_reviewed_statement(cleanup._MEMBER_SCRUB_EFFECT,read=True,returning=True,max_rows=1).as_string())
        self.assertEqual(self.after_membership_source_write.call_args.kwargs,dict(operation='update',old_text='{}',new_text='NATIVE_NEW'))
        self.assertFalse(writes[0][1]['first_valid']);self.assertTrue(writes[0][1]['positive_valid'])
    def test_advisory_denial_defers_and_collects_oldest_native_clock(self):
        self.start();self.queue(one(acquired=False),one(video_id=VIDEO,channel_id=CHANNEL,video_type='long',oldest=OLD,invalid=True),one(value=OLD))
        result=self.run_page();self.assertEqual((result['deferred'],result['oldest_remaining_expired_source_at'],result['invalid_clock_remaining']),(1,OLD,1))
        self.assertEqual(result['unknown_expiry_remaining'],0)  # a membership reports its own clocks
        self.assertEqual(self.connection.rollbacks,1);self.assertEqual(self.connection.commits,1)
        self.assertEqual(self.connection.queries[-1][1],([OLD],))
    def test_busy_inventory_root_has_no_independent_expiry_proof(self):
        self.start('inventory');self.queue(one(acquired=False))
        result=self.run_page('inventory')
        self.assertEqual((result['deferred'],result['unknown_expiry_remaining'],result['invalid_clock_remaining']),(1,1,0))
        self.assertIsNone(result['oldest_remaining_expired_source_at'])
        self.assertEqual(result['priority_references'],[])  # no page-oldest may be assigned to this fresh/busy root
        self.assertFalse(any(q.startswith(('UPDATE','DELETE')) for q,p in self.connection.queries))
        self.assertEqual(self.connection.rollbacks,1)

    def test_live_playlist_is_checked_after_ordered_lock_and_rolls_back_lock_scope(self):
        self.start();self.queue(one(acquired=True),one(channel_id=CHANNEL),Result(rows=({'value':'{}'},)),one(busy=True),Result())
        result=self.run_page();self.assertEqual(result['deferred'],1)
        queries=[sql for sql,p in self.connection.queries]
        self.assertTrue(any(cleanup._PLAYLISTS_BUSY in sql for sql in queries))
        self.assertFalse(any(cleanup._QUEUE+' FOR UPDATE NOWAIT' in sql for sql in queries))
    def test_identity_change_defers_before_mutation(self):
        self.start();self.queue(one(acquired=True),one(channel_id=CHANNEL),Result(),Result(),inventory('other'),Result())
        self.assertEqual(self.run_page()['deferred'],1)
        self.assertFalse(any(sql.startswith(('UPDATE','DELETE')) for sql,p in self.connection.queries))
    def test_expired_type_clears_borrowed_fields_and_schedules_identity_recovery(self):
        self.start('inventory');self.owners(queue=True)
        self.queue(one(value=False),count(),one(value=False),count(),one(value=False),count(),one(old_text='NATIVE_OLD',new_text='NATIVE_NEW'),one(value=True))
        result=self.run_page('inventory');self.assertEqual((result['scrubbed'],result['deleted'],result['deferred']),(3,0,0))
        writes=[sql for sql,p in self.connection.queries if sql.startswith(('UPDATE','INSERT')) or cleanup._WITHDRAW_TYPE_EFFECT in sql]
        self.assertEqual(len(writes),4);self.assertIn('published_at=NULL',writes[0]);self.assertIn('typed_source_published_at=CASE',writes[1])
        self.assertIn('typed_source_published_acquired_at=CASE',writes[1])
        self.assertIn('THEN state.next_due_at ELSE clock_timestamp() END',writes[2]);self.assertIn('video_type=NULL',writes[3])
        self.assertIn('state.availability_first_missing_at IS NOT NULL',writes[2])
        self.assertIn("state.paused_reason='availability_retry_expired'",writes[2])
        self.assertNotIn('availability_first_missing_at=',writes[2])
        self.assertNotIn('availability_resume_observed_at=',writes[2])
        self.assertIn('skeleton_since=coalesce',writes[3])
        self.after_inventory_source_write.assert_called_once()
    def test_children_or_unscrubbed_current_fields_defer_root_without_cascade(self):
        for children in (False,True):
            self.start('inventory');self.owners()
            self.queue(one(value=True),one(value=True),one(value=True),one(value=False),one(present=children))
            if not children:self.queue(one(payload=True))
            result=self.run_page('inventory');self.assertEqual((result['deferred'],result['deleted']),(1,0))
            self.assertEqual(result['unknown_expiry_remaining'],1)  # a held list row has no clock of its own
        self.assertFalse(any(sql.startswith('DELETE') for sql,p in self.connection.queries))
    def test_empty_root_retains_identity_queue_and_user_decision_link(self):
        self.start('inventory');self.owners();self.queue(one(value=True),one(value=True),one(value=True),one(value=False),
            one(present=False),Result(),count(0),count(),one(old_text='OLD',new_text='SKELETON'))
        result=self.run_page('inventory');self.assertEqual((result['deleted'],result['scrubbed'],result['unknown_expiry_remaining']),(0,1,0))
        self.assertEqual([sql for sql,p in self.connection.queries if sql.startswith('DELETE')],[
            'DELETE FROM public.videos WHERE video_id=%(video_id)s'])
        self.after_inventory_source_write.assert_called_once()
        self.assertNotIn('detach_review_source', (ROOT/'application/db/retention/inventory_retention.py').read_text())
    def test_late_nowait_failure_restores_prior_scrub_counters(self):
        self.start('inventory');self.owners();self.queue(one(value=False),count(),one(value=True),one(value=True),
            one(value=False),one(present=False),Result(execute_error=ServerFailure('55P03')))
        result=self.run_page('inventory');self.assertEqual((result['scrubbed'],result['deferred'],result['unknown_expiry_remaining']),(0,1,1))
        self.assertEqual(self.connection.rollbacks,1);self.assertEqual(self.connection.commits,1)
    def test_cancel_and_unknown_sql_error_abort_page_not_defer(self):
        for code in ('57014','23503'):
            self.header();self.queue(Result(rows=(row(),)),Result(execute_error=ServerFailure(code)))
            with self.assertRaises(DatabaseServerError) as error:self.run_page()
            self.assertEqual(error.exception.code,code)
        self.assertEqual(self.connection.commits,0)
    def test_unknown_commit_never_replays_and_fixed_entry_is_the_admission_name(self):
        self.header();self.queue(Result());self.connection.commit_error=__import__('psycopg').OperationalError('private')
        seen=[];self.database._admission=lambda name:seen.append(name)
        with self.assertRaises(DatabaseError) as error:self.run_page()
        self.assertTrue(error.exception.outcome_unknown);self.assertEqual(self.pool.borrowed,1)
        self.assertTrue(seen);self.assertTrue(all(name=='cleanup_hot_inventory' for name in seen))
    def test_statements_bind_native_records_and_bounded_queries(self):
        p=dict(target='inventory',after_branch=None,after_clock=None,after_video=None,after_channel=None,after_type=None,size=1000,upper=AT,cutoff=FLOOR,
            video_id=VIDEO,channel_id=CHANNEL,video_type='long',member='{}',inventory='{}',queue=None,observed_channel=CHANNEL,
            first_valid=False,positive_valid=True)
        for sql in (cleanup._MEMBERSHIP_PAGE,cleanup._INVENTORY_PAGE,cleanup._INVENTORY,cleanup._QUEUE,cleanup._MEMBER,cleanup._MEMBER_FACTS,cleanup._PLAYLISTS,
                    cleanup._CHILDREN,cleanup._CURRENT,cleanup._PREFLIGHT_INVENTORY,cleanup._PREFLIGHT_MEMBERSHIP):
            _reviewed_statement(sql,read=True,max_rows=1000);PostgresQuery(Transformer()).convert(sql,p)
        _reviewed_statement(cleanup._MEMBER_SCRUB,read=False,max_rows=0);PostgresQuery(Transformer()).convert(cleanup._MEMBER_SCRUB,p)
        self.assertEqual(cleanup._FRESH.count('LIMIT 101'),2)
        self.assertNotIn('database.call(', (ROOT/'application/db/retention/inventory_retention.py').read_text())


if __name__=='__main__':unittest.main()
