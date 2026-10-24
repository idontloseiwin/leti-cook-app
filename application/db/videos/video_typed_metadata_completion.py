"""Typed metadata completion around the borrowed profile writer.

Exact native typed receipts freeze new ownership before source completion clears
leases. Availability restoration is captured under its original incident locks;
the public repository owns exact receipt replay; no provider I/O occurs here.
"""
from .typed_video_read_policy import (TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL)
from contextlib import contextmanager
from collection_policy import MAX_INTERVAL_SECONDS
from .incident_statement_effects import after_incident_statement
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from .collection_policy import cadence_parameter, _NAMED_SQL as _CADENCE_SQL
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from .video_counter_completion import _clock_check
from .video_stream_completion import _schedules as _typed_schedules
from . import video_profile_completion as profile

_DEFINITION="video_id text,channel_id text,video_type text,lease_token uuid,collection_attempt_id uuid,recorded_at timestamptz,metadata_observed_at timestamptz,published_at timestamptz,availability_status text,stream_actual_end_at timestamptz"
_INPUT="""SELECT row_value.*,raw.ordinal,raw.payload ? 'metadata_observed_at' AS metadata_supplied,md5(raw.payload::text) AS payload_fingerprint
 FROM jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY raw(payload,ordinal)
 CROSS JOIN LATERAL jsonb_to_record(raw.payload) row_value("""+_DEFINITION+")"
_CAPTURE="WITH input AS MATERIALIZED ("+_INPUT+") "
_COUNT=_CAPTURE+"SELECT count(*)::integer AS count FROM input"
_VALID=_CAPTURE+"""SELECT NOT EXISTS(SELECT 1 FROM input WHERE video_id IS NULL OR btrim(video_id)=''
 OR channel_id IS NULL OR btrim(channel_id)='' OR video_type NOT IN ('short','stream')
 OR lease_token IS NULL OR collection_attempt_id IS NULL OR recorded_at IS NULL OR published_at IS NULL
 OR (metadata_supplied AND metadata_observed_at IS NULL)
 OR (video_type='stream' AND (stream_actual_end_at IS NULL OR stream_actual_end_at>coalesce(metadata_observed_at,recorded_at)))
 OR (video_type='short' AND stream_actual_end_at IS NOT NULL))
 AND NOT EXISTS(SELECT video_id FROM input GROUP BY video_id HAVING count(*)>1)
 AND (SELECT count(DISTINCT video_type) FROM input)=1 AS valid"""
_LOCK=_CAPTURE+"""SELECT state.video_id FROM public.video_collection_state state
 JOIN input ON input.video_id=state.video_id ORDER BY state.video_id FOR UPDATE OF state"""
_MISMATCH=_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input
 JOIN public.typed_video_collection_receipts receipt USING(video_id,collection_attempt_id)
 WHERE (%(indices)s::bigint[] IS NULL OR input.ordinal=ANY(%(indices)s::bigint[])) AND (
 receipt.channel_id IS DISTINCT FROM input.channel_id OR receipt.video_type IS DISTINCT FROM input.video_type
 OR receipt.lease_token IS DISTINCT FROM input.lease_token OR receipt.receipt_kind IS DISTINCT FROM 'metadata'
 OR receipt.recorded_at IS DISTINCT FROM input.recorded_at
 OR receipt.payload_fingerprint IS DISTINCT FROM input.payload_fingerprint)) AS mismatch"""
_PENDING=_CAPTURE+"""SELECT coalesce(array_agg(input.ordinal),'{}'::bigint[]) AS indices FROM input
 WHERE NOT EXISTS(SELECT 1 FROM public.typed_video_collection_receipts receipt
 WHERE receipt.video_id=input.video_id AND receipt.collection_attempt_id=input.collection_attempt_id)"""
_STALE=(_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input
 LEFT JOIN public.video_collection_state state ON state.video_id=input.video_id
 WHERE input.ordinal=ANY(%(indices)s::bigint[]) AND (state.video_id IS NULL
 OR state.channel_id IS DISTINCT FROM input.channel_id OR state.video_type IS DISTINCT FROM input.video_type
 OR state.lease_token IS DISTINCT FROM input.lease_token OR state.lease_expires_at IS NULL
 OR state.lease_expires_at<=clock_timestamp()
 OR (state.lease_token=input.lease_token AND NOT EXISTS(
 SELECT 1 FROM """ + "(" + TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL + ")" + """ candidate WHERE candidate.video_id=input.video_id
 AND candidate.channel_id=input.channel_id AND candidate.video_type=input.video_type)))) AS stale""")
