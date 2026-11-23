"""video.observed_history.v1: one inspected video's real snapshot series (views, likes, comments).

The Inspector shows timestamped observations for each video type.
Window: from the
evaluation time minus 28 days (672 hours) through the inspected row's counter
observation (checked_at); Streams start no earlier than their actual end.
At most 500 snapshots, ordered by recorded_at. Identical rows at one timestamp
collapse; different values there make that metric's series unavailable. The
series must end at a snapshot at checked_at with the current counter value.
No interpolation: intervals join adjacent real points, exact signed delta and
delta per elapsed day (2 places, ties to even). Views intervals need one known
definition epoch at both ends; likes/comments have no definition epoch.
"""
from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance
from .inspection import (COUNTERS, WINDOW_HOURS, bind, frozen_cell, inspection, inspection_parameters, instant,
                         integer, iso, membership, microseconds, per_day, seconds_text, video_identity)

ID = 'video.observed_history.v1'
ROW_LIMIT = 500
METRICS = ('views', 'likes', 'comments')
METHODS = {'ordering': 'recorded_at_ascending', 'same_timestamp': 'identical_values_collapsed_conflicts_unavailable',
           'gaps': 'no_interpolation_or_daily_allocation',
           'rate': 'signed_delta_per_exact_elapsed_second_times_86400_rounded_2dp_half_even',
           'window': 'evaluated_at_minus_28_days_through_counter_observation',
           'views_definition': 'interval_requires_one_known_definition_epoch',
           'streams': 'observations_from_actual_end',
           'interaction_metrics': 'independent_stored_counters_same_observation_window'}

DEFINITION = OperationDefinition(ID, 'public.video.observations.v1', 'video+observed_at+epoch', 'video_series',
    'additional_analytics', (),
    'Real snapshots of one inspected video from evaluation minus 28 days through its counter observation; '
    'collapsed identical timestamps, exact adjacent deltas and per-day rates, one definition epoch per views interval.',
    population='one_inspected_video_of_the_population')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    inspection_parameters(params)
    return ('video.id', 'video.views', 'video.likes', 'video.comments', 'video.actual_end_at')


def _empty(reason, duplicates=0):
    return {'status': 'unavailable', 'reason': reason, 'intervals': [], 'latest_interval': None,
            'coverage': {'point_count': 0, 'valid_count': 0, 'missing_count': 0, 'duplicate_timestamp_count': duplicates}}


def _series(metric, points, expected, end, duplicates):
    state = expected[metric + '_state']
    if state == 'unavailable':  # A stored current counter that is not a valid count.
        return _empty('invalid_catalogue_endpoint')
    if state not in ('known', 'unknown'):
        return _empty('catalogue_endpoint_outside_hot_window')
    value = integer(expected[metric]) if state == 'known' else None
    for point in points:
        distinct, missing = point[metric + '_values'], point[metric + '_missing']
        if type(distinct) is not int or type(missing) is not bool or distinct > 1 or (distinct == 1 and missing):
            if type(distinct) is not int or type(missing) is not bool:
                raise ValueError()
            return _empty('conflicting_simultaneous_observations', duplicates)
    if not points or points[-1]['at'] != end:
        return _empty('endpoint_snapshot_missing', duplicates)
    if points[-1][metric + '_invalid'] or integer(points[-1][metric]) != value:
        return _empty('endpoint_counter_mismatch', duplicates)
    intervals = []
    for earlier, later in zip(points, points[1:]):
        elapsed = microseconds(earlier['at'], later['at'])
        if elapsed <= 0 or integer(later['elapsed_us']) != elapsed:
            raise ValueError()
        before, after = integer(earlier[metric]), integer(later[metric])
        delta = None if before is None or after is None else after - before
        if (delta is None) != (later[metric + '_delta'] is None) or (
                delta is not None and int(later[metric + '_delta']) != delta):
            raise ValueError()
        interval = {'from_at': iso(earlier['at']), 'to_at': iso(later['at']),
                    'actual_interval_seconds': seconds_text(elapsed), 'delta': None, 'per_day': None}
        if metric == 'views':
            epochs = (earlier['epoch'], later['epoch'])
            interval['definition'] = ('unknown_definition' if None in epochs else
                                      'incompatible_definition' if epochs[0] != epochs[1] else 'known')
            if interval['definition'] != 'known':
                delta = None
        if delta is not None:
            interval.update(delta=str(delta), per_day=per_day(delta, elapsed))
        intervals.append(interval)
    valid = sum(point[metric] is not None for point in points)
    return {'status': 'ready', 'reason': None, 'intervals': intervals,
            'latest_interval': intervals[-1] if intervals and intervals[-1]['delta'] is not None else None,
            'coverage': {'point_count': len(points), 'valid_count': valid, 'missing_count': len(points) - valid,
                         'duplicate_timestamp_count': duplicates}}


