"""Inventory-job failure acknowledgement.

Exact failed/settled-absence acknowledgements replay before domain locks. New
inventory failures require a live wall-clock checkpoint and durable evidence;
other phases deliberately retain their token-only, no-expiry ownership rule.
Opaque snapshots, native count/retry errors and the late generation fence stay
in one owned or borrowed transaction. Unknown COMMIT outcomes are never
retried.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_SQL)
from contextlib import contextmanager
from collection_policy import youtube_quota_reset_at
from ..channel.channel_metadata_batch import _RECORD_ERROR
from ..base.operation_contracts import get_operation
from .operator_job_inventory_evidence import initial_inventory_checkpoint_evidence,_NativeObject,_encoded
from .operator_job_inventory_page_settlement import (
    settle_initial_inventory_job_chunk,_CHANNEL_ID,_CHANNEL,_JOB,_REPLAY_RESULT,_ABSENT_CHANNEL,_CHANNEL_PENDING,_fence)
from ..base.postgres import DatabaseServerError,Operation

_INPUT="""SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS lease_token,
 %s::text AS error_code,%s::boolean AS playlist_not_found"""
_VALIDATE="SELECT %s::text IS NULL OR %s::text !~ '^[a-z0-9_]{1,120}$' AS invalid"
_HASH="""SELECT md5(jsonb_build_array(%(job_id)s::uuid,%(lease_token)s::uuid,
 %(error_code)s::text,coalesce(%(playlist_not_found)s::boolean,false))::text) AS value"""
_REPLAY="""SELECT to_jsonb(j)::text AS job,
 j.summary_json->'inventory_ack'->>'kind'='settled' AND coalesce(%(playlist_not_found)s::boolean,false)
 AND j.summary_json->'inventory_ack'->>'outcome'='playlist_not_found'
 AND j.summary_json->'inventory_ack'->>'job_lease_token'=%(lease_token)s::uuid::text AS settled,
 j.summary_json->'inventory_ack'->>'error_code' IS DISTINCT FROM %(error_code)s::text AS settled_conflict,
 j.summary_json->'inventory_ack'->>'kind'='failed'
 AND j.summary_json->'inventory_ack'->>'job_lease_token'=%(lease_token)s::uuid::text AS failed,
 j.summary_json->'inventory_ack'->>'request_hash' IS DISTINCT FROM %(request_hash)s::text AS failed_conflict
 FROM public.operator_jobs j WHERE j.id=%(job_id)s::uuid"""
_LEASE=("""SELECT j.phase,j.initial_scope_version AS version,j.lease_channel_epoch AS epoch,j.accepted_epoch,
 j.status<>'running' OR j.lease_token IS DISTINCT FROM %(lease_token)s::uuid
 OR (j.phase='inventory' AND j.summary_json ? 'inventory_chain_version')
 OR coalesce((SELECT c.is_hidden FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id),true)
 OR NOT coalesce((SELECT c.typed_runtime_eligible
 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ c WHERE c.channel_id=j.channel_id),false) AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j""")
_CHECKPOINT="""SELECT j.lease_expires_at IS NULL OR j.lease_expires_at<=clock_timestamp()
 OR j.inventory_checkpoint='{}'::jsonb AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_RESERVED="""SELECT (j.inventory_checkpoint->>'reserved_units')::integer AS reserved,
 j.inventory_checkpoint->>'phase' AS checkpoint_phase,
 jsonb_build_object('transition_id',j.inventory_checkpoint->'transition_id')::text AS summary
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_ABSENCE_CHECK="""SELECT %(evidence)s::jsonb->>'kind' IS DISTINCT FROM 'failure'
 OR %(evidence)s::jsonb->>'outcome' IS DISTINCT FROM 'playlist_not_found'
 OR NOT (%(evidence)s::jsonb->>'format_absent')::boolean
 OR %(error_code)s::text IS DISTINCT FROM %(evidence)s::jsonb->>'error_code' AS invalid"""
_FLAGS="""SELECT %(error_code)s::text='initial_inventory_deferred' AS deferred,
 %(error_code)s::text='initial_inventory_outcome_unknown' AS uncertain,
 %(evidence)s::jsonb->>'kind'='quota' AS quota_deferred,%(evidence)s::jsonb->>'kind' AS kind,
 %(evidence)s::jsonb->>'outcome' AS outcome,%(evidence)s::jsonb->>'error_code' AS evidence_error"""
_UNCERTAIN="SELECT NOT %s::boolean OR (%s::jsonb->>'bound_live')::boolean AS invalid"
_ERROR_COUNT="""SELECT j.error_count+CASE WHEN %(quota_deferred)s::boolean OR %(deferred)s::boolean OR %(rate_limited)s::boolean THEN 0 ELSE 1 END AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_NEXT="""SELECT CASE WHEN %(quota_deferred)s::boolean OR %(deferred)s::boolean OR %(rate_limited)s::boolean THEN 'queued'
 WHEN %(error_count)s::integer>=j.max_target_attempts THEN 'error' ELSE 'queued' END AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_RESTART="""SELECT scan.status='failed' AS value FROM public.channel_video_inventory_scans scan
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j
 WHERE scan.scan_id=j.inventory_scan_id AND scan.channel_id=j.channel_id AND scan.video_type=j.inventory_video_type"""
_RETRY="""SELECT to_jsonb(CASE WHEN %(quota_deferred)s::boolean THEN (%(evidence)s::jsonb->>'retry_not_before')::timestamptz
 WHEN %(deferred)s::boolean THEN (%(evidence)s::jsonb->>'retry_not_before')::timestamptz
 WHEN %(rate_limited)s::boolean AND isfinite((%(evidence)s::jsonb->>'retry_not_before')::timestamptz)
 THEN greatest(clock_timestamp()+interval '1 second',least(clock_timestamp()+interval '1 minute',
 (%(evidence)s::jsonb->>'retry_not_before')::timestamptz)) END)#>>'{}' AS value"""
