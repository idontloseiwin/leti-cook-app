"""Versioned research composition. Ports carry query definitions, never SQL.

The first graph has one current source, typed query-transform edges and up to
four result blocks. Result blocks cannot feed another query or calculation.
Layout and user notes are durable user work; fetched rows are never a definition.

A run may name options next to the definition (`run`, see RunOptions). They
are an execution argument only: never part of the definition, its fingerprint
or anything saved.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re

from .aggregate import validate_operation, operation_dependencies
from .contracts import MAX_CURSOR_CHARS, QueryRequest
from types import SimpleNamespace
from .operation_kinds import OPERATION_KINDS
from .operations import OPERATIONS
from .registry import CHANNEL_SOURCE_ID
from .validation import AccessContext, QueryRejected, canonical, validate, _json_budget, _pairs, _reject_constant

MAX_DEFINITION = 16384
# Notes of a study graph (version 1) and of a canvas (version 2), in UTF-8 bytes. A canvas's notes
# count toward its MAX_DEFINITION too.
STUDY_NOTES_BYTES = 4096
CANVAS_NOTES_BYTES = 8192


class DefinitionTooLarge(ValueError):
    """A canvas over a byte budget, never reported as an invalid definition: path is
    'graph.notes' (the notes) or 'graph' (the whole definition)."""
    def __init__(self, path):
        super().__init__('research_definition_too_large')
        self.path = path


# Each registered operation that names a block is one result block type.
OPERATION_TYPES = {kind.block:kind.id for kind in OPERATION_KINDS.values() if kind.block is not None}
OUTPUT_TYPES = frozenset({'table','gallery',*OPERATION_TYPES})
ROW_TYPES = frozenset({'table','gallery'})
NODE_TYPES = frozenset({'source','filter','selection',*OUTPUT_TYPES})
if {'source','filter','selection','table','gallery','sort'} & set(OPERATION_TYPES):
    raise RuntimeError('invalid_operation_block')  # A block may not reuse a structural type.
ID = re.compile(r'[a-z][a-z0-9_]{0,31}\Z')

# Canvas graph (version 2). Every source node carries its own source, scope,
# type and zone, so one canvas holds independent sources and branches; a sort
# node orders its branch; a table or gallery block owns its columns and page
# size. A node without a path to a source is a kept draft: saved, never run.
GRAPH_V2_NODES = 16
GRAPH_V2_OUTPUTS = 6
NODE_TYPES_V2 = frozenset({'source','filter','sort','selection',*OUTPUT_TYPES})
# What a node's output edge carries: the matching rows of its branch.
ROWS = 'rows'
_IDENTITY = {CHANNEL_SOURCE_ID:'channel.id'}
_DEFAULT_ORDER = {CHANNEL_SOURCE_ID:[{'field':'channel.title','direction':'asc'}]}
VIDEO_IDENTITY = 'video.id'
VIDEO_DEFAULT_ORDER = [{'field':'video.published_at','direction':'desc'}]


@dataclass(frozen=True)
class RunOptions:
    """How one run reads its rows.

    current: every block is a current read (the same as no options).
    frozen: table and gallery blocks fed directly by the source show one fixed
    list of the source rows: frozen_ref names a live frozen result of the same
    query (Library's list), otherwise the run fixes a new one. Calculations
    and blocks behind a selection or a filter condition stay current reads.
    """
    consistency: str
    frozen_ref: str | None = None


@dataclass(frozen=True)
class GraphPlan:
    definition: bytes
    query_plans: tuple
    order: tuple[str,...]
    fingerprint: str
    context_fingerprint: str
    run: RunOptions | None = None  # None: no run options were sent.
    version: int = 1  # The graph version: 1 (one source, study query) or 2 (canvas).
    drafts: frozenset = frozenset()  # Version 2 nodes without a path to a source: never run.


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError()


def normalize_graph(value, query, context=None, *, allow_unknown=False):
    """Definition validation and execution admission are separate passes."""
    executing = context is not None
    context = context or AccessContext('definition','v1',enabled=True,allowed_operations=frozenset(OPERATIONS))
    try:
        # A definition is always current. A run reads a fixed list only
        # through its run options (RunOptions), never through saved work.
        if type(query) is not dict or query.get('consistency','current')!='current':
            raise ValueError()
        _exact(value, ('version','nodes','edges','notes','layout'))
        if type(value['version']) is not int or value['version'] != 1 or len(canonical(value)) > MAX_DEFINITION:
            raise ValueError()
        nodes, edges = value['nodes'],value['edges']
        if type(nodes) is not list or not 2 <= len(nodes) <= 12 or type(edges) is not list or len(edges)>11:
            raise ValueError()
        if type(value['notes']) is not str or len(value['notes'].encode('utf-8'))>STUDY_NOTES_BYTES or '\x00' in value['notes']:
            raise ValueError()
        lookup={}; parents={}; plans={}; order=[]
        for node in nodes:
            _exact(node,('id','type','parameters'))
            if type(node['id']) is not str or not ID.fullmatch(node['id']) or node['id'] in lookup or node['type'] not in NODE_TYPES or type(node['parameters']) is not dict:
                raise ValueError()
            lookup[node['id']]=node
        roots=[n['id'] for n in nodes if n['type']=='source']
        if len(roots)!=1 or not 1<=sum(n['type'] in OUTPUT_TYPES for n in nodes)<=4:
            raise ValueError()
        for edge in edges:
            _exact(edge,('from','to','output','input'))
            if edge['from'] not in lookup or edge['to'] not in lookup or edge['to'] in parents or edge['output']!='query' or edge['input']!='query':
                raise ValueError()
            if lookup[edge['from']]['type'] in OUTPUT_TYPES or lookup[edge['to']]['type']=='source':
                raise ValueError()
            parents[edge['to']]=edge['from']
        if set(parents)!=set(lookup)-set(roots):
            raise ValueError()
        layout=value['layout']
        if type(layout) is not dict or set(layout)!=set(lookup):
            raise ValueError()
        for position in layout.values():
            _exact(position,('x','y'))
            if any(type(position[k]) is not int or not -10000<=position[k]<=10000 for k in ('x','y')):
                raise ValueError()
        for _ in range(len(nodes)):
            for identity,node in lookup.items():
                if identity in plans or identity in parents and parents[identity] not in plans:
                    continue
                kind,params=node['type'],node['parameters']
                q=deepcopy(query if kind=='source' else json.loads(plans[parents[identity]].encoded))
                if q.get('page',{}).get('after') is not None:
                    raise ValueError()
                q['page']={'size':q.get('page',{}).get('size',25)}
                if kind in {'source','table','gallery','summary'} and params:
                    raise ValueError()
                if kind=='filter':
                    _exact(params,('where',))
                    if params['where'] is not None:
                        q['where']={'all':[q['where'],params['where']]} if q.get('where') else params['where']
                if kind=='selection':
                    _exact(params,('mode','ids'))
                    if params['mode'] not in {'include','exclude'} or type(params['ids']) is not list or not 1<=len(params['ids'])<=1000 or len(set(params['ids']))!=len(params['ids']):
                        raise ValueError()
                    leaf={'field':'channel.id' if q['source']['id']==CHANNEL_SOURCE_ID else 'video.id','op':'in','value':params['ids']}
                    if params['mode']=='exclude':leaf={'not':leaf}
                    q['where']={'all':[q['where'],leaf]} if q.get('where') else leaf
                if kind in OPERATION_TYPES:
                    operation_dependencies(OPERATION_TYPES[kind],params,q['source']['id'],q['scope']['kind'])
                try:
                    plan=validate(canonical(q),context)
                    if kind in OPERATION_TYPES:
                        validate_operation(canonical(dict(version=1,operation=OPERATION_TYPES[kind],query=json.loads(plan.encoded),parameters=params)),context)
                except QueryRejected as error:
                    if executing or not allow_unknown or error.code!='unsupported_field':raise
                    # Preserve structurally valid user work with removed/future
                    # fields. It stays inert: execution never uses this path.
                    parsed=QueryRequest.model_validate(q)
                    plan=SimpleNamespace(encoded=canonical(parsed.model_dump(mode='json',by_alias=True,exclude_none=True)))
                plans[identity]=plan;order.append(identity)
        if len(plans)!=len(nodes):
            raise ValueError()  # Cycle, disconnected block or invalid port.
        return deepcopy(value),plans,tuple(order)
    except QueryRejected:
        if executing:raise
        raise ValueError('invalid_research_definition') from None
    except (ValueError,TypeError,KeyError,OverflowError,UnicodeError):
        raise ValueError('invalid_research_definition') from None


def _source_query(params):
    """A source node's own query: its source, scope, type and zone; identity column, default order.
    Optional, as a study's query had them: the completeness rule (coverage) and the observed-change
    window (observations); the query's defaults apply without them."""
    keys=set(params) if type(params) is dict else set()
    if not {'source','scope','video_types','time'}<=keys or keys-{'source','scope','video_types','time','coverage','observations'}:
        raise ValueError()
    source=params['source']
    if type(source) is not dict or type(source.get('id')) is not str:
        raise ValueError()
    channels=source['id']==CHANNEL_SOURCE_ID
    if (params['video_types'] is None)!=channels:
        raise ValueError()  # Videos name exactly one type list; channels none.
    query={'query_version':1,'source':source,'scope':params['scope'],'time':params['time'],
           'select':[_IDENTITY.get(source['id'],VIDEO_IDENTITY)],
           'order_by':deepcopy(_DEFAULT_ORDER.get(source['id'],VIDEO_DEFAULT_ORDER)),'page':{'size':25}}
    if not channels:
        query['video_types']=params['video_types']
    for key in ('coverage','observations'):
        if key in params:
            query[key]=params[key]  # Checked by the query contract (channels have no observations).
    return query


def _transform_v2(kind,params,query):
    """The query a node passes on (or reads), from its input's query. Sort replaces the order;
    filter and selection add a condition; a table or gallery sets its own columns and page."""
    q=deepcopy(query)
    if kind=='filter':
        _exact(params,('where',))
        if params['where'] is not None:
            q['where']={'all':[q['where'],params['where']]} if q.get('where') else params['where']
    elif kind=='sort':
        _exact(params,('order_by',))
        if type(params['order_by']) is not list or not 1<=len(params['order_by'])<=3:
            raise ValueError()
        q['order_by']=deepcopy(params['order_by'])
    elif kind=='selection':
        _exact(params,('mode','ids'))
        if params['mode'] not in {'include','exclude'} or type(params['ids']) is not list or not 1<=len(params['ids'])<=1000 or len(set(params['ids']))!=len(params['ids']):
            raise ValueError()
        leaf={'field':'channel.id' if q['source']['id']==CHANNEL_SOURCE_ID else 'video.id','op':'in','value':params['ids']}
        if params['mode']=='exclude':leaf={'not':leaf}
        q['where']={'all':[q['where'],leaf]} if q.get('where') else leaf
    elif kind in ROW_TYPES:
        _exact(params,('select','page_size'))
        if type(params['select']) is not list or not 1<=len(params['select'])<=50 or type(params['page_size']) is not int or not 1<=params['page_size']<=100:
            raise ValueError()
        identity=_IDENTITY.get(q['source']['id'],VIDEO_IDENTITY)
        q['select']=[identity,*[field for field in params['select'] if field!=identity]][:50]
        q['page']={'size':params['page_size']}
    elif kind in OPERATION_TYPES:
        operation_dependencies(OPERATION_TYPES[kind],params,q['source']['id'],q['scope']['kind'])
    return q


def normalize_graph_v2(value, context=None, *, allow_unknown=False):
    """A canvas graph: (definition, plans of every node on a path from a source, order, kept drafts).

    Definition checks (shape, ports, budgets, acyclicity) always apply; a node
    without a path to a source is a draft and has no plan. With an access
    context the plans are execution plans (fields, grants, operations)."""
    executing = context is not None
    context = context or AccessContext('definition','v2',enabled=True,allowed_operations=frozenset(OPERATIONS))
    try:
        _exact(value,('version','nodes','edges','notes','layout'))
        if type(value['version']) is not int or value['version']!=2 or type(value['notes']) is not str or '\x00' in value['notes']:
            raise ValueError()
        # Size is its own refusal, so a person (or an agent) learns what to shorten: the notes first.
        if len(value['notes'].encode('utf-8'))>CANVAS_NOTES_BYTES:
            raise DefinitionTooLarge('graph.notes')
        if len(canonical(value))>MAX_DEFINITION:
            raise DefinitionTooLarge('graph')
        nodes,edges=value['nodes'],value['edges']
        if type(nodes) is not list or not 1<=len(nodes)<=GRAPH_V2_NODES or type(edges) is not list or len(edges)>GRAPH_V2_NODES:
            raise ValueError()
        lookup={};parents={}
        for node in nodes:
            _exact(node,('id','type','parameters'))
            if type(node['id']) is not str or not ID.fullmatch(node['id']) or node['id'] in lookup or node['type'] not in NODE_TYPES_V2 or type(node['parameters']) is not dict:
                raise ValueError()
            lookup[node['id']]=node
        if not any(n['type']=='source' for n in nodes) or sum(n['type'] in OUTPUT_TYPES for n in nodes)>GRAPH_V2_OUTPUTS:
            raise ValueError()
        for edge in edges:
            _exact(edge,('from','to','output','input'))
            if (edge['from'] not in lookup or edge['to'] not in lookup or edge['from']==edge['to'] or edge['to'] in parents
                    or edge['output']!=ROWS or edge['input']!=ROWS):
                raise ValueError()
            if lookup[edge['from']]['type'] in OUTPUT_TYPES or lookup[edge['to']]['type']=='source':
                raise ValueError()  # A result block feeds nothing; a source has no input.
            parents[edge['to']]=edge['from']
        layout=value['layout']
        if type(layout) is not dict or set(layout)!=set(lookup):
            raise ValueError()
        for position in layout.values():
            _exact(position,('x','y'))
            if any(type(position[k]) is not int or not -10000<=position[k]<=10000 for k in ('x','y')):
                raise ValueError()
        for identity in lookup:  # Acyclic: every chain of inputs ends.
            seen,current={identity},identity
            while current in parents:
                current=parents[current]
                if current in seen:raise ValueError()
                seen.add(current)
        queries={};plans={};order=[]
        for _ in range(len(nodes)):
            for identity,node in lookup.items():
                if identity in plans:
                    continue
                kind,params=node['type'],node['parameters']
                if kind=='source':
                    q=_source_query(params)
                elif parents.get(identity) in queries:
                    q=_transform_v2(kind,params,queries[parents[identity]])
                else:
                    continue
                try:
                    plan=validate(canonical(q),context)
                    if kind in OPERATION_TYPES:
                        validate_operation(canonical(dict(version=1,operation=OPERATION_TYPES[kind],query=json.loads(plan.encoded),parameters=params)),context)
                except QueryRejected as error:
                    if executing or not allow_unknown or error.code!='unsupported_field':raise
                    # Kept user work with a removed or future field stays inert (as in version 1).
                    parsed=QueryRequest.model_validate(q)
                    plan=SimpleNamespace(encoded=canonical(parsed.model_dump(mode='json',by_alias=True,exclude_none=True)))
                queries[identity]=json.loads(plan.encoded);plans[identity]=plan;order.append(identity)
        drafts=frozenset(lookup)-set(plans)
        return deepcopy(value),plans,tuple(order),drafts
    except DefinitionTooLarge:
        raise
    except QueryRejected:
        if executing:raise
        raise ValueError('invalid_research_definition') from None
    except (ValueError,TypeError,KeyError,OverflowError,UnicodeError):
        raise ValueError('invalid_research_definition') from None


def run_options(value):
    """Exact keys {consistency} or {consistency, frozen_ref}; the reference only for frozen.

    The reference is opaque here: an unknown, expired or foreign one is refused
    when the run resolves it (cursor_expired / cursor_context_changed)."""
    if type(value) is not dict or set(value) not in ({'consistency'},{'consistency','frozen_ref'}):
        raise ValueError()
    consistency=value['consistency']
    if type(consistency) is not str or consistency not in {'current','frozen'}:
        raise ValueError()
    if 'frozen_ref' not in value:
        return RunOptions(consistency)
    reference=value['frozen_ref']
    if consistency!='frozen' or type(reference) is not str or not 1<=len(reference)<=MAX_CURSOR_CHARS:
        raise ValueError()
    return RunOptions(consistency,reference)


def validate_graph(raw,context):
    if not isinstance(raw,bytes) or len(raw)>65536:
        raise QueryRejected('query_budget_exceeded',limit='request')
    _json_budget(raw)
    try:
        value=json.loads(raw,object_pairs_hook=_pairs,parse_constant=_reject_constant)
        if type(value) is not dict or type(value.get('version')) is not int or value['version'] not in (1,2):
            raise ValueError()
        keys=('version','query','graph') if value['version']==1 else ('version','graph')
        if set(value)-{'run'}!=set(keys):
            raise ValueError()
        run=run_options(value['run']) if 'run' in value else None
        if run is not None and run.consistency == 'frozen' and not context.allow_frozen:
            raise QueryRejected('policy_denied', 'run.consistency')
        definition={key:value[key] for key in keys}
        if value['version']==1:
            graph,plans,order=normalize_graph(value['graph'],value['query'],context);drafts=frozenset()
            # Layout/notes don't affect execution identity, but survive saving.
            semantics=dict(query=value['query'],graph={k:v for k,v in graph.items() if k not in {'layout','notes'}})
        else:
            graph,plans,order,drafts=normalize_graph_v2(value['graph'],context)
            if not any(n['type'] in OUTPUT_TYPES and n['id'] in plans for n in graph['nodes']):
                raise ValueError()  # Nothing to run: no result block on a path from a source.
            semantics=dict(graph={k:v for k,v in graph.items() if k not in {'layout','notes'}})
        return GraphPlan(canonical(definition),tuple(plans.items()),order,hashlib.sha256(canonical(semantics)).hexdigest(),
                         context.fingerprint(),run,value['version'],drafts)
    except QueryRejected:raise
    except DefinitionTooLarge as error:raise QueryRejected('query_budget_exceeded',error.path,limit='request') from None
    except (ValueError,TypeError,KeyError,RecursionError):raise QueryRejected('invalid_query') from None


def frozen_plan(plan,context):
    """The same validated node query, read as one fixed list (consistency frozen)."""
    query=json.loads(plan.encoded)
    query['consistency']='frozen'
    return validate(canonical(query),context)


def fixed_row_blocks(plan,context,target=None):
    """(fixed list plan, row blocks it feeds) of a frozen run, in execution order.

    A table or gallery block is fed directly by the source when its frozen
    query has the fingerprint of the source's own: every block between them
    is a filter without a condition. A selection or a condition changes the
    rows, so such a block stays a current read, as do all calculations.

    A canvas (version 2) never fixes a list of its own: it reuses a live list
    (Library's), whose query fingerprint is target, on every table or gallery
    whose frozen query has exactly that fingerprint (the same source, conditions,
    order, columns, page size and zone). Its other blocks read current data;
    when none matches, the plan is None and the whole run reads current data."""
    kinds={n['id']:n['type'] for n in json.loads(plan.definition)['graph']['nodes']}
    queries=dict(plan.query_plans)
    if plan.version==2:
        if target is None:
            raise QueryRejected('invalid_query')
        blocks=tuple(i for i in plan.order if kinds[i] in ROW_TYPES and frozen_plan(queries[i],context).fingerprint==target)
        return (frozen_plan(queries[blocks[0]],context) if blocks else None),blocks
    sources=[i for i,kind in kinds.items() if kind=='source']
    if len(sources)!=1:
        raise QueryRejected('invalid_query')  # A fixed list is the list of one source (Library's hand-off).
    root=frozen_plan(queries[sources[0]],context)
    return root,tuple(i for i in plan.order if kinds[i] in ROW_TYPES and frozen_plan(queries[i],context).fingerprint==root.fingerprint)


def normalize_saved_research(value):
    from .saved import normalize_definition
    _exact(value,('version','query','graph'))
    if type(value['version']) is not int or value['version']!=3:
        raise ValueError('unsupported_saved_research')
    definition=normalize_definition(dict(version=2,query=value['query']),allow_unknown=True)
    query=deepcopy(definition['query'])
    if query['scope']['kind']=='channel':query['scope']['id']='UC'+'0'*22
    graph,_,_=normalize_graph(value['graph'],query,allow_unknown=True)
    result=dict(version=3,query=definition['query'],graph=graph)
    if len(canonical(result))>MAX_DEFINITION:
        raise ValueError('research_definition_too_large')
    return result


def normalize_saved_canvas(value):
    """A saved canvas (conditions version 4): only the graph. Every source names its own scope, so the
    Saved item is bound to no channel; unconnected drafts are kept, results never are."""
    _exact(value,('version','graph'))
    if type(value['version']) is not int or value['version']!=4:
        raise ValueError('unsupported_saved_research')
    graph,_,_,_=normalize_graph_v2(value['graph'],allow_unknown=True)
    result=dict(version=4,graph=graph)
    if len(canonical(result))>MAX_DEFINITION:
        raise DefinitionTooLarge('graph')
    return result


def saved_scopes(conditions):
    """The scopes a normalized Saved definition reads: one for versions 2 and 3, each source's for a canvas."""
    if conditions['version']==4:
        return [n['parameters']['scope'] for n in conditions['graph']['nodes'] if n['type']=='source']
    return [conditions['query']['scope']]
