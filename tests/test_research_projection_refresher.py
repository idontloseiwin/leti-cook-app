"""Background refresh of the research read model: pacing, gating and shutdown. No database."""
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.inventory.search_projection import REFRESH_LIMIT, SearchProjectionRepository  # noqa: E402
import research_projection_refresher as refresher  # noqa: E402
from research_projection_refresher import SearchProjectionRefresher  # noqa: E402


class Repository:
    """Each result is (markers taken, rows refreshed) or an exception to raise."""
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def refresh_counts(self, limit):
        self.calls.append(limit)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RefresherTests(unittest.TestCase):
    def test_work_continues_soon_and_an_empty_batch_idles(self):
        repository = Repository((50, 50), (1, 1), (0, 0))
        owner = SearchProjectionRefresher(repository, ready=lambda: None, limit=50)
        self.assertFalse(owner.initial_fill_complete)
        self.assertEqual(owner.step(), refresher.BUSY_SECONDS)
        self.assertFalse(owner.initial_fill_complete)
        self.assertEqual(owner.step(), refresher.BUSY_SECONDS)
        self.assertTrue(owner.initial_fill_complete)
        self.assertEqual(owner.step(), refresher.IDLE_SECONDS)
        self.assertEqual(repository.calls, [50, 50, 50])
        self.assertLess(refresher.BUSY_SECONDS, refresher.IDLE_SECONDS)

    def test_preparation_ends_while_collection_keeps_queueing(self):
        # Every batch finds new markers, as while collection writes; the first
        # batch that took all it found ends preparation without an empty one.
        owner = SearchProjectionRefresher(Repository((50, 50), (50, 12), (3, 3), (2, 2)),
                                          ready=lambda: None, limit=50)
        for ready in (False, False, True, True):
            self.assertEqual(owner.step(), refresher.BUSY_SECONDS)
            self.assertEqual(owner.initial_fill_complete, ready)

    def test_a_full_page_of_expired_rows_keeps_preparing(self):
        owner = SearchProjectionRefresher(Repository((0, 50), (10, 60), (10, 49)),
                                          ready=lambda: None, limit=50)
        owner.step()
        owner.step()
        self.assertFalse(owner.initial_fill_complete)
        owner.step()
        self.assertTrue(owner.initial_fill_complete)

    def test_closed_gate_skips_the_database_and_retries_soon_while_preparing(self):
        repository = Repository()
        def closed():
            raise RuntimeError('collection_not_admitted')
        owner = SearchProjectionRefresher(repository, ready=closed)
        for _ in range(refresher.FAILURES_BEFORE_UNAVAILABLE):
            self.assertFalse(owner.preparation_failing)
            self.assertEqual(owner.step(), refresher.PREPARING_RETRY_SECONDS)
        self.assertTrue(owner.preparation_failing)
        self.assertEqual(repository.calls, [])
        self.assertEqual(owner.last_error, 'RuntimeError')
        self.assertFalse(owner.initial_fill_complete)
        self.assertLess(refresher.PREPARING_RETRY_SECONDS, refresher.RETRY_SECONDS)

    def test_one_failure_in_a_row_while_preparing_is_not_an_outage(self):
        owner = SearchProjectionRefresher(
            Repository(ValueError('lock'), ValueError('lock'), (50, 50), ValueError('lock'), ValueError('lock')),
            ready=lambda: None, limit=50)
        owner.step()
        owner.step()
        self.assertFalse(owner.preparation_failing)
        owner.step()  # A success resets the failures in a row.
        owner.step()
        owner.step()
        self.assertFalse(owner.preparation_failing)
        self.assertFalse(owner.initial_fill_complete)

    def test_initial_admission_survives_later_writes_and_errors(self):
        owner = SearchProjectionRefresher(Repository((0, 0), (10, 10), ValueError('offline')),
                                          ready=lambda: None)
        for expected in (refresher.IDLE_SECONDS, refresher.BUSY_SECONDS, refresher.RETRY_SECONDS):
            self.assertEqual(owner.step(), expected)
            self.assertTrue(owner.initial_fill_complete)

    def test_database_error_keeps_only_a_class_name_and_recovers(self):
        repository = Repository(ValueError('SELECT secret FROM somewhere'), (0, 0))
        owner = SearchProjectionRefresher(repository, ready=lambda: None)
        self.assertEqual(owner.step(), refresher.PREPARING_RETRY_SECONDS)
        self.assertEqual(owner.last_error, 'ValueError')
        self.assertEqual(owner.step(), refresher.IDLE_SECONDS)
        self.assertIsNone(owner.last_error)
        self.assertEqual(repository.calls, [REFRESH_LIMIT, REFRESH_LIMIT])

    def test_thread_stops_promptly_and_runs_once(self):
        stepped = threading.Event()
        class Endless(Repository):
            def refresh_counts(self, limit):
                stepped.set()
                return 0, 0
        owner = SearchProjectionRefresher(Endless(), ready=lambda: None)
        owner.start()
        try:
            self.assertTrue(stepped.wait(5))
            with self.assertRaises(RuntimeError):
                owner.start()
        finally:
            owner.close()
        self.assertIsNone(owner._thread)
        owner.close()


