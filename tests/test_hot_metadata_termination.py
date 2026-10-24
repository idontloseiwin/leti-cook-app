"""Closed metadata markers: exact scope, no source reads, no unknown ACK replay."""
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.hot import hot_metadata_termination as owner
from db.base import postgres as pg
from db import ChannelSyncRepository
import test_operator_job_lifecycle as lifecycle
import test_direct_pg_transactions as fixtures
import hot_runtime

ID = 'UC' + 'm' * 22
DATABASE_ID = 'a0000000-0000-4000-8000-000000000001'
STAMP = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
MARKER = {'channel_id': ID, 'control_epoch': 7, 'started_at': STAMP.isoformat()}
RECEIPT = {'status': 'drained', 'requested': 1, 'released': 1, 'remaining': 0}


def marked(**changes):
    return dict({'status': 'running', 'started_at': STAMP}, **changes)


class Transaction(lifecycle.Database):
    def fetch_all(self, sql, params=(), max_rows=50):
        return self.answer('all', sql, params)


def answers():
    return [[{'row': MARKER}], marked(), {'control_epoch': 7}, {'channel_id': ID},
            {'status': None, 'started_at': None}]


class MarkerProtocolTests(unittest.TestCase):
    def test_exact_locked_scope_and_final_absence_prove_release(self):
        tx = Transaction(*answers())
        self.assertEqual(owner.drain_hot_channel_metadata(tx, [MARKER]), RECEIPT)
        self.assertEqual([call[1] for call in tx.calls], [owner._ORDER, owner._LOCK, owner._CONTROL, owner._CLEAR, owner._LOCK])
        self.assertEqual(tx.calls[3][2], (ID, STAMP.isoformat()))
        self.assertEqual(tx.events, ['borrow'])

    def test_mismatched_or_missing_marker_cannot_ack_or_mutate(self):
        states = [(None, {'control_epoch': 7}), (marked(), None),
            (marked(), {'control_epoch': 8}), (marked(status=None), {'control_epoch': 7}),
            (marked(started_at=STAMP + timedelta(seconds=1)), {'control_epoch': 7})]
        for marker, control in states:
            tx = Transaction([{'row': MARKER}], marker, control)
            with self.subTest(marker=marker, control=control), self.assertRaisesRegex(RuntimeError, 'marker_mismatch'):
                owner.drain_hot_channel_metadata(tx, [MARKER])
            self.assertNotIn('returning', [call[0] for call in tx.calls])
            self.assertEqual(tx.events, ['borrow', 'poison'])

    def test_busy_and_unknown_outcomes_are_not_absence_or_retried(self):
        for error in (pg.DatabaseServerError('database_operation_failed', code='55P03'),
                      pg.DatabaseUnavailable('database_operation_failed', outcome_unknown=True)):
            tx = Transaction([{'row': MARKER}], error)
            with self.assertRaises(type(error)) as caught:
                owner.drain_hot_channel_metadata(tx, [MARKER])
            self.assertIs(caught.exception, error)
            self.assertEqual(len(tx.calls), 2)

    def test_partial_release_and_false_final_absence_cannot_ack(self):
        for index, value in ((3, None), (3, {'channel_id': 'other'}), (4, marked()), (4, None)):
            values = answers()
            values[index] = value
            tx = Transaction(*values)
            with self.subTest(index=index, value=value), self.assertRaisesRegex(RuntimeError, 'drain_unconfirmed'):
                owner.drain_hot_channel_metadata(tx, [MARKER])
            self.assertEqual(tx.events[-1], 'poison')

    def test_capability_bound_duplicate_scope_and_clock_checked_before_query(self):
        cases = [None, [MARKER] * 51, [MARKER, MARKER], [{'channel_id': ID}],
                 [dict(MARKER, control_epoch=True)], [dict(MARKER, started_at='infinity')],
                 [dict(MARKER, started_at='2026-10-10T08:00:00')], [dict(MARKER, payload='no')]]
        for value in cases:
            tx = Transaction()
            with self.subTest(value=value), self.assertRaises(ValueError):
                owner.drain_hot_channel_metadata(tx, value)
            self.assertEqual(tx.calls, [])

    def test_empty_drain_is_bounded_and_has_no_queries(self):
        tx = Transaction()
        self.assertEqual(owner.drain_hot_channel_metadata(tx, []),
                         dict(RECEIPT, requested=0, released=0))
        self.assertEqual(tx.calls, [])

    def test_sql_is_fixed_sorted_nowait_and_binds_with_actual_driver(self):
        statements = [(owner._ORDER, (Jsonb([MARKER]),), 50),
            (owner._LOCK, (ID,), 1), (owner._CONTROL, (ID,), 1), (owner._CLEAR, (ID, STAMP.isoformat()), 1)]
        for query, params, bound in statements:
            statement = pg._reviewed_statement(query, read=True, max_rows=bound, returning=query == owner._CLEAR)
            PostgresQuery(Transformer()).convert(statement.as_string().encode(), params)
            for source in ('title', 'video_count', 'checked_at', 'next_after', 'error_count', 'public.videos'):
                self.assertNotIn(source, query)
        self.assertIn('ORDER BY', owner._ORDER)
        self.assertIn('FOR UPDATE NOWAIT', owner._LOCK)
        self.assertIn('FOR SHARE NOWAIT', owner._CONTROL)
        self.assertNotIn('SKIP LOCKED', owner._LOCK + owner._CONTROL)


class ClosedMetadataEntryTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted',
            'LIC_DATABASE_TRANSPORT': 'postgres', 'LIC_DATABASE_ID': DATABASE_ID,
            'LIC_HOT_RUNTIME_SECRET': 'synthetic_test_secret_0123456789abcdef'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.events = []
        self.conn = fixtures.Connection(self.events)
        self.pool = fixtures.Pool(self.conn, self.events)
        self.db = pg.PostgresDatabase(pg.PostgresConfig(fixtures.DSN), pool_factory=Mock(return_value=self.pool))
        self.db.open(); self.addCleanup(self.db.close)

    def test_closed_entry_uses_fixed_budget_and_invalidates_borrow(self):
        with patch.object(self.db, '_transaction', wraps=self.db._transaction) as tx, \
                patch.object(owner, 'drain_hot_channel_metadata', return_value=RECEIPT) as action, \
                hot_runtime.collection_control_access():
            self.assertEqual(self.db.drain_hot_channel_metadata(markers=[MARKER]), RECEIPT)
        operation = tx.call_args.args[0]
        self.assertEqual((operation.statement_timeout_ms, operation.lock_timeout_ms, operation.total_timeout_ms), (5000, 250, 5000))
        self.assertEqual(tx.call_args.kwargs, {'rpc_name': 'drain_hot_channel_metadata', 'allowed': frozenset()})
        self.assertFalse(action.call_args.args[0]._active)

    def test_generic_transaction_provider_and_extra_callback_cannot_borrow_capability(self):
        with hot_runtime.collection_control_access():
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                with self.db.transaction(pg.Operation('drain_hot_channel_metadata')):
                    self.fail('generic metadata SQL admitted')
            with self.assertRaises(hot_runtime.HotRuntimeUnavailable):
                hot_runtime.require_hot_runtime_ready(operation='provider', rpc_name='drain_hot_channel_metadata')
        with self.assertRaises(TypeError):
            self.db.drain_hot_channel_metadata(markers=[MARKER], callback=lambda: None)
        self.assertEqual(self.pool.borrowed, 0)

    def test_pool_wait_rechecks_closed_admission_before_action(self):
        with patch.object(self.db, '_admission', side_effect=[None, hot_runtime.HotDatabaseAdmissionDenied()]) as admission, \
                patch.object(owner, 'drain_hot_channel_metadata') as action:
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                self.db.drain_hot_channel_metadata(markers=[MARKER])
        self.assertEqual(admission.call_count, 2)
        action.assert_not_called()

    def test_unknown_commit_is_preserved_without_replaying_any_statement(self):
        self.conn.commit_error = psycopg.OperationalError('synthetic')
        values = answers()
        for value in values:
            self.conn.results.append(fixtures.Result(rows=tuple(value) if isinstance(value, list) else (value,)))
        with hot_runtime.collection_control_access():
            with self.assertRaises(pg.DatabaseUnavailable) as caught:
                self.db.drain_hot_channel_metadata(markers=[MARKER])
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(self.conn.queries), 5)
        self.assertEqual(self.pool.borrowed, self.pool.returned)

    def test_facade_validates_full_receipt_not_partial_count(self):
        database = Mock(spec=['drain_hot_channel_metadata'])
        repo = ChannelSyncRepository(database)
        database.drain_hot_channel_metadata.return_value = RECEIPT
        self.assertEqual(repo.drain_metadata_running([MARKER]), RECEIPT)
        for value in (None, dict(RECEIPT, remaining=1), dict(RECEIPT, released=0),
                      dict(RECEIPT, remaining=False), dict(RECEIPT, payload='no')):
            database.drain_hot_channel_metadata.return_value = value
            with self.assertRaisesRegex(RuntimeError, 'drain_unconfirmed'):
                repo.drain_metadata_running([MARKER])
        self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())


if __name__ == '__main__':
    unittest.main()
