"""Operator/hydration chunk settlement.

Target classification stays in ordered set-based native statements. The exact
job token and status authorize settlement even after its lease deadline; adding
an expiry check here would change the retained protocol. Opaque job/summary
JSON stays native across statements. The captured epoch fence is last, after
all counters, JSON casts and intrinsic guards, in one owned or borrowed
transaction.
"""
from contextlib import contextmanager
from collections.abc import Mapping
from psycopg.types.json import Jsonb
from collection_policy import youtube_quota_reset_at
from ..channel.channel_control import assert_channel_scope
from ..videos.video_initial_policy import INITIAL_JOB_NEXT_DUE_SQL
from ..base.operation_contracts import get_operation
from .operator_job_scope import assert_full_initial_scope
from ..videos.video_queue_initialization import LONG_SHORT_CONFLICT_SQL
from ..base.postgres import DatabaseServerError,Operation

_DEFAULT_SUMMARY=object()
_INPUT="""SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS lease_token,
 %s::integer AS observed_quota_units,%s::text AS error_code,%s::jsonb::text AS summary"""
_JOB="""SELECT to_jsonb(job)::text AS job,job.job_type,job.phase,job.initial_scope_version AS version,
 cardinality(job.initial_video_types)=3 AS all_types,job.channel_id,job.lease_channel_epoch AS epoch,
 job.accepted_epoch,job.status='running' AND job.lease_token IS NOT DISTINCT FROM %(lease_token)s::uuid AS valid
 FROM public.operator_jobs job WHERE job.id=%(job_id)s::uuid FOR UPDATE"""
_HYDRATION_VALID="""SELECT coalesce(%(observed_quota_units)s::integer,-1)<0 AS invalid_units,
 %(error_code)s::text IS NOT NULL AND %(error_code)s::text !~ '^[a-z0-9_]{1,120}$' AS invalid_error"""
_J="WITH j AS(SELECT * FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb)) "
_RECORD="jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"
_CHUNK_VALID=_J+"""SELECT %(summary)s::jsonb IS NULL OR jsonb_typeof(%(summary)s::jsonb)<>'object'
 OR %(summary)s::jsonb ?| ARRAY['full_inventory_sources','full_hydration_chunk','full_retry_after','full_retry_requested_at','confirmed','inventory_ack']
 OR jsonb_typeof(j.summary_json->'full_hydration_chunk') IS DISTINCT FROM 'array'
 OR jsonb_array_length(j.summary_json->'full_hydration_chunk')>
 CASE WHEN j.job_type='channel_initial_long_inventory' THEN 400 ELSE 250 END AS invalid FROM j"""
_CHUNK=_J+"""SELECT coalesce(array_agg(v),ARRAY[]::text[]) AS ids FROM j
 CROSS JOIN LATERAL jsonb_array_elements_text(j.summary_json->'full_hydration_chunk') q(v)"""
_SCOPE="target.job_id=j.id AND target.status='pending' AND (j.initial_scope_version=0 OR target.video_id=ANY(%(chunk)s::text[]))"
# Frozen video IDs stay fixed. Only this fresh automatic same-channel policy
# may normalize a provisional Long/Short target after General supplied duration.
_IDENTITY="""UPDATE public.operator_job_targets target SET
 video_type=coalesce(classification.automatic_type,target.video_type),
 status=CASE WHEN classification.automatic_type IS NULL THEN 'error' ELSE target.status END,
 result_code=CASE WHEN classification.automatic_type IS NULL THEN 'frozen_identity_changed' ELSE target.result_code END,
 completed_at=CASE WHEN classification.automatic_type IS NULL THEN now() ELSE target.completed_at END,
 updated_at=now() FROM (WITH classification AS MATERIALIZED (
 SELECT target.job_id,target.video_id,automatic.automatic_type
 FROM public.operator_job_targets target CROSS JOIN """+_RECORD+"""
 LEFT JOIN public.video_inventory inventory ON inventory.video_id=target.video_id AND inventory.channel_id=j.channel_id
 LEFT JOIN public.video_type_automatic_candidates automatic ON automatic.video_id=inventory.video_id
 AND automatic.channel_id=inventory.channel_id AND automatic.automatic_type=inventory.video_type
 AND target.video_type IN ('long','short') AND j.initial_scope_version=1
 WHERE """+_SCOPE+""" AND NOT EXISTS(SELECT 1 FROM public.video_inventory current_identity
 WHERE current_identity.video_id=target.video_id AND current_identity.channel_id=j.channel_id
 AND current_identity.video_type=target.video_type))
 SELECT * FROM classification) classification
 WHERE target.job_id=classification.job_id AND target.video_id=classification.video_id"""
