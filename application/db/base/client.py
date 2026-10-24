"""One explicit, process-owned direct PostgreSQL pool and reviewed read recovery."""
from __future__ import annotations

from contextlib import contextmanager
import os
import time
from typing import Any, Callable

from telemetry import emit_event
from .postgres import PostgresConfig, PostgresDatabase, DatabaseUnavailable

# Kept as an internal type alias while the repository interface is migrated.
# It has no table/RPC builder, HTTP client or transport fallback.
DatabaseGateway = PostgresDatabase

READ_RECOVERY_OPERATIONS = frozenset({
    "channel_explorer.inventory_page",
    "channel_explorer.load_channel",
    "channel_explorer.load_inventory_state",
    "channel_explorer.load_type_resolutions",
    "channel_explorer.load_videos_by_ids",
    "frontend.load_channel_rows",
    "frontend.load_channel",
    "frontend.load_recent_long_inventory_page",
    "channel_counter_dynamics.load_channel",
    "channel_counter_dynamics.load_latest_snapshot",
    "channel_counter_dynamics.load_metric_baseline",
})


def load_database_config(environ=None):
    """No connection, credential fallback or environment mutation."""
    values = os.environ if environ is None else environ
    if values.get('LIC_DATABASE_TRANSPORT') != 'postgres':
        raise ValueError('direct_postgres_not_selected')
    return PostgresConfig(values.get('LIC_DATABASE_DSN'))


def create_database_gateway(config=None, *, admission=None, pool_factory=None, cadence_snapshot=None):
    """Construct an unopened pool; the process owner must open and close it."""
    return PostgresDatabase(
        load_database_config() if config is None else config,
        admission=admission, pool_factory=pool_factory, cadence_snapshot=cadence_snapshot,
    )


@contextmanager
def fresh_database_gateway(config=None, *, admission=None, pool_factory=None, cadence_snapshot=None):
    """Owned pool for a standalone process/test, never a retry of a write."""
    database = create_database_gateway(config, admission=admission, pool_factory=pool_factory,
                                       cadence_snapshot=cadence_snapshot)
    try:
        database.open()
        yield database
    finally:
        database.close()


def _emit_read_recovery(operation, phase, started, error):
    try:
        emit_event(
            'db_read_recovery', 'backend', 'postgres', operation,
            phase=phase, status='ok' if phase == 'recovered' else 'error',
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            detail='database_connection_failed',
        )
    except Exception:
        pass


def execute_recoverable_read(database: Any, read: Callable[[Any], Any], *, operation: str):
    """Retry one named read after connection loss, using the existing bounded pool.

    Each repository call owns a new transaction. Psycopg discards the failed
    connection; no shared pool is closed and no write or pool-pressure retry is
    admitted here. An unregistered operation is always executed exactly once.
    """
    started = time.monotonic()
    try:
        return read(database)
    except DatabaseUnavailable as error:
        if (operation not in READ_RECOVERY_OPERATIONS
                or error.message != 'database_connection_failed'
                or error.outcome_unknown):
            raise
        _emit_read_recovery(operation, 'initial_failure', started, error)
        try:
            result = read(database)
        except Exception as retry_error:
            _emit_read_recovery(operation, 'exhausted', started, retry_error)
            raise
        _emit_read_recovery(operation, 'recovered', started, error)
        return result
