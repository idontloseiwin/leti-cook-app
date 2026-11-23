"""Initial-statistics settlement and completed-hydration failure CAS.

Both protocols lock the exact job, keep native argument/counter/JSONB
semantics, and fence the captured channel scope after mutation. Native
source-scope guards remain enabled. A caller may borrow its transaction; no
write retry or acknowledgement precedes COMMIT.
"""
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from collection_policy import youtube_quota_reset_at
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseError, DatabaseServerError, Operation

_STATS_INPUT = """
 SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS lease_token,
 %s::integer AS reserved_units,%s::integer AS observed_quota_units,%s::text AS status,
 %s::jsonb IS NULL AS summary_is_null
"""
_CAS_INPUT = """
 SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,%s::text AS channel_id,
 %s::integer AS error_count,%s::bigint AS expected_epoch
"""
_JOB = "SELECT to_jsonb(job) AS value FROM public.operator_jobs job WHERE id=%s::uuid FOR UPDATE"
_COUNTER = "SELECT %s::integer + %s::integer AS error_count,now() AS as_of"
_SETTLE = """
 UPDATE public.operator_jobs AS job SET status=%(next_status)s,error_count=%(error_count)s::integer,
 retry_not_before=CASE WHEN %(quota_deferred)s::boolean AND %(next_status)s='queued'
     THEN greatest(now()+interval '1 hour',%(reset_at)s::timestamptz)
     WHEN %(rate_limited)s::boolean AND %(next_status)s='queued' THEN now()+interval '1 minute' END,
 quota_units_reserved=greatest(job.quota_units_reserved-%(reserved_units)s::integer+%(observed_quota_units)s::integer,0),
 quota_units_observed_estimate=job.quota_units_observed_estimate+%(observed_quota_units)s::integer,
 summary_json=coalesce(job.summary_json,'{}'::jsonb)||coalesce(%(summary)s::jsonb,'{}'::jsonb),
 last_error_code=%(error_code)s::text,last_error_summary=%(error_summary)s::text,
 finished_at=CASE WHEN %(next_status)s IN ('completed','error') THEN now() ELSE NULL END,
 heartbeat_at=now(),updated_at=now(),lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE job.id=%(job_id)s::uuid RETURNING to_jsonb(job) AS value
"""
_MARK_FAILED = "UPDATE public.operator_jobs SET status='error' WHERE id=%s::uuid"
_SUCCESS = frozenset(('completed', 'skipped_no_recent_videos', 'skipped_too_many_recent_videos'))


def _fail(code):
    raise DatabaseServerError(code, code='P0001')


def _value(row):
    if not isinstance(row, dict) or not isinstance(row.get('value'), dict):
        raise DatabaseError('database_result_shape_invalid')
    return row['value']


@contextmanager
def _transaction(database, transaction, name):
    if transaction is not None:
        with transaction.borrow():
            yield transaction
    else:
        spec = get_operation(name)
        with database.transaction(Operation(name, statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms, total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as tx:
            yield tx


def settle_initial_inventory_stats(database, job_id, lease_token, reserved_units,
                                   observed_quota_units, status, summary=None, *, transaction=None):
    """Settle one owned initial-stats attempt; an explicit repeat loses its lease."""
    with _transaction(database, transaction, 'settle_initial_inventory_stats') as tx:
        encoded = summary if isinstance(summary, Jsonb) else Jsonb(summary) if summary is not None else None
        args = tx.fetch_one(_STATS_INPUT, (job_id, lease_token, reserved_units, observed_quota_units, status, encoded))
        held = tx.fetch_one(_JOB, (args['job_id'],))
        job = _value(held) if held is not None else None
        if (job is None or job['job_type'] != 'channel_initial_long_inventory'
                or job['phase'] != 'initial_stats' or job['status'] != 'running'
                or job['lease_token'] != args['lease_token']):
            _fail('operator_job_lease_lost')
        if (args['reserved_units'] is None or args['reserved_units'] < 0
                or args['observed_quota_units'] is None or args['observed_quota_units'] < 0):
            _fail('operator_job_initial_stats_settlement_invalid')
        success = args['status'] in _SUCCESS
        deferred = args['status'] == 'quota_deferred'
        rate_limited = args['status'] == 'rate_limited'
        counter = tx.fetch_one(_COUNTER, (job['error_count'], 0 if success or deferred or rate_limited else 1))
        errors = counter['error_count']
        next_status = 'completed' if success else 'queued' if rate_limited else 'error' if errors >= job['max_target_attempts'] else 'queued'
        reset_at = youtube_quota_reset_at(counter['as_of']) if deferred and next_status == 'queued' else None
        error_code = None if success else 'initial_stats_quota_exhausted' if deferred else 'provider_rate_limited' if rate_limited else 'initial_stats_failed'
        error_summary = (None if success else 'Initial stats exhausted its bounded retry policy.' if next_status == 'error'
            else 'Waiting for the YouTube daily quota to reset.' if deferred
            else 'Waiting briefly after a YouTube rate refusal.' if rate_limited
            else 'Initial stats will retry under the existing per-job cap.')
        updated = _value(tx.returning_one(_SETTLE, {**args, 'summary': encoded, 'error_count': errors,
            'next_status': next_status, 'quota_deferred': deferred, 'rate_limited': rate_limited, 'reset_at': reset_at,
            'error_code': error_code, 'error_summary': error_summary}))
        # Retained late check: native arithmetic/JSONB/trigger errors win first.
        epoch = job['lease_channel_epoch']
        if epoch is None or job['accepted_epoch'] != epoch:
            _fail('operator_job_lease_lost')
        assert_channel_scope(tx, updated['channel_id'], epoch)
        return updated


def mark_completed_initial_hydration_failed(database, job_id, channel_id, error_count,
                                           expected_epoch, *, transaction=None):
    """Narrow completed/verify compare-and-set; do not reset progress or clocks."""
    with _transaction(database, transaction, 'mark_completed_initial_hydration_failed') as tx:
        args = tx.fetch_one(_CAS_INPUT, (job_id, channel_id, error_count, expected_epoch))
        if args['expected_epoch'] is None or args['expected_epoch'] <= 0:
            _fail('channel_collection_scope_invalid')
        held = tx.fetch_one(_JOB, (args['job_id'],))
        job = _value(held) if held is not None else None
        if (job is None or job['job_type'] != 'channel_initial_long_inventory'
                or job['channel_id'] != args['channel_id'] or job['status'] != 'completed'
                or job['phase'] != 'verify' or job['error_count'] != args['error_count']
                or job['accepted_epoch'] != args['expected_epoch']):
            return 0
        tx.execute(_MARK_FAILED, (job['id'],))
        assert_channel_scope(tx, job['channel_id'], job['accepted_epoch'])
        return 1
