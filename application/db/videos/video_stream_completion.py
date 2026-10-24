"""Publication lifecycle settlement; independent of video type.

Native JSONB fingerprint/casts and clocks preserve the retained receipt protocol.
One existing queue owner and one transaction cover receipt creation, availability
restoration and settlement. Waiting publications never write counter snapshots.
"""
from .video_identity_recovery import ELIGIBLE_SQL
from .video_source_invalidation import source_clock_invalidation
from contextlib import contextmanager
import hashlib
from psycopg.types.json import Jsonb
from collection_policy import MAX_INTERVAL_SECONDS, PolicyError
from ..channel.channel_control import assert_channel_scopes
from .collection_policy import require_cadence_snapshot, cadence_parameter, _NAMED_SQL as _CADENCE_SQL,_decode_cadence_rows
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from .video_counter_completion import _clock_check

_DEFINITION="video_id text,channel_id text,video_type text,lease_token uuid,collection_attempt_id uuid,recorded_at timestamptz,stream_actual_end_at timestamptz,availability_status text,privacy_status text,upload_status text,title text,published_at timestamptz,duration text,publication_status text,scheduled_start_at timestamptz,tags text[],category_id text,youtube_topic_categories text[],default_audio_language text,default_language text,caption_available boolean"
_INPUT="""SELECT row_value.*,raw.ordinal,md5(raw.payload::text) AS payload_fingerprint
 FROM jsonb_array_elements(%(rows)s::jsonb) WITH ORDINALITY raw(payload,ordinal)
 CROSS JOIN LATERAL jsonb_to_record(raw.payload) row_value("""+_DEFINITION+")"
_CAPTURE="WITH input AS MATERIALIZED ("+_INPUT+") "
_COUNT=_CAPTURE+"SELECT count(*)::integer AS count FROM input"
_VALID=_CAPTURE+"""SELECT NOT EXISTS(SELECT 1 FROM input WHERE video_id IS NULL OR btrim(video_id)=''
 OR channel_id IS NULL OR btrim(channel_id)='' OR video_type IS NULL
 OR video_type NOT IN ('long','short','stream','unknown')
 OR title IS NULL OR length(title)>1000 OR published_at IS NULL OR NOT isfinite(published_at)
 OR publication_status IS NULL OR publication_status NOT IN ('upcoming','live','awaiting_completion')
 OR (scheduled_start_at IS NOT NULL AND NOT isfinite(scheduled_start_at))
 OR cardinality(tags)>500 OR octet_length(array_to_string(tags,''))>25600
 OR cardinality(youtube_topic_categories)>50 OR length(default_audio_language)>64 OR length(default_language)>64
 OR lease_token IS NULL OR collection_attempt_id IS NULL OR recorded_at IS NULL OR stream_actual_end_at IS NOT NULL
 OR availability_status IS NULL OR availability_status NOT IN ('public','unlisted','private')
 OR privacy_status IS DISTINCT FROM availability_status OR upload_status IS NULL OR length(upload_status)>64)
 AND NOT EXISTS(SELECT video_id FROM input GROUP BY video_id HAVING count(*)>1) AS valid"""
_LOCK=_CAPTURE+"""SELECT state.video_id FROM public.video_collection_state state
 JOIN input ON input.video_id=state.video_id ORDER BY state.video_id FOR UPDATE OF state"""
_MISMATCH=_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input
 JOIN public.typed_video_collection_receipts receipt USING(video_id,collection_attempt_id)
 WHERE (%(indices)s::bigint[] IS NULL OR input.ordinal=ANY(%(indices)s::bigint[])) AND (
 receipt.channel_id IS DISTINCT FROM input.channel_id OR receipt.video_type IS DISTINCT FROM nullif(input.video_type,'unknown')
 OR receipt.lease_token IS DISTINCT FROM input.lease_token OR receipt.receipt_kind IS DISTINCT FROM 'stream_probe'
 OR receipt.recorded_at IS DISTINCT FROM input.recorded_at
 OR receipt.payload_fingerprint IS DISTINCT FROM input.payload_fingerprint)) AS mismatch"""
_PENDING=_CAPTURE+"""SELECT coalesce(array_agg(input.ordinal),'{}'::bigint[]) AS indices FROM input
 WHERE NOT EXISTS(SELECT 1 FROM public.typed_video_collection_receipts receipt
 WHERE receipt.video_id=input.video_id AND receipt.collection_attempt_id=input.collection_attempt_id)"""
_ADMITTED = ELIGIBLE_SQL.replace('%s::text', 'input.video_id', 1).replace('%s::text', 'input.channel_id', 1)
_STALE=(_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input
 LEFT JOIN public.video_collection_state state ON state.video_id=input.video_id
 WHERE input.ordinal=ANY(%(indices)s::bigint[]) AND (state.video_id IS NULL
 OR state.channel_id IS DISTINCT FROM input.channel_id
 OR state.video_type IS DISTINCT FROM nullif(input.video_type,'unknown')
 OR state.lease_token IS DISTINCT FROM input.lease_token OR state.lease_expires_at IS NULL
 OR state.lease_expires_at<=clock_timestamp() OR state.typed_stream_actual_end_at IS NOT NULL
 OR NOT ("""+_ADMITTED+"""))) AS stale""")
