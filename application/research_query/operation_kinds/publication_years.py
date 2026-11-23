"""video.publication_years.v1: the Overview publication calendar and its years.

The same population as the fingerprint over the whole lifetime: the
calendar does not follow the Overview period. Dates and years are the
canonical video.published_date / video.published_year of each row: local
to the query's viewer zone time.timezone, known only for a valid
publication instant whose local year is 1..9999. The default year is the
current year in that zone and is always offered; the days are those of the
requested year, or of the default year when none is requested.
"""
from ..registry import SOURCE_ID
from . import overview
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'video.publication_years.v1'

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video', 'calendar', 'descriptive_analytics', (),
    'Local publication years of the public videos of one type, with the daily counts of one year (default: the '
    'current year) in the viewer time zone of the query, independent of any period.')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    overview.accept(params, (), ('year',))
    if 'year' in params and (type(params['year']) is not int or not 1 <= params['year'] <= 9999):
        raise ValueError()
    # The local date and year are the registry fields' own columns (one rule).
    return ('video.published_at', 'video.published_date', 'video.published_year')


def check(result):
    if set(result) != {'time_zone', 'population', 'known_count', 'default_year', 'year', 'available_years', 'days'}:
        raise ValueError()
    known = overview.count(result['known_count'])
    if known > overview.count(result['population']):
        raise ValueError()
    years = result['available_years']
    if (type(years) is not list or not years or any(type(y) is not int or not 1 <= y <= 9999 for y in years)
            or years != sorted(set(years), reverse=True) or result['default_year'] not in years):
        raise ValueError()
    if type(result['year']) is not int or not 1 <= result['year'] <= 9999 or len(result['days']) > 366:
        raise ValueError()
    total, previous = 0, ''
    for day in result['days']:
        if set(day) != {'date', 'count'} or type(day['date']) is not str or day['date'] <= previous:
            raise ValueError()
        if day['date'][:4] != f"{result['year']:04d}" or overview.count(day['count']) == 0:
            raise ValueError()
        previous, total = day['date'], total + day['count']
    if total > known:
        raise ValueError()
    return result


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'))