# Only eligibility fields are needed; keep the captured job's large summary
# out of UPDATE source row marks.
_REFRESHED="""UPDATE public.operator_job_targets target SET status='updated',result_code='refreshed',
 completed_at=now(),updated_at=now() FROM jsonb_to_record(%(job)s::jsonb)
 AS j(id uuid,channel_id text,requested_at timestamptz,initial_scope_version smallint,initial_video_types text[]),
 public.video_collection_state state WHERE """+_SCOPE+"""
 AND NOT state.hydration_needed AND state.video_id=target.video_id AND state.stats_source_checked_at>=j.requested_at
 AND state.metadata_source_checked_at>=j.requested_at AND(cardinality(j.initial_video_types)=1 OR(
 state.channel_id=j.channel_id AND state.video_type=target.video_type
 AND(target.video_type='long' OR state.typed_collection_status='hydrated')))"""
_UNAVAILABLE="""UPDATE public.operator_job_targets target SET status='unavailable',result_code='unavailable_confirmed',
 completed_at=now(),updated_at=now() FROM """+_RECORD+",public.open_video_availability unavailable WHERE "+_SCOPE+"""
 AND unavailable.video_id=target.video_id AND unavailable.last_seen_at>=j.requested_at
 AND(cardinality(j.initial_video_types)=1 OR EXISTS(SELECT 1 FROM public.video_collection_state state
 WHERE state.video_id=target.video_id AND state.channel_id=j.channel_id AND state.video_type=target.video_type
 AND(target.video_type='long' OR(state.typed_collection_status='unavailable'
 AND state.typed_collection_checked_at>=j.requested_at))))"""
_INITIAL_IDENTITY_OBSERVED = """(j.initial_scope_version=1 AND state.video_id=target.video_id
 AND state.channel_id=j.channel_id AND state.video_type=target.video_type
 AND state.last_error_type IS NULL AND state.last_success_at>=j.requested_at
 AND EXISTS(SELECT 1 FROM public.typed_video_collection_receipts observed
 WHERE observed.video_id=state.video_id AND observed.channel_id=state.channel_id
 AND observed.receipt_kind='identity_refresh' AND observed.recorded_at=state.last_success_at)
 AND ("""+LONG_SHORT_CONFLICT_SQL.replace('%(video_id)s::text','target.video_id').replace('%(channel_id)s::text','j.channel_id')+"""))"""
_CHECKED="""UPDATE public.operator_job_targets target SET
 status=CASE WHEN ("""+_INITIAL_IDENTITY_OBSERVED+""") AND state.availability_first_missing_at IS NOT NULL THEN 'unavailable' ELSE 'checked' END,
 result_code=CASE WHEN state.last_error_type='youtube_view_count_absent' THEN 'counters_pending'
 WHEN ("""+_INITIAL_IDENTITY_OBSERVED+""") THEN CASE WHEN state.availability_first_missing_at IS NOT NULL
 THEN 'unavailable_confirmed' ELSE 'type_identity_checked' END ELSE 'publication_awaiting_completion' END,
 completed_at=now(),updated_at=now() FROM """+_RECORD+",public.video_collection_state state WHERE "+_SCOPE+"""
 AND (state.video_id=target.video_id AND state.video_type=target.video_type
 AND state.channel_id=j.channel_id AND state.typed_collection_status='awaiting_completion'
 AND state.hydration_needed AND state.typed_stream_actual_end_at IS NULL
 AND state.metadata_source_checked_at>=j.requested_at AND state.typed_collection_checked_at>=j.requested_at
 AND EXISTS(SELECT 1 FROM public.videos video WHERE video.video_id=target.video_id
 AND video.channel_id=j.channel_id AND video.publication_status IS NOT NULL
 AND video.metadata_checked_at=state.metadata_source_checked_at
 AND video.metadata_checked_at=state.typed_collection_checked_at)
 AND EXISTS(SELECT 1 FROM public.typed_video_collection_receipts receipt
 WHERE receipt.video_id=state.video_id AND receipt.channel_id=state.channel_id
 AND receipt.receipt_kind='stream_probe' AND receipt.video_type=state.video_type
 AND receipt.recorded_at=state.typed_collection_checked_at) OR (state.video_id=target.video_id AND state.channel_id=j.channel_id AND state.video_type=target.video_type
 AND state.last_error_type='youtube_view_count_absent' AND state.metadata_source_checked_at>=j.requested_at
 AND state.hydration_needed AND state.availability_first_missing_at IS NULL) OR ("""+_INITIAL_IDENTITY_OBSERVED+"""))"""
