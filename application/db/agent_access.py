"""Revocable research credentials in a separate, host-private SQLite store.

Created only by an explicit owner command. No API data, credentials in plaintext,
or automatic initialization during an HTTP request. No PostgreSQL migration.
"""
from contextlib import closing, contextmanager
import os
import sqlite3
import uuid

from .self_hosted_auth import checked_path, AuthStoreError

MAX_KEYS = 32
SCHEMA_VERSION = 1


class AgentAccessRepository:
    def __init__(self, owner_path):
        self.path = checked_path(owner_path).with_name('research-agents.sqlite3')

    def initialize(self):
        path = checked_path(self.path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            with self.connection():
                return
        else:
            os.close(fd)
        # Keep a failed file closed, never reset/recreate a damaged credential store.
        try:
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript('''
                    PRAGMA synchronous=FULL;
                    PRAGMA secure_delete=ON;
                    CREATE TABLE agent_keys (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL,
                        digest TEXT NOT NULL UNIQUE, binding TEXT NOT NULL,
                        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                        CHECK(expires_at > created_at)
                    );
                    PRAGMA user_version=1;
                ''')
        except sqlite3.Error:
            raise AuthStoreError('agent_store_unavailable') from None

    @contextmanager
    def connection(self, *, write=False):
        db = None
        try:
            path = checked_path(self.path)
            db = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=.5)
            db.row_factory = sqlite3.Row
            if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise AuthStoreError('agent_store_unavailable')
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA secure_delete=ON')
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except sqlite3.Error:
            raise AuthStoreError('agent_store_unavailable') from None
        finally:
            if db is not None:
                db.close()

    def create(self, name, digest, binding, now, expires_at):
        identity = str(uuid.uuid4())
        with self.connection(write=True) as db:
            db.execute('DELETE FROM agent_keys WHERE expires_at<=? OR binding<>?', (now, binding))
            if db.execute('SELECT count(*) FROM agent_keys').fetchone()[0] >= MAX_KEYS:
                raise AuthStoreError('agent_key_limit')
            db.execute('INSERT INTO agent_keys VALUES(?,?,?,?,?,?)', (identity, name, digest, binding, now, expires_at))
        return {'id': identity, 'name': name, 'created_at': now, 'expires_at': expires_at, 'scope': 'research.read'}

    def lookup(self, digest, binding, now):
        if not self.path.exists():
            return None
        with self.connection() as db:
            row = db.execute('SELECT id,expires_at FROM agent_keys WHERE digest=? AND binding=? '
                             'AND created_at<=? AND expires_at>?', (digest, binding, now, now)).fetchone()
            return dict(row) if row else None

    def list(self, binding, now):
        if not self.path.exists():
            return []
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT id,name,created_at,expires_at FROM agent_keys '
                    'WHERE binding=? AND expires_at>? ORDER BY created_at,id LIMIT ?', (binding, now, MAX_KEYS))]

    def revoke(self, identity):
        if not self.path.exists():
            return False
        with self.connection(write=True) as db:
            return db.execute('DELETE FROM agent_keys WHERE id=?', (identity,)).rowcount == 1
