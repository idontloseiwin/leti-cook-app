"""Runtime operator-job claim/release protocols.

Channel row -> job row -> optional channel update -> final control SHARE is
retained claim order. Channel workflow helpers apply channel side effects;
intrinsic operator source-scope guards remain authoritative. Release keeps its
native token/status guarded UPDATE, including separate wall-clock samples and
five-minute backoff. No replay, provider I/O, private function calls, page
settlement or full-initial initialization.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, CHANNEL_CONTROL_SQL, channel_mode_row)
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from ..channel.channel_workflow_updates import apply_channel_stage
from ..base.postgres import DatabaseError, DatabaseServerError, Operation

_CLAIM_INPUT = "SELECT %s::text AS channel_id,btrim(%s::text) AS worker,%s::integer AS lease_seconds"
_CANDIDATE = ("""
 SELECT channel.channel_id,control.is_hidden,control.collection_mode,control.control_epoch,job.id::text AS job_id
 FROM public.operator_jobs job
 JOIN public.channels channel ON channel.channel_id=job.channel_id
 JOIN """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ control ON control.channel_id=channel.channel_id
 WHERE control.owner_permission='accepted' AND NOT control.is_hidden
  AND job.accepted_epoch=control.control_epoch
  AND ((job.job_type='channel_initial_long_inventory' AND control.initial_job_id=job.id)
   OR (job.job_type='channel_current_stats_backfill' AND control.collection_mode='active'))
  AND job.requested_at>=clock_timestamp()-interval '696 hours'
  AND job.requested_at<=clock_timestamp() AND isfinite(job.requested_at)
  AND ((job.status='queued' AND (job.retry_not_before IS NULL OR job.retry_not_before<=now()))
   OR (job.status='running' AND job.lease_expires_at IS NOT NULL AND job.lease_expires_at<=now()))
 ORDER BY job.requested_at,job.id FOR UPDATE OF channel SKIP LOCKED LIMIT 1
""")
_CHANNEL = ("""
 SELECT channel.channel_id,control.is_hidden,control.policy_facts,control.control_epoch
 FROM public.channels channel JOIN (""" + CHANNEL_CONTROL_FACTS_SQL + """) control ON control.channel_id=channel.channel_id
 WHERE control.owner_permission='accepted' AND NOT control.is_hidden AND channel.channel_id=btrim(%s::text)
 FOR UPDATE OF channel SKIP LOCKED
""")
_JOB = """
 SELECT to_jsonb(job) AS value FROM public.operator_jobs job
 WHERE job.requested_at>=clock_timestamp()-interval '696 hours'
  AND job.requested_at<=clock_timestamp() AND isfinite(job.requested_at)
  AND job.id=%(job_id)s::uuid AND job.channel_id=%(channel_id)s
  AND ((job.status='queued' AND (job.retry_not_before IS NULL OR job.retry_not_before<=now()))
   OR (job.status='running' AND job.lease_expires_at IS NOT NULL AND job.lease_expires_at<=now()))
 FOR UPDATE SKIP LOCKED
"""
_INITIAL_JOB = """
 SELECT to_jsonb(job) AS value FROM public.operator_jobs job
 WHERE job.requested_at>=clock_timestamp()-interval '696 hours'
  AND job.requested_at<=clock_timestamp() AND isfinite(job.requested_at)
  AND job.channel_id=%(channel_id)s AND job.job_type='channel_initial_long_inventory'
  AND job.accepted_epoch=%(control_epoch)s
  AND job.id=(SELECT initial_job_id FROM public.channel_collection_state WHERE channel_id=%(channel_id)s)
  AND ((job.status='queued' AND (job.retry_not_before IS NULL OR job.retry_not_before<=now()))
   OR (job.status='running' AND job.lease_expires_at IS NOT NULL AND job.lease_expires_at<=now()))
 ORDER BY job.requested_at,job.id FOR UPDATE SKIP LOCKED LIMIT 1
"""
_LEASE = """
 UPDATE public.operator_jobs job SET status='running',retry_not_before=NULL,
  started_at=coalesce(job.started_at,now()),updated_at=now(),heartbeat_at=now(),lease_token=gen_random_uuid(),
  lease_channel_epoch=%(control_epoch)s,lease_owner=%(worker)s,
  lease_expires_at=now()+make_interval(secs=>%(lease_seconds)s::integer)
 WHERE job.id=%(job_id)s::uuid RETURNING to_jsonb(job) AS value
"""
_START_INVENTORY = """
 UPDATE public.channels SET initial_long_inventory_status='running',
  initial_long_inventory_started_at=coalesce(initial_long_inventory_started_at,now()) WHERE channel_id=%s
"""
_RELEASE_INPUT = "SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS lease_token,%s::text AS error_code"
_RELEASE_SUMMARY = "Worker released the job after a sanitized component error; a later timer may resume it."
_RELEASE = """
 UPDATE public.operator_jobs job
 SET status=CASE WHEN EXISTS(SELECT 1 FROM public.channel_collection_state control
  WHERE control.channel_id=job.channel_id AND NOT control.is_hidden AND control.owner_permission='accepted'
   AND control.control_epoch=job.accepted_epoch AND job.lease_channel_epoch=job.accepted_epoch)
  AND isfinite(job.requested_at) AND job.requested_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
  THEN 'queued' ELSE 'suspended' END,
 finished_at=CASE WHEN EXISTS(SELECT 1 FROM public.channel_collection_state control
  WHERE control.channel_id=job.channel_id AND NOT control.is_hidden AND control.owner_permission='accepted'
   AND control.control_epoch=job.accepted_epoch AND job.lease_channel_epoch=job.accepted_epoch)
  AND isfinite(job.requested_at) AND job.requested_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
  THEN job.finished_at ELSE coalesce(job.finished_at,now()) END,
 retry_not_before=CASE WHEN EXISTS(SELECT 1 FROM public.channel_collection_state control
  WHERE control.channel_id=job.channel_id AND NOT control.is_hidden AND control.owner_permission='accepted'
   AND control.control_epoch=job.accepted_epoch AND job.lease_channel_epoch=job.accepted_epoch)
  AND isfinite(job.requested_at) AND job.requested_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
  THEN clock_timestamp()+interval '5 minutes' ELSE job.retry_not_before END,
 updated_at=now(),heartbeat_at=now(),
 last_error_code=left(coalesce(nullif(btrim(%(error_code)s::text),''),'operator_worker_error'),120),
 last_error_summary=%(error_summary)s::text,
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE job.id=%(job_id)s::uuid AND job.status='running' AND job.lease_token IS NOT DISTINCT FROM %(lease_token)s::uuid
 RETURNING to_jsonb(job) AS value
"""


