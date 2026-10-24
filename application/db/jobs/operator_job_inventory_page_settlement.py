"""Public inventory-page acknowledgement.

Exact native request hashes can replay before locks or lease/generation checks.
Fresh work owns channel, job and borrowed evidence locks in that order, uses a
post-lock wall-clock lease check, then fences the captured channel last. Native
JSON/count expressions and distinct target snapshots preserve legacy and full
scope behavior. This owner commits once and never retries an unknown outcome.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_SQL)
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scope
from ..channel.channel_metadata_batch import _RECORD_ERROR
from ..base.operation_contracts import get_operation
from .operator_job_inventory_evidence import initial_inventory_checkpoint_evidence,_NativeObject,_encoded
from .operator_job_inventory_settlement import settle_full_initial_inventory
from ..channel.channel_workflow_updates import apply_channel_stage
from ..base.postgres import DatabaseServerError,Operation

_DEFAULT=object()
_ABSENCE_MESSAGE='The long-form playlist disappeared; no further inventory requests are needed.'
_INPUT="""SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS lease_token,
 %s::integer AS reserved,%s::integer AS pages,%s::integer AS items,%s::integer AS inserted,%s::integer AS updated,
 %s::text AS next_page_token,%s::jsonb::text AS summary"""
_VALIDATE="""WITH input AS MATERIALIZED(SELECT %(summary)s::jsonb AS summary)
 SELECT summary IS NULL OR jsonb_typeof(summary)<>'object'
 OR summary-ARRAY['transition_id']<>'{}'::jsonb
 OR coalesce(summary->>'transition_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
 OR %(next_page_token)s::text IS NOT NULL OR %(reserved)s::integer IS NULL OR %(reserved)s::integer NOT IN(0,1)
 OR %(pages)s::integer IS NULL OR %(pages)s::integer NOT IN(0,1)
 OR %(items)s::integer IS NULL OR %(items)s::integer NOT BETWEEN 0 AND 50
 OR %(inserted)s::integer IS NULL OR %(inserted)s::integer NOT BETWEEN 0 AND 50
 OR %(updated)s::integer IS NULL OR %(updated)s::integer NOT BETWEEN 0 AND 50 AS invalid FROM input"""
_HASH="""SELECT to_jsonb((%(summary)s::jsonb->>'transition_id')::uuid)#>>'{}' AS transition,
 md5(jsonb_build_array(%(job_id)s::uuid,%(lease_token)s::uuid,(%(summary)s::jsonb->>'transition_id')::uuid,
 %(reserved)s::integer,%(pages)s::integer,%(items)s::integer,%(inserted)s::integer,%(updated)s::integer)::text) AS request_hash"""
_REPLAY="""SELECT to_jsonb(j)::text AS job,
 j.summary_json->'inventory_ack'->>'kind'='settled' AND j.summary_json->'inventory_ack'->>'transition_id'=%(transition)s::text AS replayed,
 j.summary_json->'inventory_ack'->>'request_hash' IS DISTINCT FROM %(request_hash)s::text AS conflict
 FROM public.operator_jobs j WHERE j.id=%(job_id)s::uuid"""
_REPLAY_RESULT="""SELECT (to_jsonb(j)||jsonb_build_object('inventory_ack_replayed',true))::text AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_CHANNEL_ID="SELECT channel_id FROM public.operator_jobs WHERE id=%s::uuid AND job_type='channel_initial_long_inventory'"
_CHANNEL="SELECT channel_id FROM public.channels WHERE channel_id=%s::text FOR UPDATE"
_JOB="""SELECT to_jsonb(j)::text AS value FROM public.operator_jobs j WHERE j.id=%(job_id)s::uuid
 AND j.channel_id=%(channel_id)s::text AND j.job_type='channel_initial_long_inventory' FOR UPDATE"""
_LEASE=("""SELECT j.lease_channel_epoch AS epoch,j.accepted_epoch AS accepted_epoch,j.initial_scope_version AS version,
 cardinality(j.initial_video_types) AS type_count,
 j.status<>'running' OR j.lease_token IS DISTINCT FROM %(lease_token)s::uuid
 OR j.lease_expires_at IS NULL OR j.lease_expires_at<=clock_timestamp()
 OR coalesce((SELECT c.is_hidden FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id),true)
 OR NOT coalesce((SELECT c.typed_runtime_eligible
 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ c WHERE c.channel_id=j.channel_id),false)
 OR (j.phase='inventory' AND j.summary_json ? 'inventory_chain_version')
 OR j.lease_channel_epoch IS NULL OR j.accepted_epoch IS DISTINCT FROM j.lease_channel_epoch AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j""")
_CHECKPOINT="""SELECT j.inventory_checkpoint->>'phase' AS checkpoint_phase,
 j.phase<>'inventory' OR j.inventory_checkpoint='{}'::jsonb
 OR j.inventory_checkpoint->>'transition_id' IS DISTINCT FROM %(transition)s::text
 OR (j.inventory_checkpoint->>'reserved_units')::integer IS DISTINCT FROM %(reserved)s::integer AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_TERMINAL="""SELECT %(reserved)s::integer<>0 OR NOT (%(evidence)s::jsonb->>'format_finished')::boolean
 OR %(pages)s::integer<>0 OR %(items)s::integer<>0 OR %(inserted)s::integer<>0 OR %(updated)s::integer<>0 AS invalid"""
_RECEIPT="""SELECT %(evidence)s::jsonb->>'kind' NOT IN('page','failure')
 OR (%(evidence)s::jsonb->>'kind'='failure' AND (%(evidence)s::jsonb->>'outcome' IS DISTINCT FROM 'playlist_not_found'
 OR NOT (%(evidence)s::jsonb->>'format_absent')::boolean)) AS invalid"""
_COUNTS="""SELECT (%(evidence)s::jsonb->>'pages_processed')::integer IS DISTINCT FROM %(pages)s::integer
 OR (%(evidence)s::jsonb->>'items_processed')::integer IS DISTINCT FROM %(items)s::integer
 OR (%(evidence)s::jsonb->>'inserted')::integer IS DISTINCT FROM %(inserted)s::integer
 OR (%(evidence)s::jsonb->>'updated')::integer IS DISTINCT FROM %(updated)s::integer AS invalid"""
_ATTEMPTS="SELECT (%s::jsonb->>'provider_attempts')::integer AS value"
_FLAGS="""SELECT (%s::jsonb->>'format_finished')::boolean AS done,(%s::jsonb->>'format_absent')::boolean AS absent"""
_ACK="""SELECT jsonb_build_object('version',1,'kind','settled','channel_id',j.channel_id,'video_type',j.inventory_video_type,
 'transition_id',%(transition)s::uuid,'job_lease_token',%(lease_token)s::uuid,'scan_id',j.inventory_scan_id,
 'playlist_lease_token',j.inventory_checkpoint->'playlist_lease_token','request_hash',%(request_hash)s::text,
 'provider_attempts',%(attempts)s::integer,'quota_estimated',true,
 'outcome',CASE WHEN %(absent)s::boolean THEN 'playlist_not_found' ELSE %(evidence)s::jsonb->>'kind' END,
 'error_code',%(evidence)s::jsonb->'error_code')::text AS value
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_ABSENT="""UPDATE public.operator_jobs j SET status='completed',finished_at=now(),updated_at=now(),
 last_error_code='long_playlist_not_found',last_error_summary=%(absence_message)s::text,
 quota_units_reserved=greatest(quota_units_reserved-%(reserved)s::integer+%(attempts)s::integer,0),
 quota_units_observed_estimate=quota_units_observed_estimate+%(attempts)s::integer,
 inventory_scan_id=NULL,inventory_checkpoint='{}'::jsonb,resume_page_token=NULL,
 summary_json=coalesce(summary_json,'{}'::jsonb)||jsonb_build_object('inventory_ack',%(ack)s::jsonb)
 ||jsonb_build_object('outcome','skipped_no_long_playlist'),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_ABSENT_CHANNEL="""UPDATE public.channels SET initial_long_inventory_status='skipped_no_long_playlist',
 initial_long_inventory_started_at=NULL,initial_long_inventory_error_code=NULL,initial_long_inventory_error_at=NULL,
 initial_long_inventory_error_count=0,initial_28d_stats_status='skipped' WHERE channel_id=%s::text"""
_TOTALS="""SELECT j.pages_processed+%(pages)s::integer AS total_pages,j.items_processed+%(items)s::integer AS total_items,
 j.target_count AS actual_targets,j.quota_units_estimated AS required_quota,j.inventory_video_type AS current_type,
 j.max_items,j.max_pages FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_CAP="""SELECT %(total_pages)s::integer>%(max_pages)s::integer OR %(total_items)s::integer>%(max_items)s::integer
 OR (%(single)s::boolean AND NOT %(done)s::boolean AND %(total_pages)s::integer>=%(max_pages)s::integer) AS blocked"""
_CATALOGUE="SELECT EXISTS(SELECT 1 FROM public.video_inventory WHERE channel_id=%s::text AND video_type=%s::text) AS present"
_RESULTS="""SELECT CASE WHEN %(done)s::boolean AND NOT %(blocked)s::boolean THEN
 coalesce(j.summary_json->'inventory_results','{}'::jsonb)||jsonb_build_object(j.inventory_video_type,
 jsonb_build_object('status',CASE WHEN %(absent)s::boolean THEN 'not_available' ELSE 'completed' END,
 'complete',true,'inventory_ready',NOT %(absent)s::boolean)) ELSE coalesce(j.summary_json->'inventory_results','{}'::jsonb) END::text AS results,
 CASE WHEN %(done)s::boolean AND NOT %(blocked)s::boolean THEN CASE j.inventory_video_type WHEN 'long' THEN 'short'
 WHEN 'short' THEN 'stream' ELSE 'stream' END ELSE j.inventory_video_type END AS next_type,
 %(done)s::boolean AND j.inventory_video_type='stream' AND NOT %(blocked)s::boolean AS finished
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_IDENTITY="""SELECT EXISTS(SELECT 1 FROM public.video_inventory_memberships observed
 LEFT JOIN public.video_inventory canonical ON canonical.video_id=observed.video_id
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j
 WHERE observed.channel_id=j.channel_id AND observed.membership_status='present' AND observed.video_type=ANY(j.initial_video_types)
 AND (canonical.video_id IS NULL OR canonical.video_type IS NULL OR canonical.channel_id IS DISTINCT FROM observed.channel_id
 OR (canonical.video_type IS DISTINCT FROM observed.video_type AND NOT EXISTS(SELECT 1 FROM public.video_type_review_cases review
 WHERE review.video_id=canonical.video_id AND review.channel_id=canonical.channel_id AND review.decision_type=canonical.video_type
 AND review.blocked_reason IS NULL AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships foreign_evidence
 WHERE foreign_evidence.video_id=canonical.video_id AND foreign_evidence.membership_status='present'
 AND foreign_evidence.channel_id<>canonical.channel_id))))) AS invalid"""
_INSERT_ALL="""INSERT INTO public.operator_job_targets(job_id,video_id,video_type)
 SELECT j.id,i.video_id,i.video_type FROM public.video_inventory i
 CROSS JOIN jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j
 WHERE i.channel_id=j.channel_id AND i.video_type=ANY(j.initial_video_types) ON CONFLICT(job_id,video_id) DO NOTHING"""
_INSERT_LONG="""INSERT INTO public.operator_job_targets(job_id,video_id)
 SELECT %s::uuid,i.video_id FROM public.video_inventory i WHERE i.channel_id=%s::text AND i.video_type='long'
 ON CONFLICT(job_id,video_id) DO NOTHING"""
_TARGET_COUNT="SELECT count(*)::integer AS value FROM public.operator_job_targets WHERE job_id=%s::uuid"
_QUOTA="SELECT %(total_pages)s::integer+2*((%(actual_targets)s::integer+49)/50) AS value"
_TARGET_CAP="""SELECT CASE WHEN %(single)s::boolean THEN %(blocked)s::boolean OR %(actual_targets)s::integer>%(max_items)s::integer
 ELSE %(actual_targets)s::integer>%(max_items)s::integer END AS value"""
_UPDATE_ALL="""UPDATE public.operator_jobs j SET status=CASE WHEN %(blocked)s::boolean THEN 'blocked' ELSE 'queued' END,
 phase=CASE WHEN %(finished)s::boolean THEN 'hydrate_videos' ELSE 'inventory' END,inventory_video_type=%(next_type)s::text,
 inventory_scan_id=CASE WHEN %(done)s::boolean THEN NULL ELSE j.inventory_scan_id END,inventory_checkpoint='{}'::jsonb,
 resume_page_token=CASE WHEN %(current_type)s::text='long' AND NOT %(done)s::boolean THEN %(next_page_token)s::text END,
 pages_processed=%(total_pages)s::integer,items_processed=%(total_items)s::integer,
 target_count=CASE WHEN %(finished)s::boolean THEN %(actual_targets)s::integer ELSE j.target_count END,
 processed_count=CASE WHEN %(finished)s::boolean THEN 0 ELSE %(total_items)s::integer END,
 inventory_inserted_count=j.inventory_inserted_count+%(inserted)s::integer,inventory_updated_count=j.inventory_updated_count+%(updated)s::integer,
 quota_units_estimated=%(required_quota)s::integer,quota_units_reserved=greatest(j.quota_units_reserved-%(reserved)s::integer+%(attempts)s::integer,0),
 quota_units_observed_estimate=j.quota_units_observed_estimate+%(attempts)s::integer,
 summary_json=coalesce(j.summary_json,'{}'::jsonb)||jsonb_build_object('inventory_ack',%(ack)s::jsonb)
 ||jsonb_build_object('scope','all','video_types',to_jsonb(j.initial_video_types),'inventory_results',%(results)s::jsonb,
 'inventory_complete',%(finished)s::boolean AND NOT %(blocked)s::boolean,
 'hydration_target_count',CASE WHEN %(finished)s::boolean THEN %(actual_targets)s::integer ELSE 0 END,
 'target_count_matches',CASE WHEN %(finished)s::boolean THEN true END,'current_profile_ready',false,'deferred',NOT %(done)s::boolean),
 last_error_code=%(code)s::text,last_error_summary=CASE WHEN %(blocked)s::boolean THEN 'Initial inventory requires review before continuing.' END,
 finished_at=CASE WHEN %(blocked)s::boolean THEN now() END,updated_at=now(),heartbeat_at=now(),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_UPDATE_LONG="""UPDATE public.operator_jobs j SET status=CASE WHEN %(blocked)s::boolean THEN 'blocked' ELSE 'queued' END,
 phase=CASE WHEN %(finished)s::boolean THEN 'hydrate_videos' ELSE 'inventory' END,
 resume_page_token=CASE WHEN NOT %(finished)s::boolean THEN %(next_page_token)s::text END,
 inventory_scan_id=CASE WHEN %(finished)s::boolean THEN NULL ELSE j.inventory_scan_id END,inventory_checkpoint='{}'::jsonb,
 pages_processed=%(total_pages)s::integer,items_processed=%(total_items)s::integer,
 target_count=CASE WHEN %(finished)s::boolean THEN %(actual_targets)s::integer ELSE j.target_count END,
 processed_count=CASE WHEN %(finished)s::boolean THEN 0 ELSE %(total_items)s::integer END,
 updated_count=CASE WHEN %(finished)s::boolean THEN 0 ELSE j.updated_count END,
 skipped_count=CASE WHEN %(finished)s::boolean THEN 0 ELSE j.skipped_count END,
 unavailable_count=CASE WHEN %(finished)s::boolean THEN 0 ELSE j.unavailable_count END,
 error_count=CASE WHEN %(finished)s::boolean THEN 0 ELSE j.error_count END,
 inventory_inserted_count=j.inventory_inserted_count+%(inserted)s::integer,inventory_updated_count=j.inventory_updated_count+%(updated)s::integer,
 quota_units_estimated=%(required_quota)s::integer,quota_units_reserved=greatest(j.quota_units_reserved-%(reserved)s::integer+%(attempts)s::integer,0),
 quota_units_observed_estimate=j.quota_units_observed_estimate+%(attempts)s::integer,
 summary_json=coalesce(j.summary_json,'{}'::jsonb)||jsonb_build_object('inventory_ack',%(ack)s::jsonb)
 ||jsonb_build_object('pages_processed',%(total_pages)s::integer,'items_processed',%(total_items)s::integer,
 'hydration_target_count',CASE WHEN %(finished)s::boolean THEN %(actual_targets)s::integer ELSE 0 END,
 'target_count_matches',CASE WHEN %(finished)s::boolean THEN true END,
 'inventory_complete',%(finished)s::boolean AND NOT %(blocked)s::boolean,'current_profile_ready',false),
 last_error_code=CASE WHEN %(blocked)s::boolean THEN 'initial_hydration_estimate_exceeded' END,
 last_error_summary=CASE WHEN %(blocked)s::boolean THEN 'Observed inventory or complete hydration exceeded an explicitly confirmed job cap.' END,
 finished_at=CASE WHEN %(blocked)s::boolean THEN now() END,heartbeat_at=now(),updated_at=now(),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE id=%(job_id)s::uuid RETURNING to_jsonb(j)::text AS value"""
