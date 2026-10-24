"""Finite API-source expiry before startup cleanup, under a two-second budget.

This is a fixed existence observation, not a whole-database correctness proof.
Each branch returns at most one row. Existing clock indexes help several
branches; LIMIT 1 does not bound the number of rows PostgreSQL may examine.
Unknown, infinite and future source clocks belong to normal row-local cleanup,
not to the long-downtime signal. Local user-work tombstones are excluded.
"""
from types import MappingProxyType
from dataclasses import dataclass

from ..retention.current_source_retention import _TARGETS as CURRENT_SOURCES
from ..retention.inventory_chain_retention import SOURCE_FREE_JOB_SQL


def _finite_expiry(expression, *, inclusive=False, expires=False):
    # Both bounds are indexable; together they exclude NULL and both infinities.
    cutoff = "(clock.cutoff + interval '696 hours')" if expires else 'clock.cutoff'
    boundary = f'({expression}) < {cutoff}'
    if inclusive:
        # Entry is strictly beyond the grace; completion keeps the category's
        # established inclusive expiry at its frozen cutoff.
        boundary += f' OR (NOT clock.strict_entry AND ({expression}) = {cutoff})'
    return f"(({expression}) > '-infinity'::timestamptz AND ({boundary}))"


@dataclass(frozen=True)
class SourceClock:
    table: str
    expression: str
    payload: str
    inclusive: bool = False
    expires: bool = False

    def startup_query(self):
        return (f'SELECT 1 FROM public.{self.table} source WHERE ({self.payload}) AND '
                f'{_finite_expiry(self.expression, inclusive=self.inclusive, expires=self.expires)} LIMIT 1')


def _source(table, expression, payload='true', *, inclusive=False, expires=False):
    return SourceClock(table, expression, payload, inclusive, expires)


_sources = {
    name: _source(spec.table, spec.key, spec.payload)
    for name, spec in CURRENT_SOURCES.items() if spec.clock is not None
}
# Profile's indexed key deliberately maps invalid metadata status to -infinity.
# An otherwise finite old source is still old when its status is malformed.
_sources['channel_profile_invalid_status'] = _source(
    'channels', 'channel_metadata_checked_at',
    "channel_metadata_status IS DISTINCT FROM 'ok' AND (" + CURRENT_SOURCES['channel_profile'].payload + ')')

_sources['channel_inventory_estimate'] = """SELECT 1 FROM public.channels source
CROSS JOIN LATERAL unnest(CASE WHEN initial_inventory_estimate->>'scope'='all'
    THEN ARRAY['long','short','stream'] ELSE ARRAY['long'] END) kinds(kind)
CROSS JOIN LATERAL (SELECT CASE WHEN initial_inventory_estimate ? 'checked_at_by_type'
    THEN initial_inventory_estimate->'checked_at_by_type'->>kind
    ELSE initial_inventory_estimate->>'checked_at' END AS value) raw
CROSS JOIN LATERAL (SELECT CASE WHEN pg_input_is_valid(raw.value,'timestamptz')
    THEN raw.value::timestamptz END AS at) parsed
WHERE """ + _finite_expiry('parsed.at') + ' LIMIT 1'

for name, table, field in (
    ('video_snapshots', 'video_snapshots', 'recorded_at'),
    ('channel_snapshots', 'channel_snapshots', 'recorded_at'),
    ('typed_video_collection_receipts', 'typed_video_collection_receipts', 'recorded_at'),
    ('availability_events', 'video_availability_events', 'observed_at'),
):
    _sources[name] = _source(table, f"coalesce({field},'-infinity'::timestamptz)")

for name, field, payload in (
    ('first', 'first_seen_at', 'first_status IS NOT NULL OR source_first_seen IS NOT NULL'),
    ('latest', 'last_seen_at', 'latest_status IS NOT NULL OR source_latest_seen IS NOT NULL'),
    ('restored', 'restored_at', 'restored_status IS NOT NULL OR source_restored IS NOT NULL'),
):
    _sources['availability_incidents_' + name] = _source('video_availability_incidents', field, payload)
_sources['review_evidence'] = _source('video_type_review_cases', 'evidence_expires_at',
    'observed_canonical_type IS NOT NULL OR cardinality(observed_types)>0 '
    'OR cardinality(observed_channels)>0 OR blocked_reason IS NOT NULL', inclusive=True, expires=True)

# Publication dates are values, not acquisition clocks. Each retained copy owns
# its observation time; removing or refreshing another row cannot renew it.
_sources['inventory_publication'] = _source('video_inventory', 'published_acquired_at', 'published_at IS NOT NULL')
for name, field, predicate in (
    ('first', 'first_seen_at', 'true'),
    ('seen', 'last_seen_at', 'true'),
    ('present', 'last_present_at', 'true'),
    ('absent', 'absent_since', "membership_status='absent'"),
):
    _sources['memberships_' + name] = _source('video_inventory_memberships', field, predicate)
_sources['queue_publication'] = _source('video_collection_state', 'typed_source_published_acquired_at',
    'typed_source_published_at IS NOT NULL')
_sources['queue_stream_end'] = _source('video_collection_state', 'typed_source_stream_end_acquired_at',
    'typed_stream_actual_end_at IS NOT NULL')

_sources['playlist_source'] = _source('channel_playlist_state', 'source_checked_at')
_sources['playlist_legacy_completion'] = _source('channel_playlist_state', 'inventory_completed_at',
    "last_completed_scan_id IS NULL AND inventory_state_origin='legacy_long_backfill' "
    "AND inventory_status='completed'")
