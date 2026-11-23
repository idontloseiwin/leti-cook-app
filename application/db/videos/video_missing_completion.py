"""All-format missing completion.

Exact immutable receipt replay precedes fresh lease/identity/observation
checks. New ownership is frozen before availability effects can clear Long
leases. Queue then incident lock order, native clocks, typed cadence and final
captured generation fence stay in the one caller-owned transaction. No
provider/retry.
"""
from .typed_video_read_policy import (TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL)
from contextlib import contextmanager
from .incident_statement_effects import after_incident_statement
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from .collection_policy import require_cadence_snapshot
from .video_availability_schedule import missing_schedule
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from .video_counter_completion import _clock_check

_DEFINITION='video_id text,channel_id text,video_type text,lease_token uuid,collection_attempt_id uuid,recorded_at timestamptz,availability_status text,availability_source text'
_INPUT="""SELECT row_value.*,raw.ordinal,md5(raw.payload::text) AS payload_fingerprint
 FROM jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY raw(payload,ordinal)
 CROSS JOIN LATERAL jsonb_to_record(raw.payload) row_value("""+_DEFINITION+")"
_CAPTURE='WITH input AS MATERIALIZED('+_INPUT+') '
_COUNT=_CAPTURE+'SELECT count(*)::integer AS count FROM input'
_VALID=_CAPTURE+"""SELECT NOT EXISTS(SELECT 1 FROM input WHERE video_id IS NULL OR btrim(video_id)=''
 OR channel_id IS NULL OR btrim(channel_id)='' OR video_type IS NULL OR video_type NOT IN('long','short','stream')
 OR (video_type='long' AND (nullif(availability_status,'') IS NULL OR nullif(availability_source,'') IS NULL
   OR availability_source='typed_video_collection_missing'))
 OR lease_token IS NULL OR collection_attempt_id IS NULL OR recorded_at IS NULL)
 AND NOT EXISTS(SELECT video_id FROM input GROUP BY video_id HAVING count(*)>1) AS valid"""
_LOCK=_CAPTURE+"""SELECT state.video_id FROM public.video_collection_state state JOIN input USING(video_id)
 ORDER BY state.video_id FOR UPDATE OF state"""
_MISMATCH=_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input JOIN public.typed_video_collection_receipts receipt
 USING(video_id,collection_attempt_id) WHERE (%(indices)s::bigint[] IS NULL OR input.ordinal=ANY(%(indices)s::bigint[]))
 AND (receipt.channel_id IS DISTINCT FROM input.channel_id OR receipt.video_type IS DISTINCT FROM input.video_type
 OR receipt.lease_token IS DISTINCT FROM input.lease_token OR receipt.receipt_kind IS DISTINCT FROM 'missing'
 OR receipt.recorded_at IS DISTINCT FROM input.recorded_at
 OR receipt.payload_fingerprint IS DISTINCT FROM input.payload_fingerprint)) AS mismatch"""
_PENDING=_CAPTURE+"""SELECT coalesce(array_agg(input.ordinal),'{}'::bigint[]) AS indices FROM input
 WHERE NOT EXISTS(SELECT 1 FROM public.typed_video_collection_receipts receipt
 WHERE receipt.video_id=input.video_id AND receipt.collection_attempt_id=input.collection_attempt_id)"""
_STALE=(_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input LEFT JOIN public.video_collection_state state USING(video_id)
 WHERE input.ordinal=ANY(%(indices)s::bigint[]) AND (state.video_id IS NULL
 OR state.channel_id IS DISTINCT FROM input.channel_id OR state.video_type IS DISTINCT FROM input.video_type
 OR state.lease_token IS DISTINCT FROM input.lease_token OR state.lease_expires_at IS NULL OR state.lease_expires_at<=clock_timestamp()
 OR (state.lease_token=input.lease_token AND ((input.video_type='long' AND NOT EXISTS(
   SELECT 1 FROM public.video_inventory inventory WHERE inventory.video_id=input.video_id
   AND inventory.channel_id=input.channel_id AND inventory.video_type='long'))
 OR (input.video_type<>'long' AND NOT EXISTS(SELECT 1 FROM """ + "(" + TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL + ")" + """ candidate
   WHERE candidate.video_id=input.video_id AND candidate.channel_id=input.channel_id AND candidate.video_type=input.video_type)))))) AS stale""")
