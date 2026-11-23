"""video.facets.v1: Library filter counts over the whole matching population.

For each filter group, the rows that match every other condition (the query's
where and the other groups' conditions) are counted per preset, publication
year or category; a group's own condition never narrows its own counts.
The population is the query's authorized admitted rows:
a row with expired counters or metadata stays in and counts as unknown where
its value is needed. Presets come from the shared bucket registry, per video
type; years are counted in the request's time zone.

Parameters (exact keys):
- population: {"kind": "query"}, or {"kind": "frozen", "frozen_ref": <context.frozen_ref>}
  (the old session facets over the Library's frozen list, through the
  operation bind hook like the Inspector). The frozen result must hold exactly
  this request's rows: same source, scope, type, zone and coverage, and its where
  is this where with every group condition (the same conjuncts in any order),
  else cursor_context_changed. Its rows count with their frozen values: they match
  every condition, so each counts in every group. A row outside it counts with its
  current values only in a group whose own condition it fails while it passes all
  the others (a row the list would show if that group changed). A group needs its
  field frozen (published: video.published_year, or video.published_at counted by
  the canonical local-year rule), else invalid_query. matched_count is the frozen
  total; the answer lives no longer than the frozen result.
- groups: optional list of group names; default every group of STANDARD.
  "growth" (28-day growth statuses, fields) is offered on request only:
  it needs the field's own grant (CHANNEL_DERIVED_INSIGHTS_ENABLED).
- conditions: optional {group: expression}; an expression uses only that
  group's fields and is validated exactly like a query's where.
"""
from dataclasses import dataclass
import json
import re

from .. import growth
from ..registry import PRESET_GROUP_FIELDS as GROUP_FIELDS, PRESETS_VERSION as BUCKET_VERSION, SOURCE_ID
from .fingerprint import REJECTIONS
from .inspection import population_reference
from .kind import Binding, OperationDefinition, OperationKind, population_provenance

ID = 'video.facets.v1'

DEFINITION = OperationDefinition(ID, SOURCE_ID, 'video', 'facets', 'descriptive_analytics', (),
    'Counts per filter group over the full matching population; each group excludes only its own condition. '
    'Shared presets per video type, the three latest publication years in the request time zone, categories.')


@dataclass(frozen=True)
class FacetGroup:
    name: str
    kind: str  # buckets, years, categories or values (a closed vocabulary)
    field: str  # the value the group counts
    condition_fields: tuple[str, ...]  # fields its own condition may use
    values: tuple[str, ...] = ()  # kind values: every value, in display order


# A new group (for example 28-day growth) is one entry here and, for presets,
# one set in the shared presets file; its field's grant then applies to the facets request.
GROUPS = {group.name: group for group in (
    *(FacetGroup(name, 'buckets', field, (field,)) for name, field in GROUP_FIELDS.items()),
    FacetGroup('published', 'years', 'video.published_year',
               ('video.published_at', 'video.published_date', 'video.published_year')),
    FacetGroup('category', 'categories', 'video.category_id', ('video.category_id',)),
    # 28-day growth: counts per status; its own condition is any growth condition.
    FacetGroup('growth', 'values', growth.STATUS, growth.FIELDS, growth.STATUSES),
)}
# Requested when groups is absent: those every Library tab shows under the Baseline gate.
STANDARD = tuple(name for name in GROUPS if name != 'growth')
# One reason per row left out of the population: raw.rejection, named by the
# shared admission stages (research_sql.ADMISSION_STAGES, REJECTION_REASONS); a row
# a current check found not public is always source_unavailable. Their
# total is coverage.reasons.source_rows_unavailable, as in the Overview fingerprint.
EXCLUDED = REJECTIONS
# Group names and fields are written into SQL text: closed, quote-free vocabulary.
if any(not re.fullmatch(r'[a-z][a-z0-9_]*', g.name) or not re.fullmatch(r'[a-z][a-z0-9_.]*', g.field)
       for g in GROUPS.values()):
    raise RuntimeError('invalid_facet_groups')


def _expression_fields(node):
    if 'field' in node:
        return {node['field']}
    children = node['all'] if 'all' in node else node['any'] if 'any' in node else [node['not']]
    return set().union(*(_expression_fields(child) for child in children))


