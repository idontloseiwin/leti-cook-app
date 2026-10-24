"""Direct database client ownership and recovery; offline, without socket or SQL I/O.

The historical filename is retained while its HTTP-only tests are replaced.
"""
import ast
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from collection_schedule import CollectionSchedules
from db.base import client
from db.base.postgres import DatabaseServerError, DatabaseUnavailable, PostgresConfig, PostgresDatabase
from db.hot.hot_runtime import maintenance_repository, RetentionError

DSN = 'host=localhost port=5432 dbname=offline user=authenticator password=synthetic-only'


class ClientTests(unittest.TestCase):
    def setUp(self):
        forbidden = patch('socket.socket.connect', side_effect=AssertionError('network forbidden'))
        forbidden.start()
        self.addCleanup(forbidden.stop)

    def test_config_requires_explicit_direct_transport_and_never_adopts_http_credentials(self):
        for values in ({}, {'SUPABASE_URL': 'http://offline.invalid', 'SUPABASE_KEY': 'old'},
                       {'LIC_DATABASE_TRANSPORT': 'postgrest', 'LIC_DATABASE_DSN': DSN},
                       {'LIC_DATABASE_TRANSPORT': 'postgres'}):
            with self.subTest(values=tuple(values)), self.assertRaises(ValueError):
                client.load_database_config(values)
        config = client.load_database_config({'LIC_DATABASE_TRANSPORT': 'postgres', 'LIC_DATABASE_DSN': DSN})
        self.assertEqual(config.dsn, DSN)
        self.assertNotIn('synthetic-only', repr(config))

    def test_factory_is_inert_and_pool_opens_once_then_closes_once(self):
        pool, factory = Mock(), Mock()
        factory.return_value = pool
        database = client.create_database_gateway(PostgresConfig(DSN), admission=lambda name: None, pool_factory=factory)
        self.assertIsInstance(database, PostgresDatabase)
        factory.assert_not_called()
        self.assertFalse(hasattr(database, 'table'))
        self.assertFalse(hasattr(database, 'rpc'))
        self.assertFalse(hasattr(database, 'call'))
        self.assertFalse(hasattr(database, 'call_function'))
        database.open(); database.open()
        factory.assert_called_once_with(database.config)
        pool.open.assert_called_once_with(wait=False)
        database.close(); database.close()
        pool.close.assert_called_once_with(timeout=5)
        with self.assertRaises(DatabaseUnavailable):
            database.open()

    def test_owned_factory_closes_on_caller_failure(self):
        pool = Mock()
        with self.assertRaisesRegex(RuntimeError, 'body failed'):
            with client.fresh_database_gateway(PostgresConfig(DSN), admission=lambda name: None,
                                              pool_factory=lambda config: pool) as database:
                self.assertIsInstance(database, PostgresDatabase)
                pool.open.assert_called_once_with(wait=False)
                raise RuntimeError('body failed')
        pool.close.assert_called_once_with(timeout=5)




    def test_maintenance_factory_has_its_own_one_connection_pool(self):
        database = Mock(cadence_snapshot=CollectionSchedules().cadence_snapshot())
        with patch('db.hot.hot_runtime.create_database_gateway', return_value=database) as factory:
            repository = maintenance_repository(PostgresConfig(DSN), cadence_snapshot=database.cadence_snapshot)
            database.open.assert_called_once_with()
            self.assertIs(factory.call_args.kwargs["cadence_snapshot"], database.cadence_snapshot)
            config = factory.call_args.args[0]
            self.assertEqual((config.min_size, config.max_size, config.max_waiting), (0, 1, 2))
            self.assertEqual((config.acquire_timeout, config.connect_timeout), (2.0, 2))
            repository.close(); repository.close()
        database.close.assert_called_once_with()

    def test_maintenance_open_failure_closes_partial_resource(self):
        database = Mock(cadence_snapshot=CollectionSchedules().cadence_snapshot())
        database.open.side_effect = RuntimeError('private diagnostic')
        with patch('db.hot.hot_runtime.create_database_gateway', return_value=database):
            with self.assertRaisesRegex(RetentionError, 'maintenance_client_unavailable'):
                maintenance_repository(PostgresConfig(DSN), cadence_snapshot=database.cadence_snapshot)
        database.close.assert_called_once_with()

    def test_read_recovery_borrows_same_pool_for_exactly_one_new_operation(self):
        database, value = object(), [{'video_id': 'v'}]
        read = Mock(side_effect=[DatabaseUnavailable('database_connection_failed'), value])
        with patch.object(client, 'emit_event') as telemetry:
            self.assertIs(client.execute_recoverable_read(database, read, operation='frontend.load_channel'), value)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(call.args == (database,) for call in read.call_args_list))
        self.assertEqual([call.kwargs['phase'] for call in telemetry.call_args_list], ['initial_failure', 'recovered'])

    def test_write_unknown_outcome_pool_pressure_and_server_error_never_retry(self):
        cases = (
            ('write_channel', DatabaseUnavailable('database_connection_failed')),
            ('frontend.load_channel', DatabaseUnavailable('database_connection_failed', outcome_unknown=True)),
            ('frontend.load_channel', DatabaseUnavailable('database_pool_unavailable')),
            ('frontend.load_channel', DatabaseServerError('database_operation_failed', code='42703')),
        )
        for operation, error in cases:
            with self.subTest(operation=operation, error=error):
                read = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    client.execute_recoverable_read(object(), read, operation=operation)
                read.assert_called_once()

    def test_recovery_does_not_repeat_again_or_depend_on_telemetry(self):
        read = Mock(side_effect=DatabaseUnavailable('database_connection_failed'))
        with patch.object(client, 'emit_event', side_effect=RuntimeError('telemetry unavailable')):
            with self.assertRaises(DatabaseUnavailable):
                client.execute_recoverable_read(object(), read, operation='frontend.load_channel')
        self.assertEqual(read.call_count, 2)


