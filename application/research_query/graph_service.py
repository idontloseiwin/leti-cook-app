"""Small sequential composition of the existing operators, with one budget.

Without run options every block is a current read. With
run options each result block also says how it was read (`consistency`) and a
table or gallery block returns its query (`query`): its next pages come from
POST /research/queries/execute with that query and `page.after`.

run {"consistency": "frozen"}, a fixed-list run: table and gallery blocks fed
directly by the source show one fixed list of the source rows. With frozen_ref
it is that live frozen result of the same query (Library's list): no new read
of rows. Without it the run fixes a new list through the frozen store, once
for all such blocks. A reference that expired, was invalidated or belongs to
another query or access context refuses the whole run (cursor_expired /
cursor_context_changed, 409); the caller fixes a new list or reads current
data. A new list that cannot be built (busy, too many rows, incomplete) is an
error of those blocks only. Calculations and blocks behind a selection or a
filter condition stay current reads and say so. Every block keeps its own
lifetime; the run's valid_before is the earliest. Everything is read from
stored data: a run never asks YouTube.
"""
from copy import deepcopy
from datetime import timedelta
import json
import time

from .frozen_service import frozen_page, frozen_reference
from .graph import validate_graph, fixed_row_blocks, GRAPH_V2_NODES, GRAPH_V2_OUTPUTS, OUTPUT_TYPES, OPERATION_TYPES
from .service import FROZEN_BUILD_SECONDS, instant, iso
from .validation import QueryRejected, canonical, check_budget

GRAPH_SECONDS = 30
MAX_RESULT_BYTES = 4*1024*1024
# Refused for the whole run, not for one block: access, scope or field policy changed.
WHOLE_RUN = frozenset({'policy_denied', 'cursor_context_changed', 'field_disabled', 'dependency_disabled'})
CURRENT_WARNING = 'Results are current reads, not a frozen historical snapshot.'
FIXED_WARNING = 'Blocks marked frozen show one fixed list of the source rows; other blocks are current reads.'


def _paging_query(plan):
    """The block's validated query, without a continuation: page it with page.after."""
    query = json.loads(plan.encoded)
    query['page'] = {'size': query['page']['size']}
    return query


class _FixedList:
    """The fixed list of one frozen run, obtained once for all its row blocks."""
    def __init__(self, reference, page, reused):
        self.reference, self.page, self.reused = reference, page, reused

    def describe(self):
        context = self.page['context']
        return dict(reused=self.reused, evaluated_at=context['evaluated_at'], valid_before=context['valid_before'],
                    total_count=self.page['page']['total_count'])


def _fix_list(service, plan, run, deadline, stopped):
    def checked(token):
        # Same checks as a page of that list: access, scope revision, lifetime.
        return frozen_reference(service, token, cancelled=stopped,
                                deadline=min(deadline, time.monotonic() + FROZEN_BUILD_SECONDS))
    if run.frozen_ref is not None:
        reference = checked(run.frozen_ref)
        # A list of another query (conditions, sort, columns, page size, zone)
        # is never a substitute; it stays valid for its own query.
        if reference.query_fingerprint != plan.fingerprint:
            raise QueryRejected('cursor_context_changed')
        return _FixedList(reference, frozen_page(service, reference), True)
    page = service.execute(plan.encoded, cancelled=stopped)  # One build through the frozen store.
    return _FixedList(checked(page['context']['frozen_ref']), page, False)


