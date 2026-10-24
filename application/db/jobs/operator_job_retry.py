"""Owner-confirmed initial inventory retries.

Durable command receipts replay before current eligibility/source checks. Fresh
legacy work preserves its separate target snapshots, while full retries keep
targets untouched. Native arithmetic/opaque JSON remain in PostgreSQL; the
accepted job/control CAS is the final legacy owner, never an implicit retry.
"""
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from .operator_job_enqueuing import _accept
from .operator_job_inventory_evidence import _NativeObject
from .operator_job_inventory_page_settlement import _CHANNEL_ID,_CHANNEL,_JOB
from .operator_job_scope import assert_full_initial_scope
from ..channel.channel_workflow_updates import apply_channel_stage
from ..base.postgres import DatabaseServerError,Operation

_INPUT="""SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS request_id,
 %s::integer AS max_items,%s::integer AS max_pages,%s::integer AS max_quota_units,
 %s::boolean AS confirmed,%s::bigint AS expected_epoch"""
_J="jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"
_VERSION='SELECT initial_scope_version AS version FROM '+_J
_REQUEST="""SELECT r.request_control_epoch IS NULL AS legacy_epoch_missing,r.initial_scope_version<>1 AS not_full,
 r.request_control_epoch IS DISTINCT FROM %(expected_epoch)s::bigint
 OR r.max_items IS DISTINCT FROM %(max_items)s::integer OR r.max_pages IS DISTINCT FROM %(max_pages)s::integer
 OR r.max_quota_units IS DISTINCT FROM %(max_quota_units)s::integer AS conflict
 FROM operator_private.operator_job_retry_requests r WHERE r.job_id=%(job_id)s::uuid AND r.retry_request_id=%(request_id)s::uuid"""
_FULL_AVAILABLE="""SELECT j.status NOT IN('blocked','error','suspended')
 OR coalesce((SELECT c.is_hidden FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id),true)
 OR j.accepted_epoch IS DISTINCT FROM %(expected_epoch)s::bigint
 OR NOT EXISTS(SELECT 1 FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id
 AND c.initial_job_id=j.id AND c.control_epoch=%(expected_epoch)s::bigint) AS invalid,
 j.phase='inventory' AND j.last_error_code='initial_inventory_restart_required' AS restart,
 j.targets_frozen_at IS NOT NULL AS frozen FROM """+_J
_INSERT_FULL="""INSERT INTO operator_private.operator_job_retry_requests(job_id,retry_request_id,max_items,max_pages,
 max_quota_units,request_control_epoch,initial_scope_version)
 VALUES(%(job_id)s::uuid,%(request_id)s::uuid,NULL,NULL,NULL,%(expected_epoch)s::bigint,1)"""
