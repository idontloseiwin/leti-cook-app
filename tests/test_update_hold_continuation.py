"""Signed hold -> bounded worker handoff, with no server or provider calls.

The transaction tests use the real adapter and recording transport doubles.
PostgreSQL commit/rollback and the installed supervisor still need native proof.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from contextvars import copy_context
import hashlib
import hmac
import io
import os
from pathlib import Path
import sys
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import hot_runtime as hot
import collection_shutdown as shutdown
import test_direct_pg_transactions as transport
import test_collector_handoff as handoff
import test_initial_video_parallel as parallel
from db.base import postgres as pg
from collection_schedule import CollectionSchedules

CONFIG = hot.HotAdmissionConfig('91f5d9f3-9230-4ee6-b170-155d4c3c1719', 'internal_runtime_secret_0123456789')
URL = 'http://127.0.0.1:8009/internal/hot-runtime'


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.mono = 1_800_000_000.0, 100.0
        self.reason = 'update_hold'
        self.fetch = Mock(side_effect=self.proof)
        self.remote = self.reader(self.fetch)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(shutdown, '_requested', Event()))
        self.stack.enter_context(patch('telemetry.emit_event'))
        self.stack.enter_context(patch.dict(os.environ, {
            'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
            'LIC_DATABASE_ID': CONFIG.database_id, 'LIC_HOT_RUNTIME_SECRET': CONFIG.secret,
            'LIC_HOT_RUNTIME_URL': URL,
        }))

    def proof(self):
        controller = SimpleNamespace(status=lambda: {
            'status': 'ready', 'epoch': 'a' * 32,
            'api_read': {'allowed': True, 'reason': 'ready'},
            'user_work': {'allowed': True, 'reason': 'ready'},
            'collection': {'allowed': self.reason == 'ready', 'reason': self.reason},
        })
        return hot.signed_status(controller, admission=CONFIG, now=self.wall)

    def reader(self, fetch):
        return hot.RemoteAdmission(URL, admission=CONFIG, fetch=fetch,
            clock=lambda: self.wall, monotonic=lambda: self.mono)

    def advance(self):
        self.wall += 2
        self.mono += 2

    def signed(self, **changes):
        value = {**self.proof(), **changes}
        body = {key: item for key, item in value.items() if key != 'signature'}
        value['signature'] = hmac.new(CONFIG.secret.encode(), b'lic-hot-runtime-v2\0' + hot._canonical(body),
            hashlib.sha256).hexdigest()
        return value

    @contextmanager
    def wired(self):
        with patch.object(hot, '_local_controller', None), patch.object(hot, '_remote', self.remote), \
             patch.object(hot, '_remote_configuration', (URL, CONFIG)):
            yield

    def test_fresh_and_cached_hold_need_explicit_scope_and_never_change_proof(self):
        with self.assertRaises(hot.HotRuntimeUnavailable):
            self.remote.require_collection_ready()
        with hot.supervised_collection_handoff(shutdown.request_handoff):
            self.remote.require_collection_ready()
            self.remote.require_collection_ready()
            self.assertTrue(shutdown.handoff_requested())
        self.assertEqual(self.remote._access['collection'], {'allowed': False, 'reason': 'update_hold'})
        self.fetch.assert_called_once()
        with self.assertRaises(hot.HotRuntimeUnavailable):
            self.remote.require_collection_ready()
        self.advance()
        with hot.supervised_collection_handoff(shutdown.request_handoff):
            self.remote.require_collection_ready()
        self.assertEqual(self.fetch.call_count, 2)

    def test_callback_failure_denies_fresh_and_cached_without_leaking_exception(self):
        callback = Mock(side_effect=ValueError('private detail'))
        with hot.supervised_collection_handoff(callback):
            for _ in range(2):
                with self.assertRaisesRegex(hot.HotRuntimeUnavailable, '^hot_runtime_not_ready$'):
                    self.remote.require_collection_ready()
        self.assertEqual(callback.call_count, 2)

    def test_other_reasons_still_deny_after_accepted_hold(self):
        for reason in hot.ACCESS_REASONS - {'ready', 'update_hold', 'collection_priority'}:
            with self.subTest(reason=reason):
                self.reason = 'update_hold'
                remote = self.reader(self.proof)
                with hot.supervised_collection_handoff(shutdown.request_handoff):
                    remote.require_collection_ready()
                    self.reason = reason
                    self.advance()
                    with self.assertRaises(hot.HotRuntimeUnavailable):
                        remote.require_collection_ready()

    def test_bad_proofs_cannot_request_or_allow_handoff(self):
        cases = [
            {**self.proof(), 'signature': 'wrong'}, self.signed(database='foreign'),
            self.signed(issued_at=self.wall - 10, expires_at=self.wall - 5),
            self.signed(issued_at=self.wall + 10, expires_at=self.wall + 15),
            {**self.proof(), 'expires_at': float('inf')}, {**self.proof(), 'issued_at': float('nan')},
            self.signed(collection={'allowed': True, 'reason': 'update_hold'}), {},
        ]
        callback = Mock()
        with hot.supervised_collection_handoff(callback):
            for value in cases:
                with self.subTest(fields=list(value)):
                    with self.assertRaises(hot.HotRuntimeUnavailable):
                        self.reader(lambda: value).require_collection_ready()
            with self.assertRaises(hot.HotRuntimeUnavailable):
                self.reader(Mock(side_effect=OSError('offline'))).require_collection_ready()
        callback.assert_not_called()

    def test_cached_hold_cannot_survive_clock_jump_or_failed_refetch(self):
        with hot.supervised_collection_handoff(shutdown.request_handoff):
            self.remote.require_collection_ready()
            self.wall += 10
            self.fetch.side_effect = OSError('offline')
            with self.assertRaises(hot.HotRuntimeUnavailable):
                self.remote.require_collection_ready()
        self.assertEqual(self.fetch.call_count, 2)

    def test_callback_is_not_a_user_work_or_api_override(self):
        callback = Mock()
        with hot.supervised_collection_handoff(callback):
            for access in ('api_read', 'user_work'):
                value = self.signed(**{access: {'allowed': False, 'reason': 'update_hold'}},
                    status='blocked' if access == 'api_read' else 'ready')
                with self.assertRaises(hot.HotRuntimeUnavailable):
                    self.reader(lambda: value).require_ready(access=access)
        callback.assert_not_called()

    def test_context_propagates_explicitly_and_does_not_leak_to_plain_thread(self):
        def denied():
            with self.assertRaises(hot.HotRuntimeUnavailable):
                self.remote.require_collection_ready()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with hot.supervised_collection_handoff(shutdown.request_handoff):
                executor.submit(copy_context().run, self.remote.require_collection_ready).result(timeout=2)
                executor.submit(denied).result(timeout=2)
            executor.submit(denied).result(timeout=2)
        self.reason = 'ready'
        self.advance()
        self.remote.require_collection_ready()
        self.assertTrue(shutdown.handoff_requested(), 'removing hold must not restart the old pass')

    def test_local_http_controller_never_uses_worker_callback(self):
        from test_runtime_gates import runtime
        controller, _ = runtime()
        controller.initialize()
        controller.update_hold_guard = Mock(check=lambda: {'state': 'held', 'operation_id': 'a' * 32})
        callback = Mock()
        with patch.object(hot, '_local_controller', controller), hot.supervised_collection_handoff(callback):
            with self.assertRaises(hot.HotRuntimeUnavailable):
                hot.require_hot_runtime_ready(operation='provider')
        callback.assert_not_called()

    def database(self):
        events = []
        connection = transport.Connection(events)
        pool = transport.Pool(connection, events)
        database = pg.PostgresDatabase(pg.PostgresConfig(transport.DSN), pool_factory=lambda config: pool)
        database.open()
        self.addCleanup(database.close)
        return database, connection, pool

    def test_real_transaction_finishes_current_write_and_final_ack_under_hold(self):
        database, connection, pool = self.database()
        connection.results.extend(transport.Result(has_columns=False, rowcount=1) for _ in range(3))
        self.reason = 'ready'
        with self.wired(), hot.supervised_collection_handoff(shutdown.request_handoff):
            with database.transaction(pg.Operation('offline_current_unit')) as tx:
                tx.execute('UPDATE public.channels SET is_hidden=%s', (False,))
                # Synthetic provider response has arrived; the real adapter now
                # sees the newly signed hold before dispatching completion.
                self.reason = 'update_hold'
                self.advance()
                tx.execute('UPDATE public.channels SET is_hidden=%s', (False,))
            self.assertTrue(shutdown.stop_at_boundary({}))
            with database.transaction(pg.Operation('offline_final_ack')) as tx:
                tx.execute('UPDATE public.sync_runs SET status=%s WHERE id=%s', ('partial', 1))
        self.assertEqual((connection.commits, connection.rollbacks), (2, 0))
        self.assertEqual(pool.borrowed, pool.returned)
        self.assertEqual(len(connection.queries), 3)

    def test_non_hold_gate_during_unit_rolls_back_and_unknown_commit_stays_error(self):
        for reason in ('time_mismatch', 'clock_changed', 'database_unavailable', 'schema_invalid'):
            with self.subTest(reason=reason):
                database, connection, pool = self.database()
                connection.results.append(transport.Result(has_columns=False, rowcount=1))
                self.reason = 'ready'
                self.advance()
                with self.wired(), hot.supervised_collection_handoff(shutdown.request_handoff):
                    with self.assertRaises(hot.HotDatabaseAdmissionDenied):
                        with database.transaction(pg.Operation('offline_current_unit')) as tx:
                            tx.execute('UPDATE public.channels SET is_hidden=%s', (False,))
                            self.reason = reason
                            self.advance()
                            tx.execute('UPDATE public.channels SET is_hidden=%s', (False,))
                self.assertEqual((connection.commits, connection.rollbacks), (0, 1))
                self.assertEqual(len(connection.queries), 1)
                self.assertEqual(pool.borrowed, pool.returned)
        database, connection, _ = self.database()
        connection.results.append(transport.Result(has_columns=False, rowcount=1))
        connection.commit_error = transport.ServerFailure('08006')
        self.reason = 'update_hold'
        self.advance()
        with self.wired(), hot.supervised_collection_handoff(shutdown.request_handoff):
            with self.assertRaises(pg.DatabaseUnavailable) as caught:
                with database.transaction(pg.Operation('offline_final_ack')) as tx:
                    tx.execute('UPDATE public.sync_runs SET status=%s WHERE id=%s', ('partial', 1))
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(connection.commits, 0)

    def test_initial_parallel_context_finishes_accepted_window_and_stops_replenishment(self):
        repository = parallel.AtomicRepository()
        barrier, lock = Barrier(8), Lock()
        calls = []
        def fetch(ids, phase):
            with lock:
                calls.append(list(ids))
            barrier.wait(timeout=5)
            self.remote.require_collection_ready()
            return [parallel.video(value) for value in ids]
        collector = parallel.service(repository, fetch)
        with hot.supervised_collection_handoff(shutdown.request_handoff), shutdown.finish_current_chunk():
            result = parallel.run(collector, [f'video{i:06d}' for i in range(400)])
            self.assertFalse(shutdown.yield_requested())
        self.assertTrue(shutdown.yield_requested())
        self.assertEqual((len(calls), len(repository.claims), len(repository.commits)), (8, 8, 8))
        self.assertEqual(result['updated'], 400)
        self.assertEqual(result['unstarted_video_ids'], [])
        self.assertTrue(result['yield_requested'])
        self.assertFalse(result['settlement_unknown'])

    def test_parallel_hold_releases_queued_undispatched_claims(self):
        repository = parallel.AtomicRepository()
        claimed, gate = Event(), Event()
        repository.claim_hook = lambda count: claimed.set() if count == 8 else None
        calls = []
        def fetch(ids, phase):
            calls.append(list(ids))
            self.assertTrue(claimed.wait(5))
            self.remote.require_collection_ready()
            return [parallel.video(value) for value in ids]
        ids = [f'video{i:06d}' for i in range(400)]
        with hot.supervised_collection_handoff(shutdown.request_handoff), shutdown.finish_current_chunk(), \
             patch.object(parallel.initial, 'ThreadPoolExecutor', parallel.gated_executor(gate, 1)), \
             patch.object(parallel.initial, '_release_unsent', side_effect=repository.release):
            result = parallel.run(parallel.service(repository, fetch), ids, progress=lambda summary: gate.set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(repository.commits), 1)
        self.assertEqual(set(repository.released), set(ids[50:]))
        self.assertEqual(result['unstarted_video_ids'], ids[50:])
        self.assertTrue(result['yield_requested'])
        self.assertFalse(result['settlement_unknown'])

    def test_operator_hold_seen_during_schema_prevents_first_chunk_and_releases_lock(self):
        def schema():
            self.remote.require_collection_ready()
            return {'ready': True, 'schema_version': 7, 'initial_quota_limit_enforced': False}
        repository = SimpleNamespace(database=object(), schema_status=schema)
        chunk, factory, lock = Mock(), Mock(), Mock()
        ns = handoff.OperatorBoundaryTests.worker_namespace(self, repository, chunk, factory, lock)
        with hot.supervised_collection_handoff(shutdown.request_handoff), patch('sys.stdout', io.StringIO()):
            self.assertEqual(ns['main'](drain=True, supervised=True, monotonic=lambda: 0), 0)
        chunk.assert_not_called()
        factory.assert_not_called()
        lock.close.assert_called_once_with()

    def test_child_observing_hold_before_work_exits_but_unsupervised_or_invalid_cannot(self):
        from ops import self_hosted_collect as entry
        import self_hosted_configuration as configuration
        core = SimpleNamespace(COLLECTION_SCHEDULES=CollectionSchedules())
        store = Mock(read=lambda: {'prepared': True, 'settings': {'automatic_collection': True}})
        with self.wired(), patch.dict(sys.modules, {'core': core}), \
             patch.object(configuration, 'ConfigurationStore', return_value=store), \
             patch('self_hosted_settings.worker_environment', return_value={}), \
             patch.object(shutdown, 'start_control_reader'), patch.object(sys, 'stdin', SimpleNamespace(buffer=None)), \
             patch.object(sys, 'argv', ['self_hosted_collect.py', 'auto']), \
             patch.dict(os.environ, {'LIC_COLLECTION_SUPERVISED': '1', 'LIC_SETUP_DIRECTORY': 'unused'}):
            self.assertEqual(entry.main(), 0)
            self.assertIsNone(hot._SUPERVISED_HANDOFF.get())
            with patch.dict(os.environ, {'LIC_COLLECTION_SUPERVISED': '0'}):
                with self.assertRaises(hot.HotRuntimeUnavailable):
                    entry.main()
            self.reason = 'time_mismatch'
            self.advance()
            with self.assertRaises(hot.HotRuntimeUnavailable):
                entry.main()

    def run_auto(self, cycle, finish, *, close_error=False):
        import auto_sync
        from collection import api
        from runtime import process_collection
        context = SimpleNamespace(database=object())
        events = []
        @contextmanager
        def resources():
            events.append('open')
            try:
                yield
            finally:
                events.append('close')
                if close_error:
                    raise RuntimeError('resource_close_failed')
        @contextmanager
        def scope(**kwargs):
            yield context
        core = SimpleNamespace(process_database_resources=resources)
        with patch.dict(sys.modules, {'core': core}), \
             patch.object(sys, 'argv', ['auto_sync.py']), \
             patch.object(process_collection, 'process_collection_configuration', return_value=(None, None)), \
             patch.object(process_collection, 'process_collection_scope', scope), \
             patch.object(api, 'start_sync_run', return_value=1), \
             patch.object(api, 'run_auto_sync_cycle', side_effect=lambda **kwargs: cycle()), \
             patch.object(api, 'finish_sync_run', side_effect=finish), \
             patch('sys.stdout', io.StringIO()):
            code = auto_sync.main()
        self.assertEqual(events, ['open', 'close'])
        return code

    def test_auto_real_finalization_policy_composes_with_guarded_transaction(self):
        from collection import runs
        database, connection, pool = self.database()
        connection.results.append(transport.Result(has_columns=False, rowcount=1))
        summary = {'errors': 0, 'yield_requested': True}
        def ack(run_id, values):
            with database.transaction(pg.Operation('offline_final_ack')) as tx:
                tx.execute('UPDATE public.sync_runs SET status=%s WHERE id=%s', (values['status'], run_id))
            return True
        repository = SimpleNamespace(finish=ack)
        with self.wired(), hot.supervised_collection_handoff(shutdown.request_handoff), \
             patch.object(runs, 'SyncRunRepository', return_value=repository):
            code = self.run_auto(lambda: summary, runs.finish_sync_run)
        self.assertEqual(code, 0)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(pool.borrowed, pool.returned)
        self.assertTrue(shutdown.handoff_requested())

    def test_auto_failed_final_receipt_and_prior_errors_cannot_be_clean_exit(self):
        from collection import runs
        for result, errors, expected in ((True, 0, 0), (False, 0, 1), (True, 2, 1)):
            with self.subTest(result=result, errors=errors):
                summary = {'errors': errors}
                finish = Mock(return_value=result)
                self.assertEqual(self.run_auto(lambda: summary, finish), expected)
                finish.assert_called_once()
        # The actual fail-soft owner returns False for a lost final ACK. The
        # entrypoint must now count that failure rather than reporting exit0.
        repository = SimpleNamespace(finish=Mock(side_effect=pg.DatabaseUnavailable(
            'database_connection_failed', outcome_unknown=True)))
        with patch.object(runs, 'SyncRunRepository', return_value=repository):
            self.assertEqual(self.run_auto(lambda: {'errors': 0}, runs.finish_sync_run), 1)
        repository.finish.assert_called_once()

    def test_resource_close_failure_escapes_instead_of_becoming_normal_exit(self):
        with self.assertRaisesRegex(RuntimeError, '^resource_close_failed$'):
            self.run_auto(lambda: {'errors': 0}, Mock(return_value=True), close_error=True)


if __name__ == '__main__':
    unittest.main()
