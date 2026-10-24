"""Direct PostgreSQL ownership and transaction contracts with recording fakes.

No database or network is contacted. Real psycopg SQL objects are rendered, but
the pool, connection and cursor are local test doubles. PostgreSQL's transaction
and privilege implementation still needs the separate PG17 acceptance tests.
"""
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.base.postgres import (  # noqa: E402
    DatabaseError, DatabaseServerError, DatabaseUnavailable,
    Operation, PostgresConfig, PostgresDatabase,
)
from db.base import postgres as pg  # noqa: E402
import hot_runtime  # noqa: E402


DSN = 'host=db port=5432 dbname=leti-cook user=authenticator password=private-test-password'
PRIVATE = 'private SQL detail with password=not-for-output'


class ServerFailure(psycopg.Error):
    def __init__(self, state='P0001', primary=PRIVATE):
        self.state = state
        self.primary = primary
        super().__init__(PRIVATE)

    @property
    def sqlstate(self):
        return self.state

    @property
    def diag(self):
        return SimpleNamespace(message_primary=self.primary, message_detail=PRIVATE, message_hint=PRIVATE)


@dataclass
class Result:
    rows: tuple = ()
    has_columns: bool = True
    rowcount: int = 0
    execute_error: BaseException | None = None
    fetch_error: BaseException | None = None


def render(statement):
    return statement.as_string() if isinstance(statement, sql.Composable) else statement


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.description = None
        self.rowcount = -1

    def __enter__(self):
        self.connection.events.append(('cursor_enter',))
        return self

    def __exit__(self, *exc):
        self.connection.events.append(('cursor_exit', exc[0]))

    def execute(self, statement, parameters):
        self.connection.queries.append((render(statement), parameters))
        self.connection.events.append(('dispatch',))
        self.result = self.connection.results.popleft()
        if self.result.execute_error is not None:
            raise self.result.execute_error
        self.description = ('columns',) if self.result.has_columns else None
        self.rowcount = self.result.rowcount

    def fetchmany(self, count):
        self.connection.fetch_sizes.append(count)
        if self.result.fetch_error is not None:
            raise self.result.fetch_error
        return list(self.result.rows[:count])


class Connection:
    def __init__(self, events):
        self.events = events
        self.results = deque()
        self.queries, self.setup, self.fetch_sizes = [], [], []
        self.commits = self.rollbacks = 0
        self.setup_error = self.commit_error = None

    @contextmanager
    def transaction(self):
        self.events.append(('begin',))
        try:
            yield self
            if self.commit_error is not None:
                self.events.append(('commit_error',))
                raise self.commit_error
        except BaseException:
            self.rollbacks += 1
            self.events.append(('rollback',))
            raise
        else:
            self.commits += 1
            self.events.append(('commit',))

    def execute(self, statement, parameters=()):
        self.setup.append((render(statement), parameters))
        if self.setup_error is not None:
            raise self.setup_error

    def cursor(self):
        return Cursor(self)


class Pool:
    def __init__(self, connection, events):
        self.conn, self.events = connection, events
        self.opens, self.closes, self.timeouts = [], [], []
        self.borrowed = self.returned = 0
        self.open_error = self.acquire_error = None

    def open(self, **kwargs):
        self.opens.append(kwargs)
        if self.open_error is not None:
            raise self.open_error

    def close(self, **kwargs):
        self.closes.append(kwargs)

    @contextmanager
    def connection(self, *, timeout):
        self.timeouts.append(timeout)
        self.events.append(('pool_wait',))
        if self.acquire_error is not None:
            raise self.acquire_error
        self.borrowed += 1
        self.events.append(('acquired',))
        try:
            yield self.conn
        finally:
            self.returned += 1
            self.events.append(('returned',))


class DirectTransactionsTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.connection = Connection(self.events)
        self.pool = Pool(self.connection, self.events)
        self.factory = Mock(return_value=self.pool)
        self.gate = Mock(side_effect=lambda name: self.events.append(('gate', name)))
        self.config = PostgresConfig(DSN, acquire_timeout=2.5)
        self.database = PostgresDatabase(self.config, admission=self.gate, pool_factory=self.factory)
        self.addCleanup(self.database.close)

    def open(self):
        self.database.open()

    def queue(self, *results):
        self.connection.results.extend(results)

    def assertError(self, error_type, message, action):
        with self.assertRaises(error_type) as caught:
            action()
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(caught.exception.args, (message,))
        self.assertNotIn(PRIVATE, repr(caught.exception))
        return caught.exception

    def run_read(self):
        with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
            return tx.fetch_one('SELECT 1 AS value')

    def run_write(self):
        with self.database.transaction(Operation('write_fixture')) as tx:
            return tx.execute('UPDATE public.channels SET is_hidden = %s', (False,))

    def test_pool_creation_is_lazy_and_transaction_requires_explicit_open(self):
        self.factory.assert_not_called()
        self.assertError(DatabaseUnavailable, 'database_pool_not_open', self.run_read)
        self.factory.assert_not_called()
        self.open()
        self.database.open()
        self.factory.assert_called_once_with(self.config)
        self.assertEqual(self.pool.opens, [{'wait': False}])

    def test_close_is_idempotent_and_pool_cannot_reopen(self):
        self.open()
        self.database.close()
        self.database.close()
        self.assertEqual(self.pool.closes, [{'timeout': 5}])
        self.assertError(DatabaseUnavailable, 'database_pool_closed', self.database.open)
        self.assertError(DatabaseUnavailable, 'database_pool_closed', self.run_read)

    def test_context_lifecycle_closes_on_body_failure(self):
        with self.assertRaisesRegex(ValueError, '^body_failed$'):
            with self.database:
                raise ValueError('body_failed')
        self.assertEqual(self.pool.closes, [{'timeout': 5}])

    def test_failed_pool_open_is_closed_without_exposing_error(self):
        self.pool.open_error = psycopg.OperationalError(PRIVATE)
        self.assertError(DatabaseUnavailable, 'database_pool_unavailable', self.database.open)
        self.assertEqual(len(self.pool.closes), 1)
        self.assertError(DatabaseUnavailable, 'database_pool_not_open', self.run_read)

    def test_failed_pool_factory_does_not_expose_connection_configuration(self):
        self.factory.side_effect = psycopg.OperationalError(PRIVATE)
        self.assertError(DatabaseUnavailable, 'database_pool_unavailable', self.database.open)
        self.assertEqual(self.pool.borrowed, 0)

    def test_database_cannot_be_used_or_closed_by_a_forked_process(self):
        self.open()
        with patch('db.base.postgres.os.getpid', return_value=os.getpid() + 1000):
            for action in (self.database.open, self.run_read, self.database.close):
                self.assertError(DatabaseError, 'database_process_owner_invalid', action)
        self.assertEqual(self.pool.borrowed, 0)
        self.assertEqual(self.pool.closes, [])

    def test_transaction_cannot_escape_its_context(self):
        self.open()
        with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
            pass
        self.assertError(DatabaseError, 'database_transaction_owner_invalid',
                         lambda: tx.fetch_one('SELECT 1'))
        self.assertEqual(self.connection.queries, [])

    def test_transaction_cannot_be_used_by_another_thread(self):
        self.open()
        errors = []
        with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
            def other_thread():
                try:
                    tx.fetch_one('SELECT 1')
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DatabaseError)
        self.assertEqual(str(errors[0]), 'database_transaction_owner_invalid')
        self.assertEqual(self.connection.queries, [])

    def test_transaction_cannot_be_used_by_another_process(self):
        self.open()
        with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
            with patch('db.base.postgres.os.getpid', return_value=os.getpid() + 1000):
                self.assertError(DatabaseError, 'database_transaction_owner_invalid',
                                 lambda: tx.fetch_one('SELECT 1'))
        self.assertEqual(self.connection.queries, [])

    def test_nested_transaction_is_refused_before_borrowing(self):
        self.open()
        with self.database.transaction(Operation('outer_fixture')):
            self.assertError(DatabaseError, 'database_transaction_nested', self.run_read)
        self.assertEqual((self.pool.borrowed, self.pool.returned), (1, 1))

    def test_gate_is_checked_before_wait_after_wait_and_before_dispatch(self):
        self.open()
        self.queue(Result(rows=({'value': 1},)))
        self.assertEqual(self.run_read(), {'value': 1})
        important = [event for event in self.events if event[0] in {'gate', 'pool_wait', 'acquired', 'dispatch'}]
        self.assertEqual(important, [('gate', None), ('pool_wait',), ('acquired',),
                                     ('gate', None), ('gate', None), ('dispatch',)])
        self.assertEqual(self.pool.timeouts, [2.5])

    def test_early_gate_denial_never_borrows_a_connection(self):
        self.open()
        denied = RuntimeError('admission_denied')
        self.gate.side_effect = denied
        with self.assertRaises(RuntimeError) as caught:
            self.run_read()
        self.assertIs(caught.exception, denied)
        self.assertEqual(self.pool.borrowed, 0)

    def test_gate_closing_during_wait_returns_connection_before_transaction(self):
        self.open()
        self.gate.side_effect = [None, RuntimeError('admission_denied')]
        with self.assertRaisesRegex(RuntimeError, '^admission_denied$'):
            self.run_read()
        self.assertEqual((self.pool.borrowed, self.pool.returned), (1, 1))
        self.assertEqual(self.connection.setup, [])
        self.assertEqual(self.connection.queries, [])

    def test_gate_closing_at_dispatch_rolls_back_without_executing_statement(self):
        self.open()
        self.gate.side_effect = [None, None, RuntimeError('admission_denied')]
        with self.assertRaisesRegex(RuntimeError, '^admission_denied$'):
            self.run_read()
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(self.connection.queries, [])
        self.assertEqual(self.pool.returned, 1)

    def test_operation_name_cannot_grant_a_maintenance_capability(self):
        self.open()
        def maintenance_gate(name):
            if name != 'cleanup_hot_evidence':
                raise RuntimeError('scope_denied')
        self.gate.side_effect = maintenance_gate
        with self.assertRaisesRegex(RuntimeError, '^scope_denied$'):
            with self.database.transaction(Operation('cleanup_hot_evidence')):
                self.fail('An operation label must not authorize arbitrary SQL')
        self.gate.assert_called_once_with(None)
        self.assertEqual(self.pool.borrowed, 0)


    def test_settings_use_local_role_and_transaction_timeouts(self):
        self.open()
        self.queue(Result(rows=({'value': 1},)))
        with self.database.transaction(Operation('read_fixture', read_only=True,
                                                 statement_timeout_ms=1234, lock_timeout_ms=321)) as tx:
            tx.fetch_one('SELECT 1 AS value')
        self.assertEqual(self.connection.setup[0], ('SET LOCAL ROLE service_role', ()))
        self.assertIn(('SET TRANSACTION READ ONLY', ()), self.connection.setup)
        settings, parameters = self.connection.setup[-1]
        for name in ('search_path', 'TimeZone', 'statement_timeout', 'lock_timeout',
                     'idle_in_transaction_session_timeout'):
            self.assertIn(name, settings)
        self.assertEqual(parameters, ('1234ms', '321ms'))
        self.assertNotIn('SET ROLE postgres', str(self.connection.setup))
        self.assertFalse(any('jit' in statement.lower() for statement, _ in self.connection.setup))

    def test_disable_jit_requires_a_boolean_before_pool_access(self):
        self.assertFalse(Operation('read_fixture').disable_jit)
        for value in (None, 0, 1, 'off', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^database_operation_invalid$'):
                Operation('read_fixture', disable_jit=value)
        self.factory.assert_not_called()

    def test_caught_statement_error_still_rolls_back_earlier_write(self):
        self.open()
        self.queue(Result(has_columns=False, rowcount=1), Result(execute_error=ServerFailure('23505')))
        caught_inside = []
        with self.assertRaisesRegex(DatabaseError, '^database_transaction_aborted$'):
            with self.database.transaction(Operation('write_fixture')) as tx:
                tx.execute('UPDATE public.channels SET is_hidden = false')
                try:
                    tx.execute('UPDATE public.channels SET is_hidden = true')
                except DatabaseServerError as error:
                    caught_inside.append(error)
        self.assertEqual(len(caught_inside), 1)
        self.assertEqual(caught_inside[0].code, '23505')
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(self.pool.returned, 1)

    def test_aborted_transaction_refuses_later_dispatch(self):
        self.open()
        self.queue(Result(execute_error=ServerFailure('23505')))
        with self.assertRaisesRegex(DatabaseError, '^database_transaction_aborted$'):
            with self.database.transaction(Operation('write_fixture')) as tx:
                try:
                    tx.execute('UPDATE public.channels SET is_hidden = false')
                except DatabaseServerError:
                    pass
                self.assertError(DatabaseError, 'database_transaction_aborted',
                                 lambda: tx.execute('DELETE FROM public.channels'))
        self.assertEqual(len(self.connection.queries), 1)

    def test_raw_error_is_sanitized_even_when_caught_inside_transaction(self):
        self.open()
        self.queue(Result(execute_error=ServerFailure('23505', PRIVATE)))
        with self.assertRaisesRegex(DatabaseError, '^database_transaction_aborted$'):
            with self.database.transaction(Operation('write_fixture')) as tx:
                error = self.assertError(DatabaseServerError, 'database_operation_failed',
                                         lambda: tx.execute('DELETE FROM public.channels'))
                self.assertEqual(error.code, '23505')
                self.assertIsNone(error.details)
                self.assertIsNone(error.hint)
                self.assertFalse(error.outcome_unknown)



    def test_transport_error_after_write_has_unknown_outcome_and_is_not_retried(self):
        self.open()
        self.queue(Result(execute_error=psycopg.OperationalError(PRIVATE)))
        error = self.assertError(DatabaseUnavailable, 'database_connection_failed', self.run_write)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual(len(self.connection.queries), 1)
        self.assertEqual((self.pool.borrowed, self.pool.returned), (1, 1))

    def test_lost_commit_ack_has_unknown_write_outcome_and_is_not_retried(self):
        self.open()
        self.queue(Result(has_columns=False, rowcount=1))
        self.connection.commit_error = psycopg.OperationalError(PRIVATE)
        error = self.assertError(DatabaseUnavailable, 'database_connection_failed', self.run_write)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual(len(self.connection.queries), 1)
        self.assertEqual(self.pool.returned, 1)

    def test_read_only_transport_error_does_not_report_an_unknown_write(self):
        self.open()
        self.queue(Result(execute_error=psycopg.OperationalError(PRIVATE)))
        error = self.assertError(DatabaseUnavailable, 'database_connection_failed', self.run_read)
        self.assertFalse(error.outcome_unknown)
        self.assertEqual(len(self.connection.queries), 1)

    def test_setup_failure_returns_connection_without_claiming_an_unknown_write(self):
        self.open()
        self.connection.setup_error = psycopg.OperationalError(PRIVATE)
        error = self.assertError(DatabaseUnavailable, 'database_connection_failed', self.run_write)
        self.assertFalse(error.outcome_unknown)
        self.assertEqual(self.connection.queries, [])
        self.assertEqual(self.pool.returned, 1)

    def test_pool_exhaustion_is_bounded_and_not_retried(self):
        self.open()
        for error in (PoolTimeout(PRIVATE), TooManyRequests(PRIVATE), PoolClosed(PRIVATE)):
            with self.subTest(error=type(error).__name__):
                self.pool.acquire_error = error
                before = len(self.pool.timeouts)
                mapped = self.assertError(DatabaseUnavailable, 'database_pool_unavailable', self.run_write)
                self.assertFalse(mapped.outcome_unknown)
                self.assertEqual(len(self.pool.timeouts), before + 1)
        self.assertEqual(self.pool.borrowed, 0)

    def test_disconnect_sqlstates_are_not_misreported_as_server_rejection(self):
        self.open()
        for state in ('08006', '57P01', '57P02', '57P03'):
            with self.subTest(state=state):
                self.queue(Result(execute_error=ServerFailure(state)))
                error = self.assertError(DatabaseUnavailable, 'database_connection_failed', self.run_write)
                self.assertEqual(error.code, state)
                self.assertTrue(error.outcome_unknown)
        self.assertEqual(self.pool.returned, 4)

    def test_body_cancellation_returns_connection_and_rolls_back(self):
        self.open()
        with self.assertRaises(KeyboardInterrupt):
            with self.database.transaction(Operation('read_fixture', read_only=True)):
                raise KeyboardInterrupt()
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(self.pool.returned, 1)
        # The thread-local nesting flag must also be released after cancellation.
        self.queue(Result(rows=({'value': 1},)))
        self.assertEqual(self.run_read(), {'value': 1})

    def test_fetch_error_returns_connection_and_poisoned_transaction_cannot_commit(self):
        self.open()
        self.queue(Result(fetch_error=psycopg.OperationalError(PRIVATE)))
        self.assertError(DatabaseUnavailable, 'database_connection_failed', self.run_read)
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(self.pool.returned, 1)

    def test_fetch_requires_columns_and_execute_refuses_result_sets(self):
        self.open()
        self.queue(Result(has_columns=False), Result(rows=({'channel_id': 'unexpected'},)))
        self.assertError(DatabaseError, 'database_result_missing', self.run_read)
        self.assertError(DatabaseError, 'database_unexpected_result_set', self.run_write)
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 2))
        self.assertEqual(self.pool.returned, 2)

    def test_invalid_sqlstate_is_not_exposed_as_a_public_code(self):
        self.open()
        self.queue(Result(execute_error=ServerFailure('private-code-with-password')))
        error = self.assertError(DatabaseUnavailable, 'database_connection_failed', self.run_write)
        self.assertIsNone(error.code)
        self.assertTrue(error.outcome_unknown)

    def test_oversized_results_fail_whole_operation_without_partial_rows(self):
        self.open()
        self.queue(Result(rows=({'value': 1}, {'value': 2}, {'value': 3})))
        with self.assertRaisesRegex(DatabaseError, '^database_result_limit_exceeded$'):
            with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
                tx.fetch_all('SELECT value FROM fixture', max_rows=2)
        self.assertEqual(self.connection.fetch_sizes, [3])
        statement, _ = self.connection.queries[0]
        self.assertRegex(statement, r'\bLIMIT\s+3\b')
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(self.pool.returned, 1)

    def test_fetch_one_refuses_multiple_rows_instead_of_discarding_them(self):
        self.open()
        self.queue(Result(rows=({'value': 1}, {'value': 2})))
        self.assertError(DatabaseError, 'database_result_limit_exceeded', self.run_read)
        self.assertEqual(self.connection.fetch_sizes, [2])
        self.assertRegex(self.connection.queries[0][0], r'\bLIMIT\s+2\b')

    def test_invalid_max_rows_never_dispatches(self):
        self.open()
        with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
            for count in (True, 0, -1, 100001, 1.5, None):
                with self.subTest(count=count), self.assertRaisesRegex(ValueError, '^database_result_limit_invalid$'):
                    tx.fetch_all('SELECT 1', max_rows=count)
        self.assertEqual(self.connection.queries, [])

    def test_fetch_wraps_trusted_select_and_keeps_bound_values_out_of_sql(self):
        self.open()
        malicious = "' ; DROP TABLE public.channels; --"
        self.queue(Result(rows=({'channel_id': 'safe'},)))
        with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
            result = tx.fetch_all(sql.SQL('SELECT channel_id FROM public.channels WHERE name = %s'),
                                  (malicious,), max_rows=5)
        statement, parameters = self.connection.queries[0]
        self.assertEqual(result, [{'channel_id': 'safe'}])
        self.assertNotIn(malicious, statement)
        self.assertEqual(parameters, (malicious,))
        self.assertRegex(statement, r'\bLIMIT\s+6\b')

    def test_execute_only_accepts_one_dml_statement_without_returning(self):
        self.open()
        statements = (
            'SELECT 1', 'SET ROLE postgres', 'COMMIT', 'ROLLBACK',
            'UPDATE public.channels SET is_hidden = false; DELETE FROM public.channels',
            'DELETE FROM public.channels RETURNING channel_id',
            "UPDATE public.channels SET name = 'semicolon;in_literal'",
        )
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(DatabaseError):
                with self.database.transaction(Operation('write_fixture')) as tx:
                    tx.execute(statement)
        self.assertEqual(self.connection.queries, [])

    def test_fetch_refuses_session_control_scripts_and_unwrapped_dml(self):
        self.open()
        statements = ('SET ROLE postgres', 'COMMIT', 'SELECT 1; SELECT 2',
                      'DELETE FROM public.channels RETURNING channel_id')
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(DatabaseError):
                with self.database.transaction(Operation('read_fixture', read_only=True)) as tx:
                    tx.fetch_all(statement, max_rows=5)
        self.assertEqual(self.connection.queries, [])

    def test_execute_keeps_values_parameterized_and_returns_affected_count(self):
        self.open()
        value = "evil'; DELETE FROM public.channels; --"
        self.queue(Result(has_columns=False, rowcount=7))
        with self.database.transaction(Operation('write_fixture')) as tx:
            affected = tx.execute('UPDATE public.channels SET name = %s', (value,))
        self.assertEqual(affected, 7)
        self.assertEqual(self.connection.queries, [('UPDATE public.channels SET name = %s', (value,))])








