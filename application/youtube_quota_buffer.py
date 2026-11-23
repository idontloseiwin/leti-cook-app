"""Bounded RAM delivery batches backed by a durable, host-local quota journal.

Only request-attempt metadata is buffered. This module never retries YouTube.
"""

import atexit
import hashlib
import logging
import os
import re
import uuid
from pathlib import Path
import threading
import time

from db.quota.youtube_quota_journal import QuotaJournal


BATCH_SIZE = 100
FLUSH_SECONDS = 5.0
MAX_RETRY_SECONDS = 60.0
MAX_BATCHES_PER_PASS = 10
LOGGER = logging.getLogger(__name__)


def quota_journal_config(*, project_id=None, database_id=None, environ=None):
    """Keep state outside releases and bind even an explicit path to its target."""
    values = os.environ if environ is None else environ
    project = project_id or values.get("YOUTUBE_QUOTA_PROJECT_ID", "primary").strip() or "primary"
    # Upgrade retains both the former namespace and path after writers stop.
    # New installs use stable installation identity, never a DSN or credential.
    namespace = values.get("YOUTUBE_QUOTA_JOURNAL_NAMESPACE", "").strip()
    if namespace:
        if re.fullmatch(r'[0-9a-f]{64}', namespace) is None:
            raise ValueError('quota_journal_namespace_invalid')
        if values.get('YOUTUBE_QUOTA_JOURNAL_NAMESPACE_PROJECT_ID') != project:
            raise ValueError('quota_journal_project_mismatch')
    else:
        destination = database_id if database_id is not None else values.get('LIC_DATABASE_ID', '')
        try:
            if str(uuid.UUID(destination)) != destination or uuid.UUID(destination).int == 0:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('quota_journal_identity_invalid') from None
        namespace = hashlib.sha256((destination + "\n" + project).encode()).hexdigest()
    configured = values.get("YOUTUBE_QUOTA_JOURNAL_PATH", "").strip()
    path = Path(configured).expanduser() if configured else (
        Path.home() / ".local" / "state" / "leti-cook" / "quota-journal" / namespace[:16] / "events.sqlite3"
    )
    return path, namespace


def read_quota_buffer_status(*, path=None, namespace=None):
    """Read local backlog only; never create state, start a thread, or contact DB."""
    if path is None:
        path, namespace = quota_journal_config()
    path = Path(path)
    result = {"mode": "journal", "status": "ok", "pending_events": 0,
              "oldest_pending_at": None, "last_error": None}
    try:
        if not path.is_absolute():
            raise ValueError("quota_journal_path_must_be_absolute")
        if path.exists() or path.is_symlink():
            result.update(QuotaJournal(path, namespace=namespace or "default", create=False).status())
    except Exception as error:
        result.update(status="error", pending_events=None, last_error=type(error).__name__)
    return result


class QuotaEventBuffer:
    """Persist before returning; deliver at least once, acknowledge exact events."""

    def __init__(self, repository, path, *, namespace="default", background=True,
                 flush_seconds=FLUSH_SECONDS):
        self.repository = repository
        self.path = Path(path)
        self.namespace = namespace
        self.background = background
        self.flush_seconds = flush_seconds
        self._journal = None
        self._state_lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._unflushed = 0
        self._errors = {}
        self._unrecorded_events = 0
        self._last_log = {}

    def _get_journal(self):
        with self._state_lock:
            if not self.path.is_absolute():
                raise ValueError("quota_journal_path_must_be_absolute")
            if self._journal is None:
                self._journal = QuotaJournal(self.path, namespace=self.namespace)
            return self._journal

    def _failure(self, operation, error):
        error_type = type(error).__name__
        now = time.monotonic()
        with self._state_lock:
            self._errors[operation] = error_type
            log = now - self._last_log.get(operation, float("-inf")) >= 30
            if log:
                self._last_log[operation] = now
        if log:
            try:
                LOGGER.warning("quota_buffer event=error operation=%s error_type=%s", operation, error_type)
            except Exception:
                pass

    def _persist(self, events):
        if self.repository.persist_events(events) is not True:
            raise RuntimeError("quota_batch_acknowledgement_mismatch")

    def record(self, event, *, fallback_events=None):
        try:
            self._get_journal().append(event)
        except Exception as error:
            self._failure("journal_write", error)
            # Disk trouble must not silently turn the journal into a RAM-only queue.
            # This RPC is idempotent if append committed but its acknowledgement failed.
            try:
                self._persist(fallback_events if fallback_events is not None else [event])
                return True
            except Exception as fallback_error:
                with self._state_lock:
                    self._unrecorded_events += 1
                self._failure("direct_fallback", fallback_error)
                return False
        with self._state_lock:
            self._errors.pop("journal_write", None)
            self._unflushed += 1
            wake = self._unflushed >= BATCH_SIZE
        self.start(recover=False)
        if wake:
            self._wake.set()
        return True

    def start(self, *, recover=True):
        """Backend calls this on startup; short-lived producers call it lazily."""
        if not self.background:
            return
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stop.is_set():
                return
            try:
                self._thread = threading.Thread(target=self._run, name="quota-journal", daemon=True)
                if recover:
                    self._wake.set()
                self._thread.start()
                self._errors.pop("worker_start", None)
                atexit.register(self.close)
            except Exception as error:
                self._failure("worker_start", error)

    def flush_once(self):
        """One bounded RPC; network work never holds a SQLite transaction."""
        if not self._flush_lock.acquire(blocking=False):
            return 0
        try:
            journal = self._get_journal()
            with journal.drain_lock() as acquired:
                if not acquired:
                    return 0
                batch = journal.read_batch(BATCH_SIZE)
                if not batch:
                    with self._state_lock:
                        self._errors.pop("flush", None)
                        self._unflushed = 0
                    return 0
                self._persist([event for _, event in batch])
                journal.acknowledge([sequence for sequence, _ in batch])
                with self._state_lock:
                    self._unflushed = max(0, self._unflushed - len(batch))
                    self._errors.pop("flush", None)
                return len(batch)
        except Exception as error:
            self._failure("flush", error)
            return -1
        finally:
            self._flush_lock.release()

    def _run(self):
        delay = self.flush_seconds
        while not self._stop.is_set():
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                break
            for _ in range(MAX_BATCHES_PER_PASS):
                count = self.flush_once()
                if count < 0:
                    # Fresh producers cannot turn an outage into an RPC retry loop.
                    self._stop.wait(delay)
                    delay = min(MAX_RETRY_SECONDS, max(self.flush_seconds, delay * 2))
                    break
                delay = self.flush_seconds
                if count < BATCH_SIZE or self._stop.is_set():
                    break
            else:
                self._wake.set()

    def close(self, timeout=2.0):
        """Bounded shutdown; unacknowledged events stay for the next backend start."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout))

    def status(self):
        result = read_quota_buffer_status(path=self.path, namespace=self.namespace)
        with self._state_lock:
            result["unrecorded_events"] = self._unrecorded_events
            if self._errors or self._unrecorded_events:
                result["status"] = "error"
                result["last_error"] = next(iter(self._errors.values()), "UnrecordedQuotaEvent")
        return result
