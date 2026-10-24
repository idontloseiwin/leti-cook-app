"""A skipped abort row is not an ACK; confirmed drain proves token absence."""
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
from db.hot import hot_collection_termination as owner
from db.hot.hot_collection_control import HotCollectionControlRepository
from db.base.postgres import DatabaseServerError, DatabaseUnavailable, _reviewed_statement
from test_operator_job_lifecycle import Database
from test_hot_collection_termination import envelope
from collection import initial_video_hydration as initial
from types import SimpleNamespace
from db.base import postgres as pg
import test_direct_pg_transactions as fixtures
import hot_runtime

TOKEN = 'a0000000-0000-4000-8000-000000000001'
VIDEO = {'video_id': 'abcdefghijk', 'video_type': 'long', 'lease_token': TOKEN}


def receipt(**changes):
    value = dict(status='drained', remaining=0, video_requested=1, video_released=0,
                 playlist_requested=0, playlist_released=0, operator_requested=0, operator_released=0)
    value.update(changes)
    return value


class ConfirmedDrainTests(unittest.TestCase):
    def tx(self, *remaining, released=0):
        return Database(envelope(nv=1), None, {'duplicate': False}, 0, released, 0, *remaining)

    def test_short_count_can_ack_only_after_three_atomic_absence_proofs(self):
        tx = self.tx(None, None, None)
        self.assertEqual(owner.drain_hot_collection_claims(tx, [VIDEO], [], []), receipt())
        self.assertEqual([call[1] for call in tx.calls[-3:]],
                         [owner._REMAINING_PLAYLIST, owner._REMAINING_VIDEO, owner._REMAINING_OPERATOR])
        self.assertEqual(tx.events, ['borrow', 'borrow'])

    def test_each_still_owned_group_prevents_ack_and_poisons_transaction(self):
        for preceding in range(3):
            tx = self.tx(*([None] * preceding), {'id': 'still-owned'})
            with self.subTest(group=preceding), self.assertRaisesRegex(RuntimeError, 'ownership_unconfirmed'):
                owner.drain_hot_collection_claims(tx, [VIDEO], [], [])
            self.assertEqual(len(tx.calls), 7 + preceding)
            self.assertEqual(tx.events[-1], 'poison')

    def test_busy_proof_cannot_be_treated_as_absence(self):
        error = DatabaseServerError('database_operation_failed', code='55P03')
        tx = self.tx(None, error)
        with self.assertRaises(DatabaseServerError) as caught:
            owner.drain_hot_collection_claims(tx, [VIDEO], [], [])
        self.assertIs(caught.exception, error)
        self.assertEqual(len(tx.calls), 8)
        self.assertEqual(tx.events[-1], 'poison')

    def test_provider_type_change_does_not_hide_same_video_token_from_proof(self):
        self.assertNotIn('video_type', owner._REMAINING_VIDEO)
        self.assertNotIn("status='", owner._REMAINING_PLAYLIST)
        self.assertNotIn("status='", owner._REMAINING_OPERATOR)
        for statement in (owner._REMAINING_VIDEO, owner._REMAINING_PLAYLIST, owner._REMAINING_OPERATOR):
            self.assertIn('lease_token=w.lease_token', statement)
            self.assertIn('FOR UPDATE OF q NOWAIT', statement)
            self.assertNotIn('SKIP LOCKED', statement)
            query = _reviewed_statement(statement, read=True, max_rows=1)
            PostgresQuery(Transformer()).convert(query.as_string().encode(), (Jsonb([]),))

    def test_existing_counts_only_abort_contract_remains_seven_fields(self):
        tx = Database(envelope(nv=1), None, {'duplicate': False}, 0, 0, 0)
        result = owner.abort_hot_collection_claims(tx, [VIDEO], [], [])
        self.assertEqual(len(result), 7)
        self.assertEqual(result['status'], 'aborted')
        self.assertNotIn('remaining', result)

    def test_facade_refuses_counts_only_or_incomplete_confirmation(self):
        db = Mock(spec=['drain_hot_collection_claims'])
        repo = HotCollectionControlRepository(db)
        for value in (receipt(status='aborted'), receipt(remaining=True), receipt(remaining=1),
                      receipt(video_requested=0), receipt(video_released=2), receipt(extra='private')):
            db.drain_hot_collection_claims.return_value = value
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'drain_receipt_invalid'):
                repo.drain(videos=[VIDEO])
        db.drain_hot_collection_claims.return_value = receipt()
        self.assertEqual(repo.drain(videos=[VIDEO]), receipt())
        db.drain_hot_collection_claims.assert_called_with(
            video_claims=[VIDEO], playlist_claims=[], operator_claims=[])

    def test_unknown_closed_commit_is_not_retried(self):
        error = DatabaseUnavailable('database_operation_failed', outcome_unknown=True)
        db = Mock(spec=['drain_hot_collection_claims'])
        db.drain_hot_collection_claims.side_effect = error
        with self.assertRaises(DatabaseUnavailable) as caught:
            HotCollectionControlRepository(db).drain(videos=[VIDEO])
        self.assertIs(caught.exception, error)
        db.drain_hot_collection_claims.assert_called_once()

    def test_initial_and_common_helper_uses_confirmed_entry_not_old_abort(self):
        db = Mock(spec=['drain_hot_collection_claims', 'abort_hot_collection_claims'])
        db.drain_hot_collection_claims.return_value = receipt()
        claim = SimpleNamespace(**VIDEO)
        self.assertEqual(initial._drain_unsent(SimpleNamespace(database=db), [claim]), receipt())
        db.abort_hot_collection_claims.assert_not_called()
        db.drain_hot_collection_claims.assert_called_once()