_QUOTA="SELECT %(error_code)s::text ~ 'quota_exhausted$' AS quota_error"
_REFUND="""UPDATE public.operator_job_targets target SET job_attempts=greatest(target.job_attempts-1,0),updated_at=now()
 FROM """+_RECORD+",public.video_collection_state state WHERE "+_SCOPE+""" AND state.video_id=target.video_id
 AND state.last_error_type='youtube_quota_exhausted' AND state.next_due_at>now() AND state.updated_at>=target.updated_at
 AND NOT target.video_id=ANY(%(unstarted)s::text[])"""
_RATE_REFUND = _REFUND.replace("state.last_error_type='youtube_quota_exhausted'",
    "state.last_error_type='provider_rate_limited'") + """
 AND NOT coalesce((%(summary)s::jsonb->>'settlement_unknown')::boolean,false)"""
_UNSTARTED_REFUND="""UPDATE public.operator_job_targets target SET job_attempts=greatest(target.job_attempts-1,0),updated_at=now()
 FROM """+_RECORD+" WHERE "+_SCOPE+" AND target.video_id=ANY(%(unstarted)s::text[])"
_EXHAUSTED="""UPDATE public.operator_job_targets target SET status='error',result_code='retry_limit_reached',completed_at=now(),updated_at=now()
 FROM """+_RECORD+" WHERE "+_SCOPE+""" AND target.job_attempts>=j.max_target_attempts
 AND NOT(j.job_type='channel_initial_long_inventory' AND j.initial_scope_version=1)
 AND NOT coalesce((%(summary)s::jsonb->>'settlement_unknown')::boolean,false)"""
_COUNTS=_J+"""SELECT count(*) FILTER(WHERE status='pending')::integer AS pending_count,
 count(*) FILTER(WHERE status='updated')::integer AS updated_count,count(*) FILTER(WHERE status='skipped')::integer AS skipped_count,
 count(*) FILTER(WHERE status='unavailable')::integer AS unavailable_count,count(*) FILTER(WHERE status='error')::integer AS error_count
 FROM public.operator_job_targets WHERE job_id=(SELECT id FROM j)"""
_FULL_SUMMARY="SELECT (%(summary)s::jsonb-'full_hydration_chunk')::text AS summary"
_DECIDE=_J+"""SELECT CASE
 WHEN %(pending_count)s::integer=0 AND NOT(j.initial_scope_version=1 AND j.summary_json ? 'full_retry_after') THEN 'completed'
 WHEN j.job_type<>'channel_initial_long_inventory' AND j.quota_units_reserved>=j.max_quota_units THEN 'blocked'
 ELSE 'queued' END AS next_status,now() AS as_of FROM j"""
_RETURNING=" RETURNING to_jsonb(job)::text AS job,to_jsonb(job) AS value"
_UPDATE="""UPDATE public.operator_jobs job SET status=%(next_status)s::text,
 retry_not_before=CASE WHEN %(quota_error)s::boolean AND %(next_status)s::text='queued'
 THEN greatest(now()+interval '1 hour',%(reset_at)s::timestamptz)
 WHEN %(rate_limited)s::boolean AND %(next_status)s::text='queued' THEN now()+interval '1 minute'
 WHEN %(next_status)s::text='queued' AND job.job_type='channel_initial_long_inventory' AND job.initial_scope_version=1
 THEN """+INITIAL_JOB_NEXT_DUE_SQL+""" END,
 processed_count=job.target_count-%(pending_count)s::integer,updated_count=%(updated_count)s::integer,
 skipped_count=%(skipped_count)s::integer,unavailable_count=%(unavailable_count)s::integer,error_count=%(error_count)s::integer,
 quota_units_observed_estimate=job.quota_units_observed_estimate+greatest(coalesce(%(observed_quota_units)s::integer,0),0),
 summary_json=CASE WHEN cardinality(job.initial_video_types)=3 THEN
 (coalesce(job.summary_json,'{}'::jsonb)-CASE WHEN job.initial_scope_version=1 THEN 'full_hydration_chunk' ELSE '__unused__' END)
 ||coalesce(%(summary)s::jsonb,'{}'::jsonb)
 ELSE coalesce(%(summary)s::jsonb,'{}'::jsonb) END,
 last_error_code=CASE WHEN nullif(btrim(%(error_code)s::text),'') IS NULL THEN job.last_error_code ELSE left(btrim(%(error_code)s::text),120) END,
 last_error_summary=CASE WHEN %(next_status)s::text='blocked' THEN 'Job stopped because its configured quota reservation cap was exhausted.'
 WHEN %(quota_error)s::boolean THEN 'Waiting for the YouTube daily quota to reset.'
 WHEN nullif(btrim(%(error_code)s::text),'') IS NOT NULL THEN 'The last chunk reported a sanitized component error'||chr(59)||' retry policy remains bounded.'
 ELSE job.last_error_summary END,updated_at=now(),finished_at=CASE WHEN %(next_status)s::text IN('completed','blocked') THEN now() END,
 heartbeat_at=now(),lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 FROM """+_RECORD+" WHERE job.id=j.id"+_RETURNING
