"""Long metadata completion borrowed by the public profile operation.

The separate profile wrapper's availability-history locks/restoration are
not part of this base operation. Native casts, bounded batch DML, source clocks,
receipt replay and captured owner fences stay in one owned/borrowed transaction.
"""
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from collection_policy import MAX_INTERVAL_SECONDS,availability_class,select_long_policy_at_age,select_counter_policy_at_age,PolicyError
from ..channel.channel_control import assert_channel_scopes
from .collection_policy import cadence_parameter, _SQL as _CADENCE_SQL,_decode_cadence_rows
from ..base.operation_contracts import get_operation
from .video_source_invalidation import source_clock_invalidation
from ..base.postgres import DatabaseServerError,Operation
from .video_counter_completion import _clock_check

_DEFINITION='''video_id text,lease_token uuid,collection_attempt_id uuid,channel_id text,title text,
 views bigint,like_count bigint,comment_count bigint,recorded_at timestamptz,metadata_observed_at timestamptz,published_at timestamptz,
 duration text,tags text[],category_id text,availability_status text,privacy_status text,upload_status text,
 availability_checked_at timestamptz,stream_actual_end_at timestamptz,youtube_topic_categories text[],
 default_audio_language text,default_language text,caption_available boolean'''
_INPUT="SELECT input.*,raw.value ? 'youtube_topic_categories' AS profile_collected,raw.value ? 'metadata_observed_at' AS metadata_supplied FROM jsonb_array_elements(%s::jsonb) raw(value) CROSS JOIN LATERAL jsonb_to_record(raw.value) input("+_DEFINITION+")"
_OWNED_DEFINITION=_DEFINITION+',profile_collected boolean'
_PROFILE_ARRAY="""SELECT NOT EXISTS(SELECT 1 FROM jsonb_array_elements(%s::jsonb) raw(value)
 WHERE raw.value ? 'youtube_topic_categories'
 AND jsonb_typeof(raw.value->'youtube_topic_categories') IS DISTINCT FROM 'array') AS valid"""
_COUNT='SELECT count(*)::integer AS count FROM ('+_INPUT+') input'
_VALID='''WITH input AS MATERIALIZED ('''+_INPUT+''') SELECT
 NOT EXISTS(SELECT 1 FROM input WHERE video_id IS NULL OR btrim(video_id)='' OR lease_token IS NULL
 OR collection_attempt_id IS NULL OR channel_id IS NULL OR btrim(channel_id)='' OR title IS NULL
 OR views IS NULL OR views<0 OR like_count<0 OR comment_count<0 OR recorded_at IS NULL OR published_at IS NULL
 OR (metadata_supplied AND metadata_observed_at IS NULL))
 AND NOT EXISTS(SELECT video_id FROM input GROUP BY video_id HAVING count(*)>1) AS valid'''
_PROFILE_VALID='''WITH input AS MATERIALIZED ('''+_INPUT+''') SELECT NOT EXISTS(
 SELECT 1 FROM input WHERE profile_collected AND (cardinality(youtube_topic_categories)>50
 OR char_length(default_audio_language)>64 OR char_length(default_language)>64
 OR EXISTS(SELECT 1 FROM unnest(youtube_topic_categories) topic(url) WHERE url IS NULL OR char_length(url)>512
 OR url !~ '^https://[A-Za-z0-9-]+([.][A-Za-z0-9-]+)*[.]wikipedia[.]org/wiki/[^?#[:space:]]+$'))) AS valid'''
_LOCK='''SELECT state.video_id FROM public.video_collection_state state
 JOIN ('''+_INPUT+''') input ON state.video_id=input.video_id AND state.lease_token=input.lease_token
 AND state.channel_id=input.channel_id WHERE NOT EXISTS(SELECT 1 FROM public.video_snapshots snapshot
 WHERE snapshot.video_id=input.video_id AND snapshot.collection_attempt_id=input.collection_attempt_id)
 ORDER BY state.video_id FOR UPDATE OF state'''
_AVAILABILITY_CLOCKS='''WITH checks AS MATERIALIZED (
 SELECT coalesce(input.availability_checked_at,input.metadata_observed_at,input.recorded_at) AS at,
 coalesce(input.metadata_observed_at,input.recorded_at) AS metadata_at,clock_timestamp() AS wall
 FROM jsonb_to_recordset(%s::jsonb) input(recorded_at timestamptz,metadata_observed_at timestamptz,availability_checked_at timestamptz))
 SELECT coalesce(bool_and(at IS NOT NULL AND isfinite(at) AND at>=wall-interval '696 hours' AND at<=wall
 AND metadata_at IS NOT NULL AND isfinite(metadata_at) AND metadata_at>=wall-interval '696 hours' AND metadata_at<=wall),true) AS valid FROM checks'''
