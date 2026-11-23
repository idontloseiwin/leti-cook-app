"""video.publication_rhythm.v1: hours, weekdays and their grid in the viewer's zone.

The Overview rhythm rings over the fingerprint's population and period:
real publication instants converted to the query's viewer zone time.timezone, ISO
weekdays (1 = Monday). Counts only; a share is count / known_count.
"""
from ..registry import SOURCE_ID
from . import overview
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'video.publication_rhythm.v1'

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video', 'calendar', 'descriptive_analytics', (),
    'Publication counts by local hour, ISO weekday and weekday x hour for the public videos of one type published '
    'in a rolling period, in the viewer time zone of the query; first and last publication.')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    overview.accept(params, ('period',))
    return ('video.published_at',)


def _dense(items, key, size, start):
    counts = [0] * size
    seen = set()
    for item in items:
        index = item[key] - start
        if set(item) != {key, 'count'} or type(item[key]) is not int or not 0 <= index < size or index in seen:
            raise ValueError()
        seen.add(index)
        counts[index] = overview.count(item['count'])
    return counts


def _peak(counts, key, start):
    best = max(counts)
    return None if best == 0 else {key: counts.index(best) + start, 'count': best}


def check(result):
    if set(result) != {'period', 'population', 'time_zone', 'known_count', 'first_published_at', 'last_published_at',
                       'hours', 'weekdays', 'cells'}:
        raise ValueError()
    overview.check_period(result['period'])
    overview.check_population(result['population'])
    known = overview.count(result['known_count'])
    hours = _dense(result['hours'], 'hour', 24, 0)
    weekdays = _dense(result['weekdays'], 'weekday', 7, 1)
    grid = [[0] * 24 for _ in range(7)]
    for cell in result['cells']:
        if set(cell) != {'weekday', 'hour', 'count'} or type(cell['weekday']) is not int or type(cell['hour']) is not int:
            raise ValueError()
        if not 1 <= cell['weekday'] <= 7 or not 0 <= cell['hour'] <= 23 or grid[cell['weekday'] - 1][cell['hour']]:
            raise ValueError()
        grid[cell['weekday'] - 1][cell['hour']] = overview.count(cell['count'])
    if not (sum(hours) == sum(weekdays) == sum(map(sum, grid)) == known <= result['population']['matched']):
        raise ValueError()
    if [sum(row[h] for row in grid) for h in range(24)] != hours or [sum(row) for row in grid] != weekdays:
        raise ValueError()
    if (known == 0) != (result['first_published_at'] is None) or (known == 0) != (result['last_published_at'] is None):
        raise ValueError()
    if known:
        result['first_published_at'] = overview.utc_iso(result['first_published_at'])
        result['last_published_at'] = overview.utc_iso(result['last_published_at'])
    del result['cells']
    result.update(hours=[{'hour': h, 'count': n} for h, n in enumerate(hours)],
                  weekdays=[{'weekday': d + 1, 'count': n} for d, n in enumerate(weekdays)],
                  grid=[{'weekday': d + 1, 'hours': row} for d, row in enumerate(grid)],
                  # The first local hour/weekday with the most publications, as the rings show it.
                  peak_hour=_peak(hours, 'hour', 0), peak_weekday=_peak(weekdays, 'weekday', 1))
    return result


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'))
