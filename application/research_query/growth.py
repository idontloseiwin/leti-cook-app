"""28-day view growth (video.observed_growth_28d.v1): vocabulary, rounding and checks.

Five per-video fields supply growth values for cards, filters and sorting across the matching population before paging. db/reads/research_growth.py
is the only SQL owner of the rule; this leaf module holds its closed vocabulary,
the rounding rules, the v1 condition/sort translation and the integrity
checks applied to every returned cell. No SQL, I/O or package imports.
"""
from datetime import datetime
from fractions import Fraction

OPERATION_ID = 'video.observed_growth_28d.v1'
SUMMARY_ID = 'video.observed_growth_28d_summary.v1'

STATUS = 'observation.growth_28d_status'
GAINED = 'observation.views_gained_28d'
PER_DAY = 'observation.views_per_day_28d'
INTERVAL = 'observation.growth_28d_interval_seconds'
TRACKED_DAYS = 'observation.growth_28d_tracked_days'
FIELDS = (STATUS, GAINED, PER_DAY, INTERVAL, TRACKED_DAYS)
# The five cells of a row share one clock pair and one evidence object, read from
# columns with this prefix. The object is attached once per row (a frozen result
# keeps 20,000 rows): to the status cell when selected, else to the first
# selected growth field in FIELDS order. The other growth cells carry none.
EVIDENCE_PREFIX = 'observation_growth_28d'


def evidence_carrier(selected):
    return next((field for field in FIELDS if field in selected), None)

STATUSES = ('ready', 'partial', 'started_today', 'waiting_next_snapshot', 'unavailable')
CALCULABLE = ('ready', 'partial')
EPOCH_REASONS = ('unknown_definition', 'incompatible_definition')
REASONS = {
    'ready': (None,),
    'partial': ('full_window_history_unavailable',),
    'started_today': ('tracking_started_today',),
    'waiting_next_snapshot': ('waiting_for_next_snapshot',),
    # Check the v1 conditions first; the views-definition epoch rule adds the last two.
    'unavailable': ('video_not_in_scope', 'current_data_stale', 'current_counter_unavailable',
                    'current_snapshot_missing', *EPOCH_REASONS),
}

# 28 days (hot_storage.ANALYTICS_WINDOW) before each video's own endpoint. The
# same span is the freshness floor of the endpoint's counter and inventory
# proof, as in GROWTH_SQL. Its metadata needs only the retention window:
# old videos' metadata is refreshed exactly every 672 h.
WINDOW_HOURS = 672
WINDOW_SECONDS = WINDOW_HOURS * 3600
LATE_START_SECONDS = 3 * 3600  # A start up to 3 hours after the window start is a full window.
BOUNDARY_TOLERANCE_SECONDS = 86400  # Retention keeps at most one more day before it.
PARTIAL_MINIMUM_SECONDS = 86400
# Maximum life of a current-consistency result (research_sql: evaluated_at + 120 s).
# A frozen result ends at args.fresh_until (FROZEN_TTL_SECONDS after evaluation).
CURRENT_RESULT_SECONDS = 120

WINDOW = {
    'metric': 'views', 'requested_days': 28, 'boundary_tolerance_days': 1,
    'late_start_tolerance_seconds': LATE_START_SECONDS, 'endpoint': 'per_video_current_checked_at',
}
METHODS = {
    'endpoint_observation': 'current_video_row_with_matching_snapshot',
    'endpoint_freshness': 'counter_and_inventory_observed_within_672h_metadata_within_696h',
    'full_window_start': 'latest_snapshot_at_or_before_28d_before_endpoint',
    'partial_window_start': 'earliest_snapshot_within_28d_before_endpoint',
    'partial_minimum_seconds': PARTIAL_MINIMUM_SECONDS,
    'counter_decrease': 'reported_as_correction_not_clamped',
    'average_views_per_day': 'signed_delta_per_actual_second_times_86400_rounded_2dp_half_even',
    'sorting': 'signed_delta_ready_and_partial_nulls_last_then_video_id',
    'definition_epoch': 'both_points_same_views_definition_epoch_else_unavailable',
    'started_today': 'start_local_date_equals_evaluation_local_date_in_request_time_zone',
    'stream_points': 'at_or_after_actual_end',
    'points_horizon': 'points_readable_through_result_lifetime',
    'pagination_applied_after_sorting': True,
}


def average_views_per_day(delta: int, interval: int) -> str:
    """Exact signed mean per day over whole seconds, 2 places, ties to even."""
    hundredths = round(Fraction(delta * 86400, interval) * 100)
    whole, fraction = divmod(abs(hundredths), 100)
    sign = '-' if hundredths < 0 else ''
    suffix = f'.{fraction:02d}'.rstrip('0') if fraction else ''
    return f'{sign}{whole}{suffix}'


