"""Owner diagnostics from existing receipts. A repeatable, read-only snapshot; no collector calls."""
from datetime import datetime, timedelta
from ..base.postgres import Operation
from ..base.sql_values import application_value
from ..discovery_activation import _FACTS, _parameters
from discovery_policy import DiscoverySettings
from hot_storage import HOT_DATA_WINDOW

# A scan can cross midnight or join a manual request after it started. Keep the
# first page's origin for continuations, and mark later manual joins separately.
# Results prior to instrumentation may have useful counts but no causal evidence.
_RESULTS = """
WITH batch AS MATERIALIZED (
 SELECT r.scan_id,r.transition_kind,r.receipt_scope,r.source_checked_at,r.result,
 r.result->'list_observation' AS m,
 coalesce(r.scan_id, CASE WHEN r.result->'list_observation'->>'head'='false'
 AND pg_input_is_valid(r.result->'list_observation'->>'scan','uuid') THEN (r.result->'list_observation'->>'scan')::uuid END) AS walk_id
 FROM public.typed_inventory_receipts r
 WHERE r.source_checked_at >= %(start)s AND r.source_checked_at < %(end)s
 AND NOT (r.result ? 'source_expired')
 AND (r.receipt_scope='scan_transition' OR r.result->>'observation_kind'='head_page'
      OR (r.receipt_scope='quota_defer' AND r.result ? 'list_observation'))
), origins AS MATERIALIZED (
 SELECT DISTINCT ON (r.scan_id) r.scan_id,r.result->'list_observation' AS first_m
 FROM (SELECT DISTINCT walk_id AS scan_id FROM batch WHERE walk_id IS NOT NULL) ids
 JOIN public.typed_inventory_receipts r USING(scan_id)
 WHERE r.receipt_scope='scan_transition' AND r.source_checked_at>%(cutoff)s
 AND r.result->'list_observation'->>'first'='true'
 ORDER BY r.scan_id,r.source_checked_at,r.created_at,r.transition_id
), facts AS (
 SELECT b.*, s.started_at AS scan_start, s.discovery_channel_epoch,
 CASE WHEN b.m->>'first'='false' AND b.m->>'head'='false' THEN o.first_m ELSE b.m END AS origin
 FROM batch b LEFT JOIN origins o ON o.scan_id=b.walk_id
 LEFT JOIN public.channel_video_inventory_scans s ON s.scan_id=b.walk_id
), context AS (
 SELECT f.*, prev.source_checked_at AS previous_at,
   prev.result->>'outcome'='playlist_not_found'
   AND prev.result->>'error_code'='typed_playlist_not_found'
   AND (prev.result->>'provider_http_status'='404' OR prev.receipt_scope='scan_transition') AS was_absent,
   history.started_at AS prior_full,
   hits.known AS announced, hits.seconds AS signal_seconds,
   CASE WHEN f.result->>'coverage'='complete' THEN changes.unchanged END AS unchanged
 FROM facts f
 LEFT JOIN LATERAL (
   SELECT p.source_checked_at,p.result,p.receipt_scope FROM public.typed_inventory_receipts p
   WHERE p.receipt_scope IN('addressed_membership','scan_transition')
     AND p.source_checked_at>%(cutoff)s
     AND p.transition_id=CASE WHEN pg_input_is_valid(f.origin->>'prior_head','uuid') THEN (f.origin->>'prior_head')::uuid END AND p.source_checked_at<=f.source_checked_at
     AND p.result->>'channel_id'=coalesce(f.result->>'channel_id',f.m->>'channel') AND p.result->>'video_type'=f.m->>'format'
   ORDER BY p.source_checked_at DESC LIMIT 1
 ) prev ON true
 LEFT JOIN LATERAL (
   SELECT old.started_at FROM public.channel_video_inventory_scans old
   WHERE f.origin->>'reason'='periodic_full' AND old.channel_id=coalesce(f.result->>'channel_id',f.m->>'channel')
   AND old.video_type=f.m->>'format' AND old.discovery_channel_epoch=f.discovery_channel_epoch
   AND old.scan_kind IN('full_reconciliation','initial_inventory') AND old.coverage='complete'
   AND old.status='completed' AND old.completed_at<=f.scan_start AND old.scan_id<>f.walk_id
   ORDER BY old.started_at DESC LIMIT 1
 ) history ON true
 LEFT JOIN LATERAL (
   SELECT count(signal.at) AS known, sum(extract(epoch FROM f.source_checked_at-signal.at)) AS seconds
   FROM jsonb_array_elements_text(f.result->'new_video_ids') id(value)
   LEFT JOIN LATERAL (
     SELECT min(e.received_at) AS at FROM public.websub_events e
     WHERE e.kind='entry' AND e.channel_id=f.result->>'channel_id' AND e.video_id=id.value
       AND e.received_at>%(cutoff)s AND e.received_at<=f.source_checked_at
   ) signal ON true
 ) hits ON true
 LEFT JOIN LATERAL (
   SELECT CASE WHEN count(*)= (f.result->>'pages_completed')::integer
     AND count(*) FILTER (WHERE p.result->'list_observation' ? 'added_memberships')=count(*)
     THEN bool_and((p.result->'list_observation'->>'added_memberships')::integer=0
                   AND (p.result->>'memberships_marked_absent')::integer=0
                   AND (p.result->>'conflicts')::integer=0) END AS unchanged
   FROM public.typed_inventory_receipts p WHERE f.result->>'coverage'='complete'
    AND p.scan_id=f.scan_id AND p.source_checked_at>%(cutoff)s
    AND p.receipt_scope='scan_transition' AND p.transition_kind='page'
 ) changes ON true
), labelled AS (
 SELECT *,
 CASE WHEN m IS NULL THEN 'unknown'
      WHEN origin->>'reason' IN('manual','signal','before_activation','targeted_before_activation','initial','operator','targeted') THEN origin->>'reason'
      WHEN was_absent AND origin->>'reason' IN('periodic_head','initial_candidate','periodic_full') THEN 'absent_recheck'
      WHEN origin->>'reason'='periodic_head' THEN 'scheduled_head'
      WHEN origin->>'reason'='initial_candidate' THEN 'uncovered_full'
      WHEN origin->>'reason'='periodic_full' AND prior_full<=scan_start-make_interval(secs=>(origin->>'period_seconds')::integer) THEN 'scheduled_full'
      WHEN origin->>'reason'='periodic_full' AND prior_full IS NOT NULL THEN 'unknown'
      WHEN origin->>'reason'='periodic_full' AND scan_start IS NOT NULL THEN 'uncovered_full'
      ELSE 'unknown' END AS reason,
 coalesce(m->>'format',result->>'video_type','unknown') AS format,
 coalesce((result->>'inserted')::int,0) AS found,
 (m->'cost'->>'attempts')::int AS http_attempts,
 (m->'cost'->>'general')::int AS units,
 (m->'cost'->>'retries')::int AS retries,
 (result->>'outcome'='playlist_not_found' AND result->>'error_code'='typed_playlist_not_found'
   AND (result->>'provider_http_status'='404' OR m->>'first'='true' OR result ? 'first_page_absence_epoch')) AS absent
 FROM context
)
SELECT reason,format,count(*) AS settled_pages,
 count(*) FILTER(WHERE result->>'status' IN('completed','head_observed') OR absent) AS checks_finished,
 count(*) FILTER(WHERE transition_kind='failure' AND NOT coalesce(absent,false)
     OR receipt_scope='quota_defer' OR result->>'status'='error' OR (result->>'status'='playlist_not_found' AND NOT coalesce(absent,false))) AS errors,
 count(*) FILTER(WHERE m ? 'cost' AND http_attempts>0) AS pages_with_cost,
 sum(coalesce(http_attempts,0)) AS http_attempts, sum(coalesce(units,0)) AS general,
 sum(coalesce(retries,0)) AS http_retries,
 count(*) FILTER(WHERE m->>'signal'='true' AND reason<>'signal'
     OR m->>'reason'='manual' AND reason<>'manual'
     OR m->>'scheduled_head'='true' AND m->>'first'='true' AND reason IN('signal','manual','scheduled_full')) AS shared_pages,
 sum(found) AS new_videos, sum(announced) AS already_announced,
 round(coalesce(sum(signal_seconds),0))::bigint AS signal_delay_seconds_sum,
 sum(CASE WHEN m->>'first'='false' OR (result->>'pages_completed')::int>1 THEN found ELSE 0 END) AS deep_finds,
 count(*) FILTER(WHERE receipt_scope='quota_defer') AS provider_quota_refusals,
 count(*) FILTER(WHERE absent) AS verified_absences,
 count(*) FILTER(WHERE was_absent AND absent AND m->>'first'='true') AS still_absent,
 count(*) FILTER(WHERE was_absent AND m->>'first'='true' AND
     (transition_kind='page' OR result->>'status'='head_observed')) AS appeared,
 round(sum(extract(epoch FROM source_checked_at-previous_at)) FILTER(WHERE was_absent AND m->>'first'='true'
    AND (transition_kind='page' OR result->>'status'='head_observed')))::bigint AS appearance_gap_seconds_sum,
 round(avg(extract(epoch FROM source_checked_at-previous_at)) FILTER(WHERE was_absent AND m->>'first'='true'
    AND (transition_kind='page' OR result->>'status'='head_observed')))::bigint AS mean_appearance_check_gap_seconds,
 sum(coalesce((result->>'memberships_marked_absent')::int,0)) AS memberships_removed,
 count(*) FILTER(WHERE result->>'coverage'='complete') AS full_coverages,
 count(*) FILTER(WHERE unchanged) AS unchanged_full,
 count(*) FILTER(WHERE result->>'coverage'='complete' AND unchanged IS NULL) AS full_change_unknown
FROM labelled GROUP BY reason,format ORDER BY format,reason"""