class ProcessResourceTests(unittest.TestCase):
    def resources(self, *, fail_open=False, fail_recorder=False, fail_recorder_close=False):
        events = []
        def effect(name, fail=False):
            def invoke():
                events.append(name)
                if fail:
                    raise RuntimeError(name)
            return invoke
        database, recorder = Mock(), Mock()
        database.open.side_effect = effect('database_open', fail_open)
        database.close.side_effect = effect('database_close')
        recorder.start.side_effect = effect('recorder_start', fail_recorder)
        recorder.close.side_effect = effect('recorder_close', fail_recorder_close)
        path = ROOT / 'application/core.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'process_database_resources')
        namespace = dict(contextmanager=contextmanager, database=database, YOUTUBE_QUOTA_ATTEMPT_RECORDER=recorder)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        return namespace['process_database_resources'], database, events

    def test_process_opens_pool_before_recorder_and_closes_in_reverse(self):
        resources, database, events = self.resources()
        with resources() as owned:
            self.assertIs(owned, database)
            self.assertEqual(events, ['database_open', 'recorder_start'])
        self.assertEqual(events, ['database_open', 'recorder_start', 'recorder_close', 'database_close'])

    def test_process_failure_and_partial_startup_release_resources(self):
        for fail_open, fail_recorder in ((True, False), (False, True), (False, False)):
            with self.subTest(open=fail_open, recorder=fail_recorder):
                resources, _, events = self.resources(fail_open=fail_open, fail_recorder=fail_recorder)
                with self.assertRaises(RuntimeError):
                    with resources():
                        raise RuntimeError('body failed')
                self.assertEqual(events[-1], 'database_close')
                self.assertEqual(events.count('database_close'), 1)

    def test_maintenance_process_scope_does_not_start_or_close_recorder(self):
        resources, _, events = self.resources()
        with resources(record_quota=False):
            self.assertEqual(events, ['database_open'])
        self.assertEqual(events, ['database_open', 'database_close'])

    def test_recorder_close_failure_still_releases_process_pool(self):
        resources, _, events = self.resources(fail_recorder_close=True)
        with self.assertRaisesRegex(RuntimeError, 'recorder_close'):
            with resources():
                pass
        self.assertEqual(events, ['database_open', 'recorder_start', 'recorder_close', 'database_close'])


if __name__ == '__main__':
    unittest.main()
