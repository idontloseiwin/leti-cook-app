"""Atomic counter completion used by the public leased video repository.

One bounded batch owns existing queue locks, source writes, snapshot attempt
receipts and queue settlement. Replay preserves the retained receipt semantics:
valid changed payload is ignored when that video/attempt snapshot exists. No
retry or provider call is performed here. Cadence and source facts share one
native statement snapshot; Python selects policy and PostgreSQL owns all clocks,
age rounding and timestamp arithmetic, including BC/extended publication years.
"""
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from collection_policy import MAX_INTERVAL_SECONDS,availability_class,select_counter_policy_at_age,select_long_policy_at_age,PolicyError
from ..channel.channel_control import assert_channel_scopes
from .collection_policy import cadence_parameter, _SQL as _CADENCE_SQL,_decode_cadence_rows
from ..base.operation_contracts import get_operation
from .video_source_invalidation import source_clock_invalidation
from .typed_video_read_policy import TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL
from ..base.postgres import DatabaseServerError,Operation

_DEFINITION='video_id text,lease_token uuid,collection_attempt_id uuid,views bigint,like_count bigint,comment_count bigint,recorded_at timestamptz'
_INPUT='SELECT * FROM jsonb_to_recordset(%s::jsonb) input('+_DEFINITION+')'
# jsonb_to_recordset performs native casts before the legacy1000-row cap. Count
# only: never transfer/materialize an oversized parsed array in Python.
_COUNT='SELECT count(*)::integer AS count FROM jsonb_to_recordset(%s::jsonb) input('+_DEFINITION+')'
_VALID='''WITH input AS MATERIALIZED ('''+_INPUT+''') SELECT
 NOT EXISTS(SELECT 1 FROM input WHERE video_id IS NULL OR btrim(video_id)='' OR lease_token IS NULL
 OR collection_attempt_id IS NULL OR views IS NULL OR views<0 OR like_count<0 OR comment_count<0 OR recorded_at IS NULL)
 AND NOT EXISTS(SELECT video_id FROM input GROUP BY video_id HAVING count(*)>1) AS valid'''
_LOCK='''SELECT state.video_id FROM public.video_collection_state state
 JOIN jsonb_to_recordset(%s::jsonb) input('''+_DEFINITION+''') ON input.video_id=state.video_id
 ORDER BY state.video_id FOR UPDATE OF state'''
_CLOCKS='''WITH checks AS MATERIALIZED (
 SELECT input.recorded_at,clock_timestamp() AS at FROM jsonb_to_recordset(%s::jsonb) input(recorded_at timestamptz)
 ) SELECT coalesce(bool_and(recorded_at IS NOT NULL AND isfinite(recorded_at)
 AND recorded_at>=at-interval '696 hours' AND recorded_at<=at),true) AS valid FROM checks'''
_STALE='''SELECT EXISTS(SELECT 1 FROM jsonb_to_recordset(%s::jsonb) input('''+_DEFINITION+''')
 LEFT JOIN public.video_collection_state state ON state.video_id=input.video_id
 LEFT JOIN public.videos video ON video.video_id=input.video_id
 WHERE state.video_id IS NULL OR ((state.lease_token IS DISTINCT FROM input.lease_token
 OR state.lease_expires_at IS NULL OR state.lease_expires_at<=clock_timestamp()
 OR state.video_type IS NULL OR state.video_type NOT IN ('long','short','stream')
 OR video.publication_status IS NOT NULL
 OR (state.video_type IN ('short','stream') AND NOT EXISTS(
 SELECT 1 FROM ('''+TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL+''') candidate
 WHERE candidate.video_id=input.video_id AND candidate.channel_id=state.channel_id
 AND candidate.video_type=state.video_type))
 OR (state.video_type='stream' AND (video.stream_actual_end_at IS NULL
 OR NOT isfinite(video.stream_actual_end_at) OR video.stream_actual_end_at>input.recorded_at)))
 AND NOT EXISTS(SELECT 1 FROM public.video_snapshots snapshot WHERE snapshot.video_id=input.video_id
 AND snapshot.collection_attempt_id=input.collection_attempt_id))) AS stale'''