_INSERT="""INSERT INTO public.typed_video_collection_receipts(video_id,collection_attempt_id,channel_id,video_type,
 lease_token,receipt_kind,recorded_at,payload_fingerprint)
 SELECT input.video_id,input.collection_attempt_id,input.channel_id,input.video_type,input.lease_token,'metadata',
 input.recorded_at,input.payload_fingerprint FROM ("""+_INPUT+""") input JOIN public.video_collection_state state
 ON state.video_id=input.video_id AND state.channel_id=input.channel_id AND state.video_type=input.video_type
 AND state.lease_token=input.lease_token WHERE input.ordinal=ANY(%(indices)s::bigint[])
 ON CONFLICT(video_id,collection_attempt_id) DO NOTHING RETURNING video_id"""
_REGRESSION=_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input JOIN public.video_collection_state state USING(video_id)
 WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND state.typed_collection_checked_at>coalesce(input.metadata_observed_at,input.recorded_at)) AS regression"""
_SCOPES=_CAPTURE+"""SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object('channel_id',state.channel_id,
 'control_epoch',state.lease_channel_epoch)),'[]'::jsonb) AS scopes FROM input
 JOIN public.video_collection_state state ON state.video_id=input.video_id AND state.lease_token=input.lease_token
 WHERE input.video_id=ANY(%(owned_ids)s::text[])"""
_OWNED_ORDINALS=_CAPTURE+"""SELECT coalesce(array_agg(input.ordinal ORDER BY input.video_id),'{}'::bigint[]) AS ordinals
 FROM input WHERE input.video_id=ANY(%(owned_ids)s::text[])"""
_INCIDENT_LOCK=_CAPTURE+"""SELECT incident.id FROM public.video_availability_incidents incident JOIN input USING(video_id)
 WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND incident.restored_at IS NULL
 ORDER BY incident.video_id,incident.id FOR UPDATE OF incident"""
_RESTORED=_CAPTURE+"""SELECT coalesce(jsonb_agg(jsonb_build_object('video_id',input.video_id,
 'channel_id',input.channel_id,'recorded_at',coalesce(input.metadata_observed_at,input.recorded_at),'availability_status',input.availability_status,
 'incident_id',incident.id,'latest_status',incident.latest_status)),'[]'::jsonb)::text AS rows
 FROM input JOIN public.video_availability_incidents incident ON incident.video_id=input.video_id
 AND incident.restored_at IS NULL AND incident.last_seen_at<=coalesce(input.metadata_observed_at,input.recorded_at)
 WHERE input.video_id=ANY(%(owned_ids)s::text[])"""
_RESTORED_DEFINITION='video_id text,channel_id text,recorded_at timestamptz,availability_status text,incident_id bigint,latest_status text'
_RESTORE="""UPDATE public.video_availability_incidents incident SET channel_id=coalesce(incident.channel_id,restored.channel_id),
 restored_at=restored.recorded_at,restored_status=coalesce(restored.availability_status,'public'),
 source_restored='typed_video_collection_restore',updated_at=restored.recorded_at
 FROM jsonb_to_recordset(%s::jsonb) restored("""+_RESTORED_DEFINITION+""") WHERE incident.id=restored.incident_id"""
_EVENTS="""INSERT INTO public.video_availability_events(video_id,channel_id,event_type,observed_at,source,from_status,to_status)
 SELECT restored.video_id,restored.channel_id,'restored',restored.recorded_at,'typed_video_collection_restore',
 coalesce(restored.latest_status,'unknown_unavailable'),coalesce(restored.availability_status,'public')
 FROM jsonb_to_recordset(%s::jsonb) restored("""+_RESTORED_DEFINITION+")"
_POLICY="WITH input AS MATERIALIZED ("+_INPUT+"""), cadence AS MATERIALIZED (
 SELECT rule.*,row_number() OVER(ORDER BY policy_version DESC,min_age_seconds DESC,
 video_type,collection_kind,availability_class)-1 AS rule_index FROM ("""+_CADENCE_SQL+""") rule LIMIT 1001),
 sources AS MATERIALIZED (
 SELECT input.video_id,input.video_type,coalesce(input.metadata_observed_at,input.recorded_at) AS recorded_at,
 CASE WHEN input.video_type='stream' THEN input.stream_actual_end_at ELSE input.published_at END AS anchor,
 coalesce((raw.payload->>'availability_checked_at')::timestamptz,input.metadata_observed_at,input.recorded_at) AS availability_checked_at,
 CASE WHEN NOT isfinite(coalesce(input.metadata_observed_at,input.recorded_at))
 OR coalesce(input.metadata_observed_at,input.recorded_at)>clock_timestamp() THEN clock_timestamp() END AS fallback_at,
 clock_timestamp() AS metadata_clock
 FROM input JOIN jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY raw(payload,ordinal) USING(ordinal)
 WHERE input.video_id=ANY(%(owned_ids)s::text[])), projected AS (
 SELECT sources.video_id,sources.fallback_at,sources.availability_checked_at,
 sources.recorded_at IS NOT NULL AND isfinite(sources.recorded_at) AND sources.recorded_at<=sources.metadata_clock AS as_of_valid,
 sources.availability_checked_at IS NULL OR NOT isfinite(sources.availability_checked_at)
 OR sources.availability_checked_at>sources.metadata_clock AS availability_now,
 CASE WHEN fallback_at IS NULL THEN
 (SELECT coalesce(jsonb_agg(rule.rule_index ORDER BY rule.rule_index),'[]'::jsonb) FROM cadence rule
 WHERE rule.enabled AND rule.video_type=sources.video_type AND rule.collection_kind='metadata' AND rule.availability_class='all'
 AND (rule.min_age_seconds=0 OR coalesce(sources.anchor,sources.recorded_at)<sources.recorded_at-make_interval(days=>(rule.min_age_seconds/86400)::integer))
 AND (rule.max_age_seconds IS NULL OR coalesce(sources.anchor,sources.recorded_at)>=sources.recorded_at-make_interval(days=>(rule.max_age_seconds/86400)::integer)))
 ELSE '[]'::jsonb END AS matches FROM sources)
 SELECT coalesce((SELECT jsonb_agg(to_jsonb(projected)) FROM projected),'[]'::jsonb) AS sources,
 coalesce((SELECT jsonb_agg(to_jsonb(cadence)-'rule_index' ORDER BY rule_index) FROM cadence),'[]'::jsonb) AS cadence"""
_COMPLETE="""UPDATE public.video_collection_state state SET metadata_due_at=least(due.metadata_due_at,state.availability_resume_observed_at),
 stats_source_checked_at=input.recorded_at,metadata_source_checked_at=coalesce(input.metadata_observed_at,input.recorded_at),
 hydration_needed=state.availability_resume_observed_at IS NOT NULL,
 policy_version=1,paused_reason=NULL,lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,
 lease_started_at=NULL,lease_expires_at=NULL,consecutive_failures=0,last_error_type=NULL,
 last_success_at=greatest(state.last_success_at,input.recorded_at),typed_source_published_at=input.published_at,
 typed_source_published_acquired_at=CASE WHEN input.published_at IS NOT NULL THEN coalesce(input.metadata_observed_at,input.recorded_at) END,
 typed_stream_actual_end_at=CASE WHEN input.video_type='stream' THEN input.stream_actual_end_at END,
 typed_source_stream_end_acquired_at=CASE WHEN input.video_type='stream' AND input.stream_actual_end_at IS NOT NULL
 THEN coalesce(input.metadata_observed_at,input.recorded_at) END,
 next_due_at=least(state.stats_due_at,due.metadata_due_at,state.availability_resume_observed_at),
 typed_collection_status='hydrated',typed_collection_checked_at=coalesce(input.metadata_observed_at,input.recorded_at),updated_at=now()
 FROM ("""+_INPUT+""") input JOIN jsonb_to_recordset(%(schedules)s::jsonb) schedule(video_id text,seconds integer,
 fallback_at timestamptz,availability_checked_at timestamptz,availability_now boolean) USING(video_id)
 CROSS JOIN LATERAL(SELECT least(coalesce(schedule.fallback_at,
 coalesce(input.metadata_observed_at,input.recorded_at)+make_interval(secs=>schedule.seconds)),
 CASE WHEN schedule.availability_now THEN coalesce(input.metadata_observed_at,input.recorded_at)
 ELSE schedule.availability_checked_at+make_interval(secs=>%(max_interval_seconds)s::integer) END) AS metadata_due_at) due
 WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND state.video_id=input.video_id AND state.channel_id=input.channel_id
 AND state.video_type=input.video_type AND (state.lease_token IS NULL OR state.lease_token=input.lease_token)
 AND (state.typed_collection_checked_at IS NULL OR state.typed_collection_checked_at<=coalesce(input.metadata_observed_at,input.recorded_at))"""


def _reject(message):raise DatabaseServerError(message,code='P0001')

# The original owner already holds these incident row locks. Read the complete
# OLD tuple in the same UPDATE and carry actual NEW out of RETURNING.
_INCIDENT_EFFECT_UPDATE = _RESTORE.replace(' FROM ', ' FROM public.video_availability_incidents effect_previous, ',1)
_INCIDENT_EFFECT_UPDATE += ' AND effect_previous.id=incident.id RETURNING to_jsonb(effect_previous)::text AS old_text,to_jsonb(incident)::text AS new_text'


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('complete_typed_video_metadata_collection')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _schedules(captured):
    # Evaluate typed policy before the metadata as-of check, matching nested SQL.
    for source in captured['sources']:
        if source['fallback_at'] is None and not source['matches']:_reject('typed_video_collection_cadence_missing')
        if source['as_of_valid'] is not True:_reject('hot_video_refresh_as_of_invalid')
    schedules=_typed_schedules(captured)
    facts={source['video_id']:source for source in captured['sources']}
    for schedule in schedules:
        source=facts[schedule['video_id']]
        schedule.update(availability_checked_at=source['availability_checked_at'],availability_now=source['availability_now'])
    return schedules


def complete_typed_video_metadata_collection(database,rows,*,transaction=None):
    """Return requested count; only new typed receipts may write source/history."""
    with _transaction(database,transaction) as tx:
        if not isinstance(rows,list):_reject('typed_video_metadata_completion_requires_array')
        raw=Jsonb(rows)
        params=dict(rows=raw,indices=None)
        count=tx.fetch_one(_COUNT,params)['count']
        if count==0:return 0
        if count>50:_reject('typed_video_metadata_completion_input_invalid')
        if tx.fetch_one(_VALID,params)['valid'] is not True:_reject('typed_video_metadata_completion_input_invalid')
        tx.fetch_all(_LOCK,params,max_rows=count)
        _clock_check(tx,raw)
        if tx.fetch_one(_MISMATCH,params)['mismatch']:_reject('typed_video_metadata_completion_receipt_mismatch')
        pending=tx.fetch_one(_PENDING,params)['indices']
        params=dict(rows=raw,indices=pending)
        if tx.fetch_one(_STALE,params)['stale']:_reject('typed_video_metadata_completion_stale_or_ineligible')
        owned=[row['video_id'] for row in tx.returning_all(_INSERT,params,max_rows=count)]
        owned_params=dict(rows=raw,owned_ids=owned)
        if tx.fetch_one(_REGRESSION,owned_params)['regression']:_reject('typed_completion_observation_clock_regression')
        if tx.fetch_one(_MISMATCH,params)['mismatch']:_reject('typed_video_metadata_completion_receipt_mismatch')
        ordinals=tx.fetch_one(_OWNED_ORDINALS,owned_params)['ordinals']
        scopes=tx.fetch_one(_SCOPES,owned_params)['scopes']
        # Unique open-incident index proves at most one row per owned video.
        tx.fetch_all(_INCIDENT_LOCK,owned_params,max_rows=count)
        restored=tx.fetch_one(_RESTORED,owned_params)['rows']
        if owned:
            owned_rows=[rows[ordinal-1] for ordinal in ordinals]
            completed=profile.complete_video_profile_metadata_collection(None,owned_rows,transaction=tx,_reconcile_type=False)
            if completed!=len(owned):_reject('typed_video_metadata_completion_count_mismatch')
        changed=tx.returning_all(_INCIDENT_EFFECT_UPDATE,(restored,),max_rows=count)
        after_incident_statement(tx,[dict(operation='update',**row) for row in changed])
        tx.execute(_EVENTS,(restored,))
        schedules=_schedules(tx.fetch_one(_POLICY,dict(owned_params,cadence=cadence_parameter(tx))))
        if tx.execute(_COMPLETE,dict(owned_params,schedules=Jsonb(schedules),max_interval_seconds=MAX_INTERVAL_SECONDS))!=len(owned):
            _reject('typed_completion_owned_settlement_count_mismatch')
        profile._clocks(tx,raw)
        from ..inventory.automatic_type_reconciliation import reconcile_automatic_video_types
        reconcile_automatic_video_types(tx,owned)
        assert_channel_scopes(tx,scopes)
        return count
