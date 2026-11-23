"""channel.counter_change.v1: 7D/28D change of the channel's reported counters.

Owner of the channel-counter definition rule: YouTube reports channel
subscribers and views without a definition version, so no definition epoch is
recorded or required (CHANNEL_COUNTER_DEFINITION_EPOCH); the change and its
percentage are plain arithmetic over two real reported values. The channel
history operation reports the same absent epoch.

Windows follow the video observation-window rule: the end is the latest real
snapshot, at most CURRENT_MAX_AGE_HOURS old (else stale_current); the start is
the last snapshot reporting the counter at or before end - window, no earlier
than end - window - TOLERANCE_HOURS. Only such an interval, between the window
and the window plus the tolerance, is a full window. Without it, the earliest later snapshot
inside the window gives a partial change over its actual interval. No
interpolation; corrections keep their sign. The result lives as long as the
snapshots it uses.
"""
from datetime import timedelta

from ..registry import CHANNEL_SOURCE_ID
from . import overview
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'channel.counter_change.v1'
CHANNEL_COUNTER_DEFINITION_EPOCH = None
DEFINITION_RULE = 'reported_channel_counter_without_definition_v1'
WINDOWS = (('7d', 7 * 24), ('28d', 28 * 24))
METRICS = ('subscribers', 'views')
TOLERANCE_HOURS = 24  # Channel profiles are refreshed about once a day.
CURRENT_MAX_AGE_HOURS = 24

DEFINITION = OperationDefinition(ID, 'public.channel.observations.v1', 'channel+observed_at', 'channel', 'channel_header', (),
    'Change of reported channel subscribers and views over 7 and 28 days between two real snapshots, the start '
    'at most one day before the window boundary; actual interval disclosed; no interpolation or definition epoch.',
    population='authorized_single_channel')


def dependencies(params, source_id, scope_kind):
    if source_id != CHANNEL_SOURCE_ID or scope_kind != 'channel' or params:
        raise ValueError()
    return ()


def _point(value):
    if value is None:
        return None
    if set(value) != {'recorded_at', 'value', 'expires_at'}:
        raise ValueError()
    number = value['value']
    if number is not None and (type(number) is not str or not number.isdigit()):
        raise ValueError()
    return dict(recorded_at=overview.utc_iso(value['recorded_at']), value=number,
                expires_at=overview.utc_iso(value['expires_at']))


def _instant(value):
    from datetime import datetime
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _percent(change, start):
    # Return the percentage as decimal text rounded to six places.
    value = round(change * 100 / start, 6)
    return '0' if value == 0 else format(value, 'f').rstrip('0').rstrip('.')


def _metric(window_hours, end, boundary, earliest, evaluated_at):
    result = {'status': 'not_reported', 'coverage': 'unavailable', 'start_value': None, 'end_value': None,
              'absolute_change': None, 'percent_change': None, 'observed_start_at': None, 'observed_end_at': None,
              'actual_interval_seconds': None, 'expires_at': None, 'reason_code': 'no_observation'}
    if end is None:
        return result
    result.update(observed_end_at=end['recorded_at'], expires_at=end['expires_at'])
    if end['value'] is None:
        result['reason_code'] = 'counter_not_reported'
        return result
    result['end_value'] = end['value']
    if _instant(evaluated_at) - _instant(end['recorded_at']) > timedelta(hours=CURRENT_MAX_AGE_HOURS):
        result.update({'status': 'stale_current', 'reason_code': 'latest_observation_too_old'})
        return result
    start = boundary or earliest
    if start is None:
        result.update({'status': 'not_enough_history', 'reason_code': 'no_earlier_observation_in_window'})
        return result
    covered = boundary is not None
    first, last = int(start['value']), int(end['value'])
    elapsed = _instant(end['recorded_at']) - _instant(start['recorded_at'])
    seconds = elapsed.days * 86400 + elapsed.seconds
    if not 0 < seconds <= (window_hours + TOLERANCE_HOURS) * 3600 or covered and seconds < window_hours * 3600:
        raise ValueError()
    result.update(coverage='full' if covered else 'partial', start_value=start['value'], observed_start_at=start['recorded_at'],
                  actual_interval_seconds=seconds, absolute_change=str(last - first),
                  expires_at=min(end['expires_at'], start['expires_at']))
    result['reason_code'] = None
    if first == 0:
        result.update({'status': 'zero_baseline', 'reason_code': 'zero_start_value'})
    else:
        result.update({'status': 'ready' if covered else 'partial', 'percent_change': _percent(last - first, first),
                       'reason_code': None if covered else 'start_after_window_boundary'})
    return result


def check(result):
    if set(result) != {'evaluated_at', 'latest', 'windows'} or set(result['windows']) != {p for p, _ in WINDOWS}:
        raise ValueError()
    evaluated_at = overview.utc_iso(result['evaluated_at'])
    latest = result['latest']
    if latest is not None and set(latest) != {'recorded_at', 'subscribers', 'views', 'expires_at'}:
        raise ValueError()
    windows = {}
    for period, hours in WINDOWS:
        metrics = {}
        for name in METRICS:
            item = result['windows'][period][name]
            if set(item) != {'boundary', 'earliest'}:
                raise ValueError()
            end = None if latest is None else _point(dict(recorded_at=latest['recorded_at'], value=latest[name],
                                                          expires_at=latest['expires_at']))
            metrics[name] = _metric(hours, end, _point(item['boundary']), _point(item['earliest']), evaluated_at)
        windows[period] = dict(period=period, window_seconds=hours * 3600, tolerance_seconds=TOLERANCE_HOURS * 3600,
                               metrics=metrics)
    return dict(definition_epoch=CHANNEL_COUNTER_DEFINITION_EPOCH, definition_rule=DEFINITION_RULE,
                current_max_age_seconds=CURRENT_MAX_AGE_HOURS * 3600, windows=windows)


def lifetime(result):
    """The earliest expiry of the snapshots this result shows, and the
    moment a shown end point becomes too old to be current (stale_current)."""
    deadlines = []
    for window in result['windows'].values():
        for metric in window['metrics'].values():
            if metric['expires_at'] is not None:
                deadlines.append(_instant(metric['expires_at']))
            if metric['observed_end_at'] is not None and metric['status'] != 'stale_current':
                deadlines.append(_instant(metric['observed_end_at']) + timedelta(hours=CURRENT_MAX_AGE_HOURS, microseconds=1))
    return min(deadlines) if deadlines else None


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), lifetime=lifetime)
