"""Bounded installation-wide manual-add admission; not a queue or quota ledger.

The installation owner creates this private state explicitly. Runtime never
recreates missing/corrupt state; every process shares the auth-store sibling.
"""
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from uuid import UUID

from quota_guards import CHANNEL_ADD_WINDOWS

_VERSION = 1
_APPLICATION_ID = 0x4C494341
_PAGE_BYTES = 4096
_MAX_PAGES = 64
_MAX_BYTES = _PAGE_BYTES * _MAX_PAGES
_BUSY_SECONDS = 0.5
_MAX_ROWS = max(limit for _, limit in CHANNEL_ADD_WINDOWS)
_WINDOW_US = tuple((seconds * 1_000_000, limit) for seconds, limit in CHANNEL_ADD_WINDOWS)
_POLICY = ','.join(f'{seconds}:{limit}' for seconds, limit in CHANNEL_ADD_WINDOWS)
_META = '''CREATE TABLE channel_add_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 installation_id TEXT NOT NULL, policy TEXT NOT NULL,
 time_floor_us INTEGER NOT NULL CHECK(time_floor_us>=0)
)'''
_EVENTS = f'''CREATE TABLE channel_add_admissions (
 slot INTEGER PRIMARY KEY CHECK(slot>=0 AND slot<{_MAX_ROWS}),
 admitted_us INTEGER NOT NULL CHECK(admitted_us>=0)
)'''


class ChannelAddAdmissionUnavailable(RuntimeError):
    def __init__(self):
        super().__init__('channel_add_admission_unavailable')


def _path(value, *, required=True):
    path = Path(value).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)):
            raise ChannelAddAdmissionUnavailable()
    if not path.parent.is_dir():
        raise ChannelAddAdmissionUnavailable()
    if os.name != 'nt':
        info = path.parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ChannelAddAdmissionUnavailable()
    for suffix in ('', '-journal', '-wal', '-shm'):
        candidate = path.with_name(path.name + suffix)
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.lstat(), 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)):
            raise ChannelAddAdmissionUnavailable()
        if not candidate.exists():
            if not suffix and required:
                raise ChannelAddAdmissionUnavailable()
            continue
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > (_MAX_BYTES * 2 if suffix else _MAX_BYTES)
                or suffix in ('-wal', '-shm')):
            raise ChannelAddAdmissionUnavailable()
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise ChannelAddAdmissionUnavailable()
    return path


