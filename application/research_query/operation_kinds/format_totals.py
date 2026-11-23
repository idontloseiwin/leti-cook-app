"""Sum current views of all publicly listed videos of one format.

The typed list must be complete and current. Confirmed non-public videos leave
the sum. Other missing sources make the total incomplete. Expired clocks make
it stale. A stream's actual end must precede the evaluation and both source
clocks. The result expires with the earliest contributing source."""
from ..registry import CHANNEL_SOURCE_ID
from . import overview
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'channel.format_totals.v1'
FORMATS = ('long', 'short', 'stream')
LIST_LIMIT = 1_000_000  # Upper bound on the scanned list.
CLASSES = ('stale_inventory', 'missing_video_row', 'availability_withheld', 'unavailable_excluded',
           'stream_completion_withheld', 'stale_counter', 'stale_metadata', 'unknown_view', 'known')
STALE = ('stale_inventory', 'stale_counter', 'stale_metadata')
# Coverage reasons for typed playlist entries.
GAPS = {'stale_inventory': 'stale_inventory_count', 'missing_video_row': 'missing_video_row_count',
        'availability_withheld': 'availability_withheld_count', 'stream_completion_withheld': 'stream_completion_withheld',
        'stale_counter': 'stale_counter_count', 'stale_metadata': 'stale_metadata_count', 'unknown_view': 'unknown_view_count'}

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'channel', 'channel', 'channel_header', (),
    'Sum of current views of the listed public Long, Shorts and Streams of one channel, each only when its list '
    'is complete and every listed public video has a current known counter; completed Streams only.',
    population='authorized_single_channel')


def dependencies(params, source_id, scope_kind):
    if source_id != CHANNEL_SOURCE_ID or scope_kind != 'channel' or params:
        raise ValueError()
    return ()


def _typed_state(state):
    """Coverage gate of the selected format; None when the list is usable."""
    status = state['inventory_status']
    if status not in ('completed', 'playlist_not_found'):
        return {'status': 'incomplete' if status in ('running', 'error') else 'not_collected', 'reason_code': 'inventory_not_completed'}
    if not state['current']:
        return {'status': 'stale', 'reason_code': 'inventory_stale'}
    if status == 'playlist_not_found':
        return {'status': 'absent', 'reason_code': 'playlist_not_found', 'expires_at': state['expires_at']}
    return None


def _format(counts, state):
    empty = dict.fromkeys(('status', 'views', 'reason_code', 'scanned_inventory_count', 'canonical_inventory_count',
                           'known_view_count', 'missing_view_count', 'excluded', 'expires_at'))
    gate = _typed_state(state)
    if gate is not None:
        return {**empty, **gate}
    classes = {name: overview.count(counts['classes'][name]) for name in CLASSES}
    scanned, canonical = overview.count(counts['scanned']), overview.count(counts['canonical'])
    if sum(classes.values()) != scanned:
        raise ValueError()
    excluded = {wire: classes[name] for name, wire in GAPS.items()}
    scope_complete = canonical == scanned and scanned <= LIST_LIMIT
    missing = sum(excluded.values())
    stale_only = missing > 0 and missing == sum(classes[name] for name in STALE)
    complete = scope_complete and missing == 0
    views = counts['views'] if complete else None
    if complete and (classes['known'] and (type(views) is not str or not views.isdigit()) or not classes['known'] and views is not None):
        raise ValueError()
    return {
        'status': 'ready' if complete else 'stale' if scope_complete and stale_only else 'incomplete',
        'views': (views or '0') if complete else None,
        'reason_code': None if canonical == scanned else 'inventory_sources_incomplete',
        'scanned_inventory_count': scanned, 'canonical_inventory_count': canonical, 'known_view_count': classes['known'],
        'missing_view_count': missing,
        'excluded': dict(excluded, unavailable_excluded_count=classes['unavailable_excluded'], type_resolution_withheld=0),
        'expires_at': None if counts['expires_at'] is None else overview.utc_iso(counts['expires_at'])}


def check(result):
    if set(result) != set(FORMATS):
        raise ValueError()
    formats = {}
    for kind in FORMATS:
        item = result[kind]
        if set(item) != {'state', 'counts'} or set(item['counts']) != {'scanned', 'canonical', 'classes', 'views', 'expires_at'}:
            raise ValueError()
        state = item['state']
        if set(state) != {'inventory_status', 'current', 'expires_at'} or type(state['current']) is not bool:
            raise ValueError()
        if state['expires_at'] is not None:
            state['expires_at'] = overview.utc_iso(state['expires_at'])
        if set(item['counts']['classes']) != set(CLASSES):
            raise ValueError()
        formats[kind] = _format(item['counts'], state)
    return dict(list_limit=LIST_LIMIT, formats=formats)


def turned_off(result, formats):
    """The header's total of a format turned off in config.ini is not shown."""
    totals = dict(result['formats'])
    for kind in formats:
        totals[kind] = {**dict.fromkeys(totals[kind]), 'status': 'turned_off', 'reason_code': 'format_turned_off'}
    return {**result, 'formats': totals}


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), turned_off=turned_off)
