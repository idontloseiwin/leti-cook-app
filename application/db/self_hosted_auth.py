"""Host-local owner/session persistence, independent of the API-data database.

Only this repository constructs authentication SQL. Existing files are mandatory
at runtime; deleting/corrupting the store never reopens registration.
"""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import time

SCHEMA_VERSION = 2
MAX_SESSIONS = 8
SESSION_SECONDS = 12 * 3600
IDLE_SECONDS = 30 * 60
ATTEMPT_WINDOW = 60
ATTEMPT_LIMIT = 5

class AuthStoreError(RuntimeError):
    pass


def checked_path(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)):
            raise AuthStoreError('auth_path_link_refused')
    if not path.parent.is_dir():
        raise AuthStoreError('auth_directory_required')
    if os.name != 'nt':
        info = path.parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AuthStoreError('auth_directory_must_be_private')
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AuthStoreError('auth_regular_file_required')
        if os.name != 'nt' and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise AuthStoreError('auth_file_must_be_private')
    return path


class OwnerAuthRepository:
    def __init__(self, path, *, clock=time.time):
        self.path = checked_path(path)
        self.clock = clock

    def _now(self, now):
        """The session clock, read under the write lock: no committed session is newer.

        A request's own `now` is taken before it waits for the lock or for Argon2;
        purging sessions "created after now" with it would remove a browser that
        signed in meanwhile.
        """
        return max(now, int(self.clock()))

    @classmethod
    def initialize(cls, path, dummy_password_hash, now):
        path = checked_path(path)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(descriptor)
        # A failed file is retained closed for inspection; no automatic reset.
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                PRAGMA synchronous=FULL;
                PRAGMA secure_delete=ON;
                CREATE TABLE owner (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    username TEXT, password_hash TEXT NOT NULL,
                    access_mode TEXT NOT NULL DEFAULT 'pending' CHECK(access_mode IN ('pending','password','local')),
                    setup_hash TEXT, setup_expires INTEGER NOT NULL DEFAULT 0,
                    attempt_start INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    CHECK ((username IS NULL) OR setup_hash IS NULL)
                );
                CREATE TABLE sessions (
                    token_hash TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'owner' CHECK(kind IN ('owner','setup'))
                );
                PRAGMA user_version=2;
            ''')
            db.execute('INSERT INTO owner(singleton,password_hash,created_at) VALUES(1,?,?)',
                       (dummy_password_hash, now))
        return cls(path)

    @contextmanager
    def transaction(self):
        path = checked_path(self.path)
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=0.5)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA secure_delete=ON')
            if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise AuthStoreError('auth_schema_unavailable')
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT count(*) FROM owner WHERE singleton=1').fetchone()[0] != 1:
                raise AuthStoreError('auth_identity_unavailable')
            yield connection
            connection.commit()
        except sqlite3.Error:
            raise AuthStoreError('auth_store_unavailable') from None
        finally:
            if connection is not None:
                connection.close()

    def configured(self):
        with self.transaction() as db:
            return db.execute('SELECT access_mode FROM owner WHERE singleton=1').fetchone()[0] != 'pending'

    def issue_setup(self, digest, now, lifetime=900):
        with self.transaction() as db:
            if db.execute('SELECT access_mode FROM owner WHERE singleton=1').fetchone()[0] != 'pending':
                raise AuthStoreError('owner_already_created')
            db.execute('UPDATE owner SET setup_hash=?,setup_expires=?,attempt_start=0,attempt_count=0 WHERE singleton=1',
                       (digest, now+lifetime))
            db.execute("DELETE FROM sessions WHERE kind='setup'")

    @staticmethod
    def _attempt(db, now):
        row = db.execute('SELECT * FROM owner WHERE singleton=1').fetchone()
        start, count = row['attempt_start'], row['attempt_count']
        # A backwards wall clock cannot clear a live rate limit.
        if now >= start + ATTEMPT_WINDOW:
            start, count = now, 0
        if count >= ATTEMPT_LIMIT:
            return None
        db.execute('UPDATE owner SET attempt_start=?,attempt_count=? WHERE singleton=1', (start,count+1))
        return row

    @staticmethod
    def _session(db, digest, now, previous=None, *, kind='owner', lifetime=SESSION_SECONDS):
        db.execute('DELETE FROM sessions WHERE expires_at<=? OR last_seen<=? OR created_at>?',
                   (now,now-IDLE_SECONDS,now))
        if previous:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (previous,))
        db.execute('DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions '
                   'ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)', (MAX_SESSIONS-1,))
        db.execute('INSERT INTO sessions(token_hash,created_at,last_seen,expires_at,kind) VALUES(?,?,?,?,?)', (digest,now,now,now+lifetime,kind))
        db.execute('UPDATE owner SET attempt_count=0,attempt_start=? WHERE singleton=1', (now,))

    # Argon2 takes 64 MiB and three passes. It runs between two short
    # transactions, so the store lock never waits for a hash; the second
    # transaction re-checks the state the first one read.

    def register(self, username, setup_matches, hash_password, session_hash, now):
        with self.transaction() as db:
            row = self._attempt(db, now)
            if row is None: return 'rate_limited'
            if row['access_mode'] != 'pending': return 'setup_closed'
            if (not row['setup_hash'] or not now < row['setup_expires']
                    or now < row['created_at'] or not setup_matches(row['setup_hash'])):
                return 'invalid_setup_code'
            code = row['setup_hash']
        encoded = hash_password()
        with self.transaction() as db:
            row = db.execute('SELECT * FROM owner WHERE singleton=1').fetchone()
            if row['access_mode'] != 'pending': return 'setup_closed'
            # Another request may have used or replaced the code meanwhile.
            if row['setup_hash'] != code or not now < row['setup_expires']: return 'invalid_setup_code'
            db.execute("UPDATE owner SET username=?,password_hash=?,access_mode='password',setup_hash=NULL,setup_expires=0 WHERE singleton=1",
                       (username,encoded))
            db.execute('DELETE FROM sessions')
            self._session(db,session_hash,self._now(now))
            return 'ok'

    @staticmethod
    def _setup_session(db, digest, now):
        if not digest: return False
        row=db.execute("SELECT * FROM sessions WHERE token_hash=? AND kind='setup'",(digest,)).fetchone()
        return bool(row and row['created_at'] <= now < row['expires_at'])

    def access_state(self, now, setup_digest=None):
        with self.transaction() as db:
            row=db.execute('SELECT access_mode,setup_expires,setup_hash FROM owner WHERE singleton=1').fetchone()
            return {'mode':row['access_mode'],
                    'setup_verified':row['access_mode']=='pending' and self._setup_session(db,setup_digest,now),
                    'setup_expired':row['access_mode']=='pending' and bool(row['setup_hash']) and now>=row['setup_expires']}

    def claim_setup(self, matches, digest, now):
        with self.transaction() as db:
            row=self._attempt(db,now)
            if row is None: return 'rate_limited'
            if row['access_mode']!='pending': return 'setup_closed'
            if not row['setup_hash'] or now<row['created_at'] or not matches(row['setup_hash']):
                return 'invalid_setup_code'
            if now>=row['setup_expires']: return 'setup_code_expired'
            db.execute('UPDATE owner SET setup_hash=NULL,setup_expires=0 WHERE singleton=1')
            db.execute('DELETE FROM sessions')
            self._session(db,digest,self._now(now),kind='setup',lifetime=900)
            return 'ok'

    def _setup_refusal(self, db, digest, now):
        if db.execute('SELECT access_mode FROM owner WHERE singleton=1').fetchone()[0]!='pending': return 'setup_closed'
        if not self._setup_session(db,digest,now): return 'setup_session_expired'
        return None

    def complete_setup(self, digest, mode, username, hash_password, session_hash, now):
        with self.transaction() as db:
            refusal=self._setup_refusal(db,digest,now)
            if refusal: return refusal
            if mode not in ('local','password'): raise AuthStoreError('invalid_access_mode')
        encoded=hash_password() if mode=='password' else None
        with self.transaction() as db:
            refusal=self._setup_refusal(db,digest,now)
            if refusal: return refusal
            if mode=='password':
                db.execute("UPDATE owner SET access_mode='password',username=?,password_hash=? WHERE singleton=1",(username,encoded))
            else:
                db.execute("UPDATE owner SET access_mode='local' WHERE singleton=1")
            db.execute('UPDATE owner SET setup_hash=NULL,setup_expires=0 WHERE singleton=1')
            db.execute('DELETE FROM sessions')
            if mode=='password': self._session(db,session_hash,self._now(now))
            return 'ok'

    def login(self, username_matches, verify_password, session_hash, now, previous=None):
        # The attempt is committed before the hash check, so it always counts.
        with self.transaction() as db:
            row = self._attempt(db,now)
            if row is None: return 'rate_limited'
            stored = row['password_hash']
        valid = verify_password(stored)
        # Always re-read: a wrong password and a wrong username take the same path.
        with self.transaction() as db:
            row = db.execute('SELECT * FROM owner WHERE singleton=1').fetchone()
            # A password reset in between invalidates this check.
            if (not valid or row['password_hash'] != stored or row['access_mode'] != 'password'
                    or row['username'] is None or not username_matches(row['username'])):
                return 'invalid_credentials'
            self._session(db,session_hash,self._now(now),previous)
            return 'ok'

    def session(self, digest, now, *, touch=True):
        with self.transaction() as db:
            row = db.execute('SELECT s.*,o.username FROM sessions s CROSS JOIN owner o '
                             "WHERE s.token_hash=? AND s.kind='owner' AND o.access_mode='password' AND o.singleton=1", (digest,)).fetchone()
            if row is None: return None
            if (row['username'] is None or now >= row['expires_at'] or now >= row['last_seen']+IDLE_SECONDS
                    or now < row['created_at'] or now+5 < row['last_seen']):
                db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))
                return None
            if touch and now >= row['last_seen']+60:
                db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (now,digest))
            return {'username': row['username'], 'expires_at': row['expires_at']}

    def logout(self, digest):
        with self.transaction() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))

    def sign_in_limited(self, now):
        with self.transaction() as db:
            row = db.execute('SELECT attempt_start,attempt_count FROM owner WHERE singleton=1').fetchone()
            return now < row['attempt_start'] + ATTEMPT_WINDOW and row['attempt_count'] >= ATTEMPT_LIMIT

    def clear_sign_in_limit(self):
        """Owner recovery from the server: the password and sessions stay as they are."""
        with self.transaction() as db:
            db.execute('UPDATE owner SET attempt_count=0,attempt_start=0 WHERE singleton=1')

    def reset_password(self, encoded):
        with self.transaction() as db:
            if db.execute('SELECT username FROM owner WHERE singleton=1').fetchone()[0] is None:
                raise AuthStoreError('owner_not_created')
            db.execute('UPDATE owner SET password_hash=?,attempt_count=0,attempt_start=0 WHERE singleton=1',(encoded,))
            db.execute('DELETE FROM sessions')