_FORMATS="""SELECT jsonb_object_agg(format.video_type,jsonb_build_object(
 'inventory_ready',coalesce((j.summary_json->'inventory_results'->format.video_type->>'inventory_ready')::boolean,false),
 'initial_check_complete',j.phase<>'inventory' AND j.target_count=(SELECT count(*) FROM public.operator_job_targets WHERE job_id=j.id)
 AND counts.pending_count=0 AND counts.error_count=0,
 'current_profile_ready',j.phase<>'inventory' AND j.target_count=(SELECT count(*) FROM public.operator_job_targets WHERE job_id=j.id)
 AND counts.pending_count=0 AND counts.error_count=0 AND counts.checked_count=0
 AND coalesce((j.summary_json->'inventory_results'->format.video_type->>'inventory_ready')::boolean,false),
 'total',counts.total_count,'updated',counts.updated_count,'unavailable',counts.unavailable_count,'checked',counts.checked_count,
 'pending',counts.pending_count,'errors',counts.error_count))::text AS formats
 FROM """+_RECORD+""" CROSS JOIN LATERAL unnest(j.initial_video_types) format(video_type)
 CROSS JOIN LATERAL(SELECT count(*) AS total_count,count(*) FILTER(WHERE target.status IN('updated','skipped')) AS updated_count,
 count(*) FILTER(WHERE target.status='unavailable') AS unavailable_count,count(*) FILTER(WHERE target.status='checked') AS checked_count,
 count(*) FILTER(WHERE target.status='pending') AS pending_count,count(*) FILTER(WHERE target.status='error') AS error_count
 FROM public.operator_job_targets target WHERE target.job_id=j.id AND target.video_type=format.video_type) counts"""
_FORMAT_UPDATE="""UPDATE public.operator_jobs job SET summary_json=coalesce(job.summary_json,'{}'::jsonb)||jsonb_build_object(
 'scope','all','video_types',to_jsonb(j.initial_video_types),'per_format',%(formats)s::jsonb,
 'checked_count',(SELECT count(*) FROM public.operator_job_targets WHERE job_id=j.id AND status='checked'),
 'current_profile_ready',(SELECT coalesce(bool_and((entry.value->>'current_profile_ready')::boolean),false)
 FROM jsonb_each(%(formats)s::jsonb) entry)) FROM """+_RECORD+" WHERE job.id=j.id"+_RETURNING
_VERIFY="""UPDATE public.operator_jobs job SET status='queued',phase='verify',finished_at=NULL,updated_at=now(),heartbeat_at=now(),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,
 summary_json=coalesce(job.summary_json,'{}'::jsonb)||jsonb_build_object('current_profile_ready',false)
 WHERE job.id=%(job_id)s::uuid"""+_RETURNING


