"""Bounded direct PostgreSQL transactions.

A collection scope owns a pool, never a transaction across provider work.
Ordinary SQL cannot borrow a maintenance capability. A privileged call binds
its gate to the exact function this module invokes and never yields a cursor.
"""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
import math
import os
import re
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

import ui_read_trace

from .read_context import http_request_method, is_http_read

_TOKEN = re.compile(r'[a-z][a-z0-9_]{0,95}\Z')
_SQLSTATE = re.compile(r'[A-Z0-9]{5}\Z')
_MAX_ROWS = 100_000
# A single finite import fence, matching the retained atomic restore lock order.
# No caller can supply a table, lock mode or WAIT policy through this capability.
_USER_WORK_IMPORT_LOCK = """LOCK TABLE
 public.channels, public.folders, public.channel_labels,
 public.channel_label_assignments, public.saved_objects,
 public.saved_collection_members, public.saved_collection_commands,
 public.video_type_review_cases, public.video_type_review_history,
 public.user_channel_restore_items IN SHARE ROW EXCLUSIVE MODE NOWAIT"""


# Exact canonical-repair lock order from the retained repair protocol. The
# repeated final playlist lock is intentionally retained for parity. Neither
# relation names, lock modes nor WAIT behavior are supplied by a caller.
_CANONICAL_REPAIR_LOCKS = (
    'LOCK TABLE public.video_inventory IN SHARE ROW EXCLUSIVE MODE',
    'LOCK TABLE public.video_inventory_memberships IN SHARE MODE',
    'LOCK TABLE operator_private.video_collection_state_canonical_repair_lock IN SHARE MODE',
    'LOCK TABLE public.channel_video_inventory_scans IN SHARE MODE',
    'LOCK TABLE public.channel_playlist_state IN SHARE MODE',
    'LOCK TABLE public.channel_playlist_state IN SHARE MODE',
)


# Two fixed queue cursor statements. The only caller inputs are the bounded ID
# window, run UUID and optional typed format; there is no arbitrary SQL cursor.
_VIDEO_SCHEDULED_CANDIDATES_BASE = """SELECT state.video_id,state.channel_id,state.video_type,
 state.availability_first_missing_at,state.availability_schedule_type,state.availability_resume_observed_at,
 now() AS observed_at
 FROM unnest(%(video_ids)s::text[]) WITH ORDINALITY AS page(video_id,rank)
 JOIN public.video_collection_state state ON state.video_id=page.video_id
 WHERE state.paused_reason IS NULL AND state.next_due_at<=now()
 AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND state.last_claim_run_id IS DISTINCT FROM %(run_id)s::uuid
 {typed}
 ORDER BY page.rank FOR UPDATE OF state SKIP LOCKED"""
_VIDEO_SCHEDULED_CANDIDATES = {
    False: _VIDEO_SCHEDULED_CANDIDATES_BASE.format(typed=''),
    True: _VIDEO_SCHEDULED_CANDIDATES_BASE.format(typed='AND state.video_type=%(video_type)s::text'),
}


# Fixed protected source enumeration. Only channel/type values reach this cursor;
# callers receive bounded rows, never a cursor, SQL text or a connection.
_INVENTORY_SOURCE = """SELECT video_id,published_at,source_checked_at,source_expires_at
 FROM readable.hot_video_inventory WHERE channel_id=%s AND video_type=%s
 ORDER BY video_id ASC"""


class DatabaseError(RuntimeError):
    """Only public codes: never retain a DSN, SQL parameters or raw DETAIL."""
    def __init__(self, message: str, *, code: str | None = None,
                 outcome_unknown: bool = False):
        self.message, self.code = message, code
        self.outcome_unknown = outcome_unknown
        self.details = self.hint = None
        super().__init__(message)


class DatabaseServerError(DatabaseError):
    """A server-confirmed statement/transaction error, not a missing ACK."""


class DatabaseUnavailable(DatabaseError):
    def __init__(self, message: str, *, code: str | None = None,
                 outcome_unknown: bool = False, reason: str = 'unavailable'):
        # Fixed diagnostic vocabulary only; never attach the provider's text.
        if reason not in {'unavailable', 'pool_timeout', 'pool_queue_full',
                          'pool_closed', 'connection_failed'}:
            raise ValueError('database_unavailable_reason_invalid')
        self.reason = reason
        super().__init__(message, code=code, outcome_unknown=outcome_unknown)


def _integer(value: int, low: int, high: int, code: str) -> None:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(code)


@dataclass(frozen=True)
class PostgresConfig:
    dsn: str = field(repr=False)
    min_size: int = 0
    max_size: int = 4
    max_waiting: int = 16
    acquire_timeout: float = 3.0
    connect_timeout: int = 5
    statement_timeout_ceiling_ms: int = 120_000

    def __post_init__(self):
        _integer(self.min_size, 0, 16, 'database_pool_config_invalid')
        _integer(self.max_size, 1, 16, 'database_pool_config_invalid')
        _integer(self.max_waiting, 1, 128, 'database_pool_config_invalid')
        _integer(self.connect_timeout, 1, 30, 'database_pool_config_invalid')
        _integer(self.statement_timeout_ceiling_ms, 1, 120_000, 'database_timeout_invalid')
        if self.min_size > self.max_size:
            raise ValueError('database_pool_config_invalid')
        if (type(self.acquire_timeout) not in (int, float)
                or not math.isfinite(self.acquire_timeout)
                or not 0 < self.acquire_timeout <= 30):
            raise ValueError('database_pool_config_invalid')
        try:
            if not isinstance(self.dsn, str) or not self.dsn:
                raise ValueError()
            info = conninfo_to_dict(self.dsn)
            if (info.get('user') != 'authenticator'
                    or any(not info.get(k) for k in ('host', 'port', 'dbname', 'password'))
                    or any(k in info for k in ('service', 'servicefile', 'options', 'hostaddr'))):
                raise ValueError()
        except Exception:
            raise ValueError('database_connection_config_invalid') from None


@dataclass(frozen=True)
class Operation:
    name: str
    read_only: bool = False
    statement_timeout_ms: int = 60_000
    lock_timeout_ms: int = 0
    error_codes: frozenset[str] = frozenset()
    total_timeout_ms: int | None = None
    repeatable_read: bool = False
    # Opt in only after reviewing every statement, view and trigger in the
    # complete Python protocol for statement_timeout changes. Not a SQL purity
    # detector; arbitrary/native function callers must retain the default.
    cache_statement_timeout: bool = False
    # For infrequent, many-branch probes whose measured JIT compilation costs
    # more than execution. Default operations retain the server's JIT setting.
    disable_jit: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str) or not _TOKEN.fullmatch(self.name):
            raise ValueError('database_operation_invalid')
        if (type(self.read_only) is not bool or type(self.repeatable_read) is not bool
                or type(self.cache_statement_timeout) is not bool
                or type(self.disable_jit) is not bool):
            raise ValueError('database_operation_invalid')
        if self.repeatable_read and not self.read_only:
            raise ValueError('database_operation_isolation_invalid')
        if self.total_timeout_ms is not None:
            _integer(self.total_timeout_ms, 1, 120_000, 'database_timeout_invalid')
        _integer(self.statement_timeout_ms, 1, 120_000, 'database_timeout_invalid')
        _integer(self.lock_timeout_ms, 0, 60_000, 'database_timeout_invalid')
        if (type(self.error_codes) is not frozenset
                or any(not isinstance(c, str) or not _TOKEN.fullmatch(c) for c in self.error_codes)):
            raise ValueError('database_error_codes_invalid')






def _mapped(error: Exception, *, unknown: bool, allowed: frozenset[str]) -> DatabaseError:
    if isinstance(error, (PoolTimeout, TooManyRequests, PoolClosed)):
        reason = ('pool_timeout' if isinstance(error, PoolTimeout) else
                  'pool_queue_full' if isinstance(error, TooManyRequests) else 'pool_closed')
        return DatabaseUnavailable('database_pool_unavailable', reason=reason)
    state = getattr(error, 'sqlstate', None)
    state = state if isinstance(state, str) and _SQLSTATE.fullmatch(state) else None
    if state and not state.startswith('08') and state not in {'57P01', '57P02', '57P03'}:
        primary = getattr(getattr(error, 'diag', None), 'message_primary', None)
        message = primary if primary in allowed else 'database_operation_failed'
        return DatabaseServerError(message, code=state)
    return DatabaseUnavailable('database_connection_failed', code=state,
                               outcome_unknown=unknown, reason='connection_failed')


