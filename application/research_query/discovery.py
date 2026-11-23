"""Machine-readable help from the execution registries; examples are not validators."""
from copy import deepcopy
from datetime import datetime, timezone
from .contracts import QueryRequest
from .registry import FIELDS, OPERATORS, SOURCE_ID, CHANNEL_SOURCE_ID
from .operations import OPERATIONS
from .operation_kinds import OPERATION_KINDS
from .validation import QueryRejected, capabilities

INSPECT = {'video_id': 'abcdefghijk', 'population': {'kind': 'query'}}
EXAMPLES = {
    'video.summary.v1': {},
    'video.publication_calendar.v1': {'year': 2026},
    'channel.observed_history.v1': {'metric': 'views', 'hours': 168, 'bucket_hours': 24},
    'video.observed_growth_28d_summary.v1': {},
    'video.static_details.v1': INSPECT, 'video.observed_history.v1': INSPECT,
    'video.launch_comparison.v1': INSPECT, 'video.lifetime_baseline.v1': INSPECT,
    'video.fingerprint.v1': {'period': 'max'},
    'video.publication_rhythm.v1': {'period': 'max'},
    'video.publication_years.v1': {}, 'video.content_breakdown.v1': {'period': 'max'},
    'channel.counter_change.v1': {}, 'channel.format_totals.v1': {},
    'video.facets.v1': {'population': {'kind': 'query'}}, 'video.view_pace_median.v1': {},
}


def describe(context, source=SOURCE_ID):
    result = capabilities(context, source)
    for field in result['fields']:
        definition = FIELDS[field['id']]
        field.update(unit=definition.unit, origin=definition.origin,
                     meaning=definition.normalization, empty_meaning=definition.empty_meaning)
    result['operator_definitions'] = [{'id': op.id, 'argument': op.argument, 'meaning': op.semantics}
                                    for op in OPERATORS.values() if op.id in context.allowed_operators]
    return result


def contracts(context, section='query', operation=None):
    if section == 'query':
        if operation is not None:
            raise QueryRejected('invalid_query')
        schema = QueryRequest.model_json_schema()
        if not context.allow_frozen:
            schema['properties']['consistency']['enum'] = ['current']
        return {'query_version': 1, 'schema': schema,
                'note': 'Obtain allowed fields/operators from capabilities. Numeric values are decimal strings; '
                        'unknown is not zero. One video type per query. Pages use the identical query and next_cursor.'}
    if section == 'operations':
        rows = []
        for identity, kind in OPERATION_KINDS.items():
            if identity not in context.allowed_operations or (operation is not None and identity != operation):
                continue
            example = deepcopy(EXAMPLES.get(identity))
            if example is not None and 'year' in example:
                example['year'] = datetime.now(timezone.utc).year
            definition = OPERATIONS[identity]
            rows.append({'id': identity, 'meaning': definition.semantics, 'policy': definition.policy,
                         'parameters_example': example,
                         'source': CHANNEL_SOURCE_ID if identity.startswith('channel.') else SOURCE_ID,
                         'scope_requirement': 'channel' if identity.startswith('channel.') else 'any_video_scope',
                         'output_grain': definition.output_grain})
        if operation is not None and not rows:
            raise QueryRejected('policy_denied')
        return {'operations': rows, 'note': 'Use the same full query as query_catalogue. Calculations apply to '
                'the entire matching population before pagination; never derive a population statistic '
                'from one page. Clear page.after for operations. Examples use query population; '
                'replace Inspector video_id. A null example means parameter help is not yet available.'}
    if section == 'canvas':
        if operation is not None:
            raise QueryRejected('invalid_query')
        from .graph import (CANVAS_NOTES_BYTES, GRAPH_V2_NODES, GRAPH_V2_OUTPUTS, MAX_DEFINITION, NODE_TYPES_V2,
                            OPERATION_TYPES, OUTPUT_TYPES)
        allowed = {kind for kind in NODE_TYPES_V2
                   if kind not in OPERATION_TYPES or OPERATION_TYPES[kind] in context.allowed_operations}
        example = {'version': 2, 'graph': {'version': 2, 'nodes': [
            {'id': 'source', 'type': 'source', 'parameters': {
                'source': {'id': SOURCE_ID}, 'scope': {'kind': 'library'},
                'video_types': ['short'], 'time': {'mode': 'current', 'timezone': 'UTC'}}},
            {'id': 'duration', 'type': 'filter', 'parameters': {'where': {
                'field': 'video.duration_seconds', 'op': 'lte', 'value': '60'}}},
            {'id': 'cards', 'type': 'gallery', 'parameters': {
                'select': ['video.id', 'video.title', 'video.thumbnail_url', 'video.views'], 'page_size': 24}}],
            'edges': [{'from': 'source', 'to': 'duration', 'output': 'rows', 'input': 'rows'},
                      {'from': 'duration', 'to': 'cards', 'output': 'rows', 'input': 'rows'}],
            'notes': '', 'layout': {'source': {'x': 0, 'y': 0}, 'duration': {'x': 300, 'y': 0}, 'cards': {'x': 600, 'y': 0}}}}
        return {'graph_version': 2, 'node_types': sorted(allowed), 'example': example,
                'result_types': sorted(OUTPUT_TYPES & allowed), 'max_nodes': GRAPH_V2_NODES,
                'consistency_modes': ['current', 'frozen'] if context.allow_frozen else ['current'],
                'max_results': GRAPH_V2_OUTPUTS,
                'max_definition_bytes': MAX_DEFINITION, 'max_notes_bytes': CANVAS_NOTES_BYTES,
                'note': 'Execute an exported Canvas definition. Source parameters carry source, scope, '
                        'video_types and time. The graph (canonical UTF-8 JSON) and its notes have byte limits. '
                        'Validate the graph before execution.'}
    raise QueryRejected('invalid_query')
