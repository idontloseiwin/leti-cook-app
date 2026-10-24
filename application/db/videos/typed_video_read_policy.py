"""Closed addressed projections replacing the retained typed candidate view.

Only fixed import-time templates are exported. Address equality/ANY stays native;
each eligible relation has an inner OFFSET 0 fence. Existing caller fences,
materialization, clocks, bounds and lock positions remain at their original sites.
"""
from typed_video_identity_policy import (
    TYPED_IDENTITY_COMMON, TYPED_IDENTITY_BRANCHES, TYPED_MANUAL_SOURCE,
    TYPED_IDENTITY_FACT_NAMES, typed_identity_from_native_facts,
)
from ..channel.channel_read_policy import CHANNEL_CONTROL_SQL
from .video_initial_policy import VIDEO_HANDOFF_TYPED_SQL, VIDEO_INITIAL_CHUNK_TYPED_SQL

_IDENTITY_FACT_SQL = {
    "typed_type": "i.video_type=ANY(ARRAY['short'::text,'stream'::text])",
    "channel_runtime": "(coalesce((SELECT policy.typed_runtime_eligible FROM (" + CHANNEL_CONTROL_SQL +
        ") policy WHERE policy.channel_id=channel.channel_id),false) OR " + VIDEO_HANDOFF_TYPED_SQL + ")",
    "playlist_enabled": "(d.runtime_enabled OR " + VIDEO_HANDOFF_TYPED_SQL + " OR " + VIDEO_INITIAL_CHUNK_TYPED_SQL + ")",
    "inventory_ready": "s.inventory_status='completed' OR (s.inventory_status='running' AND s.last_completed_scan_id IS NOT NULL)",
    "scan_complete": "scan.status='completed' AND scan.coverage='complete'",
    "foreign_present": "EXISTS(SELECT 1 FROM public.video_inventory_memberships x WHERE x.video_id=i.video_id AND x.membership_status='present' AND x.channel_id<>i.channel_id)",
    "manual_valid": "c.decision_type=i.video_type AND c.channel_id=i.channel_id AND c.blocked_reason IS NULL",
    "automatic_valid": "EXISTS(SELECT 1 FROM public.video_type_automatic_candidates automatic WHERE automatic.video_id=i.video_id AND automatic.channel_id=i.channel_id AND automatic.automatic_type=i.video_type)",
    "exact_present": "m.video_id IS NOT NULL",
    "conflicting_present_type": "EXISTS(SELECT 1 FROM public.video_inventory_memberships x WHERE x.video_id=i.video_id AND x.membership_status='present' AND x.video_type<>i.video_type)",
}
assert set(_IDENTITY_FACT_SQL) == TYPED_IDENTITY_FACT_NAMES


def _terms(terms):
    assert all(name in TYPED_IDENTITY_FACT_NAMES and type(required) is bool for name, required in terms)
    return " AND ".join("facts." + name + (" IS TRUE" if required else " IS FALSE")
                        for name, required in terms)


_IDENTITY_ADMISSION_SQL = "(" + _terms(TYPED_IDENTITY_COMMON) + ") AND (" + " OR ".join(
    "(" + _terms(terms) + ")" for terms in TYPED_IDENTITY_BRANCHES) + ")"