def _reject(message):raise DatabaseServerError(message,code='P0001')


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation(name)
        with database.transaction(Operation(name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _held(tx,args,*,hydration=False):
    held=tx.fetch_one(_JOB,args)
    if held is None or held['valid'] is not True or (hydration and(held['job_type']!='channel_initial_long_inventory' or held['phase']!='hydrate_videos')):
        _reject('operator_job_lease_lost')
    return held


def _epoch(held):
    if held['epoch'] is None or held['accepted_epoch']!=held['epoch']:_reject('operator_job_lease_lost')


def _settle(tx,args):
    held=_held(tx,args)
    args={**args,'job':held['job'],'chunk':None}
    if held['job_type']=='channel_initial_long_inventory':
        if held['phase']!='hydrate_videos':_reject('operator_job_initial_phase_invalid')
        assert_full_initial_scope(tx,held['job'],True)
    if held['version']==1:
        if tx.fetch_one(_CHUNK_VALID,args)['invalid'] is True:_reject('full_initial_chunk_invalid')
        args['chunk']=tx.fetch_one(_CHUNK,args)['ids']
    if args['unstarted']:
        if (held['job_type']!='channel_initial_long_inventory' or held['version']!=1
                or not set(args['unstarted']).issubset(set(args['chunk'] or []))):
            _reject('operator_job_unstarted_scope_invalid')
    if held['all_types'] is True:tx.execute(_IDENTITY,args)
    tx.execute(_REFRESHED,args)
    tx.execute(_UNAVAILABLE,args)
    if held['all_types'] is True:tx.execute(_CHECKED,args)
    args['quota_error']=tx.fetch_one(_QUOTA,args)['quota_error']
    args['rate_limited']=args['error_code'] in {'provider_rate_limited','video_collection_rate_limited'}
    if args['quota_error'] is True:tx.execute(_REFUND,args)
    if args['rate_limited']:tx.execute(_RATE_REFUND,args)
    if args['unstarted']:tx.execute(_UNSTARTED_REFUND,args)
    tx.execute(_EXHAUSTED,args)
    args.update(tx.fetch_one(_COUNTS,args))
    # Match the retained IF boundary. Putting scalar JSONB subtraction in an
    # untaken SQL CASE lets the planner fold it and reject legacy summaries.
    if held['version']==1:args['summary']=tx.fetch_one(_FULL_SUMMARY,args)['summary']
    decision=tx.fetch_one(_DECIDE,args)
    args['next_status']=decision['next_status']
    args['reset_at']=youtube_quota_reset_at(decision['as_of']) if args['quota_error'] is True and args['next_status']=='queued' else None
    result=tx.returning_one(_UPDATE,args)
    if held['all_types'] is True:
        formats=tx.fetch_one(_FORMATS,{'job':result['job']})['formats']
        result=tx.returning_one(_FORMAT_UPDATE,{'job':result['job'],'formats':formats})
    _epoch(held)
    return result['value'],held


def _arguments(tx,job_id,lease_token,observed_quota_units,error_code,summary):
    if summary is _DEFAULT_SUMMARY:summary={}
    value=summary.obj if isinstance(summary,Jsonb) else summary
    requested=isinstance(value,Mapping) and 'hydration_unstarted_video_ids' in value
    unstarted=value.get('hydration_unstarted_video_ids') if requested else []
    if requested:
        if (not isinstance(unstarted,list) or len(unstarted)>400
                or any(not isinstance(v,str) or not v or v!=v.strip() for v in unstarted)
                or len(set(unstarted))!=len(unstarted)):
            _reject('operator_job_unstarted_scope_invalid')
        # This is a one-settlement command, not persistent run summary state.
        summary={key:item for key,item in value.items() if key!='hydration_unstarted_video_ids'}
    encoded=summary if isinstance(summary,Jsonb) else Jsonb(summary) if summary is not None else None
    result=tx.fetch_one(_INPUT,(job_id,lease_token,observed_quota_units,error_code,encoded))
    return {**result,'unstarted':list(unstarted)}


def settle_operator_job_chunk(database,job_id,lease_token,observed_quota_units=0,error_code=None,summary=_DEFAULT_SUMMARY,*,transaction=None):
    """Settle one existing job token; an explicit repeat loses the cleared lease."""
    with _transaction(database,transaction,'settle_operator_job_chunk') as tx:
        args=_arguments(tx,job_id,lease_token,observed_quota_units,error_code,summary)
        value,held=_settle(tx,args)
        assert_channel_scope(tx,value['channel_id'],held['epoch'])
        return value


def settle_initial_hydration_job_chunk(database,job_id,lease_token,observed_quota_units=0,error_code=None,summary=_DEFAULT_SUMMARY,*,transaction=None):
    """Hydration adds strict counters/error syntax and completed-to-verify routing."""
    with _transaction(database,transaction,'settle_initial_hydration_job_chunk') as tx:
        args=_arguments(tx,job_id,lease_token,observed_quota_units,error_code,summary)
        held=_held(tx,args,hydration=True)
        validation=tx.fetch_one(_HYDRATION_VALID,args)
        if validation['invalid_units'] is True:_reject('operator_job_hydration_settlement_invalid')
        if validation['invalid_error'] is True:_reject('operator_job_error_code_invalid')
        value,_=_settle(tx,args)
        if value['status']=='completed':value=tx.returning_one(_VERIFY,args)['value']
        _epoch(held)
        assert_channel_scope(tx,held['channel_id'],held['epoch'])
        return value