_PENDING='''SELECT coalesce(jsonb_agg(to_jsonb(input)),'[]'::jsonb) AS rows
 FROM jsonb_to_recordset(%s::jsonb) input('''+_DEFINITION+''')
 WHERE NOT EXISTS(SELECT 1 FROM public.video_snapshots snapshot
 WHERE snapshot.video_id=input.video_id AND snapshot.collection_attempt_id=input.collection_attempt_id)'''
_OWNED='''SELECT count(*)::integer AS count,
 coalesce(jsonb_agg(DISTINCT jsonb_build_object('channel_id',state.channel_id,'control_epoch',state.lease_channel_epoch)),'[]'::jsonb) AS scopes
 FROM jsonb_to_recordset(%s::jsonb) input('''+_DEFINITION+''')
 JOIN public.video_collection_state state ON state.video_id=input.video_id AND state.lease_token=input.lease_token'''
_UPDATE_SOURCE='''UPDATE public.videos video SET views=input.views,like_count=input.like_count,
 comment_count=input.comment_count,checked_at=input.recorded_at
 FROM jsonb_to_recordset(%s::jsonb) input('''+_DEFINITION+''')
 JOIN public.video_collection_state state ON state.video_id=input.video_id AND state.lease_token=input.lease_token
 WHERE video.video_id=input.video_id AND (video.checked_at IS NULL OR input.recorded_at>=video.checked_at)'''
_INSERT_SNAPSHOTS='''INSERT INTO public.video_snapshots(video_id,views,like_count,comment_count,recorded_at,collection_attempt_id)
 SELECT input.video_id,input.views,input.like_count,input.comment_count,input.recorded_at,input.collection_attempt_id
 FROM jsonb_to_recordset(%s::jsonb) input('''+_DEFINITION+''')
 JOIN public.video_collection_state state ON state.video_id=input.video_id AND state.lease_token=input.lease_token
 ON CONFLICT(video_id,collection_attempt_id) WHERE collection_attempt_id IS NOT NULL DO NOTHING'''
_POLICY_INPUT='''WITH cadence AS MATERIALIZED ('''+_CADENCE_SQL+''' LIMIT 1001),
 sources AS MATERIALIZED (
 SELECT input.video_id,input.lease_token,input.recorded_at,state.video_type,state.policy_version,
 video.published_at,video.availability_status,
 video.availability_checked_at,state.metadata_due_at,clock_timestamp() AS metadata_clock
 FROM jsonb_to_recordset(%s::jsonb) input('''+_DEFINITION+''')
 JOIN public.video_collection_state state ON state.video_id=input.video_id AND state.lease_token=input.lease_token
 JOIN public.videos video ON video.video_id=input.video_id
 ), projected AS (
 SELECT sources.video_id,sources.lease_token,sources.recorded_at,sources.video_type,sources.policy_version,
 sources.metadata_due_at,sources.availability_checked_at,
 sources.availability_status,published_at IS NULL OR NOT isfinite(published_at) AS published_invalid,
 CASE WHEN EXISTS(SELECT 1 FROM cadence rule WHERE rule.enabled AND rule.video_type=sources.video_type
 AND rule.collection_kind IN ('counters','metadata')
 AND rule.availability_class=CASE WHEN sources.availability_status='unlisted' THEN 'unlisted'
 WHEN coalesce(sources.availability_status,'public')='public' THEN 'public' END)
 THEN greatest(extract(epoch FROM (recorded_at-published_at))::bigint,0) ELSE 0 END AS age_seconds,
 recorded_at IS NULL OR NOT isfinite(recorded_at) OR recorded_at>clock_timestamp() AS stats_now,
 recorded_at IS NOT NULL AND isfinite(recorded_at) AND recorded_at<=metadata_clock AS as_of_valid,
 availability_checked_at IS NULL OR NOT isfinite(availability_checked_at)
 OR availability_checked_at>metadata_clock AS availability_now
 FROM sources)
 SELECT coalesce((SELECT jsonb_agg(to_jsonb(projected)) FROM projected),'[]'::jsonb) AS sources,
 coalesce((SELECT jsonb_agg(to_jsonb(cadence)) FROM cadence),'[]'::jsonb) AS cadence'''
