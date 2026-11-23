"""Read the preparation evidence required for discovery activation.

The host must explicitly enable provider admission. Migration or elapsed time alone cannot activate discovery."""
from dataclasses import dataclass, fields

from discovery_policy import DiscoverySettings
from hot_storage import RETENTION_HOURS
from .discovery import fail, operation
from .channel.channel_read_policy import CHANNEL_CONTROL_SQL
from .videos.collection_policy import require_cadence_snapshot


@dataclass(frozen=True)
class PreparationContract:
    full_admission: bool
    signal_admission: bool
    continuation_admission: bool
    count_head_admission: bool
    workers_drained: bool


def _host(value):
    if type(value) is not PreparationContract or any(getattr(value, field.name) is not True for field in fields(value)):
        fail('discovery_prepared_host_required')


def _formats(video_types):
    if (not isinstance(video_types, (list, tuple)) or not video_types
            or len(set(video_types)) != len(video_types) or set(video_types) - {'long', 'short', 'stream'}
            or not {'long', 'short'} <= set(video_types)):
        fail('discovery_formats_invalid')
    return list(video_types)


# Cutover alone allows a configured grace after the full-coverage period.
# The original START and current epoch stay authoritative. The grace neither
# extends retention nor applies to a not-found response or an active owner.
_FACTS = """WITH channels AS MATERIALIZED (""" + CHANNEL_CONTROL_SQL + """),
 expected AS (SELECT c.*,k.video_type FROM channels c
 CROSS JOIN unnest(%(types)s::text[]) k(video_type) WHERE c.typed_runtime_eligible),
 source_evidence AS (SELECT c.channel_id,c.video_type,c.control_epoch,c.collection_mode,p.playlist_id,p.runtime_enabled,p.status,
 p.active_scan_id,p.lease_token,p.last_error_code,p.next_due_at,p.last_result,
 full_proof.full_started_at,full_proof.any_full_started_at,
 greatest(missing_proof.missing_checked_at,head_proof.source_checked_at) AS absence_at,
 (%(full_seconds)s::jsonb->>c.video_type)::bigint AS full_period_seconds,
 (%(missing_seconds)s::jsonb->>c.video_type)::bigint AS missing_period_seconds
 FROM expected c LEFT JOIN public.channel_playlist_state p USING(channel_id,video_type)
 LEFT JOIN LATERAL (SELECT max(s.started_at) AS any_full_started_at,
   max(s.started_at) FILTER(WHERE s.discovery_channel_epoch=c.control_epoch) AS full_started_at
  FROM public.channel_video_inventory_scans s
  WHERE s.channel_id=c.channel_id AND s.video_type=c.video_type
    AND s.status='completed' AND s.coverage='complete'
    AND s.scan_kind IN('initial_inventory','full_reconciliation')
    AND isfinite(s.started_at) AND isfinite(s.completed_at)
    AND s.completed_at BETWEEN s.started_at AND statement_timestamp()) full_proof ON true
 LEFT JOIN LATERAL (SELECT max(r.source_checked_at) AS missing_checked_at
  FROM public.typed_inventory_receipts r
  JOIN public.channel_video_inventory_scans s USING(scan_id)
  WHERE r.receipt_scope='scan_transition' AND r.transition_kind='failure'
    AND r.transition_id=p.last_failure_attempt_id AND s.channel_id=c.channel_id AND s.video_type=c.video_type
    AND s.status='failed' AND s.coverage='partial' AND s.token_history_verified
    AND s.pages_completed=0 AND s.items_seen=0 AND s.new_memberships_found=0 AND s.resume_page_token IS NULL
    AND s.last_transition_id=r.transition_id AND s.last_transition_kind='failure'
    AND r.result->>'error_code'='typed_playlist_not_found'
    AND CASE WHEN r.result ? 'first_page_absence_epoch'
        THEN r.result->'first_page_absence_epoch'=to_jsonb(c.control_epoch)
        ELSE s.discovery_channel_epoch=c.control_epoch END
    AND r.result->>'outcome'='playlist_not_found'
    AND p.last_result='playlist_not_found' AND NOT(r.result ? 'source_expired')
    AND (p.head_last_attempt IS NULL OR p.head_last_attempt->>'transition_id'=r.transition_id::text)
    AND isfinite(r.source_checked_at)
    AND r.source_checked_at<=statement_timestamp()) missing_proof ON true
 LEFT JOIN LATERAL (SELECT r.source_checked_at FROM public.typed_inventory_receipts r
  WHERE r.receipt_scope='addressed_membership' AND r.transition_id=
    CASE WHEN pg_input_is_valid(p.head_last_attempt->>'transition_id','uuid')
      THEN (p.head_last_attempt->>'transition_id')::uuid END
    AND p.head_pending_request IS NULL
    AND p.head_last_attempt->>'outcome'='playlist_not_found'
    AND p.head_last_attempt->'epoch'=to_jsonb(c.control_epoch)
    AND r.result->>'transition_id'=r.transition_id::text
    AND r.result->>'lease_token'=p.head_last_attempt->>'lease_token'
    AND r.result->>'observation_kind'='head_page' AND r.result->>'outcome'='playlist_not_found'
    AND r.result->'provider_http_status'='404'::jsonb AND r.result->>'error_code'='typed_playlist_not_found'
    AND r.result->>'channel_id'=c.channel_id AND r.result->>'video_type'=c.video_type
    AND r.result->>'playlist_id'=p.playlist_id AND r.result->'control_epoch'=to_jsonb(c.control_epoch)
    AND r.source_checked_at=CASE WHEN pg_input_is_valid(p.head_last_attempt->>'source_checked_at','timestamptz')
      THEN (p.head_last_attempt->>'source_checked_at')::timestamptz END
    AND NOT(r.result ? 'source_expired') AND isfinite(r.source_checked_at)
    AND r.source_checked_at<=statement_timestamp()
    AND (p.source_checked_at IS NULL OR r.source_checked_at>p.source_checked_at)) head_proof ON true),
 evidence AS (SELECT s.*,
   CASE WHEN s.absence_at IS NOT NULL AND NOT EXISTS(
     SELECT 1 FROM public.typed_inventory_receipts positive
     WHERE positive.receipt_scope='addressed_membership' AND positive.result->>'channel_id'=s.channel_id
       AND positive.result->>'video_type'=s.video_type AND positive.result->>'playlist_id'=s.playlist_id
       AND positive.result->'control_epoch'=to_jsonb(s.control_epoch)
       AND positive.source_checked_at BETWEEN s.absence_at AND statement_timestamp()
       AND NOT(positive.result ? 'source_expired') AND positive.result->'video_ids'->>0 IS NOT NULL
   ) THEN s.absence_at END AS missing_checked_at FROM source_evidence s),
 facts AS (SELECT evidence.*,
 coalesce(full_started_at>=statement_timestamp()-make_interval(secs=>full_period_seconds+%(grace)s::bigint)
   AND full_started_at>statement_timestamp()-make_interval(hours=>%(hours)s),false) AS full_ready,
 coalesce(full_started_at>=statement_timestamp()-make_interval(secs=>full_period_seconds),false) AS within_full_period,
 coalesce(missing_checked_at>statement_timestamp()-make_interval(secs=>missing_period_seconds)
   AND missing_checked_at>statement_timestamp()-make_interval(hours=>%(hours)s),false) AS missing_ready
 FROM evidence),
 classified AS (SELECT facts.*,CASE
  WHEN NOT coalesce(runtime_enabled,false) THEN 'source_not_prepared'
  WHEN active_scan_id IS NOT NULL OR lease_token IS NOT NULL OR status='leased' THEN 'source_active'
  WHEN collection_mode='initial_inventory' THEN 'initial_inventory'
  WHEN status IS DISTINCT FROM 'ready' THEN 'source_not_ready'
  WHEN full_ready OR missing_ready THEN NULL
  WHEN missing_checked_at IS NOT NULL THEN 'missing_evidence_stale'
  WHEN full_started_at<=statement_timestamp()-make_interval(hours=>%(hours)s) THEN 'full_evidence_expired'
  WHEN full_started_at IS NOT NULL THEN 'full_coverage_stale'
  WHEN any_full_started_at IS NOT NULL THEN 'full_epoch_mismatch'
  WHEN last_result='playlist_not_found' THEN 'missing_evidence_unconfirmed'
  ELSE 'full_evidence_missing' END AS reason FROM facts) """

