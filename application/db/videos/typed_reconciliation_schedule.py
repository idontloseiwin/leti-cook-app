"""Fixed native conflict acquisition plus one Python-planned batch settlement.

A provisional same-value UPDATE obtains the actual conflict tuple using native
UPSERT arbitration. This deliberately adds MVCC/WAL; all provisional work belongs
to the original transaction and rolls back on policy, bound or fence error.
An unknown COMMIT ACK is reconciled by the owner; rollback is not assumed.
The source/cadence/availability facts share the acquisition statement snapshot.
"""
import json
from psycopg.types.json import Jsonb
from collection_policy import CADENCE_PRIORITY_FIELDS, MAX_INTERVAL_SECONDS, PolicyError
from typed_reconciliation_policy import LEGACY_CHANNEL_PAUSES, reconcile_schedule
from .typed_video_read_policy import TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL
from ..base.postgres import DatabaseError, DatabaseServerError
from .collection_policy import cadence_parameter, _NAMED_SQL as _CADENCE_SQL

_COLUMNS = ('video_id','channel_id','video_type','stats_due_at','metadata_due_at',
 'hydration_needed','policy_version','paused_reason','typed_source_published_at','typed_source_published_acquired_at',
 'next_due_at','typed_collection_status','updated_at')
_ACQUIRE_KEYS = frozenset({'video_type','inventory_ids','scopes'})
_LEGACY_SET = '(' + ','.join("'" + value + "'" for value in LEGACY_CHANNEL_PAUSES) + ')'
assert CADENCE_PRIORITY_FIELDS == ('policy_version','min_age_seconds')
_CADENCE_ORDER = ','.join('policy.' + name + ' DESC' for name in CADENCE_PRIORITY_FIELDS)

_SOURCE = """        select candidate.video_id,candidate.channel_id,%(video_type)s::text,null,null,
            video.video_id is null,1,null,candidate.source_published_at,candidate.source_published_acquired_at,now(),
            'pending',now(),video.availability_checked_at
        from (with candidates as materialized (
        select candidate.*
        from unnest(%(inventory_ids)s::text[]) as input(video_id)
        cross join lateral (
            select c.* from (""" + TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL + """) as c
            where c.video_id=input.video_id and c.video_type=%(video_type)s::text
            offset 0
        ) as candidate
        left join public.video_collection_state as state on state.video_id=candidate.video_id
        where EXISTS (SELECT 1 FROM jsonb_array_elements(%(scopes)s::jsonb) scopes(scope) WHERE scope->>'channel_id'=candidate.channel_id)
          AND (state.video_id is null
               or state.video_type is distinct from candidate.video_type
               or state.channel_id is distinct from candidate.channel_id
               or state.paused_reason='typed_identity_not_eligible' or state.paused_reason IN ('channel_hidden','channel_no_long_playlist','channel_inventory_not_ready','channel_inventory_manual_required','channel_inventory_error'))
    ) SELECT * FROM candidates) as candidate
        left join public.videos as video on video.video_id=candidate.video_id
"""
_CONFLICT_WHERE = "        where state.lease_token is null\n          and (state.video_type is distinct from excluded.video_type\n               or state.channel_id is distinct from excluded.channel_id\n               or state.paused_reason='typed_identity_not_eligible' or state.paused_reason IN ('channel_hidden','channel_no_long_playlist','channel_inventory_not_ready','channel_inventory_manual_required','channel_inventory_error'))"

_OLD = '(admitted.previous)'
_LEGACY = _OLD + '.paused_reason IN ' + _LEGACY_SET
_PRESERVE = '(' + _OLD + '.consecutive_failures>0 AND ' + _OLD + '.next_due_at IS NOT NULL)'
_TYPED_NEEDED = '(' + _LEGACY + ') AND NOT ' + _PRESERVE + ' AND ' + _OLD + '.typed_collection_checked_at IS NOT NULL'
_HOT_NEEDED = '(' + _LEGACY + ') AND NOT ' + _PRESERVE + " AND " + _OLD + ".typed_collection_status='hydrated'"
_INPUT_INVALID = _OLD + ".video_type NOT IN ('short','stream') OR " + _OLD + ".video_id IS NULL OR btrim(" + _OLD + ".video_id)='' OR " + _OLD + '.typed_collection_checked_at IS NULL'
_CLOCK_INVALID = 'NOT isfinite(' + _OLD + '.typed_collection_checked_at) OR ' + _OLD + '.typed_collection_checked_at>admitted.test_clock'
_HOT_AS_OF_INVALID = 'now() IS NULL OR NOT isfinite(now()) OR now()>admitted.hot_clock'
_AVAILABILITY_INVALID = 'source_rows.captured_availability IS NULL OR NOT isfinite(source_rows.captured_availability) OR source_rows.captured_availability>admitted.hot_clock'
_ANCHOR = 'coalesce(CASE WHEN ' + _OLD + ".video_type='stream' THEN coalesce(" + _OLD + '.typed_stream_actual_end_at,' + _OLD + '.typed_source_published_at) ELSE ' + _OLD + '.typed_source_published_at END,' + _OLD + '.typed_collection_checked_at)'
# Fixed bounded match/rank facts use the shared pure ranking vocabulary. The
# winner is unique under the validated30-rule process snapshot.
_CADENCE = """SELECT jsonb_build_object('cadence_seconds',policy.cadence_seconds,
 'jitter_window_seconds',policy.jitter_window_seconds,'policy_version',policy.policy_version,
 'min_age_seconds',policy.min_age_seconds) FROM (""" + _CADENCE_SQL + """) policy
 WHERE policy.video_type=""" + _OLD + """.video_type AND policy.collection_kind='metadata'
 AND policy.availability_class='all' AND policy.enabled
 AND (policy.min_age_seconds=0 OR """ + _ANCHOR + '<' + _OLD + """.typed_collection_checked_at-make_interval(days=>(policy.min_age_seconds/86400)::integer))
 AND (policy.max_age_seconds IS NULL OR """ + _ANCHOR + '>=' + _OLD + """.typed_collection_checked_at-make_interval(days=>(policy.max_age_seconds/86400)::integer))
 ORDER BY """ + _CADENCE_ORDER + ' LIMIT 1'
