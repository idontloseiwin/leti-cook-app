"""Real PostgreSQL owner with a local connection: no database/server is opened."""
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'application'))
from catalogue_browse import RecordingCatalogueRepository
from catalogue_scope import CatalogueReadBudget, CatalogueScopeError, CatalogueScopeRepository
from db.base import postgres as pg
from db.base.read_context import http_read_scope, http_method_scope
from db.reads.channel_explorer import ChannelExplorerRepository
from hot_storage import HotSourceReadExpired, capture_read_lifetime
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
CHANNEL = 'UC'+'x'*22


def row(index, *, expiry=None):
    return dict(video_id=f'v{index:010d}', published_at=None,
                source_checked_at=NOW.isoformat(),
                source_expires_at=(expiry or NOW+timedelta(days=1)).isoformat())


class SourceCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = deque(connection.source_rows)

    def __enter__(self):
        self.connection.events.append(('source_enter',))
        return self

    def __exit__(self, *error):
        self.connection.source_closes += 1
        self.connection.events.append(('source_close', error[0]))
        if self.connection.close_error:
            raise self.connection.close_error

    def execute(self, statement, params):
        self.connection.queries.append((statement, params))
        self.connection.events.append(('source_dispatch',))

    def fetchmany(self, size):
        c = self.connection
        c.source_sizes.append(size)
        if c.fetch_error_at == len(c.source_sizes):
            raise ServerFailure('08006')
        values = ([row(n) for n in range(size+1)] if c.oversized else
                  [self.rows.popleft() for _ in range(min(size, len(self.rows)))])
        if c.on_fetch:
            c.on_fetch(values)
        return values


class SourceConnection(Connection):
    def __init__(self, rows):
        super().__init__([])
        self.source_rows = rows
        self.source_sizes, self.source_options = [], []
        self.source_closes = 0
        self.close_error = self.on_fetch = self.fetch_error_at = None
        self.oversized = False

    def cursor(self, **options):
        if options:
            self.source_options.append(options)
            return SourceCursor(self)
        return super().cursor()