# Library v1 sort key and v1 facets.growth conditions.
LEGACY_SORT_FIELDS = {'views_gained': GAINED}
_UINT64_MAX = 2**64 - 1


def legacy_conditions(growth):
    """Translate saved Library growth filters into one conjunction.

    `growth` is the normalized v1 object (library_v1.normalize_growth_filters): any
    condition admits only ready and partial rows (gained is known exactly then),
    minimum tracked days compares the actual interval with days * 86400.
    """
    if not growth:
        return None
    leaves = [{'field': GAINED, 'op': 'is_known'}]
    if 'min_views_gained' in growth:
        leaves.append({'field': GAINED, 'op': 'gte', 'value': str(int(growth['min_views_gained']))})
    if 'max_views_gained' in growth:
        leaves.append({'field': GAINED, 'op': 'lte', 'value': str(int(growth['max_views_gained']))})
    if 'min_tracked_days' in growth:
        # No interval reaches 2**64 - 1: larger v1 bounds keep matching nothing.
        seconds = min(int(growth['min_tracked_days']) * 86400, _UINT64_MAX)
        leaves.append({'field': INTERVAL, 'op': 'gte', 'value': str(seconds)})
    if growth.get('full_history'):
        leaves.append({'field': STATUS, 'op': 'eq', 'value': 'ready'})
    return {'all': leaves}


def _integer(value):
    return None if value is None else int(value)


def _instant(value):
    return None if value is None else datetime.fromisoformat(value.replace('Z', '+00:00'))


def evidence_problem(evidence):
    """The integrity rules of one growth row; None when consistent."""
    status, reason = evidence['status'], evidence['reason']
    if reason not in REASONS[status]:
        return 'growth_reason_mismatch'
    interval = _integer(evidence['actual_interval_seconds'])
    boundary = _integer(evidence['boundary_distance_seconds'])
    tracked = _integer(evidence['tracked_days'])
    start, end = _instant(evidence['from_at']), _instant(evidence['to_at'])
    measured = None if start is None or end is None else (end - start).days * 86400 + (end - start).seconds
    epochs = (evidence['from_definition_epoch'], evidence['to_definition_epoch'])
    if status in CALCULABLE:
        if (None in (start, end, interval, evidence['from_views'], evidence['to_views'])
                or measured != interval or evidence['definition_epoch'] is None
                or epochs != (evidence['definition_epoch'],) * 2):
            return 'growth_comparison_invalid'
        if status == 'ready':
            if (boundary is None or tracked is not None or interval != WINDOW_SECONDS + boundary
                    or not -LATE_START_SECONDS <= boundary <= BOUNDARY_TOLERANCE_SECONDS):
                return 'growth_ready_invalid'
        elif (boundary is not None or not PARTIAL_MINIMUM_SECONDS <= interval < WINDOW_SECONDS
              or tracked != interval // 86400 or tracked < 1):
            return 'growth_partial_invalid'
    elif status != 'unavailable':
        if (None in (start, end, interval) or measured != interval or interval >= PARTIAL_MINIMUM_SECONDS
                or boundary is not None or tracked is not None):
            return 'growth_tracking_invalid'
    else:
        if interval is not None or boundary is not None or tracked is not None:
            return 'growth_unavailable_invalid'
        if reason in EPOCH_REASONS:
            if (start is None or end is None or evidence['definition_epoch'] is not None
                    or (reason == 'unknown_definition') != (None in epochs)
                    or (reason == 'incompatible_definition' and epochs[0] == epochs[1])):
                return 'growth_epoch_invalid'
        elif start is not None or evidence['from_views'] is not None:
            return 'growth_unavailable_invalid'
    return None


def cells_problem(cells):
    """Cells of one row against their shared evidence; None when consistent.

    `cells` maps the selected growth field ids to (state, value, evidence dict
    or None); only the evidence carrier holds the object.
    """
    carrier = evidence_carrier(cells)
    if any((evidence is None) != (field != carrier) for field, (_, _, evidence) in cells.items()):
        return 'growth_evidence_mismatch'
    evidence = cells[carrier][2]
    status = evidence['status']
    calculable = status in CALCULABLE
    gained = None
    if calculable:
        gained = int(evidence['to_views']) - int(evidence['from_views'])
    expected = {
        STATUS: status,
        GAINED: None if gained is None else str(gained),
        PER_DAY: None if gained is None else average_views_per_day(gained, int(evidence['actual_interval_seconds'])),
        INTERVAL: evidence['actual_interval_seconds'],
        TRACKED_DAYS: evidence['tracked_days'],
    }
    for field, (state, value, _) in cells.items():
        if value != expected[field] or (state == 'known') != (value is not None):
            return 'growth_cell_mismatch'
        if field != STATUS and state not in ('known', 'unavailable'):
            return 'growth_cell_mismatch'
    return None