class RepositoryTests(unittest.TestCase):
    def database(self, queued=('a0000000001',), updated=3):
        calls = []
        class Transaction:
            def returning_all(self, sql, parameters, *, max_rows):
                calls.append(('returning_all', sql, parameters, max_rows))
                return [{'video_id': video_id} for video_id in queued]
            def execute(self, sql, parameters):
                calls.append(('execute', sql, parameters))
                return updated
        class Database:
            operations = []
            @contextmanager
            def transaction(self, operation):
                self.operations.append(operation)
                yield Transaction()
        return Database(), calls

    def test_markers_then_one_update_in_one_bounded_transaction_without_jit(self):
        database, calls = self.database(queued=('a0000000001', 'a0000000002'), updated=5)
        self.assertEqual(SearchProjectionRepository(database).refresh(100), 7)
        operation, = database.operations
        self.assertFalse(operation.read_only)
        self.assertTrue(operation.disable_jit)
        self.assertLessEqual(operation.statement_timeout_ms, 10_000)
        self.assertGreater(operation.lock_timeout_ms, 0)
        (kind, dequeue, parameters, max_rows), (second, update, values) = calls
        self.assertEqual((kind, second, parameters, max_rows), ('returning_all', 'execute', {'limit': 100}, 100))
        # Ordinary statements as the backend role; no stored workflow function.
        self.assertTrue(dequeue.startswith('DELETE FROM public.video_search_projection_queue q'))
        self.assertIn('q.generation=taken.generation RETURNING q.video_id', dequeue)
        self.assertTrue(update.startswith('UPDATE public.video_search_projection p SET'))
        self.assertIn('FROM readable.hot_video_inventory h0', update)
        self.assertNotIn('RETURNING', update)
        self.assertEqual(values['queued'], ['a0000000001', 'a0000000002'])
        self.assertEqual(values['window'], '696 hours')
        self.assertEqual(set(values), {'queued', 'limit', 'window', 'duration_pattern', 'fold_map', 'fold_special'})

    def test_nothing_waiting_returns_zero(self):
        database, _ = self.database(queued=(), updated=0)
        self.assertEqual(SearchProjectionRepository(database).refresh(5), 0)

    def test_counts_separate_markers_from_refreshed_rows(self):
        database, _ = self.database(queued=('a0000000001', 'a0000000002'), updated=5)
        self.assertEqual(SearchProjectionRepository(database).refresh_counts(100), (2, 5))

    def test_limit_and_result_are_validated(self):
        database, _ = self.database()
        for limit in (0, 10_001, True, 1.5, '10'):
            with self.assertRaises(ValueError):
                SearchProjectionRepository(database).refresh(limit)
        self.assertEqual(database.operations, [])
        for updated in (None, -1, '3', 1.0):
            database, _ = self.database(updated=updated)
            with self.assertRaises(ValueError):
                SearchProjectionRepository(database).refresh(5)


if __name__ == '__main__':
    unittest.main()