_LEDGER = """SELECT phase,sum(quota_units) AS general,count(*) AS attempts,
 count(*) FILTER(WHERE retry_of_request_attempt_id IS NOT NULL) AS retries
 FROM public.youtube_api_request_attempts WHERE started_at>=%(start)s AND started_at<%(end)s
 AND quota_bucket='general' AND endpoint='playlistItems'
 AND phase IN('typed_inventory_long','typed_inventory_short','typed_inventory_stream')
 GROUP BY phase ORDER BY phase"""
_COVERAGE = _FACTS + """SELECT video_type AS format,count(*) AS eligible,
 count(*) FILTER(WHERE missing_ready) AS confirmed_absent,
 count(*) FILTER(WHERE NOT missing_ready AND NOT within_full_period) AS overdue_or_never_covered
 FROM classified GROUP BY video_type ORDER BY video_type"""


def report(database, *, start: datetime, end: datetime, formats):
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.utcoffset() is None or end.utcoffset() is None
            or not timedelta(0) < end-start <= timedelta(days=28)):
        raise ValueError('report_window_invalid')
    op = Operation('inventory_list_check_report', read_only=True, repeatable_read=True,
                   statement_timeout_ms=30000, total_timeout_ms=60000, lock_timeout_ms=1000, disable_jit=True)
    with database.transaction(op) as tx:
        at = tx.fetch_one('SELECT transaction_timestamp() AS at')['at']
        if end > at or start <= at-HOT_DATA_WINDOW:
            raise ValueError('report_outside_observation_window')
        bounds = dict(start=start, end=end, cutoff=at-HOT_DATA_WINDOW)
        groups = tx.fetch_all(_RESULTS, bounds, max_rows=48)
        ledger = tx.fetch_all(_LEDGER, bounds, max_rows=3)
        coverage = tx.fetch_all(_COVERAGE, _parameters(tx, list(formats), DiscoverySettings()), max_rows=3)
    for group in groups:
        group['pages_without_measured_cost'] = group['settled_pages'] - group['pages_with_cost']
        known = group['pages_without_measured_cost'] == 0
        group['first_page_finds'] = group['new_videos'] - group['deep_finds']
        group['general_per_find'] = round(group['general']/group['new_videos'], 3) if known and group['new_videos'] else None
        group['mean_signal_delay_seconds'] = (round(group['signal_delay_seconds_sum']/group['already_announced'])
                                              if group['already_announced'] else None)
    for row in coverage:
        existing_or_unknown = row['eligible'] - row['confirmed_absent']
        row['overdue_share'] = round(row['overdue_or_never_covered']/existing_or_unknown, 4) if existing_or_unknown else None
    return application_value(dict(version=1, start=start.isoformat(), end_exclusive=end.isoformat(),
                                  coverage_as_of=at.isoformat(), groups=groups, ledger=ledger, coverage=coverage,
                                  ledger_minus_attributed_general=sum(r['general'] for r in ledger)-sum(g['general'] for g in groups)))
