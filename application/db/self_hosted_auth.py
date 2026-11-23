"""Host-local owner/session persistence, independent of the API-data database.

Only this repository constructs authentication SQL. Existing files are mandatory
at runtime; deleting/corrupting the store never reopens registration.
"""
import base64
from collections import OrderedDict
from contextlib import closing, contextmanager
import os
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time

SCHEMA_VERSION = 2
MAX_SESSIONS = 8
SESSION_SECONDS = 12 * 3600
IDLE_SECONDS = 30 * 60
# A stored session time this far ahead of the clock is a clock set back
# while the app was stopped (ClockSteps sees the ones while it runs).
CLOCK_SLACK = 5

# Sign-in tries (SECURITY.md "Access"). A try is counted before the
# password check, in one of two lanes: the browser's own count when it carries
# a valid known-browser mark, otherwise one count shared by everyone without a
# mark (owner.attempt_count = tries in a row, owner.attempt_start = time of the
# last one). A lane in its wait is refused at once without a check; nothing is
# held while it waits, and the other lanes never notice. Setup codes (256 bits)
# are never counted.
FREE_TRIES = 5          # wrong passwords in a row before the first wait
LONGEST_WAIT = 300      # seconds; the wait doubles from 1 s up to this
FORGET_TRIES = 3600     # an hour without tries starts the count again
KNOWN_BROWSER_SECONDS = 30 * 86400
# Counts of marked browsers live in this process only: the backend runs one
# worker (ops/self_hosted_container.py). Only a successful sign-in makes a mark.
KNOWN_BROWSERS = 64
KNOWN_MARK = re.compile(r'([0-9]{1,12})\.([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})\Z')

class AuthStoreError(RuntimeError):
    pass


def try_wait(count, last, now):
    """Seconds before the next password check may start (0: now): `count` tries in a row, the last at `last`."""
    if count < FREE_TRIES or now >= last + FORGET_TRIES:
        return 0
    wait = min(1 << min(count - FREE_TRIES, 16), LONGEST_WAIT)
    # The store moves `last` with a clock set back while the app runs
    # (ClockSteps), so the time already waited still counts. A `last` ahead of
    # the clock remains only from a step while the app was stopped: it does not
    # clear the wait, and one refusal never names more than one step.
    return max(0, wait - max(0, now - last))


def _next_try(count, last, now):
    """(wait, count, last) after one more try is asked for: counted only when it may start."""
    wait = try_wait(count, last, now)
    if wait:
        return wait, count, last
    if now >= last + FORGET_TRIES:
        count = 0
    return 0, min(count + 1, 1_000_000), now


class SignInAttempt:
    """One sign-in request: the browser's known-browser marks in; the wait or a new mark out."""

    def __init__(self, marks=()):
        self.marks = tuple(marks)[:4]
        self.retry_after = 0
        self.new_mark = None


def _mark_key(row):
    # Bound to the account as stored: a password reset or a new owner ends every mark.
    value = '\0'.join(str(row[name]) for name in ('access_mode', 'username', 'password_hash', 'created_at'))
    return hashlib.sha256(b'lic-known-browser-key-v1\0' + value.encode()).digest()