def _leaves(node):
    if 'field' in node:
        return 1
    children = node['all'] if 'all' in node else node['any'] if 'any' in node else [node['not']]
    return sum(_leaves(child) for child in children)


def _condition(group, node):
    """The group's own condition, validated and canonicalized like a where."""
    # Imported here: the validator imports the operation registry.
    from ..operations import OPERATIONS
    from ..validation import AccessContext, QueryRejected, canonical, validate
    if type(node) is not dict:
        raise ValueError()
    probe = {'query_version': 1, 'source': {'id': SOURCE_ID}, 'scope': {'kind': 'channel', 'id': 'UC' + '0' * 22},
             'video_types': ['long'], 'select': ['video.id'], 'where': node,
             'order_by': [{'field': 'video.id', 'direction': 'asc'}]}
    try:
        # Shape, operators and literals only; the request's own access context
        # checks every field again when the operation adds them to the query.
        plan = validate(canonical(probe), AccessContext('facets', 'definition', enabled=True,
                                                        allowed_operations=frozenset(OPERATIONS)))
    except QueryRejected as error:
        raise QueryRejected(error.code, 'parameters.conditions.' + group, limit=error.limit) from None
    where = json.loads(plan.encoded)['where']
    if not _expression_fields(where) <= set(GROUPS[group].condition_fields):
        raise ValueError()
    return where


def _parameters(params):
    if type(params) is not dict or not set(params) <= {'population', 'groups', 'conditions'}:
        raise ValueError()
    population_reference(params.get('population'))
    groups = params.get('groups', list(STANDARD))
    if (type(groups) is not list or not groups or len(set(map(str, groups))) != len(groups)
            or any(type(name) is not str or name not in GROUPS for name in groups)):
        raise ValueError()
    conditions = params.get('conditions', {})
    if type(conditions) is not dict or not set(conditions) <= set(groups):
        raise ValueError()
    normalized = {group: _condition(group, node) for group, node in conditions.items()}
    from ..contracts import MAX_LEAVES
    if sum(_leaves(node) for node in normalized.values()) > MAX_LEAVES:
        from ..validation import QueryRejected
        raise QueryRejected('query_budget_exceeded', 'parameters.conditions', limit='request')
    # Registry order, not request order: one canonical SQL text per selection.
    return [name for name in GROUPS if name in groups], normalized


def dependencies(params, source_id, scope_kind):
    if source_id != SOURCE_ID:
        raise ValueError()
    groups, conditions = _parameters(params)
    fields = [GROUPS[name].field for name in groups]
    for node in conditions.values():
        fields.extend(sorted(_expression_fields(node)))
    return tuple(dict.fromkeys(fields))


# What selects a query's rows, apart from its where (frozen_service.conditions_key).
_SELECTION = ('query_version', 'source', 'scope', 'video_types', 'time', 'observations', 'coverage')


def _conjuncts(node):
    if node is None:
        return []
    if 'all' in node:
        return [part for child in node['all'] for part in _conjuncts(child)]
    return [node]


def _selection(query, conditions=()):
    """The query's row selection with its where as a multiset of conjuncts."""
    from ..validation import canonical
    nodes = [*_conjuncts(query.get('where')), *(part for node in conditions for part in _conjuncts(node))]
    return {key: query.get(key) for key in _SELECTION}, sorted(canonical(node) for node in nodes)


