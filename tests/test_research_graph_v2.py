"""Canvas graph (version 2): independent sources and branches, a sort node, local result columns, kept drafts."""
from copy import deepcopy
from dataclasses import replace
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query.discovery import contracts
from research_query.graph import (CANVAS_NOTES_BYTES, GRAPH_V2_NODES, GRAPH_V2_OUTPUTS, MAX_DEFINITION, DefinitionTooLarge,
                                  normalize_graph_v2, validate_graph)
from research_query.operations import OPERATIONS
from research_query.validation import AccessContext, QueryRejected, canonical
from test_research_channels import channel_query
from test_research_graph import CALENDAR_RESULT, GraphRepository
from test_research_query_contract import query
from test_research_frozen import CONTEXT as FROZEN_CONTEXT, NOW as FROZEN_NOW, frozen_query
from research_query.cursor import CursorCodec
from research_query.frozen import FrozenStore
from research_query.service import QueryService

CONTEXT = AccessContext('synthetic', 'owner', enabled=True, allowed_operations=frozenset(OPERATIONS))
VIEWS = {'field': 'video.views', 'op': 'gte', 'value': '100'}
LONG = {'field': 'video.duration_seconds', 'op': 'gte', 'value': '600'}


def source_params(q=None):
    q = q or query()
    return {'source': q['source'], 'scope': q['scope'], 'video_types': q.get('video_types'), 'time': q.get('time', {'mode': 'current', 'timezone': 'UTC'})}


def canvas(nodes, edges, run=None):
    """nodes: (id, type, parameters); edges: (from, to)."""
    graph = dict(version=2, nodes=[dict(id=i, type=t, parameters=deepcopy(p)) for i, t, p in nodes],
                 edges=[{'from': a, 'to': b, 'output': 'rows', 'input': 'rows'} for a, b in edges],
                 notes='', layout={i: dict(x=100 * n, y=0) for n, (i, _, _) in enumerate(nodes)})
    document = dict(version=2, graph=graph)
    if run is not None:
        document['run'] = run
    return document


def order(q):
    """The order of a validated query without the defaults validation adds (nulls)."""
    return [{'field': key['field'], 'direction': key['direction']} for key in q['order_by']]


def plans(document, context=CONTEXT):
    plan = validate_graph(canonical(document), context)
    return plan, {identity: json.loads(p.encoded) for identity, p in plan.query_plans}


GALLERY = {'select': ['video.title', 'video.thumbnail_url', 'video.views'], 'page_size': 24}
TABLE = {'select': ['video.title', 'video.duration_seconds'], 'page_size': 50}