_STALE='''SELECT EXISTS(SELECT 1 FROM ('''+_INPUT+''') input
 LEFT JOIN public.video_collection_state state ON state.video_id=input.video_id
 WHERE state.video_id IS NULL OR ((state.lease_token IS DISTINCT FROM input.lease_token
 OR state.channel_id IS DISTINCT FROM input.channel_id OR state.lease_expires_at IS NULL
 OR state.lease_expires_at<=clock_timestamp()) AND NOT EXISTS(SELECT 1 FROM public.video_snapshots snapshot
 WHERE snapshot.video_id=input.video_id AND snapshot.collection_attempt_id=input.collection_attempt_id))) AS stale'''
_OWNED='''SELECT count(*)::integer AS count,coalesce(jsonb_agg(to_jsonb(input)),'[]'::jsonb) AS rows,
 coalesce(jsonb_agg(DISTINCT jsonb_build_object('channel_id',state.channel_id,'control_epoch',state.lease_channel_epoch)),'[]'::jsonb) AS scopes
 FROM ('''+_INPUT+''') input JOIN public.video_collection_state state
 ON state.video_id=input.video_id AND state.lease_token=input.lease_token AND state.channel_id=input.channel_id
 WHERE NOT EXISTS(SELECT 1 FROM public.video_snapshots snapshot WHERE snapshot.video_id=input.video_id
 AND snapshot.collection_attempt_id=input.collection_attempt_id)'''
_UPSERT='''INSERT INTO public.videos AS video(video_id,channel_id,title,views,checked_at,published_at,duration,
 like_count,comment_count,tags,category_id,availability_status,privacy_status,upload_status,availability_checked_at,
 metadata_checked_at,stream_actual_end_at,youtube_topic_categories,default_audio_language,default_language,caption_available)
 SELECT input.video_id,input.channel_id,input.title,input.views,input.recorded_at,input.published_at,input.duration,
 input.like_count,input.comment_count,coalesce(input.tags,ARRAY[]::text[]),input.category_id,input.availability_status,
 input.privacy_status,input.upload_status,coalesce(input.availability_checked_at,input.metadata_observed_at,input.recorded_at),
 coalesce(input.metadata_observed_at,input.recorded_at),
 input.stream_actual_end_at,input.youtube_topic_categories,
 CASE WHEN input.profile_collected THEN input.default_audio_language END,
 CASE WHEN input.profile_collected THEN input.default_language END,
 CASE WHEN input.profile_collected THEN input.caption_available END
 FROM jsonb_to_recordset(%s::jsonb) input('''+_OWNED_DEFINITION+''')
 JOIN public.video_collection_state state ON state.video_id=input.video_id AND state.lease_token=input.lease_token
 AND state.channel_id=input.channel_id ON CONFLICT(video_id) DO UPDATE SET channel_id=excluded.channel_id,
 title=excluded.title,views=excluded.views,checked_at=excluded.checked_at,published_at=excluded.published_at,
 duration=excluded.duration,like_count=excluded.like_count,comment_count=excluded.comment_count,tags=excluded.tags,
 category_id=excluded.category_id,availability_status=excluded.availability_status,privacy_status=excluded.privacy_status,
 upload_status=excluded.upload_status,availability_checked_at=excluded.availability_checked_at,
 metadata_checked_at=excluded.metadata_checked_at,stream_actual_end_at=excluded.stream_actual_end_at,
 youtube_topic_categories=excluded.youtube_topic_categories,default_audio_language=excluded.default_audio_language,
 default_language=excluded.default_language,caption_available=excluded.caption_available,
 publication_status=NULL,scheduled_start_at=NULL
 WHERE (video.checked_at IS NULL OR excluded.checked_at>=video.checked_at)
 AND (video.metadata_checked_at IS NULL OR excluded.metadata_checked_at>=video.metadata_checked_at)
 AND (video.availability_checked_at IS NULL OR excluded.availability_checked_at>=video.availability_checked_at)'''
_SNAPSHOTS='''INSERT INTO public.video_snapshots(video_id,views,like_count,comment_count,recorded_at,collection_attempt_id)
 SELECT input.video_id,input.views,input.like_count,input.comment_count,input.recorded_at,input.collection_attempt_id
 FROM jsonb_to_recordset(%s::jsonb) input('''+_OWNED_DEFINITION+''') JOIN public.video_collection_state state
 ON state.video_id=input.video_id AND state.lease_token=input.lease_token AND state.channel_id=input.channel_id
 ON CONFLICT(video_id,collection_attempt_id) WHERE collection_attempt_id IS NOT NULL DO NOTHING'''