_ACQUIRE = f"""WITH source_rows({','.join(_COLUMNS)},captured_availability) AS MATERIALIZED (
{_SOURCE}
), admitted AS (
 INSERT INTO public.video_collection_state AS state ({','.join(_COLUMNS)})
 SELECT {','.join(name+'::timestamptz' if name in ('stats_due_at','metadata_due_at') else name for name in _COLUMNS)} FROM source_rows WHERE true
 ON CONFLICT(video_id) DO UPDATE SET updated_at=state.updated_at
{_CONFLICT_WHERE}
 RETURNING state.video_id,state AS previous,clock_timestamp() AS test_clock,
 clock_timestamp() AS fallback_clock,clock_timestamp() AS hot_clock
)
SELECT admitted.video_id,to_jsonb(admitted.previous)::text AS previous,
 to_jsonb(source_rows)::text AS proposed,now()::text AS transaction_at,
 admitted.fallback_clock::text AS fallback_at,
 ({_INPUT_INVALID}) AS typed_input_invalid,({_CLOCK_INVALID}) AS typed_clock_invalid,
 ({_HOT_AS_OF_INVALID}) AS hot_as_of_invalid,({_AVAILABILITY_INVALID}) AS hot_availability_invalid,
 CASE WHEN ({_HOT_NEEDED}) AND ({_HOT_AS_OF_INVALID}) IS NOT TRUE AND ({_AVAILABILITY_INVALID}) IS NOT TRUE
 THEN (source_rows.captured_availability+make_interval(secs=>%(max_interval_seconds)s::integer))::text END AS hot_cap_at,
 CASE WHEN ({_TYPED_NEEDED}) AND ({_INPUT_INVALID}) IS NOT TRUE AND ({_CLOCK_INVALID}) IS NOT TRUE
 THEN ({_CADENCE}) END AS cadence
 FROM admitted JOIN source_rows USING(video_id) LIMIT 1001"""
_FINALIZE = """UPDATE public.video_collection_state state SET channel_id=plan.channel_id,video_type=plan.video_type,
 metadata_due_at=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL)
 OR state.paused_reason='availability_retry_expired' THEN state.metadata_due_at
 ELSE coalesce(state.metadata_due_at,least(plan.base_at+make_interval(secs=>plan.seconds),plan.cap_at,plan.old_cap_at)) END,
 hydration_needed=plan.hydration_needed,policy_version=1,
 paused_reason=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL)
 OR state.paused_reason='availability_retry_expired' THEN state.paused_reason END,
 typed_source_published_at=plan.typed_source_published_at,
 typed_source_published_acquired_at=plan.typed_source_published_acquired_at,
 next_due_at=CASE WHEN (state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL)
 OR state.paused_reason='availability_retry_expired' THEN state.next_due_at
 ELSE coalesce(state.next_due_at,least(state.stats_due_at,state.metadata_due_at,
 plan.base_at+make_interval(secs=>plan.seconds),plan.cap_at,plan.old_cap_at)) END,
 typed_collection_status=coalesce(state.typed_collection_status,'pending'),updated_at=now()
 FROM jsonb_to_recordset(%s::jsonb) plan(video_id text,channel_id text,video_type text,hydration_needed boolean,
 typed_source_published_at timestamptz,typed_source_published_acquired_at timestamptz,base_at timestamptz,seconds integer,cap_at timestamptz,old_cap_at timestamptz)
 WHERE state.video_id=plan.video_id"""


def acquisition_parameters(transaction, parameters):
    """Driver-only merge after its exact three-key capability guard."""
    if type(parameters) is not dict or set(parameters) != _ACQUIRE_KEYS:
        raise DatabaseError('database_typed_reconcile_capability_invalid')
    return dict(parameters, cadence=cadence_parameter(transaction), max_interval_seconds=MAX_INTERVAL_SECONDS)


def reconcile_typed_rows_in_transaction(tx, args):
    """Two batch data commands, one original owner/deadline and at most1000 rows."""
    with tx.borrow():
        rows = tx.typed_reconcile_acquire(video_type=args['video_type'],
            inventory_ids=args['inventory_ids'], scopes=args['scopes'])
        plans, seen = [], set()
        for row in rows:
            previous, proposed = json.loads(row['previous']), json.loads(row['proposed'])
            video_id = row['video_id']
            if video_id in seen or previous['video_id'] != video_id or proposed['video_id'] != video_id:
                raise DatabaseError('database_result_shape_invalid')
            seen.add(video_id)
            try:
                schedule = reconcile_schedule(previous, row)
            except PolicyError as error:
                raise DatabaseServerError(error.code, code=error.sqlstate) from None
            plans.append(dict(video_id=video_id,channel_id=proposed['channel_id'],video_type=proposed['video_type'],
                hydration_needed=proposed['hydration_needed'],typed_source_published_at=proposed['typed_source_published_at'],
                typed_source_published_acquired_at=proposed['typed_source_published_acquired_at'],**schedule))
        if not plans:
            return 0
        affected = tx.execute(_FINALIZE, (Jsonb(plans),))
        if affected != len(plans):
            raise DatabaseError('database_result_shape_invalid')
        return affected
