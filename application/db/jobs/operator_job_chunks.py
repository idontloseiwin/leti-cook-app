"""Operator target chunk/initial-hydration protocols.

Job lock, bounded target page, native status/counter/JSONB updates and final
captured channel generation share one transaction. Intrinsic target/source
scope guards remain active. No video lease is acquired here and no provider is
called.
"""
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scope
from ..videos.video_initial_policy import INITIAL_RETRY_ALLOWED_SQL, INITIAL_JOB_NEXT_DUE_SQL
from ..base.operation_contracts import get_operation
from .operator_job_inventory_evidence import _NativeObject,_encoded
from .operator_job_scope import assert_full_initial_scope
from ..videos.video_queue_initialization import LONG_SHORT_CONFLICT_SQL
from ..base.postgres import DatabaseServerError,Operation

_INPUT="SELECT %s::uuid::text AS job_id,%s::uuid::text AS lease_token,%s::integer AS limit,%s::integer AS lease_seconds"
_LOCK="""SELECT to_jsonb(j)::text AS value,j.status<>'running' OR j.lease_token IS DISTINCT FROM %s::uuid
 OR j.lease_expires_at<=now() AS invalid FROM public.operator_jobs j WHERE id=%s::uuid FOR UPDATE"""
_JOB="SELECT * FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb)"
_REPAIR_CLOCK="""SELECT j.summary_json->>'full_retry_after' AS after_id,
 to_jsonb((j.summary_json->>'full_retry_requested_at')::timestamptz)#>>'{}' AS cutoff FROM ("""+_JOB+") j"
_REPAIR_IDS="""SELECT coalesce(array_agg(video_id ORDER BY video_id),ARRAY[]::text[]) AS ids FROM (
 SELECT video_id FROM public.operator_job_targets WHERE job_id=%(job_id)s::uuid AND status='error'
 AND video_id>%(after_id)s::text AND completed_at<=%(cutoff)s::timestamptz ORDER BY video_id LIMIT %(limit)s::integer) q"""
_REPAIR_TARGETS="""UPDATE public.operator_job_targets SET status='pending',job_attempts=0,result_code=NULL,
 completed_at=NULL,updated_at=now() WHERE job_id=%s::uuid AND video_id=ANY(%s::text[])"""