_ACK="""SELECT jsonb_build_object('version',1,'kind','failed','channel_id',j.channel_id,'video_type',j.inventory_video_type,
 'transition_id',j.inventory_checkpoint->'transition_id','job_lease_token',%(lease_token)s::uuid,'scan_id',j.inventory_scan_id,
 'playlist_lease_token',j.inventory_checkpoint->'playlist_lease_token','request_hash',%(request_hash)s::text,
 'provider_attempts',%(attempts)s::integer,'quota_estimated',true,
 'outcome',CASE WHEN %(deferred)s::boolean THEN 'deferred' WHEN %(uncertain)s::boolean THEN 'unknown'
 WHEN %(quota_deferred)s::boolean THEN 'quota' ELSE 'error' END,
 'error_code',CASE WHEN %(deferred)s::boolean THEN NULL ELSE %(error_code)s::text END)::text AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_UPDATE_INVENTORY="""UPDATE public.operator_jobs j SET status=%(next_status)s::text,error_count=%(error_count)s::integer,
 retry_not_before=%(retry)s::timestamptz,finished_at=CASE WHEN %(next_status)s::text='error' THEN now() END,
 quota_units_reserved=greatest(quota_units_reserved-%(reserved)s::integer+%(attempts)s::integer,0),
 quota_units_observed_estimate=quota_units_observed_estimate+%(attempts)s::integer,
 inventory_checkpoint='{}'::jsonb,resume_page_token=NULL,
 summary_json=coalesce(summary_json,'{}'::jsonb)||jsonb_build_object('inventory_ack',%(ack)s::jsonb,'inventory_attempt_estimated',%(uncertain)s::boolean),
 last_error_code=CASE WHEN %(restart)s::boolean THEN 'initial_inventory_restart_required' WHEN %(deferred)s::boolean THEN NULL ELSE %(error_code)s::text END,
 last_error_summary=%(message)s::text,heartbeat_at=now(),updated_at=now(),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_KEEP_LONG="""SELECT cardinality(j.initial_video_types)=3 AND j.inventory_video_type<>'long'
 AND j.summary_json->'inventory_results' ? 'long' AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_ALL_TYPES="SELECT cardinality(j.initial_video_types)=3 AS value FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"
_LEGACY_ABSENT="""UPDATE public.operator_jobs j SET status='completed',finished_at=now(),updated_at=now(),
 last_error_code='long_playlist_not_found',last_error_summary=%s::text,
 summary_json=coalesce(summary_json,'{}'::jsonb)||jsonb_build_object('outcome','skipped_no_long_playlist'),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%s::uuid RETURNING to_jsonb(j)::text AS value"""
_LEGACY_COUNT="""SELECT %(error_code)s::text ~ 'quota_exhausted$' AS quota_deferred,
 j.error_count+CASE WHEN %(error_code)s::text ~ 'quota_exhausted$' OR %(error_code)s::text='provider_rate_limited' THEN 0 ELSE 1 END AS error_count,
 j.max_target_attempts,now() AS as_of FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_UPDATE_LEGACY="""UPDATE public.operator_jobs j SET status=%(next_status)s::text,error_count=%(error_count)s::integer,
 retry_not_before=CASE WHEN %(quota_deferred)s::boolean THEN greatest(now()+interval '1 hour',%(reset)s::timestamptz)
 WHEN %(error_code)s::text='provider_rate_limited' THEN now()+interval '1 minute' END,
 finished_at=CASE WHEN %(next_status)s::text='error' THEN now() END,last_error_code=%(error_code)s::text,last_error_summary=%(message)s::text,
 heartbeat_at=now(),updated_at=now(),lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_EXHAUSTED='Initial inventory exhausted its bounded retry policy.'
_QUOTA='Waiting for the YouTube daily quota to reset.'
_RETRY_MESSAGE='Initial inventory chunk failed safely and will retry.'
_RESTART_MESSAGE='The inventory source cannot resume safely; explicitly start a new initial inventory job.'
_UNKNOWN='The interrupted inventory attempt has no durable outcome; its cost is estimated.'
_ABSENT_MESSAGE='The long-form playlist disappeared; no further inventory requests are needed.'


def _fail(code):raise DatabaseServerError(code,code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('fail_initial_inventory_job_chunk')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _channel(tx,args):
    if args['next_status']=='error':
        channel=args['channel_id']
        if channel is None or not channel.strip(' '):_fail('channel_sync_channel_required')
        code='initial_inventory_restart_required' if args.get('restart') is True else args['error_code'][:64]
        if tx.returning_one(_RECORD_ERROR,(code,channel)) is None:_fail('channel_sync_channel_not_found')
    else:tx.execute(_CHANNEL_PENDING,(args['channel_id'],))


def _inventory(database,tx,args):
    if tx.fetch_one(_CHECKPOINT,(args['job'],))['invalid'] is True:_fail('initial_inventory_job_checkpoint_mismatch')
    evidence=initial_inventory_checkpoint_evidence(tx,args['job'])
    args['evidence']=_encoded(evidence)
    args.update(tx.fetch_one(_RESERVED,(args['job'],)))
    if args['playlist_not_found'] is True:
        if tx.fetch_one(_ABSENCE_CHECK,args)['invalid'] is True:_fail('initial_inventory_absence_receipt_required')
        return settle_initial_inventory_job_chunk(database,args['job_id'],args['lease_token'],args['reserved'],0,0,0,0,None,
            args['summary'],transaction=tx)
    args.update(tx.fetch_one(_FLAGS,args))
    if args['checkpoint_phase']=='prepared':
        if args['deferred'] is not True:_fail('initial_inventory_unbound_failure_invalid')
        args['attempts']=0
    elif args['kind']=='page':_fail('initial_inventory_page_requires_job_settlement')
    elif args['quota_deferred'] is True:
        if args['error_code']!='initial_inventory_quota_exhausted':_fail('initial_inventory_quota_failure_mismatch')
        args['attempts']=1
    elif args['kind']=='failure':
        if args['error_code']!=args['evidence_error']:_fail('initial_inventory_failure_code_mismatch')
        args['attempts']=1
    else:
        if tx.fetch_one(_UNCERTAIN,(args['uncertain'],args['evidence']))['invalid'] is True:_fail('initial_inventory_outcome_unresolved')
        args['attempts']=args['reserved']
    args['rate_limited']=(args['kind']=='failure' and args['outcome']=='error'
                          and args['evidence_error']=='provider_rate_limited')
    if args['rate_limited']:
        # Exact source receipt must prove resumability; no replay from an unknown attempt.
        if evidence.get('scan_retained') is not True:_fail('initial_inventory_outcome_unresolved')
    args['error_count']=tx.fetch_one(_ERROR_COUNT,args)['value']
    args['next_status']=tx.fetch_one(_NEXT,args)['value']
    args['restart']=False
    if args['version']==1 and args['kind']=='failure' and args['outcome']=='error':
        row=tx.fetch_one(_RESTART,(args['job'],))
        args['restart']=row['value'] if row else None
        if args['restart'] is True:args['next_status']='error'
    args['retry']=tx.fetch_one(_RETRY,args)['value']
    if args['rate_limited'] and args['retry'] is None:_fail('initial_inventory_outcome_unresolved')
    args['ack']=tx.fetch_one(_ACK,args)['value']
    args['message']=(_RESTART_MESSAGE if args['restart'] is True else None if args['deferred'] is True else
        _QUOTA if args['quota_deferred'] is True else _UNKNOWN if args['uncertain'] is True else
        _EXHAUSTED if args['next_status']=='error' else _RETRY_MESSAGE)
    result=_NativeObject(tx.returning_one(_UPDATE_INVENTORY,args)['value'])
    # Retained IF NOT(null) skips this branch, unlike the legacy IF null return.
    if tx.fetch_one(_KEEP_LONG,(_encoded(result),))['value'] is False:_channel(tx,args)
    _fence(tx,args)
    return result


def fail_initial_inventory_job_chunk(database,job_id,lease_token,error_code,playlist_not_found=False,*,transaction=None):
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(job_id,lease_token,error_code,playlist_not_found))
        if tx.fetch_one(_VALIDATE,(args['error_code'],args['error_code']))['invalid'] is True:_fail('operator_job_error_code_invalid')
        args['request_hash']=tx.fetch_one(_HASH,args)['value']
        replay=tx.fetch_one(_REPLAY,args)
        if replay is not None and (replay['settled'] is True or replay['failed'] is True):
            if replay['settled_conflict' if replay['settled'] is True else 'failed_conflict'] is True:_fail('initial_inventory_job_ack_conflict')
            return _NativeObject(tx.fetch_one(_REPLAY_RESULT,(replay['job'],))['value'])
        row=tx.fetch_one(_CHANNEL_ID,(args['job_id'],))
        if row is None:_fail('operator_job_lease_lost')
        args['channel_id']=row['channel_id']
        if tx.fetch_one(_CHANNEL,(args['channel_id'],)) is None:_fail('operator_job_lease_lost')
        row=tx.fetch_one(_JOB,args)
        if row is None:_fail('operator_job_lease_lost')
        args['job']=row['value']
        args.update(tx.fetch_one(_LEASE,args))
        if args['invalid'] is True:_fail('operator_job_lease_lost')
        if args['phase']=='inventory':return _inventory(database,tx,args)
        if tx.fetch_one(_ALL_TYPES,(args['job'],))['value'] is True and args['playlist_not_found'] is True:
            _fail('all_format_playlist_absence_requires_inventory_settlement')
        if args['playlist_not_found'] is True:
            result=_NativeObject(tx.returning_one(_LEGACY_ABSENT,(_ABSENT_MESSAGE,args['job_id']))['value'])
            tx.execute(_ABSENT_CHANNEL,(args['channel_id'],))
            _fence(tx,args)
            return result
        args.update(tx.fetch_one(_LEGACY_COUNT,args))
        args['next_status']='queued' if args['error_code']=='provider_rate_limited' else 'error' if args['error_count']>=args['max_target_attempts'] else 'queued'
        args['reset']=youtube_quota_reset_at(args['as_of']) if args['quota_deferred'] is True else None
        args['message']=_EXHAUSTED if args['next_status']=='error' else _QUOTA if args['quota_deferred'] is True else _RETRY_MESSAGE
        result=_NativeObject(tx.returning_one(_UPDATE_LEGACY,args)['value'])
        if tx.fetch_one(_KEEP_LONG,(_encoded(result),))['value'] is not True:_channel(tx,args)
        _fence(tx,args)
        return result
