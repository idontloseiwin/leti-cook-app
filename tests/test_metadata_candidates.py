"""Offline metadata keyset/capture contracts through the actual DB driver.

The recording transport does not execute PostgreSQL or establish a native plan.
"""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'application'), str(Path(__file__).parent)]
from db.base import postgres as pg
from db.channel import channel_sync as owner
from test_direct_pg_transactions import Connection, Pool, Result, DSN

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
DUE = NOW - timedelta(hours=1)
CEILING = NOW.isoformat(timespec='microseconds')


def channel(number):
    return 'UC' + str(number).zfill(22)


def row(number, *, due=DUE, **changes):
    return dict(channel_id=channel(number), channel_metadata_next_after=due,
                collection_control_epoch=7, collection_mode='active',
                collection_permission='accepted', is_hidden=False,
                channel_metadata_status='ok', **changes)


def scope(number, epoch=7):
    return dict(channel_id=channel(number), control_epoch=epoch)


class MetadataCandidates(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.conn = Connection(self.events)
        self.pool = Pool(self.conn, self.events)
        self.admission = Mock()
        self.db = pg.PostgresDatabase(pg.PostgresConfig(DSN), admission=self.admission,
                                      pool_factory=lambda _: self.pool).open()
        self.addCleanup(self.db.close)
        self.repo = owner.ChannelSyncRepository(self.db)

    def queue(self, *rows, now=NOW):
        self.conn.results.extend((Result(({'at': now},)), Result(rows)))

    def test_page_is_source_free_read_only_with_named_budgets(self):
        self.queue(row(1, title='source must not escape', video_count=3000))
        result = self.repo.metadata_candidate_page(limit=50)
        self.assertEqual(result['rows'], [scope(1)])
        self.assertEqual((result['due_ceiling'], result['inspected_count'], result['scan_exhausted']),
                         (CEILING, 1, True))
        self.assertEqual((self.pool.borrowed, self.conn.commits), (1, 1))
        setup = repr(self.conn.setup)
        for value in ('SET TRANSACTION READ ONLY', '5000ms', '250ms', 'SET LOCAL ROLE service_role'):
            self.assertIn(value, setup)
        self.assertGreater(self.admission.call_count, 0)
        self.assertTrue(all(call.args == (None,) for call in self.admission.call_args_list))
        self.assertEqual(len(self.conn.queries), 2)
        self.assertNotIn('title', owner._METADATA_CANDIDATE_PAGE)

    def test_skipped_prefix_consumes_physical_limit_and_keeps_tail(self):
        rows = [row(i) | {'collection_permission': 'manual_required'} for i in range(1, 101)]
        self.queue(*rows)
        first = self.repo.metadata_candidate_page(limit=100)
        self.assertEqual((first['rows'], first['inspected_count'], first['scan_exhausted']), ([], 100, False))
        self.assertEqual(first['next_after'].channel_id, channel(100))
        self.queue(row(101))
        second = self.repo.metadata_candidate_page(limit=100, after=first['next_after'],
                                                   due_ceiling=first['due_ceiling'])
        self.assertEqual(second['rows'], [scope(101)])
        params = self.conn.queries[-1][1]
        self.assertEqual((params['after_id'], params['due_ceiling']), (channel(100), CEILING))

    def test_equal_timestamps_then_null_tail_and_empty_page(self):
        self.queue(row(1), row(2), row(3, due=None))
        first = self.repo.metadata_candidate_page(limit=3)
        self.assertIsNone(first['next_after'].next_after)
        self.queue(row(4, due=None))
        tail = self.repo.metadata_candidate_page(limit=1, after=first['next_after'], due_ceiling=CEILING)
        self.assertEqual(tail['rows'], [scope(4)])
        params = self.conn.queries[-1][1]
        self.assertIsNone(params['after_due'])
        self.assertEqual(params['after_id'], channel(3))
        self.queue()
        empty = self.repo.metadata_candidate_page(after=tail['next_after'], due_ceiling=CEILING)
        self.assertEqual((empty['rows'], empty['inspected_count'], empty['scan_exhausted']), ([], 0, True))
        self.assertEqual(empty['next_after'], tail['next_after'])

    def test_eligibility_matches_current_policy_and_running_is_skipped(self):
        changes = ({'is_hidden': True}, {'collection_mode': 'initial_inventory'},
                   {'collection_permission': 'manual_required'}, {'collection_control_epoch': None},
                   {'channel_metadata_status': 'running'}, {'collection_mode': 'metadata_only'})
        self.queue(*(row(i) | values for i, values in enumerate(changes, 1)))
        result = self.repo.metadata_candidate_page()
        self.assertEqual(result['rows'], [scope(6)])
        self.assertEqual(result['inspected_count'], 6)

    def test_cursor_is_frozen_typed_and_requires_canonical_clock(self):
        for due in ('2026-10-10T00:00:00Z', NOW, True, '', '2026-10-10T00:00:00.000000+01:00'):
            with self.subTest(due=due), self.assertRaises(ValueError):
                owner.MetadataCandidateCursor(due, channel(1))
        for identity in ('', ' '+channel(1), 'x'*129, None, True):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                owner.MetadataCandidateCursor(None, identity)
        cursor = owner.MetadataCandidateCursor(None, channel(1))
        with self.assertRaises(FrozenInstanceError):
            cursor.channel_id = channel(2)

    def test_invalid_page_bounds_fail_before_borrow(self):
        cursor = owner.MetadataCandidateCursor(CEILING, channel(1))
        for params in (dict(limit=True), dict(limit=0), dict(limit=101), dict(limit='50'),
                       dict(after={}), dict(after=cursor), dict(due_ceiling=NOW),
                       dict(after=cursor, due_ceiling=DUE.isoformat(timespec='microseconds'))):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.repo.metadata_candidate_page(**params)
        self.assertEqual(self.pool.borrowed, 0)

    def test_future_ceiling_and_invalid_native_clock_fail_before_page_read(self):
        self.conn.results.append(Result(({'at': DUE},)))
        with self.assertRaises(ValueError):
            self.repo.metadata_candidate_page(due_ceiling=CEILING)
        self.assertEqual(len(self.conn.queries), 1)
        for clock in (None, CEILING, NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=1)))):
            self.conn.results.append(Result(({'at': clock},)))
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                self.repo.metadata_candidate_page()
        self.assertEqual(self.conn.commits, 0)

    def test_corrupt_result_never_advances_or_returns_authority(self):
        for rows in ((row(1), row(1)), (row(1, due=NOW+timedelta(seconds=1)),),
                     (row(1, due=None), row(2)), (row(1), row(2, due=DUE-timedelta(seconds=1)))):
            self.queue(*rows)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.repo.metadata_candidate_page()
        cursor = owner.MetadataCandidateCursor(DUE.isoformat(timespec='microseconds'), channel(1))
        self.queue(row(1))
        with self.assertRaises(ValueError):
            self.repo.metadata_candidate_page(after=cursor, due_ceiling=CEILING)
        self.assertEqual(self.conn.commits, 0)

    def test_page_driver_rejects_overflow(self):
        self.queue(row(1), row(2))
        with self.assertRaises(pg.DatabaseError):
            self.repo.metadata_candidate_page(limit=1)
        self.assertEqual(self.conn.commits, 0)

    def test_clock_and_fetch_failures_return_no_page_without_repo_retry(self):
        self.queue(row(1))
        self.conn.commit_error = psycopg.OperationalError('synthetic')
        with self.assertRaises(pg.DatabaseError):
            self.repo.metadata_candidate_page()
        self.assertEqual(self.pool.borrowed, 1)

    def test_candidate_sql_preserves_due_order_and_bounds_before_control(self):
        sql = owner._METADATA_CANDIDATE_PAGE
        self.assertLess(sql.index('LIMIT %(limit)s'), sql.index('LEFT JOIN public.channels_with_control'))
        self.assertIn('channel_metadata_next_after ASC NULLS LAST,channel_id ASC', sql)
        self.assertIn('channel_metadata_next_after IS NULL', sql)
        self.assertIn('channel_metadata_next_after=%(after_due)s::timestamptz', sql)
        self.assertIn('channel_id>%(after_id)s::text', sql)
        for prohibited in ('OFFSET', '100000', 'COLLATE', 'FOR UPDATE'):
            self.assertNotIn(prohibited, sql)

    def test_capture_only_exact_pairs_preserves_input_order_and_legacy_shape(self):
        self.queue(row(1, title='one', video_count=1), row(3, title='three', video_count=3))
        rows = self.repo.capture_metadata_candidates([scope(3), scope(2), scope(1)], due_ceiling=CEILING)
        self.assertEqual([value['channel_id'] for value in rows], [channel(3), channel(1)])
        self.assertEqual(rows[0]['channel_metadata_next_after'], DUE.isoformat())
        params = self.conn.queries[-1][1]
        self.assertEqual((params['channel_ids'], params['epochs'], params['limit']),
                         ([channel(3), channel(2), channel(1)], [7, 7, 7], 3))
        self.assertIn('c.collection_control_epoch=requested.control_epoch', owner._METADATA_CAPTURE)

    def test_capture_omits_changed_policy_running_and_deleted_without_substitution(self):
        self.queue(row(1) | {'is_hidden': True}, row(2) | {'channel_metadata_status': 'running'},
                   row(3) | {'collection_permission': 'manual_required'})
        result = self.repo.capture_metadata_candidates([scope(i) for i in range(1, 5)], due_ceiling=CEILING)
        self.assertEqual(result, [])
        self.assertEqual(len(self.conn.queries), 2)

    def test_capture_rejects_inexact_or_duplicate_epoch_results(self):
        for rows in ((row(2),), (row(1) | {'collection_control_epoch': 8},),
                     (row(1) | {'collection_control_epoch': True},)):
            self.queue(*rows)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.repo.capture_metadata_candidates([scope(1)], due_ceiling=CEILING)
        self.queue(row(1), row(1))
        with self.assertRaises(ValueError):
            self.repo.capture_metadata_candidates([scope(1), scope(2)], due_ceiling=CEILING)
        self.queue(row(1, due=NOW+timedelta(seconds=1)))
        with self.assertRaises(ValueError):
            self.repo.capture_metadata_candidates([scope(1)], due_ceiling=CEILING)

    def test_capture_50_channels_is_one_read_and_51_is_rejected(self):
        self.queue(*(row(i) for i in range(1, 51)))
        self.assertEqual(len(self.repo.capture_metadata_candidates(
            [scope(i) for i in range(1, 51)], due_ceiling=CEILING)), 50)
        before = self.pool.borrowed
        with self.assertRaises(ValueError):
            self.repo.capture_metadata_candidates([scope(i) for i in range(1, 52)], due_ceiling=CEILING)
        self.assertEqual(self.pool.borrowed, before)

    def test_invalid_capture_input_and_empty_capture_do_not_borrow(self):
        invalid = (None, {}, [None], [scope(1), scope(1)], [scope(1) | {'extra': 1}],
                   [scope(1, True)], [{'channel_id': [], 'control_epoch': 7}], [scope(1) | {'channel_id': ''}])
        for scopes in invalid:
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                self.repo.capture_metadata_candidates(scopes, due_ceiling=CEILING)
        self.assertEqual(self.repo.capture_metadata_candidates([], due_ceiling=CEILING), [])
        self.assertEqual(self.pool.borrowed, 0)

    def test_capture_rechecks_ceiling_against_db_clock(self):
        self.conn.results.append(Result(({'at': DUE},)))
        with self.assertRaises(ValueError):
            self.repo.capture_metadata_candidates([scope(1)], due_ceiling=CEILING)
        self.assertEqual(len(self.conn.queries), 1)

    def test_capture_commit_failure_is_not_safe_empty(self):
        self.queue(row(1))
        self.conn.commit_error = psycopg.OperationalError('synthetic')
        with self.assertRaises(pg.DatabaseError):
            self.repo.capture_metadata_candidates([scope(1)], due_ceiling=CEILING)
        self.assertEqual(self.pool.borrowed, 1)

    def test_hot_denial_uses_ordinary_gate_before_any_borrow(self):
        self.admission.side_effect = pg.DatabaseUnavailable('synthetic_denial')
        for action in (lambda: self.repo.metadata_candidate_page(),
                       lambda: self.repo.capture_metadata_candidates([scope(1)], due_ceiling=CEILING)):
            with self.assertRaises(pg.DatabaseUnavailable):
                action()
        self.assertEqual((self.pool.borrowed, len(self.conn.queries)), (0, 0))
        self.assertTrue(all(call.args == (None,) for call in self.admission.call_args_list))

    def test_all_fixed_sql_binds_through_reviewed_driver(self):
        cursor = owner.MetadataCandidateCursor(DUE.isoformat(timespec='microseconds'), channel(1))
        self.queue(row(2))
        self.repo.metadata_candidate_page(after=cursor, due_ceiling=CEILING)
        self.queue(row(2))
        self.repo.capture_metadata_candidates([scope(2)], due_ceiling=CEILING)
        for statement, params in self.conn.queries:
            pg._reviewed_statement(statement, read=True, max_rows=100)
            PostgresQuery(Transformer()).convert(statement, params)


if __name__ == '__main__':
    unittest.main()