_FULL_UPDATE="""UPDATE public.operator_jobs j SET status='queued',finished_at=NULL,
 phase=CASE WHEN targets_frozen_at IS NOT NULL THEN 'hydrate_videos' ELSE 'inventory' END,
 summary_json=summary_json||jsonb_build_object('confirmed',true,'last_retry_request_id',%(request_id)s::uuid,
 'full_retry_requested_at',clock_timestamp(),'full_retry_after',''),last_error_code=NULL,last_error_summary=NULL,
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_SOURCE="""SELECT j.requested_at IS NULL OR NOT isfinite(j.requested_at) OR j.requested_at>clock_timestamp()
 OR j.requested_at<clock_timestamp()-interval '696 hours' AS invalid FROM """+_J
_HIDDEN="SELECT coalesce((SELECT is_hidden FROM public.channel_collection_state WHERE channel_id=%s::text),true) AS hidden"
_CAP="""SELECT coalesce(%(max_items)s::integer,0)<=0 OR coalesce(%(max_pages)s::integer,0)<=0
 OR %(max_pages)s::integer>2000 OR coalesce(%(max_quota_units)s::integer,0)<=0 AS invalid"""
_NORMALIZE="""SELECT jsonb_set(to_jsonb(j),'{status}',to_jsonb(CASE
 WHEN j.status='completed' AND j.phase='verify' AND j.error_count>0 THEN 'error'
 WHEN j.status IN('queued','running') AND j.accepted_epoch IS DISTINCT FROM %(expected_epoch)s::bigint THEN 'suspended'
 ELSE j.status END))::text AS job FROM """+_J
_AVAILABLE="""SELECT j.status NOT IN('blocked','error','suspended') AS invalid,
 %(max_items)s::integer<j.items_processed OR %(max_pages)s::integer<j.pages_processed
 OR(j.phase='inventory' AND j.resume_page_token IS NOT NULL AND %(max_pages)s::integer<=j.pages_processed) AS below,
 j.status='error' AND j.phase='verify' AND j.last_error_code='initial_hydration_target_count_mismatch' AS mismatch,
 j.phase,j.target_count FROM """+_J
_STALE="""UPDATE public.operator_jobs stale SET status='suspended',finished_at=coalesce(stale.finished_at,now()),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,last_error_code='channel_sync_not_ready',
 last_error_summary='Channel control generation changed.' WHERE stale.channel_id=%(channel_id)s::text
 AND stale.id<>%(job_id)s::uuid AND stale.status IN('queued','running') AND stale.accepted_epoch IS DISTINCT FROM %(expected_epoch)s::bigint"""
_ACTIVE="""SELECT EXISTS(SELECT 1 FROM public.operator_jobs active WHERE active.channel_id=%(channel_id)s::text
 AND active.id<>%(job_id)s::uuid AND active.status IN('queued','running') AND active.accepted_epoch=%(expected_epoch)s::bigint) AS present"""
_INSERT="""INSERT INTO operator_private.operator_job_retry_requests(job_id,retry_request_id,max_items,max_pages,max_quota_units,request_control_epoch)
 VALUES(%(job_id)s::uuid,%(request_id)s::uuid,%(max_items)s::integer,%(max_pages)s::integer,%(max_quota_units)s::integer,%(expected_epoch)s::bigint)"""
_MISMATCH_OWNER="""SELECT coalesce((SELECT c.is_hidden FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id),true)
 OR j.lease_token IS NOT NULL OR j.lease_owner IS NOT NULL OR j.lease_expires_at IS NOT NULL AS invalid FROM """+_J
_EVIDENCE="""SELECT coalesce(jsonb_typeof(j.summary_json->'hydration_target_count')<>'number',true)
 OR coalesce((j.summary_json->>'hydration_target_count')!~'^[0-9]+$',true) AS invalid FROM """+_J
_VERIFIED="SELECT (j.summary_json->>'hydration_target_count')::integer AS value FROM "+_J
_COUNT='SELECT count(*)::integer AS value FROM public.operator_job_targets WHERE job_id=%(job_id)s::uuid'
_RESET="""UPDATE public.operator_job_targets SET status='pending',job_attempts=0,result_code=NULL,completed_at=NULL,updated_at=now()
 WHERE job_id=%(job_id)s::uuid AND status='error'"""
_COUNTS="""SELECT count(*)::integer AS total_count,count(*) FILTER(WHERE status='pending')::integer AS pending_count,
 count(*) FILTER(WHERE status='updated')::integer AS updated_count,count(*) FILTER(WHERE status='skipped')::integer AS skipped_count,
 count(*) FILTER(WHERE status='unavailable')::integer AS unavailable_count,count(*) FILTER(WHERE status='error')::integer AS error_count
 FROM public.operator_job_targets WHERE job_id=%(job_id)s::uuid"""
_REQUIRED="""SELECT greatest(j.quota_units_reserved+2*((%(pending_count)s::integer+49)/50),j.quota_units_observed_estimate,
 j.pages_processed+2*((%(verified_count)s::integer+49)/50)) AS value FROM """+_J
_MISMATCH_UPDATE="""UPDATE public.operator_jobs job SET status='queued',
 phase=CASE WHEN %(pending_count)s::integer>0 THEN 'hydrate_videos' ELSE 'verify' END,
 target_count=%(verified_count)s::integer,processed_count=%(verified_count)s::integer-%(pending_count)s::integer,
 updated_count=%(updated_count)s::integer,skipped_count=%(skipped_count)s::integer,
 unavailable_count=%(unavailable_count)s::integer,error_count=%(error_count)s::integer,
 finished_at=NULL,updated_at=now(),heartbeat_at=now(),max_items=%(max_items)s::integer,max_pages=%(max_pages)s::integer,
 max_quota_units=%(max_quota_units)s::integer,quota_units_estimated=greatest(job.quota_units_estimated,%(required_quota)s::integer),
 last_error_code=NULL,last_error_summary=NULL,summary_json=coalesce(job.summary_json,'{}'::jsonb)||jsonb_build_object(
 'current_profile_ready',false,'retry_confirmed',true,'retry_request_id',%(request_id)s::uuid,
 'retry_max_items',%(max_items)s::integer,'retry_max_pages',%(max_pages)s::integer,'retry_max_quota_units',%(max_quota_units)s::integer,
 'workset_repaired',true,'workset_repair_source','verified_frozen_operator_job_targets',
 'workset_repair_previous_target_count',j.target_count,'workset_repair_reset_count',%(reset_count)s::integer,
 'hydration_target_count',%(verified_count)s::integer,'hydration_processed_count',%(verified_count)s::integer-%(pending_count)s::integer,
 'hydration_error_count',%(error_count)s::integer,'target_count_matches',true),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 FROM """+_J+" WHERE job.id=j.id RETURNING to_jsonb(job)::text AS job"
_TARGET_INSERT="""INSERT INTO public.operator_job_targets(job_id,video_id) SELECT %(job_id)s::uuid,inventory.video_id
 FROM public.video_inventory inventory WHERE inventory.channel_id=%(channel_id)s::text AND inventory.video_type='long'
 ON CONFLICT(job_id,video_id) DO NOTHING"""
_PENDING="""SELECT count(*)::integer AS actual_count,count(*) FILTER(WHERE status='pending')::integer AS pending_count
 FROM public.operator_job_targets WHERE job_id=%(job_id)s::uuid"""
_CURRENT_COUNTS="""SELECT j.target_count,greatest(j.target_count-j.processed_count,0) AS pending_count FROM """+_J
_LEGACY_UPDATE="""UPDATE public.operator_jobs job SET status='queued',
 phase=CASE WHEN job.phase IN('hydrate_videos','verify','initial_stats') THEN 'hydrate_videos' ELSE job.phase END,
 target_count=%(target_count)s::integer,processed_count=greatest(%(actual_count)s::integer-%(pending_count)s::integer,0),
 finished_at=NULL,updated_at=now(),max_items=%(max_items)s::integer,max_pages=%(max_pages)s::integer,
 max_quota_units=%(max_quota_units)s::integer,
 quota_units_estimated=greatest(job.quota_units_estimated,job.pages_processed+2*((%(target_count)s::integer+49)/50)),
 error_count=0,last_error_code=NULL,last_error_summary=NULL,
 summary_json=coalesce(job.summary_json,'{}'::jsonb)||jsonb_build_object('current_profile_ready',false,'retry_confirmed',true,
 'retry_request_id',%(request_id)s::uuid,'retry_max_items',%(max_items)s::integer,
 'retry_max_pages',%(max_pages)s::integer,'retry_max_quota_units',%(max_quota_units)s::integer),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE job.id=%(job_id)s::uuid RETURNING to_jsonb(job)::text AS job,job.phase"""
_CHANNEL_FINISH="""UPDATE public.channels channel SET initial_long_inventory_status=CASE WHEN cardinality(j.initial_video_types)=3
 AND j.summary_json->'inventory_results'->'long'->>'status'='not_available' THEN 'skipped_no_long_playlist' ELSE 'completed' END,
 initial_long_inventory_started_at=NULL,initial_28d_stats_status='pending',initial_28d_stats_started_at=NULL
 FROM """+_J+" WHERE channel.channel_id=j.channel_id"
_CHANNEL_PENDING="""UPDATE public.channels channel SET initial_long_inventory_status=CASE WHEN cardinality(j.initial_video_types)=3
 AND j.inventory_video_type<>'long' AND j.summary_json->'inventory_results' ? 'long' THEN
 CASE WHEN j.summary_json->'inventory_results'->'long'->>'status'='not_available' THEN 'skipped_no_long_playlist' ELSE 'completed' END
 ELSE 'pending' END,initial_long_inventory_started_at=NULL FROM """+_J+" WHERE channel.channel_id=j.channel_id"
_RESULT='SELECT to_jsonb(j)::text AS value FROM public.operator_jobs j WHERE id=%s::uuid'


def _fail(code):raise DatabaseServerError(code,code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('retry_channel_initial_inventory_job_v5')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _full(tx,args):
    request=tx.fetch_one(_REQUEST,args)
    if request is not None:
        if request['not_full'] is True or request['conflict'] is True:_fail('operator_job_retry_request_conflict')
        return _NativeObject(args['job'])
    if any(args[key] is not None for key in ('max_items','max_pages','max_quota_units')):_fail('full_initial_limits_must_be_null')
    if args['confirmed'] is not True:_fail('operator_job_confirmation_required')
    state=tx.fetch_one(_FULL_AVAILABLE,args)
    if state['invalid'] is True:_fail('operator_job_retry_not_available')
    if state['restart'] is True:_fail('initial_inventory_restart_required')
    assert_full_initial_scope(tx,args['job'],state['frozen'])
    tx.execute(_INSERT_FULL,args)
    result=tx.returning_one(_FULL_UPDATE,args)
    assert_channel_scope(tx,args['channel_id'],args['expected_epoch'])
    return _NativeObject(result['value'])


def _mismatch(tx,args):
    if tx.fetch_one(_MISMATCH_OWNER,args)['invalid'] is True:_fail('operator_job_retry_not_available')
    if tx.fetch_one(_EVIDENCE,args)['invalid'] is True:_fail('initial_hydration_workset_evidence_missing')
    args['verified_count']=tx.fetch_one(_VERIFIED,args)['value']
    before=tx.fetch_one(_COUNT,args)['value']
    if before!=args['verified_count']:_fail('initial_hydration_workset_changed_after_verify')
    if before>args['max_items']:_fail('operator_job_retry_cap_below_observed')
    args['reset_count']=tx.execute(_RESET,args)
    args.update(tx.fetch_one(_COUNTS,args))
    if args['total_count']!=args['verified_count']:_fail('initial_hydration_workset_repair_incomplete')
    args['required_quota']=tx.fetch_one(_REQUIRED,args)['value']
    result=tx.returning_one(_MISMATCH_UPDATE,args)
    apply_channel_stage(tx,'retry_finish',(result['job'],))


def _legacy(tx,args,state):
    phase=state['phase']
    if phase=='initial_stats':tx.execute(_TARGET_INSERT,args)
    if phase in ('hydrate_videos','verify','initial_stats'):
        tx.execute(_RESET,args)
        args.update(tx.fetch_one(_PENDING,args))
        args['target_count']=args['actual_count'] if phase=='initial_stats' else state['target_count']
    else:
        current=tx.fetch_one(_CURRENT_COUNTS,args)
        args.update(current)
        args['actual_count']=current['target_count']
    result=tx.returning_one(_LEGACY_UPDATE,args)
    apply_channel_stage(tx,'retry_pending' if result['phase']=='inventory' else 'retry_finish',(result['job'],))


def retry_channel_initial_inventory_job_v5(database,job_id,retry_request_id,max_items,max_pages,max_quota_units,
                                         confirmed=False,expected_epoch=None,*,transaction=None):
    """Retry one explicit owner command; recorded command identities reconcile."""
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(job_id,retry_request_id,max_items,max_pages,max_quota_units,confirmed,expected_epoch))
        if args['expected_epoch'] is None or args['expected_epoch']<=0:_fail('channel_collection_scope_invalid')
        if args['request_id'] is None:_fail('operator_job_retry_request_id_required')
        channel=tx.fetch_one(_CHANNEL_ID,(args['job_id'],))
        if channel is None:_fail('operator_job_retry_not_available')
        args['channel_id']=channel['channel_id']
        if tx.fetch_one(_CHANNEL,(args['channel_id'],)) is None:_fail('operator_job_retry_not_available')
        row=tx.fetch_one(_JOB,args)
        if row is None:_fail('operator_job_retry_not_available')
        args['job']=row['value']
        if tx.fetch_one(_VERSION,args)['version']==1:return _full(tx,args)
        request=tx.fetch_one(_REQUEST,args)
        if request is not None:
            if request['legacy_epoch_missing'] is True:_fail('operator_job_retry_legacy_scope_unverifiable')
            if request['conflict'] is True:_fail('operator_job_retry_request_conflict')
            return _NativeObject(args['job'])
        if tx.fetch_one(_SOURCE,args)['invalid'] is True:_fail('operator_job_source_scope_expired')
        if args['confirmed'] is not True:_fail('operator_job_confirmation_required')
        if tx.fetch_one(_HIDDEN,(args['channel_id'],))['hidden'] is True:_fail('operator_job_retry_not_available')
        if tx.fetch_one(_CAP,args)['invalid'] is True:_fail('operator_job_retry_cap_invalid')
        args['job']=tx.fetch_one(_NORMALIZE,args)['job']
        state=tx.fetch_one(_AVAILABLE,args)
        if state['invalid'] is True:_fail('operator_job_retry_not_available')
        if state['below'] is True:_fail('operator_job_retry_cap_below_observed')
        tx.execute(_STALE,args)
        if tx.fetch_one(_ACTIVE,args)['present'] is True:_fail('operator_job_channel_has_active_job')
        tx.execute(_INSERT,args)
        if state['mismatch'] is True:_mismatch(tx,args)
        else:_legacy(tx,args,state)
        _accept(tx,dict(id=args['job_id'],channel_id=args['channel_id'],expected_epoch=args['expected_epoch']))
        return _NativeObject(tx.fetch_one(_RESULT,(args['job_id'],))['value'])
