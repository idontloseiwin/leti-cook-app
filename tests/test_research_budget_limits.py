"""Every "too large / too long" refusal names the limit it reached.

One code, query_budget_exceeded, covers five bounds; the added `limit` key says
which, so the page can say what to change. A read whose client left is not a
time limit: it keeps the bare code. Offline: fakes stand in for PostgreSQL.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sys
import time
import unittest
from unittest.mock import patch

import anyio
from fastapi import FastAPI
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import research_mcp
from research_query import aggregate, aggregate_service, contracts, frozen_service, graph_service
from research_query import service as service_module
from research_query.admission import ReadAdmission
from research_query.contracts import MAX_DEPTH, MAX_LEAVES, MAX_LIST_VALUES
from research_query.cursor import CursorCodec
from research_query.frozen import FrozenStore
from research_query.graph import validate_graph
from research_query.operation_kinds import facets
from research_query.service import QueryService
from research_query import validation
from research_query.validation import QueryRejected, canonical, validate
from test_research_frozen import CONTEXT, NOW, Repository as FrozenRepository, frozen_query
from test_research_graph import CALENDAR_RESULT, GraphRepository, study
from test_research_operation_kinds import Repository as OperationRepository, request
from test_research_query_contract import query
from test_research_query_service import NOW as PAGE_NOW, Repository as PageRepository

LEAF = {'field': 'video.views', 'op': 'gte', 'value': '1'}
CALENDAR = ('video.publication_calendar.v1', {'year': 2026, 'time_zone': 'UTC'})
# The columns the page fake serves (tests/test_research_query_service.py).
PAGE_SELECT = ['video.id', 'video.title', 'video.views']


def page_query(**changes):
    return query(select=list(PAGE_SELECT), **changes)


def nested(depth):
    node = LEAF
    for _ in range(depth):
        node = {'not': node}
    return node


class BudgetLimitTests(unittest.TestCase):
    def refusal(self, call):
        with self.assertRaises(QueryRejected) as caught:
            call()
        self.assertEqual(caught.exception.code, 'query_budget_exceeded')
        return caught.exception.payload()

    def frozen_service(self, repo=None):
        store = FrozenStore(clock=lambda: NOW, monotonic=lambda: 1000.0, auto_prune=False)
        return QueryService(repo or FrozenRepository(), lambda: CONTEXT, clock=lambda: NOW, codec=CursorCodec(), frozen=store)

    def page_service(self, repo=None):
        return QueryService(repo or PageRepository(), lambda: CONTEXT, clock=lambda: PAGE_NOW)

    @staticmethod
    def two_rows():
        repo = PageRepository()
        repo.data['rows'].append({**repo.data['rows'][0], 'video_id': 'a0000000002', 'sort_keys': ['a0000000002']})
        return repo

    def operation(self, service=None, cancelled=lambda: False):
        operation, parameters = CALENDAR
        service = service or QueryService(OperationRepository(CALENDAR_RESULT), lambda: CONTEXT, clock=lambda: PAGE_NOW)
        return service.execute_operation(request(operation, query(select=['video.id']), parameters), cancelled=cancelled)

    def test_every_place_names_its_limit(self):
        """The table "place -> limit"; each row fails if the refusal names another limit or none."""
        too_many_values = {'field': 'video.id', 'op': 'in', 'value': ['a%010d' % n for n in range(MAX_LIST_VALUES + 1)]}
        many_leaves = {'all': [{'all': [LEAF] * 100}, {'all': [LEAF] * 100}]}
        group = lambda field, count: {'any': [{'field': field, 'op': 'eq', 'value': str(n)} for n in range(count)]}
        facet_conditions = {'views': group('video.views', 70), 'likes': group('video.likes', 70)}
        frozen = self.frozen_service()
        first = frozen.execute(canonical(frozen_query()))
        ids_request = canonical({'version': 1, 'frozen_ref': first['context']['frozen_ref']})
        next_page = canonical({**frozen_query(), 'page': {'size': 50, 'after': first['page']['next_cursor']}})

        @contextmanager
        def nothing():
            yield

        places = [
            # request: the request, its conditions or its values are too large or too complex.
            ('validation.py: body over 64 KiB', lambda: validate(b' ' * 65537, CONTEXT), nothing(), None, 'request'),
            ('validation.py: JSON nesting', lambda: validate(b'[' * 30 + b']' * 30, CONTEXT), nothing(), None, 'request'),
            ('validation.py: where depth', lambda: validate(canonical(query(where=nested(MAX_DEPTH + 1))), CONTEXT), nothing(), 'where', 'request'),
            ('validation.py: where children', lambda: validate(canonical(query(where={'all': [LEAF] * (MAX_LEAVES + 1)})), CONTEXT),
             nothing(), 'where', 'request'),
            ('validation.py: where leaves', lambda: validate(canonical(query(where=many_leaves)), CONTEXT), nothing(), 'where', 'request'),
            ('validation.py: list values', lambda: validate(canonical(query(where=too_many_values)), CONTEXT), nothing(), None, 'request'),
            ('aggregate.py: operation body', lambda: aggregate.validate_operation(b'x' * 65537, CONTEXT), nothing(), None, 'request'),
            ('graph.py: graph body', lambda: validate_graph(b'x' * 65537, CONTEXT), nothing(), None, 'request'),
            ('frozen_service.py: ids body', lambda: frozen.frozen_ids(b'x' * (64 * 1024 + 1)), nothing(), None, 'request'),
            ('facets.py: conditions of all groups', lambda: aggregate.validate_operation(request(facets.ID, query(select=['video.id']),
             {'population': {'kind': 'query'}, 'conditions': facet_conditions}), CONTEXT), nothing(), 'parameters.conditions', 'request'),
            ('facets.py: one group condition', lambda: aggregate.validate_operation(request(facets.ID, query(select=['video.id']),
             {'population': {'kind': 'query'}, 'conditions': {'views': group('video.views', MAX_LEAVES + 1)}}), CONTEXT),
             nothing(), 'parameters.conditions.views', 'request'),
            # response: the answer is too large to send.
            ('service.py: current page', lambda: self.page_service().execute(canonical(page_query())),
             patch.object(service_module, 'MAX_RESPONSE_BYTES', 100), None, 'response'),
            ('cursor.py: page cursor', lambda: self.page_service(self.two_rows()).execute(canonical(page_query(page={'size': 1}))),
             patch('research_query.cursor.MAX_CURSOR_CHARS', 8), None, 'response'),
            ('cursor.py: frozen reference', lambda: self.frozen_service().execute(canonical(frozen_query())),
             patch('research_query.cursor.MAX_CURSOR_CHARS', 8), None, 'response'),
            ('frozen_service.py: next page', lambda: frozen.execute(next_page),
             patch.object(frozen_service, 'MAX_RESPONSE_BYTES', 100), None, 'response'),
            ('frozen_service.py: ids answer', lambda: frozen.frozen_ids(ids_request),
             patch.object(frozen_service, 'MAX_RESPONSE_BYTES', 100), None, 'response'),
            ('aggregate_service.py: operation answer', self.operation,
             patch.object(aggregate_service, 'MAX_OUTPUT_BYTES', 100), None, 'response'),
            # time: the read is past its own deadline.
            ('service.py: page deadline', lambda: self.page_service().execute(canonical(page_query())),
             patch.object(service_module.time, 'monotonic', self.clock_that_jumps()), None, 'time'),
            ('aggregate_service.py: operation deadline', self.operation,
             patch.object(aggregate_service, 'DEADLINE_SECONDS', -1), None, 'time'),
            ('graph_service.py: run deadline', lambda: self.graph_service().execute_graph(canonical(study())),
             patch.object(graph_service, 'GRAPH_SECONDS', -1), None, 'time'),
        ]
        for name, call, context, path, limit in places:
            with self.subTest(place=name), context:
                self.assertEqual(self.refusal(call), {'code': 'query_budget_exceeded', 'path': path, 'limit': limit})

    def clock_that_jumps(self):
        """time.monotonic for one page read: 9 s pass between the start and the final check."""
        real, calls = time.monotonic, []
        def clock():
            calls.append(1)
            return real() + (0 if len(calls) == 1 else 9)
        return clock

    def graph_service(self):
        store = FrozenStore(clock=lambda: NOW, monotonic=lambda: 1000.0, auto_prune=False)
        return QueryService(GraphRepository(), lambda: CONTEXT, clock=lambda: NOW, codec=CursorCodec(), frozen=store)

    def test_a_graph_block_over_the_result_bytes_names_the_response_limit(self):
        with patch.object(graph_service, 'MAX_RESULT_BYTES', 10):
            result = self.graph_service().execute_graph(canonical(study(run={'consistency': 'current'})))
        live = result['nodes']['live']
        self.assertEqual((live['state'], live['code'], live['limit']), ('error', 'query_budget_exceeded', 'response'))

    def test_a_client_that_left_is_not_a_time_limit(self):
        later, passed = time.monotonic() + 60, time.monotonic() - 1
        check_budget = validation.check_budget
        self.assertEqual(self.refusal(lambda: check_budget(later, lambda: True)), {'code': 'query_budget_exceeded', 'path': None})
        self.assertEqual(self.refusal(lambda: check_budget(passed, lambda: False))['limit'], 'time')
        self.assertEqual(self.refusal(lambda: check_budget(passed, lambda: True))['limit'], 'time')
        self.assertIsNone(check_budget(later, lambda: False))
        stopped = lambda: True
        for name, call in [
            ('current page', lambda: self.page_service().execute(canonical(page_query()), cancelled=stopped)),
            ('frozen list', lambda: self.frozen_service().execute(canonical(frozen_query()), cancelled=stopped)),
            ('operation', lambda: self.operation(cancelled=stopped)),
            ('graph run', lambda: self.graph_service().execute_graph(canonical(study()), cancelled=stopped)),
        ]:
            with self.subTest(read=name):
                self.assertEqual(self.refusal(call), {'code': 'query_budget_exceeded', 'path': None})
        admission = ReadAdmission(concurrency=1)
        with self.assertRaises(QueryRejected) as caught:
            with admission.enter(deadline=later, cancelled=stopped):
                pass
        self.assertEqual(caught.exception.payload(), {'code': 'query_budget_exceeded', 'path': None})

    def test_the_contract_lists_the_same_limits_everywhere(self):
        self.assertEqual(contracts.BUDGET_LIMITS, ('rows', 'memory', 'time', 'request', 'response'))
        self.assertEqual(research_mcp.BUDGET_LIMITS, contracts.BUDGET_LIMITS)
        for limit in contracts.BUDGET_LIMITS:
            self.assertEqual(contracts.QueryError(code='query_budget_exceeded', limit=limit).limit, limit)
            self.assertEqual(QueryRejected('query_budget_exceeded', limit=limit).payload()['limit'], limit)
        for code, limit in (('invalid_query', 'request'), ('query_budget_exceeded', 'bytes')):
            with self.assertRaises(ValueError):
                QueryRejected(code, limit=limit)
        # The page reads the same list (application/frontend/src/explorer/api.ts).
        source = (ROOT / 'application/frontend/src/explorer/api.ts').read_text(encoding='utf-8')
        listed = re.search(r"const BUDGET_LIMITS: readonly string\[\] = \[([^\]]*)\];", source).group(1)
        self.assertEqual(tuple(re.findall(r"'([a-z]+)'", listed)), contracts.BUDGET_LIMITS)

    def test_mcp_tool_errors_carry_the_limit_as_a_suffix(self):
        answers = {}
        app = FastAPI()

        @app.post('/research/queries/execute')
        async def execute():
            return JSONResponse(answers['body'], status_code=422)

        caller = research_mcp.ResearchCaller(app, 'http://127.0.0.1:8000', 'Bearer test')

        def failure(**body):
            answers['body'] = {'code': 'query_budget_exceeded', 'path': None, **body}
            with self.assertRaises(research_mcp.ToolFailure) as caught:
                anyio.run(lambda: caller.request('POST', '/research/queries/execute', body={'query_version': 1}))
            return str(caught.exception)

        for limit in contracts.BUDGET_LIMITS:
            self.assertEqual(failure(limit=limit), 'query_budget_exceeded:' + limit)
        self.assertEqual(failure(), 'query_budget_exceeded')
        self.assertEqual(failure(limit='bytes'), 'query_budget_exceeded')
        # A request too large to send is refused before any call, with the same suffix.
        with self.assertRaises(research_mcp.ToolFailure) as caught:
            anyio.run(lambda: caller.request('POST', '/research/queries/execute', body={'x': 'y' * research_mcp.MAX_REQUEST}))
        self.assertEqual(str(caught.exception), 'query_budget_exceeded:request')


if __name__ == '__main__':
    unittest.main()