def _runtime_admission(function_name: str | None) -> None:
    from hot_runtime import (HotDatabaseAdmissionDenied, HotRuntimeUnavailable,
                             require_hot_runtime_ready, mark_collection_interrupted)
    if os.environ.get('LIC_DATABASE_TRANSPORT', 'postgrest').strip() != 'postgres':
        raise DatabaseUnavailable('direct_postgres_not_selected')
    try:
        require_hot_runtime_ready(operation='database', rpc_name=function_name)
    except HotRuntimeUnavailable as error:
        mark_collection_interrupted()
        raise HotDatabaseAdmissionDenied(error.code) from None


class _SafeConnection(psycopg.Connection):
    @classmethod
    def connect(cls, *args, **kwargs):
        # psycopg_pool logs connection exceptions in background threads. Strip
        # libpq's endpoint/config text before those exceptions reach its logger.
        try:
            return super().connect(*args, **kwargs)
        except psycopg.Error:
            raise psycopg.OperationalError('database_connection_failed') from None


def _validate_runtime_connection(connection):
    # Configuration names alone do not prove that an installation has kept
    # the expected least-privilege roles. Probe once per new connection.
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT current_setting('server_version_num')::integer AS server_version,
                       r.rolname AS login, r.rolsuper, r.rolcreatedb,
                       r.rolcreaterole, r.rolreplication, r.rolbypassrls,
                       r.rolinherit, r.rolcanlogin AS can_login, current_user AS active_role,
                       (SELECT pg_catalog.array_agg(g.rolname ORDER BY g.rolname)
                        FROM pg_catalog.pg_auth_members m
                        JOIN pg_catalog.pg_roles g ON g.oid=m.roleid
                        WHERE m.member=r.oid) AS memberships,
                       EXISTS(SELECT 1 FROM pg_catalog.pg_database d
                              WHERE d.datname=pg_catalog.current_database()
                                AND d.datdba=r.oid) AS owns_database,
                       m.set_option AS member_set, m.admin_option AS member_admin,
                       m.inherit_option AS member_inherit,
                       s.oid IS NOT NULL AS service_exists,
                       s.rolsuper AS service_super, s.rolcreatedb AS service_createdb,
                       s.rolcreaterole AS service_createrole,
                       s.rolreplication AS service_replication,
                       s.rolcanlogin AS service_can_login,
                       s.rolinherit AS service_inherit, s.rolbypassrls AS service_bypassrls,
                       COALESCE((SELECT pg_catalog.array_agg(g.rolname ORDER BY g.rolname)
                           FROM pg_catalog.pg_auth_members sm
                           JOIN pg_catalog.pg_roles g ON g.oid=sm.roleid
                           WHERE sm.member=s.oid), ARRAY[]::name[]) AS service_memberships,
                       EXISTS(SELECT 1 FROM pg_catalog.pg_database d
                              WHERE d.datname=pg_catalog.current_database()
                                AND d.datdba=s.oid) AS service_owns_database,
                       reader.oid IS NOT NULL AS reader_exists,
                       reader.rolsuper AS reader_super, reader.rolcreatedb AS reader_createdb,
                       reader.rolcreaterole AS reader_createrole,
                       reader.rolreplication AS reader_replication,
                       reader.rolcanlogin AS reader_can_login,
                       reader.rolinherit AS reader_inherit, reader.rolbypassrls AS reader_bypassrls,
                       rm.set_option AS reader_member_set, rm.admin_option AS reader_member_admin,
                       rm.inherit_option AS reader_member_inherit,
                       COALESCE((SELECT pg_catalog.array_agg(g.rolname ORDER BY g.rolname)
                           FROM pg_catalog.pg_auth_members membership
                           JOIN pg_catalog.pg_roles g ON g.oid=membership.roleid
                           WHERE membership.member=reader.oid), ARRAY[]::name[]) AS reader_memberships,
                       EXISTS(SELECT 1 FROM pg_catalog.pg_database d
                              WHERE d.datname=pg_catalog.current_database()
                                AND d.datdba=reader.oid) AS reader_owns_database
                FROM pg_catalog.pg_roles r
                LEFT JOIN pg_catalog.pg_roles s ON s.rolname='service_role'
                LEFT JOIN pg_catalog.pg_auth_members m ON m.member=r.oid AND m.roleid=s.oid
                LEFT JOIN pg_catalog.pg_roles reader ON reader.rolname='lic_reader'
                LEFT JOIN pg_catalog.pg_auth_members rm ON rm.member=r.oid AND rm.roleid=reader.oid
                WHERE r.rolname=session_user
            """)
            row = cursor.fetchone()
        if (not row or row['login'] != 'authenticator'
                or row['active_role'] != 'authenticator' or not row['can_login']
                or row['memberships'] != ['lic_reader', 'service_role']
                or not row['service_exists'] or not row['member_set']
                or row['member_admin'] or row['member_inherit']
                or not row['service_bypassrls'] or row['service_memberships'] != []
                or not row['reader_exists'] or not row['reader_member_set']
                or row['reader_member_admin'] or row['reader_member_inherit']
                or row['reader_memberships'] != []
                or any(row[k] for k in ('reader_super', 'reader_createdb', 'reader_createrole',
                                       'reader_replication', 'reader_can_login', 'reader_inherit',
                                       'reader_bypassrls', 'reader_owns_database'))
                or any(row[k] for k in ('service_super', 'service_createdb', 'service_createrole',
                                       'service_replication', 'service_can_login',
                                       'service_owns_database'))
                or any(row[k] for k in ('rolsuper', 'rolcreatedb', 'rolcreaterole',
                                       'rolreplication', 'rolbypassrls', 'rolinherit',
                                       'owns_database'))):
            raise DatabaseUnavailable('database_runtime_role_invalid')
        # Membership INSERT-vs-conflict capture relies on PG17's native xmax
        # representation. Revalidate that protocol before enabling a new major;
        # an image pin alone cannot protect an externally configured DSN.
        version = row['server_version']
        if type(version) is not int or not 170000 <= version < 180000:
            raise DatabaseUnavailable('database_server_version_unsupported')
    except DatabaseError:
        raise
    except Exception:
        raise DatabaseUnavailable('database_role_check_failed') from None


def _reviewed_statement(statement, *, read, max_rows, returning=False):
    # A conservative guard for repository-owned SQL, not a SQL parser or an
    # injection firewall. Values MUST be parameters. Arbitrary scripts and
    # session commands are not admitted. DML RETURNING has a separate path.
    if not isinstance(statement, (str, sql.Composable)):
        raise DatabaseError('database_statement_invalid')
    text = statement if isinstance(statement, str) else statement.as_string()
    first = re.match(r'\s*([A-Za-z]+)\b', text)
    if (not first or ';' in text
            or first[1].upper() not in ({'INSERT', 'UPDATE', 'DELETE'} if returning else
                                       {'SELECT', 'WITH'} if read else {'INSERT', 'UPDATE', 'DELETE'})
            or (returning and not re.search(r'\bRETURNING\b', text, re.IGNORECASE))
            or (not read and re.search(r'\bRETURNING\b', text, re.IGNORECASE))):
        raise DatabaseError('database_statement_invalid')
    query = sql.SQL(text)
    if returning:
        return sql.SQL('WITH lic_changed AS ({}) SELECT * FROM lic_changed LIMIT {}').format(
            query, sql.Literal(max_rows + 1))
    if read:
        # Server-side cap: fetchmany alone would buffer an unlimited result in
        # a regular Psycopg cursor before detecting overflow.
        query = sql.SQL('SELECT * FROM ({}) AS lic_bounded_result LIMIT {}').format(
            query, sql.Literal(max_rows + 1))
    return query


def _reconnect_waiting_pool(pool):
    """Keep an empty pool recoverable after expired waits fill its queue.

    psycopg_pool 3.3.3 removes timed-out waiter records only when a connection
    arrives. After reconnect_timeout it can instead reach zero connections;
    max_waiting then rejects every request before it can ask the pool to grow.
    This callback runs on our one pool worker after that failed attempt ends.
    With no connection left, check() only schedules bounded background growth:
    it checks no live connection, closes no shared pool and replays no SQL.
    Recorded (including expired) waiters keep retries active until recovery or
    closure, using the pool's existing connection backoff and size limits.
    """
    if pool.closed:
        return
    stats = pool.get_stats()
    if stats['pool_size'] == 0 and stats['requests_waiting'] > 0:
        pool.check()


def _new_pool(config: PostgresConfig):
    return ConnectionPool(
        config.dsn, connection_class=_SafeConnection,
        kwargs={'autocommit': True, 'row_factory': dict_row,
                'connect_timeout': config.connect_timeout,
                'application_name': 'leti-cook', 'prepare_threshold': None, 'options': ''},
        min_size=config.min_size, max_size=config.max_size,
        max_waiting=config.max_waiting, timeout=config.acquire_timeout,
        reconnect_timeout=config.connect_timeout, num_workers=1,
        reconnect_failed=_reconnect_waiting_pool,
        name='leti-cook-db', open=False, configure=_validate_runtime_connection,
    )


class Transaction:
    """An operation-owned connection. No commit, cursor or raw-client escape."""
    def __init__(self, connection, admission, *, rpc_name=None, allowed=frozenset(), read_only=False,
                 total_timeout_ms=None, statement_timeout_ms=60_000,
                 cache_statement_timeout=False, cadence_snapshot=None):
        self._connection, self._admission = connection, admission
        self._cadence_snapshot = cadence_snapshot
        self._rpc_name = rpc_name
        self._allowed, self._read_only = allowed, read_only
        self._thread, self._pid = threading.get_ident(), os.getpid()
        self._active, self._failed, self._dispatched = True, False, False
        self._deadline = None if total_timeout_ms is None else time.monotonic() + total_timeout_ms / 1000
        self._statement_timeout_ms = statement_timeout_ms
        # Native RPCs may temporarily or persistently change LOCAL settings.
        # Only explicitly reviewed Python protocols can reuse a known setting.
        self._cache_statement_timeout = cache_statement_timeout and rpc_name is None
        self._known_statement_timeout_ms = None
        self._savepoint_depth = 0
        self._scheduled_cursor_active = False

    @property
    def cadence_snapshot(self):
        self._check()
        return self._cadence_snapshot

    def _check(self):
        if (not self._active or self._pid != os.getpid()
                or self._thread != threading.get_ident()):
            raise DatabaseError('database_transaction_owner_invalid')
        if self._failed:
            raise DatabaseError('database_transaction_aborted')

    def _check_budget(self, *, configure=False):
        if self._deadline is None:
            return
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self._failed = True
            raise DatabaseServerError('database_operation_failed', code='57014')
        if configure:
            # The former single RPC had one statement budget. Python steps must
            # not multiply it. Settings are transaction-local and contain no
            # data; the domain statement receives another admission check.
            timeout = min(self._statement_timeout_ms, max(1, math.ceil(remaining * 1000)))
            if self._cache_statement_timeout and self._known_statement_timeout_ms == timeout:
                return
            self._known_statement_timeout_ms = None
            self._connection.execute("SELECT set_config('statement_timeout', %s, true)", (f'{timeout}ms',))
            if self._cache_statement_timeout:
                self._known_statement_timeout_ms = timeout

    @contextmanager
    def borrow(self):
        """Join this transaction; a caught helper failure still requires rollback.

        Native statement errors already poison it. Python domain checks must
        have the same property when a composed helper uses this context.
        An enclosing explicit savepoint may clear it only after rollback.
        """
        self._check()
        try:
            yield self
        except BaseException:
            self._failed = True
            raise

    @contextmanager
    def savepoint(self):
        """Recover a known per-item failure only after its rollback succeeds.

        Errors caught outside this explicit context still poison the enclosing
        transaction. This creates no retry and cannot commit the parent work.
        """
        self._check()
        if self._rpc_name not in (None, 'cleanup_deleted_saved_objects', 'cleanup_hot_evidence', 'cleanup_hot_current_source', 'cleanup_hot_inventory_copies', 'cleanup_hot_operational_copies', 'cleanup_hot_deleted_channels', 'cleanup_hot_inventory', 'cleanup_hot_category_reference', 'cleanup_hot_priority') or self._savepoint_depth >= 8:
            raise DatabaseError('database_savepoint_invalid')
        original = None
        self._savepoint_depth += 1
        try:
            with self._connection.transaction():
                try:
                    yield self
                    if self._failed:
                        raise DatabaseError('database_transaction_aborted')
                    self._check_budget()
                except BaseException as error:
                    original = error
                    raise
        except BaseException as error:
            # ROLLBACK TO restores LOCAL GUCs too. Invalidate before clearing
            # poisoning, including nested, enter/release and rollback failures.
            self._known_statement_timeout_ms = None
            # Identity proves the transaction context returned the original
            # failure after rollback, rather than a new rollback/connection
            # failure. Never clear poisoning when rollback itself failed.
            if error is original:
                self._failed = False
                raise
            self._failed = True
            if isinstance(error, psycopg.Error):
                raise _mapped(error, unknown=self._dispatched and not self._read_only,
                              allowed=self._allowed) from None
            raise
        finally:
            self._savepoint_depth -= 1

    def _run(self, statement, parameters, mode, max_rows):
        self._check()
        invalidates_timeout = self._rpc_name is not None
        try:
            if mode == 'user_work_import_lock':
                if self._read_only or self._rpc_name is not None:
                    raise DatabaseError('database_lock_capability_invalid')
                statement, parameters, mode = _USER_WORK_IMPORT_LOCK, (), 'execute'
            elif mode == 'canonical_repair_lock':
                if (self._read_only or self._rpc_name is not None or statement is not None
                        or type(parameters) is not tuple or len(parameters) != 1
                        or type(parameters[0]) is not int
                        or not 0 <= parameters[0] < len(_CANONICAL_REPAIR_LOCKS)):
                    raise DatabaseError('database_lock_capability_invalid')
                statement = _CANONICAL_REPAIR_LOCKS[parameters[0]]
                parameters, mode = (), 'execute'
            elif mode == 'operational_assignment_validation':
                if (self._read_only or self._rpc_name is not None or statement is not None
                        or self._deadline is None or type(parameters) is not tuple
                        or len(parameters) != 3 or max_rows != 1):
                    raise DatabaseError('database_assignment_capability_invalid')
                from .operational_assignment_validation import assignment_plan
                statement, parameters = assignment_plan(*parameters)
                mode = 'assignment_plan'
            elif mode == 'typed_reconcile_acquire':
                from ..videos.typed_reconciliation_schedule import _ACQUIRE, _ACQUIRE_KEYS, acquisition_parameters
                if (self._read_only or self._rpc_name is not None or statement is not None
                        or self._deadline is None or type(parameters) is not dict
                        or set(parameters) != _ACQUIRE_KEYS or max_rows != 1000
                        or type(parameters['inventory_ids']) not in (list, tuple)
                        or len(parameters['inventory_ids']) > 1000):
                    raise DatabaseError('database_typed_reconcile_capability_invalid')
                parameters = acquisition_parameters(self, parameters)
                statement, mode = _ACQUIRE, 'all'
            elif mode == 'saved_membership_finalize':
                from ..user_work.saved_mutations import _FINALIZE_CHANGED_MEMBERS, _FINALIZE_CHANGED_MEMBERS_KEYS
                if (self._read_only or self._rpc_name is not None or statement is not None
                        or type(parameters) is not dict or set(parameters) != _FINALIZE_CHANGED_MEMBERS_KEYS
                        or max_rows != 1):
                    raise DatabaseError('database_saved_finalize_capability_invalid')
                # Fixed top-level DML CTE only. A generic SELECT wrapper would
                # make PostgreSQL reject nested data-modifying WITH. The SQL
                # itself returns at most two rows; the normal bound remains1.
                statement, mode = _FINALIZE_CHANGED_MEMBERS, 'one'
            elif mode == 'channel_insert_effects':
                if (self._read_only or self._rpc_name is not None or statement is not None
                        or type(max_rows) is not int or max_rows != 1
                        or type(parameters) is not tuple or len(parameters) != 2):
                    raise DatabaseError('database_channel_insert_capability_invalid')
                from ..channel.channel_insert_effects import channel_insert_statement
                statement, parameters = channel_insert_statement(*parameters)
                mode = 'one'
            else:
                statement = _reviewed_statement(statement, read=mode != 'execute', max_rows=max_rows,
                                                returning=mode in {'returning_one', 'returning_all'})
            if self._cache_statement_timeout:
                text = statement if isinstance(statement, str) else statement.as_string()
                # Defense in depth, not a parser: quoted/schema-qualified and
                # case variants match; any escape may conceal a Unicode name.
                invalidates_timeout = 'set_config' in text.lower() or '\\' in text
            if invalidates_timeout:
                self._known_statement_timeout_ms = None
            self._admission(self._rpc_name)
            self._check_budget(configure=True)
            if self._deadline is not None:
                self._admission(self._rpc_name)
            with self._connection.cursor() as cursor:
                self._dispatched = True
                cursor.execute(statement, parameters)
                if mode == 'execute':
                    if cursor.description is not None:
                        raise DatabaseError('database_unexpected_result_set')
                    return cursor.rowcount
                if cursor.description is None:
                    raise DatabaseError('database_result_missing')
                rows = cursor.fetchmany(max_rows + 1)
                if len(rows) > max_rows:
                    raise DatabaseError('database_result_limit_exceeded')
                if mode == 'assignment_plan':
                    if (len(rows) != 1 or not isinstance(rows[0], Mapping)
                            or set(rows[0]) != {'QUERY PLAN'} or type(rows[0]['QUERY PLAN']) is not list
                            or len(rows[0]['QUERY PLAN']) != 1):
                        raise DatabaseError('database_result_shape_invalid')
                    return None
                if mode in {'one', 'returning_one'}:
                    return rows[0] if rows else None
                return rows
        except psycopg.Error as error:
            self._failed = True
            raise _mapped(error, unknown=self._dispatched and not self._read_only,
                          allowed=self._allowed) from None
        except BaseException:
            # Even when callers catch an error, the outer context must roll
            # back all work instead of silently committing an earlier write.
            self._failed = True
            raise
        finally:
            if invalidates_timeout:
                self._known_statement_timeout_ms = None

    @contextmanager
    def inventory_source_rows(self, channel_id, video_type):
        """One protected read, with bounded fetches under a finite operation."""
        self._check()
        owned = False
        try:
            if (not self._read_only or self._rpc_name is not None
                    or self._deadline is None or self._scheduled_cursor_active
                    or not isinstance(channel_id, str)
                    or video_type not in ('long', 'short', 'stream')):
                raise DatabaseError('database_inventory_source_capability_invalid')
            self._scheduled_cursor_active = True
            owned = True
            self._admission(None)
            self._check_budget(configure=True)
            self._admission(None)
            with self._connection.cursor(name='lic_inventory_source', scrollable=False,
                                         withhold=False, row_factory=dict_row) as cursor:
                self._dispatched = True
                cursor.execute(_INVENTORY_SOURCE, (channel_id, video_type))
                stream = _InventorySourceRows(self, cursor)
                try:
                    yield stream
                    self._check()
                    self._check_budget()
                finally:
                    stream._active = False
        except psycopg.Error as error:
            self._failed = True
            raise _mapped(error, unknown=False, allowed=self._allowed) from None
        except BaseException:
            self._failed = True
            raise
        finally:
            if owned:
                self._scheduled_cursor_active = False

    @contextmanager
    def video_scheduled_candidates(self, video_ids, run_id, *, video_type=None):
        """Fixed ordered SKIP LOCKED cursor, preserving PG17 FOR-query batches.

        PostgreSQL17 atomic PL/pgSQL FOR queries fetch10 rows first, then50.
        Keeping these batches preserves early-exit row locks without eagerly
        locking the entire1000-ID window. Native acceptance remains required.
        The iterator owns no commit, cursor API, SQL text or pool escape.
        """
        self._check()
        owned=False
        try:
            if (self._read_only or self._rpc_name is not None or self._scheduled_cursor_active
                    or not isinstance(video_ids, (list, tuple)) or len(video_ids)>1000
                    or any(not isinstance(value,str) for value in video_ids)
                    or len(set(video_ids))!=len(video_ids) or video_type not in (None,'short','stream')):
                raise DatabaseError('database_cursor_capability_invalid')
            self._scheduled_cursor_active=True
            owned=True
            self._admission(None)
            self._check_budget(configure=True)
            if self._deadline is not None:self._admission(None)
            with self._connection.cursor(name='lic_video_scheduled_candidates',scrollable=False,
                                         withhold=False,row_factory=dict_row) as cursor:
                self._dispatched=True
                cursor.execute(_VIDEO_SCHEDULED_CANDIDATES[video_type is not None],
                               dict(video_ids=list(video_ids),run_id=run_id,video_type=video_type))
                stream=_ScheduledVideoCandidates(self,cursor,len(video_ids))
                try:
                    yield stream
                    self._check()
                    self._check_budget()
                finally:
                    stream._active=False
        except psycopg.Error as error:
            self._failed=True
            raise _mapped(error,unknown=self._dispatched,allowed=self._allowed) from None
        except BaseException:
            self._failed=True
            raise
        finally:
            if owned:self._scheduled_cursor_active=False

    @contextmanager
    def inventory_claim_candidates(self, arguments):
        """Fixed playlist cursor, with the same bounded PG17 FOR-query batches."""
        self._check()
        owned = False
        try:
            if (self._read_only or self._rpc_name is not None or self._scheduled_cursor_active
                    or not isinstance(arguments, Mapping)):
                raise DatabaseError('database_cursor_capability_invalid')
            limit = arguments.get('claim_limit')
            channels, types = arguments.get('channel_ids'), arguments.get('video_types')
            if (type(limit) is not int or not 1 <= limit <= 500
                    or type(arguments.get('only_due')) is not bool
                    or type(arguments.get('include_due')) is not bool):
                raise DatabaseError('database_cursor_capability_invalid')
            keys = ('claim_limit', 'channel_ids', 'video_types', 'scopes_text', 'only_due',
                    'continuations_text', 'include_due', 'initial_job_id', 'initial_job_lease_token')
            if any(key not in arguments for key in keys):
                raise DatabaseError('database_cursor_capability_invalid')
            parameters = {key: arguments[key] for key in keys}
            # Native text[] inputs can contain NULLs and multiple dimensions.
            # Preserve their shape; SQL owns rectangularity and casts already.
            def copy_array(array, maximum, *, nullable=False, choices=None):
                visited = items = 0
                def copy(value, depth=0):
                    nonlocal visited, items
                    visited += 1
                    if depth > 32 or visited > maximum * 32:
                        raise DatabaseError('database_cursor_capability_invalid')
                    if isinstance(value, (list, tuple)):
                        return [copy(item, depth + 1) for item in value]
                    if not (isinstance(value, str) or (nullable and value is None)):
                        raise DatabaseError('database_cursor_capability_invalid')
                    if choices is not None and value not in choices:
                        raise DatabaseError('database_cursor_capability_invalid')
                    items += 1
                    if items > maximum:
                        raise DatabaseError('database_cursor_capability_invalid')
                    return value
                if not isinstance(array, (list, tuple)):
                    raise DatabaseError('database_cursor_capability_invalid')
                result = copy(array)
                if items == 0:
                    raise DatabaseError('database_cursor_capability_invalid')
                return result
            parameters['channel_ids'] = None if channels is None else copy_array(channels, 1000, nullable=True)
            parameters['video_types'] = copy_array(types, 3, choices=('long', 'short', 'stream'))
            from ..inventory.inventory_claims import _CANDIDATES
            self._scheduled_cursor_active = True
            owned = True
            self._admission(None)
            self._check_budget(configure=True)
            if self._deadline is not None:
                self._admission(None)
            with self._connection.cursor(name='lic_inventory_claim_candidates', scrollable=False,
                                         withhold=False, row_factory=dict_row) as cursor:
                self._dispatched = True
                cursor.execute(_CANDIDATES, parameters)
                stream = _ScheduledVideoCandidates(self, cursor, limit)
                try:
                    yield stream
                    self._check()
                    self._check_budget()
                finally:
                    stream._active = False
        except psycopg.Error as error:
            self._failed = True
            raise _mapped(error, unknown=self._dispatched, allowed=self._allowed) from None
        except BaseException:
            self._failed = True
            raise
        finally:
            if owned:
                self._scheduled_cursor_active = False

    @contextmanager
    def quota_retry_children(self, *, parent_id):
        """Fixed maintenance-only child snapshot; no child page or SQL input."""
        self._check()
        owned = False
        try:
            if (self._read_only or self._rpc_name not in ('cleanup_hot_operational_copies', 'cleanup_hot_priority')
                    or self._scheduled_cursor_active or self._deadline is None
                    or type(parent_id) is not str or len(parent_id) != 36):
                raise DatabaseError('database_cursor_capability_invalid')
            from ..quota.quota_retry_detachment import _CHILDREN
            self._scheduled_cursor_active = True
            owned = True
            self._admission(self._rpc_name)
            self._check_budget(configure=True)
            self._admission(self._rpc_name)
            with self._connection.cursor(name='lic_quota_retry_children', scrollable=False,
                                         withhold=False, row_factory=dict_row) as cursor:
                self._dispatched = True
                cursor.execute(_CHILDREN, (parent_id, parent_id))
                stream = _QuotaRetryChildrenRows(self, cursor)
                try:
                    yield stream
                    self._check()
                    self._check_budget()
                finally:
                    stream._active = False
        except psycopg.Error as error:
            self._failed = True
            raise _mapped(error, unknown=self._dispatched, allowed=self._allowed) from None
        except BaseException:
            self._failed = True
            raise
        finally:
            if owned:
                self._scheduled_cursor_active = False

    @contextmanager
    def sync_run_recovery_candidates(self, *, before, finished_at):
        """Fixed stale-run snapshot; one row per fetch, one transaction deadline.

        The recovery owner normalizes each complete proposed row and writes it
        while its lock remains held. No SQL, batch size or total cutoff is input.
        """
        self._check()
        owned = False
        try:
            if (self._read_only or self._rpc_name is not None or self._scheduled_cursor_active
                    or self._deadline is None):
                raise DatabaseError('database_cursor_capability_invalid')
            from ..runs.sync_run_recovery import _RECOVERY_CANDIDATES
            self._scheduled_cursor_active = True
            owned = True
            self._admission(None)
            self._check_budget(configure=True)
            self._admission(None)
            with self._connection.cursor(name='lic_sync_run_recovery', scrollable=False,
                                         withhold=False, row_factory=dict_row) as cursor:
                self._dispatched = True
                cursor.execute(_RECOVERY_CANDIDATES, dict(before=before,finished_at=finished_at))
                stream = _SyncRunRecoveryRows(self,cursor)
                try:
                    yield stream
                    self._check()
                    self._check_budget()
                finally:
                    stream._active = False
        except psycopg.Error as error:
            self._failed = True
            raise _mapped(error,unknown=self._dispatched,allowed=self._allowed) from None
        except BaseException:
            self._failed = True
            raise
        finally:
            if owned:
                self._scheduled_cursor_active = False

    def lock_user_work_import(self) -> None:
        """NOWAIT lock of the fixed user-work graph; caller owns its savepoint."""
        self._run(None, (), 'user_work_import_lock', 0)

    def lock_typed_inventory_repair(self) -> None:
        """Hold the six fixed evidence/gate locks in this caller's transaction."""
        for index in range(len(_CANONICAL_REPAIR_LOCKS)):
            self._run(None, (index,), 'canonical_repair_lock', 0)

    def typed_reconcile_acquire(self, *, video_type, inventory_ids, scopes):
        """Acquire at most1000 native UPSERT rows within this existing owner."""
        return self._run(None, dict(video_type=video_type, inventory_ids=inventory_ids, scopes=scopes),
                         'typed_reconcile_acquire', 1000)

    def finalize_saved_collection_members(self, *, collection_id, request_id,
                                          fingerprint, action, requested_count,
                                          changed_count, delta, changed_at):
        """Fixed positive-membership finalization, in the existing transaction.

        The owner has already locked/validated the collection, read its receipt,
        captured the clock and completed membership DML and its statement guards.
        This method opens no transaction and does not expose arbitrary CTE SQL.
        """
        return self._run(None, dict(id=collection_id, request_id=request_id,
            fingerprint=fingerprint, action=action, requested_count=requested_count,
            changed_count=changed_count, delta=delta, now=changed_at),
            'saved_membership_finalize', 1)

    def validate_operational_assignments(self, family, values, *, identity=None) -> None:
        """Plan one of five fixed original DML shapes without executing it.

        Native assignment type checks precede explicit proposed-row casts,
        including an absent target. This borrows the existing write admission,
        transaction deadline and error handling; no general EXPLAIN API exists.
        """
        return self._run(None, (family, values, identity), 'operational_assignment_validation', 1)

    def insert_channel_with_effects(self, family, parameters):
        """One of two fixed channel INSERTs, under this operation's ownership."""
        return self._run(None, (family, parameters), 'channel_insert_effects', 1)

    def execute(self, statement, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        return self._run(statement, parameters, 'execute', 0)

    def fetch_one(self, statement, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> dict | None:
        return self._run(statement, parameters, 'one', 1)

    def fetch_all(self, statement, parameters: Sequence[Any] | Mapping[str, Any] = (), *, max_rows: int) -> list[dict]:
        _integer(max_rows, 1, _MAX_ROWS, 'database_result_limit_invalid')
        return self._run(statement, parameters, 'all', max_rows)

    def returning_one(self, statement, parameters: Sequence[Any] | Mapping[str, Any] = ()) -> dict | None:
        return self._run(statement, parameters, 'returning_one', 1)

    def returning_all(self, statement, parameters: Sequence[Any] | Mapping[str, Any] = (), *, max_rows: int) -> list[dict]:
        _integer(max_rows, 1, _MAX_ROWS, 'database_result_limit_invalid')
        return self._run(statement, parameters, 'returning_all', max_rows)

    @contextmanager
    def channel_membership_cascade_rows(self, *, channel_ids):
        """Fixed final-channel cascade snapshot; no SQL, row limit or callback."""
        self._check()
        owned = False
        try:
            if (self._read_only or self._rpc_name not in ('cleanup_hot_deleted_channels', 'cleanup_hot_priority')
                    or self._scheduled_cursor_active or self._deadline is None
                    or type(channel_ids) not in (list, tuple) or not 1 <= len(channel_ids) <= 100
                    or any(type(value) is not str for value in channel_ids)):
                raise DatabaseError('database_cursor_capability_invalid')
            from ..channel.channel_membership_cascade import _MEMBERS
            self._scheduled_cursor_active = True
            owned = True
            self._admission(self._rpc_name)
            self._check_budget(configure=True)
            self._admission(self._rpc_name)
            with self._connection.cursor(name='lic_channel_membership_cascade', scrollable=False,
                                         withhold=False, row_factory=dict_row) as cursor:
                self._dispatched = True
                cursor.execute(_MEMBERS, (channel_ids,))
                stream = _ChannelMembershipCascadeBatches(self, cursor)
                try:
                    yield stream
                    self._check()
                    self._check_budget()
                    if not stream._ended:
                        raise DatabaseError('database_cursor_incomplete')
                finally:
                    stream._active = False
        except psycopg.Error as error:
            self._failed = True
            raise _mapped(error, unknown=self._dispatched, allowed=self._allowed) from None
        except BaseException:
            self._failed = True
            raise
        finally:
            if owned:
                self._scheduled_cursor_active = False

    @contextmanager
    def inventory_absence_changes(self, arguments):
        """Private page protocol: freeze OLD, execute one UPDATE, then stream50."""
        self._check()
        owned = False
        try:
            from ..inventory.membership_absence_effects import _KEYS, _statements
            if (self._read_only or self._rpc_name is not None or self._scheduled_cursor_active
                    or self._deadline is None or not isinstance(arguments, Mapping)
                    or set(arguments) != _KEYS):
                raise DatabaseError('database_cursor_capability_invalid')
            parameters = dict(arguments)
            lock_statement, old_statement, update_statement = _statements()
            self._scheduled_cursor_active = True
            owned = True
            self._admission(None)
            self._check_budget(configure=True)
            self._admission(None)
            expected = 0
            with self._connection.cursor(name='lic_inventory_absence_locks', scrollable=False,
                                         withhold=False, row_factory=dict_row) as cursor:
                self._dispatched = True
                cursor.execute(lock_statement, parameters)
                locked = _InventoryAbsenceBatches(self, cursor)
                try:
                    for rows in locked:
                        expected += len(rows)
                finally:
                    locked._active = False
            self._admission(None)
            self._check_budget(configure=True)
            self._admission(None)
            with self._connection.cursor(name='lic_inventory_absence_old', scrollable=False,
                                         withhold=False, row_factory=dict_row) as cursor:
                # DECLARE fixes the insensitive snapshot before our own UPDATE,
                # even though its first FETCH occurs only after that DML ends.
                cursor.execute(old_statement, parameters)
                updated = self.execute(update_statement, parameters)
                if updated != expected:
                    raise DatabaseError('database_result_shape_invalid')
                stream = _InventoryAbsenceBatches(self, cursor)
                try:
                    yield stream
                    self._check()
                    self._check_budget()
                    if not stream._ended or stream._count != updated:
                        raise DatabaseError('database_cursor_incomplete')
                finally:
                    stream._active = False
        except psycopg.Error as error:
            self._failed = True
            raise _mapped(error, unknown=self._dispatched, allowed=self._allowed) from None
        except BaseException:
            self._failed = True
            raise
        finally:
            if owned:
                self._scheduled_cursor_active = False



class _InventorySourceRows:
    """A fixed read cursor; EOF and close never become a partial success."""
    def __init__(self, transaction, cursor):
        self._transaction, self._cursor = transaction, cursor
        self._active, self._ended = True, False

    def fetch(self, size=1000):
        tx = self._transaction
        try:
            tx._check()
            if not self._active:
                raise DatabaseError('database_cursor_owner_invalid')
            _integer(size, 1, 1000, 'database_result_limit_invalid')
            tx._check_budget()
            if self._ended:
                return []
            tx._admission(None)
            tx._check_budget(configure=True)
            tx._admission(None)
            rows = self._cursor.fetchmany(size)
            if not isinstance(rows, list) or len(rows) > size:
                raise DatabaseError('database_result_limit_exceeded')
            if not rows:
                self._ended = True
            return rows
        except psycopg.Error as error:
            tx._failed = True
            raise _mapped(error, unknown=False, allowed=tx._allowed) from None
        except BaseException:
            tx._failed = True
            raise


class _ScheduledVideoCandidates:
    """Private bounded iterator; even buffered reads keep transaction ownership."""
    def __init__(self,transaction,cursor,max_rows):
        self._transaction,self._cursor=transaction,cursor
        self._remaining=max_rows
        self._buffer=deque()
        self._size=10
        self._active=True
        self._ended=False

    def __iter__(self):return self

    def __next__(self):
        rows=self.locked_prefix(1)
        if not rows:raise StopIteration
        return rows[0]

    def locked_prefix(self, limit):
        """Take only the current FETCH buffer, never prefetch a second portion.

        A short prefix leaves its already locked tail untouched. Buffer sizes,
        ownership and the native selection snapshot remain transport concerns.
        """
        tx=self._transaction
        try:
            tx._check()
            if not self._active:raise DatabaseError('database_cursor_owner_invalid')
            if type(limit) is not int or not 1<=limit<=50:
                raise DatabaseError('database_cursor_input_invalid')
            tx._check_budget()
            if self._ended:return []
            if not self._buffer:
                tx._admission(None)
                tx._check_budget(configure=True)
                if tx._deadline is not None:tx._admission(None)
                rows=self._cursor.fetchmany(self._size)
                self._size=50
                self._remaining-=len(rows)
                if self._remaining<0:raise DatabaseError('database_result_limit_exceeded')
                if not rows:
                    self._ended=True
                    return []
                self._buffer.extend(rows)
            return [self._buffer.popleft() for _ in range(min(limit,len(self._buffer)))]
        except psycopg.Error as error:
            tx._failed=True
            raise _mapped(error,unknown=tx._dispatched,allowed=tx._allowed) from None
        except BaseException:
            tx._failed=True
            raise


class _QuotaRetryChildrenRows:
    """One native row per fetch; fixed snapshot and transaction budget persist."""
    def __init__(self, transaction, cursor):
        self._transaction, self._cursor = transaction, cursor
        self._active, self._ended = True, False

    def __iter__(self):
        return self

    def __next__(self):
        tx = self._transaction
        try:
            tx._check()
            if not self._active:
                raise DatabaseError('database_cursor_owner_invalid')
            tx._check_budget()
            if self._ended:
                raise StopIteration
            tx._admission(tx._rpc_name)
            tx._check_budget(configure=True)
            tx._admission(tx._rpc_name)
            rows = self._cursor.fetchmany(1)
            if len(rows) > 1:
                raise DatabaseError('database_result_limit_exceeded')
            if not rows:
                self._ended = True
                raise StopIteration
            return rows[0]
        except StopIteration:
            raise
        except psycopg.Error as error:
            tx._failed = True
            raise _mapped(error, unknown=tx._dispatched, allowed=tx._allowed) from None
        except BaseException:
            tx._failed = True
            raise


class _SyncRunRecoveryRows:
    """No prefetch: retain the fixed statement snapshot and bound memory to one row."""
    def __init__(self,transaction,cursor):
        self._transaction,self._cursor=transaction,cursor
        self._active,self._ended=True,False

    def __iter__(self):return self

    def __next__(self):
        tx=self._transaction
        try:
            tx._check()
            if not self._active:raise DatabaseError('database_cursor_owner_invalid')
            tx._check_budget()
            if self._ended:raise StopIteration
            tx._admission(None)
            tx._check_budget(configure=True)
            tx._admission(None)
            rows=self._cursor.fetchmany(1)
            if len(rows)>1:raise DatabaseError('database_result_limit_exceeded')
            if not rows:
                self._ended=True
                raise StopIteration
            return rows[0]
        except StopIteration:
            raise
        except psycopg.Error as error:
            tx._failed=True
            raise _mapped(error,unknown=tx._dispatched,allowed=tx._allowed) from None
        except BaseException:
            tx._failed=True
            raise


class PostgresDatabase:
    """Explicit process lifetime; no connection or thread is opened on import."""
    def __init__(self, config: PostgresConfig, *, admission: Callable | None = None,
                 pool_factory: Callable | None = None, cadence_snapshot=None):
        self.config = config
        self._cadence_snapshot = cadence_snapshot
        self._admission = admission if admission is not None else _runtime_admission
        self._pool_factory = pool_factory if pool_factory is not None else _new_pool
        self._pool = None
        self._pid, self._closed = os.getpid(), False
        self._lifecycle = threading.Lock()
        self._local = threading.local()

    @property
    def cadence_snapshot(self):
        self._check_process()
        return self._cadence_snapshot

    def _check_process(self):
        if self._pid != os.getpid():
            raise DatabaseError('database_process_owner_invalid')
        if self._closed:
            raise DatabaseUnavailable('database_pool_closed', reason='pool_closed')

    def open(self):
        self._check_process()
        with self._lifecycle:
            self._check_process()
            if self._pool is None:
                pool = None
                try:
                    pool = self._pool_factory(self.config)
                    pool.open(wait=False)
                except Exception:
                    if pool is not None:
                        try:
                            pool.close()
                        except Exception:
                            pass
                    raise DatabaseUnavailable('database_pool_unavailable') from None
                self._pool = pool
        return self

    def close(self):
        if self._pid != os.getpid():
            raise DatabaseError('database_process_owner_invalid')
        with self._lifecycle:
            if self._closed:
                return
            self._closed = True
            if self._pool is not None:
                self._pool.close(timeout=5)

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self, operation: Operation) -> Iterator[Transaction]:
        # No RPC capability can be supplied with arbitrary repository SQL.
        with self._transaction(operation, rpc_name=None, allowed=operation.error_codes) as tx:
            yield tx

    @contextmanager
    def _transaction(self, operation: Operation, *, rpc_name, allowed):
        # UI diagnostics observe this existing owner; collection and writes
        # retain their original unobserved path. Names are allowlisted by the
        # observer, not trusted merely because Operation accepts a token.
        observed = (
            isinstance(operation, Operation) and operation.read_only
            and is_http_read() and ui_read_trace.is_active()
        )
        with (
            ui_read_trace.database_scope(operation.name, phase='db_transaction')
            if observed else nullcontext()
        ):
            with self._transaction_body(
                operation, rpc_name=rpc_name, allowed=allowed, observed=observed,
            ) as tx:
                yield tx

    @contextmanager
    def _transaction_body(self, operation: Operation, *, rpc_name, allowed, observed):
        self._check_process()
        if not isinstance(operation, Operation):
            raise ValueError('database_operation_invalid')
        if http_request_method() in {'GET', 'HEAD'} and not operation.read_only:
            raise DatabaseError('database_http_write_forbidden')
        if self._pool is None:
            raise DatabaseUnavailable('database_pool_not_open')
        if getattr(self._local, 'in_transaction', False):
            raise DatabaseError('database_transaction_nested')
        self._admission(rpc_name)
        self._local.in_transaction = True
        tx = None
        try:
            with ExitStack() as connection_owner:
                # Only acquiring the connection is pool wait. Its release stays
                # owned by the same surrounding context, including on failure.
                with (
                    ui_read_trace.database_scope(operation.name, phase='db_pool_wait')
                    if observed else nullcontext()
                ):
                    connection = connection_owner.enter_context(
                        self._pool.connection(timeout=self.config.acquire_timeout),
                    )
                # Includes admission, transaction setup, caller work and commit/
                # rollback. This is deliberately not labelled pure SQL time.
                with (
                    ui_read_trace.database_scope(operation.name, phase='db_execution')
                    if observed else nullcontext()
                ):
                    self._admission(rpc_name)  # the gate may close during a pool wait
                    with connection.transaction():
                        # All settings are local to this transaction and cannot
                        # leak a role/timeout to the next owner of the connection.
                        if operation.read_only and is_http_read():
                            connection.execute('SET LOCAL ROLE lic_reader')
                        else:
                            connection.execute('SET LOCAL ROLE service_role')
                        if operation.repeatable_read:
                            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                        if operation.read_only:
                            connection.execute('SET TRANSACTION READ ONLY')
                        if operation.disable_jit:
                            connection.execute('SET LOCAL jit = off')
                        connection.execute(
                            "SELECT set_config('search_path', 'pg_catalog, public, extensions, pg_temp', true), "
                            "set_config('TimeZone', 'UTC', true), "
                            "set_config('statement_timeout', %s, true), "
                            "set_config('lock_timeout', %s, true), "
                            "set_config('idle_in_transaction_session_timeout', '60s', true)",
                            (f'{min(operation.statement_timeout_ms, self.config.statement_timeout_ceiling_ms)}ms',
                             f'{operation.lock_timeout_ms}ms'),
                        )
                        tx = Transaction(connection, self._admission, rpc_name=rpc_name,
                                         allowed=allowed, read_only=operation.read_only,
                                         total_timeout_ms=operation.total_timeout_ms,
                                         statement_timeout_ms=min(operation.statement_timeout_ms, self.config.statement_timeout_ceiling_ms),
                                         cache_statement_timeout=operation.cache_statement_timeout,
                                         cadence_snapshot=self._cadence_snapshot)
                        yield tx
                        if tx._failed:
                            raise DatabaseError('database_transaction_aborted')
                        tx._check_budget(configure=True)
        except (psycopg.Error, PoolTimeout, TooManyRequests, PoolClosed) as error:
            unknown = bool(tx and tx._dispatched and not operation.read_only)
            raise _mapped(error, unknown=unknown, allowed=allowed) from None
        finally:
            if tx is not None:
                tx._active = False
            self._local.in_transaction = False

    def abort_hot_collection_claims(self, *, video_claims, playlist_claims, operator_claims):
        """Release only supplied capabilities under the fixed HOT control gate."""
        from ..hot.hot_collection_termination import abort_hot_collection_claims, ABORT_ERRORS
        operation = Operation('abort_hot_collection_claims', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ABORT_ERRORS)
        with self._transaction(operation, rpc_name='abort_hot_collection_claims', allowed=ABORT_ERRORS) as tx:
            return abort_hot_collection_claims(tx, video_claims, playlist_claims, operator_claims)

    def drain_hot_collection_claims(self, *, video_claims, playlist_claims, operator_claims):
        """Release exact known tokens and confirm absence, never lend SQL access."""
        from ..hot.hot_collection_termination import drain_hot_collection_claims
        from .operation_contracts import get_operation
        operation = get_operation('drain_hot_collection_claims')
        with self._transaction(operation, rpc_name=operation.name, allowed=operation.error_codes) as tx:
            return drain_hot_collection_claims(tx, video_claims, playlist_claims, operator_claims)

    def drain_hot_channel_metadata(self, *, markers):
        """Clear only exact acknowledged metadata markers; never lend SQL access."""
        from ..hot.hot_metadata_termination import drain_hot_channel_metadata, validate
        from .operation_contracts import get_operation
        markers = validate(markers)
        operation = get_operation('drain_hot_channel_metadata')
        with self._transaction(operation, rpc_name=operation.name, allowed=operation.error_codes) as tx:
            return drain_hot_channel_metadata(tx, markers)

    def abort_hot_reconnect_claim(self, *, identity, expected_revision, lease_token):
        """Cancel only one supplied pending lease; no generic SQL or callbacks."""
        from ..hot.hot_reconnect_termination import abort_hot_reconnect_claim, validate, ERRORS
        values = validate(identity, expected_revision, lease_token)
        operation = Operation('abort_hot_reconnect_claim', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='abort_hot_reconnect_claim', allowed=ERRORS) as tx:
            return abort_hot_reconnect_claim(tx, *values)

    def interrupt_hot_sync_run(self, *, run_id, started_at):
        """Interrupt one exact running identity under the fixed HOT control gate."""
        from ..hot.hot_collection_termination import interrupt_hot_sync_run, INTERRUPT_ERRORS
        operation = Operation('interrupt_hot_sync_run', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=INTERRUPT_ERRORS)
        with self._transaction(operation, rpc_name='interrupt_hot_sync_run', allowed=INTERRUPT_ERRORS) as tx:
            return interrupt_hot_sync_run(tx, run_id, started_at)

    def cleanup_deleted_saved_objects(self, *, target, after_id=None, limit=100):
        """Run one fixed maintenance protocol; callers cannot lend it SQL or code.

        Only this internal dispatch lends the existing cleanup admission label.
        The domain helper owns its finite statements and row-local savepoints;
        ordinary transaction names cannot acquire maintenance permission.
        """
        from ..retention.retention_queries import cleanup_deleted_saved_objects, SAVED_ERRORS
        operation = Operation('cleanup_deleted_saved_objects', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=SAVED_ERRORS)
        with self._transaction(operation, rpc_name='cleanup_deleted_saved_objects', allowed=SAVED_ERRORS) as tx:
            return cleanup_deleted_saved_objects(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_evidence(self, *, target, after_id=None, limit=100):
        """Fixed evidence-retention action under the existing cleanup permission."""
        from ..retention.evidence_retention import cleanup_hot_evidence, ERRORS
        operation = Operation('cleanup_hot_evidence', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='cleanup_hot_evidence', allowed=ERRORS) as tx:
            return cleanup_hot_evidence(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_category_reference(self, *, target, after_id=None, limit=100):
        """Fixed singleton cleanup under the maintenance admission capability."""
        from ..retention.category_reference_retention import cleanup_hot_category_reference, ERRORS
        operation = Operation('cleanup_hot_category_reference', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='cleanup_hot_category_reference', allowed=ERRORS) as tx:
            return cleanup_hot_category_reference(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_current_source(self, *, target, after_id=None, limit=100):
        """Finite source-payload cleanup under its existing maintenance permission."""
        from ..retention.current_source_retention import cleanup_hot_current_source, ERRORS
        operation = Operation('cleanup_hot_current_source', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='cleanup_hot_current_source', allowed=ERRORS) as tx:
            return cleanup_hot_current_source(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_inventory_copies(self, *, target, after_id=None, limit=100):
        """Finite scan/source/receipt cleanup under the existing maintenance gate."""
        from ..retention.inventory_copies_retention import cleanup_hot_inventory_copies, ERRORS
        operation = Operation('cleanup_hot_inventory_copies', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='cleanup_hot_inventory_copies', allowed=ERRORS) as tx:
            return cleanup_hot_inventory_copies(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_operational_copies(self, *, target, after_id=None, limit=100):
        """Fixed operational-copy cleanup under its existing maintenance gate."""
        from ..retention.operational_retention import cleanup_hot_operational_copies, ERRORS
        operation = Operation('cleanup_hot_operational_copies', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='cleanup_hot_operational_copies', allowed=ERRORS) as tx:
            return cleanup_hot_operational_copies(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_deleted_channels(self, *, target, after_id=None, limit=100):
        """Fixed retention action under the existing maintenance permission."""
        from ..retention.deleted_channel_retention import cleanup_hot_deleted_channels, ERRORS
        operation = Operation('cleanup_hot_deleted_channels', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        from ..readiness.deleted_channel_retention_capabilities import READINESS_SQL, validate_readiness
        with self._transaction(operation, rpc_name='cleanup_hot_deleted_channels', allowed=ERRORS) as tx:
            validate_readiness(tx.fetch_one(READINESS_SQL))
            return cleanup_hot_deleted_channels(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_inventory(self, *, target, after_id=None, limit=100):
        """Fixed retention action under the existing maintenance permission."""
        from ..retention.inventory_retention import cleanup_hot_inventory, ERRORS
        operation = Operation('cleanup_hot_inventory', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='cleanup_hot_inventory', allowed=ERRORS) as tx:
            return cleanup_hot_inventory(tx, target=target, after_id=after_id, limit=limit)

    def cleanup_hot_priority(self, *, references):
        """Exact retained blockers under the same bounded maintenance owner."""
        from ..retention.priority_retention import cleanup_hot_priority, ERRORS
        operation = Operation('cleanup_hot_priority', statement_timeout_ms=5_000,
                              lock_timeout_ms=250, total_timeout_ms=5_000, error_codes=ERRORS)
        with self._transaction(operation, rpc_name='cleanup_hot_priority', allowed=ERRORS) as tx:
            return cleanup_hot_priority(tx, references=references)

    def read_update_copy_window(self):
        """Fixed acquisition-clock snapshot; no caller SQL or data payload."""
        from ..hot.update_copy_sources import UPDATE_COPY_WINDOW_SQL
        from .sql_values import application_value
        from update_copy_protocol import WINDOW_READ_SECONDS
        # One indexed source-age observation per update. Its many branches make
        # JIT compilation more expensive than execution; disable it only here.
        operation = Operation('maintenance_update_copy_window', read_only=True, repeatable_read=True,
                              statement_timeout_ms=WINDOW_READ_SECONDS * 1000,
                              total_timeout_ms=WINDOW_READ_SECONDS * 1000, disable_jit=True)
        with self._transaction(operation, rpc_name='get_update_copy_window', allowed=frozenset()) as tx:
            row = tx.fetch_one(UPDATE_COPY_WINDOW_SQL)
            if row is None:
                raise DatabaseError('database_result_missing')
            return application_value(row)

    def read_startup_expiry(self, *, cutoff, strict_entry=False):
        """One fixed read-only source-expiry probe; never accepts caller SQL."""
        from hot_storage import utc_instant
        from ..hot.startup_expiry import STARTUP_EXPIRY_SQL
        from .sql_values import application_value
        try:
            cutoff = utc_instant(cutoff)
            if type(strict_entry) is not bool:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise DatabaseError('invalid_startup_cutoff') from None
        operation = Operation('maintenance_startup_expiry', read_only=True,
                              statement_timeout_ms=2_000, total_timeout_ms=2_000)
        with self._transaction(operation, rpc_name='get_hot_startup_expiry', allowed=frozenset()) as tx:
            row = tx.fetch_one(STARTUP_EXPIRY_SQL, (cutoff, strict_entry))
            if row is None:
                raise DatabaseError('database_result_missing')
            return application_value(row)

    def read_maintenance_capability(self, name: str):
        """Fixed catalog reads; never a caller-supplied statement or callback.

        They belong to the existing HOT-contract permission. Read-only mode and
        the maintenance pool timeout ceiling still apply during blocked startup.
        Ordinary SQL cannot acquire this capability from an operation name.
        """
        if type(name) is not str or name not in {'schema_release', 'inventory_switch', 'hot_runtime', 'user_work_restore', 'video_failure', 'evidence_retention', 'inventory_receipts', 'operational_retention', 'deleted_channels', 'collection_completion', 'collection_cadence'}:
            raise ValueError('database_maintenance_capability_invalid')
        from ..readiness.capabilities import MAINTENANCE_CAPABILITY_QUERIES
        from .sql_values import application_value
        operation = Operation('maintenance_' + name, read_only=True, statement_timeout_ms=8_000)
        with self._transaction(operation, rpc_name='get_hot_runtime_contract', allowed=frozenset()) as tx:
            row = tx.fetch_one(MAINTENANCE_CAPABILITY_QUERIES[name])
            if row is None:
                raise DatabaseError('database_result_missing')
            return application_value(row)


    def read_rows(self, operation, statement, parameters=(), *, max_rows):
        from .sql_values import application_value
        with self.transaction(Operation(operation, read_only=True)) as tx:
            return application_value(tx.fetch_all(statement, parameters, max_rows=max_rows))

    def read_row(self, operation, statement, parameters=()):
        from .sql_values import application_value
        with self.transaction(Operation(operation, read_only=True)) as tx:
            return application_value(tx.fetch_one(statement, parameters))

    def write(self, operation, statement, parameters=(), *, error_codes=frozenset()):
        with self.transaction(Operation(operation, error_codes=error_codes)) as tx:
            return tx.execute(statement, parameters)

    def write_rows(self, operation, statement, parameters=(), *, max_rows, error_codes=frozenset()):
        from .sql_values import application_value
        with self.transaction(Operation(operation, error_codes=error_codes)) as tx:
            return application_value(tx.returning_all(statement, parameters, max_rows=max_rows))


class _ChannelMembershipCascadeBatches:
    """One fixed50-row buffer; no overall truncation of the native snapshot."""
    def __init__(self, transaction, cursor):
        self._transaction, self._cursor = transaction, cursor
        self._active, self._ended = True, False

    def __iter__(self):
        return self

    def __next__(self):
        tx = self._transaction
        try:
            tx._check()
            if not self._active:
                raise DatabaseError('database_cursor_owner_invalid')
            tx._check_budget()
            if self._ended:
                raise StopIteration
            tx._admission(tx._rpc_name)
            tx._check_budget(configure=True)
            tx._admission(tx._rpc_name)
            rows = self._cursor.fetchmany(50)
            if len(rows) > 50:
                raise DatabaseError('database_result_limit_exceeded')
            if not rows:
                self._ended = True
                raise StopIteration
            return rows
        except StopIteration:
            raise
        except psycopg.Error as error:
            tx._failed = True
            raise _mapped(error, unknown=tx._dispatched, allowed=tx._allowed) from None
        except BaseException:
            tx._failed = True
            raise


class _InventoryAbsenceBatches(_ChannelMembershipCascadeBatches):
    """Fixed native OLD buffers; the owning context verifies complete consumption."""
    def __init__(self, transaction, cursor):
        super().__init__(transaction, cursor)
        self._count = 0

    def __next__(self):
        rows = super().__next__()
        self._count += len(rows)
        return rows


