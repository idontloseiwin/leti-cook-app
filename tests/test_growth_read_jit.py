"""Growth uses the real transport lifecycle, with an offline pool/LOCAL-GUC model.

No PostgreSQL/network. Native GUC reset and speed need the separate paired probe.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.base.postgres import DatabaseError, DatabaseUnavailable, Operation, PostgresConfig, PostgresDatabase, _reviewed_statement
from db.base.read_context import http_method_scope, http_read_scope
from db.reads import analytics_read_queries as queries
from hot_storage import HotSourceReadExpired
from test_direct_pg_transactions import Connection, Cursor, Pool, Result, ServerFailure, DSN, render

NOW = datetime.now(timezone.utc)
VIDEO, OTHER = 'aaaaaaaaaaa', 'bbbbbbbbbbb'
SET_LOCAL_JIT = "SELECT set_config('jit','off',true) AS value"


def bounded(statement, maximum):
    return render(_reviewed_statement(statement, read=True, max_rows=maximum))


def row(video=VIDEO):
    return dict(video_id=video, growth_status='partial', delta_views=Decimal('-10'),
                tracked_days=1, observed_start_at=NOW-timedelta(hours=25),
                observed_end_at=NOW, observed_current_views=90, actual_interval_seconds=90000,
                boundary_distance_seconds=None, reason='full_window_history_unavailable',
                source_expires_at=NOW+timedelta(days=1))


class LocalCursor(Cursor):
    def execute(self, statement, parameters):
        self.connection.seen_jit.append((render(statement), self.connection.jit))
        super().execute(statement, parameters)
        match = re.search(r"SELECT set_config\('jit','(on|off)',(true|false)\) AS value", render(statement))
        if match:
            if match[2] == 'true':
                self.connection.local_jit = match[1]
            else:
                self.connection.session_jit = match[1]


class LocalConnection(Connection):
    def __init__(self, events):
        super().__init__(events)
        self.local_jit = None
        self.session_jit = 'on'
        self.seen_jit = []

    @property
    def jit(self):
        return self.session_jit if self.local_jit is None else self.local_jit

    @contextmanager
    def transaction(self):
        previous = self.local_jit
        try:
            with super().transaction():
                yield self
        finally:
            self.local_jit = previous

    def cursor(self):
        return LocalCursor(self)


class GrowthJitTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.connection = LocalConnection(self.events)
        self.pool = Pool(self.connection, self.events)
        self.gate = lambda name: self.events.append(('gate', name))
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=lambda name: self.gate(name),
                                         pool_factory=lambda config: self.pool)
        self.addCleanup(self.database.close)

    def prepare(self, rows=None):
        self.database.open()
        self.connection.results.extend((Result(({'value': 'off'},)),
                                        Result(tuple([row()] if rows is None else rows))))

    def read(self, **changes):
        params = dict(channel_id='channel', video_ids=[VIDEO], as_of=NOW)
        params.update(changes)
        with http_method_scope('GET'), http_read_scope():
            return queries.read_video_view_growth_28d(self.database, **params)

    def next_read(self):
        self.connection.results.append(Result(({'value': 1},)))
        self.assertEqual(self.database.read_rows('unrelated_read', 'SELECT 1 AS value', max_rows=1), [{'value': 1}])
        self.assertEqual(self.connection.seen_jit[-1], (bounded('SELECT 1 AS value', 1), self.connection.session_jit))

    def test_exact_query_params_results_and_operation_budget_one_owned_transaction(self):
        self.prepare([row(OTHER), row()])
        with patch.object(self.database, 'transaction', wraps=self.database.transaction) as transaction:
            result = self.read(video_ids=[VIDEO, ' '+OTHER+' ', VIDEO])
        transaction.assert_called_once_with(Operation('analytics_video_growth', read_only=True))
        self.assertEqual(self.connection.queries, [(bounded(SET_LOCAL_JIT, 1), ()), (bounded(queries.GROWTH_SQL, 2),
            dict(channel_id='channel', video_ids=[VIDEO, ' '+OTHER+' ', VIDEO],
                 as_of=queries.iso_utc(NOW), recorded_from=None, video_type='long'))])
        self.assertEqual(self.connection.seen_jit, [(bounded(SET_LOCAL_JIT, 1), 'on'), (bounded(queries.GROWTH_SQL, 2), 'off')])
        self.assertEqual([v['video_id'] for v in result], [VIDEO, OTHER])
        self.assertEqual(result[0]['delta_views'], -10)
        self.assertIs(type(result[0]['delta_views']), int)
        self.assertEqual(result[0]['observed_end_at'], NOW.isoformat())
        self.assertEqual(self.connection.fetch_sizes, [2, 3])
        self.assertEqual((self.pool.borrowed, self.pool.returned, self.connection.commits), (1, 1, 1))
        self.assertEqual(self.connection.setup[:2], [('SET LOCAL ROLE lic_reader', ()), ('SET TRANSACTION READ ONLY', ())])
        self.assertEqual(self.connection.setup[2][1], ('60000ms', '0ms'))
        self.assertEqual(self.connection.jit, 'on')
        self.next_read()

    def test_existing_session_off_remains_off_after_success(self):
        self.connection.session_jit = 'off'
        self.prepare()
        self.read()
        self.assertEqual(self.connection.jit, 'off')
        self.next_read()

    def test_setup_or_growth_failure_rolls_back_and_never_retries(self):
        for fail_index in (0, 1):
            with self.subTest(fail_index=fail_index):
                self.connection.results.clear()
                self.prepare()
                self.connection.results[fail_index] = Result(execute_error=ServerFailure('57014'))
                before = (self.pool.borrowed, self.connection.rollbacks, len(self.connection.queries))
                with self.assertRaises(DatabaseError) as error:
                    self.read()
                self.assertEqual(error.exception.code, '57014')
                self.assertEqual(self.pool.borrowed-before[0], 1)
                self.assertEqual(self.connection.rollbacks-before[1], 1)
                self.assertEqual(len(self.connection.queries)-before[2], fail_index+1)
                self.assertEqual(self.connection.jit, 'on')
                self.connection.results.clear()
                self.next_read()

    def test_result_overflow_rolls_back_without_second_borrow(self):
        self.prepare([row(), row(OTHER)])
        with self.assertRaisesRegex(DatabaseError, 'database_result_limit_exceeded'):
            self.read()
        self.assertEqual((self.connection.commits, self.connection.rollbacks, self.pool.borrowed), (0, 1, 1))
        self.assertEqual(self.connection.jit, 'on')
        self.next_read()

    def test_commit_connection_failure_is_read_failure_without_retry_or_setting_leak(self):
        self.prepare()
        self.connection.commit_error = psycopg.OperationalError('private detail')
        with self.assertRaises(DatabaseUnavailable) as error:
            self.read()
        self.assertFalse(error.exception.outcome_unknown)
        self.assertEqual((self.pool.borrowed, self.pool.returned, self.connection.rollbacks), (1, 1, 1))
        self.assertEqual(self.connection.jit, 'on')
        self.connection.commit_error = None
        self.next_read()

    def test_hot_gate_rechecked_between_local_setting_and_growth(self):
        self.prepare()
        def gate(name):
            if self.connection.queries:
                raise DatabaseUnavailable('fixture_hot_closed')
        self.gate = gate
        with self.assertRaisesRegex(DatabaseUnavailable, 'fixture_hot_closed'):
            self.read()
        self.assertEqual(self.connection.queries, [(bounded(SET_LOCAL_JIT, 1), ())])
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))
        self.assertEqual(self.connection.jit, 'on')

    def test_no_new_pool_lifecycle_and_empty_or_invalid_input_never_borrows(self):
        self.assertEqual(self.read(video_ids=[]), [])
        with self.assertRaises(DatabaseError):
            self.read(video_ids=[None])
        self.assertEqual((self.pool.borrowed, self.pool.opens), (0, []))
        with self.assertRaisesRegex(DatabaseUnavailable, 'database_pool_not_open'):
            self.read()
        self.database.open()
        self.database.close()
        with self.assertRaises(DatabaseUnavailable):
            self.read()
        self.assertEqual(self.pool.borrowed, 0)

    def test_source_deadline_rejection_still_occurs_after_read_and_local_reset(self):
        self.prepare([dict(row(), source_expires_at=NOW-timedelta(seconds=1))])
        with self.assertRaises(HotSourceReadExpired):
            self.read()
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (1, 0))
        self.assertEqual(self.connection.jit, 'on')


if __name__ == '__main__':
    unittest.main()