class InventorySourceStreamTests(unittest.TestCase):
    def database(self, rows, *, gate=None, clock=None):
        connection = SourceConnection(rows)
        pool = Pool(connection, connection.events)
        database = pg.PostgresDatabase(pg.PostgresConfig(DSN), admission=gate or (lambda name: None),
                                       pool_factory=lambda config: pool).open()
        self.addCleanup(database.close)
        repo = ChannelExplorerRepository(database, read_clock=clock or (lambda: NOW))
        return repo, database, connection, pool

    def test_real_owner_one_reader_statement_and_bounded_fetch_through_eof(self):
        repo, db, c, pool = self.database([row(n) for n in range(1001)])
        with http_method_scope('GET'), http_read_scope():
            result = CatalogueScopeRepository(repo).load_full_long_inventory(CHANNEL)
        self.assertEqual(len(result), 1001)
        self.assertEqual(c.source_sizes, [1000, 1000, 1000])
        self.assertEqual(len(c.queries), 1)
        sql, params = c.queries[0]
        self.assertIn('FROM readable.hot_video_inventory', sql)
        self.assertNotIn('public.', sql)
        self.assertNotIn(CHANNEL, sql)
        self.assertEqual(params, (CHANNEL, 'long'))
        self.assertEqual(c.source_options, [dict(name='lic_inventory_source', scrollable=False,
                         withhold=False, row_factory=pg.dict_row)])
        self.assertIn(('SET LOCAL ROLE lic_reader', ()), c.setup)
        self.assertIn(('SET TRANSACTION READ ONLY', ()), c.setup)
        self.assertEqual((c.source_closes, c.commits, c.rollbacks, pool.returned), (1, 1, 0, 1))

    def test_all_three_format_types_keep_fixed_scope(self):
        for kind in ('long', 'short', 'stream'):
            with self.subTest(kind=kind):
                repo, db, c, pool = self.database([row(0)])
                self.assertEqual(repo.load_format_inventory(CHANNEL, video_type=kind, limit=2), [row(0)])
                self.assertEqual(c.queries[0][1], (CHANNEL, kind))
                self.assertEqual(c.source_sizes, [2, 1])
                self.assertEqual(c.source_closes, 1)

    def test_capability_requires_readonly_finite_ordinary_owner(self):
        for values in ({'read_only': False, 'total_timeout_ms': 60_000},
                       {'read_only': True},
                       {'read_only': True, 'total_timeout_ms': 60_000, 'rpc_name': 'cleanup_hot_inventory'}):
            c = SourceConnection([])
            tx = pg.Transaction(c, Mock(), **values)
            with self.assertRaises(pg.DatabaseError):
                with tx.inventory_source_rows(CHANNEL, 'long'):
                    self.fail('not admitted')
            self.assertEqual(c.source_options, [])
            self.assertTrue(tx._failed)

    def test_invalid_fetch_sizes_never_dispatch_and_poison_owner(self):
        for size in (0, 1001, True):
            c = SourceConnection([])
            tx = pg.Transaction(c, Mock(), read_only=True, total_timeout_ms=60_000)
            with self.assertRaises(ValueError):
                with tx.inventory_source_rows(CHANNEL, 'long') as stream:
                    stream.fetch(size)
            self.assertEqual(c.source_sizes, [])
            self.assertEqual(c.source_closes, 1)
            self.assertTrue(tx._failed)

    def test_oversized_native_buffer_fails_closed(self):
        repo, db, c, pool = self.database([])
        c.oversized = True
        with self.assertRaisesRegex(pg.DatabaseError, 'result_limit'):
            list(repo.iter_long_inventory_pages(CHANNEL))
        self.assertEqual((c.source_closes, c.rollbacks), (1, 1))

    def test_explicit_empty_fetch_is_required_for_zero_rows(self):
        repo, db, c, pool = self.database([])
        self.assertEqual(list(repo.iter_long_inventory_pages(CHANNEL)), [])
        self.assertEqual(c.source_sizes, [1000])
        self.assertEqual((c.source_closes, c.commits), (1, 1))

    def test_direct_early_abandonment_closes_and_returns_connection_once(self):
        repo, db, c, pool = self.database([row(n) for n in range(1001)])
        iterator = repo.iter_long_inventory_pages(CHANNEL)
        self.assertEqual(len(next(iterator)), 1000)
        iterator.close()
        iterator.close()
        self.assertEqual(c.source_sizes, [1000])
        self.assertEqual((c.source_closes, c.rollbacks, pool.returned), (1, 1, 1))

    def test_recording_and_scope_budget_abort_forward_close(self):
        repo, db, c, pool = self.database([row(n) for n in range(1001)])
        captured = RecordingCatalogueRepository(repo)
        with self.assertRaisesRegex(CatalogueScopeError, 'byte_budget'):
            CatalogueScopeRepository(captured, CatalogueReadBudget(max_bytes=1)).load_full_long_inventory(CHANNEL)
        self.assertEqual(c.source_sizes, [1000])
        self.assertEqual((c.source_closes, c.rollbacks, pool.returned), (1, 1, 1))

    def test_caller_time_budget_aborts_before_another_fetch(self):
        clock = [0.0]
        repo, db, c, pool = self.database([row(n) for n in range(1001)])
        c.on_fetch = lambda values: clock.__setitem__(0, 2.0)
        with self.assertRaisesRegex(CatalogueScopeError, 'deadline'):
            CatalogueScopeRepository(repo, CatalogueReadBudget(timeout_seconds=1, clock=lambda: clock[0])).load_full_long_inventory(CHANNEL)
        self.assertEqual(c.source_sizes, [1000])
        self.assertEqual((c.source_closes, c.rollbacks, pool.returned), (1, 1, 1))

    def test_total_operation_timeout_is_not_reset_between_fetches(self):
        clock = [0.0]
        with patch.object(pg.time, 'monotonic', side_effect=lambda: clock[0]):
            repo, db, c, pool = self.database([row(n) for n in range(1001)])
            iterator = repo.iter_long_inventory_pages(CHANNEL)
            next(iterator)
            clock[0] = 61.0
            with self.assertRaises(pg.DatabaseServerError) as caught:
                next(iterator)
        self.assertEqual(caught.exception.code, '57014')
        self.assertEqual(c.source_sizes, [1000])
        self.assertEqual((c.source_closes, c.rollbacks), (1, 1))

    def test_hot_admission_denial_between_fetches_closes_without_replay(self):
        denied = [False]
        def gate(name):
            if denied[0]: raise RuntimeError('hot_denied')
        repo, db, c, pool = self.database([row(n) for n in range(1001)], gate=gate)
        iterator = repo.iter_long_inventory_pages(CHANNEL)
        next(iterator)
        denied[0] = True
        with self.assertRaisesRegex(RuntimeError, 'hot_denied'):
            next(iterator)
        self.assertEqual((len(c.queries), c.source_closes, c.rollbacks), (1, 1, 1))
        self.assertEqual(c.source_sizes, [1000])

    def test_earliest_source_is_rechecked_after_eof_and_transaction_close(self):
        clock = [NOW]
        repo, db, c, pool = self.database([row(0, expiry=NOW+timedelta(seconds=1))], clock=lambda: clock[0])
        c.on_fetch = lambda values: clock.__setitem__(0, NOW+timedelta(seconds=2)) if not values else None
        with self.assertRaises(HotSourceReadExpired):
            CatalogueScopeRepository(repo).load_full_long_inventory(CHANNEL)
        self.assertEqual((c.source_closes, c.commits, pool.returned), (1, 1, 1))

    def test_fetch_connection_failure_after_first_page_is_not_replayed(self):
        repo, db, c, pool = self.database([row(n) for n in range(1001)])
        c.fetch_error_at = 2
        iterator = repo.iter_long_inventory_pages(CHANNEL)
        next(iterator)
        with self.assertRaises(pg.DatabaseUnavailable) as caught:
            next(iterator)
        self.assertFalse(caught.exception.outcome_unknown)  # Read-only; still no complete bag.
        self.assertEqual((len(c.queries), c.source_closes, c.rollbacks), (1, 1, 1))

    def test_cursor_close_failure_cannot_be_reported_as_eof(self):
        repo, db, c, pool = self.database([])
        c.close_error = ServerFailure('08006')
        with self.assertRaises(pg.DatabaseUnavailable):
            list(repo.iter_long_inventory_pages(CHANNEL))
        self.assertEqual((len(c.queries), c.source_closes, c.rollbacks, pool.returned), (1, 1, 1, 1))

    def test_transaction_exit_failure_cannot_be_reported_as_eof(self):
        repo, db, c, pool = self.database([row(0)])
        c.commit_error = ServerFailure('08006')
        with self.assertRaises(pg.DatabaseUnavailable):
            list(repo.iter_long_inventory_pages(CHANNEL))
        self.assertEqual((len(c.queries), c.source_closes, c.rollbacks, pool.returned), (1, 1, 1, 1))

    def test_overflow_closes_before_legacy_fallback_and_uses_only_prefix_deadline(self):
        early, late = NOW+timedelta(seconds=1), NOW+timedelta(hours=2)
        repo, db, c, pool = self.database([row(n, expiry=early) for n in range(4)])
        prefix = [row(n, expiry=late) for n in (10, 9, 8)]
        c.results.append(Result(rows=tuple(prefix)))
        with capture_read_lifetime() as lifetime:
            result = repo.load_format_inventory(CHANNEL, video_type='long', limit=3)
        self.assertEqual((result, lifetime.expires_at), (prefix, late))
        self.assertEqual(c.source_sizes, [3])
        events = [event[0] for event in c.events]
        self.assertLess(events.index('source_close'), events.index('cursor_enter'))
        self.assertEqual((c.source_closes, c.commits, pool.returned), (1, 2, 2))

    def test_overflow_close_failure_does_not_start_fallback(self):
        repo, db, c, pool = self.database([row(n) for n in range(4)])
        c.close_error = ServerFailure('08006')
        with self.assertRaises(pg.DatabaseUnavailable):
            repo.load_format_inventory(CHANNEL, video_type='short', limit=3)
        self.assertEqual((len(c.queries), c.source_closes, pool.returned), (1, 1, 1))

    def test_close_failure_during_budget_abort_is_visible(self):
        repo, db, c, pool = self.database([row(0)])
        c.close_error = ServerFailure('08006')
        with self.assertRaises(pg.DatabaseUnavailable):
            CatalogueScopeRepository(repo, CatalogueReadBudget(max_bytes=1)).load_full_long_inventory(CHANNEL)
        self.assertEqual((c.source_closes, c.rollbacks, pool.returned), (1, 1, 1))

    def test_stream_cannot_escape_owner_thread_or_context(self):
        c = SourceConnection([row(0)])
        tx = pg.Transaction(c, Mock(), read_only=True, total_timeout_ms=60_000)
        errors = []
        with self.assertRaises(pg.DatabaseError):
            with tx.inventory_source_rows(CHANNEL, 'long') as stream:
                def foreign():
                    try: stream.fetch()
                    except pg.DatabaseError as error: errors.append(error)
                worker = threading.Thread(target=foreign)
                worker.start(); worker.join()
                self.assertEqual(len(errors), 1)
        self.assertEqual(c.source_sizes, [])
        self.assertEqual(c.source_closes, 1)
        with self.assertRaises(pg.DatabaseError): stream.fetch()


if __name__ == '__main__': unittest.main()
