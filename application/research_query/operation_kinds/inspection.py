"""Shared parts of the per-video Inspector operations: one inspected video of one population.

The Library Inspector panels (static details, observed history, launch comparison,
lifetime baseline) are kinds over a validated query population like every other
operation, for Long-form, Shorts and Streams alike. Not a kind itself: no KIND here.

Parameters every Inspector operation accepts (exact keys; ValueError otherwise):
  video_id    the inspected video, 11 URL-safe characters.
  population  how the inspected video and the inspection clock are chosen:
    {"kind": "query"}  no browse session (saved collections, agents): the video
        must belong to this query's current population; values and clock (as_of)
        are the statement's own. A video outside it gives the explicit state
        membership "not_in_population", never an HTTP error.
    {"kind": "frozen", "frozen_ref": "<context.frozen_ref of a frozen page>"}
        a frozen browse session. The reference is
        checked like a page (access, scope revision, lifetime); its result must
        have this query's source, scope and video type (else
        cursor_context_changed); the video must be one of its rows (else
        source_unavailable, frozen-query contract); the clock as_of is the frozen
        evaluated_at; the frozen row's cells are the inspected values where the
        frozen query selected them; the answer lives no longer than the frozen
        result and is fenced by require_current() after the read.

The query population stays the reference cohort/sample in both modes (the
Library sends the channel query without conditions for launch and baseline):
the frozen result holds only its matching rows, not the unfiltered source.
"""
from fractions import Fraction
import re

from .kind import Binding  # Package imports stay inside functions: validation imports this registry.

VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
FROZEN_REF = re.compile(r'[A-Za-z0-9_-]{1,4096}')
STATES = frozenset({'known', 'unknown', 'expired', 'unavailable'})
WINDOW_HOURS = 672  # 28 days as exact hours (ANALYTICS_WINDOW); never a DST-dependent day interval.
COUNTERS = ('video.views', 'video.likes', 'video.comments')
# Frozen cells an Inspector operation may present; others in the frozen row are not echoed.
FROZEN_FIELDS = frozenset({'video.id', 'video.title', 'video.thumbnail_url', 'video.duration_seconds', 'video.views',
                           'video.likes', 'video.comments', 'video.published_at', 'video.category_id', 'video.tags',
                           'video.description',
                           # The static calculations (registry fields) frozen next to their inputs.
                           'video.like_rate', 'video.comment_rate', 'video.title_length', 'video.tag_count'})
CURRENT = {'inspect_as_of': None, 'inspect_frozen': None, 'inspect_end_at': None, 'inspect_frozen_counters': False}


def inspection_parameters(params, optional=()):
    """Validated Inspector parameters; ValueError for any other key, type or value."""
    if type(params) is not dict or not {'video_id', 'population'} <= set(params) \
            or set(params) - {'video_id', 'population', *optional}:
        raise ValueError()
    if type(params['video_id']) is not str or VIDEO_ID.fullmatch(params['video_id']) is None:
        raise ValueError()
    population_reference(params['population'])
    return params


def population_reference(value):
    if type(value) is not dict or type(value.get('kind')) is not str:
        raise ValueError()
    if value['kind'] == 'query' and set(value) == {'kind'}:
        return value
    if value['kind'] == 'frozen' and set(value) == {'kind', 'frozen_ref'} and type(value['frozen_ref']) is str \
            and FROZEN_REF.fullmatch(value['frozen_ref']):
        return value
    raise ValueError()