# Failed scans retain local identity / clocks after their API copies are cleared.
# Do not turn those cleared rows into a permanent catch-up condition.
_scan_payload = ('resume_page_token IS NOT NULL OR items_seen IS NOT NULL '
                 'OR new_memberships_found IS NOT NULL OR token_history_verified '
                 'OR EXISTS(SELECT 1 FROM public.channel_playlist_state state '
                 'WHERE state.active_scan_id=source.scan_id OR state.last_completed_scan_id=source.scan_id LIMIT 1)')
for field in ('started_at', 'scan_boundary_at', 'source_checked_at', 'completed_at'):
    predicate = '(' + _scan_payload + ')'
    if field == 'completed_at':
        predicate += " AND status='completed'"
    _sources['inventory_scans_' + field] = _source('channel_video_inventory_scans', field, predicate)
_sources['inventory_receipts'] = _source('typed_inventory_receipts', 'source_checked_at',
    "NOT(result ? 'source_expired') OR claims IS NOT NULL OR next_page_token_hash IS NOT NULL")

_job_payload = 'NOT coalesce((' + SOURCE_FREE_JOB_SQL + '),false)'
_sources['operator_jobs_and_targets'] = _source('operator_jobs', 'requested_at', _job_payload)
# A fresh job may retain progress based on an older linked scan or receipt.
# Include the canonical clock even if the scan copies were already scrubbed.
# A missing/deleted source has unknown age and is not a finite-expiry signal.
_job_scans = """SELECT 1 FROM public.operator_jobs job
CROSS JOIN LATERAL (SELECT job.inventory_scan_id AS id
 UNION ALL SELECT CASE WHEN pg_input_is_valid(job.summary_json->'inventory_ack'->>'scan_id','uuid')
    THEN (job.summary_json->'inventory_ack'->>'scan_id')::uuid END
 UNION ALL SELECT CASE WHEN pg_input_is_valid(job.summary_json->'full_inventory_sources'->kind->>'scan_id','uuid')
    THEN (job.summary_json->'full_inventory_sources'->kind->>'scan_id')::uuid END
    FROM unnest(ARRAY['long','short','stream']) kinds(kind) WHERE job.initial_scope_version=1) refs
JOIN public.channel_video_inventory_scans scan ON scan.scan_id=refs.id
WHERE """
_sources['operator_jobs_linked_scans'] = _job_scans + '(' + ' OR '.join(
    _finite_expiry('scan.' + field) for field in ('started_at', 'scan_boundary_at', 'source_checked_at', 'completed_at')) + ') LIMIT 1'
_sources['operator_jobs_ack_receipts'] = """SELECT 1 FROM public.operator_jobs job
JOIN public.typed_inventory_receipts receipt ON receipt.transition_id=
    CASE WHEN pg_input_is_valid(job.summary_json->'inventory_ack'->>'transition_id','uuid')
    THEN (job.summary_json->'inventory_ack'->>'transition_id')::uuid END
    AND receipt.receipt_scope=CASE WHEN job.summary_json->'inventory_ack'->>'outcome'='quota'
    THEN 'quota_defer' ELSE 'scan_transition' END
WHERE job.summary_json->'inventory_ack'->>'outcome' IN('page','playlist_not_found','error','quota')
AND """ + _finite_expiry('receipt.source_checked_at') + ' LIMIT 1'
_sources['operator_chain_linked_scans'] = """SELECT 1 FROM public.operator_job_inventory_chains child
CROSS JOIN LATERAL (SELECT child.scan_id AS id
 UNION ALL SELECT CASE WHEN pg_input_is_valid(child.last_ack->>'scan_id','uuid')
    THEN (child.last_ack->>'scan_id')::uuid END) refs
JOIN public.channel_video_inventory_scans scan ON scan.scan_id=refs.id
 AND scan.channel_id=child.channel_id AND scan.video_type=child.video_type
WHERE """ + '(' + ' OR '.join(
    _finite_expiry('scan.' + field) for field in ('started_at', 'scan_boundary_at', 'source_checked_at', 'completed_at')) + ') LIMIT 1'
_sources['operator_chain_ack_receipts'] = """SELECT 1 FROM public.operator_job_inventory_chains child
JOIN public.typed_inventory_receipts receipt ON receipt.transition_id=
    CASE WHEN pg_input_is_valid(child.last_ack->>'transition_id','uuid')
    THEN (child.last_ack->>'transition_id')::uuid END
    AND receipt.receipt_scope=CASE WHEN child.last_ack->>'outcome'='quota'
    THEN 'quota_defer' ELSE 'scan_transition' END
WHERE child.last_ack->>'outcome' IN('page','playlist_not_found','error','quota')
AND """ + _finite_expiry('receipt.source_checked_at') + ' LIMIT 1'
for name, table in (('sync_runs', 'sync_runs'), ('quota_attempts', 'youtube_api_request_attempts')):
    _sources[name] = _source(table, 'started_at')
_sources['category_reference'] = _source('youtube_category_reference', 'retrieved_at', inclusive=True)

SOURCE_CLOCKS = MappingProxyType(_sources)
STARTUP_EXPIRY_SOURCES = MappingProxyType({
    name: value.startup_query() if isinstance(value, SourceClock) else value
    for name, value in SOURCE_CLOCKS.items()
})
del _sources