_HAS_LONG="SELECT %s::jsonb ? 'long' AS value"
_CHANNEL_ALL="""UPDATE public.channels SET initial_long_inventory_status=CASE WHEN %s::jsonb->'long'->>'status'='not_available'
 THEN 'skipped_no_long_playlist' ELSE 'completed' END,initial_long_inventory_started_at=NULL,initial_28d_stats_status='pending'
 WHERE channel_id=%s::text"""
_CHANNEL_FINISH="""UPDATE public.channels SET initial_long_inventory_status='completed',initial_long_inventory_started_at=NULL,
 initial_28d_stats_status='pending' WHERE channel_id=%s::text"""
_CHANNEL_PENDING="UPDATE public.channels SET initial_long_inventory_status='pending',initial_long_inventory_started_at=NULL WHERE channel_id=%s::text"


def _fail(code):raise DatabaseServerError(code,code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('settle_initial_inventory_job_chunk')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _fence(tx,args):
    if args['epoch'] is None or args['accepted_epoch']!=args['epoch']:_fail('operator_job_lease_lost')
    assert_channel_scope(tx,args['channel_id'],args['epoch'])


def _count_frozen(tx,args,*,single):
    args['actual_targets']=tx.fetch_one(_TARGET_COUNT,(args['job_id'],))['value']
    args['required_quota']=tx.fetch_one(_QUOTA,args)['value']
    args['single']=single
    args['blocked']=tx.fetch_one(_TARGET_CAP,args)['value']


def _legacy(tx,args):
    if args['type_count']==1 and args['absent'] is True:
        args['absence_message']=_ABSENCE_MESSAGE
        result=_NativeObject(tx.returning_one(_ABSENT,args)['value'])
        apply_channel_stage(tx,'page_absent',(args['channel_id'],))
        _fence(tx,args)
        return result
    args.update(tx.fetch_one(_TOTALS,args))
    args['single']=args['type_count']!=3
    args['blocked']=tx.fetch_one(_CAP,args)['blocked']
    if args['type_count']==3:
        args['code']='initial_hydration_estimate_exceeded' if args['blocked'] is True else None
        if args['absent'] is True and tx.fetch_one(_CATALOGUE,(args['channel_id'],args['current_type']))['present'] is True:
            args.update(blocked=True,code='initial_inventory_absence_conflicts_with_catalogue')
        args.update(tx.fetch_one(_RESULTS,args))
        if args['finished'] is True:
            if tx.fetch_one(_IDENTITY,(args['job'],))['invalid'] is True:
                args.update(blocked=True,finished=False,code='initial_inventory_identity_unresolved')
            else:
                tx.execute(_INSERT_ALL,(args['job'],))
                _count_frozen(tx,args,single=False)
                if args['blocked'] is True:args['code']='initial_hydration_estimate_exceeded'
        result=_NativeObject(tx.returning_one(_UPDATE_ALL,args)['value'])
        if tx.fetch_one(_HAS_LONG,(args['results'],))['value'] is True:apply_channel_stage(tx,'page_all',(args['results'],args['channel_id']))
    else:
        args['finished']=args['done']
        if args['finished'] is True:
            tx.execute(_INSERT_LONG,(args['job_id'],args['channel_id']))
            _count_frozen(tx,args,single=True)
        result=_NativeObject(tx.returning_one(_UPDATE_LONG,args)['value'])
        if args['finished'] is True:apply_channel_stage(tx,'page_finish',(args['channel_id'],))
        elif args['blocked'] is True:
            if args['channel_id'] is None or not args['channel_id'].strip(' '):_fail('channel_sync_channel_required')
            if apply_channel_stage(tx,'record_error',('initial_hydration_estimate_exceeded',args['channel_id'])) is None:_fail('channel_sync_channel_not_found')
        else:apply_channel_stage(tx,'page_pending',(args['channel_id'],))
    _fence(tx,args)
    return result


def settle_initial_inventory_job_chunk(database,job_id,lease_token,reserved_units,pages_processed,items_processed,
                                      inserted,updated,next_page_token,summary=_DEFAULT,*,transaction=None):
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(job_id,lease_token,reserved_units,pages_processed,items_processed,inserted,updated,
            next_page_token,_encoded({} if summary is _DEFAULT else summary)))
        if tx.fetch_one(_VALIDATE,args)['invalid'] is True:_fail('operator_job_inventory_settlement_invalid')
        args.update(tx.fetch_one(_HASH,args))
        replay=tx.fetch_one(_REPLAY,args)
        if replay is not None and replay['replayed'] is True:
            if replay['conflict'] is True:_fail('initial_inventory_job_ack_conflict')
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
        checkpoint=tx.fetch_one(_CHECKPOINT,args)
        if checkpoint['invalid'] is True:_fail('initial_inventory_job_checkpoint_mismatch')
        args['evidence']=_encoded(initial_inventory_checkpoint_evidence(tx,args['job']))
        if checkpoint['checkpoint_phase']=='prepared':
            if tx.fetch_one(_TERMINAL,args)['invalid'] is True:_fail('initial_inventory_terminal_evidence_missing')
            args['attempts']=0
        else:
            if tx.fetch_one(_RECEIPT,args)['invalid'] is True:_fail('initial_inventory_job_receipt_required')
            if tx.fetch_one(_COUNTS,args)['invalid'] is True:_fail('initial_inventory_job_receipt_counts_mismatch')
            args['attempts']=tx.fetch_one(_ATTEMPTS,(args['evidence'],))['value']
        args.update(tx.fetch_one(_FLAGS,(args['evidence'],args['evidence'])))
        args['ack']=tx.fetch_one(_ACK,args)['value']
        if args['version']==1:
            result=settle_full_initial_inventory(tx,args['job'],args['evidence'],args['ack'],
                *(args[key] for key in ('reserved','pages','items','inserted','updated','attempts')))
            assert_channel_scope(tx,result['channel_id'],args['epoch'])
            return result
        return _legacy(tx,args)