def bind(service, plan, cancelled, deadline):
    """A frozen reference (operation_kinds.kind.Binding): the frozen rows and their group values."""
    groups, conditions = _parameters(plan.parameters)
    reference = plan.parameters['population']
    if reference['kind'] == 'query':
        return None
    from ..frozen_service import frozen_reference
    from ..validation import QueryRejected
    handle = frozen_reference(service, reference['frozen_ref'], cancelled=cancelled, deadline=deadline)
    session, request = handle.session, plan.query.request
    if (session.source_id, session.video_type, session.scope_kind, session.scope_id) != (
            request.source.id, request.video_types[0], request.scope.kind, request.scope.id):
        raise QueryRejected('cursor_context_changed')
    # The old session facets answered only for the conditions of their own browse session.
    if _selection(json.loads(session.plan.encoded)) != _selection(json.loads(plan.query.encoded), conditions.values()):
        raise QueryRejected('cursor_context_changed')
    columns = {field: index for index, field in enumerate(session.columns)}
    sources = {}
    for name in groups:
        field = GROUPS[name].field
        if field not in columns and name == 'published':
            field = 'video.published_at'
        if field not in columns:
            raise QueryRejected('invalid_query', 'parameters.groups')
        sources[name] = columns[field]
    counters, metadata = columns.get('video.views'), columns.get('video.title')
    ids, values, expired_counters, expired_metadata = [], {name: [] for name in groups}, 0, 0
    for encoded in session.rows:
        row_id, cells = json.loads(encoded)
        ids.append(row_id)
        for name, index in sources.items():
            state, value = cells[index][0], cells[index][1]
            values[name].append(value if state == 'known' else None)
        expired_counters += counters is not None and cells[counters][0] == 'expired'
        expired_metadata += metadata is not None and cells[metadata][0] == 'expired'
    if cancelled():  # A stopped caller gets the bare code; the operation checks its own deadline after the read.
        raise QueryRejected('query_budget_exceeded')
    instant = 'published' in sources and session.columns[sources['published']] == 'video.published_at'
    return Binding({'facet_frozen_ids': ids, 'facet_frozen_instant': instant,
                    'facet_frozen_expired_counters': expired_counters, 'facet_frozen_expired_metadata': expired_metadata,
                    **{'facet_frozen_' + name: values[name] for name in groups}},
                   valid_before=session.valid_before, finish=handle.require_current)


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError()
    return value


def check(result):
    if set(result) != {'population', 'time_zone', 'bucket_registry_version', 'groups'}:
        raise ValueError()
    population = result['population']
    if set(population) != {'kind', 'count', 'matched_count', 'expired_counters', 'expired_metadata', 'excluded'}:
        raise ValueError()
    if population['kind'] not in ('query', 'frozen') or set(population['excluded']) != set(EXCLUDED):
        raise ValueError()
    for value in (population['count'], population['matched_count'], population['expired_counters'],
                  population['expired_metadata'], *population['excluded'].values()):
        _count(value)
    if population['matched_count'] > population['count'] or type(result['time_zone']) is not str:
        raise ValueError()
    if result['bucket_registry_version'] != BUCKET_VERSION or type(result['groups']) is not dict:
        raise ValueError()
    for name, group in result['groups'].items():
        definition = GROUPS[name]
        if group['field'] != definition.field or _count(group['unknown_count']) > _count(group['count']):
            raise ValueError()
        known = group['count'] - group['unknown_count']
        if definition.kind == 'buckets':
            if set(group) != {'field', 'count', 'unknown_count', 'buckets'} or not group['buckets']:
                raise ValueError()
            if sum(_count(bucket['count']) for bucket in group['buckets']) != known:
                raise ValueError()  # A set covers every known value exactly once.
        elif definition.kind == 'years':
            if set(group) != {'field', 'count', 'unknown_count', 'years', 'older'} or len(group['years']) > 3:
                raise ValueError()
            years = [entry['year'] for entry in group['years']]
            if years != sorted(set(years), reverse=True) or sum(_count(entry['count']) for entry in group['years']) > known:
                raise ValueError()
            older = group['older']
            if older is not None and (len(years) != 3 or older['before_year'] != years[-1] or _count(older['count']) > known):
                raise ValueError()
        elif definition.kind == 'values':
            if set(group) != {'field', 'count', 'unknown_count', 'values'}:
                raise ValueError()
            if [entry['value'] for entry in group['values']] != list(definition.values):
                raise ValueError()
            if sum(_count(entry['count']) for entry in group['values']) != known:
                raise ValueError()
        else:
            if set(group) != {'field', 'count', 'unknown_count', 'categories'}:
                raise ValueError()
            if sum(_count(entry['count']) for entry in group['categories']) != known:
                raise ValueError()
            # Display labels from the shared category registry, ordered as Library listed them.
            from youtube_categories import youtube_video_category_name
            for entry in group['categories']:
                if type(entry['id']) is not str:
                    raise ValueError()
                entry['label'] = youtube_video_category_name(entry['id'])
            group['categories'].sort(key=lambda entry: (entry['label'], entry['id']))
    return result


KIND = OperationKind(DEFINITION, dependencies, check, population_provenance('calculated'), bind=bind)
