"""Borrowed queue initialization for page settlement/explicit ensure.

Long keeps its conditional UPSERT; Shorts/Streams only initialize missing rows.
Native SQL projects age/clock facts and performs timestamp arithmetic,
preserving BC, infinity and extended finite years. Python owns cadence
thresholds and pause selection. Injected cadence and source facts are combined in
one statement snapshot. No retained business function is called and no second
transaction is opened.
"""
from __future__ import annotations

from .typed_video_read_policy import (TYPED_CANDIDATE_INPUT_ID_SQL)
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, CHANNEL_CONTROL_SQL, channel_mode_row)
from psycopg.types.json import Jsonb
from .video_initial_policy import VIDEO_HANDOFF_INVENTORY_SQL, VIDEO_HANDOFF_STATE_SQL

from collection_policy import MAX_INTERVAL_SECONDS, availability_class, select_long_policy_at_age, PolicyError
from .collection_policy import cadence_parameter, require_cadence_snapshot, _SQL as _CADENCE_SQL, _decode_cadence_rows
from .video_availability_schedule import missing_schedule
from ..base.postgres import DatabaseServerError

# This is permission to refresh metadata by a saved ID, not proof of a type.
# Duration deliberately does not participate: General is how it becomes known.
LONG_SHORT_CONFLICT_SQL = """EXISTS(SELECT 1 FROM public.video_inventory conflict_inventory
 WHERE conflict_inventory.video_id=%(video_id)s::text
 AND conflict_inventory.channel_id=%(channel_id)s::text
 AND conflict_inventory.video_type IN ('long','short') AND conflict_inventory.skeleton_since IS NULL
 AND NOT EXISTS(SELECT 1 FROM public.video_type_review_cases choice
 WHERE choice.video_id=conflict_inventory.video_id AND choice.decision_type IS NOT NULL)
 AND NOT EXISTS(SELECT 1 FROM public.videos profile WHERE profile.video_id=conflict_inventory.video_id
 AND profile.channel_id<>conflict_inventory.channel_id)
 AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships foreign_member
 WHERE foreign_member.video_id=conflict_inventory.video_id AND foreign_member.membership_status='present'
 AND foreign_member.channel_id<>conflict_inventory.channel_id)
 AND (SELECT array_agg(DISTINCT member.video_type ORDER BY member.video_type)
 FROM public.video_inventory_memberships member WHERE member.video_id=conflict_inventory.video_id
 AND member.channel_id=conflict_inventory.channel_id AND member.membership_status='present'
 AND isfinite(member.last_present_at) AND member.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 AND isfinite(member.last_seen_at) AND member.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp())
 =ARRAY['long','short']::text[])"""
_CONFLICT_INVENTORY = LONG_SHORT_CONFLICT_SQL.replace('%(video_id)s::text','i.video_id').replace('%(channel_id)s::text','i.channel_id')
# Keep a new Short conflict in the existing queue so its missing duration can be
# obtained. A matching ordinary candidate wins; this fallback never admits Stats.
_TYPED_BOOTSTRAP = """SELECT i.video_id,i.channel_id,i.video_type,i.published_at AS source_published_at,
 i.published_acquired_at AS source_published_acquired_at,i.discovered_at AS first_seen_at,
 NULL::uuid AS last_completed_scan_id FROM public.video_inventory i
 JOIN (""" + CHANNEL_CONTROL_SQL + """) control USING(channel_id)
 WHERE i.video_id=input.id AND i.video_type='short' AND control.owner_permission='accepted'
 AND NOT control.is_hidden AND control.collection_mode IN ('active','metadata_only')
 AND """ + _CONFLICT_INVENTORY + """
 AND NOT EXISTS(SELECT 1 FROM (""" + TYPED_CANDIDATE_INPUT_ID_SQL + """) ordinary
 WHERE ordinary.video_id=i.video_id)"""

