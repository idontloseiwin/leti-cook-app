"""Borrowed full-initial target reconciliation and inventory finish.

The composing public writer must own the job lock, transaction budget and final
channel fence. These private-protocol replacements retain their own read order,
native count/JSONB arithmetic and transaction clock. They do not acquire a
second connection, add retries, call retained business functions or broaden
privileges.
"""
from .operator_job_inventory_evidence import _NativeObject, _encoded
from .operator_job_scope import assert_full_initial_scope, _TYPE
from ..base.postgres import DatabaseServerError

_INPUT='SELECT %s::uuid AS job_id,%s::integer AS limit'
_JOB='SELECT to_jsonb(j)::text AS value FROM public.operator_jobs j WHERE id=%s::uuid'
_DONE="""SELECT count(*)=3 AND coalesce(bool_and(coalesce(value->>'terminal_outcome'
 IN('completed','playlist_not_found'),false)),false) AS value
 FROM jsonb_each(%s::jsonb->'summary_json'->'full_inventory_sources')"""
_COUNT='SELECT count(*)::integer AS value FROM public.operator_job_targets WHERE job_id=%s::uuid'
_DECISION="""SELECT CASE WHEN j.target_count>10000 AND NOT coalesce((j.summary_json->>'confirmed')::boolean,false)
 THEN 'initial_inventory_confirmation_required'
 WHEN EXISTS(SELECT 1 FROM public.operator_job_targets t WHERE t.job_id=j.id AND t.video_type IS NULL)
 THEN 'initial_inventory_identity_unresolved' END AS code
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_BLOCK="""UPDATE public.operator_jobs j SET status='blocked',last_error_code=%s::text,
 last_error_summary='Initial inventory requires owner review before hydration.',finished_at=now(),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
 WHERE id=%s::uuid RETURNING to_jsonb(j)::text AS value"""
_FINISH="""UPDATE public.operator_jobs j SET targets_frozen_at=now(),phase='hydrate_videos',status='queued',processed_count=0,
 quota_units_estimated=quota_units_observed_estimate+2*((target_count+49)/50),
 updated_count=0,skipped_count=0,unavailable_count=0,error_count=0,inventory_scan_id=NULL,inventory_checkpoint='{}'::jsonb,
 last_error_code=NULL,last_error_summary=NULL,finished_at=NULL,
 summary_json=summary_json||jsonb_build_object('inventory_complete',true,'hydration_target_count',target_count,
 'target_count_matches',true,'current_profile_ready',false),
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
 WHERE id=%s::uuid RETURNING to_jsonb(j)::text AS value"""
_VALIDATE="""SELECT j.targets_frozen_at IS NOT NULL OR j.phase<>'inventory'
 OR %s::integer NOT BETWEEN 1 AND 250 AS invalid
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
# Reuse the exact scalar identity predicate, changing only the fixed video input
# to this finite batch's column. Both forms retain the same statement snapshot.
_BATCH_TYPE=_TYPE.replace('%(video_id)s::text','part.video_id')
_RESOLVE="""UPDATE public.operator_job_targets t SET video_type=resolved.video_type,updated_at=now()
 FROM (WITH part AS MATERIALIZED(SELECT t.video_id FROM public.operator_job_targets t
 WHERE t.job_id=%(job_id)s::uuid AND t.video_type IS NULL ORDER BY t.video_id LIMIT %(limit)s::integer)
 SELECT part.video_id,("""+_BATCH_TYPE+""") AS video_type FROM part) resolved
 WHERE t.job_id=%(job_id)s::uuid AND t.video_id=resolved.video_id AND resolved.video_type IS NOT NULL"""
_REMAINING="""SELECT EXISTS(SELECT 1 FROM public.operator_job_targets WHERE job_id=%s::uuid AND video_type IS NULL) AS value"""
_REQUEUE="""UPDATE public.operator_jobs j SET status='queued',lease_token=NULL,lease_channel_epoch=NULL,
 lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=%s::uuid RETURNING to_jsonb(j)::text AS value"""
# Chain finish alone opts into an actual-row lease check at the final UPDATE,
# after bounded reconciliation. Existing scalar statements remain unchanged.
_LEASE_PREDICATE="""WHERE id=%s::uuid AND j.status='running' AND j.lease_token=%s::uuid
 AND j.accepted_epoch=%s::bigint AND j.lease_channel_epoch=%s::bigint
 AND j.lease_expires_at>clock_timestamp() RETURNING"""
_GUARDED_BLOCK=_BLOCK.replace('WHERE id=%s::uuid RETURNING',_LEASE_PREDICATE)
_GUARDED_FINISH=_FINISH.replace('WHERE id=%s::uuid RETURNING',_LEASE_PREDICATE)
_GUARDED_REQUEUE=_REQUEUE.replace('WHERE id=%s::uuid RETURNING',_LEASE_PREDICATE)


def _value(row):
    return _NativeObject(row['value']) if row is not None else None


def _release(transaction,statement,parameters,lease_guard):
    if lease_guard is None:
        return _value(transaction.returning_one(statement,parameters))
    token,epoch=lease_guard
    if token is None or type(epoch) is not int:
        raise DatabaseServerError('operator_job_lease_lost',code='P0001')
    guarded={_BLOCK:_GUARDED_BLOCK,_FINISH:_GUARDED_FINISH,_REQUEUE:_GUARDED_REQUEUE}[statement]
    row=transaction.returning_one(guarded,(*parameters,token,epoch,epoch))
    if row is None:
        raise DatabaseServerError('operator_job_lease_lost',code='P0001')
    return _value(row)


def finish_full_initial_inventory(transaction,job_id,*,lease_guard=None):
    """Finish or block a job only after its complete source proof and count check."""
    tx=transaction
    with tx.borrow():
        job=_value(tx.fetch_one(_JOB,(job_id,)))
        encoded=_encoded(job)
        assert_full_initial_scope(tx,encoded,False)
        if tx.fetch_one(_DONE,(encoded,))['value'] is not True:return job
        if tx.fetch_one(_COUNT,(job['id'],))['value']!=job['target_count']:
            raise DatabaseServerError('full_initial_target_count_mismatch',code='P0001')
        code=tx.fetch_one(_DECISION,(encoded,))['code']
        return _release(tx,_BLOCK,(code,job['id']),lease_guard) if code is not None else _release(tx,_FINISH,(job['id'],),lease_guard)


def reconcile_full_initial_targets(transaction,job_id,limit=250,*,lease_guard=None):
    """Resolve the native ordered page, then yield or finish under the same owner.

    NULL deliberately retains PostgreSQL LIMIT NULL semantics from the oracle;
    callers use the default250. Tightening that inherited domain is separate.
    """
    tx=transaction
    with tx.borrow():
        args=tx.fetch_one(_INPUT,(job_id,limit))
        job=_value(tx.fetch_one(_JOB,(args['job_id'],)))
        encoded=_encoded(job)
        assert_full_initial_scope(tx,encoded,False)
        if tx.fetch_one(_VALIDATE,(args['limit'],encoded))['invalid'] is True:
            raise DatabaseServerError('full_initial_reconciliation_invalid',code='P0001')
        changed=tx.execute(_RESOLVE,dict(job_id=job['id'],channel_id=job['channel_id'],limit=args['limit']))
        remaining=tx.fetch_one(_REMAINING,(job['id'],))['value']
        if remaining is True and changed>0:return _release(tx,_REQUEUE,(job['id'],),lease_guard)
        if lease_guard is not None:return finish_full_initial_inventory(tx,job['id'],lease_guard=lease_guard)
        return finish_full_initial_inventory(tx,job['id'])
