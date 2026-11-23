"""video.observed_growth_28d_summary.v1: 28-day growth statuses of the whole population.

The growth response's coverage.growth counts, window and methods for the
matching population before paging. Each row's status and reason come from the
same columns as the growth fields (db/reads/research_growth.py); nothing is
recomputed here. A frozen population reference plugs into `population` later.
"""
from .. import growth
from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance

ID = growth.SUMMARY_ID

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video+observed_at+epoch', 'population', 'additional_analytics', (),
    'Counts of 28-day growth statuses and reasons (video.observed_growth_28d.v1) over the full matching population, '
    'with the window and methods of that rule.')

# Absent or {"kind": "query"}: the validated query's current population.
_POPULATIONS = ({}, {'population': {'kind': 'query'}})


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID or params not in _POPULATIONS:
        raise ValueError()
    return (growth.STATUS,)


def check(result):
    if type(result) is not dict or set(result) != {'population', 'statuses', 'reasons', 'time_zone'}:
        raise ValueError()
    population, statuses, reasons = result['population'], result['statuses'], result['reasons']
    if (type(population) is not int or population < 0 or type(statuses) is not dict or type(reasons) is not dict
            or type(result['time_zone']) is not str or not set(statuses) <= set(growth.STATUSES)):
        raise ValueError()
    known_reasons = {reason for values in growth.REASONS.values() for reason in values if reason is not None}
    if not set(reasons) <= known_reasons:
        raise ValueError()
    for value in (*statuses.values(), *reasons.values()):
        if type(value) is not int or value < 0:
            raise ValueError()
    counts = {status: statuses.get(status, 0) for status in growth.STATUSES}
    if sum(counts.values()) != population or sum(reasons.values()) != population - counts['ready']:
        raise ValueError()
    return {
        'population': population,
        # The coverage.growth fields: every matching row has one status.
        'requested_video_count': population, 'returned_growth_row_count': population,
        'calculable_count': counts['ready'] + counts['partial'],
        **{status + '_count': counts[status] for status in growth.STATUSES},
        'reasons': {reason: reasons[reason] for reason in sorted(reasons)},
        'time_zone': result['time_zone'],
        'window': dict(growth.WINDOW), 'methods': dict(growth.METHODS),
    }


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), block=None)