_INSERT="""INSERT INTO public.typed_video_collection_receipts(video_id,collection_attempt_id,channel_id,video_type,
 lease_token,receipt_kind,recorded_at,payload_fingerprint)
 SELECT input.video_id,input.collection_attempt_id,input.channel_id,nullif(input.video_type,'unknown'),input.lease_token,'stream_probe',
 input.recorded_at,input.payload_fingerprint FROM ("""+_INPUT+""") input JOIN public.video_collection_state state
 ON state.video_id=input.video_id AND state.channel_id=input.channel_id AND state.video_type IS NOT DISTINCT FROM nullif(input.video_type,'unknown')
 AND state.lease_token=input.lease_token WHERE input.ordinal=ANY(%(indices)s::bigint[])
 ON CONFLICT(video_id,collection_attempt_id) DO NOTHING RETURNING video_id"""
_REGRESSION=_CAPTURE+"""SELECT EXISTS(SELECT 1 FROM input JOIN public.video_collection_state state USING(video_id)
 LEFT JOIN public.videos video USING(video_id)
 LEFT JOIN public.video_availability_incidents incident ON incident.video_id=input.video_id AND incident.restored_at IS NULL
 WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND greatest(state.typed_collection_checked_at,
 video.availability_checked_at,video.metadata_checked_at,video.checked_at,incident.last_seen_at)>input.recorded_at) AS regression"""
_SCOPES=_CAPTURE+"""SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object('channel_id',state.channel_id,
 'control_epoch',state.lease_channel_epoch)),'[]'::jsonb) AS scopes FROM input
 JOIN public.video_collection_state state ON state.video_id=input.video_id AND state.lease_token=input.lease_token
 WHERE input.video_id=ANY(%(owned_ids)s::text[])"""
_INCIDENT_LOCK=_CAPTURE+"""SELECT incident.id FROM public.video_availability_incidents incident
 JOIN input USING(video_id) WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND incident.restored_at IS NULL
 ORDER BY incident.video_id FOR UPDATE OF incident"""
# Every metadata field belongs to this same General observation. Missing fields
# are replaced by NULL; renewing a date must never renew an older title/profile.
_AVAILABILITY="""INSERT INTO public.videos AS video(video_id,channel_id,title,published_at,duration,
 tags,category_id,youtube_topic_categories,default_audio_language,default_language,caption_available,
 metadata_checked_at,availability_status,privacy_status,upload_status,availability_checked_at,
 publication_status,scheduled_start_at)
 SELECT input.video_id,input.channel_id,input.title,input.published_at,input.duration,input.tags,
 input.category_id,input.youtube_topic_categories,input.default_audio_language,input.default_language,
 input.caption_available,input.recorded_at,input.availability_status,input.privacy_status,input.upload_status,
 input.recorded_at,input.publication_status,input.scheduled_start_at
 FROM ("""+_INPUT+""") input WHERE input.video_id=ANY(%(owned_ids)s::text[])
 ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,published_at=excluded.published_at,
 views=NULL,like_count=NULL,comment_count=NULL,checked_at=NULL,
 duration=excluded.duration,tags=excluded.tags,category_id=excluded.category_id,
 youtube_topic_categories=excluded.youtube_topic_categories,default_audio_language=excluded.default_audio_language,
 default_language=excluded.default_language,caption_available=excluded.caption_available,
 stream_actual_end_at=NULL,metadata_checked_at=excluded.metadata_checked_at,
 availability_status=excluded.availability_status,privacy_status=excluded.privacy_status,
 upload_status=excluded.upload_status,availability_checked_at=excluded.availability_checked_at,
 publication_status=excluded.publication_status,scheduled_start_at=excluded.scheduled_start_at
 WHERE video.channel_id=excluded.channel_id
 AND (video.metadata_checked_at IS NULL OR video.metadata_checked_at<=excluded.metadata_checked_at)
 AND (video.availability_checked_at IS NULL OR video.availability_checked_at<=excluded.availability_checked_at)"""
