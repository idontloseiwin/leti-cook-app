"""Sync-run UPDATE owners sharing the caller transaction and native assignments.

Only the supplied writer transaction is borrowed. PostgreSQL owns the locked
full proposed row, assignment casts and native clocks. Stored JSONB numbers
cross Python solely as text; normalization precedes persisted projection.
INSERT/defaults and multirow recovery use their separate transaction owners.
"""
from collections.abc import Mapping

from psycopg import sql
from psycopg.types.json import Jsonb

from ..base.postgres import DatabaseError, DatabaseServerError
from ..base.sql_values import identifier
from .sync_run_summary import normalize_sync_run_summary_text, persisted_summary_text

# Exact schema23 physical composite order. These are database types, not a
# Python input validator; the normalizer runs before final table constraints.
COLUMN_TYPES = (
    ('id', 'bigint'), ('run_type', 'text'), ('status', 'text'),
    ('started_at', 'timestamptz'), ('finished_at', 'timestamptz'),
    ('duration_seconds', 'double precision'), ('errors', 'integer'),
    ('metadata_processed', 'integer'), ('metadata_updated', 'integer'),
    ('video_count_changed', 'integer'), ('discovery_processed', 'integer'),
    ('new_videos', 'integer'), ('new_video_stats_updated', 'integer'),
    ('regular_stats_requested', 'integer'), ('regular_stats_updated', 'integer'),
    ('stats_updated', 'integer'), ('unavailable_marked', 'integer'),
    ('missing', 'integer'), ('summary_json', 'jsonb'), ('created_at', 'timestamptz'),
)
_TYPES = dict(COLUMN_TYPES)
_HOT_COLUMNS = ('status', 'finished_at', 'duration_seconds', 'errors', 'summary_json')
_RUNNING = "run.id=%s::bigint AND run.started_at=%s::timestamptz AND run.status='running' AND run.finished_at IS NULL"
_HOT_LOCK = "SELECT to_jsonb(run)::text AS columns_text,run.summary_json::text AS summary_text FROM public.sync_runs run WHERE " + _RUNNING + " FOR UPDATE OF run"
_SCRUB_LOCK = "SELECT to_jsonb(run)::text AS columns_text,run.summary_json::text AS summary_text FROM public.sync_runs run WHERE run.id=%s::bigint FOR UPDATE OF run NOWAIT"
_HOT_FLAGS = '{"completion_outcome":"error","incomplete":true,"infrastructure_error":true,"stopped":true}'


def _native_row(expressions):
    return sql.SQL('ROW({})::public.sync_runs').format(sql.SQL(',').join(
        expressions.get(name, sql.SQL('run.{}').format(identifier(name))) for name, _ in COLUMN_TYPES))


def _addressed_proposal(values):
    # The supplied expressions keep the original parameter order and native
    # destination casts. Do not route ordinary supplied values through JSON.
    supplied = sql.SQL(',').join(sql.SQL('%s::{} AS {}').format(sql.SQL(_TYPES.get(name, 'text')), identifier(name))
                               for name in values)
    expressions = {name:sql.SQL('(locked.current).{}').format(identifier(name)) for name, _ in COLUMN_TYPES}
    expressions.update({name:sql.SQL('locked.{}').format(identifier(name)) for name in values})
    row = _native_row(expressions)
    unknown = [name for name in values if name not in _TYPES]
    # Parse unknown identifiers natively, even for an absent target; binding all
    # original values also retains the client's normal adaptation boundary.
    checks = sql.SQL('').join(sql.SQL(',run.{}').format(identifier(name)) for name in unknown)
    # Target-list casts occur only for matching rows, as in UPDATE. A separate
    # one-row supplied CTE could evaluate a cast even when the target is absent.
    return sql.SQL("""WITH locked AS MATERIALIZED (
 SELECT run AS current,{}{} FROM public.sync_runs run
 WHERE run.id=%s::bigint FOR {} OF run), proposed AS (
 SELECT {} AS value FROM locked)
 SELECT to_jsonb(value)::text AS columns_text,(value).summary_json::text AS summary_text FROM proposed""").format(supplied, checks, sql.SQL('UPDATE' if 'id' in values else 'NO KEY UPDATE'), row)


def _update_statement(columns, predicate):
    # Both identifiers and predicate come only from this module's finite owners.
    terms = sql.SQL(',').join(sql.SQL('{}={}').format(identifier(name),
        sql.SQL('%s::jsonb') if name == 'summary_json' else sql.SQL('proposed.{}').format(identifier(name)))
        for name in columns)
    return sql.SQL("UPDATE public.sync_runs run SET {} FROM jsonb_populate_record(NULL::public.sync_runs,%s::jsonb) proposed WHERE ").format(terms) + sql.SQL(predicate)


def _proposal(value):
    if (not isinstance(value, Mapping) or set(value) != {'columns_text', 'summary_text'}
            or type(value['columns_text']) is not str
            or (value['summary_text'] is not None and type(value['summary_text']) is not str)):
        raise DatabaseError('database_result_shape_invalid')
    return value


