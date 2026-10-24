"""Focused direct-runtime contracts: local fakes only, no providers or network."""
from datetime import date, datetime, timezone
from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.base.client import create_database_gateway, execute_recoverable_read, load_database_config
from db.base.postgres import DatabaseError, DatabaseServerError, DatabaseUnavailable, Operation
from db.base.sql_values import application_value, assignments, insert_values, projection
from youtube_quota_buffer import quota_journal_config
from tests import test_direct_pg_transactions as fixtures
from tests.test_direct_pg_transactions import Result, ServerFailure, DSN

ID = '91f5d9f3-9230-4ee6-b170-155d4c3c1719'


class DirectRuntimeTests(unittest.TestCase):
    def test_configuration_does_not_fallback_or_disclose_dsn(self):
        for env in ({}, {'SUPABASE_URL': 'http://gateway', 'SUPABASE_KEY': 'secret'},
                    {'LIC_DATABASE_TRANSPORT': 'postgres', 'LIC_DATABASE_DSN': 'bad secret'}):
            with self.assertRaises(ValueError) as caught:
                load_database_config(env)
            self.assertNotIn('secret', str(caught.exception))
        config = load_database_config({'LIC_DATABASE_TRANSPORT': 'postgres', 'LIC_DATABASE_DSN': DSN})
        self.assertNotIn('private-test-password', repr(config))
        pool_factory = Mock()
        db = create_database_gateway(config, pool_factory=pool_factory)
        pool_factory.assert_not_called()
        db.close()
        pool_factory.assert_not_called()

    def test_reviewed_read_reacquires_without_closing_pool(self):
        database = Mock()
        read = Mock(side_effect=[DatabaseUnavailable('database_connection_failed'), ['ready']])
        with patch('db.base.client.emit_event'):
            self.assertEqual(execute_recoverable_read(database, read, operation='frontend.load_channel'), ['ready'])
        self.assertEqual(read.call_count, 2)
        database.close.assert_not_called()

    def test_unreviewed_pressure_or_uncertain_operations_are_not_replayed(self):
        for operation, error in (
            ('writer', DatabaseUnavailable('database_connection_failed')),
            ('frontend.load_channel', DatabaseUnavailable('database_pool_unavailable')),
            ('frontend.load_channel', DatabaseUnavailable('database_connection_failed', outcome_unknown=True)),
            ('frontend.load_channel', DatabaseServerError('user_work_limit_reached', code='P0001')),
        ):
            read = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                execute_recoverable_read(Mock(), read, operation=operation)
            self.assertEqual(read.call_count, 1)

    def test_safe_typed_values_preserve_precision_and_clocks(self):
        value = {'id': UUID(ID), 'count': 9007199254740993, 'day': date(2026, 10, 5),
                 'at': datetime(2026, 10, 5, tzinfo=timezone.utc), 'items': (None, False, Decimal('2.5'))}
        normalized = application_value(value)
        self.assertEqual(normalized, {'id': ID, 'count': 9007199254740993, 'day': '2026-10-05',
                                     'at': '2026-10-05T00:00:00+00:00', 'items': [None, False, 2.5]})
        with self.assertRaisesRegex(ValueError, 'database_result_value_invalid'):
            application_value(b'not_json')

    def test_identifiers_are_quoted_and_values_remain_parameters(self):
        attack = "x'); DELETE FROM channels; --"
        cols, values, parameters = insert_values([{'name': attack}, {'name': None, 'rank': 1}])
        self.assertEqual(cols.as_string(), '"name", "rank"')
        self.assertEqual(values.as_string(), '(%s, DEFAULT), (%s, %s)')
        self.assertEqual(parameters, (attack, None, 1))
        self.assertNotIn(attack, values.as_string())
        for text in ('name;DROP TABLE channels', 'name,(SELECT 1)', 'name,name'):
            with self.assertRaises(ValueError): projection(text)
        terms, parameters = assignments({'name': attack})
        self.assertEqual(terms.as_string(), '"name" = %s')
        self.assertEqual(parameters, (attack,))

    def test_new_journal_identity_is_independent_of_credentials(self):
        first = quota_journal_config(project_id='primary', database_id=ID, environ={})
        second = quota_journal_config(project_id='primary', database_id=ID,
                                      environ={'LIC_DATABASE_DSN': DSN, 'SUPABASE_URL': 'http://old'})
        self.assertEqual(first, second)
        self.assertEqual(first[1], hashlib.sha256((ID + '\nprimary').encode()).hexdigest())

    def test_upgrade_keeps_pending_journal_namespace_and_path(self):
        old = hashlib.sha256(b'http://gateway:8080\nprimary').hexdigest()
        environment = {'YOUTUBE_QUOTA_JOURNAL_NAMESPACE': old,
                       'YOUTUBE_QUOTA_JOURNAL_NAMESPACE_PROJECT_ID': 'primary',
                       'YOUTUBE_QUOTA_JOURNAL_PATH': str(ROOT / 'fixture-events.sqlite3')}
        path, namespace = quota_journal_config(database_id=ID, project_id='primary', environ=environment)
        self.assertEqual(namespace, old)
        self.assertEqual(path, ROOT / 'fixture-events.sqlite3')
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ValueError, 'quota_journal_project_mismatch'):
            quota_journal_config(database_id=ID, project_id='different', environ=environment)


