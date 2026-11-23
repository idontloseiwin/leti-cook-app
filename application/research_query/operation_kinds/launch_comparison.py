"""video.launch_comparison.v1: views at 24 h / 7 d / 28 d against the population, leave-one-out.

The Inspector compares launches across videos of the selected type.
Reference point: publication; for Streams the actual end of the broadcast.
Per horizon (target = reference + horizon; tolerance 2 h, 1 d, 1 d before it):
not_reached (target after the clock), outside_observation_window (target before
the clock minus 28 days), missing_publication, ineligible_video (no counter clock),
else the latest real snapshot in [max(window start, target - tolerance), target],
never after the row's own counter observation: missing_snapshot, conflicting_snapshots
(different values at that timestamp), invalid_snapshot (no usable views) or ready.
only that one timestamp is read per video and horizon (at most three reads per
video), instead of every snapshot of every candidate under a 20,000-row ceiling.
Cohort per horizon = ready observations of the population with one known views
definition epoch (unknown epochs are left out and counted; more than one known
epoch withholds the comparison). Each member is classified against the others
(linear quartiles, R-7 over n-1): below P25 / typical / above P75; the inspected
video also gets the leave-one-out median and its multiplier (2 places, ties to even).
Quality: fewer than 4 insufficient_data, fewer than 12 preliminary, else ready.
"""
from fractions import Fraction

from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance
from .inspection import (WINDOW_HOURS, bind, inspection, inspection_parameters, instant, integer, iso, membership,
                         seconds_text, video_identity)

ID = 'video.launch_comparison.v1'
HORIZONS = (('24h', 86_400, 7_200), ('7d', 604_800, 86_400), ('28d', 2_419_200, 86_400))
READS_PER_VIDEO = len(HORIZONS)
STATUSES = frozenset({'ready', 'not_reached', 'missing_publication', 'outside_observation_window', 'missing_snapshot',
                      'invalid_snapshot', 'conflicting_snapshots', 'ineligible_video'})
CLASSES = ('below_typical', 'typical', 'above_typical')

DEFINITION = OperationDefinition(ID, 'public.video.observations.v1', 'video+observed_at+epoch', 'video_comparison',
    'launch_analytics', (),
    'Views of one inspected video at 24 hours, 7 and 28 days after publication (Streams: after their actual end), '
    'each the latest real snapshot within tolerance before the target, against the same-epoch observations of the '
    'population with leave-one-out linear quartiles; only the snapshots at those targets are read.',
    population='inspected_video_against_the_population')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    inspection_parameters(params)
    return ('video.id', 'video.published_at', 'video.actual_end_at')


def quality(count):
    return 'insufficient_data' if count < 4 else 'preliminary' if count < 12 else 'ready'


def quarters(value):
    """A linear quartile of integer counters: whole, .25, .5 or .75 (the old wire format)."""
    number = Fraction(value) * 4
    if number.denominator != 1 or number < 0:
        raise ValueError()
    whole, remainder = divmod(number.numerator, 4)
    return str(whole) + ('', '.25', '.5', '.75')[remainder]


def multiplier(views, median):
    hundredths = round(Fraction(views) / median * 100)  # Fraction rounds ties to even, exactly.
    whole, remainder = divmod(hundredths, 100)
    return f"{whole}.{remainder:02d}"


def _comparison(status, reference_count, **values):
    base = dict(status=status, reference_count=reference_count, median=None, p25=None, p75=None,
                classification=None, multiplier=None, multiplier_reason=None)
    base.update(values)
    return base


def _horizon(raw, member):
    key, seconds, tolerance = HORIZONS[raw['ordinal'] - 1]
    if (raw['key'], raw['seconds'], raw['tolerance']) != (key, seconds, tolerance):
        raise ValueError()
    ready, unknown, epochs, cohort = (raw[name] for name in ('ready_count', 'unknown_definition_count', 'definition_epochs', 'cohort_count'))
    if any(type(value) is not int or value < 0 for value in (ready, unknown, epochs, cohort)) or unknown > ready or cohort > ready:
        raise ValueError()
    state = 'mixed_definition_epochs' if epochs > 1 else 'no_known_values' if epochs == 0 else 'known'
    if (state == 'known') != (cohort == ready - unknown and cohort > 0):
        raise ValueError()
    counts = {name: raw['counts'][name] for name in CLASSES}
    if sum(counts.values()) != (cohort if cohort >= 4 else 0):
        raise ValueError()
    horizon = {'key': key, 'horizon_seconds': seconds, 'tolerance_before_seconds': tolerance,
               'eligible_count': cohort, 'quality': quality(cohort), 'counts': counts,
               'ready_count': ready, 'definition': {'state': state, 'epoch': raw['definition_epoch'] if state == 'known' else None,
                                                    'unknown_definition_count': unknown},
               'target': None}
    if member:
        horizon['target'] = _target(raw['target'], cohort, state, seconds, tolerance)
    return horizon