_INPUT = "SELECT ids,cardinality(ids) AS count FROM (SELECT %s::text[] AS ids) input"
_LONG_INPUT = """
    WITH cadence AS MATERIALIZED (
""" + _CADENCE_SQL + ("""
        LIMIT 1001
    ), sources AS MATERIALIZED (
        SELECT inventory.video_id,inventory.channel_id,video.video_id IS NULL AS needs_hydration,
            video.published_at,video.checked_at,video.metadata_checked_at,video.availability_checked_at,
            video.availability_status,unavailable.video_id IS NOT NULL AS is_unavailable,
            unavailable.first_seen_at AS unavailable_first_missing_at,
            control.policy_facts,control.owner_permission,(""" + VIDEO_HANDOFF_INVENTORY_SQL + """) AS initial_handoff
        FROM public.video_inventory inventory LEFT JOIN public.videos video USING(video_id)
        JOIN public.channels channel ON channel.channel_id=inventory.channel_id
        JOIN """ + "(" + CHANNEL_CONTROL_FACTS_SQL + ")" + """ control ON control.channel_id=inventory.channel_id
        LEFT JOIN public.open_video_availability unavailable ON unavailable.video_id=inventory.video_id
        WHERE inventory.video_type='long' AND control.owner_permission='accepted' AND NOT control.is_hidden
          AND inventory.video_id=ANY(%s::text[])
    ), projected AS (
        SELECT sources.*,now() AS as_of,
            CASE WHEN EXISTS(SELECT 1 FROM cadence rule WHERE rule.enabled AND rule.video_type='long'
                AND rule.collection_kind IN ('counters','metadata')
                AND rule.availability_class=coalesce(sources.availability_status,'public'))
              THEN greatest(extract(epoch FROM (now()-published_at))::bigint,0) ELSE 0 END AS age_seconds,
            published_at IS NULL OR NOT isfinite(published_at) AS published_invalid,
            checked_at IS NULL OR NOT isfinite(checked_at) OR checked_at>clock_timestamp() AS stats_now,
            metadata_checked_at IS NULL OR NOT isfinite(metadata_checked_at) OR metadata_checked_at>clock_timestamp() AS metadata_now,
            availability_checked_at IS NULL OR NOT isfinite(availability_checked_at)
                OR availability_checked_at>clock_timestamp() AS availability_now,
            now()<=clock_timestamp() AS as_of_valid
        FROM sources
    )
    SELECT coalesce((SELECT jsonb_agg(to_jsonb(projected)) FROM projected),'[]'::jsonb) AS sources,
           coalesce((SELECT jsonb_agg(to_jsonb(cadence)) FROM cadence),'[]'::jsonb) AS cadence
""")
_LONG_UPSERT = ("""
    INSERT INTO public.video_collection_state AS state
        (video_id,channel_id,stats_due_at,metadata_due_at,stats_source_checked_at,metadata_source_checked_at,
         next_due_at,policy_version,hydration_needed,paused_reason,updated_at,
         availability_first_missing_at,availability_schedule_type,availability_resume_observed_at)
    SELECT input.video_id,input.channel_id,
        CASE WHEN input.first_missing_at IS NULL THEN nominal.stats_due_at END,
        CASE WHEN input.first_missing_at IS NULL THEN schedule.metadata_due_at ELSE input.episode_next_due_at END,
        input.checked_at,input.metadata_checked_at,
        CASE WHEN input.paused_reason IS NULL THEN CASE WHEN input.first_missing_at IS NOT NULL
            THEN input.episode_next_due_at ELSE least(nominal.stats_due_at,schedule.metadata_due_at) END END,
        input.policy_version,input.needs_hydration,input.paused_reason,now(),input.first_missing_at,input.schedule_type,NULL
    FROM jsonb_to_recordset(%s::jsonb) input(video_id text,channel_id text,published_at timestamptz,
        checked_at timestamptz,metadata_checked_at timestamptz,availability_checked_at timestamptz,as_of timestamptz,
        needs_hydration boolean,published_invalid boolean,availability_supported boolean,stats_now boolean,metadata_now boolean,
        availability_now boolean,stats_seconds bigint,metadata_seconds bigint,policy_version integer,paused_reason text,
        first_missing_at timestamptz,schedule_type text,episode_next_due_at timestamptz)
    CROSS JOIN LATERAL (SELECT
        CASE WHEN input.needs_hydration OR input.published_invalid THEN input.as_of
             WHEN NOT input.availability_supported THEN NULL WHEN input.stats_now THEN input.as_of
             ELSE input.checked_at+make_interval(secs=>input.stats_seconds::double precision) END AS stats_due_at,
        CASE WHEN input.needs_hydration OR input.published_invalid THEN input.as_of
             WHEN NOT input.availability_supported THEN NULL WHEN input.metadata_now THEN input.as_of
             ELSE input.metadata_checked_at+make_interval(secs=>input.metadata_seconds::double precision) END AS metadata_due_at
    ) nominal
    CROSS JOIN LATERAL (SELECT CASE WHEN input.needs_hydration THEN input.as_of
        ELSE least(coalesce(nominal.metadata_due_at,input.as_of),
            CASE WHEN input.availability_now THEN input.as_of ELSE input.availability_checked_at+make_interval(secs=>%s::integer) END)
        END AS metadata_due_at) schedule
    ON CONFLICT (video_id) DO UPDATE SET channel_id=excluded.channel_id,stats_due_at=excluded.stats_due_at,
        metadata_due_at=excluded.metadata_due_at,stats_source_checked_at=excluded.stats_source_checked_at,
        metadata_source_checked_at=excluded.metadata_source_checked_at,
        next_due_at=CASE WHEN state.consecutive_failures>0
            AND NOT (state.paused_reason IN ('channel_hidden','channel_no_long_playlist','channel_inventory_not_ready',
                'channel_inventory_manual_required','channel_inventory_error') AND state.next_due_at IS NULL)
            THEN state.next_due_at ELSE excluded.next_due_at END,
        policy_version=excluded.policy_version,hydration_needed=excluded.hydration_needed,
        availability_first_missing_at=excluded.availability_first_missing_at,
        availability_schedule_type=excluded.availability_schedule_type,
        paused_reason=excluded.paused_reason,updated_at=now()
    WHERE state.lease_token IS NULL AND state.availability_first_missing_at IS NULL AND state.availability_resume_observed_at IS NULL
        AND state.paused_reason IS DISTINCT FROM 'availability_retry_expired'
        AND (state.paused_reason IS NULL
        OR (state.paused_reason IN ('channel_hidden','channel_no_long_playlist','channel_inventory_not_ready',
            'channel_inventory_manual_required','channel_inventory_error') AND (EXISTS(
                SELECT 1 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ control WHERE control.channel_id=state.channel_id
                    AND NOT control.is_hidden AND control.collection_mode='active')
                OR """ + VIDEO_HANDOFF_STATE_SQL + """)))
    RETURNING video_id
""")
_TYPED_INSERT = ("""
    INSERT INTO public.video_collection_state
        (video_id,channel_id,video_type,hydration_needed,policy_version,typed_source_published_at,typed_source_published_acquired_at,
         next_due_at,typed_collection_status,updated_at)
    SELECT candidate.video_id,candidate.channel_id,candidate.video_type,
        NOT EXISTS(SELECT 1 FROM public.videos current_video WHERE current_video.video_id=candidate.video_id)
        OR candidate.last_completed_scan_id IS NULL,
        1,candidate.source_published_at,candidate.source_published_acquired_at,now(),'pending',now()
    FROM (SELECT DISTINCT id FROM unnest(%s::text[]) ids(id)) input
    CROSS JOIN LATERAL (SELECT c.* FROM """ + "(" + TYPED_CANDIDATE_INPUT_ID_SQL + ")" + """ c WHERE c.video_id=input.id
    UNION ALL """ + _TYPED_BOOTSTRAP + """ OFFSET 0) candidate
    WHERE NOT EXISTS(SELECT 1 FROM public.video_collection_state state WHERE state.video_id=candidate.video_id)
    ORDER BY candidate.video_id ON CONFLICT(video_id) DO NOTHING RETURNING video_id
""")


