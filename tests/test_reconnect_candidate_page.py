"""Reconnect keyset wire/driver protocol; no live PostgreSQL, locks or provider."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from uuid import UUID

import psycopg
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from db.base import postgres as pg
from db.user_work import user_work_reconnect as owner
from db.user_work.user_work_backup import UserWorkBackupRepository
import test_direct_pg_transactions as fixtures

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
REQUESTED = NOW - timedelta(hours=1)


def stamp(value):
    return value.isoformat(timespec='microseconds')


def identity(number):
    return str(UUID(int=number, version=4))


def row(number, *, attempt=None, requested=REQUESTED, eligible=True):
    return dict(id=identity(number), last_attempt_at=attempt, requested_at=requested, eligible=eligible)


class ReconnectCandidatePageTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.conn = fixtures.Connection(self.events)
        self.pool = fixtures.Pool(self.conn, self.events)
        self.db = pg.PostgresDatabase(pg.PostgresConfig(fixtures.DSN),
            pool_factory=Mock(return_value=self.pool), admission=lambda name: None)
        self.db.open(); self.addCleanup(self.db.close)

    def queue(self, *rows, clock=NOW):
        self.conn.results.extend((fixtures.Result(rows=({'at': clock},)), fixtures.Result(rows=rows)))

    def test_facade_returns_typed_cursor_and_exact_native_utc_ceiling(self):
        self.queue(row(1), row(2))
        result = UserWorkBackupRepository(self.db).reconnect_candidate_page(limit=2)
        self.assertEqual(result, dict(candidates=[{'id': identity(1)}, {'id': identity(2)}],
            next_after=owner.ReconnectCandidateCursor(None, stamp(REQUESTED), identity(2)),
            ceiling=stamp(NOW), inspected=2, exhausted=False))
        self.assertEqual((len(self.conn.queries), self.pool.borrowed, self.conn.commits), (2, 1, 1))
        self.assertIn(('SET TRANSACTION READ ONLY', ()), self.conn.setup)
        self.assertEqual(self.conn.queries[1][1], dict(limit=2, ceiling=stamp(NOW),
            after_id=None, after_attempt=None, after_requested=None))

    def test_ineligible_physical_page_is_counted_and_advances_cursor(self):
        self.queue(row(1, eligible=False), row(2, eligible=False))
        first = owner.reconnect_candidate_page(self.db, limit=2)
        self.assertEqual((first['candidates'], first['inspected'], first['exhausted']), ([], 2, False))
        self.queue(row(3))
        second = owner.reconnect_candidate_page(self.db, limit=2,
            after=first['next_after'], ceiling=first['ceiling'])
        self.assertEqual(second['candidates'], [{'id': identity(3)}])
        self.assertTrue(second['exhausted'])
        self.assertEqual(self.conn.queries[3][1]['after_id'], identity(2))
        self.assertEqual(self.conn.queries[3][1]['ceiling'], first['ceiling'])

    def test_locked_head_exact_claims_do_not_rewind_advisory_cursor(self):
        self.queue(row(1), row(2))
        first = owner.reconnect_candidate_page(self.db, limit=2)
        for candidate in first['candidates']:
            self.conn.results.extend((fixtures.Result(rows=({'locked': True},)), fixtures.Result()))
            self.assertEqual(owner.claim_reconnect(self.db, identity=candidate['id']), [])
            self.assertEqual(self.conn.queries[-1][1]['identity'], candidate['id'])
        self.queue(row(3))
        tail = owner.reconnect_candidate_page(self.db, limit=2, after=first['next_after'], ceiling=first['ceiling'])
        self.assertEqual(tail['candidates'], [{'id': identity(3)}])
        self.assertEqual(self.conn.queries[-1][1]['after_id'], identity(2))

    def test_null_first_then_attempt_timestamp_requested_and_uuid_order(self):
        earlier = REQUESTED - timedelta(hours=1)
        self.queue(row(3, requested=earlier), row(1), row(2),
                   row(5, attempt=earlier), row(4, attempt=REQUESTED))
        result = owner.reconnect_candidate_page(self.db, limit=5)
        self.assertEqual([item['id'] for item in result['candidates']],
                         [identity(i) for i in (3, 1, 2, 5, 4)])
        after = result['next_after']
        self.assertEqual(after, owner.ReconnectCandidateCursor(stamp(REQUESTED), stamp(REQUESTED), identity(4)))
        self.queue()
        end = owner.reconnect_candidate_page(self.db, limit=5, after=after, ceiling=result['ceiling'])
        self.assertEqual((end['next_after'], end['inspected'], end['exhausted']), (after, 0, True))
        self.assertEqual(self.conn.queries[-1][1]['after_attempt'], stamp(REQUESTED))

    def test_more_than_one_hundred_rows_are_consumed_as_bounded_pages(self):
        after, ceiling, found, inspected = None, None, [], 0
        for start in range(1, 302, 100):
            page_rows = [row(i, eligible=i > 200) for i in range(start, min(start + 100, 302))]
            self.queue(*page_rows)
            page = owner.reconnect_candidate_page(self.db, limit=100, after=after, ceiling=ceiling)
            self.assertLessEqual(page['inspected'], 100)
            inspected += page['inspected']
            found.extend(value['id'] for value in page['candidates'])
            after, ceiling = page['next_after'], page['ceiling']
        self.assertTrue(page['exhausted'])
        self.assertEqual(inspected, 301)
        self.assertEqual(found, [identity(i) for i in range(201, 302)])
        self.assertEqual(self.pool.borrowed, 4)

    def test_first_empty_page_and_fresh_wake_use_new_sweeps(self):
        self.queue()
        first = owner.reconnect_candidate_page(self.db)
        self.assertEqual((first['next_after'], first['inspected'], first['exhausted']), (None, 0, True))
        # A fresh wake resets only the caller's advisory cursor, never queue rows.
        later = NOW + timedelta(seconds=1)
        self.queue(row(1, requested=later), clock=later)
        fresh = owner.reconnect_candidate_page(self.db)
        self.assertEqual(fresh['ceiling'], stamp(later))
        self.assertEqual(fresh['candidates'], [{'id': identity(1)}])
        self.assertIsNone(self.conn.queries[-1][1]['after_id'])

    def test_invalid_public_inputs_are_rejected_before_any_borrow(self):
        valid = owner.ReconnectCandidateCursor(None, stamp(REQUESTED), identity(1))
        for kwargs in (dict(limit=0), dict(limit=101), dict(limit=True), dict(limit='2'),
                       dict(after={}), dict(after=valid), dict(ceiling='infinity'),
                       dict(ceiling=NOW), dict(ceiling=stamp(NOW).replace('+00:00', 'Z')),
                       dict(after=valid, ceiling=stamp(REQUESTED - timedelta(seconds=1)))):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                owner.reconnect_candidate_page(self.db, **kwargs)
        self.assertEqual(self.pool.borrowed, 0)

    def test_cursor_requires_canonical_finite_utc_clocks_and_uuid(self):
        for values in ((None, 'infinity', identity(1)), (None, stamp(REQUESTED), identity(10).upper()),
                       (True, stamp(REQUESTED), identity(1)), (None, stamp(REQUESTED), 'not-uuid'),
                       (None, '2026-10-10T12:00:00.000000+01:00', identity(1)),
                       (None, '2026-10-10 12:00:00.000000+00:00', identity(1)),
                       (None, '2026-10-10T12:00:00+00:00', identity(1))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                owner.ReconnectCandidateCursor(*values)

    def test_future_ceiling_fails_after_native_clock_before_page_query(self):
        self.conn.results.append(fixtures.Result(rows=({'at': NOW},)))
        with self.assertRaises(ValueError):
            owner.reconnect_candidate_page(self.db, ceiling=stamp(NOW + timedelta(seconds=1)))
        self.assertEqual((len(self.conn.queries), self.conn.commits, self.conn.rollbacks), (1, 0, 1))

    def test_invalid_database_clock_does_not_become_cursor_authority(self):
        self.conn.results.append(fixtures.Result(rows=({'at': NOW.replace(tzinfo=None)},)))
        with self.assertRaisesRegex(RuntimeError, 'candidate_clock_invalid'):
            owner.reconnect_candidate_page(self.db)
        self.assertEqual(len(self.conn.queries), 1)

    def test_out_of_order_duplicate_or_nonboolean_eligibility_never_returns_page(self):
        for rows in ((row(2), row(1)), (row(1), row(1)), (row(1, eligible=None),)):
            with self.subTest(rows=rows):
                self.queue(*rows)
                with self.assertRaisesRegex(RuntimeError, 'page_unconfirmed'):
                    owner.reconnect_candidate_page(self.db)
        self.assertEqual(self.conn.commits, 0)

    def test_ceiling_violation_in_database_reply_fails_closed(self):
        self.queue(row(1, attempt=NOW + timedelta(seconds=1)))
        with self.assertRaisesRegex(RuntimeError, 'page_unconfirmed'):
            owner.reconnect_candidate_page(self.db)
        self.assertEqual(self.conn.commits, 0)

    def test_read_commit_error_does_not_return_advance_or_retry(self):
        self.queue(row(1))
        self.conn.commit_error = psycopg.OperationalError(fixtures.PRIVATE)
        with self.assertRaises(pg.DatabaseUnavailable) as caught:
            owner.reconnect_candidate_page(self.db)
        self.assertFalse(caught.exception.outcome_unknown)  # read-only transaction
        self.assertEqual((self.pool.borrowed, self.pool.returned, len(self.conn.queries)), (1, 1, 2))


    def test_legacy_manual_waits_are_eligible_without_bypassing_provider_or_scope(self):
        # Execute the unchanged scalar SQL expression in SQLite, not a Python
        # reimplementation. PostgreSQL queue locking remains a native contract.
        for query in (owner._CANDIDATES, owner._CANDIDATE_PAGE, owner._CLAIM):
            self.assertIn(owner._ELIGIBLE, query)
        with closing(sqlite3.connect(':memory:')) as db:
            db.create_function('clock_timestamp', 0, lambda: stamp(NOW))
            db.execute("ATTACH DATABASE ':memory:' AS public")
            db.execute('CREATE TABLE public.channels(user_work_id TEXT)')
            columns = ('is_hidden', 'reconnect_revision', 'revision', 'user_input',
                       'parent_ref', 'reconnect_state', 'reconnect_not_before',
                       'reconnect_error_code', 'reconnect_lease_expires_at')
            def eligible(**changes):
                row = dict(zip(columns, (False, 7, 7, 'saved', None, 'waiting',
                    stamp(NOW + timedelta(days=1)), 'provider_quota_exceeded',
                    stamp(NOW + timedelta(seconds=180)))))
                row.update(changes)
                sql = ('WITH candidate(' + ','.join(columns) + ') AS (VALUES (' +
                       ','.join('?' for _ in columns) + ')) SELECT COALESCE((' +
                       owner._ELIGIBLE + '),0) FROM candidate')
                return bool(db.execute(sql, tuple(row[k] for k in columns)).fetchone()[0])
            for reason in ('channel_add_rate_limited', 'channel_add_admission_unavailable'):
                self.assertTrue(eligible(reconnect_error_code=reason))
                for change in ({'is_hidden': True}, {'reconnect_revision': 6},
                               {'user_input': None}, {'parent_ref': 'missing'},
                               {'reconnect_state': 'blocked'}, {'reconnect_state': 'idle'},
                               {'reconnect_state': 'running'}):
                    self.assertFalse(eligible(reconnect_error_code=reason, **change))
            for reason in ('provider_quota_exceeded', 'provider_rate_limited',
                           'provider_unavailable', 'parent_pending', None):
                self.assertFalse(eligible(reconnect_error_code=reason))
                self.assertTrue(eligible(reconnect_error_code=reason,
                                         reconnect_not_before=stamp(NOW)))
            self.assertFalse(eligible(reconnect_not_before=None))
            self.assertTrue(eligible(reconnect_state='running',
                                     reconnect_lease_expires_at=stamp(NOW)))

    def test_sql_is_bounded_before_eligibility_and_adapts_real_driver_parameters(self):
        sql = owner._CANDIDATE_PAGE
        physical = sql.split(')\n SELECT physical.id::text', 1)[0]
        self.assertIn('LIMIT %(limit)s', physical)
        self.assertNotIn(owner._ELIGIBLE, physical)
        self.assertIn(owner._ELIGIBLE, sql)
        self.assertIn('reconnect_last_attempt_at<=%(ceiling)s::timestamptz', physical)
        self.assertIn('reconnect_requested_at<=%(ceiling)s::timestamptz', physical)
        for forbidden in ('FOR UPDATE', 'SKIP LOCKED', 'pg_try_advisory', ' OFFSET ', 'UPDATE public'):
            self.assertNotIn(forbidden, sql)
        params = dict(limit=100, ceiling=stamp(NOW), after_id=identity(1),
                      after_attempt=None, after_requested=stamp(REQUESTED))
        query = pg._reviewed_statement(sql, read=True, max_rows=100)
        adapted = PostgresQuery(Transformer())
        adapted.convert(query.as_string().encode(), params)
        self.assertTrue(adapted.query)


if __name__ == '__main__':
    unittest.main()