_RESTORE="""UPDATE public.video_availability_incidents incident SET restored_at=input.recorded_at,
 restored_status=input.availability_status,source_restored='typed_video_collection_restore',updated_at=input.recorded_at
 FROM ("""+_INPUT+""") input,public.video_availability_incidents previous
 WHERE incident.id=previous.id AND incident.video_id=input.video_id AND incident.restored_at IS NULL
 AND input.video_id=ANY(%(owned_ids)s::text[]) AND incident.last_seen_at<=input.recorded_at
 RETURNING incident.video_id,incident.channel_id,incident.restored_at::text AS restored_at,
 incident.restored_status,previous.latest_status"""
_EVENTS="""INSERT INTO public.video_availability_events(video_id,channel_id,event_type,observed_at,source,from_status,to_status)
 SELECT video_id,channel_id,'restored',restored_at,'typed_video_collection_restore',latest_status,restored_status
 FROM jsonb_to_recordset(%s::jsonb) restored(video_id text,channel_id text,restored_at timestamptz,
 restored_status text,latest_status text)"""
# Native calendar comparisons avoid parsing BC/extended dates in Python. Only
# matching rule indices cross the boundary, bounded by50 inputs x1000 rules.
_POLICY="WITH input AS MATERIALIZED ("+_INPUT+"""), cadence AS MATERIALIZED (
 SELECT rule.*,row_number() OVER(ORDER BY policy_version DESC,min_age_seconds DESC,
 video_type,collection_kind,availability_class)-1 AS rule_index FROM ("""+_CADENCE_SQL+""") rule LIMIT 1001),
 sources AS MATERIALIZED (
 SELECT input.video_id,input.recorded_at,input.video_type,input.availability_status,input.published_at AS anchor,
 CASE WHEN input.video_type='unknown' THEN %(identity_seconds)s::integer END AS identity_seconds,
 greatest(extract(epoch FROM (input.recorded_at-input.published_at))::bigint,0) AS age_seconds,
 CASE WHEN NOT isfinite(input.recorded_at) OR input.recorded_at>clock_timestamp() THEN clock_timestamp() END AS fallback_at
 FROM input WHERE input.video_id=ANY(%(owned_ids)s::text[])), projected AS (
 SELECT sources.video_id,sources.fallback_at,sources.identity_seconds,CASE WHEN fallback_at IS NULL THEN
 (SELECT coalesce(jsonb_agg(rule.rule_index ORDER BY rule.rule_index),'[]'::jsonb) FROM cadence rule
 WHERE rule.enabled AND rule.video_type=sources.video_type AND rule.collection_kind='metadata'
 AND ((sources.video_type='long' AND rule.availability_class=CASE WHEN sources.availability_status='unlisted'
 THEN 'unlisted' ELSE 'public' END AND rule.min_age_seconds<=sources.age_seconds
 AND (rule.max_age_seconds IS NULL OR sources.age_seconds<rule.max_age_seconds))
 OR (sources.video_type IN ('short','stream') AND rule.availability_class='all'
 AND (rule.min_age_seconds=0 OR sources.anchor<sources.recorded_at-make_interval(days=>(rule.min_age_seconds/86400)::integer))
 AND (rule.max_age_seconds IS NULL OR sources.anchor>=sources.recorded_at-make_interval(days=>(rule.max_age_seconds/86400)::integer)))))
 ELSE '[]'::jsonb END AS matches FROM sources)
 SELECT coalesce((SELECT jsonb_agg(to_jsonb(projected)) FROM projected),'[]'::jsonb) AS sources,
 coalesce((SELECT jsonb_agg(to_jsonb(cadence)-'rule_index' ORDER BY rule_index) FROM cadence),'[]'::jsonb) AS cadence"""
_DUE="""least(CASE WHEN schedule.fallback_at IS NOT NULL THEN schedule.fallback_at
 ELSE input.recorded_at+make_interval(secs=>schedule.seconds) END,
 input.recorded_at+make_interval(secs=>%(max_interval_seconds)s::integer),
 CASE WHEN input.scheduled_start_at>input.recorded_at THEN input.scheduled_start_at END,
 CASE WHEN state.availability_resume_observed_at>input.recorded_at THEN state.availability_resume_observed_at END)"""