class RuntimeRoleAndPoolTests(unittest.TestCase):
    @staticmethod
    def role():
        # PG16+ checks INHERIT on each membership. Login NOINHERIT only supplies
        # the default for a new GRANT, so both values must be checked explicitly.
        return {
            'login': 'authenticator', 'active_role': 'authenticator', 'can_login': True,
            'rolsuper': False, 'rolcreatedb': False, 'rolcreaterole': False,
            'rolreplication': False, 'rolbypassrls': False, 'rolinherit': False,
            'memberships': ['lic_reader', 'service_role'], 'owns_database': False,
            'member_set': True, 'member_admin': False, 'member_inherit': False,
            'service_exists': True, 'service_super': False, 'service_createdb': False,
            'service_createrole': False, 'service_replication': False,
            'service_can_login': False, 'service_inherit': True,
            'service_bypassrls': True, 'service_memberships': [],
            'service_owns_database': False, 'server_version': 170011,
            'reader_exists': True, 'reader_super': False, 'reader_createdb': False,
            'reader_createrole': False, 'reader_replication': False,
            'reader_can_login': False, 'reader_inherit': False, 'reader_bypassrls': False,
            'reader_member_set': True, 'reader_member_admin': False, 'reader_member_inherit': False,
            'reader_memberships': [], 'reader_owns_database': False,
        }

    @staticmethod
    def connection(row):
        connection = Mock()
        cursor = Mock()
        cursor.fetchone.return_value = row

        @contextmanager
        def open_cursor():
            yield cursor

        connection.cursor.side_effect = open_cursor
        return connection, cursor

    def test_expected_authenticator_and_service_role_graph_is_accepted(self):
        connection, cursor = self.connection(self.role())
        pg._validate_runtime_connection(connection)
        cursor.execute.assert_called_once()
        cursor.fetchone.assert_called_once_with()
        query = render(cursor.execute.call_args.args[0])
        self.assertIn('pg_auth_members', query)
        self.assertIn('session_user', query)
        self.assertIn('current_user', query)

    def test_only_reviewed_major17_is_accepted_before_pool_use(self):
        for version in (170000,170011,179999):
            with self.subTest(accepted=version):
                connection,cursor=self.connection({**self.role(),'server_version':version})
                pg._validate_runtime_connection(connection)
                self.assertIn("current_setting('server_version_num')::integer",render(cursor.execute.call_args.args[0]))
        for version in (160099,180000,180011,190000,None,'170011',True):
            with self.subTest(rejected=version):
                connection,_=self.connection({**self.role(),'server_version':version})
                with self.assertRaisesRegex(DatabaseUnavailable,'^database_server_version_unsupported$'):
                    pg._validate_runtime_connection(connection)

    def test_extra_runtime_privileges_and_missing_role_capability_are_rejected(self):
        invalid = (
            ('login', 'postgres'), ('active_role', 'service_role'), ('can_login', False),
            ('rolsuper', True), ('rolcreatedb', True), ('rolcreaterole', True),
            ('rolreplication', True), ('rolbypassrls', True), ('rolinherit', True),
            ('memberships', []), ('memberships', ['service_role', 'postgres']),
            ('owns_database', True), ('member_set', False), ('member_admin', True),
            ('member_inherit', True),
            ('service_exists', False), ('service_super', True), ('service_createdb', True),
            ('service_createrole', True), ('service_replication', True),
            ('service_can_login', True), ('service_bypassrls', False),
            ('service_memberships', ['other_privileged_role']), ('service_owns_database', True),
            ('reader_exists', False), ('reader_super', True), ('reader_createdb', True),
            ('reader_createrole', True), ('reader_replication', True), ('reader_can_login', True),
            ('reader_inherit', True), ('reader_bypassrls', True), ('reader_member_set', False),
            ('reader_member_admin', True), ('reader_member_inherit', True),
            ('reader_memberships', ['service_role']), ('reader_owns_database', True),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                connection, _ = self.connection({**self.role(), field: value})
                with self.assertRaisesRegex(DatabaseUnavailable, '^database_runtime_role_invalid$'):
                    pg._validate_runtime_connection(connection)

    def test_service_inherit_flag_is_harmless_without_service_role_memberships(self):
        for service_inherit in (False, True):
            with self.subTest(service=service_inherit):
                connection, _ = self.connection({**self.role(), 'service_inherit': service_inherit})
                pg._validate_runtime_connection(connection)

    def test_missing_runtime_role_is_rejected(self):
        connection, _ = self.connection(None)
        with self.assertRaisesRegex(DatabaseUnavailable, '^database_runtime_role_invalid$'):
            pg._validate_runtime_connection(connection)

    def test_failed_role_probe_is_sanitized_before_pool_logging(self):
        connection, cursor = self.connection(self.role())
        cursor.execute.side_effect = psycopg.OperationalError(PRIVATE)
        with self.assertRaisesRegex(DatabaseUnavailable, '^database_role_check_failed$') as caught:
            pg._validate_runtime_connection(connection)
        self.assertIsNone(caught.exception.details)
        self.assertIsNone(caught.exception.hint)
        self.assertNotIn(PRIVATE, repr(caught.exception))

    def test_incomplete_probe_result_fails_closed(self):
        for key in ('active_role', 'memberships', 'member_set', 'member_inherit', 'service_exists',
                    'service_super', 'service_memberships', 'server_version', 'reader_exists',
                    'reader_member_set', 'reader_memberships', 'reader_bypassrls'):
            with self.subTest(key=key):
                row = self.role()
                del row[key]
                connection, _ = self.connection(row)
                with self.assertRaises(DatabaseUnavailable):
                    pg._validate_runtime_connection(connection)

    def test_new_pool_is_bounded_unopened_and_resets_libpq_options(self):
        config = PostgresConfig(DSN, min_size=1, max_size=3, max_waiting=7,
                                acquire_timeout=2.5, connect_timeout=4)
        with patch('db.base.postgres.ConnectionPool') as factory, patch.dict(os.environ, {
            'PGOPTIONS': '-c role=postgres',
        }):
            result = pg._new_pool(config)
        self.assertIs(result, factory.return_value)
        args, options = factory.call_args
        self.assertEqual(args, (DSN,))
        self.assertIs(options['connection_class'], pg._SafeConnection)
        self.assertIs(options['configure'], pg._validate_runtime_connection)
        self.assertFalse(options['open'])
        self.assertEqual(options['min_size'], 1)
        self.assertEqual(options['max_size'], 3)
        self.assertEqual(options['max_waiting'], 7)
        self.assertEqual(options['timeout'], 2.5)
        self.assertEqual(options['reconnect_timeout'], 4)
        self.assertEqual(options['num_workers'], 1)
        connection_options = options['kwargs']
        self.assertEqual(connection_options['options'], '')
        self.assertTrue(connection_options['autocommit'])
        self.assertIs(connection_options['row_factory'], pg.dict_row)
        self.assertIsNone(connection_options['prepare_threshold'])
        self.assertEqual(connection_options['connect_timeout'], 4)

    def test_background_connection_failures_do_not_expose_libpq_details(self):
        with patch.object(psycopg.Connection, 'connect', side_effect=psycopg.OperationalError(PRIVATE)):
            with self.assertRaisesRegex(psycopg.OperationalError, '^database_connection_failed$') as caught:
                pg._SafeConnection.connect(DSN)
        self.assertNotIn(PRIVATE, repr(caught.exception))

    def test_default_admission_uses_the_existing_database_denial_exception(self):
        denied = hot_runtime.HotRuntimeUnavailable('hot_runtime_maintenance_scope_denied')
        with patch.dict(os.environ, {'LIC_DATABASE_TRANSPORT': 'postgres'}), \
                patch.object(hot_runtime, 'require_hot_runtime_ready', side_effect=denied) as gate, \
                patch.object(hot_runtime, 'mark_collection_interrupted') as interrupted:
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied) as caught:
                pg._runtime_admission('cleanup_hot_evidence')
        gate.assert_called_once_with(operation='database', rpc_name='cleanup_hot_evidence')
        interrupted.assert_called_once_with()
        self.assertEqual(caught.exception.code, 'hot_runtime_maintenance_scope_denied')
        self.assertTrue(caught.exception.__suppress_context__)

    def test_default_admission_preserves_generic_operation_capability(self):
        with patch.dict(os.environ, {'LIC_DATABASE_TRANSPORT': 'postgres'}), \
                patch.object(hot_runtime, 'require_hot_runtime_ready') as gate, \
                patch.object(hot_runtime, 'mark_collection_interrupted') as interrupted:
            pg._runtime_admission(None)
        gate.assert_called_once_with(operation='database', rpc_name=None)
        interrupted.assert_not_called()

    def test_default_admission_refuses_unselected_direct_transport_before_hot_gate(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(hot_runtime, 'require_hot_runtime_ready') as gate, \
                patch.object(hot_runtime, 'mark_collection_interrupted') as interrupted:
            with self.assertRaisesRegex(DatabaseUnavailable, '^direct_postgres_not_selected$'):
                pg._runtime_admission(None)
        gate.assert_not_called()
        interrupted.assert_not_called()


if __name__ == '__main__':
    unittest.main()