def _mark_signature(key, issued, nonce):
    digest = hmac.new(key, b'lic-known-browser-v1\0%d.%s' % (issued, nonce.encode()), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def _known_browser(row, marks, now):
    """The nonce of a valid mark for the current account, or None (the shared lane)."""
    if row['access_mode'] != 'password' or row['username'] is None:
        return None
    key = _mark_key(row)
    for mark in marks:
        match = KNOWN_MARK.fullmatch(mark) if isinstance(mark, str) else None
        if match and now < int(match[1]) + KNOWN_BROWSER_SECONDS and hmac.compare_digest(
                match[3], _mark_signature(key, int(match[1]), match[2])):
            return match[2]
    return None


def _new_mark(row, now):
    # No name or password inside: the time it was issued, a random nonce and the signature.
    nonce = secrets.token_urlsafe(16)
    return f'{now}.{nonce}.{_mark_signature(_mark_key(row), now, nonce)}'


class ClockSteps:
    """Backward steps of the server clock while the app runs.

    time.monotonic() does not step with the wall clock. When the wall clock has
    fallen behind the running time since the last look, it was set back; the
    store moves session times and the times of the last sign-in tries back by
    as much, so the step neither ends nor lengthens a session or a wait. A
    forward jump is never undone: the server may really have slept (monotonic
    time stops during suspend), so sessions only end sooner.
    Reads the system clocks, not a test clock injected into the repository.
    """
    THRESHOLD = 2  # seconds; clock slewing stays far below this between looks

    def __init__(self):
        self.lock = threading.Lock()
        self.base = None
        self.owed = 0
        self.before_step = None  # furthest possible request time on an earlier clock

    def take(self):
        """(seconds the stored times still have to move back, seconds this look found)."""
        with self.lock:
            wall, running = time.time(), time.monotonic()
            found = 0
            if self.base is not None:
                prior_wall = self.base[0] + (running - self.base[1])
                behind = prior_wall - wall
                if behind > self.THRESHOLD:
                    found = round(behind)
                    if self.before_step is None or prior_wall > self.before_step:
                        self.before_step = prior_wall
            if self.before_step is not None and wall >= self.before_step:
                self.before_step = None
            self.base = (wall, running)
            owed, self.owed = self.owed + found, 0
            return owed, found

    def give_back(self, seconds):
        """A transaction that took a step failed: the next one moves the stored times."""
        with self.lock:
            self.owed += seconds

    def decision_time(self, request_time, clock):
        # Called under the same store lock as take(). Use the wall reading that
        # dated this transaction when the repository uses the system clock.
        current = int(self.base[0] if clock == time.time else clock())
        if self.before_step is not None:
            return current
        return max(current, request_time)


_QUEUES = {}
_QUEUES_LOCK = threading.Lock()
QUEUE_SECONDS = 5


def _queue(path):
    """One lock per store file in this process: its transactions wait in line.

    SQLite's own wait for its write lock polls and gives up after 0.5 s, so a
    flood of short transactions (wrong passwords, page checks) could starve one
    waiter, such as the owner signing in. Waiting here is blocking and bounded
    (QUEUE_SECONDS); SQLite's wait remains for other processes (manage.py).
    """
    with _QUEUES_LOCK:
        return _QUEUES.setdefault(str(path), threading.RLock())


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
        self.queue = _queue(self.path)
        self.steps = ClockSteps()
        self._known_tries = OrderedDict()  # mark nonce -> (tries in a row, time of the last)
        self._known_lock = threading.Lock()

    def _now(self, now):
        """The session clock, read under the write lock: no committed session is newer.

        A request's own `now` is taken before it waits for the lock or for Argon2;
        purging sessions "created after now" with it would remove a browser that
        signed in meanwhile. An earlier clock can stay ahead of the store for
        the entire size of a backward step, however long the request waited.
        """
        return self.steps.decision_time(now, self.clock)

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
        if not self.queue.acquire(timeout=QUEUE_SECONDS):
            raise AuthStoreError('auth_store_unavailable')
        try:
            with self._transaction(path) as connection:
                yield connection
        finally:
            self.queue.release()

    @contextmanager
    def _transaction(self, path):
        connection = None
        owed = 0
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
            # Taken under the write lock: the next transaction sees the moved times.
            owed, found = self.steps.take()
            if found:
                # Kept in this process, not in the store: moved once, whatever
                # becomes of this transaction.
                self._set_known_tries_back(found)
            if owed:
                connection.execute('UPDATE sessions SET created_at=created_at-?,last_seen=last_seen-?,'
                                   'expires_at=expires_at-?', (owed, owed, owed))
                # The shared count's last try: its wait still ends when it was said to.
                connection.execute('UPDATE owner SET attempt_start=max(attempt_start-?,0) WHERE singleton=1', (owed,))
            yield connection
            connection.commit()
            owed = 0
        except sqlite3.Error:
            raise AuthStoreError('auth_store_unavailable') from None
        finally:
            if owed:
                self.steps.give_back(owed)
            if connection is not None:
                connection.close()

    def configured(self):
        with self.transaction() as db:
            return db.execute('SELECT access_mode FROM owner WHERE singleton=1').fetchone()[0] != 'pending'

    def agent_binding(self):
        """Resetting the owner password or replacing access state revokes agent keys too."""
        with self.transaction() as db:
            row = db.execute('SELECT access_mode,password_hash,created_at FROM owner WHERE singleton=1').fetchone()
            if row['access_mode'] not in ('password', 'local'):
                return None
            value = '\0'.join(str(row[name]) for name in ('access_mode', 'password_hash', 'created_at'))
            return hashlib.sha256(b'lic-agent-owner-binding-v1\0' + value.encode()).hexdigest()

    def issue_setup(self, digest, now, lifetime=900):
        with self.transaction() as db:
            if db.execute('SELECT access_mode FROM owner WHERE singleton=1').fetchone()[0] != 'pending':
                raise AuthStoreError('owner_already_created')
            db.execute('UPDATE owner SET setup_hash=?,setup_expires=?,attempt_start=0,attempt_count=0 WHERE singleton=1',
                       (digest, now+lifetime))
            db.execute("DELETE FROM sessions WHERE kind='setup'")

    def _take_try(self, db, row, browser, now):
        """Count one sign-in try in its lane before the password check; return its wait (0: go on)."""
        if browser is None:
            wait, count, last = _next_try(row['attempt_count'], row['attempt_start'], now)
            if not wait:
                db.execute('UPDATE owner SET attempt_count=?,attempt_start=? WHERE singleton=1', (count, last))
            return wait
        with self._known_lock:
            wait, count, last = _next_try(*self._known_tries.get(browser, (0, 0)), now)
            if not wait:
                self._known_tries[browser] = (count, last)
                self._known_tries.move_to_end(browser)
                while len(self._known_tries) > KNOWN_BROWSERS:
                    self._known_tries.popitem(last=False)
            return wait

    def _set_known_tries_back(self, seconds):
        """The clock was set back while the app runs: marked browsers' last tries move with it."""
        with self._known_lock:
            for browser, (count, last) in self._known_tries.items():
                self._known_tries[browser] = (count, last - seconds)

    def _forget_tries(self, db, browser, now):
        """A right password starts its own lane's count again, and only that one."""
        if browser is None:
            db.execute('UPDATE owner SET attempt_count=0,attempt_start=? WHERE singleton=1', (now,))
        else:
            with self._known_lock:
                self._known_tries.pop(browser, None)

    @staticmethod
    def _session(db, digest, now, previous=None, *, kind='owner', lifetime=SESSION_SECONDS):
        # The same rule as session(): `now` here is already the store clock (_now).
        db.execute('DELETE FROM sessions WHERE expires_at<=? OR last_seen<=? OR created_at>? OR last_seen>?',
                   (now,now-IDLE_SECONDS,now+CLOCK_SLACK,now+CLOCK_SLACK))
        if previous:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (previous,))
        db.execute('DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions '
                   'ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)', (MAX_SESSIONS-1,))
        db.execute('INSERT INTO sessions(token_hash,created_at,last_seen,expires_at,kind) VALUES(?,?,?,?,?)', (digest,now,now,now+lifetime,kind))

    # Argon2 takes 64 MiB and three passes. It runs between two short
    # transactions, so the store lock never waits for a hash and no
    # connection is open during it; the second transaction re-checks the
    # state the first one read.

    def register(self, username, setup_matches, hash_password, session_hash, now):
        # Store fixture only (no HTTP route). The code is checked first and never counted.
        with self.transaction() as db:
            row = db.execute('SELECT * FROM owner WHERE singleton=1').fetchone()
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
            db.execute("UPDATE owner SET username=?,password_hash=?,access_mode='password',setup_hash=NULL,setup_expires=0,"
                       'attempt_count=0,attempt_start=0 WHERE singleton=1', (username,encoded))
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
        # The code (256 bits) is checked first and never counted: wrong codes,
        # however many, are refused at once and never delay the right one.
        with self.transaction() as db:
            row=db.execute('SELECT * FROM owner WHERE singleton=1').fetchone()
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

    def complete_setup(self, digest, mode, username, hash_password, session_hash, now, attempt=None):
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
            # The new account starts with a fresh shared count.
            db.execute('UPDATE owner SET setup_hash=NULL,setup_expires=0,attempt_count=0,attempt_start=0 WHERE singleton=1')
            db.execute('DELETE FROM sessions')
            if mode=='password':
                now=self._now(now)
                self._session(db,session_hash,now)
                # The browser that created the account is known from the start.
                if attempt is not None:
                    attempt.new_mark=_new_mark(db.execute('SELECT * FROM owner WHERE singleton=1').fetchone(),now)
            return 'ok'

    def login(self, username_matches, verify_password, session_hash, now, previous=None, attempt=None):
        attempt = attempt or SignInAttempt()
        # The try is counted in its lane and committed before the hash check, so
        # it always counts; a lane in its wait is refused here, without a check.
        with self.transaction() as db:
            now = self._now(now)
            row = db.execute('SELECT * FROM owner WHERE singleton=1').fetchone()
            browser = _known_browser(row, attempt.marks, now)
            attempt.retry_after = self._take_try(db, row, browser, now)
            if attempt.retry_after: return 'rate_limited'
            stored = row['password_hash']
        valid = verify_password(stored)
        # Always re-read: a wrong password and a wrong username take the same path.
        with self.transaction() as db:
            row = db.execute('SELECT * FROM owner WHERE singleton=1').fetchone()
            # A password reset in between invalidates this check.
            if (not valid or row['password_hash'] != stored or row['access_mode'] != 'password'
                    or row['username'] is None or not username_matches(row['username'])):
                return 'invalid_credentials'
            now = self._now(now)
            self._session(db,session_hash,now,previous)
            self._forget_tries(db,browser,now)
            attempt.new_mark = _new_mark(row, now)
            return 'ok'

    def session(self, digest, now, *, touch=True):
        with self.transaction() as db:
            # A request that waited for the lock must not judge a session that
            # another request touched meanwhile by its older clock.
            now = self._now(now)
            row = db.execute('SELECT s.*,o.username FROM sessions s CROSS JOIN owner o '
                             "WHERE s.token_hash=? AND s.kind='owner' AND o.access_mode='password' AND o.singleton=1", (digest,)).fetchone()
            if row is None: return None
            # Times ahead of the clock remain only from a clock set back while the
            # app was stopped (transaction() moves them for steps while it runs).
            if (row['username'] is None or now >= row['expires_at'] or now >= row['last_seen']+IDLE_SECONDS
                    or now+CLOCK_SLACK < row['created_at'] or now+CLOCK_SLACK < row['last_seen']):
                db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))
                return None
            if touch and now >= row['last_seen']+60:
                db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (now,digest))
            return {'username': row['username'], 'expires_at': row['expires_at']}

    def logout(self, digest):
        with self.transaction() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (digest,))

    def sign_in_limited(self, now):
        """Whether browsers without a known-browser mark must wait now."""
        with self.transaction() as db:
            row = db.execute('SELECT attempt_start,attempt_count FROM owner WHERE singleton=1').fetchone()
            return try_wait(row['attempt_count'], row['attempt_start'], self._now(now)) > 0

    def clear_sign_in_limit(self):
        """Owner recovery from the server: restarts the shared count; the password,
        sessions and marked browsers' own counts stay as they are."""
        with self.transaction() as db:
            db.execute('UPDATE owner SET attempt_count=0,attempt_start=0 WHERE singleton=1')

    def reset_password(self, encoded):
        with self.transaction() as db:
            if db.execute('SELECT username FROM owner WHERE singleton=1').fetchone()[0] is None:
                raise AuthStoreError('owner_not_created')
            db.execute('UPDATE owner SET password_hash=?,attempt_count=0,attempt_start=0 WHERE singleton=1',(encoded,))
            db.execute('DELETE FROM sessions')