_POLICY_INPUT='''WITH cadence AS MATERIALIZED ('''+_CADENCE_SQL+''' LIMIT 1001),
 sources AS MATERIALIZED (SELECT input.*,state.video_type AS schedule_video_type,clock_timestamp() AS metadata_clock
 FROM jsonb_to_recordset(%s::jsonb) input('''+_OWNED_DEFINITION+''') JOIN public.video_collection_state state
 ON state.video_id=input.video_id AND state.lease_token=input.lease_token AND state.channel_id=input.channel_id),
 projected AS (SELECT video_id,channel_id,lease_token,recorded_at,schedule_video_type,
 coalesce(metadata_observed_at,recorded_at) AS metadata_observed_at,profile_collected,availability_status,
 coalesce(availability_checked_at,metadata_observed_at,recorded_at) AS availability_checked_at,
 published_at IS NULL OR NOT isfinite(published_at) AS published_invalid,
 CASE WHEN EXISTS(SELECT 1 FROM cadence rule WHERE rule.enabled AND rule.video_type=sources.schedule_video_type
 AND rule.collection_kind='counters' AND rule.availability_class=CASE WHEN sources.availability_status='unlisted'
 THEN 'unlisted' WHEN coalesce(sources.availability_status,'public')='public' THEN 'public' END)
 THEN greatest(extract(epoch FROM (recorded_at-published_at))::bigint,0) ELSE 0 END AS age_seconds,
 CASE WHEN EXISTS(SELECT 1 FROM cadence rule WHERE rule.enabled AND rule.video_type='long'
 AND rule.collection_kind='metadata' AND rule.availability_class=CASE WHEN sources.availability_status='unlisted'
 THEN 'unlisted' WHEN coalesce(sources.availability_status,'public')='public' THEN 'public' END)
 THEN greatest(extract(epoch FROM (coalesce(metadata_observed_at,recorded_at)-published_at))::bigint,0) ELSE 0 END AS metadata_age_seconds,
 recorded_at IS NULL OR NOT isfinite(recorded_at) OR recorded_at>clock_timestamp() AS stats_now,
 coalesce(metadata_observed_at,recorded_at) IS NULL OR NOT isfinite(coalesce(metadata_observed_at,recorded_at))
 OR coalesce(metadata_observed_at,recorded_at)>clock_timestamp() AS metadata_now,
 recorded_at IS NOT NULL AND isfinite(recorded_at) AND recorded_at<=metadata_clock
 AND coalesce(metadata_observed_at,recorded_at) IS NOT NULL AND isfinite(coalesce(metadata_observed_at,recorded_at))
 AND coalesce(metadata_observed_at,recorded_at)<=metadata_clock AS as_of_valid,
 NOT isfinite(coalesce(availability_checked_at,metadata_observed_at,recorded_at))
 OR coalesce(availability_checked_at,metadata_observed_at,recorded_at)>metadata_clock AS availability_now FROM sources)
 SELECT coalesce((SELECT jsonb_agg(to_jsonb(projected)) FROM projected),'[]'::jsonb) AS sources,
 coalesce((SELECT jsonb_agg(to_jsonb(cadence)) FROM cadence),'[]'::jsonb) AS cadence'''
_COMPLETE='''UPDATE public.video_collection_state state SET stats_due_at=schedule.stats_due_at,
 metadata_due_at=least(schedule.metadata_due_at,CASE WHEN state.availability_resume_observed_at>input.availability_checked_at
 THEN state.availability_resume_observed_at END),stats_source_checked_at=input.recorded_at,
 metadata_source_checked_at=input.metadata_observed_at,next_due_at=least(schedule.stats_due_at,schedule.metadata_due_at,
 CASE WHEN state.availability_resume_observed_at>input.availability_checked_at THEN state.availability_resume_observed_at END),
 policy_version=input.policy_version,hydration_needed=coalesce(state.availability_resume_observed_at>input.availability_checked_at,false),
 availability_first_missing_at=NULL,availability_schedule_type=NULL,
 typed_collection_status=CASE WHEN state.video_type='long' THEN NULL ELSE state.typed_collection_status END,
 availability_resume_observed_at=CASE WHEN state.availability_resume_observed_at>input.availability_checked_at
 THEN state.availability_resume_observed_at END,
 profile_metadata_version=CASE WHEN input.profile_collected THEN 1 ELSE 0 END,paused_reason=input.paused_reason,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 consecutive_failures=0,last_error_type=NULL,last_success_at=input.recorded_at,updated_at=now()
 FROM jsonb_to_recordset(%s::jsonb) input(video_id text,channel_id text,lease_token uuid,recorded_at timestamptz,metadata_observed_at timestamptz,
 availability_checked_at timestamptz,profile_collected boolean,published_invalid boolean,
 availability_supported boolean,stats_now boolean,metadata_now boolean,availability_now boolean,
 stats_seconds bigint,metadata_seconds bigint,policy_version integer,paused_reason text)
 CROSS JOIN LATERAL(SELECT CASE WHEN input.published_invalid THEN input.recorded_at
 WHEN NOT input.availability_supported THEN NULL WHEN input.stats_now THEN input.recorded_at
 ELSE input.recorded_at+make_interval(secs=>input.stats_seconds::double precision) END AS stats_due_at,
 CASE WHEN input.published_invalid THEN input.metadata_observed_at WHEN NOT input.availability_supported THEN NULL
 WHEN input.metadata_now THEN input.metadata_observed_at
 ELSE input.metadata_observed_at+make_interval(secs=>input.metadata_seconds::double precision) END AS metadata_due_at) nominal
 CROSS JOIN LATERAL(SELECT nominal.stats_due_at,least(coalesce(nominal.metadata_due_at,input.metadata_observed_at),
 CASE WHEN input.availability_now THEN input.metadata_observed_at ELSE input.availability_checked_at+make_interval(secs=>%s::integer) END) AS metadata_due_at) schedule
 WHERE state.video_id=input.video_id AND state.lease_token=input.lease_token AND state.channel_id=input.channel_id'''


