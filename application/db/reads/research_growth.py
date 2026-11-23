"""28-day view growth of every admitted video: the one SQL owner of the rule.

The prepared CTE computes video.observed_growth_28d.v1 for each admitted row.
The same columns feed cards, conditions and ordering of the whole
matching population before paging, the Inspector and the population operation.
Protected views only, one statement snapshot, values only as parameters.

Per admitted row:
- endpoint: the latest snapshot recorded exactly at the current counter clock
  with the same views, only if the counter and the inventory proof were observed
  within the 28-day span and the metadata within the retention window, as the
  row's own metadata (else unavailable: video_not_in_scope,
  current_data_stale, current_counter_unavailable, current_snapshot_missing);
  a Stream uses only snapshots at or after its actual end.
- window start: endpoint - 28 days. Start: the latest snapshot at or before it,
  else the earliest after it, up to the endpoint. Each point must stay readable
  through the result's whole life (points_until).
- ready: start at most 3 hours after the window start; partial: endpoint - start
  at least 24 hours; started_today: the start's local date equals the evaluation's
  in the request time zone; otherwise waiting_next_snapshot.
- ready and partial need both points in one known views definition epoch;
  otherwise unavailable with unknown_definition or incompatible_definition.
- signed delta (a decrease is a correction, not clamped); interval in whole
  seconds; per day = delta * 86400 / interval, half-even to 0.01; tracked days =
  floor(interval / 86400) for partial; boundary distance = window start - start.
"""
from research_query import growth


def requested(plan):
    return bool(set(plan.referenced_fields) & set(growth.FIELDS))


def frozen(plan):
    return plan.request.consistency == 'frozen'


def parameters(plan):
    """Fixed spans as interval strings (exact hours, never days across DST)."""
    request = plan.request
    return {'growth_window': f'{growth.WINDOW_HOURS} hours',
            'growth_late_start': f'{growth.LATE_START_SECONDS} seconds',
            'growth_partial_minimum': f'{growth.PARTIAL_MINIMUM_SECONDS} seconds',
            'growth_time_zone': request.time.timezone,
            **({} if frozen(plan) else {'growth_horizon': f'{growth.CURRENT_RESULT_SECONDS} seconds'})}


def points_until(plan):
    """Every point used must stay readable until the result itself ends.

    Frozen pages use their shared expiry; current pages use the query horizon.
    """
    return 'g_args.fresh_until' if frozen(plan) else 'g_args.evaluated_at+%(growth_horizon)s::interval'


def metadata_horizon(plan):
    """Check metadata at the shared freshness boundary for this result."""
    return 'g_args.fresh_until' if frozen(plan) else 'g_args.evaluated_at'


def joins(plan):
    return JOINS.replace('{points_until}', points_until(plan)).replace('{metadata_horizon}', metadata_horizon(plan))


