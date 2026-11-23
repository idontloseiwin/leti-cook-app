"""video.fingerprint.v1: the Library Overview fingerprint of one period.

The same statistics as video.summary.v1 (one SQL owner, summary.summary_ctes)
over the Overview population and period (overview.py), plus what the old
channel baseline gave the Fingerprint: uploads, excluded counts and the coverage
block. Views follow the definition rule a value without a recorded
definition is excluded and counted, never the whole metric.
"""
from ..registry import SOURCE_ID
from . import overview, summary
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'video.fingerprint.v1'
FIELDS = summary.SUMMARY_FIELDS + ('video.published_at',)
# Why a scanned row is not in the population: the admission stage names of
# research_sql.ADMISSION_STAGES (REJECTION_REASONS), which own the conditions.
REJECTIONS = ('identity', 'source_missing', 'source_mismatch', 'source_stale', 'source_unavailable',
              'source_unpublished', 'stream_incomplete')
STALE = ('stale_count', 'stale_counter_count', 'stale_metadata_count')

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video', 'population', 'descriptive_analytics', (),
    'Overview fingerprint of the public videos of one type published in a rolling period: exact summaries and fixed '
    'distributions, unknown values excluded per metric and counted, uploads and coverage.')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    overview.accept(params, ('period',))
    return FIELDS


def check(result):
    if set(result) != {'period', 'population', 'uploads', 'metrics'} or set(result['metrics']) != set(summary.METRICS):
        raise ValueError()
    overview.check_period(result['period'])
    population = overview.check_population(result['population'], ('scanned', 'rejected', *STALE))
    for key in ('scanned', *STALE):
        overview.count(population[key])
    rejected = population['rejected']
    if type(rejected) is not dict or set(rejected) - set(REJECTIONS):
        raise ValueError()
    for value in rejected.values():
        overview.count(value)
    population['rejected'] = {reason: rejected.get(reason, 0) for reason in REJECTIONS}
    if population['admitted'] + sum(population['rejected'].values()) > population['scanned']:
        raise ValueError()
    if population['stale_count'] > population['admitted']:
        raise ValueError()
    matched = population['matched']
    uploads = result['uploads']
    if set(uploads) != {'known_count', 'unknown_count'} or overview.count(uploads['known_count']) + overview.count(uploads['unknown_count']) != matched:
        raise ValueError()
    for name, metric in result['metrics'].items():
        known, missing, excluded = (overview.count(metric[key]) for key in ('known_count', 'missing_count', 'excluded_count'))
        if known + missing + excluded != matched or sum(overview.count(b['count']) for b in metric['buckets']) != known:
            raise ValueError()
        if (name == 'views') != ('unknown_definition_epoch_count' in metric):
            raise ValueError()
        summary.round_metric(metric)
    return result


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'))
