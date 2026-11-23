"""HOT readiness exercises closed protocols while ordinary admission is blocked."""
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import unittest
from unittest.mock import Mock, patch

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import test_direct_pg_transactions as fixtures
from db.base import postgres as pg
from db.hot import hot_collection_termination as helper
from db.hot.hot_collection_control import verify_control_contract
import hot_runtime as runtime


def zero():
    return {'status': 'aborted', **{kind+'_'+suffix: 0
        for kind in ('video', 'playlist', 'operator') for suffix in ('requested', 'released')}}


def boundary(value=None, error=None):
    database = SimpleNamespace(
        abort_hot_collection_claims=Mock(return_value=zero() if value is None else value),
        interrupt_hot_sync_run=Mock(side_effect=error or pg.DatabaseServerError('hot_interrupt_invalid_run', code='P0001')))
    return database


class HotControlReadinessTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
            'LIC_DATABASE_ID': 'f9900000-0000-4000-8000-000000000001',
            'LIC_HOT_RUNTIME_SECRET': 'synthetic_test_secret_0123456789abcdef'}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)
        blocked = SimpleNamespace(require_collection_ready=Mock(side_effect=runtime.HotRuntimeUnavailable('hot_runtime_not_ready')))
        self.owner = patch.object(runtime, '_local_controller', blocked)
        self.owner.start(); self.addCleanup(self.owner.stop)
        self.events = []; self.conn = fixtures.Connection(self.events)
        self.pool = fixtures.Pool(self.conn, self.events)
        self.database = pg.PostgresDatabase(pg.PostgresConfig(fixtures.DSN), pool_factory=Mock(return_value=self.pool))
        self.database.open(); self.addCleanup(self.database.close)

    def queue_abort(self):
        self.conn.results.extend((fixtures.Result(rows=({'videos':'[]', 'playlists':'[]', 'operators':'[]',
            'nv':0, 'np':0, 'no':0, 'arrays':True, 'at':'2026-10-06T10:00:00+00:00'},)),
            fixtures.Result(), fixtures.Result(rows=({'duplicate':False},)),
            *(fixtures.Result(has_columns=False, rowcount=0) for _ in range(3))))

    def assert_scope_closed(self):
        with self.assertRaises(runtime.HotDatabaseAdmissionDenied) as caught:
            pg._runtime_admission('abort_hot_collection_claims')
        self.assertEqual(caught.exception.code, 'hot_runtime_not_ready')

    def test_actual_closed_methods_and_helpers_under_blocked_owner(self):
        self.queue_abort(); self.conn.results.append(fixtures.Result(rows=({'valid':False},)))
        self.assertFalse(hasattr(self.database, 'call'))
        with patch.object(self.database, 'transaction', side_effect=AssertionError('generic transaction forbidden')):
            self.assertEqual(verify_control_contract(self.database),
                {'status':'ready', 'abort_contract':True, 'interrupt_contract':True})
        self.assertIs(self.database._admission, pg._runtime_admission)
        expected = [helper._ABORT_INPUT, helper._CLAIM_ERROR, helper._DUPLICATES,
                    helper._PLAYLISTS, helper._VIDEOS, helper._OPERATORS, helper._INTERRUPT_INPUT]
        self.assertEqual(len(self.conn.queries), len(expected))
        for (statement, _), native in zip(self.conn.queries, expected):
            self.assertIn(native, statement)
        self.assertTrue(all(value.obj == [] for value in self.conn.queries[0][1]))
        self.assertEqual(self.conn.queries[-1][1], (0, '1970-01-01T00:00:00+00:00'))
        self.assertEqual((self.pool.borrowed, self.pool.returned, self.conn.commits, self.conn.rollbacks), (2,2,1,1))
        self.assert_scope_closed()

    def test_invalid_abort_receipts_stop_before_interrupt_without_retry(self):
        values = [dict(zero(), video_requested=True), dict(zero(), playlist_released=1),
                  dict(zero(), extra=0), {'status':'aborted'}, [], [zero(), zero()], 'private']
        for value in values:
            with self.subTest(value=value):
                database = boundary(value)
                with self.assertRaisesRegex(RuntimeError, '^hot_control_(probe_)?receipt_invalid$'):
                    verify_control_contract(database)
                database.abort_hot_collection_claims.assert_called_once_with(
                    video_claims=[], playlist_claims=[], operator_claims=[])
                database.interrupt_hot_sync_run.assert_not_called()
                self.assert_scope_closed()

    def test_one_row_receipt_adapter_is_retained(self):
        database = boundary([zero()])
        self.assertEqual(verify_control_contract(database)['status'], 'ready')
        database.interrupt_hot_sync_run.assert_called_once_with(
            run_id=0, started_at='1970-01-01T00:00:00+00:00')
        self.assert_scope_closed()

    def test_abort_transport_failure_propagates_once(self):
        database = boundary(); error = pg.DatabaseUnavailable('database_operation_failed', outcome_unknown=True)
        database.abort_hot_collection_claims.side_effect = error
        with self.assertRaises(pg.DatabaseUnavailable) as caught:
            verify_control_contract(database)
        self.assertIs(caught.exception, error)
        database.abort_hot_collection_claims.assert_called_once()
        database.interrupt_hot_sync_run.assert_not_called()
        self.assert_scope_closed()

    def test_unexpected_interrupt_errors_are_sanitized_once(self):
        for error in (pg.DatabaseServerError('private', code='P0001'),
                      pg.DatabaseServerError('hot_interrupt_invalid_run', code='22007'),
                      pg.DatabaseUnavailable('database_operation_failed', outcome_unknown=True), RuntimeError('private')):
            database = boundary(error=error)
            with self.assertRaisesRegex(RuntimeError, '^hot_control_probe_contract_unavailable$') as caught:
                verify_control_contract(database)
            self.assertIsNone(caught.exception.__cause__)
            database.abort_hot_collection_claims.assert_called_once()
            database.interrupt_hot_sync_run.assert_called_once()
            self.assert_scope_closed()

    def test_interrupt_returning_success_is_not_a_contract(self):
        database = boundary(); database.interrupt_hot_sync_run.side_effect = None
        database.interrupt_hot_sync_run.return_value = {'status':'interrupted', 'matched':0}
        with self.assertRaisesRegex(RuntimeError, '^hot_control_probe_contract_invalid$'):
            verify_control_contract(database)
        database.interrupt_hot_sync_run.assert_called_once()
        self.assert_scope_closed()

    def test_actual_abort_unknown_commit_does_not_retry_or_probe_interrupt(self):
        self.queue_abort(); self.conn.commit_error = psycopg.OperationalError('private')
        with patch.object(self.database, 'interrupt_hot_sync_run') as interrupt:
            with self.assertRaises(pg.DatabaseUnavailable) as caught:
                verify_control_contract(self.database)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual((len(self.conn.queries), self.pool.borrowed, self.pool.returned), (6,1,1))
        interrupt.assert_not_called(); self.assert_scope_closed()

    def test_actual_interrupt_unexpected_native_error_never_retries(self):
        self.queue_abort(); self.conn.results.append(fixtures.Result(
            execute_error=fixtures.ServerFailure('22007', 'private')))
        with self.assertRaisesRegex(RuntimeError, '^hot_control_probe_contract_unavailable$'):
            verify_control_contract(self.database)
        self.assertEqual((len(self.conn.queries), self.pool.borrowed, self.pool.returned), (7,2,2))
        self.assert_scope_closed()


if __name__ == '__main__':
    unittest.main()