def _frozen_expected(frozen):
    """The frozen row's counters in the shape of the current ones (value text, state)."""
    expected = {}
    for field in COUNTERS:
        cell = frozen_cell(frozen[field])
        metric = field.removeprefix('video.')
        expected[metric], expected[metric + '_state'] = cell['value'], cell['state']
    return expected


def check(result):
    if type(result) is not dict or set(result) != {'video_id', 'video_type', 'member', 'frozen', 'as_of', 'bounds',
                                                   'expected', 'scanned', 'points'}:
        raise ValueError()
    video_id, video_type = video_identity(result)
    reference, frozen = inspection(result)
    frozen_counters = frozen is not None and all(field in frozen for field in COUNTERS)
    output = {'video_id': video_id, 'video_type': video_type, 'membership': membership(result), 'inspection': reference,
              'status': 'unavailable', 'reason': None,
              'window': {'days': 28, 'hours': WINDOW_HOURS, 'from_at': None, 'to_at': None,
                         'rule': 'evaluated_at_minus_28_days_through_counter_observation',
                         'endpoint': 'frozen_counter_observation' if frozen_counters else 'counter_observation',
                         'start': 'actual_end_at_if_later' if video_type == 'stream' else 'window'},
              'row_limit': ROW_LIMIT, 'scanned_count': 0, 'points': [],
              'series': {metric: _empty('video_not_in_population') for metric in METRICS}, 'methods': dict(METHODS)}

    def unavailable(reason):
        output['reason'] = reason
        output['series'] = {metric: _empty(reason) for metric in METRICS}
        return output

    if not result['member']:
        return unavailable('video_not_in_population')
    bounds, expected = result['bounds'], _frozen_expected(frozen) if frozen_counters else result['expected']
    if type(bounds) is not dict or type(expected) is not dict or type(result['points']) is not list:
        raise ValueError()
    if bounds['end_at'] is None:
        return unavailable('invalid_catalogue_endpoint')
    as_of, start, end = instant(result['as_of']), instant(bounds['from_at']), instant(bounds['end_at'])
    if not start <= end <= as_of:
        return unavailable('catalogue_endpoint_outside_hot_window')
    output['window'].update(from_at=iso(start), to_at=iso(end))
    scanned = result['scanned']
    if type(scanned) is not int or not 0 <= scanned <= ROW_LIMIT + 1:
        raise ValueError()
    output['scanned_count'] = scanned
    if scanned > ROW_LIMIT:
        return unavailable('observation_limit_exceeded')
    points, previous, rows = [], None, 0
    for raw in result['points']:
        at = instant(raw['recorded_at'])
        if not start <= at <= end or (previous is not None and at <= previous) or type(raw['rows']) is not int or raw['rows'] < 1:
            raise ValueError()
        previous, rows = at, rows + raw['rows']
        points.append({**raw, 'at': at})
    if rows != scanned:
        raise ValueError()
    duplicates = scanned - len(points)
    series = {metric: _series(metric, points, expected, end, duplicates) for metric in METRICS}
    output.update(status=series['views']['status'], reason=series['views']['reason'], series=series)
    if any(item['status'] == 'ready' for item in series.values()):
        output['points'] = [{'recorded_at': iso(point['at']),
                             **{metric: point[metric] if series[metric]['status'] == 'ready' else None for metric in METRICS},
                             'views_definition_epoch': point['epoch'] if series['views']['status'] == 'ready' else None}
                            for point in points]
    return output


_POINT = ",".join(
    f"'{metric}',{metric}::text,'{metric}_values',{metric}_values,'{metric}_missing',{metric}_missing,"
    f"'{metric}_invalid',{metric}_invalid,'{metric}_delta',{metric}_delta::text" for metric in METRICS)


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), bind=bind)
