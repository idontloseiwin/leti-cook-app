"""video.publication_calendar.v1: local dates, hours and weekdays of real publications.

The zone is the query's viewer zone time.timezone, like every other
local reading. Studies saved with the block's own `time_zone` parameter keep
it: when present it names the zone (compatibility). Local times follow the
same local date and year rules as the catalogue. The local year must be in 1..9999.
"""
from zoneinfo import ZoneInfo

from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'video.publication_calendar.v1'

DEFINITION = OperationDefinition(ID,'public.videos.current.v1','video','calendar','descriptive_analytics',(),
    'Local calendar dates, hours and ISO weekdays from real publication timestamps, before pagination.')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    if not {'year'} <= set(params) <= {'year', 'time_zone'} or type(params['year']) is not int or not 1 <= params['year'] <= 9999:
        raise ValueError()
    if 'time_zone' in params:
        # Calendar parameter persisted in saved studies; checked.
        if type(params['time_zone']) is not str or len(params['time_zone']) > 64:
            raise ValueError()
        ZoneInfo(params['time_zone'])
    return ('video.published_at',)


def check(result):
    if len(result['days']) > 366 or len(result['hours']) > 24 or len(result['weekdays']) > 7:
        raise ValueError()
    return result


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), block='calendar')