_INSERT="""INSERT INTO public.typed_video_collection_receipts(video_id,collection_attempt_id,channel_id,video_type,
 lease_token,receipt_kind,recorded_at,payload_fingerprint)
 SELECT input.video_id,input.collection_attempt_id,input.channel_id,input.video_type,input.lease_token,'missing',
 input.recorded_at,input.payload_fingerprint FROM ("""+_INPUT+""") input JOIN public.video_collection_state state
 ON state.video_id=input.video_id AND state.channel_id=input.channel_id AND state.video_type=input.video_type
 AND state.lease_token=input.lease_token WHERE input.ordinal=ANY(%(indices)s::bigint[])
 ON CONFLICT(video_id,collection_attempt_id) DO NOTHING RETURNING video_id"""
_REGRESSION=_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input JOIN public.video_collection_state state USING(video_id)
 LEFT JOIN public.videos video USING(video_id)
 LEFT JOIN public.video_availability_incidents incident ON incident.video_id=input.video_id AND incident.restored_at IS NULL
 WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND ((input.video_type<>'long' AND state.typed_collection_checked_at>input.recorded_at)
 OR (input.video_type='long' AND greatest(state.stats_source_checked_at,state.metadata_source_checked_at,
 video.availability_checked_at,video.checked_at,incident.last_seen_at)>input.recorded_at))) AS regression"""
_SCOPES=_CAPTURE+"""SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object('channel_id',state.channel_id,
 'control_epoch',state.lease_channel_epoch)),'[]'::jsonb) AS scopes FROM input JOIN public.video_collection_state state
 ON state.video_id=input.video_id AND state.lease_token=input.lease_token WHERE input.video_id=ANY(%(owned_ids)s::text[])"""
_INCIDENT_LOCK=_CAPTURE+"""SELECT incident.id FROM public.video_availability_incidents incident JOIN input USING(video_id)
 WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND incident.restored_at IS NULL
 ORDER BY incident.video_id FOR UPDATE OF incident"""
_HISTORY=_CAPTURE+"""SELECT coalesce(jsonb_agg(to_jsonb(history)),'[]'::jsonb)::text AS rows FROM (
 SELECT input.video_id,input.channel_id,input.video_type,input.recorded_at,'unknown_unavailable'::text AS observed_status,
 CASE WHEN input.video_type='long' THEN input.availability_source ELSE 'typed_video_collection_missing' END AS observation_source,
 unavailable.availability_status AS previous_unavailable_status,unavailable.first_seen_at AS unavailable_first_seen_at,
 unavailable.attempt_count AS unavailable_attempt_count,open_incident.id AS open_incident_id,open_incident.latest_status AS incident_latest_status
 FROM input LEFT JOIN public.open_video_availability unavailable USING(video_id)
 LEFT JOIN LATERAL(SELECT incident.id,incident.latest_status FROM public.video_availability_incidents incident
  WHERE incident.video_id=input.video_id AND incident.restored_at IS NULL ORDER BY incident.id DESC LIMIT 1) open_incident ON true
 WHERE input.video_id=ANY(%(owned_ids)s::text[])) history"""
_HISTORY_DEFINITION='video_id text,channel_id text,video_type text,recorded_at timestamptz,observed_status text,observation_source text,previous_unavailable_status text,unavailable_first_seen_at timestamptz,unavailable_attempt_count integer,open_incident_id bigint,incident_latest_status text'
_HISTORY_ROWS='jsonb_to_recordset(%s::jsonb) missing('+_HISTORY_DEFINITION+')'
_UPDATE_INCIDENT="""UPDATE public.video_availability_incidents incident SET channel_id=coalesce(incident.channel_id,missing.channel_id),
 latest_status=missing.observed_status,last_seen_at=missing.recorded_at,attempt_count=coalesce(missing.unavailable_attempt_count,0)+1,
 source_latest_seen=missing.observation_source,updated_at=missing.recorded_at FROM """+_HISTORY_ROWS+"""
 WHERE missing.open_incident_id IS NOT NULL AND incident.id=missing.open_incident_id"""
_INSERT_INCIDENT="""INSERT INTO public.video_availability_incidents(video_id,channel_id,first_status,latest_status,first_seen_at,last_seen_at,
 attempt_count,source_first_seen,source_latest_seen,updated_at)
 SELECT missing.video_id,missing.channel_id,coalesce(missing.previous_unavailable_status,missing.observed_status),missing.observed_status,
 coalesce(missing.unavailable_first_seen_at,missing.recorded_at),missing.recorded_at,coalesce(missing.unavailable_attempt_count,0)+1,
 missing.observation_source,missing.observation_source,missing.recorded_at FROM """+_HISTORY_ROWS+' WHERE missing.open_incident_id IS NULL'
_DETECTED="""INSERT INTO public.video_availability_events(video_id,channel_id,event_type,observed_at,source,to_status)
 SELECT missing.video_id,missing.channel_id,'unavailable_detected',coalesce(missing.unavailable_first_seen_at,missing.recorded_at),
 missing.observation_source,coalesce(missing.previous_unavailable_status,missing.observed_status) FROM """+_HISTORY_ROWS+' WHERE missing.open_incident_id IS NULL'
_CHANGED="""INSERT INTO public.video_availability_events(video_id,channel_id,event_type,observed_at,source,from_status,to_status)
 SELECT missing.video_id,missing.channel_id,'unavailable_status_changed',missing.recorded_at,missing.observation_source,
 CASE WHEN missing.open_incident_id IS NOT NULL THEN missing.incident_latest_status
 ELSE coalesce(missing.previous_unavailable_status,missing.observed_status) END,missing.observed_status FROM """+_HISTORY_ROWS+"""
 WHERE (missing.open_incident_id IS NOT NULL AND missing.incident_latest_status IS DISTINCT FROM missing.observed_status)
 OR (missing.open_incident_id IS NULL AND coalesce(missing.previous_unavailable_status,missing.observed_status) IS DISTINCT FROM missing.observed_status)"""
_VIDEO="""UPDATE public.videos video SET availability_status='unknown_unavailable',
 privacy_status=NULL,upload_status=NULL,availability_checked_at=input.recorded_at
 FROM ("""+_INPUT+""") input WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND video.video_id=input.video_id
 AND (video.availability_checked_at IS NULL OR video.availability_checked_at<=input.recorded_at)"""
_OWNED_WHERE="""input.video_id=ANY(%(owned_ids)s::text[]) AND state.video_id=input.video_id
 AND state.channel_id=input.channel_id AND state.video_type=input.video_type
 AND state.lease_token=input.lease_token
 AND (input.video_type='long' OR state.typed_collection_checked_at IS NULL OR state.typed_collection_checked_at<=input.recorded_at)"""
_POLICY=_CAPTURE+"""SELECT coalesce(jsonb_agg(jsonb_build_object(
 'video_id',input.video_id,'video_type',coalesce(state.availability_schedule_type,input.video_type),
 'first_missing_at',state.availability_first_missing_at,'observed_at',input.recorded_at)),'[]'::jsonb) AS sources
 FROM input JOIN public.video_collection_state state ON state.video_id=input.video_id WHERE """+_OWNED_WHERE
_COMPLETE="""UPDATE public.video_collection_state state SET stats_due_at=NULL,
 metadata_due_at=least(schedule.next_due_at,CASE WHEN state.availability_resume_observed_at>input.recorded_at
 THEN state.availability_resume_observed_at END),hydration_needed=true,
 availability_first_missing_at=schedule.first_missing_at,availability_schedule_type=schedule.schedule_type,
 availability_resume_observed_at=CASE WHEN state.availability_resume_observed_at>input.recorded_at
 THEN state.availability_resume_observed_at END,
 paused_reason=CASE WHEN schedule.stopped AND NOT coalesce(state.availability_resume_observed_at>input.recorded_at,false)
 THEN 'availability_retry_expired' ELSE NULL END,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 consecutive_failures=0,last_error_type=NULL,last_success_at=greatest(state.last_success_at,input.recorded_at),
 next_due_at=least(schedule.next_due_at,CASE WHEN state.availability_resume_observed_at>input.recorded_at
 THEN state.availability_resume_observed_at END),
 typed_collection_status=CASE WHEN input.video_type='long' THEN state.typed_collection_status ELSE 'unavailable' END,
 typed_collection_checked_at=CASE WHEN input.video_type='long' THEN state.typed_collection_checked_at ELSE input.recorded_at END,updated_at=now()
 FROM ("""+_INPUT+""") input JOIN jsonb_to_recordset(%(schedules)s::jsonb)
 schedule(video_id text,first_missing_at timestamptz,schedule_type text,next_due_at timestamptz,stopped boolean)
 USING(video_id) WHERE """+_OWNED_WHERE

_INSERT_INCIDENT_EFFECT = _INSERT_INCIDENT + ' RETURNING to_jsonb(video_availability_incidents)::text AS new_text'

# The original owner already holds these incident row locks. Read the complete
# OLD tuple in the same UPDATE and carry actual NEW out of RETURNING.
_INCIDENT_EFFECT_UPDATE = _UPDATE_INCIDENT.replace(' FROM ', ' FROM public.video_availability_incidents effect_previous, ',1)
_INCIDENT_EFFECT_UPDATE += ' AND effect_previous.id=incident.id RETURNING to_jsonb(effect_previous)::text AS old_text,to_jsonb(incident)::text AS new_text'


def _reject(message):raise DatabaseServerError(message,code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('complete_typed_video_missing')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def complete_typed_video_missing(database,rows,*,transaction=None):
    """Return requested count, including accepted exact receipt replays."""
    with _transaction(database,transaction) as tx:
        if not isinstance(rows,list):_reject('typed_video_missing_completion_requires_array')
        raw=Jsonb(rows)
        params=dict(rows=raw,indices=None)
        count=tx.fetch_one(_COUNT,params)['count']
        if count==0:return 0
        if count>50 or tx.fetch_one(_VALID,params)['valid'] is not True:_reject('typed_video_missing_completion_input_invalid')
        tx.fetch_all(_LOCK,params,max_rows=count)
        _clock_check(tx,raw)
        if tx.fetch_one(_MISMATCH,params)['mismatch']:_reject('typed_video_missing_completion_receipt_mismatch')
        params['indices']=tx.fetch_one(_PENDING,params)['indices']
        if tx.fetch_one(_STALE,params)['stale']:_reject('typed_video_missing_completion_stale_or_ineligible')
        owned=tx.returning_all(_INSERT,params,max_rows=count)
        params['owned_ids']=[row['video_id'] for row in owned]
        if tx.fetch_one(_REGRESSION,params)['regression']:_reject('typed_completion_observation_clock_regression')
        if tx.fetch_one(_MISMATCH,params)['mismatch']:_reject('typed_video_missing_completion_receipt_mismatch')
        scopes=tx.fetch_one(_SCOPES,params)['scopes']
        tx.fetch_all(_INCIDENT_LOCK,params,max_rows=count)
        history=tx.fetch_one(_HISTORY,params)['rows']
        changed=tx.returning_all(_INCIDENT_EFFECT_UPDATE,(history,),max_rows=count)
        after_incident_statement(tx,[dict(operation='update',**row) for row in changed])
        inserted=tx.returning_all(_INSERT_INCIDENT_EFFECT,(history,),max_rows=count)
        after_incident_statement(tx,[dict(operation='insert',old_text=None,**row) for row in inserted])
        for statement in (_DETECTED,_CHANGED):tx.execute(statement,(history,))
        tx.execute(_VIDEO,params)
        snapshot=require_cadence_snapshot(tx)
        sources=tx.fetch_one(_POLICY,params)['sources']
        params['schedules']=Jsonb([dict(video_id=row['video_id'],**missing_schedule(snapshot,
            video_type=row['video_type'],first_missing_at=row['first_missing_at'],
            observed_at=row['observed_at'])) for row in sources])
        if tx.execute(_COMPLETE,params)!=len(owned):_reject('typed_completion_owned_settlement_count_mismatch')
        _clock_check(tx,raw)
        assert_channel_scopes(tx,scopes)
        return count
