"""Bounded, private disk queue for generated quota-attempt metadata.

Append commits before returning; acknowledgement deletes exact delivered event
sequences. Network delivery belongs to the caller and never runs in a SQLite
transaction. DELETE/EXTRA avoids depending on a particular SQLite WAL fix.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
import errno
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
from zoneinfo import ZoneInfo


MAX_EVENT_BYTES = 8 * 1024
MAX_BATCH_EVENTS = 100
DEFAULT_MAX_EVENTS = 50_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
SCHEMA_VERSION = 1
APPLICATION_ID = 0x5954514A
BUSY_TIMEOUT_MS = 1_000

_META_SQL = """CREATE TABLE quota_journal_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    namespace TEXT NOT NULL,
    pending_events INTEGER NOT NULL CHECK (pending_events >= 0),
    pending_bytes INTEGER NOT NULL CHECK (pending_bytes >= 0)
)"""
_EVENTS_SQL = """CREATE TABLE quota_journal_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_bytes INTEGER NOT NULL CHECK (payload_bytes > 0 AND payload_bytes <= 8192)
)"""
_START_FIELDS = frozenset({
    "request_attempt_id", "project_id", "quota_bucket", "quota_day", "phase",
    "endpoint", "quota_units", "status", "started_at", "retry_of_request_attempt_id",
})
_IDENTIFIER = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
_PACIFIC = ZoneInfo("America/Los_Angeles")
_FINISH_FIELDS = frozenset({
    "status", "finished_at", "duration_ms", "http_status", "error_type",
})


class QuotaJournalError(RuntimeError):
    """Safe error category: messages never contain event content or local paths."""


class QuotaJournalStorageError(QuotaJournalError):
    pass


class QuotaJournalFullError(QuotaJournalError):
    pass


class QuotaJournalSchemaError(QuotaJournalError):
    pass


class QuotaJournalUnsafePathError(QuotaJournalError):
    pass


class QuotaJournalPayloadError(QuotaJournalError):
    pass


def _positive_integer(value, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise QuotaJournalPayloadError("Quota journal bound is invalid")
    return value


def _require_pattern(value, pattern):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise QuotaJournalPayloadError("Quota journal metadata label is invalid")


def _require_integer(value, minimum, maximum=2**31 - 1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise QuotaJournalPayloadError("Quota journal metadata integer is invalid")


def _timestamp(value):
    _require_pattern(value, _TIMESTAMP)
    try:
        if not value.endswith("Z") and (int(value[-5:-3]) > 15 or int(value[-2:]) > 59):
            raise ValueError
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QuotaJournalPayloadError("Quota journal metadata timestamp is invalid") from None


def _encode_event(event):
    if not isinstance(event, Mapping) or set(event) != {
        "kind", "request_attempt_id", "project_id", "values",
    }:
        raise QuotaJournalPayloadError("Quota journal envelope is invalid")
    kind = event["kind"]
    if not isinstance(kind, str) or kind not in {"start", "finish"}:
        raise QuotaJournalPayloadError("Quota journal event kind is invalid")
    _require_pattern(event["request_attempt_id"], _IDENTIFIER)
    _require_pattern(event["project_id"], _PROJECT)
    values = event["values"]
    if not isinstance(values, Mapping):
        raise QuotaJournalPayloadError("Quota journal values are invalid")
    fields = set(values)
    if kind == "start":
        if not _START_FIELDS - {"retry_of_request_attempt_id"} <= fields <= _START_FIELDS:
            raise QuotaJournalPayloadError("Quota journal start fields are invalid")
        if any(values.get(field) != event[field] for field in ("request_attempt_id", "project_id")):
            raise QuotaJournalPayloadError("Quota journal start identity is invalid")
        for field in ("quota_bucket", "phase", "endpoint"):
            _require_pattern(values[field], _LABEL)
        if values["status"] != "started":
            raise QuotaJournalPayloadError("Quota journal start status is invalid")
        _require_integer(values["quota_units"], 1)
        started_at = _timestamp(values["started_at"])
        quota_day = values["quota_day"]
        try:
            if (not isinstance(quota_day, str) or len(quota_day) != 10
                    or date.fromisoformat(quota_day) != started_at.astimezone(_PACIFIC).date()):
                raise ValueError
        except (ValueError, OverflowError):
            raise QuotaJournalPayloadError("Quota journal quota day is invalid") from None
        retry = values.get("retry_of_request_attempt_id")
        if retry is not None:
            _require_pattern(retry, _IDENTIFIER)
            if retry == event["request_attempt_id"]:
                raise QuotaJournalPayloadError("Quota journal retry identity is invalid")
    else:
        if fields != _FINISH_FIELDS:
            raise QuotaJournalPayloadError("Quota journal finish fields are invalid")
        if values["status"] not in ("success", "http_error", "transport_error"):
            raise QuotaJournalPayloadError("Quota journal finish status is invalid")
        _timestamp(values["finished_at"])
        _require_integer(values["duration_ms"], 0)
        if values["http_status"] is not None:
            _require_integer(values["http_status"], 100, 599)
        if values["error_type"] is not None:
            _require_pattern(values["error_type"], _LABEL)
    try:
        payload = json.dumps(dict(event, values=dict(values)), ensure_ascii=True,
                             separators=(",", ":"), allow_nan=False)
        size = len(payload.encode("utf-8"))
    except (TypeError, ValueError, OverflowError):
        raise QuotaJournalPayloadError("Quota journal serialization failed") from None
    if size > MAX_EVENT_BYTES:
        raise QuotaJournalPayloadError("Quota journal event exceeds its size bound")
    return payload, size


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _check_private(path, *, directory=False):
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if _is_link(info) or not expected_type(info.st_mode):
        raise QuotaJournalUnsafePathError("Quota journal path is not a private regular path")
    if not directory and info.st_nlink != 1:
        raise QuotaJournalUnsafePathError("Quota journal file has additional links")
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise QuotaJournalUnsafePathError("Quota journal path permissions are not private")


def _sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class QuotaJournal:
    """Short local transactions, a bounded delivery batch, and no silent eviction.

    ``namespace`` binds an explicit path to one destination/project fingerprint.
    The caller owns retries and sticky delivery/storage error reporting. This
    class neither recreates a damaged file nor acknowledges an uncertain RPC.
    """

    def __init__(self, path, *, namespace="default", max_events=DEFAULT_MAX_EVENTS,
                 max_bytes=DEFAULT_MAX_BYTES, create=True):
        self.path = Path(path).absolute()
        if not isinstance(namespace, str) or not namespace or len(namespace) > 256:
            raise QuotaJournalSchemaError("Quota journal namespace is invalid")
        self.namespace = namespace
        self.max_events = _positive_integer(max_events, DEFAULT_MAX_EVENTS)
        self.max_bytes = _positive_integer(max_bytes, DEFAULT_MAX_BYTES)
        self._readonly = not create
        self.lock_path = self.path.with_name(self.path.name + ".drain.lock")
        try:
            if self._readonly:
                with self._connection():
                    pass
                return
            newly_created = self._prepare_path()
            with self._connection(initialize=True) as connection:
                with self._transaction(connection):
                    objects = self._schema_objects(connection)
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    application = connection.execute("PRAGMA application_id").fetchone()[0]
                    if not objects and not version and not application:
                        if not newly_created:
                            raise QuotaJournalSchemaError("Existing quota journal is empty or incomplete")
                        connection.execute(_META_SQL)
                        connection.execute(_EVENTS_SQL)
                        connection.execute(
                            "INSERT INTO quota_journal_meta VALUES (1, ?, 0, 0)",
                            (self.namespace,),
                        )
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                    self._validate_schema(connection)
        except OSError:
            raise QuotaJournalStorageError("Quota journal initialization failed") from None

    def _prepare_path(self):
        missing = []
        for parent in self.path.parents:
            try:
                info = parent.lstat()
            except FileNotFoundError:
                missing.append(parent)
                continue
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise QuotaJournalUnsafePathError("Quota journal ancestor is unsafe")
        for parent in reversed(missing):
            try:
                parent.mkdir(mode=0o700)
                _sync_directory(parent.parent)
            except FileExistsError:
                _check_private(parent, directory=True)
        _check_private(self.path.parent, directory=True)
        newly_created = False
        try:
            descriptor = os.open(
                self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            _check_private(self.path)
        else:
            newly_created = True
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _sync_directory(self.path.parent)
        self._check_paths()
        return newly_created

    def _check_paths(self):
        _check_private(self.path.parent, directory=True)
        _check_private(self.path)
        for suffix in ("-journal", "-wal", "-shm", ".drain.lock"):
            sidecar = self.path.with_name(self.path.name + suffix)
            try:
                _check_private(sidecar)
            except FileNotFoundError:
                pass

    @staticmethod
    def _schema_objects(connection):
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name <> 'sqlite_sequence'"
        ).fetchall()

    def _validate_schema(self, connection):
        def normalize(sql):
            return " ".join(sql.split()) if isinstance(sql, str) else None
        actual = {(kind, name): normalize(sql) for kind, name, sql in self._schema_objects(connection)}
        expected = {
            ("table", "quota_journal_meta"): normalize(_META_SQL),
            ("table", "quota_journal_events"): normalize(_EVENTS_SQL),
        }
        if (actual != expected
                or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                or connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID):
            raise QuotaJournalSchemaError("Quota journal schema does not match its version")
        rows = connection.execute("SELECT namespace FROM quota_journal_meta WHERE singleton = 1").fetchall()
        if rows != [(self.namespace,)]:
            raise QuotaJournalSchemaError("Quota journal destination does not match")

    @contextmanager
    def _connection(self, *, initialize=False):
        connection = None
        try:
            self._check_paths()
            target = self.path.as_uri() + "?mode=ro" if self._readonly else str(self.path)
            connection = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000,
                                         isolation_level=None, uri=self._readonly)
            connection.execute("PRAGMA cache_size = -256")
            if not self._readonly:
                connection.execute("PRAGMA synchronous = EXTRA")
            connection.execute("PRAGMA trusted_schema = OFF")
            if not initialize:
                self._validate_schema(connection)
            # Do not convert or touch another journal mode, including unknown WAL files.
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise QuotaJournalSchemaError("Quota journal mode is not supported")
            yield connection
        except (sqlite3.Error, OSError):
            raise QuotaJournalStorageError("Quota journal storage operation failed") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    @contextmanager
    def _transaction(connection):
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def append(self, event):
        payload, size = _encode_event(event)
        with self._connection() as connection, self._transaction(connection):
            count, pending_bytes = connection.execute(
                "SELECT pending_events, pending_bytes FROM quota_journal_meta WHERE singleton = 1"
            ).fetchone()
            if count >= self.max_events or pending_bytes + size > self.max_bytes:
                raise QuotaJournalFullError("Quota journal capacity is exhausted")
            sequence = connection.execute(
                "INSERT INTO quota_journal_events (created_at, payload, payload_bytes) VALUES (?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), payload, size),
            ).lastrowid
            connection.execute(
                "UPDATE quota_journal_meta SET pending_events = pending_events + 1, "
                "pending_bytes = pending_bytes + ? WHERE singleton = 1", (size,),
            )
        return int(sequence)

    def read_batch(self, limit=MAX_BATCH_EVENTS):
        limit = _positive_integer(limit, MAX_BATCH_EVENTS)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence, payload FROM quota_journal_events ORDER BY sequence LIMIT ?", (limit,),
            ).fetchall()
        result = []
        for sequence, payload in rows:
            try:
                event = json.loads(payload)
            except (ValueError, TypeError):
                raise QuotaJournalPayloadError("Quota journal contains an invalid event") from None
            _encode_event(event)
            result.append((sequence, event))
        return result

    def acknowledge(self, sequences):
        exact = set()
        for index, sequence in enumerate(sequences):
            if index >= MAX_BATCH_EVENTS:
                raise QuotaJournalPayloadError("Quota journal acknowledgement exceeds its bound")
            exact.add(_positive_integer(sequence, 2**63 - 1))
        if not exact:
            return
        placeholders = ",".join("?" for _ in exact)
        parameters = tuple(exact)
        with self._connection() as connection, self._transaction(connection):
            count, size = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0) FROM quota_journal_events "
                f"WHERE sequence IN ({placeholders})", parameters,
            ).fetchone()
            connection.execute(f"DELETE FROM quota_journal_events WHERE sequence IN ({placeholders})",
                               parameters)
            connection.execute(
                "UPDATE quota_journal_meta SET pending_events = pending_events - ?, "
                "pending_bytes = pending_bytes - ? WHERE singleton = 1", (count, size),
            )

    def status(self):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT pending_events, pending_bytes, "
                "(SELECT created_at FROM quota_journal_events ORDER BY sequence LIMIT 1) "
                "FROM quota_journal_meta WHERE singleton = 1"
            ).fetchone()
        return {"pending_events": row[0], "pending_bytes": row[1],
                "oldest_pending_at": row[2], "schema_version": SCHEMA_VERSION}

    @contextmanager
    def drain_lock(self):
        """Yield False immediately when another process/thread owns delivery."""
        if self._readonly:
            raise QuotaJournalStorageError("Read-only quota journal cannot acquire delivery")
        descriptor = None
        acquired = False
        try:
            try:
                self._check_paths()
                descriptor = os.open(self.lock_path,
                                     os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                _check_private(self.lock_path)
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
            except OSError:
                raise QuotaJournalStorageError("Quota journal drain lock preparation failed") from None
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise QuotaJournalStorageError("Quota journal drain lock failed") from None
            yield acquired
        finally:
            if descriptor is not None:
                try:
                    if acquired:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    raise QuotaJournalStorageError("Quota journal drain lock release failed") from None
                finally:
                    os.close(descriptor)