def _identity(value):
    try:
        if type(value) is str and str(UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise ChannelAddAdmissionUnavailable()


def _microseconds(clock):
    value = clock()
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 253402300800:
        raise ChannelAddAdmissionUnavailable()
    return int(value * 1_000_000)


def _configure(db):
    db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA synchronous=EXTRA')
    db.execute('PRAGMA cache_size=-256')
    if (db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
            or db.execute('PRAGMA page_size').fetchone()[0] != _PAGE_BYTES
            or db.execute(f'PRAGMA max_page_count={_MAX_PAGES}').fetchone()[0] > _MAX_PAGES):
        raise ChannelAddAdmissionUnavailable()


class ChannelAddAdmission:
    """acquire() returns 0 or retry seconds, with the current RateLimiter policy."""

    def __init__(self, path, installation_id, *, clock=time.time):
        self.path = Path(path).absolute()
        self.installation_id = _identity(installation_id)
        self.clock = clock

    @classmethod
    def for_installation(cls, auth_database, installation_id, *, clock=time.time):
        if not isinstance(auth_database, (str, Path)) or not str(auth_database):
            raise ChannelAddAdmissionUnavailable()
        path = Path(auth_database)
        if not path.is_absolute():
            raise ChannelAddAdmissionUnavailable()
        return cls(path.with_name('channel-add.sqlite3'), installation_id, clock=clock)

    def verify(self):
        """Validate existing state without admitting, pruning or advancing its clock."""
        try:
            path = _path(self.path)
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                         timeout=_BUSY_SECONDS, isolation_level=None)) as db:
                db.execute('PRAGMA trusted_schema=OFF')
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                if (db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'
                        or db.execute('PRAGMA page_size').fetchone()[0] != _PAGE_BYTES
                        or db.execute('PRAGMA page_count').fetchone()[0] > _MAX_PAGES):
                    raise ChannelAddAdmissionUnavailable()
                floor = self._validate(db)
                events = db.execute('SELECT slot,admitted_us FROM channel_add_admissions LIMIT 301').fetchall()
                if (len(events) > _MAX_ROWS or any(type(slot) is not int or not 0 <= slot < _MAX_ROWS
                        or type(at) is not int or not 0 <= at <= floor for slot, at in events)):
                    raise ChannelAddAdmissionUnavailable()
        except (sqlite3.Error, OSError):
            raise ChannelAddAdmissionUnavailable() from None

    @classmethod
    def initialize(cls, path, installation_id, *, clock=time.time):
        """Exclusive install-time creation. Failure retains the file, never resets it."""
        identity = _identity(installation_id)
        try:
            path = _path(path, required=False)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(descriptor)
            with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                                         timeout=_BUSY_SECONDS, isolation_level=None)) as db:
                db.execute(f'PRAGMA page_size={_PAGE_BYTES}')
                _configure(db)
                db.execute('BEGIN IMMEDIATE')
                db.execute(_META)
                db.execute(_EVENTS)
                db.execute('INSERT INTO channel_add_meta VALUES(1,?,?,?)', (identity, _POLICY, _microseconds(clock)))
                db.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                db.execute(f'PRAGMA user_version={_VERSION}')
                db.commit()
            return cls(path, identity, clock=clock)
        except (sqlite3.Error, OSError):
            raise ChannelAddAdmissionUnavailable() from None

    def _validate(self, db):
        normalize = lambda value: ' '.join(value.split()) if isinstance(value, str) else None
        objects = {(kind, name): normalize(statement) for kind, name, statement in
                   db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        expected = {('table', 'channel_add_meta'): normalize(_META),
                    ('table', 'channel_add_admissions'): normalize(_EVENTS)}
        if (objects != expected or db.execute('PRAGMA user_version').fetchone()[0] != _VERSION
                or db.execute('PRAGMA application_id').fetchone()[0] != _APPLICATION_ID):
            raise ChannelAddAdmissionUnavailable()
        rows = db.execute('SELECT installation_id,policy,time_floor_us FROM channel_add_meta').fetchall()
        if (len(rows) != 1 or rows[0][:2] != (self.installation_id, _POLICY)
                or type(rows[0][2]) is not int or not 0 <= rows[0][2] < 253402300800000000):
            raise ChannelAddAdmissionUnavailable()
        return rows[0][2]

    def acquire(self):
        """No retries or fallback. Commit an admission before the caller may dispatch."""
        try:
            path = _path(self.path)
            with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                                         timeout=_BUSY_SECONDS, isolation_level=None)) as db:
                _configure(db)
                db.execute('BEGIN IMMEDIATE')
                floor = self._validate(db)
                now = max(_microseconds(self.clock), floor)
                db.execute('DELETE FROM channel_add_admissions WHERE admitted_us<=?', (now - max(w for w, _ in _WINDOW_US),))
                events = db.execute('SELECT slot,admitted_us FROM channel_add_admissions ORDER BY admitted_us,slot').fetchall()
                if (len(events) > _MAX_ROWS or any(type(at) is not int or not 0 <= at <= now for _, at in events)):
                    raise ChannelAddAdmissionUnavailable()
                wait_us = 0
                for window, limit in _WINDOW_US:
                    active = [at for _, at in events if at > now - window]
                    if len(active) >= limit:
                        wait_us = max(wait_us, active[0] + window - now)
                db.execute('UPDATE channel_add_meta SET time_floor_us=? WHERE singleton=1', (now,))
                if not wait_us:
                    used = {slot for slot, _ in events}
                    slot = next((value for value in range(_MAX_ROWS) if value not in used), None)
                    if slot is None:
                        raise ChannelAddAdmissionUnavailable()
                    db.execute('INSERT INTO channel_add_admissions VALUES(?,?)', (slot, now))
                db.commit()
                return (wait_us + 999_999) // 1_000_000
        except (sqlite3.Error, OSError):
            raise ChannelAddAdmissionUnavailable() from None


def configured_channel_add_admission():
    """Stable existing configuration for ordinary manual channel additions."""
    return ChannelAddAdmission.for_installation(os.getenv('LIC_AUTH_DATABASE'), os.getenv('LIC_DATABASE_ID'))