class CanvasGraphTests(unittest.TestCase):
    def test_source_filter_sort_cards_compose_one_query(self):
        plan, q = plans(canvas([('videos', 'source', source_params()), ('views', 'filter', {'where': VIEWS}),
                                ('order', 'sort', {'order_by': [{'field': 'video.views', 'direction': 'desc'}]}),
                                ('cards', 'gallery', GALLERY)],
                               [('videos', 'views'), ('views', 'order'), ('order', 'cards')]))
        self.assertEqual(plan.version, 2)
        self.assertEqual(plan.drafts, frozenset())
        cards = q['cards']
        self.assertEqual(cards['where'], VIEWS)
        self.assertEqual(order(cards), [{'field': 'video.views', 'direction': 'desc'}])
        self.assertEqual(cards['select'][0], 'video.id')  # Identity first, then the block's own columns.
        self.assertEqual(cards['select'][1:], GALLERY['select'])
        self.assertEqual(cards['page']['size'], 24)
        self.assertEqual(order(q['videos']), [{'field': 'video.published_at', 'direction': 'desc'}])  # Default order.

    def test_branches_are_independent(self):
        _, q = plans(canvas([('videos', 'source', source_params()),
                             ('a', 'filter', {'where': VIEWS}), ('cards', 'gallery', GALLERY),
                             ('b', 'filter', {'where': LONG}),
                             ('order', 'sort', {'order_by': [{'field': 'video.duration_seconds', 'direction': 'asc'}]}),
                             ('table', 'table', TABLE)],
                            [('videos', 'a'), ('a', 'cards'), ('videos', 'b'), ('b', 'order'), ('order', 'table')]))
        self.assertEqual(q['cards']['where'], VIEWS)
        self.assertEqual(q['table']['where'], LONG)
        self.assertNotEqual(order(q['cards']), order(q['table']))
        self.assertEqual(q['cards']['select'][1:], GALLERY['select'])
        self.assertEqual(q['table']['select'][1:], TABLE['select'])  # Columns are local to each result block.

    def test_two_independent_sources_keep_their_source_scope_and_type(self):
        shorts = query(video_types=['short'])
        channels = channel_query()
        _, q = plans(canvas([('videos', 'source', source_params(shorts)), ('cards', 'gallery', GALLERY),
                             ('channels', 'source', source_params(channels)),
                             ('table', 'table', {'select': ['channel.title', 'channel.subscribers'], 'page_size': 25})],
                            [('videos', 'cards'), ('channels', 'table')]))
        self.assertEqual(q['cards']['video_types'], ['short'])
        self.assertEqual(q['table']['source'], channels['source'])
        self.assertNotIn('video_types', q['table'])
        self.assertEqual(q['table']['select'][0], 'channel.id')
        self.assertEqual(order(q['table']), [{'field': 'channel.title', 'direction': 'asc'}])

    def test_last_sort_sets_the_whole_order(self):
        _, q = plans(canvas([('videos', 'source', source_params()),
                             ('one', 'sort', {'order_by': [{'field': 'video.views', 'direction': 'desc'}]}),
                             ('two', 'sort', {'order_by': [{'field': 'video.published_at', 'direction': 'asc'},
                                                           {'field': 'video.views', 'direction': 'desc'}]}),
                             ('table', 'table', TABLE)],
                            [('videos', 'one'), ('one', 'two'), ('two', 'table')]))
        self.assertEqual(order(q['table']), [{'field': 'video.published_at', 'direction': 'asc'},
                                                  {'field': 'video.views', 'direction': 'desc'}])

    def test_source_keeps_a_studys_completeness_rule_and_change_window(self):
        # An old study or a Library hand-off opens with the same meaning: these reach every branch.
        day = {'version': 1, 'window_seconds': 86400, 'tolerance_seconds': 7200, 'age_seconds': 86400, 'age_tolerance_seconds': 7200}
        params = {**source_params(), 'coverage': {'mode': 'require_complete'}, 'observations': day}
        _, q = plans(canvas([('videos', 'source', params), ('table', 'table', TABLE)], [('videos', 'table')]))
        self.assertEqual(q['table']['coverage']['mode'], 'require_complete')
        self.assertEqual(q['table']['observations']['window_seconds'], 86400)
        _, plain = plans(canvas([('videos', 'source', source_params()), ('table', 'table', TABLE)], [('videos', 'table')]))
        self.assertEqual(plain['table']['coverage']['mode'], 'available')
        self.assertEqual(plain['table']['observations']['window_seconds'], 3600)
        for extra in ({'where': VIEWS}, {'select': ['video.title']}, {'coverage': {'mode': 'everything'}}):
            with self.assertRaises(QueryRejected):
                plans(canvas([('videos', 'source', {**source_params(), **extra}), ('table', 'table', TABLE)], [('videos', 'table')]))
        with self.assertRaises(QueryRejected):  # Channels have no observed-change window.
            plans(canvas([('channels', 'source', {**source_params(channel_query()), 'observations': day}), ('table', 'table', {'select': ['channel.title'], 'page_size': 25})],
                         [('channels', 'table')]))

    def test_selection_keeps_exact_ids_on_its_branch(self):
        ids = ['a0000000001', 'a0000000002']
        _, q = plans(canvas([('videos', 'source', source_params()), ('views', 'filter', {'where': VIEWS}),
                             ('picked', 'selection', {'mode': 'include', 'ids': ids}), ('cards', 'gallery', GALLERY),
                             ('all', 'table', TABLE)],
                            [('videos', 'views'), ('views', 'picked'), ('picked', 'cards'), ('views', 'all')]))
        self.assertEqual(q['cards']['where'], {'all': [VIEWS, {'field': 'video.id', 'op': 'in', 'value': ids}]})
        self.assertEqual(q['all']['where'], VIEWS)

    def test_unconnected_nodes_are_kept_drafts(self):
        document = canvas([('videos', 'source', source_params()), ('cards', 'gallery', GALLERY),
                           ('loose', 'filter', {'where': VIEWS}), ('later', 'table', TABLE)],
                          [('videos', 'cards'), ('loose', 'later')])
        plan, q = plans(document)
        self.assertEqual(plan.drafts, frozenset({'loose', 'later'}))
        self.assertEqual(set(q), {'videos', 'cards'})
        # A definition (saving) keeps drafts; a run needs one result block on a path from a source.
        definition, _, _, drafts = normalize_graph_v2(document['graph'])
        self.assertEqual(drafts, frozenset({'loose', 'later'}))
        only_drafts = canvas([('videos', 'source', source_params()), ('loose', 'filter', {'where': VIEWS}),
                              ('later', 'table', TABLE)], [('loose', 'later')])
        normalize_graph_v2(only_drafts['graph'])
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            validate_graph(canonical(only_drafts), CONTEXT)

    def test_ports_cycles_types_and_budgets_are_refused(self):
        base = [('videos', 'source', source_params()), ('views', 'filter', {'where': VIEWS}), ('cards', 'gallery', GALLERY)]
        cases = {
            'result feeds nothing': (base + [('more', 'filter', {'where': LONG})], [('videos', 'views'), ('views', 'cards'), ('cards', 'more')]),
            'source has no input': (base, [('views', 'videos'), ('videos', 'cards')]),
            'one input per node': (base, [('videos', 'cards'), ('views', 'cards')]),
            'no cycles': ([('videos', 'source', source_params()), ('a', 'filter', {'where': VIEWS}), ('b', 'filter', {'where': LONG}),
                           ('cards', 'gallery', GALLERY)], [('a', 'b'), ('b', 'a'), ('videos', 'cards')]),
            'summary needs videos': ([('channels', 'source', source_params(channel_query())), ('summary', 'summary', {})], [('channels', 'summary')]),
            'channels have no type': ([('channels', 'source', {**source_params(channel_query()), 'video_types': ['long']}),
                                       ('table', 'table', TABLE)], [('channels', 'table')]),
            'videos name a type': ([('videos', 'source', {**source_params(), 'video_types': None}), ('cards', 'gallery', GALLERY)], [('videos', 'cards')]),
            'sort keys 1..3': (base[:1] + [('order', 'sort', {'order_by': []}), ('cards', 'gallery', GALLERY)], [('videos', 'order'), ('order', 'cards')]),
            'page size bounded': (base[:1] + [('cards', 'gallery', {**GALLERY, 'page_size': 101})], [('videos', 'cards')]),
            'a source is required': ([('views', 'filter', {'where': VIEWS}), ('cards', 'gallery', GALLERY)], [('views', 'cards')]),
        }
        for name, (nodes, edges) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises((QueryRejected, ValueError)):
                    validate_graph(canonical(canvas(nodes, edges)), CONTEXT)
        many = [('videos', 'source', source_params())] + [(f'f{i}', 'filter', {'where': VIEWS}) for i in range(GRAPH_V2_NODES)]
        with self.assertRaises(QueryRejected):
            validate_graph(canonical(canvas(many, [('videos', 'f0')])), CONTEXT)
        outputs = [('videos', 'source', source_params())] + [(f'r{i}', 'table', TABLE) for i in range(GRAPH_V2_OUTPUTS + 1)]
        with self.assertRaises(QueryRejected):
            validate_graph(canonical(canvas(outputs, [('videos', f'r{i}') for i in range(GRAPH_V2_OUTPUTS + 1)])), CONTEXT)

    def test_notes_and_the_whole_canvas_are_byte_budgets_with_their_own_refusal(self):
        """Notes up to 8,192 UTF-8 bytes (4,096 Cyrillic letters); a size refusal names what is too large."""
        def document(notes, ids=None):
            nodes = [('videos', 'source', source_params()), ('cards', 'gallery', GALLERY)]
            edges = [('videos', 'cards')]
            if ids:
                nodes.append(('picked', 'selection', {'mode': 'include', 'ids': ids}))
                edges = [('videos', 'picked'), ('picked', 'cards')]
            value = canvas(nodes, edges)
            value['graph']['notes'] = notes
            return value
        self.assertEqual(CANVAS_NOTES_BYTES, 8192)
        for notes in ('ж' * 4096, 'n' * 8192, 'ж' * 4095 + 'nn'):
            validate_graph(canonical(document(notes)), CONTEXT)
            normalize_graph_v2(document(notes)['graph'])
        for notes in ('ж' * 4097, 'n' * 8193, '€' * 2731):
            with self.subTest(notes=notes[:1]), self.assertRaises(QueryRejected) as refused:
                validate_graph(canonical(document(notes)), CONTEXT)
            self.assertEqual((refused.exception.code, refused.exception.path, refused.exception.limit),
                             ('query_budget_exceeded', 'graph.notes', 'request'))
            with self.assertRaises(DefinitionTooLarge) as kept:
                normalize_graph_v2(document(notes)['graph'], allow_unknown=True)
            self.assertEqual(kept.exception.path, 'graph.notes')
        ids = [f'v{i:010d}' for i in range(1000)]
        validate_graph(canonical(document('n' * 1000, ids)), CONTEXT)
        with self.assertRaises(QueryRejected) as whole:
            validate_graph(canonical(document('n' * 3000, ids)), CONTEXT)
        self.assertEqual((whole.exception.code, whole.exception.path, whole.exception.limit), ('query_budget_exceeded', 'graph', 'request'))
        self.assertGreater(len(canonical(document('n' * 3000, ids)['graph'])), MAX_DEFINITION)
        # Other mistakes stay "invalid", never a budget.
        broken = document('ok')
        broken['graph']['notes'] = 'a\x00b'
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            validate_graph(canonical(broken), CONTEXT)

    def test_a_study_graph_keeps_its_4096_byte_notes(self):
        study = {'version': 1, 'query': query(), 'graph': {'version': 1, 'nodes': [
            {'id': 's', 'type': 'source', 'parameters': {}}, {'id': 't', 'type': 'table', 'parameters': {}}],
            'edges': [{'from': 's', 'to': 't', 'output': 'query', 'input': 'query'}],
            'notes': 'ж' * 2048, 'layout': {'s': {'x': 0, 'y': 0}, 't': {'x': 1, 'y': 0}}}}
        validate_graph(canonical(study), CONTEXT)
        study['graph']['notes'] += 'ж'
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            validate_graph(canonical(study), CONTEXT)

    def test_agents_learn_the_canvas_byte_limits(self):
        described = contracts(CONTEXT, 'canvas')
        self.assertEqual((described['max_definition_bytes'], described['max_notes_bytes']), (MAX_DEFINITION, CANVAS_NOTES_BYTES))
        validate_graph(canonical(described['example']), CONTEXT)

    def test_server_permissions_apply_to_every_branch(self):
        document = canvas([('videos', 'source', source_params()), ('summary', 'summary', {})], [('videos', 'summary')])
        with self.assertRaisesRegex(QueryRejected, 'policy_denied'):
            validate_graph(canonical(document), replace(CONTEXT, allowed_operations=frozenset()))

    def test_layout_and_notes_do_not_change_execution_identity(self):
        a = canvas([('videos', 'source', source_params()), ('cards', 'gallery', GALLERY)], [('videos', 'cards')])
        b = deepcopy(a)
        b['graph']['layout']['cards'] = dict(x=900, y=300)
        b['graph']['notes'] = 'Why these?'
        self.assertEqual(validate_graph(canonical(a), CONTEXT).fingerprint, validate_graph(canonical(b), CONTEXT).fingerprint)