def _reject():raise DatabaseServerError('database_operation_failed',code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('complete_video_metadata_collection')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as owned:yield owned


def _clocks(tx,raw):
    _clock_check(tx,raw)
    if tx.fetch_one(_AVAILABILITY_CLOCKS,(raw,))['valid'] is not True:
        raise DatabaseServerError('hot_source_clock_out_of_window',code='22023')


def _schedules(captured):
    try:
        snapshot=_decode_cadence_rows(captured['cadence']) if captured['cadence'] else None
        result=[]
        for source in captured['sources']:
            if source['as_of_valid'] is not True:
                raise DatabaseServerError('hot_video_refresh_as_of_invalid',code='P0001')
            stats=select_counter_policy_at_age(snapshot,source.get('schedule_video_type','long'),source['availability_status'],source['age_seconds']) if snapshot else None
            metadata=select_long_policy_at_age(snapshot,'metadata',source['availability_status'],source['metadata_age_seconds']) if snapshot else None
            result.append({**source,'availability_supported':availability_class(source['availability_status']) is not None,
                'stats_seconds':stats.cadence_seconds if stats else None,'metadata_seconds':metadata.cadence_seconds if metadata else None,
                'policy_version':max(stats.policy_version if stats else 1,metadata.policy_version if metadata else 1),
                'paused_reason':None if availability_class(source['availability_status']) is not None else 'availability_unsupported'})
        return result
    except PolicyError as error:raise DatabaseServerError(error.code,code=error.sqlstate) from None


def complete_video_metadata_collection(database,rows,*,transaction=None):
    """Return requested count; successful prior snapshot receipts do not mutate."""
    with _transaction(database,transaction) as tx:
        if not isinstance(rows,list):_reject()
        raw=Jsonb(rows)
        if tx.fetch_one(_PROFILE_ARRAY,(raw,))['valid'] is not True:_reject()
        count=tx.fetch_one(_COUNT,(raw,))['count']
        if count==0:return 0
        if count>1000:_reject()
        if tx.fetch_one(_VALID,(raw,))['valid'] is not True:_reject()
        if tx.fetch_one(_PROFILE_VALID,(raw,))['valid'] is not True:_reject()
        tx.fetch_all(_LOCK,(raw,),max_rows=count)
        _clocks(tx,raw)
        if tx.fetch_one(_STALE,(raw,))['stale']:_reject()
        owned=tx.fetch_one(_OWNED,(raw,))
        if owned['count']==0:
            # Retained base function repeats these two clock passes on replay.
            _clocks(tx,raw);_clocks(tx,raw)
            return count
        pending=Jsonb(owned['rows'])
        with source_clock_invalidation(tx,[row['video_id'] for row in owned['rows']]):
            if tx.execute(_UPSERT,(pending,))!=owned['count']:_reject()
        if tx.execute(_SNAPSHOTS,(pending,))!=owned['count']:_reject()
        schedules=_schedules(tx.fetch_one(_POLICY_INPUT,(cadence_parameter(tx),pending)))
        if tx.execute(_COMPLETE,(Jsonb(schedules),MAX_INTERVAL_SECONDS))!=owned['count']:_reject()
        _clocks(tx,raw)
        assert_channel_scopes(tx,owned['scopes'])
        return count