def execute_graph(service,raw,*,cancelled=lambda:False):
    start=time.monotonic();deadline=start+GRAPH_SECONDS
    context=service.context_provider()
    plan=validate_graph(raw,context)
    document=json.loads(plan.definition)
    nodes={n['id']:n for n in document['graph']['nodes']}
    queries=dict(plan.query_plans)
    run=plan.run
    results={};deadlines=[];bytes_used=0
    def stopped():return cancelled() or time.monotonic()>=deadline
    # One scope revision per source (a canvas may hold several). Each is fenced
    # again before any combined output.
    roots=[i for i in plan.order if nodes[i]['type']=='source']
    scopes={root:service.repository.scope_revision(queries[root],deadline=min(deadline,time.monotonic()+5),cancelled=stopped)
            for root in roots}
    fixed_plan,fixed_blocks=None,()
    if run is not None and run.consistency=='frozen':
        # A canvas only reuses a live list (Library's): its query fingerprint picks the blocks that show it.
        # A damaged or foreign reference is refused like any reference (cursor_expired), before any read.
        target=(service.codec.decode_frozen(run.frozen_ref,reference=True).query_fingerprint
                if plan.version==2 and run.frozen_ref is not None else None)
        fixed_plan,fixed_blocks=fixed_row_blocks(plan,context,target)
    fixed=failure=None
    for identity in plan.order:
        check_budget(deadline,cancelled)
        if service.context_provider().fingerprint()!=plan.context_fingerprint:
            raise QueryRejected('cursor_context_changed')
        node=nodes[identity];kind=node['type']
        if kind not in OUTPUT_TYPES:
            results[identity]={'state':'ready','output':'query','grain':'channel' if queries[identity].request.source.id.startswith('public.channels.') else 'video'}
            continue
        started=time.monotonic()
        from_list=identity in fixed_blocks
        try:
            if from_list:
                if failure is not None:raise failure  # One attempt serves every row block.
                if fixed is None:
                    try:fixed=_fix_list(service,fixed_plan,run,deadline,stopped)
                    except QueryRejected as error:
                        failure=error;raise
                payload=deepcopy(fixed.page)
            elif kind in OPERATION_TYPES:
                payload=service.execute_operation(canonical(dict(version=1,operation=OPERATION_TYPES[kind],query=json.loads(queries[identity].encoded),parameters=node['parameters'])),cancelled=stopped)
            else:
                payload=service.execute(queries[identity].encoded,cancelled=stopped)
            result={'state':'ready','output':'operation' if kind in OPERATION_TYPES else 'page'}
            if run is not None:
                result['consistency']='frozen' if from_list else 'current'
                if kind not in OPERATION_TYPES:
                    result['query']=_paging_query(fixed_plan if from_list else queries[identity])
                    bytes_used+=len(canonical(result['query']))
            bytes_used+=len(canonical(payload))
            if bytes_used>MAX_RESULT_BYTES:raise QueryRejected('query_budget_exceeded',limit='response')
            deadlines.append(instant(payload['context']['valid_before']))
            results[identity]={**result,'value':payload,'elapsed_ms':round((time.monotonic()-started)*1000,1)}
        except QueryRejected as error:
            # A block stopped by this run's own 30 s needs no limit of its own: the run past its
            # deadline is refused as a whole below, naming the time limit.
            # The run's own list expired, was invalidated or belongs elsewhere: refuse the run.
            if error.code in WHOLE_RUN or (from_list and error.code=='cursor_expired'):
                raise
            result={'state':'error','code':error.code}
            if run is not None:
                result['consistency']='frozen' if from_list else 'current'
                if error.path is not None:result['path']=error.path
                if error.limit is not None:result['limit']=error.limit  # The bound a too-large refusal reached.
            results[identity]={**result,'elapsed_ms':round((time.monotonic()-started)*1000,1)}
    for root,scope in scopes.items():
        service.repository.recheck_scope(queries[root],scope,deadline=deadline,cancelled=stopped)
    for identity in plan.drafts:
        results[identity]={'state':'draft'}  # Kept on the canvas without a path to a source: not run.
    if service.context_provider().fingerprint()!=plan.context_fingerprint:
        raise QueryRejected('cursor_context_changed')
    if fixed is not None:
        fixed.reference.require_current()  # Still stored and unexpired as the run answers.
    now=instant(service.clock())
    until=min(deadlines,default=now+timedelta(seconds=GRAPH_SECONDS))
    if until<=now:raise QueryRejected('cursor_expired')
    check_budget(deadline,cancelled)
    output=dict(version=1,graph_fingerprint=plan.fingerprint,
        context=dict(evaluated_at=iso(now),valid_before=iso(until),consistency='fixed_source_list' if fixed else 'sequential_current_reads'),
        nodes=results,status='partial' if any(r['state']=='error' for r in results.values()) else 'ready',
        budgets=(dict(nodes=12,result_blocks=4) if plan.version==1 else dict(nodes=GRAPH_V2_NODES,result_blocks=GRAPH_V2_OUTPUTS))
        | dict(result_bytes=MAX_RESULT_BYTES,seconds=GRAPH_SECONDS),
        warnings=[FIXED_WARNING if fixed else CURRENT_WARNING])
    if run is not None:
        output['run']=dict(consistency=run.consistency,fixed=fixed.describe() if fixed else None)
    return output
