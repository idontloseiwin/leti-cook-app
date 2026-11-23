"""Borrowed full-initial page settlement used by the public page writer.

The composing writer owns channel/job locks, checkpoint/receipt validation,
transaction budget and final captured channel fence. This private step keeps
its own proof, ordered target statements, native JSON/count arithmetic and
optional finish proof. It neither commits nor retries a received page.
"""
from .operator_job_inventory_evidence import _NativeObject, _encoded
from .operator_job_reconciliation import finish_full_initial_inventory
from .operator_job_scope import assert_full_initial_scope, _TYPE
from ..channel.channel_workflow_updates import apply_channel_stage
from ..base.postgres import DatabaseServerError

_INPUT="""SELECT to_jsonb(j)::text AS job,j.id AS job_id,j.channel_id,
 j.initial_scope_version<>1 OR j.targets_frozen_at IS NOT NULL OR j.phase<>'inventory'
 OR j.inventory_checkpoint->>'phase' IS DISTINCT FROM 'bound' AS invalid,
 %s::jsonb::text AS evidence,%s::jsonb::text AS ack,%s::integer AS reserved,
 %s::integer AS pages,%s::integer AS items,%s::integer AS inserted,
 %s::integer AS updated,%s::integer AS attempts
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_IDS="""WITH page AS(SELECT coalesce(array_agg(v ORDER BY v),ARRAY[]::text[]) AS ids
 FROM jsonb_array_elements_text(%s::jsonb->'observed_video_ids') x(v))
 SELECT cardinality(ids)>50 AS oversized,CASE WHEN cardinality(ids)<=50 THEN ids END AS ids FROM page"""
_INSERT_TYPE=_TYPE.replace('%(video_id)s::text','part.video_id')
_UPDATE_TYPE=_TYPE.replace('%(video_id)s::text','target.video_id')
_INSERT="""INSERT INTO public.operator_job_targets(job_id,video_id,video_type)
 SELECT %(job_id)s::uuid,part.video_id,("""+_INSERT_TYPE+""")
 FROM unnest(%(ids)s::text[]) part(video_id) ORDER BY part.video_id
 ON CONFLICT(job_id,video_id) DO NOTHING"""
_TARGETS="""UPDATE public.operator_job_targets target SET video_type=("""+_UPDATE_TYPE+"""),updated_at=now()
 WHERE target.job_id=%(job_id)s::uuid AND target.video_id=ANY(%(ids)s::text[])
 AND target.status='pending' AND target.job_attempts=0 AND target.video_type IS DISTINCT FROM ("""+_UPDATE_TYPE+")"
_FLAGS="""SELECT coalesce((%s::jsonb->>'format_finished')::boolean,false) AS done,
 coalesce((%s::jsonb->>'format_absent')::boolean,false) AS absent"""
_PROOF="""SELECT ((j.summary_json->'full_inventory_sources'->j.inventory_video_type)||jsonb_build_object(
 'acked_pages',(j.summary_json->'full_inventory_sources'->j.inventory_video_type->>'acked_pages')::integer+%(pages)s::integer,
 'last_transition_id',%(ack)s::jsonb->'transition_id','source_checked_at',%(evidence)s::jsonb->'source_checked_at',
 'terminal_outcome',CASE WHEN %(done)s::boolean THEN CASE WHEN %(absent)s::boolean THEN 'playlist_not_found' ELSE 'completed' END END))::text AS proof
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j"""
_RESULTS="""WITH progress AS MATERIALIZED(SELECT
 CASE WHEN %(done)s::boolean THEN coalesce(j.summary_json->'inventory_results','{}'::jsonb)||jsonb_build_object(
 j.inventory_video_type,jsonb_build_object('status',CASE WHEN %(absent)s::boolean THEN 'not_available' ELSE 'completed' END,
 'complete',true,'inventory_ready',NOT %(absent)s::boolean)) ELSE coalesce(j.summary_json->'inventory_results','{}'::jsonb) END AS results,
 CASE WHEN %(done)s::boolean THEN CASE j.inventory_video_type WHEN 'long' THEN 'short' ELSE 'stream' END ELSE j.inventory_video_type END AS next_type
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j)
 SELECT results::text AS results,results ? 'long' AS has_long,
 results ? 'stream' AS has_stream,next_type FROM progress"""
_CONFIRM="""SELECT CASE WHEN j.target_count+%s::integer>10000 AND NOT coalesce((j.summary_json->>'confirmed')::boolean,false)
 THEN 'initial_inventory_confirmation_required' END AS code
 FROM jsonb_populate_record(NULL::public.operator_jobs,%s::jsonb) j"""
_UPDATE="""UPDATE public.operator_jobs x SET target_count=x.target_count+%(added)s::integer,processed_count=0,
 pages_processed=x.pages_processed+%(pages)s::integer,items_processed=x.items_processed+%(items)s::integer,
 inventory_inserted_count=x.inventory_inserted_count+%(inserted)s::integer,
 inventory_updated_count=x.inventory_updated_count+%(updated)s::integer,
 quota_units_reserved=greatest(x.quota_units_reserved-%(reserved)s::integer+%(attempts)s::integer,0),
 quota_units_observed_estimate=x.quota_units_observed_estimate+%(attempts)s::integer,
 summary_json=jsonb_set(x.summary_json,ARRAY['full_inventory_sources',j.inventory_video_type],%(proof)s::jsonb)
 ||jsonb_build_object('inventory_ack',%(ack)s::jsonb,'inventory_results',%(results)s::jsonb,'inventory_complete',false),
 inventory_video_type=%(next_type)s::text,inventory_scan_id=CASE WHEN %(done)s::boolean THEN NULL ELSE x.inventory_scan_id END,
 inventory_checkpoint='{}'::jsonb,resume_page_token=NULL,status=CASE WHEN %(code)s::text IS NULL THEN 'queued' ELSE 'blocked' END,
 last_error_code=%(code)s::text,last_error_summary=CASE WHEN %(code)s::text IS NOT NULL THEN 'Confirm full loading to continue beyond 10000 observed videos.' END,
 finished_at=CASE WHEN %(code)s::text IS NOT NULL THEN now() END,retry_not_before=NULL,
 lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now(),heartbeat_at=now()
 FROM jsonb_populate_record(NULL::public.operator_jobs,%(job)s::jsonb) j
 WHERE x.id=j.id RETURNING to_jsonb(x)::text AS value"""
_CHANNEL="""UPDATE public.channels SET initial_long_inventory_status=CASE WHEN %(results)s::jsonb->'long'->>'status'='not_available'
 THEN 'skipped_no_long_playlist' ELSE 'completed' END,initial_long_inventory_started_at=NULL,initial_28d_stats_status='pending'
 WHERE channel_id=%(channel_id)s::text"""


def settle_full_initial_inventory(transaction,job,evidence,ack,reserved,pages,items,inserted,updated,attempts):
    """Apply one already-validated receipt under the public caller's ownership."""
    tx=transaction
    with tx.borrow():
        values=tx.fetch_one(_INPUT,(_encoded(evidence),_encoded(ack),reserved,pages,items,inserted,updated,attempts,_encoded(job)))
        if values['invalid'] is True:raise DatabaseServerError('full_initial_job_checkpoint_invalid',code='P0001')
        assert_full_initial_scope(tx,values['job'],False)
        page=tx.fetch_one(_IDS,(values['evidence'],))
        if page['oversized'] is True:raise DatabaseServerError('full_initial_page_coverage_invalid',code='P0001')
        values['ids']=page['ids']
        values['added']=tx.execute(_INSERT,values)
        tx.execute(_TARGETS,values)
        values.update(tx.fetch_one(_FLAGS,(values['evidence'],values['evidence'])))
        values.update(tx.fetch_one(_PROOF,values))
        values.update(tx.fetch_one(_RESULTS,values))
        values['code']=tx.fetch_one(_CONFIRM,(values['added'],values['job']))['code']
        result=_NativeObject(tx.returning_one(_UPDATE,values)['value'])
        if values['has_long'] is True:apply_channel_stage(tx,'full_settlement',(values['results'],result['channel_id']))
        if values['done'] is True and values['next_type']=='stream' and values['has_stream'] is True and values['code'] is None:
            return finish_full_initial_inventory(tx,result['id'])
        return result
