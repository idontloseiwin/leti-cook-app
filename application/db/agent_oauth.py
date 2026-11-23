"""Owner-approved agent connections (MCP OAuth) in their own host-private SQLite file.

Only hashes of codes and tokens are stored. A separate file keeps the key store's
schema unchanged. Its contents are never reset automatically.
"""
from contextlib import closing, contextmanager
import json
import hmac
import os
import sqlite3
import uuid

from .self_hosted_auth import checked_path, AuthStoreError

SCHEMA_VERSION = 1
MAX_CLIENTS = 64
REGISTRATIONS_PER_MINUTE = 10
MAX_CONNECTIONS = 32
UNUSED_CLIENT_SECONDS = 86400
REQUEST_SECONDS = 600
CODE_SECONDS = 120
# Shared by age and capacity cleanup: an unfinished sign-in is still in use.
UNUSED_CLIENT_SQL = ('id NOT IN (SELECT client_id FROM connections) '
                     'AND id NOT IN (SELECT client_id FROM requests) '
                     'AND id NOT IN (SELECT client_id FROM codes)')


class AgentOAuthRepository:
    def __init__(self, owner_path):
        self.path = checked_path(owner_path).with_name('research-oauth.sqlite3')

    def initialize(self):
        path = checked_path(self.path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            with self.connection():
                return
        else:
            os.close(fd)
        try:
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript('''
                    PRAGMA synchronous=FULL;
                    PRAGMA secure_delete=ON;
                    CREATE TABLE clients (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE requests (
                        id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                        redirect_uri TEXT NOT NULL, code_challenge TEXT NOT NULL, state TEXT,
                        resource TEXT NOT NULL, expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE connections (
                        id TEXT PRIMARY KEY, client_id TEXT NOT NULL, name TEXT NOT NULL,
                        redirect_host TEXT NOT NULL, binding TEXT NOT NULL, resource TEXT NOT NULL,
                        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER,
                        CHECK(expires_at > created_at)
                    );
                    CREATE TABLE codes (
                        digest TEXT PRIMARY KEY, client_id TEXT NOT NULL, name TEXT NOT NULL,
                        redirect_uri TEXT NOT NULL, redirect_host TEXT NOT NULL, code_challenge TEXT NOT NULL,
                        resource TEXT NOT NULL, binding TEXT NOT NULL, days INTEGER NOT NULL, expires_at INTEGER NOT NULL
                    );
                    CREATE TABLE tokens (
                        digest TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL CHECK(kind IN ('access','refresh')), expires_at INTEGER NOT NULL,
                        replaced INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX tokens_connection ON tokens(connection_id);
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
            db.execute('PRAGMA foreign_keys=ON')
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

    def _prune(self, db, now):
        db.execute('DELETE FROM requests WHERE expires_at<=?', (now,))
        db.execute('DELETE FROM codes WHERE expires_at<=?', (now,))
        db.execute('DELETE FROM tokens WHERE expires_at<=?', (now,))
        db.execute('DELETE FROM connections WHERE expires_at<=?', (now,))
        # A client may reuse its registration while an Allow request or code is live.
        db.execute('DELETE FROM clients WHERE created_at<=? AND ' + UNUSED_CLIENT_SQL,
                   (now - UNUSED_CLIENT_SECONDS,))

    def register(self, name, redirect_uris, now):
        self.initialize()
        identity = 'lic-client-' + uuid.uuid4().hex
        with self.connection(write=True) as db:
            self._prune(db, now)
            if db.execute('SELECT count(*) FROM clients WHERE created_at>?', (now - 60,)).fetchone()[0] >= REGISTRATIONS_PER_MINUTE:
                raise AuthStoreError('agent_client_rate')
            if db.execute('SELECT count(*) FROM clients').fetchone()[0] >= MAX_CLIENTS:
                # Only a registration nobody is using gives way: no connection, no open
                # Allow page and no unredeemed code. Otherwise refuse the new one.
                oldest = db.execute('SELECT id FROM clients WHERE ' + UNUSED_CLIENT_SQL +
                                    ' ORDER BY created_at LIMIT 1').fetchone()
                if oldest is None:
                    raise AuthStoreError('agent_client_limit')
                db.execute('DELETE FROM clients WHERE id=?', (oldest['id'],))
            db.execute('INSERT INTO clients VALUES(?,?,?,?)', (identity, name, json.dumps(redirect_uris), now))
        return identity

    def client(self, identity):
        if not self.path.exists():
            return None
        with self.connection() as db:
            row = db.execute('SELECT id,name,redirect_uris,created_at FROM clients WHERE id=?', (identity,)).fetchone()
        return None if row is None else {**dict(row), 'redirect_uris': json.loads(row['redirect_uris'])}

    def open_request(self, client_id, redirect_uri, code_challenge, state, resource, now):
        identity = uuid.uuid4().hex
        with self.connection(write=True) as db:
            self._prune(db, now)
            if db.execute('SELECT 1 FROM clients WHERE id=?', (client_id,)).fetchone() is None:
                raise AuthStoreError('agent_client_unknown')
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)', (identity, client_id, redirect_uri, code_challenge,
                                                                      state, resource, now + REQUEST_SECONDS))
        return identity

    def request(self, identity, now):
        if not self.path.exists():
            return None
        with self.connection() as db:
            row = db.execute('SELECT r.*, c.name AS client_name FROM requests r JOIN clients c ON c.id=r.client_id '
                             'WHERE r.id=? AND r.expires_at>?', (identity, now)).fetchone()
        return dict(row) if row else None

    def decide(self, identity, allow, code_digest, binding, redirect_host, days, now):
        """Consume a pending request once. Allow stores a short-lived code; the
        connection itself appears only when the program exchanges that code."""
        with self.connection(write=True) as db:
            self._prune(db, now)
            row = db.execute('SELECT r.*, c.name AS client_name FROM requests r JOIN clients c ON c.id=r.client_id '
                             'WHERE r.id=?', (identity,)).fetchone()
            if row is None:
                raise AuthStoreError('agent_request_expired')
            db.execute('DELETE FROM requests WHERE id=?', (identity,))
            request = dict(row)
            if allow:
                db.execute('INSERT INTO codes VALUES(?,?,?,?,?,?,?,?,?,?)', (code_digest, request['client_id'],
                           request['client_name'], request['redirect_uri'], redirect_host, request['code_challenge'],
                           request['resource'], binding, days, now + CODE_SECONDS))
            return request

    def exchange_code(self, code_digest, client_id, *, binding, resource, redirect_uri, challenge,
                      now, access_digest, access_expires, refresh_digest):
        """Consume once and create the connection atomically, with no cleanup/eviction gap."""
        connection = str(uuid.uuid4())
        with self.connection(write=True) as db:
            self._prune(db, now)  # The still-live code protects its registration here.
            row = db.execute('SELECT * FROM codes WHERE digest=?', (code_digest,)).fetchone()
            if row is None:
                return None
            db.execute('DELETE FROM codes WHERE digest=?', (code_digest,))
            if (row['client_id'] != client_id or row['expires_at'] <= now or row['binding'] != binding
                    or row['resource'] != resource or (redirect_uri is not None and redirect_uri != row['redirect_uri'])
                    or not hmac.compare_digest(row['code_challenge'], challenge)
                    or db.execute('SELECT 1 FROM clients WHERE id=?', (client_id,)).fetchone() is None):
                return None
            expires = now + row['days'] * 86400
            db.execute('DELETE FROM connections WHERE binding<>?', (binding,))
            limited = db.execute('SELECT count(*) FROM connections').fetchone()[0] >= MAX_CONNECTIONS
            if not limited:
                db.execute('INSERT INTO connections VALUES(?,?,?,?,?,?,?,?,NULL)', (connection, client_id,
                           row['name'], row['redirect_host'], binding, resource, now, expires))
                db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (access_digest, connection, 'access',
                                                                     min(access_expires, expires)))
                db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (refresh_digest, connection, 'refresh', expires))
        if limited:
            # Commit consumption even when the connection limit refuses issuance.
            raise AuthStoreError('agent_connection_limit')
        return {'connection_id': connection, 'expires_at': expires}

    def rotate(self, refresh_digest, client_id, binding, now, access_digest, access_expires, new_refresh_digest, resource):
        """Exchange a refresh token once. Reuse of a replaced one ends the whole connection."""
        with self.connection(write=True) as db:
            row = db.execute("SELECT t.*, n.client_id, n.binding, n.resource, n.expires_at AS connection_expires "
                             "FROM tokens t JOIN connections n ON n.id=t.connection_id WHERE t.digest=? AND t.kind='refresh'",
                             (refresh_digest,)).fetchone()
            if row is None or row['client_id'] != client_id or row['resource'] != resource:
                return None
            if row['replaced'] or row['binding'] != binding:
                db.execute('DELETE FROM connections WHERE id=?', (row['connection_id'],))
                return None
            if row['expires_at'] <= now or row['connection_expires'] <= now:
                return None
            db.execute('UPDATE tokens SET replaced=1 WHERE digest=?', (refresh_digest,))
            db.execute("DELETE FROM tokens WHERE connection_id=? AND kind='access'", (row['connection_id'],))
            refresh_expires = row['connection_expires']
            db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (access_digest, row['connection_id'], 'access',
                                                                 min(access_expires, refresh_expires)))
            db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (new_refresh_digest, row['connection_id'], 'refresh',
                                                                 refresh_expires))
            return {'connection_id': row['connection_id'], 'expires_at': refresh_expires}

    def lookup_access(self, digest, binding, now, resource):
        """An access token is valid only for the resource (this /mcp URL) it was issued for."""
        if not self.path.exists():
            return None
        with self.connection() as db:
            row = db.execute("SELECT n.id FROM tokens t JOIN connections n ON n.id=t.connection_id "
                             "WHERE t.digest=? AND t.kind='access' AND t.expires_at>? AND n.expires_at>? AND n.binding=? "
                             "AND n.resource=?", (digest, now, now, binding, resource)).fetchone()
        return row['id'] if row else None

    def touch(self, connection, now):
        with self.connection(write=True) as db:
            db.execute('UPDATE connections SET used_at=? WHERE id=? AND (used_at IS NULL OR used_at<?)',
                       (now, connection, now - 60))

    def list(self, binding, now):
        if not self.path.exists():
            return []
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,name,redirect_host,created_at,expires_at,used_at FROM connections '
                'WHERE binding=? AND expires_at>? ORDER BY created_at,id LIMIT ?', (binding, now, MAX_CONNECTIONS))]

    def revoke(self, identity):
        if not self.path.exists():
            return False
        with self.connection(write=True) as db:
            return db.execute('DELETE FROM connections WHERE id=?', (identity,)).rowcount == 1
