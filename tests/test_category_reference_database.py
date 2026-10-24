"""Reference ownership, exclusive lifetime and nonblocking cleanup contracts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

from db.base.postgres import DatabaseServerError, Operation, PostgresConfig, PostgresDatabase
from db.category_reference import CategoryReferenceRepository
from db.hot.hot_retention import CleanupPage, HotRetentionRepository, RetentionError, TARGET_FUNCTIONS
from hot_storage import HOT_DATA_WINDOW, HotSourceReadExpired, capture_read_lifetime
from hot_retention import HotCleanupCycle
import hot_runtime
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
AT = NOW.isoformat()
CUTOFF = (NOW - HOT_DATA_WINDOW).isoformat()
OLD = (NOW - timedelta(days=30)).isoformat()
FRESH = (NOW - timedelta(days=1)).isoformat()
TARGET = "category_reference"


def one(**value):
    return Result(rows=(value,))


class CategoryReferenceDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.connection = Connection(self.trace)
        self.pool = Pool(self.connection, self.trace)
        self.gates = []
        self.database = PostgresDatabase(
            PostgresConfig(DSN), admission=self.gates.append,
            pool_factory=lambda config: self.pool,
        ).open()
        self.addCleanup(self.database.close)
        self.repository = CategoryReferenceRepository(self.database, read_clock=lambda: NOW)

    def queue(self, *values):
        self.connection.results.extend(values)

    def cleanup_header(self, **changes):
        args = dict(target=TARGET, after_id=None, limit=100)
        args.update(changes)
        self.queue(one(**args), one(at=AT, cutoff=CUTOFF))

    def candidate(self, at=OLD, *, expired=True, invalid=False):
        return one(source_at=at, expired=expired, invalid_clock=invalid)

    def cleanup(self, **options):
        return self.database.cleanup_hot_category_reference(target=TARGET, **options)

    def test_read_uses_only_view_and_admits_original_source_deadline(self):
        value = dict(retrieved_at=FRESH, labels="original")
        deadline = NOW + timedelta(days=28)
        self.queue(one(document=value, source_expires_at=deadline.isoformat()))
        with capture_read_lifetime() as lifetime:
            self.assertEqual(self.repository.load_reference(), value)
        self.assertEqual(lifetime.expires_at, deadline)
        self.assertIn("readable.youtube_category_reference", self.connection.queries[0][0])
        self.assertNotIn("public.youtube_category_reference", self.connection.queries[0][0])
        self.assertEqual(self.pool.borrowed, 1)

    def test_read_missing_reference_is_none_and_boundary_expiry_fails_closed(self):
        self.queue(Result(rows=()))
        self.assertIsNone(self.repository.load_reference())
        self.queue(one(document={"retrieved_at": CUTOFF}, source_expires_at=AT))
        with self.assertRaises(HotSourceReadExpired):
            self.repository.load_reference()

    def test_replace_keeps_acquisition_clock_and_one_atomic_upsert(self):
        value = dict(retrieved_at=FRESH, labels="original")
        self.queue(one(singleton=True))
        self.assertIsNone(self.repository.replace_reference(value))
        self.assertEqual((self.pool.borrowed, self.connection.commits), (1, 1))
        sql, params = self.connection.queries[0]
        self.assertIn("ON CONFLICT (singleton) DO UPDATE", sql)
        self.assertIn("retrieved_at <= excluded.retrieved_at", sql)
        self.assertEqual(params["source"], FRESH)
        self.assertEqual(params["document"].obj, value)

    def test_older_copy_rejected_without_unconditional_retry(self):
        self.queue(Result(rows=()))
        with self.assertRaisesRegex(ValueError, "^category_reference_newer_copy_exists$"):
            self.repository.replace_reference(dict(retrieved_at=FRESH))
        self.assertEqual(len(self.connection.queries), 1)

    def test_invalid_future_and_expired_clocks_never_borrow_a_connection(self):
        for stamp in ("infinity", CUTOFF, OLD, (NOW + timedelta(seconds=1)).isoformat(), None):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.repository.replace_reference(dict(retrieved_at=stamp))
        self.assertEqual(self.pool.borrowed, 0)

    def test_cleanup_ignores_fresh_rows_and_completes_empty_singleton_page(self):
        self.cleanup_header()
        self.queue(Result(rows=()))
        result = self.cleanup()
        self.assertEqual((result["scanned"], result["deleted"], result["complete"]), (0, 0, True))
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(self.pool.borrowed, 1)
        self.assertEqual(set(self.gates), {"cleanup_hot_category_reference"})
        self.assertIn("retrieved_at<=", self.connection.queries[-1][0])

    def test_cleanup_rechecks_locked_row_before_delete(self):
        self.cleanup_header()
        self.queue(self.candidate(), one(source_at=OLD), Result(has_columns=False, rowcount=1))
        result = self.cleanup()
        self.assertEqual((result["scanned"], result["deleted"]), (1, 1))
        statements = [q for q, _ in self.connection.queries]
        self.assertIn("FOR UPDATE NOWAIT", statements[-2])
        self.assertIn("retrieved_at<=", statements[-1])
        self.assertIn("retrieved_at>clock_timestamp()", statements[-1])

    def test_concurrent_refresh_is_rechecked_and_missing_row_is_ignored(self):
        self.cleanup_header()
        self.queue(self.candidate(), one(source_at=FRESH), Result(has_columns=False, rowcount=0))
        self.assertEqual(self.cleanup()["deleted"], 0)
        self.cleanup_header()
        self.queue(self.candidate(), Result(rows=()))
        self.assertEqual(self.cleanup()["deleted"], 0)

    def test_busy_expired_row_reports_original_clock_without_waiting(self):
        self.cleanup_header()
        self.queue(self.candidate(), Result(execute_error=ServerFailure("55P03")))
        result = self.cleanup()
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["oldest_remaining_expired_source_at"], OLD)
        self.assertEqual(result["invalid_clock_remaining"], 0)
        self.assertEqual(self.pool.borrowed, 1)

    def test_busy_future_or_nonfinite_clock_is_reported_invalid(self):
        for stamp in ("infinity", "-infinity", (NOW + timedelta(days=1)).isoformat()):
            with self.subTest(stamp=stamp):
                self.cleanup_header()
                self.queue(self.candidate(stamp, expired=False, invalid=True),
                           Result(execute_error=ServerFailure("55P03")))
                result = self.cleanup()
                self.assertEqual((result["deferred"], result["invalid_clock_remaining"]), (1, 1))
                self.assertIsNone(result["oldest_remaining_expired_source_at"])

    def test_invalid_clock_is_deleted_when_unlocked(self):
        self.cleanup_header()
        self.queue(self.candidate("infinity", expired=False, invalid=True),
                   one(source_at="infinity"), Result(has_columns=False, rowcount=1))
        result = self.cleanup()
        self.assertEqual((result["deleted"], result["invalid_clock_remaining"]), (1, 0))

    def test_delete_error_rolls_back_without_replaying(self):
        self.cleanup_header()
        self.queue(self.candidate(), one(source_at=OLD),
                   Result(execute_error=ServerFailure("23514")))
        with self.assertRaises(DatabaseServerError):
            self.cleanup()
        self.assertEqual((self.pool.borrowed, self.connection.commits), (1, 0))

    def test_singleton_has_no_cursor_and_rejects_invalid_arguments(self):
        for args, code in (
            (dict(target="wrong", after_id=None, limit=100), "target"),
            (dict(target=TARGET, after_id=None, limit=0), "limit"),
            (dict(target=TARGET, after_id="anything", limit=100), "cursor"),
        ):
            self.queue(one(**args))
            with self.assertRaises(DatabaseServerError) as caught:
                self.cleanup()
            self.assertEqual(caught.exception.message, f"hot_category_reference_{code}_invalid")

    def test_exact_cutoff_is_expired_only_for_exclusive_reference_contract(self):
        self.cleanup_header()
        self.queue(self.candidate(CUTOFF), Result(execute_error=ServerFailure("55P03")))
        value = self.cleanup()
        self.assertEqual(CleanupPage.parse(TARGET, value, 100).deferred, 1)
        with self.assertRaises(RetentionError):
            CleanupPage.parse("video_snapshots", value, 100)

    def test_registered_target_dispatches_once_and_cycle_now_has_twenty_four(self):
        self.assertEqual(len(TARGET_FUNCTIONS), 24)
        self.assertEqual(TARGET_FUNCTIONS[TARGET], "cleanup_hot_category_reference")
        self.cleanup_header()
        self.queue(Result(rows=()))
        self.assertTrue(HotRetentionRepository(self.database).cleanup_page(TARGET).complete)

    def test_default_cleanup_cycle_visits_reference_as_twenty_fourth_target(self):
        visited = []

        def page(target, **options):
            visited.append(target)
            window = timedelta(days=30) if target == "deleted_saved_objects" else HOT_DATA_WINDOW
            return CleanupPage(
                target=target, as_of=AT, cutoff=(NOW - window).isoformat(),
                scanned=0, deleted=0, scrubbed=0, deferred=0, complete=True,
                next_cursor=None, oldest_remaining_expired_source_at=None,
                invalid_clock_remaining=0,
            )

        repository = Mock()
        repository.cleanup_page.side_effect = page
        result = HotCleanupCycle(repository).run_slice(clock=lambda: 0)
        self.assertEqual(visited, list(TARGET_FUNCTIONS))
        self.assertEqual(visited[-1], TARGET)
        self.assertEqual((result["status"], result["batches"]), ("scan_exhausted", 24))
        self.assertEqual(result["targets_not_visited"], [])

    def test_maintenance_capability_cannot_be_borrowed_with_arbitrary_sql(self):
        from db.base.postgres import _runtime_admission
        self.database._admission = _runtime_admission
        environment = {
            "LIC_DEPLOYMENT_PROFILE": "self-hosted", "LIC_DATABASE_TRANSPORT": "postgres",
            "LIC_DATABASE_ID": "91f5d9f3-9230-4ee6-b170-155d4c3c1719",
            "LIC_HOT_RUNTIME_SECRET": "synthetic_test_secret_0123456789abcdef",
        }
        controller = Mock()
        controller.require_collection_ready.side_effect = hot_runtime.HotRuntimeUnavailable("hot_runtime_not_ready")
        with patch.dict(os.environ, environment, clear=True), patch.object(hot_runtime, "_local_controller", controller):
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                self.cleanup()
            with hot_runtime.maintenance_access():
                self.cleanup_header()
                self.queue(Result(rows=()))
                self.cleanup()
                with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):
                    with self.database.transaction(Operation("cleanup_hot_category_reference")):
                        pass
        self.assertEqual(self.pool.borrowed, 1)


if __name__ == "__main__":
    unittest.main()
