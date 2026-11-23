"""video.summary.v1: exact numeric summaries and fixed distributions of the population.

The statistics (definition rule, metric pairs, quartiles) come from
population_stats, the shared owner. summary_ctes and metrics_json assemble them
over any population relation; the Overview fingerprint (fingerprint.py) runs
the same text over its period rows.
"""
from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance, round_summary

ID = 'video.summary.v1'
SUMMARY_FIELDS = ('video.views', 'video.duration_seconds', 'video.likes',
                  'video.comments', 'video.title', 'video.tags')
METRICS = ('views', 'duration_seconds', 'likes', 'comments', 'title_length', 'tag_count')
ROUNDED = ('total', 'mean', 'median', 'q1', 'q3', 'min', 'max')

DEFINITION = OperationDefinition(ID,'public.videos.current.v1','video','population','descriptive_analytics',(),
    'Exact numeric summaries and fixed distributions over the full matching population; unknown values excluded per metric.')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    if params:
        raise ValueError()
    return SUMMARY_FIELDS


def round_metric(metric):
    for key in ROUNDED:
        metric[key] = round_summary(metric[key])
    return metric


def check(result):
    if set(result) != {'population', 'metrics'} or set(result['metrics']) != set(METRICS):
        raise ValueError()
    for metric in result['metrics'].values():
        round_metric(metric)
    return result


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), block='summary')
