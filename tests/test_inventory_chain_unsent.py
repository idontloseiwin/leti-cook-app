"""Bound unsent cancellation: offline owner/transaction protocol, no native SQL."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from db.base.postgres import DatabaseError, DatabaseServerError, PostgresDatabase, PostgresConfig, _reviewed_statement
from db.jobs import operator_job_inventory_chains as chains
from db.jobs.operator_jobs import OperatorJobRepository
from db.hot import hot_collection_termination as control
from hot_runtime import HotDatabaseAdmissionDenied
from test_operator_job_inventory_chains import JOB, LEASE, SCAN, TRANSITION, PLAYLIST_LEASE, CHANNEL, args, held, child, checkpoint, raw, sqls
from test_operator_job_lifecycle import Database as Base
from test_direct_pg_transactions import Connection, Pool, Result, DSN

REAL_DRAIN = control.drain_hot_collection_claims

class Database(Base):
    def fetch_all(self, sql, parameters=(), *, max_rows):
        value = self.answer('all', sql, parameters)
        if len(value) > max_rows:
            raise AssertionError('fixture_row_bound')
        return value

class UnsentTests(unittest.TestCase):
    def setUp(self):
        self.scope = patch.object(chains, 'assert_full_initial_scope').start()
        self.fence = patch.object(chains, 'assert_channel_scope').start()
        self.drain = patch.object(control, 'drain_hot_collection_claims',
            return_value={'status': 'drained', 'remaining': 0}).start()
        self.connect = patch.object(psycopg, 'connect', side_effect=AssertionError('offline_only')).start()
        self.addCleanup(patch.stopall)

    def answers(self, *, source=True, receipts=(), final=True, cp=None, token=PLAYLIST_LEASE):
        return [args('long', TRANSITION, SCAN, PLAYLIST_LEASE), *held(),
            {'value': child(cp=checkpoint('bound') if cp is None else cp)}, {'value': raw()},
            {'value': '{"lease_token":' + ('null' if token is None else '"'+token+'"') + '}'},
            {'value': '{}'}, {'identity_invalid': False, 'kind_invalid': False},
            {'valid': source}, list(receipts), 1, 1, {'valid': final}]

    def call(self, tx, *, unsent=True):
        return chains.cancel_unsent_inventory_chain(tx, JOB, LEASE, 'long', TRANSITION,
            SCAN, PLAYLIST_LEASE, fetch_unsent=unsent)

    def test_live_source_drains_then_clears_one_checkpoint_refunds_once_and_keeps_parent(self):
        tx = Database(*self.answers())
        self.assertEqual(self.call(tx), {'status': 'cancelled', 'dispatch_allowed': False,
                                        'reserved_units_refunded': 1})
        self.drain.assert_called_once_with(tx, [], [dict(channel_id=CHANNEL, video_type='long',
            lease_token=PLAYLIST_LEASE)], [])
        locks = [sql for sql in sqls(tx) if 'FOR UPDATE' in sql]
        self.assertEqual(locks, [chains.planning._CHANNEL, chains.planning._JOB, chains._CHAIN,
            chains._UNSENT_PLAYLIST, chains._UNSENT_SCAN, chains._UNSENT_RECEIPTS])
        self.assertEqual(sqls(tx)[-3:], [chains._CANCEL, chains._UNSENT_REFUND, chains._FINAL])
        self.assertEqual(tx.events[-1], 'commit')
        self.fence.assert_called_once_with(tx, CHANNEL, 17)
        self.assertNotIn('lease_token=', chains._UNSENT_REFUND)
        self.assertNotIn('last_ack', chains._CANCEL)
        self.assertNotIn('scan_id=', chains._CANCEL)

    def test_previously_confirmed_leaf_drain_still_requires_locked_source_and_absence_proof(self):
        tx = Database(*self.answers(token=None))
        self.assertEqual(self.call(tx)['reserved_units_refunded'], 1)
        self.assertIn(chains._UNSENT_SOURCE, sqls(tx))
        self.assertIn(chains._UNSENT_RECEIPTS, sqls(tx))
        self.drain.assert_called_once()
        self.assertIn("state.lease_token IS NULL AND state.status='ready'", chains._UNSENT_SOURCE)

    def test_explicit_unsent_evidence_is_required_before_opening_transaction(self):
        for value in (False, None, 1, 'true'):
            tx = Database()
            with self.subTest(value=value), self.assertRaisesRegex(DatabaseError, 'chain_input_invalid'):
                self.call(tx, unsent=value)
            self.assertEqual(tx.calls, [])
        self.drain.assert_not_called()

    def test_prepared_replaced_terminal_or_already_cancelled_checkpoint_cannot_refund(self):
        cases = [checkpoint(), {}, dict(checkpoint('bound'), transition_id=JOB),
                 dict(checkpoint('bound'), playlist_lease_token=LEASE),
                 dict(checkpoint('bound'), job_lease_token=JOB),
                 dict(checkpoint('bound'), reserved_units=0)]
        for cp in cases:
            answers = self.answers(cp=cp)
            # child() uses cp or {}; all invalid cases remain invalid.
            tx = Database(*answers)
            with self.subTest(cp=cp), self.assertRaisesRegex(DatabaseError, 'checkpoint_mismatch'):
                self.call(tx)
            self.assertNotIn(chains._CANCEL, sqls(tx))
        for identity in (child(cp=checkpoint('bound'), terminal='completed'),
                         child(cp=checkpoint('bound')).replace(SCAN, JOB)):
            answers = self.answers(); answers[5] = {'value': identity}
            tx = Database(*answers)
            with self.assertRaisesRegex(DatabaseError, 'checkpoint_mismatch'):
                self.call(tx)
        self.drain.assert_not_called()

    def test_missing_identity_parent_or_source_refuses_without_mutation(self):
        cases = [(0, dict(args('long', TRANSITION, SCAN, PLAYLIST_LEASE), scan_id=None)),
                 (4, dict(epoch=17, accepted_epoch=18, invalid=False)), (7, None), (8, None),
                 (9, {'identity_invalid': None, 'kind_invalid': False}),
                 (9, {'identity_invalid': False, 'kind_invalid': True}),
                 (10, {'valid': False}), (10, {'valid': None})]
        for index, value in cases:
            answers = self.answers(); answers[index] = value
            tx = Database(*answers)
            with self.subTest(index=index, value=value), self.assertRaises(DatabaseError):
                self.call(tx)
            self.assertFalse(any(kind == 'execute' for kind, _, _ in tx.calls))
        self.drain.assert_not_called()

    def test_any_page_or_quota_receipt_including_conflict_refuses_refund(self):
        for scopes in (('scan_transition',), ('quota_defer',), ('scan_transition', 'quota_defer')):
            tx = Database(*self.answers(receipts=[{'receipt_scope': scope} for scope in scopes]))
            with self.subTest(scopes=scopes), self.assertRaisesRegex(DatabaseError, 'outcome_unresolved'):
                self.call(tx)
            self.assertNotIn(chains._CANCEL, sqls(tx))
        self.drain.assert_not_called()

    def test_locked_or_changed_source_is_not_skipped_into_success(self):
        for index in (7, 8, 11):
            answers = self.answers(); answers[index] = DatabaseServerError('database_operation_failed', code='55P03')
            tx = Database(*answers)
            with self.subTest(index=index), self.assertRaises(DatabaseError):
                self.call(tx)
            self.assertEqual(tx.events[-1], 'rollback')
            self.assertNotIn(chains._UNSENT_REFUND, sqls(tx))
        self.drain.assert_not_called()

    def test_unknown_or_unconfirmed_drain_never_reaches_refund(self):
        for result in ({'status': 'aborted', 'remaining': 0}, {'status': 'drained', 'remaining': 1}):
            self.drain.return_value = result
            tx = Database(*self.answers())
            with self.assertRaisesRegex(DatabaseError, 'outcome_unresolved'):
                self.call(tx)
            self.assertNotIn(chains._CANCEL, sqls(tx))
        self.drain.side_effect = RuntimeError('unknown_drain_ack')
        tx = Database(*self.answers())
        with self.assertRaisesRegex(RuntimeError, 'unknown_drain_ack'):
            self.call(tx)
        self.assertNotIn(chains._CANCEL, sqls(tx))

    def test_zero_row_checkpoint_or_refund_or_late_parent_fence_rolls_back(self):
        for index in (12, 13):
            answers = self.answers(); answers[index] = 0
            tx = Database(*answers)
            with self.subTest(index=index), self.assertRaisesRegex(DatabaseError, 'chain_state_invalid'):
                self.call(tx)
            self.assertEqual(tx.events[-1], 'rollback')
        tx = Database(*self.answers(final=False))
        with self.assertRaisesRegex(DatabaseError, 'operator_job_lease_lost'):
            self.call(tx)
        self.assertEqual(tx.events[-1], 'rollback')

    def test_existing_confirmed_drain_is_borrowed_in_same_owner_transaction(self):
        for released in (0, 1):
            answers = self.answers(token=None if released == 0 else PLAYLIST_LEASE)
            native = dict(videos='[]', playlists='[]', operators='[]', at='2026-10-10T00:00:00Z',
                          arrays=True, nv=0, np=1, no=0)
            answers[12:12] = [native, None, {'duplicate': False}, released, 0, 0, None, None, None]
            tx = Database(*answers)
            with self.subTest(released=released):
                self.drain.side_effect = REAL_DRAIN
                self.assertEqual(self.call(tx)['reserved_units_refunded'], 1)
            self.assertEqual(len(tx.operations), 1)
            self.assertEqual(tx.events.count('commit'), 1)
            self.assertLess(sqls(tx).index(control._REMAINING_PLAYLIST), sqls(tx).index(chains._CANCEL))
            self.assertEqual(sqls(tx)[-3:], [chains._CANCEL, chains._UNSENT_REFUND, chains._FINAL])

    def test_existing_drain_remaining_token_rolls_back_before_chain_or_parent_writes(self):
        answers = self.answers()
        native = dict(videos='[]', playlists='[]', operators='[]', at='2026-10-10T00:00:00Z',
                      arrays=True, nv=0, np=1, no=0)
        answers[12:12] = [native, None, {'duplicate': False}, 0, 0, 0, {'channel_id': CHANNEL}]
        tx = Database(*answers)
        self.drain.side_effect = REAL_DRAIN
        with self.assertRaisesRegex(RuntimeError, 'hot_drain_ownership_unconfirmed'):
            self.call(tx)
        self.assertNotIn(chains._CANCEL, sqls(tx))
        self.assertNotIn(chains._UNSENT_REFUND, sqls(tx))
        self.assertEqual(tx.events[-1], 'rollback')

    def test_real_driver_unknown_commit_raises_without_replay_or_false_cancelled_result(self):
        events = []; connection = Connection(events); pool = Pool(connection, events)
        db = PostgresDatabase(PostgresConfig(DSN), admission=lambda name: None,
                              pool_factory=lambda config: pool).open()
        self.addCleanup(db.close)
        for value in self.answers():
            connection.results.append(Result(tuple(value)) if isinstance(value, list)
                else Result((value,)) if isinstance(value, dict)
                else Result(has_columns=False, rowcount=value))
        connection.commit_error = psycopg.OperationalError('private')
        with self.assertRaises(DatabaseError) as caught:
            self.call(db)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(pool.borrowed, 1)
        self.assertEqual(sum(chains._UNSENT_REFUND in query for query, _ in connection.queries), 1)
        self.drain.assert_called_once()

    def test_facade_has_exact_scope_and_no_hot_denial_abort_or_retry(self):
        repository = OperatorJobRepository(object())
        with patch.object(chains, 'cancel_unsent_inventory_chain', return_value={'status': 'cancelled'}) as cancel:
            repository.cancel_unsent_inventory_chain(JOB, LEASE, 'long', transition_id=TRANSITION,
                scan_id=SCAN, playlist_lease_token=PLAYLIST_LEASE, fetch_unsent=True)
        cancel.assert_called_once_with(repository.database, JOB, LEASE, 'long', TRANSITION,
                                       SCAN, PLAYLIST_LEASE, fetch_unsent=True)
        denied = HotDatabaseAdmissionDenied('hot_runtime_cleanup_priority')
        with patch.object(chains, 'cancel_unsent_inventory_chain', side_effect=denied) as cancel:
            with self.assertRaises(HotDatabaseAdmissionDenied):
                repository.cancel_unsent_inventory_chain(JOB, LEASE, 'long', transition_id=TRANSITION,
                    scan_id=SCAN, playlist_lease_token=PLAYLIST_LEASE, fetch_unsent=True)
        self.assertEqual(cancel.call_count, 1)
        self.drain.assert_not_called()
        self.assertTrue(denied.outcome_may_be_committed)

    def test_new_queries_are_bound_native_and_null_fails_closed(self):
        params = dict(args('long', TRANSITION, SCAN, PLAYLIST_LEASE), epoch=17, job='{}', scan='{}', playlist='{}')
        for statement, values, limit in ((chains._UNSENT_PLAYLIST, ('{}',), 1),
                (chains._UNSENT_SCAN, (SCAN,), 1), (chains._UNSENT_SOURCE, params, 1),
                (chains._UNSENT_RECEIPTS, params, 2)):
            PostgresQuery(Transformer()).convert(statement, values)
            _reviewed_statement(statement, read=True, max_rows=limit)
        PostgresQuery(Transformer()).convert(chains._UNSENT_REFUND, params)
        _reviewed_statement(chains._UNSENT_REFUND, read=False, max_rows=0)
        self.assertIn('coalesce(', chains._UNSENT_SOURCE)
        self.assertIn(',false) AS valid', chains._UNSENT_SOURCE)
        self.assertIn('clock_timestamp()', chains._UNSENT_SOURCE)
        self.assertIn('scan.last_transition_id IS NOT DISTINCT FROM', chains._UNSENT_SOURCE)
        self.assertIn('quota_units_reserved>=1', chains._UNSENT_REFUND)
        self.assertNotIn('greatest', chains._UNSENT_REFUND)

if __name__ == '__main__':
    unittest.main()