def _fail(code):raise DatabaseServerError(code,code='P0001')


def _value(row):
    if not isinstance(row,dict) or not isinstance(row.get('value'),dict):
        raise DatabaseError('database_result_shape_invalid')
    return row['value']


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation(name)
        with database.transaction(Operation(name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as tx:yield tx


def _claim(database,channel_id,worker_id,lease_seconds,*,initial,transaction,identity=None,expected_epoch=None):
    exact = identity is not None
    name='claim_operator_job_id' if exact else 'claim_channel_initial_hydration_job' if initial else 'claim_operator_job'
    with _transaction(database,transaction,name) as tx:
        args=tx.fetch_one(_CLAIM_INPUT,(channel_id,worker_id,lease_seconds))
        if initial and (args['channel_id'] is None or not args['channel_id'].strip(' ')):
            _fail('operator_job_channel_required')
        if not args['worker']:_fail('operator_job_worker_required')
        if args['lease_seconds'] is None or not 60<=args['lease_seconds']<=3600:
            _fail('operator_job_lease_seconds_invalid')
        candidate=channel_mode_row(tx.fetch_one(_CHANNEL,(args['channel_id'],))) if initial or exact else tx.fetch_one(_CANDIDATE)
        if candidate is None:return None
        if exact:
            if candidate['control_epoch'] != expected_epoch:return None
            candidate={**candidate,'job_id':identity,'expected_epoch':expected_epoch}
        held=tx.fetch_one(_EXACT_JOB if exact else _INITIAL_JOB if initial else _JOB,candidate)
        if held is None:return None
        job=_value(held)
        if exact and (job['id']!=identity or job['channel_id']!=channel_id):
            raise DatabaseError('operator_dispatch_claim_unconfirmed')
        if job['accepted_epoch']!=candidate['control_epoch']:return None
        is_initial=job['job_type']=='channel_initial_long_inventory'
        if candidate['is_hidden'] or (is_initial and (candidate['collection_mode'] not in
                ('initial_inventory','active','metadata_only') or job['phase'] not in
                ('inventory','hydrate_videos','verify','initial_stats'))):return None
        effective=60 if is_initial and job['phase']=='verify' else args['lease_seconds']
        leased=tx.returning_one(_EXACT_LEASE if exact else _LEASE,dict(job_id=job['id'],control_epoch=candidate['control_epoch'],
                                               worker=args['worker'],lease_seconds=effective))
        if exact and leased is None:_fail('operator_job_lease_lost')
        job=_value(leased)
        if is_initial and job['phase']=='inventory' and (len(job['initial_video_types'])==1 or job['inventory_video_type']=='long'):
            apply_channel_stage(tx,'claim_running',(candidate['channel_id'],))
        assert_channel_scope(tx,job['channel_id'],candidate['control_epoch'])
        return job


def claim_operator_job(database,worker_id,lease_seconds=1200,*,transaction=None):
    return _claim(database,None,worker_id,lease_seconds,initial=False,transaction=transaction)


def claim_channel_initial_hydration_job(database,channel_id,worker_id,lease_seconds=1200,*,transaction=None):
    return _claim(database,channel_id,worker_id,lease_seconds,initial=True,transaction=transaction)


def release_operator_job(database,job_id,lease_token,error_code,*,transaction=None):
    with _transaction(database,transaction,'release_operator_job') as tx:
        args=tx.fetch_one(_RELEASE_INPUT,(job_id,lease_token,error_code))
        row=tx.returning_one(_RELEASE,{**args,'error_summary':_RELEASE_SUMMARY})
        if row is None:_fail('operator_job_lease_lost')
        return _value(row)
# Opt-in shared-child scheduling. Existing legacy claim SQL/order stays intact.
_FAIR_KEY = 'coalesce(job.last_scheduled_at,job.requested_at)'
_FAIR_ELIGIBLE = """control.owner_permission='accepted' AND NOT control.is_hidden
 AND job.accepted_epoch=control.control_epoch
 AND ((job.job_type='channel_initial_long_inventory' AND control.initial_job_id=job.id
       AND control.collection_mode IN ('initial_inventory','active','metadata_only')
       AND job.phase IN ('inventory','hydrate_videos','verify','initial_stats'))
   OR (job.job_type='channel_current_stats_backfill' AND control.collection_mode='active'))
 AND isfinite(job.requested_at)
 AND job.requested_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND (job.last_scheduled_at IS NULL OR job.last_scheduled_at<=clock_timestamp())
 AND ((job.status='queued' AND (job.retry_not_before IS NULL OR job.retry_not_before<=clock_timestamp()))
   OR (job.status='running' AND job.lease_expires_at IS NOT NULL
       AND job.lease_expires_at<=clock_timestamp()))"""
_FAIR_CLOCK = 'SELECT clock_timestamp() AS at'
_FAIR_PAGE = """WITH physical AS MATERIALIZED (
 SELECT job.id,job.requested_at,""" + _FAIR_KEY + """ AS fair_at
 FROM public.operator_jobs job
 WHERE job.status IN ('queued','running') AND isfinite(job.requested_at)
 AND """ + _FAIR_KEY + """<=%(ceiling)s::timestamptz
 AND (%(after_id)s::uuid IS NULL OR (""" + _FAIR_KEY + """,job.requested_at,job.id)>
  (%(after_fair)s::timestamptz,%(after_requested)s::timestamptz,%(after_id)s::uuid))
 ORDER BY """ + _FAIR_KEY + """,job.requested_at,job.id LIMIT %(limit)s::integer)
 SELECT physical.id::text AS id,physical.requested_at,physical.fair_at,
 job.channel_id,job.accepted_epoch,job.job_type,job.phase,
 coalesce((""" + _FAIR_ELIGIBLE + """),false) AS eligible
 FROM physical JOIN public.operator_jobs job ON job.id=physical.id
 LEFT JOIN (""" + CHANNEL_CONTROL_SQL + """) control ON control.channel_id=job.channel_id
 ORDER BY physical.fair_at,physical.requested_at,physical.id"""
_EXACT_JOB = """SELECT to_jsonb(job) AS value FROM public.operator_jobs job
 JOIN (""" + CHANNEL_CONTROL_SQL + """) control ON control.channel_id=job.channel_id
 WHERE job.id=%(job_id)s::uuid AND job.channel_id=%(channel_id)s
 AND job.accepted_epoch=%(expected_epoch)s::bigint AND (""" + _FAIR_ELIGIBLE + """)
 FOR UPDATE OF job SKIP LOCKED"""
_EXACT_LEASE = """UPDATE public.operator_jobs job SET status='running',retry_not_before=NULL,
 started_at=coalesce(job.started_at,native.at),updated_at=native.at,heartbeat_at=native.at,
 last_scheduled_at=native.at,lease_token=gen_random_uuid(),
 lease_channel_epoch=%(control_epoch)s,lease_owner=%(worker)s,
 lease_expires_at=native.at+make_interval(secs=>%(lease_seconds)s::integer)
 FROM (SELECT clock_timestamp() AS at OFFSET 0) native WHERE job.id=%(job_id)s::uuid AND job.accepted_epoch=%(control_epoch)s
 AND job.requested_at BETWEEN native.at-interval '696 hours' AND native.at
 AND (job.last_scheduled_at IS NULL OR job.last_scheduled_at<=native.at)
 RETURNING to_jsonb(job) AS value"""


def _canonical_uuid(value):
    if type(value) is not str:
        raise ValueError('operator_dispatch_identity_invalid')
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError):
        raise ValueError('operator_dispatch_identity_invalid') from None
    if value != canonical:
        raise ValueError('operator_dispatch_identity_invalid')
    return canonical


def _cursor_clock(value):
    if type(value) is not str or len(value) != 32:
        raise ValueError('operator_candidate_cursor_invalid')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('operator_candidate_cursor_invalid') from None
    if (parsed.utcoffset() != timedelta(0)
            or parsed.astimezone(timezone.utc).isoformat(timespec='microseconds') != value):
        raise ValueError('operator_candidate_cursor_invalid')
    return value


def _native_clock(value):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() != timedelta(0)):
        raise DatabaseError('operator_candidate_clock_invalid')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


