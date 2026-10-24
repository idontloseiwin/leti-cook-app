"""DB-backed category labels with one bounded, source-expiring memory cache.

Refresh is explicit; this module makes no YouTube requests. The process owner
injects its repository. The maintenance cycle owns deletion from PostgreSQL;
this module only removes legacy files and expires its local cache.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import stat
import threading
import time

from hot_storage import HOT_DATA_WINDOW, register_read_deadline, utc_instant

MAX_REFERENCE_BYTES = 1024 * 1024
REFERENCE_CACHE_SECONDS = 1.0


class CategoryReferenceUnavailable(OSError):
    """The process has no usable category-reference storage."""


def default_reference_path():
    """Legacy path, used only to delete the former managed file."""
    return Path.home() / ".local/state/leti-cook/youtube-category-reference.json"


def _safe_parent(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("unsafe_category_reference_path")
        if parent.exists() and getattr(parent.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError("unsafe_category_reference_path")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("unsafe_category_reference_path")
    return path


def validate_reference(value, *, now):
    from youtube_categories import _validate_category_registry
    if not isinstance(value, dict) or value.get("clock_origin") != "acquisition_started_at":
        raise ValueError("category_reference_clock_origin_missing")
    languages, categories = _validate_category_registry(value)
    source = utc_instant(value["retrieved_at"])
    deadline = source + HOT_DATA_WINDOW
    reference = utc_instant(now)
    if not source <= reference < deadline:
        raise ValueError("category_reference_expired_or_future")
    if len(languages) > 200 or any(len(labels) > 100 for labels in categories.values()):
        raise ValueError("category_reference_size_exceeded")
    if any(len(name) > 200 for name in languages.values()) or any(
        len(label) > 300 for labels in categories.values() for label in labels.values()
    ):
        raise ValueError("category_reference_size_exceeded")
    return languages, categories, deadline


def _validate_bounded_reference(value, *, now):
    parsed = validate_reference(value, now=now)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) + 1 > MAX_REFERENCE_BYTES:
        raise ValueError("category_reference_size_exceeded")
    return parsed


def load_reference_for_refresh(*, repository=None, now=None):
    """Return a validated DB copy, releasing its transaction before any HTTP."""
    store = managed_reference_store() if repository is None else CategoryReferenceStore(repository, auto_prune=False)
    return store.load_reference(now=now)


def install_reference(value, *, repository=None, now=None):
    """Replace the DB copy atomically, retaining its original acquisition time."""
    store = managed_reference_store() if repository is None else CategoryReferenceStore(repository, auto_prune=False)
    return store.install(value, now=now)


class CategoryReferenceStore:
    """One reference in RAM, reloaded at most once per second and checked per hit."""
    def __init__(self, repository=None, *, legacy_path=None,
                 clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic,
                 auto_prune=True):
        self.repository = repository
        self.legacy_path = Path(legacy_path) if legacy_path is not None else default_reference_path()
        # Compatibility cleanup stays under this process owner's home. An explicit
        # test/managed path never widens cleanup to another directory.
        self._legacy_paths = (self.legacy_path,) if legacy_path is not None else (
            self.legacy_path, Path.home() / ".local/state/let-it-cook/youtube-category-reference.json",
        )
        self.clock, self.monotonic, self.auto_prune = clock, monotonic, auto_prune
        self._lock = threading.RLock()
        self._data = self._source = self._deadline = self._monotonic_deadline = None
        self._reload_at = 0.0
        self._rejected_source = None
        self._timer = None
        self._closed = False

    def _clear(self):
        self._data = None
        self._reload_at = 0.0

    def _reject_source(self):
        if self._source is not None:
            self._rejected_source = max(self._source, self._rejected_source or self._source)
        self._clear()

    def _schedule(self):
        if self.auto_prune and not self._closed and self._timer is None and self._data is not None:
            remaining = self._monotonic_deadline - self.monotonic()
            self._timer = threading.Timer(max(0.01, min(60, remaining)), self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _expire(self):
        with self._lock:
            self._timer = None
            self._expire_cache()
            self._schedule()

    def _expire_cache(self):
        if self._deadline is None:
            return
        now = utc_instant(self.clock())
        if now < self._source or now >= self._deadline or self.monotonic() >= self._monotonic_deadline:
            self._rejected_source = max(self._source, self._rejected_source or self._source)
            if self._data is not None:
                self._clear()

    def close(self):
        with self._lock:
            self._closed = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._clear()

    def purge_expired(self):
        """Prune RAM and remove legacy files; DB deletion belongs to maintenance."""
        with self._lock:
            self._expire_cache()
            removed = 0
            try:
                # Do not open/create a lock file: no category files are written now.
                for legacy_path in self._legacy_paths:
                    for suffix in ("", ".pending", ".lock"):
                        target = _safe_parent(legacy_path.with_name(legacy_path.name + suffix))
                        try:
                            target.unlink()
                            removed += 1
                        except FileNotFoundError:
                            pass
            except (OSError, ValueError):
                return {"status": "unavailable", "removed": removed}
            return {"status": "expired_or_invalid" if removed else "absent", "removed": removed}

    def load_reference(self, *, now=None):
        with self._lock:
            if self.repository is None or self._closed:
                return None
            self._expire_cache()
            try:
                value = self.repository.load_reference()
            except Exception as error:
                raise CategoryReferenceUnavailable("category_reference_storage_unavailable") from error
            if value is None:
                return None
            try:
                _validate_bounded_reference(value, now=now or self.clock())
                if self._rejected_source is not None and utc_instant(value["retrieved_at"]) <= self._rejected_source:
                    return None
            except (ValueError, TypeError, KeyError, OverflowError):
                return None
            return value

    def install(self, value, *, now=None):
        with self._lock:
            _validate_bounded_reference(value, now=now or self.clock())
            if self.repository is None or self._closed:
                raise CategoryReferenceUnavailable("category_reference_storage_unavailable")
            try:
                self.repository.replace_reference(value)
            except ValueError:
                raise
            except Exception as error:
                raise CategoryReferenceUnavailable("category_reference_storage_unavailable") from error
            finally:
                # A lost write acknowledgement is not retried; invalidate either way.
                self._clear()
            return {"status": "ok", "languages": len(value["languages"]), "source_clock_preserved": True}

    def category_name(self, category_id, language_code="en"):
        key = str(category_id or "").strip()
        fallback = f"Category {key}" if key.isascii() and key.isdigit() and 1 <= len(key) <= 9 and key[0] != "0" else "Unknown category"
        with self._lock:
            if self.repository is None or self._closed:
                return fallback
            now, monotonic = utc_instant(self.clock()), self.monotonic()
            try:
                self._expire_cache()
                if monotonic >= self._reload_at:
                    value = self.load_reference(now=now)
                    if value is None:
                        self._clear()
                        self._reload_at = monotonic + REFERENCE_CACHE_SECONDS
                        return fallback
                    now, monotonic = utc_instant(self.clock()), self.monotonic()
                    languages, labels, deadline = validate_reference(value, now=now)
                    source = utc_instant(value["retrieved_at"])
                    monotonic_deadline = monotonic + (deadline - now).total_seconds()
                    if source == self._source:
                        monotonic_deadline = min(monotonic_deadline, self._monotonic_deadline)
                    self._data = (languages, labels)
                    self._source, self._deadline = source, deadline
                    self._monotonic_deadline = monotonic_deadline
                    self._reload_at = monotonic + REFERENCE_CACHE_SECONDS
                if self._data is None:
                    return fallback
                if now < self._source or now >= self._deadline or monotonic >= self._monotonic_deadline:
                    self._reject_source()
                    return fallback
                languages, labels = self._data
                codes = {code.lower(): code for code in languages}
                requested = str(language_code or "en").strip().replace("_", "-").lower()
                locale = codes.get(requested) or codes.get(requested.split("-", 1)[0]) or "en"
                result = labels[locale].get(key, labels["en"].get(key))
                if result is None:
                    return fallback
                register_read_deadline(self._deadline)
                return result
            except (OSError, ValueError, UnicodeError, TypeError, KeyError, OverflowError):
                self._clear()
                self._reload_at = monotonic + REFERENCE_CACHE_SECONDS
                return fallback
            finally:
                self._schedule()


_STORE = None
_STORE_LOCK = threading.Lock()


def managed_reference_store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            # An unbound process can only return ID labels; it opens no DB pool.
            _STORE = CategoryReferenceStore()
        return _STORE


def install_managed_reference_store(store):
    global _STORE
    with _STORE_LOCK:
        previous, _STORE = _STORE, store
    if previous is not None and previous is not store:
        previous.close()


def remove_managed_reference_store(store):
    global _STORE
    with _STORE_LOCK:
        if _STORE is store:
            _STORE = None
    store.close()