_REPAIR_JOB="""UPDATE public.operator_jobs j SET summary_json=CASE WHEN cardinality(%(ids)s::text[])<%(limit)s::integer
 THEN summary_json-'full_retry_after'-'full_retry_requested_at'
 ELSE jsonb_set(summary_json,'{full_retry_after}',to_jsonb((%(ids)s::text[])[cardinality(%(ids)s::text[])])) END
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_FULL_CHUNK="""SELECT coalesce(array_agg(video_id ORDER BY video_id),ARRAY[]::text[]) AS ids FROM (
 SELECT t.video_id FROM public.operator_job_targets t JOIN public.operator_jobs j ON j.id=t.job_id
 LEFT JOIN public.video_collection_state state USING(video_id)
 WHERE t.job_id=%s::uuid AND t.status='pending' AND(state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND """+INITIAL_RETRY_ALLOWED_SQL+"""
 ORDER BY t.video_id LIMIT %s::integer) part"""
_FILTER="target.job_id=j.id AND target.status='pending' AND (j.initial_scope_version=0 OR target.video_id=ANY(%(chunk)s::text[]))"
# Frozen video IDs stay fixed. Only this fresh automatic same-channel policy
# may normalize a provisional Long/Short target after General supplied duration.
_IDENTITY="""UPDATE public.operator_job_targets target SET
 video_type=coalesce(classification.automatic_type,target.video_type),
 status=CASE WHEN classification.automatic_type IS NULL THEN 'error' ELSE target.status END,
 result_code=CASE WHEN classification.automatic_type IS NULL THEN 'frozen_identity_changed' ELSE target.result_code END,
 completed_at=CASE WHEN classification.automatic_type IS NULL THEN now() ELSE target.completed_at END,
 updated_at=now() FROM (WITH classification AS MATERIALIZED (
 SELECT target.job_id,target.video_id,automatic.automatic_type
 FROM public.operator_job_targets target CROSS JOIN ("""+_JOB+""") j
 LEFT JOIN public.video_inventory inventory ON inventory.video_id=target.video_id AND inventory.channel_id=j.channel_id
 LEFT JOIN public.video_type_automatic_candidates automatic ON automatic.video_id=inventory.video_id
 AND automatic.channel_id=inventory.channel_id AND automatic.automatic_type=inventory.video_type
 AND target.video_type IN ('long','short') AND j.initial_scope_version=1
 WHERE """+_FILTER+""" AND NOT EXISTS(SELECT 1 FROM public.video_inventory current_identity
 WHERE current_identity.video_id=target.video_id AND current_identity.channel_id=j.channel_id
 AND current_identity.video_type=target.video_type))
 SELECT * FROM classification) classification
 WHERE target.job_id=classification.job_id AND target.video_id=classification.video_id"""
_REFRESHED="""UPDATE public.operator_job_targets target SET status='skipped',result_code='already_refreshed',
 completed_at=now(),updated_at=now() FROM public.video_collection_state state,("""+_JOB+") j WHERE "+_FILTER+"""
 AND NOT state.hydration_needed AND state.video_id=target.video_id AND state.stats_source_checked_at>=j.requested_at AND state.metadata_source_checked_at>=j.requested_at
 AND (cardinality(j.initial_video_types)=1 OR (state.channel_id=j.channel_id AND state.video_type=target.video_type
 AND (target.video_type='long' OR state.typed_collection_status='hydrated')))"""
_UNAVAILABLE="""UPDATE public.operator_job_targets target SET status='unavailable',result_code='unavailable_confirmed',
 completed_at=now(),updated_at=now() FROM public.open_video_availability unavailable,("""+_JOB+") j WHERE "+_FILTER+"""
 AND unavailable.video_id=target.video_id AND unavailable.last_seen_at>=j.requested_at
 AND (cardinality(j.initial_video_types)=1 OR EXISTS(SELECT 1 FROM public.video_collection_state state
 WHERE state.video_id=target.video_id AND state.channel_id=j.channel_id AND state.video_type=target.video_type
 AND (target.video_type='long' OR (state.typed_collection_status='unavailable'
 AND state.typed_collection_checked_at>=j.requested_at))))"""
_INITIAL_IDENTITY_OBSERVED = """(j.initial_scope_version=1 AND state.video_id=target.video_id
 AND state.channel_id=j.channel_id AND state.video_type=target.video_type
 AND state.last_error_type IS NULL AND state.last_success_at>=j.requested_at
 AND EXISTS(SELECT 1 FROM public.typed_video_collection_receipts observed
 WHERE observed.video_id=state.video_id AND observed.channel_id=state.channel_id
 AND observed.receipt_kind='identity_refresh' AND observed.recorded_at=state.last_success_at)
 AND ("""+LONG_SHORT_CONFLICT_SQL.replace('%(video_id)s::text','target.video_id').replace('%(channel_id)s::text','j.channel_id')+"""))"""
_AWAITING="""UPDATE public.operator_job_targets target SET
 status=CASE WHEN ("""+_INITIAL_IDENTITY_OBSERVED+""") AND state.availability_first_missing_at IS NOT NULL THEN 'unavailable' ELSE 'checked' END,
 result_code=CASE WHEN state.last_error_type='youtube_view_count_absent' THEN 'counters_pending'
 WHEN ("""+_INITIAL_IDENTITY_OBSERVED+""") THEN CASE WHEN state.availability_first_missing_at IS NOT NULL
 THEN 'unavailable_confirmed' ELSE 'type_identity_checked' END ELSE 'publication_awaiting_completion' END,
 completed_at=now(),updated_at=now() FROM public.video_collection_state state,("""+_JOB+") j WHERE "+_FILTER+"""
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
_RETRIES="""UPDATE public.operator_job_targets target SET status='error',result_code='retry_limit_reached',
 completed_at=now(),updated_at=now() FROM ("""+_JOB+""" ) j WHERE target.job_id=j.id
 AND target.status='pending' AND target.job_attempts>=j.max_target_attempts
 AND NOT(j.job_type='channel_initial_long_inventory' AND j.initial_scope_version=1)"""
_PENDING="SELECT count(*)::integer AS count FROM public.operator_job_targets WHERE job_id=%s::uuid AND status='pending'"
_COMPLETE="""UPDATE public.operator_jobs job SET status='completed',processed_count=job.target_count,
 updated_count=counts.updated_count,skipped_count=counts.skipped_count,unavailable_count=counts.unavailable_count,
 error_count=counts.error_count,updated_at=now(),finished_at=now(),lease_token=NULL,lease_channel_epoch=NULL,
 lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=now() FROM (
 SELECT count(*) FILTER(WHERE status='updated')::integer AS updated_count,
 count(*) FILTER(WHERE status='skipped')::integer AS skipped_count,
 count(*) FILTER(WHERE status='unavailable')::integer AS unavailable_count,
 count(*) FILTER(WHERE status='error')::integer AS error_count FROM public.operator_job_targets WHERE job_id=%s::uuid) counts
 WHERE job.id=%s::uuid RETURNING to_jsonb(job)::text AS value"""
_SELECT="""SELECT coalesce(array_agg(candidate.video_id ORDER BY candidate.video_id),ARRAY[]::text[]) AS ids FROM (
 SELECT target.video_id FROM public.operator_job_targets target LEFT JOIN public.video_collection_state state
 ON state.video_id=target.video_id CROSS JOIN ("""+_JOB+") j WHERE "+_FILTER+"""
 AND (state.video_id IS NULL OR state.lease_token IS NULL OR state.lease_expires_at<=now())
 AND """+INITIAL_RETRY_ALLOWED_SQL+"""
 ORDER BY target.video_id FOR UPDATE OF target SKIP LOCKED LIMIT %(limit)s::integer) candidate"""
_CHUNK="""UPDATE public.operator_jobs j SET summary_json=jsonb_set(summary_json,'{full_hydration_chunk}',to_jsonb(%s::text[]))
 WHERE id=%s::uuid RETURNING to_jsonb(j)::text AS value"""
_DEFER="""UPDATE public.operator_jobs job SET status='queued',updated_at=now(),heartbeat_at=now(),
 retry_not_before=CASE WHEN job.job_type='channel_initial_long_inventory' AND job.initial_scope_version=1
 THEN greatest(now()+interval '5 seconds',"""+INITIAL_JOB_NEXT_DUE_SQL+""") ELSE job.retry_not_before END,
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%s::uuid RETURNING to_jsonb(job)::text AS value"""
_QUOTA="""SELECT (CASE WHEN j.job_type='channel_initial_long_inventory' AND j.initial_scope_version=1 THEN 1 ELSE 2 END)
 *((%(count)s::integer+49)/50) AS reserved,
 CASE WHEN j.job_type<>'channel_initial_long_inventory' THEN
 j.quota_units_reserved+2*((%(count)s::integer+49)/50)>j.max_quota_units ELSE false END AS blocked FROM ("""+_JOB+") j"
_BLOCK="""UPDATE public.operator_jobs job SET status='blocked',updated_at=now(),finished_at=now(),
 last_error_code='quota_reservation_exhausted',
 last_error_summary='Job stopped before a request because its configured quota reservation cap was exhausted.',
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=now()
 WHERE id=%s::uuid RETURNING to_jsonb(job)::text AS value"""
_ATTEMPTS="""UPDATE public.operator_job_targets SET job_attempts=job_attempts+1,updated_at=now()
 WHERE job_id=%s::uuid AND video_id=ANY(%s::text[])"""
_RESERVE="""UPDATE public.operator_jobs job SET quota_units_reserved=quota_units_reserved+%s::integer,
 updated_at=now(),heartbeat_at=now(),lease_expires_at=now()+make_interval(secs=>%s::integer)
 WHERE id=%s::uuid RETURNING to_jsonb(job)::text AS value"""
_FORMATS="""SELECT jsonb_object_agg(format.video_type,jsonb_build_object(
 'inventory_ready',coalesce((j.summary_json->'inventory_results'->format.video_type->>'inventory_ready')::boolean,false),
 'initial_check_complete',j.phase<>'inventory' AND j.target_count=(SELECT count(*) FROM public.operator_job_targets WHERE job_id=j.id)
 AND counts.pending_count=0 AND counts.error_count=0,
 'current_profile_ready',j.phase<>'inventory' AND j.target_count=(SELECT count(*) FROM public.operator_job_targets WHERE job_id=j.id)
 AND counts.pending_count=0 AND counts.error_count=0 AND counts.checked_count=0
 AND coalesce((j.summary_json->'inventory_results'->format.video_type->>'inventory_ready')::boolean,false),
 'total',counts.total_count,'updated',counts.updated_count,'unavailable',counts.unavailable_count,'checked',counts.checked_count,
 'pending',counts.pending_count,'errors',counts.error_count)) AS value FROM ("""+_JOB+""" ) j
 CROSS JOIN unnest(j.initial_video_types) format(video_type) CROSS JOIN LATERAL (
 SELECT count(*) AS total_count,count(*) FILTER(WHERE target.status IN('updated','skipped')) AS updated_count,
 count(*) FILTER(WHERE target.status='unavailable') AS unavailable_count,count(*) FILTER(WHERE target.status='checked') AS checked_count,
 count(*) FILTER(WHERE target.status='pending') AS pending_count,count(*) FILTER(WHERE target.status='error') AS error_count
 FROM public.operator_job_targets target WHERE target.job_id=j.id AND target.video_type=format.video_type) counts"""
_SUMMARY="""UPDATE public.operator_jobs job SET summary_json=coalesce(job.summary_json,'{}'::jsonb)||jsonb_build_object(
 'scope','all','video_types',to_jsonb(job.initial_video_types),'per_format',%(formats)s::jsonb,
 'checked_count',(SELECT count(*) FROM public.operator_job_targets WHERE job_id=job.id AND status='checked'),
 'current_profile_ready',(SELECT coalesce(bool_and((entry.value->>'current_profile_ready')::boolean),false)
 FROM jsonb_each(%(formats)s::jsonb) entry)) WHERE job.id=%(job_id)s::uuid RETURNING to_jsonb(job)::text AS value"""
_TARGETS="""SELECT coalesce(jsonb_agg(jsonb_build_object('video_id',video_id,'video_type',video_type) ORDER BY video_id),'[]'::jsonb) AS value
 FROM public.operator_job_targets WHERE job_id=%s::uuid AND video_id=ANY(%s::text[])"""
_VERIFY="""UPDATE public.operator_jobs job SET status='queued',phase='verify',finished_at=NULL,updated_at=now(),heartbeat_at=now(),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,
 summary_json=coalesce(job.summary_json,'{}'::jsonb)||jsonb_build_object('current_profile_ready',false)
 WHERE id=%s::uuid RETURNING to_jsonb(job)::text AS value"""


def _fail(code):raise DatabaseServerError(code,code='P0001')
def _value(row):return _NativeObject(row['value']) if row is not None else None


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation(name)
        with database.transaction(Operation(name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _bounds(args, *, initial=False):
    maximum=400 if initial else 250
    if args['limit'] is None or not 1<=args['limit']<=maximum:_fail('operator_job_chunk_limit_invalid')
    if args['lease_seconds'] is None or not 60<=args['lease_seconds']<=3600:_fail('operator_job_lease_seconds_invalid')


def repair_full_initial_target_chunk(transaction,job,limit):
    """Borrow the already-locked job and public caller's validated bound (400 initial, 250 manual)."""
    with transaction.borrow():
        if job['initial_scope_version']!=1 or job['targets_frozen_at'] is None or 'full_retry_after' not in job['summary_json']:return job
        values=dict(job=_encoded(job),job_id=job['id'],limit=limit)
        values.update(transaction.fetch_one(_REPAIR_CLOCK,values))
        values['ids']=transaction.fetch_one(_REPAIR_IDS,values)['ids']
        transaction.execute(_REPAIR_TARGETS,(job['id'],values['ids']))
        return _value(transaction.returning_one(_REPAIR_JOB,values))


def _formats(tx,job):
    if len(job['initial_video_types'])!=3:return job
    # Only native aggregate data crosses this small three-format boundary.
    from psycopg.types.json import Jsonb
    formats=tx.fetch_one(_FORMATS,dict(job=_encoded(job)))['value']
    return _value(tx.returning_one(_SUMMARY,dict(job_id=job['id'],formats=Jsonb(formats))))


def _result(job,epoch,accepted,**values):
    if epoch is None or accepted!=epoch:_fail('operator_job_lease_lost')
    return dict(job=job,**values,_collection_scope=dict(channel_id=job['channel_id'],control_epoch=epoch))


def _provisional(tx,args,*,initial=False):
    _bounds(args,initial=initial)
    held=tx.fetch_one(_LOCK,(args['lease_token'],args['job_id']))
    job=_value(held)
    if job is None or held['invalid'] is True:_fail('operator_job_lease_lost')
    epoch,accepted=job['lease_channel_epoch'],job['accepted_epoch']
    if job['job_type']=='channel_initial_long_inventory':
        if job['phase']!='hydrate_videos':_fail('operator_job_initial_phase_invalid')
        assert_full_initial_scope(tx,_encoded(job),True)
    chunk=[]
    if job['initial_scope_version']==1:
        job=repair_full_initial_target_chunk(tx,job,args['limit'])
        chunk=tx.fetch_one(_FULL_CHUNK,(job['id'],args['limit']))['ids']
    values=dict(job=_encoded(job),chunk=chunk,limit=args['limit'])
    typed=len(job['initial_video_types'])==3
    if typed:tx.execute(_IDENTITY,values)
    tx.execute(_REFRESHED,values)
    tx.execute(_UNAVAILABLE,values)
    if typed:tx.execute(_AWAITING,values)
    tx.execute(_RETRIES,values)
    pending=tx.fetch_one(_PENDING,(job['id'],))['count']
    if job['initial_scope_version']==1 and 'full_retry_after' in job['summary_json'] and pending==0:pending=1
    if pending==0:
        job=_formats(tx,_value(tx.returning_one(_COMPLETE,(job['id'],job['id']))))
        return _result(job,epoch,accepted,video_ids=[],reserved_units=0)
    ids=tx.fetch_one(_SELECT,values)['ids']
    if job['initial_scope_version']==1:job=_value(tx.returning_one(_CHUNK,(ids,job['id'])))
    if not ids:
        job=_formats(tx,_value(tx.returning_one(_DEFER,(job['id'],))))
        return _result(job,epoch,accepted,video_ids=[],reserved_units=0,deferred=True)
    quota=tx.fetch_one(_QUOTA,dict(job=_encoded(job),count=len(ids)))
    if quota['blocked'] is True:
        job=_formats(tx,_value(tx.returning_one(_BLOCK,(job['id'],))))
        return _result(job,epoch,accepted,video_ids=[],reserved_units=0)
    tx.execute(_ATTEMPTS,(job['id'],ids))
    job=_formats(tx,_value(tx.returning_one(_RESERVE,(quota['reserved'],args['lease_seconds'],job['id']))))
    # Retained final scope-generation failure precedes the targets result read.
    if epoch is None or accepted!=epoch:_fail('operator_job_lease_lost')
    targets=tx.fetch_one(_TARGETS,(job['id'],ids))['value']
    return _result(job,epoch,accepted,video_ids=ids,scope='all' if typed else 'long',targets=targets,reserved_units=quota['reserved'],deferred=False)


def prepare_operator_job_chunk(database,job_id,lease_token,limit=50,lease_seconds=1200,*,transaction=None):
    with _transaction(database,transaction,'prepare_operator_job_chunk') as tx:
        args=tx.fetch_one(_INPUT,(job_id,lease_token,limit,lease_seconds))
        result=_provisional(tx,args)
        assert_channel_scope(tx,result['_collection_scope']['channel_id'],result['_collection_scope']['control_epoch'])
        return {key:value for key,value in result.items() if key!='_collection_scope'}


def prepare_initial_hydration_job_chunk(database,job_id,lease_token,limit=50,lease_seconds=1200,*,transaction=None):
    with _transaction(database,transaction,'prepare_initial_hydration_job_chunk') as tx:
        args=tx.fetch_one(_INPUT,(job_id,lease_token,limit,lease_seconds))
        _bounds(args,initial=True)
        held=tx.fetch_one(_LOCK,(args['lease_token'],args['job_id']))
        job=_value(held)
        if job is None or job['job_type']!='channel_initial_long_inventory' or job['phase']!='hydrate_videos' or held['invalid'] is True:_fail('operator_job_lease_lost')
        epoch,accepted=job['lease_channel_epoch'],job['accepted_epoch']
        result=_provisional(tx,args,initial=True)
        if result['job']['status']=='completed':result['job']=_value(tx.returning_one(_VERIFY,(args['job_id'],)))
        if epoch is None or accepted!=epoch:_fail('operator_job_lease_lost')
        assert_channel_scope(tx,job['channel_id'],epoch)
        return {key:value for key,value in result.items() if key!='_collection_scope'}