class ClosedDrainEntryTests(unittest.TestCase):
    """Real driver/owner/facade; only the Psycopg connection and pool are fake."""
    def setUp(self):
        env = patch.dict(os.environ, {
            'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
            'LIC_DATABASE_ID': TOKEN, 'LIC_HOT_RUNTIME_SECRET': 'synthetic_test_secret_0123456789abcdef',
        }, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.events = []
        self.conn = fixtures.Connection(self.events)
        self.pool = fixtures.Pool(self.conn, self.events)
        self.db = pg.PostgresDatabase(pg.PostgresConfig(fixtures.DSN), pool_factory=Mock(return_value=self.pool))
        self.db.open(); self.addCleanup(self.db.close)
        self.repo = HotCollectionControlRepository(self.db)

    def success(self):
        self.conn.results.extend((
            fixtures.Result(rows=(envelope(nv=1),)), fixtures.Result(),
            fixtures.Result(rows=({'duplicate': False},)),
            fixtures.Result(has_columns=False), fixtures.Result(has_columns=False, rowcount=1),
            fixtures.Result(has_columns=False), fixtures.Result(), fixtures.Result(), fixtures.Result(),
        ))

    def call(self):
        value = self.repo.drain(videos=[VIDEO])
        self.events.append(('ack',))
        return value

    def test_real_closed_drain_ack_is_after_proofs_commit_and_pool_return(self):
        self.success()
        with patch.object(self.db, '_transaction', wraps=self.db._transaction) as tx:
            self.assertEqual(self.call(), receipt(video_released=1))
        self.assertEqual((len(self.conn.queries), self.pool.borrowed, self.pool.returned), (9, 1, 1))
        self.assertEqual(self.events[-3:], [('commit',), ('returned',), ('ack',)])
        for actual, expected in zip(self.conn.queries[-3:],
                (owner._REMAINING_PLAYLIST, owner._REMAINING_VIDEO, owner._REMAINING_OPERATOR)):
            self.assertIn(expected, actual[0])
        operation = tx.call_args.args[0]
        self.assertEqual((operation.statement_timeout_ms, operation.lock_timeout_ms, operation.total_timeout_ms),
                         (5000, 250, 5000))
        self.assertEqual(tx.call_args.kwargs['rpc_name'], 'drain_hot_collection_claims')
        self.assertEqual(self.conn.setup[0], ('SET LOCAL ROLE service_role', ()))
        self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())

    def test_real_commit_error_preserves_unknown_without_ack_or_retry(self):
        self.success()
        self.conn.commit_error = psycopg.OperationalError(fixtures.PRIVATE)
        with patch.object(owner, 'drain_hot_collection_claims', wraps=owner.drain_hot_collection_claims) as action:
            with self.assertRaises(pg.DatabaseUnavailable) as caught:
                self.call()
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertNotIn(fixtures.PRIVATE, repr(caught.exception))
        action.assert_called_once()
        self.assertEqual((len(self.conn.queries), self.pool.borrowed, self.pool.returned), (9, 1, 1))
        self.assertEqual(self.events.count(('commit_error',)), 1)
        self.assertNotIn(('ack',), self.events)
        self.assertNotIn(('commit',), self.events)
        self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())

    def test_generic_sql_and_provider_cannot_borrow_closed_drain_permission(self):
        with hot_runtime.collection_control_access():
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                with self.db.transaction(pg.Operation('drain_hot_collection_claims')):
                    self.fail('generic SQL admitted')
            with self.assertRaises(hot_runtime.HotRuntimeUnavailable):
                hot_runtime.require_hot_runtime_ready(operation='provider', rpc_name='drain_hot_collection_claims')
        self.assertEqual((self.pool.borrowed, self.conn.queries), (0, []))
        self.assertNotIn(('ack',), self.events)

    def test_denial_after_pool_wait_sends_no_sql_or_ack_and_does_not_retry(self):
        calls = []
        def admission(name):
            calls.append(name)
            if len(calls) == 2:
                raise hot_runtime.HotDatabaseAdmissionDenied()
        with patch.object(self.db, '_admission', side_effect=admission), \
                patch.object(owner, 'drain_hot_collection_claims', wraps=owner.drain_hot_collection_claims) as action:
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                self.call()
            action.assert_not_called()
        self.assertEqual(calls, ['drain_hot_collection_claims'] * 2)
        self.assertEqual((self.pool.borrowed, self.pool.returned, self.conn.queries), (1, 1, []))
        self.assertNotIn(('begin',), self.events)
        self.assertNotIn(('ack',), self.events)
        self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())

    def test_denial_before_absence_proof_rolls_back_releases_without_ack(self):
        self.success()
        def admission(name):
            if len(self.conn.queries) == 6:
                raise hot_runtime.HotDatabaseAdmissionDenied()
        with patch.object(self.db, '_admission', side_effect=admission):
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                self.call()
        self.assertEqual((len(self.conn.queries), self.pool.borrowed, self.pool.returned), (6, 1, 1))
        self.assertEqual((self.conn.commits, self.conn.rollbacks), (0, 1))
        self.assertNotIn(('ack',), self.events)
        self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())


if __name__ == '__main__':
    unittest.main()
