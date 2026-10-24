"""Closed pending-lease cancellation: no source reads, replay or generic admission."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.hot import hot_reconnect_termination as owner
from db.base import postgres as pg
from db.user_work.user_work_backup import UserWorkBackupRepository
from test_operator_job_lifecycle import Database
import test_direct_pg_transactions as fixtures
import hot_runtime

ID = 'a0000000-0000-4000-8000-000000000001'
TOKEN = 'a0000000-0000-4000-8000-000000000002'


def held(**changes):
    return dict(revision=7, reconnect_revision=7, reconnect_state='running', lease_token=TOKEN, **changes)


class ProtocolTests(unittest.TestCase):
    def test_acknowledged_release_matches_exact_revision_and_token(self):
        tx = Database(held(), {'id': ID})
        self.assertEqual(owner.abort_hot_reconnect_claim(tx, ID, 7, TOKEN), {'outcome': 'ready'})
        self.assertEqual(tx.calls, [('one', owner._OWNED, (ID,)),
            ('returning', owner._RELEASE, (ID, 7, 7, TOKEN))])
        self.assertEqual(tx.events, ['borrow'])

    def test_missing_stale_or_replaced_claim_never_mutates(self):
        states = [None]
        for field, value in [('revision', 8), ('reconnect_revision', 8),
                              ('lease_token', ID), ('reconnect_state', 'queued')]:
            row = held()
            row[field] = value
            states.append(row)
        for row in states:
            with self.subTest(row=row):
                tx = Database(row)
                self.assertEqual(owner.abort_hot_reconnect_claim(tx, ID, 7, TOKEN), {'outcome': 'lease_lost'})
                self.assertEqual(len(tx.calls), 1)

    def test_busy_and_unknown_abort_are_not_lost_lease_or_replayed(self):
        errors = [pg.DatabaseServerError('database_operation_failed', code='55P03'),
                  pg.DatabaseUnavailable('database_operation_failed', outcome_unknown=True)]
        for error in errors:
            tx = Database(error)
            with self.assertRaises(type(error)) as caught:
                owner.abort_hot_reconnect_claim(tx, ID, 7, TOKEN)
            self.assertIs(caught.exception, error)
            self.assertEqual(len(tx.calls), 1)
            self.assertEqual(tx.events, ['borrow', 'poison'])

    def test_unconfirmed_update_cannot_ack(self):
        for reply in (None, {'id': TOKEN}):
            tx = Database(held(), reply)
            with self.assertRaisesRegex(RuntimeError, 'abort_unconfirmed'):
                owner.abort_hot_reconnect_claim(tx, ID, 7, TOKEN)
            self.assertEqual(tx.events, ['borrow', 'poison'])

    def test_bad_capability_rejected_before_query(self):
        for args in ((None, 7, TOKEN), (ID, True, TOKEN), (ID, 0, TOKEN),
                     (ID, 9007199254740992, TOKEN), (ID.upper(), 7, TOKEN), (ID, 7, 'bad')):
            tx = Database()
            with self.subTest(args=args), self.assertRaises(ValueError):
                owner.abort_hot_reconnect_claim(tx, *args)
            self.assertEqual(tx.calls, [])

    def test_reviewed_statements_bind_with_real_driver_and_no_payload_access(self):
        for query, params in ((owner._OWNED, (ID,)), (owner._RELEASE, (ID, 7, 7, TOKEN))):
            statement = pg._reviewed_statement(query, read=True,
                                                max_rows=1, returning=query == owner._RELEASE)
            PostgresQuery(Transformer()).convert(statement.as_string().encode(), params)
            for source in ('user_input', 'saved_views', 'label_keys', 'public.channels', 'public.videos'):
                self.assertNotIn(source, query)
        self.assertIn('FOR UPDATE NOWAIT', owner._OWNED)
        self.assertNotIn('SKIP LOCKED', owner._OWNED)


class ClosedEntryTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted',
            'LIC_DATABASE_TRANSPORT': 'postgres',
            'LIC_DATABASE_ID': ID, 'LIC_HOT_RUNTIME_SECRET': 'synthetic_test_secret_0123456789abcdef'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.events = []
        self.conn = fixtures.Connection(self.events)
        self.pool = fixtures.Pool(self.conn, self.events)
        self.db = pg.PostgresDatabase(pg.PostgresConfig(fixtures.DSN), pool_factory=Mock(return_value=self.pool))
        self.db.open(); self.addCleanup(self.db.close)

    def call(self):
        return self.db.abort_hot_reconnect_claim(identity=ID, expected_revision=7, lease_token=TOKEN)

    def test_closed_capability_owns_budget_and_borrowed_transaction(self):
        with patch.object(self.db, '_transaction', wraps=self.db._transaction) as tx, \
                patch.object(owner, 'abort_hot_reconnect_claim', return_value={'outcome': 'ready'}) as action, \
                hot_runtime.collection_control_access():
            self.assertEqual(self.call(), {'outcome': 'ready'})
        operation = tx.call_args.args[0]
        self.assertEqual((operation.statement_timeout_ms, operation.lock_timeout_ms, operation.total_timeout_ms),
                         (5000, 250, 5000))
        self.assertEqual(tx.call_args.kwargs, {'rpc_name': 'abort_hot_reconnect_claim', 'allowed': owner.ERRORS})
        self.assertFalse(action.call_args.args[0]._active)

    def test_generic_transaction_and_provider_cannot_borrow_control_permission(self):
        with hot_runtime.collection_control_access():
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                with self.db.transaction(pg.Operation('abort_hot_reconnect_claim')):
                    self.fail('generic transaction admitted')
            with self.assertRaises(hot_runtime.HotRuntimeUnavailable):
                hot_runtime.require_hot_runtime_ready(operation='provider', rpc_name='abort_hot_reconnect_claim')
        self.assertEqual(self.pool.borrowed, 0)

    def test_pool_wait_rechecks_control_and_no_custom_sql_or_budget(self):
        calls = []
        def admission(name):
            calls.append(name)
            if len(calls) == 2:
                raise hot_runtime.HotDatabaseAdmissionDenied()
        with patch.object(self.db, '_admission', side_effect=admission), \
                patch.object(owner, 'abort_hot_reconnect_claim') as action:
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                self.call()
            action.assert_not_called()
        self.assertEqual(calls, ['abort_hot_reconnect_claim'] * 2)
        with self.assertRaises(TypeError):
            self.db.abort_hot_reconnect_claim(identity=ID, expected_revision=7, lease_token=TOKEN,
                                              callback=lambda: None)

    def test_unknown_commit_retains_uncertainty_without_retry(self):
        self.conn.commit_error = psycopg.OperationalError('synthetic')
        self.conn.results.extend((fixtures.Result(rows=(held(),)), fixtures.Result(rows=({'id': ID},))))
        with hot_runtime.collection_control_access():
            with self.assertRaises(pg.DatabaseUnavailable) as caught:
                self.call()
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(self.conn.queries), 2)
        self.assertEqual(self.pool.borrowed, self.pool.returned)

    def test_facade_validates_receipt_and_restores_control_scope(self):
        db = Mock(spec=['abort_hot_reconnect_claim'])
        repo = UserWorkBackupRepository(db)
        for response in ({'outcome': 'ready'}, {'outcome': 'lease_lost'}):
            db.abort_hot_reconnect_claim.return_value = response
            self.assertEqual(repo.abort_reconnect(ID, 7, TOKEN), response)
        for response in (None, {'outcome': 'busy'}, {'outcome': 'ready', 'payload': 1}):
            db.abort_hot_reconnect_claim.return_value = response
            with self.assertRaisesRegex(RuntimeError, 'abort_unconfirmed'):
                repo.abort_reconnect(ID, 7, TOKEN)
        self.assertFalse(hot_runtime._COLLECTION_CONTROL.get())


if __name__ == '__main__':
    unittest.main()