class DirectReturningTests(unittest.TestCase):
    setUp = fixtures.DirectTransactionsTests.setUp
    open = fixtures.DirectTransactionsTests.open
    queue = fixtures.DirectTransactionsTests.queue

    def test_maintenance_ceiling_preserves_stricter_function_timeout(self):
        from db.base.postgres import PostgresDatabase
        database = PostgresDatabase(replace(self.config, statement_timeout_ceiling_ms=8000),
                                    admission=self.gate, pool_factory=self.factory)
        self.addCleanup(database.close)
        database.open()
        for timeout, expected in ((60000, '8000ms'), (5000, '5000ms')):
            self.queue(Result(rows=({'n': 1},)))
            with database.transaction(Operation('maintenance_probe', statement_timeout_ms=timeout)) as tx:
                tx.fetch_one('SELECT 1 AS n')
            self.assertEqual(self.connection.setup[-1][1][0], expected)

    def test_returning_is_one_write_and_commits_the_generated_identifier(self):
        self.open(); self.queue(Result(rows=({'id': 42},)))
        rows = self.database.write_rows('start_run', 'INSERT INTO public.sync_runs(started_at) VALUES (%s) RETURNING id',
                                        ('2026-10-05T00:00:00Z',), max_rows=1)
        self.assertEqual(rows, [{'id': 42}])
        self.assertEqual(self.connection.commits, 1)
        self.assertEqual(len(self.connection.queries), 1)
        self.assertIn('WITH lic_changed AS (INSERT', self.connection.queries[0][0])

    def test_returning_overflow_rolls_back_whole_write(self):
        self.open(); self.queue(Result(rows=({'id': 1}, {'id': 2})))
        with self.assertRaisesRegex(DatabaseError, 'database_result_limit_exceeded'):
            self.database.write_rows('bounded_insert', 'INSERT INTO public.rows(id) VALUES (1), (2) RETURNING id', max_rows=1)
        self.assertEqual(self.connection.commits, 0)
        self.assertEqual(self.connection.rollbacks, 1)

    def test_known_write_trigger_error_is_safe_and_others_are_sanitized(self):
        self.open()
        for primary, expected in (('user_work_limit_reached', 'user_work_limit_reached'),
                                  ('private SQL input', 'database_operation_failed')):
            self.queue(Result(execute_error=ServerFailure(primary=primary)))
            with self.assertRaises(DatabaseServerError) as caught:
                self.database.write('folder_create', 'INSERT INTO public.folders(name) VALUES (%s)', ('example',),
                                    error_codes=frozenset({'user_work_limit_reached'}))
            self.assertEqual(caught.exception.message, expected)
            self.assertIsNone(caught.exception.details)
        self.assertEqual(self.connection.commits, 0)

    def test_returning_cannot_smuggle_script_or_role_change(self):
        self.open()
        for statement in ('SET ROLE postgres', 'DELETE FROM rows RETURNING id; SELECT 1', 'DELETE FROM rows'):
            with self.assertRaisesRegex(DatabaseError, 'database_statement_invalid'):
                self.database.write_rows('invalid', statement, max_rows=1)
        self.assertEqual(self.connection.queries, [])


if __name__ == '__main__': unittest.main()
