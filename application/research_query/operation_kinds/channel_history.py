"""channel.observed_history.v1: the last real retained channel observation per bucket."""
from ..registry import CHANNEL_SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'channel.observed_history.v1'

DEFINITION = OperationDefinition(ID,'public.channel.observations.v1','channel+observed_at','channel_series','channel_history',(),
    'Last real retained channel observation per time bucket; no interpolation, video epoch or computed rate.',population='authorized_single_channel')


def dependencies(params, source_id, scope_kind):
    if source_id != CHANNEL_SOURCE_ID or scope_kind != 'channel':
        raise ValueError()
    if set(params) != {'metric', 'hours', 'bucket_hours'} or params['metric'] not in {'views', 'subscribers'}:
        raise ValueError()
    if type(params['hours']) is not int or not 1 <= params['hours'] <= 696 or type(params['bucket_hours']) is not int or params['bucket_hours'] not in {1, 6, 24}:
        raise ValueError()
    return ('channel.' + params['metric'],)


def check(result):
    if len(result['points']) > 697:
        raise ValueError()
    return result


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('youtube_reported'), block='history')
