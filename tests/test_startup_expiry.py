"""The startup observer has fixed SQL, strict clocks and no mutation capability."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import hot_runtime
from db.base.postgres import DatabaseError, DatabaseUnavailable, PostgresConfig, PostgresDatabase
from db.hot.hot_runtime import MaintenanceRepository
from db.hot.hot_retention import RetentionError
from db.hot.startup_expiry import STARTUP_EXPIRY_SQL, STARTUP_EXPIRY_SOURCES
from test_direct_pg_transactions import Connection, Pool, Result, DSN

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
CUTOFF = NOW-timedelta(days=29)
VALUE = {'as_of': NOW.isoformat(), 'cutoff': CUTOFF.isoformat(), 'expired': False}


class StartupRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.database = Mock()
        self.database.read_startup_expiry.return_value = dict(VALUE)
        self.repository = MaintenanceRepository(self.database)

    def test_strict_result_and_exact_caller_supplied_cutoff(self):
        for flag in (False, True):
            self.database.read_startup_expiry.return_value = {**VALUE, 'expired': flag}
            self.assertEqual(self.repository.startup_expiry(cutoff=CUTOFF), {'as_of': VALUE['as_of'], 'expired': flag})
        for changes in ({'expired': 1}, {'as_of': None}, {'as_of': 'infinity'},
                        {'cutoff': 'not-a-date'}, {'cutoff': (NOW-timedelta(days=28)).isoformat()},
                        {'extra': True}):
            with self.subTest(changes=changes):
                self.database.read_startup_expiry.return_value = {**VALUE, **changes}
                with self.assertRaisesRegex(RetentionError, 'invalid_startup_expiry'):
                    self.repository.startup_expiry(cutoff=CUTOFF)
        self.database.read_startup_expiry.return_value = {'as_of': VALUE['as_of'], 'expired': False}
        with self.assertRaisesRegex(RetentionError, 'invalid_startup_expiry'):
            self.repository.startup_expiry(cutoff=CUTOFF)

    def test_entry_grace_and_frozen_cutoff_are_transported_without_recalculation(self):
        for cutoff, strict in ((CUTOFF-timedelta(hours=1), True), (CUTOFF-timedelta(days=2), False)):
            with self.subTest(strict_entry=strict):
                self.database.read_startup_expiry.return_value = {**VALUE, 'cutoff': cutoff.isoformat()}
                self.assertEqual(self.repository.startup_expiry(cutoff=cutoff, strict_entry=strict),
                                 {'as_of': VALUE['as_of'], 'expired': False})
                self.database.read_startup_expiry.assert_called_with(cutoff=cutoff, strict_entry=strict)

    def test_invalid_request_is_rejected_without_database_access(self):
        for request in ({'cutoff': 'infinity'}, {'cutoff': NOW.replace(tzinfo=None)},
                        {'cutoff': CUTOFF, 'strict_entry': 1}):
            with self.subTest(request=request), self.assertRaisesRegex(RetentionError, 'invalid_startup_expiry'):
                self.repository.startup_expiry(**request)
        self.database.read_startup_expiry.assert_not_called()

    def test_probe_failure_classification_and_closed_lifecycle(self):
        for error, expected in ((DatabaseUnavailable('offline'), 'startup_expiry_unavailable'),
                                (DatabaseError('timeout', code='57014'), 'startup_expiry_failed')):
            self.database.read_startup_expiry.side_effect = error
            with self.assertRaisesRegex(RetentionError, expected):
                self.repository.startup_expiry(cutoff=CUTOFF)
        self.repository.close()
        with self.assertRaisesRegex(RetentionError, 'maintenance_repository_closed'):
            self.repository.startup_expiry(cutoff=CUTOFF)

    def test_contract_separates_transport_schema_and_own_validation(self):
        for error, code in ((DatabaseUnavailable('offline'), 'runtime_contract_unavailable'),
                            (DatabaseError('schema_not_ready'), 'runtime_contract_schema_invalid')):
            with patch('db.hot.hot_runtime.get_hot_runtime_contract', side_effect=error):
                with self.assertRaisesRegex(RetentionError, code):
                    self.repository.contract()
        value = {'version': 1, 'as_of': VALUE['as_of'], 'schema_ready': True}
        for change, code in (({'schema_ready': False}, 'runtime_contract_schema_invalid'),
                             ({'schema_ready': 1}, 'invalid_runtime_contract'),
                             ({'as_of': 'invalid'}, 'invalid_runtime_contract'),
                             ({'extra': True}, 'invalid_runtime_contract')):
            with patch('db.hot.hot_runtime.get_hot_runtime_contract', return_value={**value, **change}):
                with self.assertRaisesRegex(RetentionError, code):
                    self.repository.contract()


class StartupTransportTests(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.connection = Connection(self.trace)
        self.pool = Pool(self.connection, self.trace)
        self.gates = []
        self.database = PostgresDatabase(PostgresConfig(DSN), admission=self.gates.append,
                                        pool_factory=lambda config: self.pool).open()
        self.addCleanup(self.database.close)

    def test_fixed_one_row_query_and_read_only_capability(self):
        self.connection.results.append(Result(rows=(dict(VALUE),)))
        self.assertEqual(self.database.read_startup_expiry(cutoff=CUTOFF), VALUE)
        self.assertEqual(self.gates, ['get_hot_startup_expiry'] * 4)
        self.assertIn(('SET TRANSACTION READ ONLY', ()), self.connection.setup)
        self.assertEqual(len(self.connection.queries), 1)
        self.assertIn(STARTUP_EXPIRY_SQL, self.connection.queries[0][0])
        self.assertEqual(self.connection.queries[0][1], (CUTOFF, False))
        with self.assertRaises(TypeError):
            self.database.read_startup_expiry(cutoff=CUTOFF, statement='SELECT 1')

    def test_invalid_cutoff_or_entry_flag_never_borrows_a_connection(self):
        for request in ({'cutoff': 'infinity'}, {'cutoff': NOW.replace(tzinfo=None)},
                        {'cutoff': CUTOFF, 'strict_entry': 'yes'}):
            with self.subTest(request=request), self.assertRaisesRegex(DatabaseError, 'invalid_startup_cutoff'):
                self.database.read_startup_expiry(**request)
        self.assertEqual(self.pool.borrowed, 0)

    def test_real_gate_only_allows_fixed_observer_in_maintenance_scope(self):
        self.database._admission = __import__('db.base.postgres', fromlist=['_runtime_admission'])._runtime_admission
        environment = {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
                       'LIC_DATABASE_ID': '91f5d9f3-9230-4ee6-b170-155d4c3c1719',
                       'LIC_HOT_RUNTIME_SECRET': 'internal_runtime_secret_0123456789'}
        controller = Mock()
        for name in ('require_ready', 'require_collection_ready', 'require_database_ready'):
            getattr(controller, name).side_effect = hot_runtime.HotRuntimeUnavailable('hot_runtime_not_ready')
        with patch.dict(os.environ, environment, clear=True), patch.object(hot_runtime, '_local_controller', controller):
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                self.database.read_startup_expiry(cutoff=CUTOFF)
            with hot_runtime.maintenance_access():
                self.connection.results.append(Result(rows=(dict(VALUE),)))
                self.database.read_startup_expiry(cutoff=CUTOFF)
                with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                    self.database.read_row('get_hot_startup_expiry', 'SELECT 1')
        self.assertEqual(self.pool.borrowed, 1)

    def test_empty_result_is_error_and_transaction_rolls_back(self):
        self.connection.results.append(Result(rows=()))
        with self.assertRaises(DatabaseError):
            self.database.read_startup_expiry(cutoff=CUTOFF)
        self.assertEqual(self.connection.rollbacks, 1)


class StartupSourceManifestTests(unittest.TestCase):
    def test_finite_clock_queries_and_explicit_retention_boundary(self):
        self.assertEqual(STARTUP_EXPIRY_SQL.count('clock_timestamp()'), 1)
        self.assertIn("clock.cutoff + interval '696 hours'", STARTUP_EXPIRY_SOURCES['review_evidence'])
        self.assertIn('%s::timestamptz AS cutoff', STARTUP_EXPIRY_SQL)
        self.assertIn('%s::boolean AS strict_entry', STARTUP_EXPIRY_SQL)
        for name, query in STARTUP_EXPIRY_SOURCES.items():
            with self.subTest(source=name):
                self.assertTrue(query.endswith('LIMIT 1'))
                self.assertIn(" > '-infinity'::timestamptz", query)
                self.assertIn('clock.cutoff', query)
                if name!='review_evidence':self.assertNotIn("interval '696 hours'",query)
        self.assertIn('< clock.cutoff', STARTUP_EXPIRY_SOURCES['category_reference'])
        self.assertIn('NOT clock.strict_entry', STARTUP_EXPIRY_SOURCES['category_reference'])
        self.assertIn('= clock.cutoff', STARTUP_EXPIRY_SOURCES['category_reference'])
        self.assertNotIn('<= clock.cutoff', STARTUP_EXPIRY_SQL)
        self.assertNotIn('public.saved_objects', STARTUP_EXPIRY_SQL)
        self.assertNotIn('deleted_at', STARTUP_EXPIRY_SQL)
        self.assertNotIn('published_at <', STARTUP_EXPIRY_SQL)


if __name__ == '__main__':
    unittest.main()