def ensure_typed_collection_state_in_transaction(transaction, video_ids):
    """Only create missing typed queues; never rewrite a Long deadline."""
    if (not isinstance(video_ids,(list,tuple)) or len(video_ids)>1000
            or any(type(identity) is not str or not identity or identity!=identity.strip() for identity in video_ids)):
        raise ValueError('typed_collection_ids_invalid')
    ids=list(dict.fromkeys(video_ids))
    if not ids:return 0
    with transaction.borrow():
        return len(transaction.returning_all(_TYPED_INSERT,(ids,),max_rows=len(ids)))


def _pause(source):
    if not source["is_unavailable"] and not source["needs_hydration"] and availability_class(source["availability_status"]) is None:
        return "availability_unsupported"
    mode = source["collection_mode"]
    if mode == "active" or source.get("initial_handoff") is True: return None
    if mode == "hidden": return "channel_hidden"
    if mode == "metadata_only": return "channel_no_long_playlist"
    if source["owner_permission"] == "manual_required": return "channel_inventory_manual_required"
    return "channel_inventory_not_ready"


def ensure_video_collection_state_in_transaction(transaction, video_ids):
    """Return affected rows inside caller ownership; caller owns final scopes.

    This is the provisional initializer, not the public addressed wrapper. Its
    caller must perform the existing final channel fence before committing.
    """
    with transaction.borrow():
        arguments = transaction.fetch_one(_INPUT, (video_ids,))
        count, ids = arguments["count"], arguments["ids"]
        if count is None or count > 1000:
            # Retained non-token exception messages are sanitized by the driver.
            raise DatabaseServerError("database_operation_failed", code="P0001")
        if count == 0: return 0
        captured = transaction.fetch_one(_LONG_INPUT, (cadence_parameter(transaction), ids))
        sources = captured["sources"]
        prepared = []
        if sources:
            try:
                # The injected process parameter always supplies 30 rules;
                # native fact shape validation remains strict.
                snapshot = _decode_cadence_rows(captured["cadence"]) if captured["cadence"] else None
                for source in sources:
                    source = channel_mode_row(source)
                    if not source["needs_hydration"] and source["as_of_valid"] is not True:
                        raise DatabaseServerError("hot_video_refresh_as_of_invalid", code="P0001")
                    stats = select_long_policy_at_age(snapshot,"counters",source["availability_status"],source["age_seconds"]) if snapshot else None
                    metadata = select_long_policy_at_age(snapshot,"metadata",source["availability_status"],source["age_seconds"]) if snapshot else None
                    episode = (missing_schedule(require_cadence_snapshot(transaction), video_type='long',
                        first_missing_at=source.get('unavailable_first_missing_at'), observed_at=source['as_of'])
                        if source['is_unavailable'] else dict(first_missing_at=None, schedule_type=None,
                                                             next_due_at=None, stopped=False))
                    prepared.append({**source,"stats_seconds": stats.cadence_seconds if stats else None,
                        "metadata_seconds": metadata.cadence_seconds if metadata else None,
                        "policy_version":max(stats.policy_version if stats else 1,metadata.policy_version if metadata else 1),
                        "availability_supported":availability_class(source["availability_status"]) is not None,
                        # Channel admission is checked at claim time. A rebuilt
                        # episode must keep its calendar while its channel is
                        # held, otherwise initialization cannot resume it later.
                        "paused_reason":('availability_retry_expired' if episode['stopped']
                            else None if episode['first_missing_at'] is not None else _pause(source)),
                        "first_missing_at":episode['first_missing_at'],"schedule_type":episode['schedule_type'],
                        "episode_next_due_at":episode['next_due_at']})
            except PolicyError as error:
                raise DatabaseServerError(error.code, code=error.sqlstate) from None
        changed = len(transaction.returning_all(_LONG_UPSERT,(Jsonb(prepared),MAX_INTERVAL_SECONDS),max_rows=max(1,count)))
        changed += len(transaction.returning_all(_TYPED_INSERT,(ids,),max_rows=count))
        return changed
