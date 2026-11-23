"""Read the selected video's static fields, their states, provenance and expiry."""
from datetime import timedelta

from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance
from .inspection import STATES, bind, frozen_cell, inspection, inspection_parameters, iso, membership, video_identity

ID = 'video.static_details.v1'
FIELDS_READ = ('video.id', 'video.title', 'video.thumbnail_url', 'video.duration_seconds', 'video.views',
               'video.likes', 'video.comments', 'video.published_at', 'video.category_id', 'video.tags')
DERIVED = ('video.like_rate', 'video.comment_rate', 'video.title_length', 'video.tag_count')
# The inputs of each calculation: a frozen input never meets a current calculation.
DERIVED_INPUTS = {'video.like_rate': ('video.likes', 'video.views'), 'video.comment_rate': ('video.comments', 'video.views'),
                  'video.title_length': ('video.title',), 'video.tag_count': ('video.tags',)}
METHODS = {'rates': 'counter_times_100_divided_by_views_6dp_half_even', 'zero_views': 'unavailable',
           'title_length': 'unicode_code_points', 'tag_count': 'stored_array_length',
           'duration': 'youtube_duration_seconds_v1_integer', 'stale_values': 'unknown_or_expired_row_kept'}
# Source family of each cell: (clock column, expiry column or None for clock+696 hours).
FAMILIES = {'counters': ('checked_at', None), 'metadata': ('metadata_checked_at', None),
            'membership': ('identity_clock', 'identity_expiry'), 'description': ('description_checked_at', None)}
CELLS = {'video.id': ('video_id', 'membership'), 'video.title': ('title', 'metadata'),
         'video.thumbnail_url': ('thumbnail_url', 'membership'), 'video.duration_seconds': ('duration_seconds', 'metadata'),
         'video.views': ('views', 'counters'), 'video.likes': ('likes', 'counters'), 'video.comments': ('comments', 'counters'),
         'video.published_at': ('published_at', 'metadata'), 'video.category_id': ('category_id', 'metadata'),
         'video.tags': ('tags', 'metadata'), 'video.description': ('description', 'description'),
         'video.title_length': ('title_length', 'metadata'), 'video.tag_count': ('tag_count', 'metadata'),
         'video.like_rate': ('like_rate', 'counters'), 'video.comment_rate': ('comment_rate', 'counters')}
HOT_WINDOW = timedelta(hours=696)

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video', 'video_details', 'descriptive_analytics', (),
    'One inspected video of the population: current values with source clocks, category label and the static '
    'Leti Cook calculations (rates over positive views, title code points, tag count); no history.',
    population='one_inspected_video_of_the_population')


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    inspection_parameters(params, optional=('description',))
    if 'description' in params and type(params['description']) is not bool:
        raise ValueError()
    return FIELDS_READ + DERIVED + (('video.description',) if params.get('description') else ())


def _cell(row, field_id):
    column, family = CELLS[field_id]
    clock_column, expiry_column = FAMILIES[family]
    state = 'known' if field_id == 'video.id' else row[column + '_state']
    if state not in STATES:
        raise ValueError()
    observed = iso(row[clock_column])
    expires = iso(row[expiry_column]) if expiry_column else (
        iso(_instant(row[clock_column]) + HOT_WINDOW) if row[clock_column] is not None else None)
    value = row[column] if state == 'known' else None
    if state == 'known':
        if value is None or observed is None or expires is None:
            raise ValueError()
        if field_id == 'video.published_at':
            value = iso(value)
        elif field_id == 'video.tags':
            if type(value) is not list or any(type(tag) is not str for tag in value):
                raise ValueError()
        elif type(value) is not str:
            raise ValueError()
    return {'value': value, 'state': state, 'observed_at': observed, 'expires_at': expires}


def _instant(value):
    from .inspection import instant
    return instant(value)


def _category_label(category):
    """Resolve the category label from the managed reference."""
    if category['state'] != 'known' or not category['value'].strip():
        return None
    from youtube_categories import youtube_video_category_name
    return youtube_video_category_name(category['value'].strip())


UNAVAILABLE = {'value': None, 'state': 'unavailable', 'observed_at': None, 'expires_at': None}


def check(result):
    if type(result) is not dict or set(result) != {'video_id', 'video_type', 'member', 'frozen', 'description', 'row'} \
            or type(result['description']) is not bool:
        raise ValueError()
    video_id, video_type = video_identity(result)
    reference, frozen = inspection(result)
    output = {'video_id': video_id, 'video_type': video_type, 'membership': membership(result), 'inspection': reference,
              'cells': None, 'derived': None, 'category': None, 'clocks': None, 'methods': dict(METHODS)}
    if not result['member'] and frozen is None:
        return output
    row = result['row']
    if result['member'] and (type(row) is not dict or row.get('video_id') != video_id):
        raise ValueError()
    fields = (*FIELDS_READ, *(('video.description',) if result['description'] else ()))
    current = {field: _cell(row, field) for field in (*fields, *DERIVED)} if result['member'] else {}
    # A frozen reference shows the browsed values where its query selected them (the old frozen row).
    cells = {field: frozen_cell(frozen[field]) if frozen and field in frozen else current.get(field, dict(UNAVAILABLE))
             for field in fields}
    derived = {}
    for field in DERIVED:
        inputs = set(DERIVED_INPUTS[field])
        frozen_inputs = inputs & set(frozen) if frozen else set()
        if not frozen_inputs:
            derived[field] = current.get(field, dict(UNAVAILABLE))  # Current inputs: their current calculation.
        elif frozen_inputs == inputs and field in frozen:
            derived[field] = frozen_cell(frozen[field])
        else:
            derived[field] = dict(UNAVAILABLE)  # Frozen inputs without their calculation, or mixed: never mixed.
    output.update(cells=cells, derived=derived, category={'id': cells['video.category_id']['value'], 'label': _category_label(cells['video.category_id'])},
        clocks={'counters_checked_at': cells['video.views']['observed_at'],
                'metadata_checked_at': cells['video.title']['observed_at']})
    return output


DESCRIPTION = """,'description',description,'description_state',description_state,'description_checked_at',description_checked_at"""


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), bind=bind)
