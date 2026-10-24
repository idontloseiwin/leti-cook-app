"""Offline U3 clock snapshot, fixed admission and keyless watcher regressions."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
import hashlib
import json
import os
from pathlib import Path
import sys
import stat
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from db.hot.startup_expiry import STARTUP_EXPIRY_SQL, STARTUP_EXPIRY_SOURCES
from db.hot.update_copy_sources import UPDATE_COPY_WINDOW_SQL, UPDATE_COPY_SOURCE_ROWS
from db.hot.hot_runtime import MaintenanceRepository
from db.hot.hot_retention import RetentionError
from db.base.postgres import DatabaseError, DatabaseUnavailable, PostgresConfig, PostgresDatabase, _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from update_copy_protocol import Marker,marker_bytes,marker_from_window,UpdateCopyError,WINDOW_READ_SECONDS
from update_copy_guard import UpdateCopyGuard,warning_for_status,MARKER_ENV,CIPHER_ENV
from test_direct_pg_transactions import Connection,Pool,Result,DSN
from test_runtime_gates import runtime
from ops.update_copy_window import collect_window
from collection_schedule import CollectionSchedules
from runtime.collection_schedules import schedule_environment
import hot_runtime

VALUE={'as_of':'2026-10-09T12:00:00+00:00','oldest_source_at':'2026-10-01T00:00:00+00:00',
       'expired':False,'invalid_source_clock':False}


class CopyWindowTests(unittest.TestCase):
    def test_unchanged_sources_and_extended_shared_registry_are_preserved(self):
        expected=json.loads((ROOT/'tests/fixtures/startup_expiry_u2.json').read_text())
        changed={'queue_api_copies','review_evidence','operator_jobs_and_targets'}
        added={'inventory_publication','queue_publication','queue_stream_end',
               'operator_chain_linked_scans','operator_chain_ack_receipts'}
        self.assertEqual(set(STARTUP_EXPIRY_SOURCES),(set(expected)-{'__statement__','queue_api_copies'})|added)
        self.assertEqual(set(UPDATE_COPY_SOURCE_ROWS),set(STARTUP_EXPIRY_SOURCES))
        for name in set(expected)-changed-{'__statement__'}:
            statement=STARTUP_EXPIRY_SOURCES[name]
            if name=='video_metadata':
                # Schema31 adds exactly two fields under the same metadata clock.
                # Undo only that additive predicate to retain the historical pin.
                extension=' OR publication_status IS NOT NULL OR scheduled_start_at IS NOT NULL'
                self.assertEqual(statement.count(extension),1)
                self.assertIn(extension,UPDATE_COPY_SOURCE_ROWS[name])
                statement=statement.replace(extension,'')
            self.assertEqual(hashlib.sha256(statement.encode()).hexdigest(),expected[name],name)

    def test_fixed_snapshot_passes_binding_and_readonly_guard(self):
        statement=_reviewed_statement(UPDATE_COPY_WINDOW_SQL,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(statement.as_string(),())
        self.assertNotIn('readable.',UPDATE_COPY_WINDOW_SQL)
        self.assertNotIn('FOR UPDATE',UPDATE_COPY_WINDOW_SQL)
        self.assertEqual(UPDATE_COPY_WINDOW_SQL.count('clock_timestamp()'),1)
        self.assertNotIn('strict_entry',UPDATE_COPY_WINDOW_SQL)

    def test_optional_observations_and_unresolved_references_are_distinct(self):
        self.assertIn('first_seen_scan_id IS NOT NULL',UPDATE_COPY_SOURCE_ROWS['memberships_first'])
        self.assertIn("membership_status='present'",UPDATE_COPY_SOURCE_ROWS['memberships_present'])
        self.assertIn('source.pages_completed=0',UPDATE_COPY_SOURCE_ROWS['inventory_scans_source_checked_at'])
        self.assertIn("inventory_status IN('completed','playlist_not_found')",UPDATE_COPY_SOURCE_ROWS['playlist_source'])
        for name in ('operator_jobs_linked_scans','operator_jobs_ack_receipts'):
            self.assertIn('LEFT JOIN',UPDATE_COPY_SOURCE_ROWS[name])
        self.assertIn('true AS inclusive',UPDATE_COPY_SOURCE_ROWS['category_reference'])
        self.assertIn('source_at IS NULL OR NOT isfinite(source_at)',UPDATE_COPY_WINDOW_SQL)

    def test_copied_type_needs_source_unless_it_is_user_work_and_receipt_marker_is_exact(self):
        self.assertIn("decision.decision_type=i.video_type",UPDATE_COPY_WINDOW_SQL)
        self.assertIn("m.video_type=i.video_type AND m.membership_status='present'",UPDATE_COPY_WINDOW_SQL)
        receipt=UPDATE_COPY_SOURCE_ROWS['inventory_receipts']
        self.assertIn("result IS DISTINCT FROM '{\"source_expired\":true}'::jsonb",receipt)
        self.assertNotIn("NOT(result ? 'source_expired')",receipt)

    def test_repository_strict_shape_and_finite_clocks(self):
        db=Mock(); db.read_update_copy_window.return_value=dict(VALUE)
        repo=MaintenanceRepository(db)
        self.assertEqual(repo.update_copy_window(),VALUE)
        for changes in ({'expired':1},{'invalid_source_clock':None},{'as_of':'infinity'},
                        {'oldest_source_at':'infinity'},{'oldest_source_at':VALUE['as_of'],'extra':True},
                        {'oldest_source_at':'2026-10-10T00:00:00Z'}, {'oldest_source_at':None,'expired':True}):
            db.read_update_copy_window.return_value={**VALUE,**changes}
            with self.subTest(changes=changes),self.assertRaisesRegex(RetentionError,'invalid_update_copy_window'):
                repo.update_copy_window()
        db.read_update_copy_window.return_value={**VALUE,'oldest_source_at':None}
        self.assertIsNone(repo.update_copy_window()['oldest_source_at'])
        repo.close()
        calls=db.read_update_copy_window.call_count
        with self.assertRaisesRegex(RetentionError,'maintenance_repository_closed'):repo.update_copy_window()
        self.assertEqual(db.read_update_copy_window.call_count,calls)

    def test_repository_failures_never_mean_no_sources(self):
        db=Mock();repo=MaintenanceRepository(db)
        for error,code in ((DatabaseUnavailable('database_connection_failed'),'unavailable'),
                           (DatabaseError('statement_timeout'),'failed')):
            db.read_update_copy_window.side_effect=error
            with self.assertRaisesRegex(RetentionError,'update_copy_window_'+code):repo.update_copy_window()

    def test_repository_summary_keeps_empty_and_finite_copy_deadlines(self):
        as_of=datetime.fromisoformat(VALUE['as_of']);now=int(as_of.timestamp())
        cases=((None,24*3600), (VALUE['oldest_source_at'],24*3600),
               ((as_of-timedelta(days=29)).isoformat(),23*3600))
        for oldest,lifetime in cases:
            with self.subTest(oldest=oldest):
                db=Mock();db.read_update_copy_window.return_value={**VALUE,'oldest_source_at':oldest}
                result=MaintenanceRepository(db).update_copy_window()
                self.assertEqual(result['oldest_source_at'],oldest)
                self.assertEqual(marker_from_window(result,created_at=now),Marker(now,now+lifetime))
                db.read_update_copy_window.assert_called_once_with()

    def test_repository_diagnostic_summary_cannot_authorize_invalid_copy(self):
        as_of=datetime.fromisoformat(VALUE['as_of']);now=int(as_of.timestamp())
        cases=(
            ({'oldest_source_at':None,'invalid_source_clock':True},'unconfirmed'),
            ({'invalid_source_clock':True},'unconfirmed'),
            ({'oldest_source_at':(as_of+timedelta(microseconds=1)).isoformat(),
              'invalid_source_clock':True},'unconfirmed'),
            ({'oldest_source_at':(as_of-timedelta(days=29,microseconds=1)).isoformat(),
              'expired':True},'expired'),
        )
        for changes,reason in cases:
            with self.subTest(changes=changes):
                db=Mock();db.read_update_copy_window.return_value={**VALUE,**changes}
                result=MaintenanceRepository(db).update_copy_window()
                # The repository preserves diagnostic facts, including a future
                # finite minimum. They must never turn into an empty valid copy.
                self.assertEqual(result,{**VALUE,**changes})
                with self.assertRaisesRegex(UpdateCopyError,'^update_copy_window_'+reason+'$'):
                    marker_from_window(result,created_at=now)
                db.read_update_copy_window.assert_called_once_with()

    def test_repository_fractional_oldest_keeps_deadline_after_slow_read(self):
        as_of=datetime.fromisoformat(VALUE['as_of']);now=int(as_of.timestamp())
        oldest=(as_of-timedelta(days=29)+timedelta(microseconds=999999)).isoformat()
        db=Mock();db.read_update_copy_window.return_value={**VALUE,'oldest_source_at':oldest}
        result=MaintenanceRepository(db).update_copy_window()
        self.assertEqual(result['oldest_source_at'],oldest)
        for elapsed in (0,WINDOW_READ_SECONDS):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(marker_from_window(result,created_at=now+elapsed),
                                 Marker(now+elapsed,now+23*3600))
        db.read_update_copy_window.assert_called_once_with()

    def test_driver_fixed_readonly_repeatable_read_and_no_caller_sql(self):
        trace=[];conn=Connection(trace);pool=Pool(conn,trace);gates=[]
        db=PostgresDatabase(PostgresConfig(DSN),admission=gates.append,pool_factory=lambda _:pool).open()
        try:
            conn.results.append(Result(rows=(VALUE,)))
            self.assertEqual(db.read_update_copy_window(),VALUE)
            setup=[sql for sql,params in conn.setup]
            self.assertIn('SET LOCAL ROLE service_role',setup)
            self.assertIn('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ',setup)
            self.assertIn('SET TRANSACTION READ ONLY',setup)
            self.assertEqual(conn.setup.count(('SET LOCAL jit = off',())),1)
            self.assertTrue(all(name=='get_update_copy_window' for name in gates))
            with self.assertRaises(TypeError):db.read_update_copy_window(statement='SELECT 1')
            self.assertEqual(pool.borrowed,1)
        finally:db.close()

    def test_real_gate_allows_only_fixed_window_inside_maintenance_scope(self):
        from db.base import postgres
        conn=Connection([]);pool=Pool(conn,[])
        db=PostgresDatabase(PostgresConfig(DSN),pool_factory=lambda _:pool).open()
        self.addCleanup(db.close)
        environment={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres',
                     'LIC_DATABASE_ID':'91f5d9f3-9230-4ee6-b170-155d4c3c1719',
                     'LIC_HOT_RUNTIME_SECRET':'internal_runtime_secret_0123456789'}
        controller=Mock()
        for name in ('require_ready','require_collection_ready','require_database_ready'):
            getattr(controller,name).side_effect=hot_runtime.HotRuntimeUnavailable()
        with patch.dict(os.environ,environment,clear=True),patch.object(hot_runtime,'_local_controller',controller):
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):db.read_update_copy_window()
            with hot_runtime.maintenance_access():
                conn.results.append(Result(rows=(dict(VALUE),)))
                self.assertEqual(db.read_update_copy_window(),VALUE)
                with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                    db.read_row('get_update_copy_window','SELECT 1')
                with self.assertRaises(hot_runtime.HotRuntimeUnavailable):
                    hot_runtime.require_hot_runtime_ready(operation='provider')
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):db.read_update_copy_window()
        self.assertEqual(pool.borrowed,1)

    def test_cli_owns_pool_and_checks_contract_before_fixed_read(self):
        events=[]
        class Repository:
            def __enter__(self):events.append('open');return self
            def contract(self):events.append('contract')
            def update_copy_window(self):events.append('window');return dict(VALUE)
            def __exit__(self,*args):events.append('close')
        factory=Mock(return_value=Repository())
        env={'LIC_DEPLOYMENT_PROFILE':'self-hosted','LIC_DATABASE_TRANSPORT':'postgres','LIC_DATABASE_DSN':DSN,
             **schedule_environment(CollectionSchedules())}
        self.assertEqual(collect_window(env,repository_factory=factory),VALUE)
        self.assertEqual(events,['open','contract','window','close'])
        self.assertIsNotNone(factory.call_args.kwargs['cadence_snapshot'])


class EmptyReplacedScanTests(unittest.TestCase):
    """Execute the unchanged portable owner expressions, not a Python copy."""
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute("ATTACH DATABASE ':memory:' AS public")
        self.db.execute("""CREATE TABLE public.channel_video_inventory_scans (
            scan_id TEXT, status TEXT, coverage TEXT, error_code TEXT,
            source_checked_at TEXT, pages_completed INTEGER, items_seen INTEGER,
            new_memberships_found INTEGER, resume_page_token TEXT,
            last_transition_id TEXT, last_transition_kind TEXT, completed_at TEXT,
            token_history_verified INTEGER)""")
        self.db.execute('CREATE TABLE public.channel_playlist_state (active_scan_id TEXT,last_completed_scan_id TEXT)')
        self.empty=dict(scan_id='empty',status='abandoned',coverage='partial',
            error_code='full_initial_generation_replaced',source_checked_at=None,
            pages_completed=0,items_seen=0,new_memberships_found=0,resume_page_token=None,
            last_transition_id=None,last_transition_kind=None,completed_at=None,
            token_history_verified=True)
        linked=UPDATE_COPY_SOURCE_ROWS['operator_jobs_linked_scans']
        self.required=linked.split('(source.source_checked_at,',1)[1].split('),\n   (source.completed_at',1)[0]
        child=UPDATE_COPY_SOURCE_ROWS['operator_chain_linked_scans']
        child_required=child.split('(source.source_checked_at,',1)[1].split('),\n   (source.completed_at',1)[0]
        self.assertEqual(child_required,self.required,'Parent and child use the same source-clock guard.')

    def observe(self,changes=None,*,active=None,completed=None):
        self.db.execute('DELETE FROM public.channel_video_inventory_scans')
        self.db.execute('DELETE FROM public.channel_playlist_state')
        values={**self.empty,**(changes or {})}
        columns=','.join(values)
        placeholders=','.join('?' for _ in values)
        self.db.execute(f'INSERT INTO public.channel_video_inventory_scans ({columns}) VALUES ({placeholders})',tuple(values.values()))
        self.db.execute('INSERT INTO public.channel_playlist_state VALUES (?,?)',(active,completed))
        scan_rows=self.db.execute(UPDATE_COPY_SOURCE_ROWS['inventory_scans_source_checked_at']).fetchall()
        required=self.db.execute('SELECT '+self.required+' FROM public.channel_video_inventory_scans source').fetchone()[0]
        return scan_rows,bool(required)

    def test_empty_replaced_scan_has_no_missing_source_in_either_owner(self):
        self.assertEqual(self.observe(),([],False))
        self.assertEqual(self.observe(active='another',completed='another'),([],False))

    def test_original_running_attempt_remains_optional(self):
        self.assertEqual(self.observe({'status':'running','error_code':None},active='empty'),([],False))

    def test_progress_transitions_and_other_terminal_states_still_need_source(self):
        changes=(
            {'status':'failed'},{'status':'completed'},{'status':None},
            {'coverage':'complete'},{'coverage':None},
            {'error_code':'inventory_error'},{'error_code':None},
            {'pages_completed':1},{'pages_completed':None},
            {'items_seen':1},{'items_seen':None},
            {'new_memberships_found':1},{'new_memberships_found':None},
            {'resume_page_token':'opaque-token'},
            {'last_transition_id':'transition'},{'last_transition_kind':'page'},
            {'last_transition_kind':'failure'},{'completed_at':'2026-10-14T00:00:00Z'},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.observe(change),([(None,False)],True))

    def test_referenced_replaced_scan_is_not_treated_as_empty(self):
        self.assertEqual(self.observe(active='empty'),([(None,False)],True))
        self.assertEqual(self.observe(completed='empty'),([(None,False)],True))

    def test_observed_clocks_are_never_discarded_by_the_exception(self):
        for stamp in ('2026-10-14T00:00:00Z','2099-01-01T00:00:00Z','infinity','-infinity'):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.observe({'source_checked_at':stamp}),([(stamp,False)],True))

    def test_missing_joined_scan_still_requires_source_and_other_clocks_are_kept(self):
        self.assertTrue(self.observe(dict.fromkeys(self.empty))[1])
        for owner in ('operator_jobs_linked_scans','operator_chain_linked_scans'):
            linked=UPDATE_COPY_SOURCE_ROWS[owner]
            self.assertIn('(source.started_at,true),(source.scan_boundary_at,true)',linked,owner)
            self.assertIn("(source.completed_at,source.status='completed')",linked,owner)
        for name,query in UPDATE_COPY_SOURCE_ROWS.items():
            if name not in ('inventory_scans_source_checked_at','operator_jobs_linked_scans','operator_chain_linked_scans'):
                self.assertNotIn('full_initial_generation_replaced',query,name)


class CopyGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.marker=self.root/'marker';self.cipher=self.root/'cipher'
        self.marker.mkdir();self.cipher.mkdir();self.wall=1000.;self.mono=500.
        self.guard=UpdateCopyGuard(self.marker,self.cipher,strict_owner=False,
                                  wall_time=lambda:self.wall,monotonic=lambda:self.mono)
        if os.name == 'nt':
            # Windows cannot open directory FDs. Exercise watcher behavior with
            # real files here; Linux O_PATH/dir_fd/UID permissions are a native
            # test, rather than weakening the production directory open.
            def directory(path):
                if path is None or path.is_symlink() or not stat.S_ISDIR(path.lstat().st_mode):
                    raise ValueError('invalid_test_directory')
                return None
            adapter=patch.object(self.guard,'_directory',side_effect=directory)
            adapter.start();self.addCleanup(adapter.stop)
    def create(self,*,expires=1100,raw=None):
        (self.marker/'marker.json').write_bytes(marker_bytes(Marker(1000,expires)) if raw is None else raw)
        (self.cipher/'copy.enc').write_bytes(b'ciphertext; never opened by watcher')
    def tick(self,seconds=1):
        self.wall+=seconds;self.mono+=seconds;return self.guard.check()

    def test_current_copy_warns_without_reading_ciphertext_or_key(self):
        self.create();key=self.root/'key';key.write_bytes(b'untouched')
        self.assertEqual(self.guard.check(),{'status':'current','expires_at':1100})
        self.assertEqual(warning_for_status(self.guard.status())['code'],'update_copy_present')
        self.assertEqual(key.read_bytes(),b'untouched')
        self.assertTrue((self.cipher/'copy.enc').exists())

    def test_expiry_removes_only_two_fixed_cipher_files(self):
        self.create();(self.cipher/'copy.partial').write_bytes(b'partial');other=self.cipher/'keep';other.write_bytes(b'keep')
        self.guard.check();self.tick(100)
        self.assertEqual(self.guard.status()['status'],'removed')
        self.assertFalse((self.cipher/'copy.enc').exists());self.assertFalse((self.cipher/'copy.partial').exists())
        self.assertEqual(other.read_bytes(),b'keep');self.assertTrue((self.marker/'marker.json').exists())
        self.assertIsNone(warning_for_status(self.guard.status()))

    def test_missing_or_invalid_marker_cannot_keep_ciphertext_forever(self):
        for raw in (b'{}',b'{"created_at":1000,"created_at":1000,"expires_at":1100}',b'x'*513):
            self.create(raw=raw);self.guard.check(force=True)
            self.assertFalse((self.cipher/'copy.enc').exists())
        self.create();(self.marker/'marker.json').unlink();self.guard.check(force=True)
        self.assertFalse((self.cipher/'copy.enc').exists())

    def test_partial_only_is_removed_and_absence_is_idempotent(self):
        (self.cipher/'copy.partial').write_bytes(b'partial')
        self.assertEqual(self.guard.check()['status'],'removed')
        self.assertEqual(self.tick()['status'],'absent')

    def test_wall_rollback_does_not_extend_live_monotonic_deadline(self):
        self.create();self.guard.check()
        self.wall=1050;self.mono+=101
        self.assertEqual(self.guard.check()['status'],'removed')

    def test_monotonic_rollback_does_not_hide_behind_poll_backoff(self):
        self.create();self.guard.check();self.mono-=1
        self.assertEqual(self.guard.check()['status'],'removed')

    def test_restart_after_expiry_and_future_creation_fail_closed(self):
        self.create();self.wall=1100
        self.assertEqual(self.guard.check()['status'],'removed')
        self.create();self.wall=999
        self.assertEqual(self.guard.check(force=True)['status'],'removed')

    def test_invalid_configuration_never_uses_arbitrary_environment_paths(self):
        self.create()
        self.assertEqual(UpdateCopyGuard.from_environment({}).check()['status'],'disabled')
        for env in ({MARKER_ENV:str(self.marker)}, {MARKER_ENV:str(self.marker),CIPHER_ENV:str(self.cipher)}):
            guard=UpdateCopyGuard.from_environment(env)
            self.assertEqual(guard.check()['status'],'error')
            self.assertTrue((self.cipher/'copy.enc').exists())

    def test_deletion_failure_is_warning_and_retried_without_opening_db_gate(self):
        self.create(expires=1001);self.wall=1002
        with patch.object(self.guard,'_unlink',side_effect=PermissionError()):
            self.assertEqual(self.guard.check()['status'],'error')
        self.assertEqual(warning_for_status(self.guard.status())['code'],'update_copy_cleanup_failed')
        self.assertEqual(self.tick()['status'],'removed')

    def test_status_never_waits_for_filesystem_check(self):
        self.create();entered=threading.Event();release=threading.Event()
        original=self.guard._observe
        def blocked():entered.set();release.wait(2);return original()
        with patch.object(self.guard,'_observe',side_effect=blocked):
            thread=threading.Thread(target=self.guard.check);thread.start();self.assertTrue(entered.wait(1))
            self.assertEqual(self.guard.status()['status'],'absent')
            self.assertEqual(self.guard.check()['status'],'absent')
            release.set();thread.join(2);self.assertFalse(thread.is_alive())
            self.assertEqual(self.guard.status()['status'],'current')

    def test_watcher_runs_before_db_schema_time_and_backoff_early_returns(self):
        for mode in ('schema','database','backoff','time'):
            with self.subTest(mode=mode):
                c,clocks=runtime();c.update_copy_guard=self.guard
                if mode in ('schema','database'):
                    c.repository.error='runtime_contract_schema_invalid' if mode=='schema' else 'runtime_contract_unavailable'
                c.initialize()
                if mode=='backoff':c._failed=True;c._retry_at=clocks.mono+600
                if mode=='time':c.time_trust.state='mismatch'
                self.create(expires=1001);self.wall=1002;self.mono+=1
                c.step()
                self.assertFalse((self.cipher/'copy.enc').exists())
                if mode in ('schema','database'):
                    self.assertFalse(c.status()['api_read']['allowed'])
                self.wall=1000


class CopySystemWarningTests(unittest.TestCase):
    def test_current_copy_and_cleanup_failure_warn_without_rewriting_pipeline_state(self):
        from test_honest_status import summary,health,FAILED_RUNS
        for state,code in (('current','update_copy_present'),('error','update_copy_cleanup_failed')):
            with self.subTest(state=state),patch.object(hot_runtime,'current_update_copy_status',
                    return_value={'status':state,'expires_at':1100 if state=='current' else None}):
                result=summary(paused=False)
                self.assertEqual([warning['code'] for warning in result['warnings']].count(code),1)
                self.assertEqual(result['status'],'error','Existing collection errors remain visible.')
                self.assertEqual(health(paused=False),FAILED_RUNS)
                self.assertEqual(summary(paused=True)['status'],'paused')

    def test_removed_copy_has_no_persistent_warning(self):
        from test_honest_status import summary
        with patch.object(hot_runtime,'current_update_copy_status',return_value={'status':'removed','expires_at':None}):
            self.assertFalse(any(w['code'].startswith('update_copy_') for w in summary(paused=False)['warnings']))


class CopyLifecycleTests(unittest.TestCase):
    def test_initial_guard_precedes_database_open_even_when_open_fails(self):
        from web.lifecycle import create_lifespan
        events=[]
        def fail_open():
            events.append('database_open')
            raise RuntimeError('synthetic_db_failure')
        guard=Mock();guard.check.side_effect=lambda **_:events.append('guard')
        core=SimpleNamespace(database=SimpleNamespace(open=fail_open,close=lambda:events.append('database_close')))
        cache=SimpleNamespace(clear_read_cache=lambda:None,catalogue=Mock())
        recorder=SimpleNamespace(close=lambda:events.append('recorder_close'))
        lifespan=create_lifespan(core=core,cache=cache,YOUTUBE_QUOTA_ATTEMPT_RECORDER=recorder)
        async def run():
            with patch.object(UpdateCopyGuard,'from_environment',return_value=guard):
                async with lifespan(SimpleNamespace()):
                    self.fail('Database startup failed but lifespan opened.')
        with self.assertRaisesRegex(RuntimeError,'synthetic_db_failure'):asyncio.run(run())
        self.assertEqual(events,['guard','database_open','recorder_close','database_close'])
        guard.check.assert_called_once_with(force=True)

    def test_production_directory_open_checks_owner_and_never_follows_final_symlink(self):
        guard=UpdateCopyGuard()
        target=Mock();target.is_symlink.return_value=False
        with patch('update_copy_guard.os.open',return_value=5) as opened,patch('update_copy_guard.os.fstat',
                return_value=SimpleNamespace(st_mode=stat.S_IFDIR|0o730,st_uid=0)),patch('update_copy_guard.os.close'):
            self.assertEqual(guard._directory(target),5)
            flags=opened.call_args.args[1]
            self.assertEqual(flags & getattr(os,'O_NOFOLLOW',0),getattr(os,'O_NOFOLLOW',0))
        with patch('update_copy_guard.os.open',return_value=5),patch('update_copy_guard.os.fstat',
                return_value=SimpleNamespace(st_mode=stat.S_IFDIR|0o730,st_uid=10001)),patch('update_copy_guard.os.close') as closed:
            with self.assertRaisesRegex(ValueError,'directory_invalid'):guard._directory(target)
            closed.assert_called_once_with(5)
        target.is_symlink.return_value=True
        with patch('update_copy_guard.os.open') as opened:
            with self.assertRaisesRegex(ValueError,'directory_invalid'):guard._directory(target)
            opened.assert_not_called()


if __name__=='__main__':unittest.main()