_MANUAL_SOURCE_SQL = _terms(TYPED_MANUAL_SOURCE)
_NATIVE_FACTS = """SELECT i.video_id,i.channel_id,i.video_type,
 m.source_published_at AS member_published_at,i.published_at AS inventory_published_at,
 m.last_present_at AS member_published_acquired_at,i.published_acquired_at AS inventory_published_acquired_at,
 coalesce(m.first_seen_at,i.discovered_at) AS first_seen_at,s.last_completed_scan_id,
""" + ",\n".join("(" + expression + ") AS " + name for name, expression in _IDENTITY_FACT_SQL.items()) + """
 FROM public.video_inventory i
 LEFT JOIN public.video_inventory_memberships m ON m.video_id=i.video_id
   AND m.channel_id=i.channel_id AND m.video_type=i.video_type AND m.membership_status='present'
 LEFT JOIN public.video_type_review_cases c ON c.video_id=i.video_id
 JOIN public.channel_playlist_state d ON d.channel_id=i.channel_id AND d.video_type=i.video_type
 JOIN public.channel_playlist_state s ON s.channel_id=i.channel_id AND s.video_type=i.video_type
 JOIN public.channel_video_inventory_scans scan ON scan.scan_id=s.last_completed_scan_id
   AND scan.channel_id=s.channel_id AND scan.video_type=s.video_type
 JOIN public.channels channel ON channel.channel_id=i.channel_id
"""
# These are the entire allowed address vocabulary. No runtime SQL/identifier is accepted.
_ADDRESSES = {
    'input_id': 'i.video_id=input.id',
    'input_video_id': 'i.video_id=input.video_id',
    'inventory_ids': 'i.video_id=ANY(%(inventory_ids)s::text[])',
    'inventory_row': 'i.video_id=inventory.video_id',
    'state_row': 'i.video_id=state.video_id',
}


def _candidate(address):
    return """SELECT facts.video_id,facts.channel_id,facts.video_type,
 CASE WHEN """ + _MANUAL_SOURCE_SQL + """ THEN coalesce(facts.member_published_at,facts.inventory_published_at)
 ELSE facts.member_published_at END AS source_published_at,
 CASE WHEN facts.member_published_at IS NOT NULL THEN facts.member_published_acquired_at
 WHEN """ + _MANUAL_SOURCE_SQL + """ AND facts.inventory_published_at IS NOT NULL
 THEN facts.inventory_published_acquired_at END AS source_published_acquired_at,
 facts.first_seen_at,facts.last_completed_scan_id FROM (""" + _NATIVE_FACTS + " WHERE " + _ADDRESSES[address] + """
 ) facts WHERE """ + _IDENTITY_ADMISSION_SQL + " OFFSET 0"


TYPED_CANDIDATE_INPUT_ID_SQL = _candidate('input_id')
TYPED_CANDIDATE_INPUT_VIDEO_ID_SQL = _candidate('input_video_id')
TYPED_CANDIDATE_INVENTORY_IDS_SQL = _candidate('inventory_ids')
TYPED_CANDIDATE_INVENTORY_ROW_SQL = _candidate('inventory_row')
TYPED_CANDIDATE_STATE_ROW_SQL = _candidate('state_row')
_FACT_OBJECT = "jsonb_build_object(" + ",".join("'" + name + "',facts." + name for name in _IDENTITY_FACT_SQL) + ")"
# A scalar subquery preserves one result row with NULL facts for absent identity.
# All original text casts remain in their original parameter order and statement.
_IDENTITY_LOOKUP = "SELECT (SELECT " + _FACT_OBJECT + " FROM (" + _NATIVE_FACTS + """
 WHERE i.video_id=input.video_id) facts WHERE facts.channel_id=input.channel_id
 AND facts.video_type=input.video_type) AS identity_facts FROM ("""
TYPED_IDENTITY_POSITIONAL_SQL = _IDENTITY_LOOKUP + "SELECT %s::text AS video_id,%s::text AS channel_id,%s::text AS video_type) input"
TYPED_IDENTITY_NAMED_SQL = _IDENTITY_LOOKUP + "SELECT %(video_id)s::text AS video_id,%(channel_id)s::text AS channel_id,%(video_type)s::text AS video_type) input"


def typed_identity_row(row):
    from ..base.postgres import DatabaseError
    if not isinstance(row, dict) or set(row) != {'identity_facts'}:
        raise DatabaseError('database_result_shape_invalid')
    facts = row['identity_facts']
    if facts is None:
        return {'eligible': False}
    try:
        return {'eligible': typed_identity_from_native_facts(facts)}
    except (KeyError, TypeError, ValueError):
        raise DatabaseError('database_result_shape_invalid') from None