def bind(service, plan, cancelled, deadline):
    """Resolve the population reference before the read slot (operation_kinds.kind.Binding)."""
    reference = plan.parameters['population']
    if reference['kind'] == 'query':
        return Binding(dict(CURRENT))
    from psycopg.types.json import Jsonb
    from ..frozen_service import frozen_reference
    from ..validation import QueryRejected
    handle = frozen_reference(service, reference['frozen_ref'], cancelled=cancelled, deadline=deadline)
    session, request = handle.session, plan.query.request
    if (session.source_id, session.video_type, session.scope_kind, session.scope_id) != (
            request.source.id, request.video_types[0], request.scope.kind, request.scope.id):
        raise QueryRejected('cursor_context_changed')
    member = handle.member(plan.parameters['video_id'])  # source_unavailable when not one of its rows
    cells = {field: cell for field, cell in member.row['cells'].items() if field in FROZEN_FIELDS}
    counters = all(field in cells for field in COUNTERS)
    frozen = {'evaluated_at': iso(member.evaluated_at), 'valid_before': iso(member.valid_before),
              'position': member.position, 'total_count': session.total_count, 'cells': cells}
    return Binding({'inspect_as_of': member.evaluated_at, 'inspect_frozen': Jsonb(frozen),
                    'inspect_end_at': cells['video.views']['observed_at'] if counters else None,
                    'inspect_frozen_counters': counters},
                   valid_before=member.valid_before, finish=handle.require_current)


def bind_target(plan, params):
    """SQL parameters of the inspected video and its bound reference (current defaults)."""
    params['inspect_video_id'] = plan.parameters['video_id']
    params.update(plan.binding.parameters if plan.binding is not None else CURRENT)
    return params


def inspection(result):
    """The result's reference: how the video, its values and the clock were chosen."""
    frozen = result.get('frozen')
    if frozen is None:
        return {'mode': 'query'}, None
    if type(frozen) is not dict or type(frozen.get('cells')) is not dict or type(frozen.get('position')) is not int:
        raise ValueError()
    return {'mode': 'frozen', 'evaluated_at': iso(frozen['evaluated_at']), 'valid_before': iso(frozen['valid_before']),
            'position': frozen['position'], 'total_count': frozen['total_count'],
            'frozen_fields': sorted(frozen['cells'])}, frozen['cells']


def frozen_cell(cell):
    """A frozen page cell in the Inspector cell shape (value, state, clocks)."""
    if type(cell) is not dict or cell.get('state') not in STATES:
        raise ValueError()
    return {'value': cell['value'], 'state': cell['state'], 'observed_at': cell['observed_at'], 'expires_at': cell['expires_at']}


def instant(value):
    """A wire/database instant as an aware UTC datetime (service owns the format)."""
    from ..service import instant as service_instant
    return service_instant(value)


def iso(value):
    from ..service import iso as service_iso
    return None if value is None else service_iso(value)


def membership(result):
    if type(result.get('member')) is not bool:
        raise ValueError()
    return 'member' if result['member'] else 'not_in_population'


def video_identity(result):
    if type(result.get('video_id')) is not str or VIDEO_ID.fullmatch(result['video_id']) is None \
            or result.get('video_type') not in ('long', 'short', 'stream'):
        raise ValueError()
    return result['video_id'], result['video_type']


def integer(value):
    """An exact non-negative integer from SQL decimal text, or None."""
    if value is None:
        return None
    if type(value) is not str or not value.isascii() or not value.isdecimal():
        raise ValueError()
    return int(value)


def microseconds(start, end):
    elapsed = instant(end) - instant(start)
    return (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds


def seconds_text(value):
    """Exact seconds from microseconds ('3600', '3600.5')."""
    whole, fraction = divmod(value, 1000000)
    return str(whole) + (f".{fraction:06d}".rstrip("0") if fraction else "")


def per_day(delta, elapsed_microseconds):
    """Signed delta per exact elapsed time times one day; 2 places, ties to even, no floats."""
    hundredths = round(Fraction(delta * 86_400 * 1_000_000, elapsed_microseconds) * 100)
    whole, fraction = divmod(abs(hundredths), 100)
    sign = "-" if hundredths < 0 else ""
    return f"{sign}{whole}" + (f".{fraction:02d}".rstrip("0") if fraction else "")


def decimal_text(value, places):
    """An exact rational rounded half-even to `places` decimals, without trailing zeros."""
    scaled = round(Fraction(value) * 10 ** places)
    sign = "-" if scaled < 0 else ""
    whole, fraction = divmod(abs(scaled), 10 ** places)
    return sign + str(whole) + (f".{fraction:0{places}d}".rstrip("0") if fraction else "")
