"""Initial hydration verification.

Persisted verification states reconcile under channel/job locks before current
eligibility checks. New verification uses the retained transaction-now lease,
separate proof/count/format snapshots and native JSON arithmetic. Discovery
initialization remains insert-only; the captured generation fence is last.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, CHANNEL_CONTROL_SQL, channel_eligible_row)
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from .operator_job_inventory_evidence import _NativeObject
from .operator_job_inventory_page_settlement import _CHANNEL_ID,_CHANNEL,_JOB
from .operator_job_chunk_settlement import _FORMATS as _CHUNK_FORMATS,_FORMAT_UPDATE
from .operator_job_scope import assert_full_initial_scope,_TYPE
from ..base.postgres import DatabaseServerError,Operation

_INPUT="SELECT to_jsonb(%s::uuid)#>>'{}' AS job_id,to_jsonb(%s::uuid)#>>'{}' AS lease_token"
_J="jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"
_STATE="""SELECT j.lease_channel_epoch AS epoch,j.accepted_epoch,j.initial_scope_version AS version,
 cardinality(j.initial_video_types)=3 AS all_types,
 j.lease_token IS NULL AND j.lease_owner IS NULL AND j.lease_expires_at IS NULL
 AND((j.phase='verify' AND j.status IN('completed','error') AND j.finished_at IS NOT NULL)
 OR(j.phase='hydrate_videos' AND j.status='queued')) AS replayed FROM """+_J
_LEASE=("""SELECT coalesce((SELECT c.is_hidden FROM public.channel_collection_state c WHERE c.channel_id=j.channel_id),true)
 OR NOT coalesce((SELECT c.typed_runtime_eligible
 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ c WHERE c.channel_id=j.channel_id),false)
 OR j.phase<>'verify' OR j.status<>'running' OR j.lease_token IS DISTINCT FROM %(lease_token)s::uuid
 OR j.lease_expires_at IS NULL OR j.lease_expires_at<=now() AS invalid FROM """)+_J
_TARGET_TYPE=_TYPE.replace('%(video_id)s::text','target.video_id')
# Type is provisional within the exact fresh Long+Short automatic policy;
# frozen video IDs and the owning channel are never normalized here.
_AUTOMATIC_TYPE="""SELECT automatic.automatic_type
 FROM (SELECT candidate.* FROM public.video_type_automatic_candidates candidate
 WHERE candidate.video_id=target.video_id OFFSET 0) automatic
 JOIN public.video_inventory inventory ON inventory.video_id=automatic.video_id
 AND inventory.channel_id=automatic.channel_id AND inventory.video_type=automatic.automatic_type
 WHERE automatic.channel_id=%(channel_id)s::text AND target.video_type IN ('long','short')"""
_NORMALIZE_AUTOMATIC="""UPDATE public.operator_job_targets target SET video_type=changed.video_type,updated_at=now()
 FROM (WITH changed AS MATERIALIZED (
 SELECT target.video_id,target.video_type AS previous_type,("""+_AUTOMATIC_TYPE+""") AS video_type
 FROM public.operator_job_targets target WHERE target.job_id=%(job_id)s::uuid
 AND target.status IN ('checked','updated','skipped','unavailable'))
 SELECT video_id,video_type FROM changed WHERE video_type IS NOT NULL AND video_type<>previous_type) changed
 WHERE target.job_id=%(job_id)s::uuid AND target.video_id=changed.video_id"""
# Another General may cross the duration boundary between normalization and
# this snapshot. A currently provable automatic Long/Short choice is still the
# same frozen video; all other identity changes retain the original veto.
_IDENTITY="""SELECT EXISTS(SELECT 1 FROM public.operator_job_targets target
 WHERE target.job_id=%(job_id)s::uuid AND coalesce(("""+_AUTOMATIC_TYPE+"""),target.video_type)
 IS DISTINCT FROM ("""+_TARGET_TYPE+")) AS mismatch"
_LEGACY_IDENTITY="""UPDATE public.operator_job_targets target SET status='error',result_code='frozen_identity_changed',
 completed_at=now(),updated_at=now() WHERE target.job_id=%(job_id)s::uuid AND NOT EXISTS(
 SELECT 1 FROM public.video_inventory inventory WHERE inventory.video_id=target.video_id
 AND inventory.channel_id=%(channel_id)s::text AND inventory.video_type=target.video_type)"""
_COUNTS="""SELECT count(*)::integer AS total_count,count(*) FILTER(WHERE status='pending')::integer AS pending_count,
 count(*) FILTER(WHERE status='updated')::integer AS updated_count,count(*) FILTER(WHERE status='skipped')::integer AS skipped_count,
 count(*) FILTER(WHERE status='unavailable')::integer AS unavailable_count,count(*) FILTER(WHERE status='error')::integer AS error_count
 FROM public.operator_job_targets WHERE job_id=%(job_id)s::uuid"""
_READY="""SELECT %(total_count)s::integer=j.target_count AS target_count_matches,
 NOT %(identity_mismatch)s::boolean AND %(total_count)s::integer=j.target_count
 AND %(pending_count)s::integer=0 AND %(error_count)s::integer=0 AS ready FROM """+_J
_UPDATE="""UPDATE public.operator_jobs job SET
 status=CASE WHEN %(pending_count)s::integer>0 THEN 'queued' WHEN %(ready)s::boolean THEN 'completed' ELSE 'error' END,
 phase=CASE WHEN %(pending_count)s::integer>0 THEN 'hydrate_videos' ELSE 'verify' END,
 processed_count=%(total_count)s::integer-%(pending_count)s::integer,updated_count=%(updated_count)s::integer,
 skipped_count=%(skipped_count)s::integer,unavailable_count=%(unavailable_count)s::integer,
 error_count=%(error_count)s::integer+CASE WHEN %(identity_mismatch)s::boolean THEN 1 ELSE 0 END
 +CASE WHEN %(target_count_matches)s::boolean THEN 0 ELSE 1 END,
 summary_json=coalesce(job.summary_json,'{}'::jsonb)||jsonb_build_object(
 'hydration_target_count',%(total_count)s::integer,'hydration_processed_count',%(total_count)s::integer-%(pending_count)s::integer,
 'hydration_error_count',%(error_count)s::integer,'target_count_matches',%(target_count_matches)s::boolean,
 'current_profile_ready',%(ready)s::boolean),
 last_error_code=CASE WHEN %(pending_count)s::integer>0 THEN NULL
 WHEN %(identity_mismatch)s::boolean THEN 'frozen_identity_changed'
 WHEN NOT %(target_count_matches)s::boolean THEN 'initial_hydration_target_count_mismatch'
 WHEN %(error_count)s::integer>0 THEN 'initial_hydration_target_errors' END,
 last_error_summary=CASE WHEN %(pending_count)s::integer>0 THEN NULL
 WHEN NOT %(target_count_matches)s::boolean THEN 'Initial hydration frozen target rows no longer match the persisted frozen target count.'
 WHEN %(error_count)s::integer>0 THEN 'Initial hydration has terminal target errors and is not profile-ready.' END,
 finished_at=CASE WHEN %(pending_count)s::integer>0 THEN NULL ELSE now() END,
 updated_at=now(),heartbeat_at=now(),lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL
 WHERE job.id=%(job_id)s::uuid RETURNING to_jsonb(job)::text AS job"""
# Same accepted format projection, with verification's additional identity veto.
_FORMATS=_CHUNK_FORMATS.replace("'current_profile_ready',j.phase", "'current_profile_ready',NOT %(identity_mismatch)s::boolean AND j.phase")
_CHANNEL_UPDATE="""UPDATE public.channels SET initial_28d_stats_status=CASE WHEN %(ready)s::boolean THEN 'completed'
 WHEN %(pending_count)s::integer>0 THEN 'pending' ELSE 'error' END,initial_28d_stats_started_at=NULL
 WHERE channel_id=%(channel_id)s::text"""
_DISCOVERY_CONTROL=('SELECT (SELECT policy_facts FROM (' + CHANNEL_CONTROL_FACTS_SQL + ') c WHERE c.channel_id=%s::text) AS policy_facts,control.control_epoch FROM public.channel_collection_state control WHERE control.channel_id=%s::text')
_DISCOVERY_INSERT="""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,
 next_due_at,state_origin,inventory_state_origin) VALUES(%s::text,'long','UULF'||substr(%s::text,3),%s::boolean,
 CASE WHEN %s::boolean THEN 'ready' ELSE 'paused' END,now(),'legacy_long_backfill','legacy_long_backfill')
 ON CONFLICT(channel_id,video_type) DO NOTHING"""


def _fail():raise DatabaseServerError('operator_job_lease_lost',code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('verify_initial_hydration_job')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def verify_initial_hydration_job(database,job_id,lease_token,*,transaction=None):
    """Verify once or return one already-persisted verification outcome."""
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(job_id,lease_token))
        row=tx.fetch_one(_CHANNEL_ID,(args['job_id'],))
        if row is None:_fail()
        args['channel_id']=row['channel_id']
        if tx.fetch_one(_CHANNEL,(args['channel_id'],)) is None:_fail()
        row=tx.fetch_one(_JOB,args)
        if row is None:_fail()
        args['job']=row['value']
        held=tx.fetch_one(_STATE,args)
        if held['replayed'] is True:return _NativeObject(args['job'])
        if tx.fetch_one(_LEASE,args)['invalid'] is True:_fail()
        assert_full_initial_scope(tx,args['job'],True)
        if held['version']==1:tx.execute(_NORMALIZE_AUTOMATIC,args)
        args['identity_mismatch']=tx.fetch_one(_IDENTITY,args)['mismatch'] if held['version']==1 else False
        if held['version']==0 and held['all_types'] is True:tx.execute(_LEGACY_IDENTITY,args)
        args.update(tx.fetch_one(_COUNTS,args))
        args.update(tx.fetch_one(_READY,args))
        result=tx.returning_one(_UPDATE,args)
        if held['all_types'] is True:
            formats=tx.fetch_one(_FORMATS,dict(job=result['job'],identity_mismatch=args['identity_mismatch']))['formats']
            result=tx.returning_one(_FORMAT_UPDATE,dict(job=result['job'],formats=formats))
        tx.execute(_CHANNEL_UPDATE,args)
        control=channel_eligible_row(tx.fetch_one(_DISCOVERY_CONTROL,(args['channel_id'],args['channel_id'])))
        eligible=control['eligible'] if control is not None else False
        tx.execute(_DISCOVERY_INSERT,(args['channel_id'],args['channel_id'],eligible,eligible))
        if held['epoch'] is None or held['accepted_epoch']!=held['epoch']:_fail()
        assert_channel_scope(tx,args['channel_id'],held['epoch'])
        return _NativeObject(result['job'])