class CanvasRunTests(unittest.TestCase):
    def setUp(self):
        self.repo = GraphRepository()
        self.service = QueryService(self.repo, lambda: FROZEN_CONTEXT, clock=lambda: FROZEN_NOW, codec=CursorCodec(),
                                    frozen=FrozenStore(clock=lambda: FROZEN_NOW, monotonic=lambda: 1000.0, auto_prune=False))

    def test_two_sources_each_fenced_and_drafts_reported(self):
        q = frozen_query(consistency='current')
        document = canvas([('a', 'source', source_params(q)), ('cards', 'gallery', {'select': ['video.title'], 'page_size': 2}),
                           ('b', 'source', source_params(q)), ('calendar', 'calendar', {'year': 2026, 'time_zone': 'UTC'}),
                           ('loose', 'table', TABLE)],
                          [('a', 'cards'), ('b', 'calendar')])
        output = self.service.execute_graph(canonical(document))
        self.assertEqual(output['nodes']['cards']['state'], 'ready')
        self.assertEqual(output['nodes']['calendar']['value']['result'], CALENDAR_RESULT)
        self.assertEqual(output['nodes']['loose'], {'state': 'draft'})
        self.assertEqual(output['budgets']['nodes'], GRAPH_V2_NODES)
        self.assertEqual(self.repo.calls.count('scope_revision'), 2)
        # Each block's read fences its own scope; the run fences every source once more at the end.
        self.assertEqual(self.repo.calls[-2:], ['recheck_scope', 'recheck_scope'])

    def library_canvas(self, run, cards=None):
        """Library's query as a canvas opens it (Source, Sort, Cards with Library's columns and page), a second
        branch with other columns, and a second source."""
        q = frozen_query()
        cards = cards or {'select': [f for f in q['select'] if f != 'video.id'], 'page_size': q['page']['size']}
        order = {'order_by': [{'field': k['field'], 'direction': k['direction']} for k in q['order_by']]}
        return canvas([('videos', 'source', source_params(q)), ('order', 'sort', order), ('cards', 'gallery', cards),
                       ('other', 'table', {'select': ['video.title'], 'page_size': 50}),
                       ('again', 'source', source_params(q)), ('order_2', 'sort', order), ('later', 'gallery', cards)],
                      [('videos', 'order'), ('order', 'cards'), ('videos', 'other'), ('again', 'order_2'), ('order_2', 'later')], run=run)

    def test_a_canvas_reuses_librarys_list_on_the_branches_with_its_query(self):
        library = self.service.execute(canonical(frozen_query()))
        reads, current = self.repo.reads, self.repo.current
        output = self.service.execute_graph(canonical(self.library_canvas({'consistency': 'frozen', 'frozen_ref': library['context']['frozen_ref']})))
        # No new list is built; only the other table reads current rows.
        self.assertEqual((self.repo.reads, self.repo.current), (reads, current + 1))
        nodes = output['nodes']
        self.assertEqual(output['context']['consistency'], 'fixed_source_list')
        self.assertEqual(output['run']['fixed']['reused'], True)
        self.assertEqual(output['run']['fixed']['total_count'], library['page']['total_count'])
        for identity in ('cards', 'later'):  # Library's exact query, also behind another source of the same scope.
            self.assertEqual(nodes[identity]['consistency'], 'frozen')
            self.assertEqual([row['id'] for row in nodes[identity]['value']['rows']], [row['id'] for row in library['rows']])
            self.assertEqual(nodes[identity]['value']['page']['total_count'], library['page']['total_count'])
        self.assertEqual(nodes['other']['consistency'], 'current')
        self.assertEqual(nodes['other']['value']['context']['consistency'], 'current')

    def test_a_changed_branch_reads_current_data_and_says_so(self):
        library = self.service.execute(canonical(frozen_query()))
        changed = self.library_canvas({'consistency': 'frozen', 'frozen_ref': library['context']['frozen_ref']},
                                      cards={'select': ['video.title'], 'page_size': 50})
        output = self.service.execute_graph(canonical(changed))
        self.assertEqual(output['run'], {'consistency': 'frozen', 'fixed': None})
        self.assertEqual(output['context']['consistency'], 'sequential_current_reads')
        self.assertTrue(all(r.get('consistency') == 'current' for r in output['nodes'].values() if r['state'] == 'ready' and r['output'] == 'page'))

    def test_a_canvas_never_fixes_a_list_of_its_own_and_refuses_a_bad_reference(self):
        with self.assertRaisesRegex(QueryRejected, 'invalid_query'):
            self.service.execute_graph(canonical(self.library_canvas({'consistency': 'frozen'})))
        reads = self.repo.reads
        with self.assertRaisesRegex(QueryRejected, 'cursor_expired'):
            self.service.execute_graph(canonical(self.library_canvas({'consistency': 'frozen', 'frozen_ref': 'not-a-reference'})))
        self.assertEqual(self.repo.reads, reads)


if __name__ == '__main__':
    unittest.main()