_COMPLETE="""UPDATE public.video_collection_state state SET stats_due_at=NULL,stats_source_checked_at=NULL,
 metadata_due_at="""+_DUE+""",
 metadata_source_checked_at=input.recorded_at,hydration_needed=true,policy_version=1,paused_reason=NULL,
 availability_first_missing_at=NULL,availability_schedule_type=NULL,
 availability_resume_observed_at=CASE WHEN state.availability_resume_observed_at>input.recorded_at
 THEN state.availability_resume_observed_at END,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 consecutive_failures=0,last_error_type=NULL,last_success_at=greatest(state.last_success_at,input.recorded_at),
 next_due_at="""+_DUE+""",
 typed_collection_status='awaiting_completion',typed_collection_checked_at=input.recorded_at,updated_at=now()
 FROM ("""+_INPUT+""") input JOIN jsonb_to_recordset(%(schedules)s::jsonb) schedule(video_id text,seconds integer,fallback_at timestamptz)
 USING(video_id) WHERE input.video_id=ANY(%(owned_ids)s::text[]) AND state.video_id=input.video_id
 AND state.channel_id=input.channel_id AND state.video_type IS NOT DISTINCT FROM nullif(input.video_type,'unknown')
 AND state.lease_token=input.lease_token
 AND (state.typed_collection_checked_at IS NULL OR state.typed_collection_checked_at<=input.recorded_at)"""


def _reject(message):raise DatabaseServerError(message,code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('complete_stream_lifecycle_probe')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx


def _schedules(captured):
    if not captured['sources']:return []
    try:
        snapshot=_decode_cadence_rows(captured['cadence']) if captured['cadence'] else None
        schedules=[]
        for source in captured['sources']:
            if source['fallback_at'] is not None:
                schedules.append(dict(video_id=source['video_id'],fallback_at=source['fallback_at'],seconds=None))
                continue
            if source.get('identity_seconds') is not None:
                schedules.append(dict(video_id=source['video_id'],fallback_at=None,seconds=source['identity_seconds']))
                continue
            rules=[snapshot.rules[index] for index in source['matches']] if snapshot else []
            selected=max(rules,key=lambda rule:(rule.policy_version,rule.min_age_seconds),default=None)
            if selected is None:_reject('typed_video_collection_cadence_missing')
            jitter=int(hashlib.md5(source['video_id'].encode('utf-8')).hexdigest()[:8],16)%(selected.jitter_window_seconds+1)
            schedules.append(dict(video_id=source['video_id'],fallback_at=None,
                seconds=selected.cadence_seconds-selected.jitter_window_seconds+jitter))
        return schedules
    except PolicyError as error:raise DatabaseServerError(error.code,code=error.sqlstate) from None


def complete_stream_lifecycle_probe(database,rows,*,transaction=None):
    """Return requested count, including exact accepted typed receipt replays."""
    with _transaction(database,transaction) as tx:
        if not isinstance(rows,list):_reject('stream_lifecycle_completion_requires_array')
        raw=Jsonb(rows)
        params=dict(rows=raw,indices=None)
        count=tx.fetch_one(_COUNT,params)['count']
        if count==0:return 0
        if count>50:_reject('stream_lifecycle_completion_input_invalid')
        if tx.fetch_one(_VALID,params)['valid'] is not True:_reject('stream_lifecycle_completion_input_invalid')
        tx.fetch_all(_LOCK,params,max_rows=count)
        _clock_check(tx,raw)
        if tx.fetch_one(_MISMATCH,params)['mismatch']:_reject('stream_lifecycle_completion_receipt_mismatch')
        pending=tx.fetch_one(_PENDING,params)['indices']
        params=dict(rows=raw,indices=pending)
        if tx.fetch_one(_STALE,params)['stale']:_reject('stream_lifecycle_completion_stale_or_ineligible')
        owned=[row['video_id'] for row in tx.returning_all(_INSERT,params,max_rows=count)]
        owned_params=dict(rows=raw,owned_ids=owned)
        if tx.fetch_one(_REGRESSION,owned_params)['regression']:_reject('typed_completion_observation_clock_regression')
        if tx.fetch_one(_MISMATCH,params)['mismatch']:_reject('stream_lifecycle_completion_receipt_mismatch')
        scopes=tx.fetch_one(_SCOPES,owned_params)['scopes']
        tx.fetch_all(_INCIDENT_LOCK,owned_params,max_rows=count)
        schedules=_schedules(tx.fetch_one(_POLICY,dict(owned_params,cadence=cadence_parameter(tx),
            identity_seconds=require_cadence_snapshot(tx).rechecks.identity_seconds)))
        with source_clock_invalidation(tx,owned):
            if tx.execute(_AVAILABILITY,owned_params)!=len(owned):
                _reject('typed_completion_owned_settlement_count_mismatch')
        restored=tx.returning_all(_RESTORE,owned_params,max_rows=count)
        tx.execute(_EVENTS,(Jsonb(restored),))
        if tx.execute(_COMPLETE,dict(owned_params,schedules=Jsonb(schedules),max_interval_seconds=MAX_INTERVAL_SECONDS))!=len(owned):
            _reject('typed_completion_owned_settlement_count_mismatch')
        _clock_check(tx,raw)
        assert_channel_scopes(tx,scopes)
        return count
