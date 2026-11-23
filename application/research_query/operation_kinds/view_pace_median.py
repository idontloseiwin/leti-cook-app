"""video.view_pace_median.v1: the median lifetime views per day of a population.

Home "Latest videos": the relative view pace of each video
is its video.lifetime_views_per_day divided by this median, taken over the
WHOLE window population (every matching row of the query, before pagination),
not over the rows the browser has loaded. The age is the Overview's
views/day age (publication to the counter observation) with the Home feed's
one-hour minimum (db/reads/research_sql.py pace_columns owns the per-row value).

Median of the known per-row values (each exact to six places): the middle
value, or the mean of the two middle values, rounded half to even to six
places. Rows whose value is not known stay in the population and are counted
by state. Views follow the definition rule (population_stats, the same
rule as Overview and the Inspector baseline): a value whose views have no
recorded definition is left out on its own and counted; two recorded
definitions in the population withhold the median (every known value is then
left out). `definition` names the state and the number left out. Lifetime
the data clocks of the population and the instant a row leaves a
relative publication window, never the 120 seconds of a page.
"""
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import re

from ..registry import SOURCE_ID
from .kind import OperationDefinition, OperationKind, population_provenance

ID = 'video.view_pace_median.v1'
FIELD = 'video.lifetime_views_per_day'
METHOD = 'median_of_lifetime_views_per_day_v1'
MISSING_STATES = ('unknown', 'expired', 'unavailable')
DEFINITION_STATES = ('known', 'mixed_definition_epochs', 'unknown_definition_epoch', 'no_known_values')

DEFINITION = OperationDefinition(ID, 'public.videos.current.v1', 'video', 'population', 'home_feed', (),
    'Median lifetime views per day (views*86400 / max(counter observation - publication, 1 hour)) of every '
    'matching row with a known value; rows without one are counted by state. Half-even to six places.')


def population(params):
    """`population`: absent or {"kind": "query"} (the query's own rows). A
    frozen reference ({"kind": "frozen", ...}) can be added here later
    without changing the meaning of the other parameters."""
    if set(params) - {'population'}:
        raise ValueError()
    value = params.get('population', {'kind': 'query'})
    if type(value) is not dict or value != {'kind': 'query'}:
        raise ValueError()


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    population(params)
    return (FIELD,)


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError()
    return value


def median_text(value):
    """Half-even six places, canonical text (no trailing zeros)."""
    if type(value) is not str or re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) is None:
        raise ValueError()
    with localcontext() as ctx:
        ctx.prec = 120
        number = Decimal(value).quantize(Decimal('0.000001'), rounding=ROUND_HALF_EVEN)
    text = format(number, 'f')
    text = text.rstrip('0').rstrip('.') if '.' in text else text
    if len(text.partition('.')[0]) > 20:
        raise ValueError()
    return text


def check(result):
    if type(result) is not dict or set(result) != {'field', 'method', 'population_count', 'known_count', 'missing',
                                                   'definition', 'median'}:
        raise ValueError()
    if result['field'] != FIELD or result['method'] != METHOD:
        raise ValueError()
    missing = result['missing']
    if type(missing) is not dict or set(missing) != set(MISSING_STATES):
        raise ValueError()
    definition = result['definition']
    if type(definition) is not dict or set(definition) != {'state', 'epoch', 'excluded_count'}:
        raise ValueError()
    state, excluded = definition['state'], _count(definition['excluded_count'])
    if state not in DEFINITION_STATES or (definition['epoch'] is not None and type(definition['epoch']) is not str):
        raise ValueError()
    total, known = _count(result['population_count']), _count(result['known_count'])
    if known + excluded + sum(_count(missing[state]) for state in MISSING_STATES) != total:
        raise ValueError()
    # Only one recorded definition lets values in; any other state leaves every known value out.
    if (state != 'known' and known) or (state == 'known') != (definition['epoch'] is not None):
        raise ValueError()
    median = result['median']
    if (median is None) != (known == 0):
        raise ValueError()
    return {**result, 'missing': {state: missing[state] for state in MISSING_STATES},
            'definition': {'state': state, 'epoch': definition['epoch'], 'excluded_count': excluded},
            'median': None if median is None else median_text(median)}


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'))