_SUMMARY = _FACTS + """SELECT count(*)::integer AS expected_sources,
 count(*) FILTER(WHERE reason IS NULL)::integer AS ready_sources,
 count(*) FILTER(WHERE full_ready)::integer AS full_sources,
 count(*) FILTER(WHERE missing_ready)::integer AS absent_sources,
 count(*) FILTER(WHERE NOT coalesce(runtime_enabled,false))::integer AS unprepared_sources,
 count(*) FILTER(WHERE active_scan_id IS NOT NULL OR lease_token IS NOT NULL OR status='leased')::integer AS active_sources,
 count(*) FILTER(WHERE collection_mode='initial_inventory')::integer AS initial_sources,
 count(*) FILTER(WHERE full_ready AND NOT within_full_period)::integer AS grace_sources,
 count(*) FILTER(WHERE reason IS NOT NULL)::integer AS blocker_count,
 (SELECT count(*)::integer FROM channels WHERE NOT typed_runtime_eligible) AS excluded_channels,
 coalesce((SELECT jsonb_agg(to_jsonb(blocker) ORDER BY channel_id,video_type) FROM
   (SELECT channel_id,video_type,reason,last_error_code,next_due_at,active_scan_id,
     full_started_at,missing_checked_at FROM classified WHERE reason IS NOT NULL
     ORDER BY channel_id,video_type LIMIT %(blocker_limit)s) blocker),'[]'::jsonb) AS blockers
 FROM classified"""


