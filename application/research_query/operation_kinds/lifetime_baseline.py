"""video.lifetime_baseline.v1: the population's current lifetime views median and P25-P75.

The Inspector channel baseline (old video_lifetime_baseline over channel_baseline
with period=max) for all types: every population row, including the inspected
video. The Library sends the channel's query without conditions (all time, one
type). Statistics are the shared ones of video.summary.v1 (population_stats): exact
linear quartiles (R-7, n-1), views only under one known definition epoch;
a value without a recorded definition is left out and counted.
Sample = rows with known views; status by sample size: fewer than 4
insufficient_data (no statistics), fewer than 12 preliminary, else ready. Not an
individual score: the inspected video's own views are returned beside it.
rows with stale counters stay in the population; their views count as missing.
"""
from datetime import timedelta

from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance, round_summary
from .inspection import (STATES, bind, frozen_cell, inspection, inspection_parameters, instant, integer, iso,
                         membership, video_identity)

ID = 'video.lifetime_baseline.v1'
METHOD = 'current_lifetime_views_median_iqr_v1'
DEFINITION_STATES = frozenset({'known', 'mixed_definition_epochs', 'unknown_definition_epoch', 'no_known_values'})

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video', 'population', 'descriptive_analytics', (),
    'Current lifetime views of the whole population (median, P25, P75; the shared summary statistics and '
    'definition rule) beside one inspected video; sample-size status, no per-video score.',
    population='inspected_video_against_the_population')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    inspection_parameters(params)
    return ('video.id', 'video.views')


def status(sample):
    return 'insufficient_data' if sample < 4 else 'preliminary' if sample < 12 else 'ready'


def check(result):
    if type(result) is not dict or set(result) != {'video_id', 'video_type', 'member', 'frozen', 'conditions_applied',
                                                   'population', 'definition', 'views', 'target'}:
        raise ValueError()
    video_id, video_type = video_identity(result)
    reference, frozen = inspection(result)
    population, definition, views = result['population'], result['definition'], result['views']
    if type(population) is not int or population < 0 or type(result['conditions_applied']) is not bool \
            or type(definition) is not dict or definition.get('state') not in DEFINITION_STATES:
        raise ValueError()
    if type(definition.get('unknown_epoch')) is not int or definition['unknown_epoch'] < 0:
        raise ValueError()
    sample = 0 if views is None else views['known_count']
    if type(sample) is not int or not 0 <= sample <= population or (sample > 0) != (definition['state'] == 'known'):
        raise ValueError()
    stats = {name: round_summary(views[key]) if sample >= 4 else None
             for name, key in (('median', 'median'), ('p25', 'q1'), ('p75', 'q3'))}
    output = {'video_id': video_id, 'video_type': video_type, 'membership': membership(result), 'inspection': reference,
              'method': METHOD,
              'status': status(sample), 'sample_size': sample, 'missing_views': population - sample, **stats,
              'definition': {'state': definition['state'], 'epoch': definition['epoch'] if definition['state'] == 'known' else None,
                             # Known views left out because no recorded definition matches them.
                             'unknown_definition_epoch_count': definition['unknown_epoch']},
              'population_count': population,
              'reference': {'video_type': video_type, 'publication_period': 'all_time',
                            'query_conditions_applied': result['conditions_applied'], 'includes_inspected_video': True},
              'target': None}
    if frozen is not None and 'video.views' in frozen:  # The browsed (frozen) value beside the current sample.
        output['target'] = {'views': frozen_cell(frozen['video.views'])}
    elif result['member']:
        target = result['target']
        state = target['views_state']
        if state not in STATES or (state == 'known') != (target['views'] is not None):
            raise ValueError()
        clock = instant(target['checked_at']) if target['checked_at'] is not None else None
        output['target'] = {'views': {'value': str(integer(target['views'])) if state == 'known' else None, 'state': state,
                                      'observed_at': iso(clock), 'expires_at': iso(clock + timedelta(hours=696)) if clock else None}}
    return output


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), bind=bind)