# Requires normalized n (video_type, identity_clock, checked_at, metadata_checked_at,
# views, actual_end_at, core_expiry), the args CTE and %(window)s (retention hours).
JOINS = """CROSS JOIN args g_args
CROSS JOIN LATERAL (SELECT
 coalesce(n.identity_clock>=g_args.evaluated_at-%(growth_window)s::interval,false) AS in_scope,
 coalesce(isfinite(n.checked_at) AND isfinite(n.metadata_checked_at)
 AND n.checked_at>=g_args.evaluated_at-%(growth_window)s::interval AND n.checked_at<=g_args.evaluated_at
 AND n.metadata_checked_at>{metadata_horizon}-%(window)s::interval
 AND n.metadata_checked_at<=g_args.evaluated_at,false) AS fresh,
 {points_until}-%(window)s::interval AS points_from) g_in
LEFT JOIN LATERAL (
 SELECT s.recorded_at,s.views,s.views_definition_epoch,s.source_expires_at
 FROM readable.video_snapshots s WHERE g_in.in_scope AND g_in.fresh AND n.views IS NOT NULL
 AND s.video_id=n.video_id AND s.recorded_at=n.checked_at AND s.views=n.views
 AND (n.video_type<>'stream' OR s.recorded_at>=n.actual_end_at)
 ORDER BY s.id DESC LIMIT 1) g_end ON true
LEFT JOIN LATERAL (
 SELECT s.recorded_at,s.views,s.views_definition_epoch,s.source_expires_at
 FROM readable.video_snapshots s WHERE g_end.recorded_at IS NOT NULL AND s.video_id=n.video_id
 AND s.recorded_at<=g_end.recorded_at-%(growth_window)s::interval AND s.recorded_at>=g_in.points_from
 AND s.views>=0 AND (n.video_type<>'stream' OR s.recorded_at>=n.actual_end_at)
 ORDER BY s.recorded_at DESC,s.id DESC LIMIT 1) g_full ON true
LEFT JOIN LATERAL (
 SELECT s.recorded_at,s.views,s.views_definition_epoch,s.source_expires_at
 FROM readable.video_snapshots s WHERE g_end.recorded_at IS NOT NULL AND g_full.recorded_at IS NULL
 AND s.video_id=n.video_id AND s.recorded_at>g_end.recorded_at-%(growth_window)s::interval
 AND s.recorded_at<=g_end.recorded_at AND s.recorded_at>=g_in.points_from
 AND s.views>=0 AND (n.video_type<>'stream' OR s.recorded_at>=n.actual_end_at)
 ORDER BY s.recorded_at ASC,s.id DESC LIMIT 1) g_first ON true
CROSS JOIN LATERAL (SELECT
 CASE WHEN g_full.recorded_at IS NOT NULL THEN g_full.recorded_at ELSE g_first.recorded_at END AS at,
 CASE WHEN g_full.recorded_at IS NOT NULL THEN g_full.views ELSE g_first.views END AS views,
 CASE WHEN g_full.recorded_at IS NOT NULL THEN g_full.views_definition_epoch ELSE g_first.views_definition_epoch END AS epoch,
 CASE WHEN g_full.recorded_at IS NOT NULL THEN g_full.source_expires_at ELSE g_first.source_expires_at END AS expires_at,
 g_end.recorded_at-%(growth_window)s::interval AS window_start,
 CASE WHEN g_in.in_scope AND g_in.fresh THEN n.checked_at END AS end_at,
 CASE WHEN g_in.in_scope AND g_in.fresh THEN n.views END AS end_views) g_start
CROSS JOIN LATERAL (SELECT
 coalesce(g_start.at<=g_start.window_start+%(growth_late_start)s::interval,false) AS ready_window,
 coalesce(g_end.recorded_at-g_start.at>=%(growth_partial_minimum)s::interval,false) AS partial_window,
 CASE WHEN g_start.epoch IS NULL OR g_end.views_definition_epoch IS NULL THEN 'unknown_definition'
 WHEN g_start.epoch<>g_end.views_definition_epoch THEN 'incompatible_definition' END AS epoch_problem,
 coalesce((g_start.at AT TIME ZONE %(growth_time_zone)s::text)::date
 =(g_args.evaluated_at AT TIME ZONE %(growth_time_zone)s::text)::date,false) AS started_today) g_test
CROSS JOIN LATERAL (SELECT CASE
 WHEN NOT g_in.in_scope THEN 'video_not_in_scope'
 WHEN NOT g_in.fresh THEN 'current_data_stale'
 WHEN n.views IS NULL THEN 'current_counter_unavailable'
 WHEN g_end.recorded_at IS NULL THEN 'current_snapshot_missing'
 WHEN (g_test.ready_window OR g_test.partial_window) AND g_test.epoch_problem IS NOT NULL THEN g_test.epoch_problem
 WHEN g_test.ready_window THEN NULL
 WHEN g_test.partial_window THEN 'full_window_history_unavailable'
 WHEN g_test.started_today THEN 'tracking_started_today'
 ELSE 'waiting_for_next_snapshot' END AS reason) g_reason
CROSS JOIN LATERAL (SELECT CASE
 WHEN g_reason.reason IS NULL THEN 'ready'
 WHEN g_reason.reason='full_window_history_unavailable' THEN 'partial'
 WHEN g_reason.reason='tracking_started_today' THEN 'started_today'
 WHEN g_reason.reason='waiting_for_next_snapshot' THEN 'waiting_next_snapshot'
 ELSE 'unavailable' END AS status) g_status
CROSS JOIN LATERAL (SELECT
 CASE WHEN g_status.status<>'unavailable' THEN floor(extract(epoch FROM g_end.recorded_at-g_start.at)) END AS interval_seconds,
 CASE WHEN g_status.status IN('ready','partial') THEN g_end.views::numeric-g_start.views::numeric END AS delta,
 CASE WHEN g_status.status IN('ready','partial')
 THEN abs(g_end.views::numeric-g_start.views::numeric)*8640000 END AS scaled) g_value
CROSS JOIN LATERAL (SELECT
 CASE WHEN g_value.delta IS NOT NULL THEN trim_scale(0.01*sign(g_value.delta)*(div(g_value.scaled,g_value.interval_seconds)
 +CASE WHEN 2*mod(g_value.scaled,g_value.interval_seconds)>g_value.interval_seconds
 OR (2*mod(g_value.scaled,g_value.interval_seconds)=g_value.interval_seconds
 AND mod(div(g_value.scaled,g_value.interval_seconds),2)=1) THEN 1 ELSE 0 END)) END AS per_day) g_rate"""

