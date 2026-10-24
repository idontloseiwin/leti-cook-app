"""Initial-job video leases in Python with bounded native claims.

The durable job lease is the run identity. Channel then job SHARE locks precede
source/target checks; the sole video queue owner is changed in one SKIP LOCKED
statement and the captured channel epoch is fenced last. No provider call or
automatic retry belongs here. Typed eligibility uses the shared finite policy
at its original native SQL positions; intrinsic operator source/target scope
guards remain authoritative.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, channel_eligible_row)
from .typed_video_read_policy import (TYPED_CANDIDATE_INVENTORY_ROW_SQL, TYPED_CANDIDATE_STATE_ROW_SQL)
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scopes
from .video_initial_policy import INITIAL_RETRY_ALLOWED_SQL
from ..base.operation_contracts import get_operation
from ..jobs.operator_job_scope import assert_full_initial_scope
from ..base.postgres import DatabaseServerError, Operation
from ..base.sql_values import application_value
from .video_queue_initialization import LONG_SHORT_CONFLICT_SQL

_INPUT = """WITH args AS MATERIALIZED (
 SELECT %s::uuid AS job_id,%s::text[] AS video_ids,%s::text AS worker_id,
 %s::uuid AS run_id,%s::integer AS lease_seconds)
 SELECT args.*,cardinality(video_ids) AS id_count,
 job_id IS NULL OR run_id IS NULL OR nullif(btrim(worker_id),'') IS NULL
 OR length(worker_id)>200 OR video_ids IS NULL OR cardinality(video_ids)>50
 OR EXISTS(SELECT 1 FROM unnest(video_ids) requested(id)
 WHERE nullif(btrim(id),'') IS NULL OR id<>btrim(id) OR length(id)>128)
 OR (SELECT count(DISTINCT id) FROM unnest(video_ids) requested(id))<>cardinality(video_ids)
 OR lease_seconds IS NULL OR lease_seconds<30 OR lease_seconds>3600 AS invalid
 FROM args"""
_FIND = "SELECT channel_id FROM public.operator_jobs WHERE id=%s::uuid"
_CHANNEL = "SELECT channel_id FROM public.channels WHERE channel_id=%s::text FOR SHARE"
_ELIGIBLE = ('SELECT (SELECT policy_facts FROM (' + CHANNEL_CONTROL_FACTS_SQL + ') policy WHERE channel_id=%s::text) AS policy_facts')
_JOB = """SELECT to_jsonb(job)::text AS value,lease_channel_epoch AS epoch,
 to_jsonb(lease_expires_at)#>>'{}' AS expires,
 job_type<>'channel_initial_long_inventory' OR phase<>'hydrate_videos' OR status<>'running'
 OR lease_token IS DISTINCT FROM %(run_id)s::uuid OR lease_owner IS DISTINCT FROM btrim(%(worker_id)s::text)
 OR lease_expires_at IS NULL OR lease_expires_at<=now() OR lease_channel_epoch IS NULL
 OR accepted_epoch IS DISTINCT FROM lease_channel_epoch OR initial_video_types IS NULL
 OR cardinality(initial_video_types)=0 OR NOT(initial_video_types<@ARRAY['long','short','stream']::text[]) AS invalid
 FROM public.operator_jobs job WHERE id=%(job_id)s::uuid AND channel_id=%(channel_id)s::text FOR SHARE"""
_SOURCE = """SELECT requested_at IS NULL OR NOT isfinite(requested_at)
 OR requested_at>clock_timestamp() OR requested_at<clock_timestamp()-interval '696 hours' AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) job"""
_TARGETS = """SELECT EXISTS(SELECT 1 FROM unnest(%(video_ids)s::text[]) requested(id)
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) job
 LEFT JOIN public.operator_job_targets target ON target.job_id=job.id AND target.video_id=requested.id
 LEFT JOIN public.video_inventory inventory ON inventory.video_id=requested.id
 WHERE target.video_id IS NULL OR target.status<>'pending' OR target.job_attempts<1
 OR target.video_type IS NULL OR NOT(target.video_type=ANY(job.initial_video_types))
 OR inventory.video_id IS NULL OR inventory.channel_id IS DISTINCT FROM job.channel_id
 OR inventory.video_type IS DISTINCT FROM target.video_type) AS invalid"""

_CONFLICT_INVENTORY = LONG_SHORT_CONFLICT_SQL.replace('%(video_id)s::text','inventory.video_id').replace('%(channel_id)s::text','inventory.channel_id')
_CONFLICT_STATE = LONG_SHORT_CONFLICT_SQL.replace('%(video_id)s::text','state.video_id').replace('%(channel_id)s::text','state.channel_id')
_IDENTITY_REFRESH = "(" + _CONFLICT_STATE + """ AND NOT EXISTS(SELECT 1 FROM public.video_type_automatic_candidates automatic
 WHERE automatic.video_id=state.video_id AND automatic.channel_id=state.channel_id
 AND automatic.automatic_type=state.video_type))"""

_INSERT = """insert into public.video_collection_state(
        video_id,channel_id,video_type,hydration_needed,
        stats_due_at,metadata_due_at,next_due_at,
        typed_source_published_at,typed_source_published_acquired_at,typed_collection_status
    )
    select inventory.video_id,inventory.channel_id,inventory.video_type,
        current_video.video_id is null OR ("""+_CONFLICT_INVENTORY+"""),
        case when inventory.video_type='long' then now() end,
        case when inventory.video_type='long' then now() end,
        now(),
        case when inventory.video_type<>'long' then source.source_published_at end,
        case when inventory.video_type<>'long' AND source.source_published_at IS NOT NULL then source.last_present_at end,
        case when inventory.video_type<>'long' then 'pending' end
    from public.video_inventory as inventory
    left join public.videos as current_video on current_video.video_id=inventory.video_id
    left join lateral (
        select membership.source_published_at,membership.last_present_at
        from public.video_inventory_memberships as membership
        where membership.video_id=inventory.video_id
          and membership.channel_id=inventory.channel_id
          and membership.video_type=inventory.video_type
          and membership.membership_status='present'
        order by membership.source_published_at desc nulls last limit 1
    ) source on true
    where inventory.video_id=any(%(video_ids)s::text[])
      and inventory.channel_id=%(channel_id)s::text
      and (inventory.video_type='long' or ("""+_CONFLICT_INVENTORY+""") or EXISTS(SELECT 1 FROM (""" + TYPED_CANDIDATE_INVENTORY_ROW_SQL + """) candidate
          WHERE candidate.video_id=inventory.video_id AND candidate.channel_id=inventory.channel_id
          AND candidate.video_type=inventory.video_type))
    on conflict on constraint video_collection_state_pkey do nothing"""
_CLAIM = """update public.video_collection_state as state
        set lease_token=gen_random_uuid(),lease_owner=btrim(%(worker_id)s::text),
            lease_channel_epoch=%(epoch)s::bigint,
            lease_started_at=now(),lease_expires_at=least(
                now()+make_interval(secs=>%(lease_seconds)s::integer),%(expires)s::timestamptz),
            attempt_count=state.attempt_count+1,last_claim_run_id=%(run_id)s::uuid,
            last_attempt_at=now(),updated_at=now()
        FROM (WITH candidates AS MATERIALIZED (        select state.video_id
        from public.video_collection_state as state
        cross join jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
        join public.operator_job_targets as target
          on target.job_id=%(job_id)s::uuid and target.video_id=state.video_id
         and target.video_type=state.video_type and target.status='pending'
        where state.video_id=any(%(video_ids)s::text[]) and state.channel_id=%(channel_id)s::text
          and (state.lease_token is null or state.lease_expires_at<=now())
          and state.last_claim_run_id is distinct from %(run_id)s::uuid
          and """ + INITIAL_RETRY_ALLOWED_SQL + """
          and (state.video_type='long' or ("""+_CONFLICT_STATE+""") or EXISTS(SELECT 1 FROM (""" + TYPED_CANDIDATE_STATE_ROW_SQL + """) candidate
              WHERE candidate.video_id=state.video_id AND candidate.channel_id=state.channel_id
              AND candidate.video_type=state.video_type))
        order by array_position(%(video_ids)s::text[],state.video_id)
        for update of state skip locked
) SELECT video_id FROM candidates) candidates WHERE state.video_id=candidates.video_id
RETURNING state.video_id,state.channel_id,state.video_type,
 CASE WHEN """+_IDENTITY_REFRESH+""" THEN 'identity_refresh'
 WHEN state.video_type='stream' AND NOT EXISTS(
  SELECT 1 FROM public.videos current_video WHERE current_video.video_id=state.video_id
  AND current_video.stream_actual_end_at IS NOT NULL) THEN 'stream_lifecycle' ELSE 'metadata' END AS collection_mode,
 state.lease_token,state.lease_expires_at,coalesce(state.next_due_at,now()) AS next_due_at,
 state.lease_channel_epoch,array_position(%(video_ids)s::text[],state.video_id) AS input_position"""


def _fail(code):
    raise DatabaseServerError(code, code='P0001')


@contextmanager
def _transaction(database, transaction):
    if transaction is not None:
        with transaction.borrow():
            yield transaction
    else:
        spec = get_operation('claim_initial_video_collection_batch')
        with database.transaction(Operation(spec.name,
                statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,
                total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as owned:
            yield owned


def claim_initial_video_collection_batch(database, job_id, video_ids, worker_id,
        run_id, lease_seconds=900, *, transaction=None):
    with _transaction(database, transaction) as tx:
        args = tx.fetch_one(_INPUT, (job_id, video_ids, worker_id, run_id, lease_seconds))
        if args['invalid'] is True:
            _fail('initial_video_collection_claim_input_invalid')
        located = tx.fetch_one(_FIND, (args['job_id'],))
        if located is None:
            _fail('initial_video_collection_job_missing')
        args['channel_id'] = located['channel_id']
        channel = tx.fetch_one(_CHANNEL, (args['channel_id'],))
        if channel is None or channel_eligible_row(tx.fetch_one(_ELIGIBLE, (args['channel_id'],)))['eligible'] is not True:
            _fail('initial_video_collection_channel_ineligible')
        job = tx.fetch_one(_JOB, args)
        if job is None or job['invalid'] is True:
            _fail('initial_video_collection_job_lease_lost')
        if tx.fetch_one(_SOURCE, (job['value'],))['invalid'] is True:
            _fail('operator_job_source_scope_expired')
        assert_full_initial_scope(tx, job['value'], True)
        # The retained empty request validates ownership/sources, then returns
        # before the final control fence and without any queue mutation.
        if args['id_count'] == 0:
            return []
        args.update(job=job['value'], epoch=job['epoch'], expires=job['expires'])
        if tx.fetch_one(_TARGETS, args)['invalid'] is True:
            _fail('initial_video_collection_target_scope_invalid')
        tx.execute(_INSERT, args)
        rows = tx.returning_all(_CLAIM, args, max_rows=50)
        result = []
        for claimed in sorted(rows, key=lambda item: item['input_position']):
            row = application_value(claimed)
            row.pop('lease_channel_epoch')
            row.pop('input_position')
            result.append(row)
        # Every candidate is restricted to this already-validated job/channel/
        # epoch; the original DISTINCT scope aggregation therefore has one row.
        assert_channel_scopes(tx, [dict(channel_id=args['channel_id'], control_epoch=job['epoch'])])
        return result
