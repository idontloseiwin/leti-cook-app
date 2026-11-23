"""Offline protocol/order guards for the prepared closed HOT helpers."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.hot import hot_collection_termination as hot
from db.base.postgres import DatabaseServerError, DatabaseUnavailable, _reviewed_statement
from test_operator_job_lifecycle import Database


def envelope(**changes):
    value = dict(videos='[]', playlists='[]', operators='[]', nv=0, np=0, no=0,
                 arrays=True, at='2026-10-06T10:00:00.123450+00:00')
    value.update(changes)
    return value


def run(**changes):
    value = dict(run_id=4, started_at='2026-10-06T09:00:00+00:00', valid=True,
                 at='2026-10-06T10:00:00.123450+00:00')
    value.update(changes)
    return value


class AbortTests(unittest.TestCase):
    def test_counts_follow_native_write_order_and_exact_captured_clock(self):
        values = envelope(nv=4, np=6, no=1)
        tx = Database(values, None, {'duplicate': False}, 3, 2, 1)
        result = hot.abort_hot_collection_claims(tx, [], [], [])
        self.assertEqual(result, dict(status='aborted', video_requested=4, video_released=2,
            playlist_requested=6, playlist_released=3, operator_requested=1, operator_released=1))
        self.assertEqual([call[1] for call in tx.calls], [hot._ABORT_INPUT, hot._CLAIM_ERROR,
            hot._DUPLICATES, hot._PLAYLISTS, hot._VIDEOS, hot._OPERATORS])
        self.assertTrue(all(call[2] is values for call in tx.calls[1:]))
        self.assertEqual(tx.events, ['borrow'])

    def test_empty_arrays_still_execute_all_three_native_writes(self):
        tx = Database(envelope(), None, {'duplicate': False}, 0, 0, 0)
        result = hot.abort_hot_collection_claims(tx, [], [], [])
        self.assertEqual(len(tx.calls), 6)
        self.assertTrue(all(value == 0 for key, value in result.items() if key != 'status'))

    def test_shape_precedes_bounds_and_no_table_writes_on_validation_failure(self):
        tx = Database(envelope(arrays=None, nv=51))
        with self.assertRaisesRegex(DatabaseServerError, 'hot_abort_invalid_input'):
            hot.abort_hot_collection_claims(tx, None, [], [])
        self.assertEqual(len(tx.calls), 1)
        self.assertEqual(tx.events, ['borrow', 'poison'])

    def test_each_bound_is_checked_before_any_item_validation(self):
        for key, value in [('nv', 51), ('np', 501), ('no', 2)]:
            with self.subTest(key=key):
                tx = Database(envelope(**{key: value}))
                with self.assertRaisesRegex(DatabaseServerError, 'hot_abort_input_bound'):
                    hot.abort_hot_collection_claims(tx, [], [], [])
                self.assertEqual(len(tx.calls), 1)

    def test_native_item_error_is_raised_before_duplicate_query(self):
        for error in ('hot_abort_invalid_video_claim', 'hot_abort_invalid_playlist_claim',
                      'hot_abort_invalid_operator_claim', 'hot_abort_invalid_token'):
            with self.subTest(error=error):
                tx = Database(envelope(), {'error': error})
                with self.assertRaisesRegex(DatabaseServerError, error) as caught:
                    hot.abort_hot_collection_claims(tx, [], [], [])
                self.assertEqual(caught.exception.code, 'P0001')
                self.assertEqual(len(tx.calls), 2)
                self.assertEqual(tx.events, ['borrow', 'poison'])

    def test_duplicates_precede_all_writes(self):
        tx = Database(envelope(), None, {'duplicate': True})
        with self.assertRaisesRegex(DatabaseServerError, 'hot_abort_duplicate_claim'):
            hot.abort_hot_collection_claims(tx, [], [], [])
        self.assertEqual(len(tx.calls), 3)

    def test_write_failure_poisons_borrow_without_replaying_or_skipping(self):
        error = DatabaseUnavailable('database_operation_failed')
        tx = Database(envelope(), None, {'duplicate': False}, 1, error)
        with self.assertRaises(DatabaseUnavailable) as caught:
            hot.abort_hot_collection_claims(tx, [], [], [])
        self.assertIs(caught.exception, error)
        self.assertEqual([call[1] for call in tx.calls[-2:]], [hot._PLAYLISTS, hot._VIDEOS])
        self.assertEqual(tx.events, ['borrow', 'poison'])

    def test_native_shape_guards_uuid_syntax_and_input_order_are_explicit(self):
        self.assertIn("CASE WHEN jsonb_typeof(item)='object' THEN", hot._CLAIM_ERROR)
        self.assertIn('WITH ORDINALITY', hot._CLAIM_ERROR)
        self.assertIn('ORDER BY kind,ordinal LIMIT 1', hot._CLAIM_ERROR)
        self.assertIn("pg_input_is_valid(item->>'lease_token','uuid')", hot._CLAIM_ERROR)
        self.assertIn("count(DISTINCT value->>'video_id')", hot._DUPLICATES)
        self.assertIn("count(DISTINCT (value->>'channel_id',value->>'video_type'))", hot._DUPLICATES)

    def test_locks_are_ordered_skip_locked_and_cas_rechecked(self):
        for query, order, identity in ((hot._PLAYLISTS, 'q.channel_id,q.video_type', 'q.lease_token=o.lease_token'),
                (hot._VIDEOS, 'q.video_id', 'q.lease_token=o.lease_token'),
                (hot._OPERATORS, 'j.id', 'j.lease_token=o.lease_token')):
            self.assertIn('ORDER BY ' + order + ' FOR UPDATE OF ', query)
            self.assertIn('SKIP LOCKED', query)
            self.assertIn(identity, query)
            self.assertIn('owned AS MATERIALIZED', query)
        self.assertIn("WHERE q.status='leased'", hot._PLAYLISTS)
        self.assertIn("WHERE j.status='running'", hot._OPERATORS)
        self.assertNotIn('status=', hot._VIDEOS)
        self.assertEqual(hot._OPERATORS.count('clock_timestamp()'), 2)
        self.assertIn('control.control_epoch=j.accepted_epoch', hot._OPERATORS)


class InterruptTests(unittest.TestCase):
    def test_native_validation_then_shared_writer_uses_same_owner_and_clock(self):
        values=run();tx=Database(values)
        with patch.object(hot,'interrupt_sync_run',return_value=1) as writer:
            self.assertEqual(hot.interrupt_hot_sync_run(tx,'4','native date'),{'status':'interrupted','matched':1})
            writer.assert_called_once_with(tx,run_id=values['run_id'],started_at=values['started_at'],at=values['at'])
        self.assertEqual(tx.calls[0][2],('4','native date'));self.assertEqual(tx.events,['borrow'])
    def test_zero_match_receipt_is_preserved(self):
        tx=Database(run())
        with patch.object(hot,'interrupt_sync_run',return_value=0) as writer:
            self.assertEqual(hot.interrupt_hot_sync_run(tx,4,'x'),{'status':'interrupted','matched':0})
            self.assertEqual(writer.call_count,1)
    def test_native_cast_error_precedes_domain_and_propagates_unchanged(self):
        error=DatabaseServerError('database_operation_failed',code='22003');tx=Database(error)
        with patch.object(hot,'interrupt_sync_run') as writer:
            with self.assertRaises(DatabaseServerError) as caught:hot.interrupt_hot_sync_run(tx,2**63,None)
            self.assertIs(caught.exception,error);writer.assert_not_called()
        self.assertEqual(len(tx.calls),1);self.assertEqual(tx.events,['borrow','poison'])
    def test_domain_invalid_run_never_acquires_run_lock(self):
        for valid in (False,None):
            tx=Database(run(valid=valid))
            with patch.object(hot,'interrupt_sync_run') as writer:
                with self.assertRaisesRegex(DatabaseServerError,'hot_interrupt_invalid_run'):hot.interrupt_hot_sync_run(tx,0,'infinity')
                writer.assert_not_called()
            self.assertEqual(len(tx.calls),1)
    def test_failed_update_is_not_replayed(self):
        error=DatabaseUnavailable('database_connection_failed',outcome_unknown=True);tx=Database(run())
        with patch.object(hot,'interrupt_sync_run',side_effect=error) as writer:
            with self.assertRaises(DatabaseUnavailable) as caught:hot.interrupt_hot_sync_run(tx,4,'x')
            self.assertIs(caught.exception,error);self.assertEqual(writer.call_count,1)
        self.assertEqual(tx.events,['borrow','poison'])


class Boundaries(unittest.TestCase):
    def test_all_queries_fit_real_driver_statement_and_parameter_adapters(self):
        values = envelope()
        values.update(run(), summary='{}')
        queries = [(hot._ABORT_INPUT, (Jsonb([]), Jsonb([]), Jsonb([])), True),
                   (hot._INTERRUPT_INPUT, (1, None), True)]
        queries += [(sql, values, True) for sql in (hot._CLAIM_ERROR, hot._DUPLICATES)]
        queries += [(sql, values, False) for sql in (hot._PLAYLISTS, hot._VIDEOS, hot._OPERATORS)]
        for sql, params, read in queries:
            query = _reviewed_statement(sql, read=read, max_rows=1 if read else None)
            adapted = PostgresQuery(Transformer())
            adapted.convert(query.as_string().encode(), params)
            self.assertTrue(adapted.query)

    def test_json_null_is_not_double_wrapped_and_sql_null_stays_null(self):
        explicit = Jsonb(None)
        self.assertIs(hot._json(explicit), explicit)
        self.assertIsNone(hot._json(None))
        adapted = PostgresQuery(Transformer())
        adapted.convert(b'SELECT %s::jsonb,%s::jsonb', (hot._json(None), hot._json(explicit)))
        self.assertEqual(adapted.params, [None, b'null'])

    def test_retained_timeouts_and_allowlists_match_closed_entry_proposal(self):
        # Timeouts and full error sets are pinned by tests/fixtures/operation_contracts.json.
        self.assertIn('hot_abort_duplicate_claim', hot.ABORT_ERRORS)
        self.assertIn('hot_interrupt_invalid_run', hot.INTERRUPT_ERRORS)


if __name__ == '__main__':
    unittest.main()
