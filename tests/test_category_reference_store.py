"""Category-reference lifetime, process ownership and legacy-file regressions."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from hot_storage import HOT_DATA_WINDOW, capture_read_lifetime
from youtube_category_reference import (
    CategoryReferenceStore, CategoryReferenceUnavailable, install_reference,
    load_reference_for_refresh, managed_reference_store,
    install_managed_reference_store, remove_managed_reference_store,
)

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def reference(at=NOW, label="Music"):
    return {
        "schema_version": 1,
        "retrieved_at": at.isoformat(),
        "clock_origin": "acquisition_started_at",
        "source": {"languages_url": "https://example.test/languages",
                   "categories_url": "https://example.test/categories"},
        "languages": {"en": "English", "fr": "French"},
        "categories": {"en": {"10": label, "20": "Gaming"}, "fr": {"10": "Musique"}},
        "validators": {"languages": "lang-etag", "categories": {"en": "en-etag", "fr": "fr-etag"}},
    }


class Repository:
    def __init__(self, value=None):
        self.value = deepcopy(value)
        self.reads = self.writes = 0
        self.error = None

    def load_reference(self):
        self.reads += 1
        if self.error:
            raise self.error
        return deepcopy(self.value)

    def replace_reference(self, value):
        self.writes += 1
        if self.error:
            raise self.error
        if self.value and self.value["retrieved_at"] > value["retrieved_at"]:
            raise ValueError("category_reference_newer_copy_exists")
        self.value = deepcopy(value)


class CategoryReferenceStoreTests(unittest.TestCase):
    def store(self, value=None):
        repository = Repository(value)
        clock = SimpleNamespace(wall=NOW, mono=10.0)
        store = CategoryReferenceStore(
            repository, clock=lambda: clock.wall, monotonic=lambda: clock.mono, auto_prune=False,
        )
        self.addCleanup(store.close)
        return store, repository, clock

    def test_labels_locales_and_id_fallback_preserve_public_shape(self):
        store, repository, _ = self.store(reference())
        for category, language, expected in [
            ("10", "fr_CA", "Musique"), ("20", "fr", "Gaming"), ("10", "de", "Music"),
            ("91", "en", "Category 91"), ("0", "en", "Unknown category"),
            ("non-id", "en", "Unknown category"),
        ]:
            self.assertEqual(store.category_name(category, language), expected)
        self.assertEqual(repository.reads, 1)

    def test_each_cached_label_admits_original_deadline(self):
        store, _, _ = self.store(reference())
        for _ in range(2):
            with capture_read_lifetime() as lifetime:
                self.assertEqual(store.category_name("10"), "Music")
            self.assertEqual(lifetime.expires_at, NOW + HOT_DATA_WINDOW)
        with capture_read_lifetime() as lifetime:
            self.assertEqual(store.category_name("91"), "Category 91")
        self.assertIsNone(lifetime.expires_at)

    def test_external_replacement_is_seen_within_one_second(self):
        store, repository, clock = self.store(reference())
        self.assertEqual(store.category_name("10"), "Music")
        repository.value = reference(label="Updated")
        clock.mono += 0.9
        self.assertEqual(store.category_name("10"), "Music")
        clock.mono += 0.1
        self.assertEqual(store.category_name("10"), "Updated")
        self.assertEqual(repository.reads, 2)

    def test_local_install_invalidates_cache_without_wait_or_file_write(self):
        store, repository, _ = self.store(reference(NOW - timedelta(hours=1)))
        self.assertEqual(store.category_name("10"), "Music")
        result = store.install(reference(label="Updated"))
        self.assertEqual(result, {"status": "ok", "languages": 2, "source_clock_preserved": True})
        self.assertEqual(repository.value["retrieved_at"], NOW.isoformat())
        self.assertEqual(store.category_name("10"), "Updated")

    def test_missing_reference_is_negative_cached_without_n_plus_one(self):
        store, repository, clock = self.store()
        for _ in range(100):
            self.assertEqual(store.category_name("10"), "Category 10")
        self.assertEqual(repository.reads, 1)
        clock.mono += 1
        self.assertEqual(store.category_name("10"), "Category 10")
        self.assertEqual(repository.reads, 2)

    def test_failed_refresh_does_not_serve_stale_cache_or_retry_each_label(self):
        store, repository, clock = self.store(reference())
        self.assertEqual(store.category_name("10"), "Music")
        repository.error = RuntimeError("offline test storage failure")
        clock.mono += 1
        for _ in range(100):
            self.assertEqual(store.category_name("10"), "Category 10")
        self.assertEqual(repository.reads, 2)
        with self.assertRaises(CategoryReferenceUnavailable):
            store.load_reference()

    def test_expired_future_and_invalid_reference_never_reaches_labels(self):
        for value in [
            reference(NOW - HOT_DATA_WINDOW),
            reference(NOW + timedelta(microseconds=1)),
            dict(reference(), clock_origin="copied_at"),
            dict(reference(), retrieved_at="invalid"),
            dict(reference(), categories={}),
        ]:
            with self.subTest(value=value["retrieved_at"]):
                store, _, _ = self.store(value)
                self.assertEqual(store.category_name("10"), "Category 10")
                self.assertIsNone(store.load_reference())

    def test_wall_expiry_applies_before_cached_reload_interval(self):
        store, _, clock = self.store(reference(NOW - HOT_DATA_WINDOW + timedelta(milliseconds=100)))
        self.assertEqual(store.category_name("10"), "Music")
        clock.wall += timedelta(milliseconds=100)
        clock.mono += 0.1
        self.assertEqual(store.category_name("10"), "Category 10")
        self.assertIsNone(store._data)

    def test_monotonic_expiry_survives_clock_rollback_and_repeated_reads(self):
        store, _, clock = self.store(reference(NOW - HOT_DATA_WINDOW + timedelta(seconds=2)))
        self.assertEqual(store.category_name("10"), "Music")
        clock.wall -= timedelta(minutes=5)
        clock.mono += 1
        self.assertEqual(store.category_name("10"), "Music")
        clock.mono += 1
        for _ in range(3):
            self.assertEqual(store.category_name("10"), "Category 10")
            clock.mono += 1
        self.assertIsNone(store.load_reference())
        store.install(reference(NOW - timedelta(minutes=6), label="Fresh"), now=clock.wall)
        self.assertEqual(store.category_name("10"), "Fresh")

    def test_expiry_evidence_survives_storage_failure(self):
        store, repository, clock = self.store(reference(NOW - HOT_DATA_WINDOW + timedelta(seconds=2)))
        self.assertEqual(store.category_name("10"), "Music")
        repository.error = RuntimeError("offline")
        clock.mono += 1
        self.assertEqual(store.category_name("10"), "Category 10")
        repository.error = None
        clock.wall -= timedelta(minutes=5)
        clock.mono += 1
        self.assertEqual(store.category_name("10"), "Category 10")

    def test_wall_rollback_before_acquisition_hides_reference(self):
        store, _, clock = self.store(reference())
        self.assertEqual(store.category_name("10"), "Music")
        clock.wall -= timedelta(seconds=1)
        self.assertEqual(store.category_name("10"), "Category 10")

    def test_older_install_is_rejected_without_changing_source_clock(self):
        store, repository, _ = self.store(reference())
        with self.assertRaisesRegex(ValueError, "newer_copy_exists"):
            store.install(reference(NOW - timedelta(seconds=1), label="Old"))
        self.assertEqual(repository.value, reference())

    def test_oversized_install_is_rejected_before_database_write(self):
        store, repository, _ = self.store()
        value = reference()
        value["validators"]["extra"] = "x" * (1024 * 1024)
        with self.assertRaisesRegex(ValueError, "size_exceeded"):
            store.install(value)
        self.assertEqual(repository.writes, 0)

    def test_write_unknown_outcome_is_not_retried(self):
        store, repository, _ = self.store(reference())
        repository.error = RuntimeError("write acknowledgement lost")
        with self.assertRaises(CategoryReferenceUnavailable):
            store.install(reference())
        self.assertEqual(repository.writes, 1)

    def test_legacy_cleanup_removes_current_copy_pending_and_lock_without_db(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reference.json"
            for suffix in ("", ".pending", ".lock"):
                target.with_name(target.name + suffix).write_text("old copy", encoding="utf-8")
            store = CategoryReferenceStore(legacy_path=target, auto_prune=False)
            self.assertEqual(store.purge_expired(), {"status": "expired_or_invalid", "removed": 3})
            self.assertEqual(list(Path(directory).iterdir()), [])
            self.assertEqual(store.purge_expired(), {"status": "absent", "removed": 0})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_default_cleanup_removes_both_names_only_under_process_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            targets = [home / '.local/state' / name / 'youtube-category-reference.json'
                       for name in ('leti-cook', 'let-it-cook')]
            for target in targets:
                target.parent.mkdir(parents=True)
                for suffix in ('', '.pending', '.lock'):
                    target.with_name(target.name + suffix).write_text('old copy', encoding='utf-8')
            outside = Path(directory) / 'another-installation' / 'reference.json'
            outside.parent.mkdir()
            outside.write_text('keep', encoding='utf-8')
            with patch.object(Path, 'home', return_value=home):
                store = CategoryReferenceStore(auto_prune=False)
                self.assertEqual(store.legacy_path, targets[0])
                self.assertEqual(store.purge_expired(), {'status': 'expired_or_invalid', 'removed': 6})
            self.assertEqual(outside.read_text(encoding='utf-8'), 'keep')
            self.assertFalse(any(home.rglob('*.json*')))

    def test_explicit_cleanup_does_not_touch_home_legacy_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            old = home / '.local/state/let-it-cook/youtube-category-reference.json'
            old.parent.mkdir(parents=True)
            old.write_text('keep', encoding='utf-8')
            target = Path(directory) / 'selected.json'
            target.write_text('remove', encoding='utf-8')
            with patch.object(Path, 'home', return_value=home):
                store = CategoryReferenceStore(legacy_path=target, auto_prune=False)
                self.assertEqual(store.purge_expired(), {'status': 'expired_or_invalid', 'removed': 1})
            self.assertEqual(old.read_text(encoding='utf-8'), 'keep')

    def test_legacy_cleanup_never_creates_missing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing" / "reference.json"
            store = CategoryReferenceStore(legacy_path=target, auto_prune=False)
            self.assertEqual(store.purge_expired()["status"], "absent")
            self.assertFalse(target.parent.exists())

    def test_legacy_cleanup_refuses_unsafe_path(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reference.json"
            target.write_text("keep", encoding="utf-8")
            store = CategoryReferenceStore(legacy_path=target, auto_prune=False)
            with patch.object(Path, "is_symlink", return_value=True):
                self.assertEqual(store.purge_expired()["status"], "unavailable")
            self.assertTrue(target.is_file())

    def test_unbound_and_closed_store_never_opens_database(self):
        store = CategoryReferenceStore(auto_prune=False)
        self.assertEqual(store.category_name("10"), "Category 10")
        self.assertIsNone(store.load_reference())
        with self.assertRaises(CategoryReferenceUnavailable):
            store.install(reference(), now=NOW)
        bound, repository, _ = self.store(reference())
        bound.close()
        self.assertEqual(bound.category_name("10"), "Category 10")
        self.assertEqual(repository.reads, 0)

    def test_process_binding_and_explicit_repository_share_no_pool_lifecycle(self):
        store, repository, _ = self.store(reference())
        with patch("youtube_category_reference._STORE", None):
            install_managed_reference_store(store)
            self.assertIs(managed_reference_store(), store)
            self.assertEqual(load_reference_for_refresh(now=NOW), reference())
            install_reference(reference(label="Updated"), now=NOW)
            self.assertEqual(load_reference_for_refresh(repository=repository, now=NOW)["categories"]["en"]["10"], "Updated")
            remove_managed_reference_store(store)
            self.assertTrue(store._closed)
            self.assertIsNone(managed_reference_store().repository)

    def test_explicit_install_cli_uses_existing_process_database_outside_fetch(self):
        from scripts.export_youtube_category_reference import main
        current = reference(datetime.now(timezone.utc) - timedelta(seconds=5))
        repository = Repository(current)
        events = []

        @contextmanager
        def resources():
            events.append("open")
            try:
                yield
            finally:
                events.append("close")

        original_load, original_replace = repository.load_reference, repository.replace_reference
        repository.load_reference = lambda: (events.append("read"), original_load())[1]
        repository.replace_reference = lambda value: (events.append("write"), original_replace(value))[1]
        database = object()
        core = SimpleNamespace(API_KEY="offline-placeholder", YOUTUBE_QUOTA_ATTEMPT_RECORDER=object(),
                               process_database_resources=resources, database=database)

        def exported(*args, **kwargs):
            events.append("fetch")
            self.assertEqual(kwargs["previous"], current)
            return current

        output = io.StringIO()
        with patch.dict(sys.modules, {"core": core}), patch.dict("os.environ", {"LIC_DEPLOYMENT_PROFILE": "self-hosted"}), \
                patch("db.category_reference.CategoryReferenceRepository", return_value=repository) as make, \
                patch("scripts.export_youtube_category_reference.export_reference", side_effect=exported), \
                patch("youtube_client.youtube_api_get", side_effect=AssertionError("No real provider request")), redirect_stdout(output):
            self.assertEqual(main(["--install", "--fetch", "--category-ids", "10"]), 0)
        make.assert_called_once_with(database)
        self.assertEqual(events, ["open", "read", "fetch", "write", "close"])
        self.assertTrue(json.loads(output.getvalue().splitlines()[-1])["application_data_writes"])


if __name__ == "__main__":
    unittest.main()