def _final_summary(transaction, proposed):
    proposed = _proposal(proposed)
    normalized = normalize_sync_run_summary_text(
        proposed['summary_text'], proposed['columns_text'], transaction=transaction)
    return persisted_summary_text(normalized)


def update_sync_run(transaction, run_id, values):
    """Addressed UPDATE, including counter-only and missing-row cases.

    The public repository still owns self-hosted started_at stripping. Empty
    assignments issue no SQL. Unknown valid identifiers preserve schema23's
    sanitized42703 rejection before a row can be locked. No INSERT fallback.
    """
    values = dict(values)
    if not values:
        return 0
    if len(values) > 256:
        raise ValueError('database_assignments_invalid')
    with transaction.borrow():
        for name in values:
            identifier(name)  # Preserve the existing identifier boundary.
        parameters = tuple(Jsonb(value) if name == 'summary_json' and value is not None else value
                           for name, value in values.items())
        transaction.validate_operational_assignments('sync_update', values, identity=run_id)
        proposed = transaction.fetch_one(_addressed_proposal(values), parameters+(run_id,))
        if any(name not in _TYPES for name in values):
            # Schema drift cannot silently introduce a new writable column.
            raise DatabaseServerError('database_operation_failed', code='42703')
        if proposed is None:
            return 0
        summary = _final_summary(transaction, proposed)
        columns = tuple(values) if 'summary_json' in values else (*values, 'summary_json')
        return transaction.execute(_update_statement(columns, 'run.id=%s::bigint'),
                                   (summary, proposed['columns_text'], run_id))


_HOT_PROPOSAL = sql.SQL("""WITH clock AS (SELECT %s::timestamptz AS at), proposed AS (
 SELECT {} AS value FROM jsonb_populate_record(NULL::public.sync_runs,%s::jsonb) run CROSS JOIN clock)
 SELECT to_jsonb(value)::text AS columns_text,(value).summary_json::text AS summary_text FROM proposed""").format(
    _native_row({
        'status':sql.SQL("'error'::text"), 'finished_at':sql.SQL('clock.at'),
        'duration_seconds':sql.SQL('greatest(0,extract(epoch FROM clock.at-run.started_at))'),
        'errors':sql.SQL('greatest(run.errors,1)'),
        'summary_json':sql.SQL('%s::jsonb || {}::jsonb').format(sql.Literal(_HOT_FLAGS)),
    }))
_HOT_UPDATE = _update_statement(_HOT_COLUMNS, _RUNNING)


def interrupt_sync_run(transaction, *, run_id, started_at, at):
    """Final HOT stage after its owner's native input/clock validation.

    at is the already captured native clock, not a new sample. The exact running
    predicate locks and rechecks before the current summary is preprojected.
    Domain argument validation/receipt and closed admission remain caller-owned.
    """
    with transaction.borrow():
        current = transaction.fetch_one(_HOT_LOCK, (run_id, started_at))
        if current is None:
            return 0
        current = _proposal(current)
        projected = persisted_summary_text(current['summary_text'])
        # Parameter order follows SQL: clock, projected summary, then native row.
        proposed = transaction.fetch_one(_HOT_PROPOSAL, (at, projected, current['columns_text']))
        summary = _final_summary(transaction, proposed)
        return transaction.execute(_HOT_UPDATE, (summary, proposed['columns_text'], run_id, started_at))


_SCRUB_PROPOSAL = """SELECT %s::jsonb::text AS summary_text,
 (%s::jsonb || jsonb_build_object('summary_json',%s::jsonb))::text AS columns_text,
 %s::jsonb IS DISTINCT FROM %s::jsonb AS changed"""
_SCRUB_UPDATE = """UPDATE public.sync_runs SET summary_json=%s::jsonb
 WHERE id=%s::bigint AND summary_json IS DISTINCT FROM %s::jsonb"""


def scrub_sync_run(transaction, *, run_id):
    """Scrub after the owner's existing source/page/expiry decisions.

    Keep NOWAIT and re-read the current row; do not re-evaluate expiry or select
    another candidate. The UPDATE predicate compares the raw locked summary to
    its preprojection, exactly as before normalization triggers. Comparing the
    final normalized value instead could incorrectly remove an affected row.
    """
    with transaction.borrow():
        current = transaction.fetch_one(_SCRUB_LOCK, (run_id,))
        if current is None:
            return 0
        current = _proposal(current)
        projected = persisted_summary_text(current['summary_text'])
        proposed = transaction.fetch_one(_SCRUB_PROPOSAL, (
            projected, current['columns_text'], projected, current['summary_text'], projected))
        if not isinstance(proposed, Mapping) or set(proposed) != {'columns_text', 'summary_text', 'changed'} or type(proposed['changed']) is not bool:
            raise DatabaseError('database_result_shape_invalid')
        if not proposed['changed']:
            return 0
        normalized = _final_summary(transaction, {key:proposed[key] for key in ('columns_text', 'summary_text')})
        return transaction.execute(_SCRUB_UPDATE, (normalized, run_id, projected))