def _settings(value):
    if type(value) is not DiscoverySettings:
        fail('discovery_settings_required')
    return value


def _parameters(tx, types, settings):
    import json
    schedules = require_cadence_snapshot(tx).inventory_by_type
    return dict(types=types, hours=RETENTION_HOURS,grace=settings.activation_grace_seconds,
        full_seconds=json.dumps({kind:getattr(schedules, kind).reconciliation_seconds for kind in types}),
        missing_seconds=json.dumps({kind:getattr(schedules, kind).missing_playlist_seconds for kind in types}))


def _readiness(tx, types, settings, blocker_limit):
    result = tx.fetch_one(_SUMMARY, dict(_parameters(tx, types, settings),blocker_limit=blocker_limit))
    result['ready'] = result['ready_sources'] == result['expected_sources']
    result['blockers_truncated'] = result['blocker_count'] > len(result['blockers'])
    return result


def readiness(database, *, video_types=('long', 'short'), settings=None, blocker_limit=50):
    """Diagnostic only: does not prepare work or authorize activation."""
    types = _formats(video_types)
    settings = _settings(settings)
    if type(blocker_limit) is not int or not 1 <= blocker_limit <= 100:
        fail('discovery_blocker_limit_invalid')
    with database.transaction(operation('discovery_activation_readiness')) as tx:
        return _readiness(tx, types, settings, blocker_limit)


def prepare(database, *, prepared_host=None, video_types=('long', 'short'), limit=100, settings=None):
    """Request bounded real full work while discovery remains disabled.

    The prepared host must apply the approved full/continuation admission BEFORE
    dispatching these existing source owners. Outstanding work, errors and 404s
    keep their retry schedule; repeated preparation does not reset it.
    """
    _host(prepared_host)
    types = _formats(video_types)
    settings = _settings(settings)
    if type(limit) is not int or not 1 <= limit <= 500:
        fail('discovery_preparation_limit_invalid')
    with database.transaction(operation('discovery_prepare_coverage')) as tx:
        tx.fetch_one('SELECT pg_advisory_xact_lock(1856149,49)')
        control = tx.fetch_one('SELECT enabled FROM public.discovery_runtime_control WHERE singleton FOR UPDATE')
        if not control or control['enabled']:
            fail('discovery_preparation_requires_disabled')
        parameters = _parameters(tx, types, settings)
        parameters['limit'] = limit
        rows = tx.fetch_all(_FACTS + """SELECT p.channel_id,p.video_type FROM facts f
            JOIN public.channel_playlist_state p USING(channel_id,video_type)
            WHERE f.runtime_enabled AND f.status='ready' AND f.active_scan_id IS NULL AND f.lease_token IS NULL
              AND f.collection_mode<>'initial_inventory' AND NOT(f.full_ready OR f.missing_ready)
              AND p.full_reconcile_required_token IS NOT DISTINCT FROM p.full_reconcile_served_token
            ORDER BY p.channel_id,p.video_type FOR UPDATE OF p SKIP LOCKED LIMIT %(limit)s""", parameters, max_rows=500)
        for row in rows:
            tx.execute("""UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),
                next_due_at=CASE WHEN last_error_code IS NOT NULL OR last_result='playlist_not_found'
                  THEN next_due_at ELSE least(next_due_at,clock_timestamp()) END
                WHERE channel_id=%s AND video_type=%s""", (row['channel_id'],row['video_type']))
        return {'requested':len(rows), 'enabled':False}


def activate(database, *, prepared_host=None, video_types=('long', 'short'), settings=None):
    """Cross the cutover only after an actual fresh source readiness check."""
    _host(prepared_host)
    types = _formats(video_types)
    settings = _settings(settings)
    with database.transaction(operation('discovery_activate')) as tx:
        tx.fetch_one('SELECT pg_advisory_xact_lock(1856149,49)')
        current = tx.fetch_one('SELECT enabled,first_activated_at FROM public.discovery_runtime_control WHERE singleton FOR UPDATE')
        if current and current['enabled']:
            return current['first_activated_at']
        # Hold current channel epochs through the proof and control transition.
        # The caller has already drained all dispatchers, as required above.
        tx.fetch_one('SELECT count(*) FROM (SELECT channel_id FROM public.channel_collection_state ORDER BY channel_id FOR SHARE) locked')
        result = _readiness(tx, types, settings, 50)
        if not result['ready']:
            fail('discovery_preparation_incomplete')
        row = tx.returning_one('UPDATE public.discovery_runtime_control SET enabled=true,'
            'first_activated_at=coalesce(first_activated_at,clock_timestamp()) WHERE singleton RETURNING first_activated_at')
        if row is None:
            fail('discovery_control_missing')
        return row['first_activated_at']