@dataclass(frozen=True)
class OperatorCandidateCursor:
    """Strict advisory key, never a lease or authorization token."""
    fair_at: str
    requested_at: str
    id: str

    def __post_init__(self):
        _cursor_clock(self.fair_at)
        _cursor_clock(self.requested_at)
        _canonical_uuid(self.id)
        if self.requested_at > self.fair_at:
            raise ValueError('operator_candidate_cursor_invalid')

    def order_key(self):
        return self.fair_at, self.requested_at, UUID(self.id).int


def operator_candidate_page(database, *, limit=16, after=None, ceiling=None):
    """Physical keyset before eligibility; caller bounds ready/scan budgets."""
    if (type(limit) is not int or not 1 <= limit <= 100
            or (after is not None and type(after) is not OperatorCandidateCursor)
            or (after is not None and ceiling is None)):
        raise ValueError('operator_candidate_page_invalid')
    if ceiling is not None:
        _cursor_clock(ceiling)
        if after is not None and after.fair_at > ceiling:
            raise ValueError('operator_candidate_page_invalid')
    spec = get_operation('operator_candidate_page')
    with database.transaction(spec) as tx:
        now = _native_clock(tx.fetch_one(_FAIR_CLOCK)['at'])
        if ceiling is None:
            ceiling = now
        elif ceiling > now:
            raise ValueError('operator_candidate_page_invalid')
        params = dict(limit=limit, ceiling=ceiling,
            after_id=None if after is None else after.id,
            after_fair=None if after is None else after.fair_at,
            after_requested=None if after is None else after.requested_at)
        rows = tx.fetch_all(_FAIR_PAGE, params, max_rows=limit)
        candidates, next_after = [], after
        for row in rows:
            cursor = OperatorCandidateCursor(_native_clock(row['fair_at']),
                _native_clock(row['requested_at']), row['id'])
            if (type(row['eligible']) is not bool or cursor.fair_at > ceiling
                    or (next_after is not None and cursor.order_key() <= next_after.order_key())):
                raise DatabaseError('operator_candidate_page_unconfirmed')
            next_after = cursor
            if row['eligible']:
                if (type(row['channel_id']) is not str or not row['channel_id']
                        or row['channel_id'] != row['channel_id'].strip()
                        or type(row['accepted_epoch']) is not int or row['accepted_epoch'] <= 0
                        or row['job_type'] not in ('channel_initial_long_inventory','channel_current_stats_backfill')
                        or row['phase'] not in ('inventory','hydrate_videos','verify','initial_stats')):
                    raise DatabaseError('operator_candidate_page_unconfirmed')
                candidates.append(dict(job_id=cursor.id, channel_id=row['channel_id'],
                    accepted_epoch=row['accepted_epoch'], job_type=row['job_type'], phase=row['phase']))
        return dict(candidates=candidates, next_after=next_after, ceiling=ceiling,
                    inspected=len(rows), exhausted=len(rows)<limit)


def claim_operator_job_id(database, job_id, expected_channel, expected_epoch,
                          worker_id, lease_seconds=1200, *, transaction=None):
    """Claim one advisory identity or none; never substitute another job."""
    _canonical_uuid(job_id)
    if (type(expected_channel) is not str or not expected_channel
            or expected_channel != expected_channel.strip()
            or type(expected_epoch) is not int or not 1 <= expected_epoch <= 9223372036854775807
            or type(worker_id) is not str or not worker_id.strip()
            or type(lease_seconds) is not int or not 60 <= lease_seconds <= 3600):
        raise ValueError('operator_dispatch_claim_invalid')
    return _claim(database, expected_channel, worker_id, lease_seconds,
        initial=False, transaction=transaction, identity=job_id, expected_epoch=expected_epoch)