def _target(raw, cohort, state, seconds, tolerance):
    status = raw['status']
    if status not in STATUSES:
        raise ValueError()
    target_at = None if status in ('missing_publication', 'ineligible_video') else iso(raw['target_at'])
    target = {'status': status, 'target_at': target_at, 'observed_at': None, 'actual_age_seconds': None,
              'views': None, 'definition_epoch': None}
    if status != 'ready':
        target['comparison'] = _comparison('unavailable', cohort)
        return target
    observed, views, age = instant(raw['observed_at']), integer(raw['views']), integer(raw['age_us'])
    if not 0 <= (instant(raw['target_at']) - observed).total_seconds() <= tolerance or age > seconds * 1_000_000:
        raise ValueError()
    target.update(observed_at=iso(observed), actual_age_seconds=seconds_text(age), views=str(views),
                  definition_epoch=raw['epoch'])
    if type(raw['in_cohort']) is not bool or raw['in_cohort'] != (state == 'known' and raw['epoch'] is not None):
        raise ValueError()
    if raw['epoch'] is None: # Its own definition is unknown: never compared.
        target['comparison'] = _comparison('unknown_definition', cohort)
    elif state != 'known':
        target['comparison'] = _comparison(state, 0)
    elif cohort < 4:
        target['comparison'] = _comparison('insufficient_data', cohort - 1)
    else:
        median, low, high = (Fraction(raw[name]) for name in ('median', 'p25', 'p75'))
        classification = 'below_typical' if views < low else 'above_typical' if views > high else 'typical'
        if classification != raw['classification'] or not low <= median <= high:
            raise ValueError()
        target['comparison'] = _comparison(
            quality(cohort), cohort - 1, median=quarters(median), p25=quarters(low), p75=quarters(high),
            classification=classification, multiplier=multiplier(views, median) if median else None,
            multiplier_reason=None if median else 'zero_reference_median')
    return target


def check(result):
    if type(result) is not dict or set(result) != {'video_id', 'video_type', 'member', 'frozen', 'as_of', 'window_from',
                                                   'conditions_applied', 'population', 'candidates', 'snapshot_rows',
                                                   'horizons'}:
        raise ValueError()
    video_id, video_type = video_identity(result)
    as_of, start = instant(result['as_of']), instant(result['window_from'])
    if not start < as_of:
        raise ValueError()
    reference, _frozen = inspection(result)
    horizons = result['horizons']
    if type(horizons) is not list or [item['ordinal'] for item in horizons] != [1, 2, 3]:
        raise ValueError()
    for name in ('population', 'candidates', 'snapshot_rows'):
        if type(result[name]) is not int or result[name] < 0:
            raise ValueError()
    if result['candidates'] > READS_PER_VIDEO * result['population'] or type(result['conditions_applied']) is not bool:
        raise ValueError()
    return {'video_id': video_id, 'video_type': video_type, 'membership': membership(result), 'inspection': reference,
            'method': 'measured_launch_before_horizon_v1',
            # From the request time minus 28 days to the inspection clock (frozen or now).
            'window': {'days': 28, 'hours': WINDOW_HOURS, 'from_at': iso(start), 'to_at': iso(as_of)},
            'reference': {'video_type': video_type, 'publication_period': 'all_time',
                          'reference_point': 'actual_end_at' if video_type == 'stream' else 'published_at',
                          'query_conditions_applied': result['conditions_applied'], 'leave_one_out': True},
            'coverage': {'population_count': result['population'], 'candidate_targets': result['candidates'],
                         'snapshot_rows': result['snapshot_rows'], 'reads_per_video_limit': READS_PER_VIDEO,
                         'complete': True},
            'horizons': [_horizon(item, result['member']) for item in horizons]}


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), bind=bind)