_COMPLETE='''UPDATE public.video_collection_state state SET
 stats_due_at=schedule.stats_due_at,stats_source_checked_at=input.recorded_at,
 metadata_due_at=least(schedule.metadata_due_at,state.availability_resume_observed_at),
 next_due_at=CASE WHEN input.paused_reason IS NOT NULL THEN NULL
 ELSE least(schedule.stats_due_at,schedule.metadata_due_at,state.availability_resume_observed_at) END,
 policy_version=input.policy_version,paused_reason=input.paused_reason,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 consecutive_failures=0,last_error_type=NULL,last_success_at=input.recorded_at,updated_at=now()
 FROM jsonb_to_recordset(%s::jsonb) input(video_id text,lease_token uuid,recorded_at timestamptz,
 metadata_due_at timestamptz,availability_checked_at timestamptz,published_invalid boolean,
 availability_supported boolean,stats_now boolean,availability_now boolean,stats_seconds bigint,
 policy_version integer,paused_reason text)
 CROSS JOIN LATERAL(SELECT CASE WHEN input.published_invalid THEN input.recorded_at
 WHEN NOT input.availability_supported THEN NULL WHEN input.stats_now THEN input.recorded_at
 ELSE input.recorded_at+make_interval(secs=>input.stats_seconds::double precision) END AS stats_due_at,
 least(coalesce(input.metadata_due_at,input.recorded_at),CASE WHEN input.availability_now THEN input.recorded_at
 ELSE input.availability_checked_at+make_interval(secs=>%s::integer) END) AS metadata_due_at) schedule
 WHERE state.video_id=input.video_id AND state.lease_token=input.lease_token'''


def _reject():raise DatabaseServerError('database_operation_failed',code='P0001')


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('complete_video_counter_collection')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as owned:yield owned


def _clock_check(tx,rows):
    if tx.fetch_one(_CLOCKS,(rows,))['valid'] is not True:
        raise DatabaseServerError('hot_source_clock_out_of_window',code='22023')


def _schedules(captured):
    sources=captured['sources']
    if not sources:return []
    try:
        snapshot=_decode_cadence_rows(captured['cadence']) if captured['cadence'] else None
        result=[]
        for source in sources:
            if source['as_of_valid'] is not True:
                raise DatabaseServerError('hot_video_refresh_as_of_invalid',code='P0001')
            video_type=source['video_type']
            if video_type not in ('long','short','stream'):
                _reject()
            stats=select_counter_policy_at_age(snapshot,video_type,source['availability_status'],source['age_seconds']) if snapshot else None
            metadata=select_long_policy_at_age(snapshot,'metadata',source['availability_status'],source['age_seconds']) if snapshot and video_type=='long' else None
            supported=availability_class(source['availability_status']) is not None
            result.append({**source,'availability_supported':supported,'stats_seconds':stats.cadence_seconds if stats else None,
                'policy_version':max(source['policy_version'],stats.policy_version if stats else 1,metadata.policy_version if metadata else 1),
                'paused_reason':None if supported else 'availability_unsupported'})
        return result
    except PolicyError as error:
        raise DatabaseServerError(error.code,code=error.sqlstate) from None


def complete_video_counter_collection(database,rows,*,transaction=None):
    """Return requested count, including valid prior snapshot-receipt replays."""
    with _transaction(database,transaction) as tx:
        if not isinstance(rows,list):_reject()
        raw=Jsonb(rows)
        count=tx.fetch_one(_COUNT,(raw,))['count']
        if count==0:return 0
        if count>1000:_reject()
        if tx.fetch_one(_VALID,(raw,))['valid'] is not True:_reject()
        tx.fetch_all(_LOCK,(raw,),max_rows=count)
        _clock_check(tx,raw)
        if tx.fetch_one(_STALE,(raw,))['stale']:_reject()
        pending=Jsonb(tx.fetch_one(_PENDING,(raw,))['rows'])
        owned=tx.fetch_one(_OWNED,(pending,))
        with source_clock_invalidation(tx,[row['video_id'] for row in pending.obj]):
            if tx.execute(_UPDATE_SOURCE,(pending,))!=owned['count']:_reject()
        if tx.execute(_INSERT_SNAPSHOTS,(pending,))!=owned['count']:_reject()
        schedules=_schedules(tx.fetch_one(_POLICY_INPUT,(cadence_parameter(tx),pending)))
        if tx.execute(_COMPLETE,(Jsonb(schedules),MAX_INTERVAL_SECONDS))!=owned['count']:_reject()
        _clock_check(tx,raw)
        assert_channel_scopes(tx,owned['scopes'])
        return count