FIELDS = (
    "g_status.status AS observation_growth_28d_status",
    "'known'::text AS observation_growth_28d_status_state",
    "g_reason.reason AS observation_growth_28d_reason",
    "g_value.delta AS observation_views_gained_28d",
    "CASE WHEN g_value.delta IS NOT NULL THEN 'known' ELSE 'unavailable' END AS observation_views_gained_28d_state",
    "g_rate.per_day AS observation_views_per_day_28d",
    "CASE WHEN g_rate.per_day IS NOT NULL THEN 'known' ELSE 'unavailable' END AS observation_views_per_day_28d_state",
    "g_value.interval_seconds AS observation_growth_28d_interval_seconds",
    "CASE WHEN g_value.interval_seconds IS NOT NULL THEN 'known' ELSE 'unavailable' END AS observation_growth_28d_interval_seconds_state",
    "CASE WHEN g_status.status='partial' THEN div(g_value.interval_seconds,86400) END AS observation_growth_28d_tracked_days",
    "CASE WHEN g_status.status='partial' THEN 'known' ELSE 'unavailable' END AS observation_growth_28d_tracked_days_state",
    # A classification without an observation is made at evaluation time.
    "coalesce(g_start.end_at,g_args.evaluated_at) AS observation_growth_28d_observed_at",
    "least(g_start.expires_at,g_end.source_expires_at,g_start.end_at+%(window)s::interval,n.core_expiry)"
    " AS observation_growth_28d_expires_at",
    """jsonb_build_object('method','video.observed_growth_28d.v1','version',1,
 'status',g_status.status,'reason',g_reason.reason,
 'from_at',g_start.at,'to_at',g_start.end_at,'from_views',g_start.views::text,'to_views',g_start.end_views::text,
 'actual_interval_seconds',g_value.interval_seconds::text,
 'boundary_distance_seconds',CASE WHEN g_status.status='ready'
 THEN floor(extract(epoch FROM g_start.window_start-g_start.at))::text END,
 'tracked_days',CASE WHEN g_status.status='partial' THEN div(g_value.interval_seconds,86400)::text END,
 'definition_epoch',CASE WHEN g_test.epoch_problem IS NULL THEN g_end.views_definition_epoch END,
 'from_definition_epoch',g_start.epoch,'to_definition_epoch',g_end.views_definition_epoch,
 'time_zone',%(growth_time_zone)s::text) AS observation_growth_28d_evidence""",
)

# Only the points' own retention ends this result; the horizon keeps them past its life.
DEADLINES = ('g_start.expires_at', 'g_end.source_expires_at')
