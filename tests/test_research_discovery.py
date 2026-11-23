"""Every published example passes the real validator; source SQL stays bounded/parameterized."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from research_query.discovery import describe, contracts, EXAMPLES
from research_query.operation_kinds import OPERATION_KINDS
from research_query.operations import OPERATIONS
from research_query.validation import AccessContext, QueryRejected, canonical
from research_query.aggregate import validate_operation
from research_query.contracts import QueryRequest
from research_query.graph import validate_graph
from test_research_query_contract import query
from test_research_channels import channel_query
from db.reads.research_sources import ResearchSourcesRepository

CONTEXT = AccessContext('test', 'agent', enabled=True, allowed_operations=frozenset(OPERATIONS))


class DiscoveryTests(unittest.TestCase):
    def test_operation_examples_execute_validation(self):
        self.assertEqual(set(EXAMPLES), set(OPERATION_KINDS))
        for item in contracts(CONTEXT, 'operations')['operations']:
            with self.subTest(operation=item['id']):
                q = channel_query() if item['source'].startswith('public.channels') else query()
                q['scope'] = {'kind': 'channel', 'id': 'UC' + 'A' * 22}
                validate_operation(canonical({'version':1, 'operation':item['id'], 'query':q,
                                              'parameters':item['parameters_example']}), CONTEXT)

    def test_contracts_do_not_grant_disabled_operations(self):
        context = AccessContext('test', 'agent', enabled=True)
        self.assertEqual(contracts(context, 'operations')['operations'], [])
        with self.assertRaises(QueryRejected): contracts(context, 'operations', 'video.summary.v1')
        self.assertEqual(contracts(context, 'query')['schema'], QueryRequest.model_json_schema())
        self.assertEqual(contracts(context, 'canvas')['graph_version'], 2)
        self.assertNotIn('summary', contracts(context, 'canvas')['node_types'])
        validate_graph(canonical(contracts(context, 'canvas')['example']), context)

    def test_live_fields_have_units_states_and_supported_operators(self):
        fields = {f['id']: f for f in describe(CONTEXT)['fields']}
        self.assertEqual(fields['video.duration_seconds']['unit'], 'seconds')
        self.assertTrue(fields['video.views']['empty_meaning'])
        channel_fields = {f['id'] for f in describe(CONTEXT, 'public.channels.current.v1')['fields']}
        self.assertNotIn('video.views', channel_fields)
        self.assertIn('channel.subscribers', channel_fields)

    def test_source_search_closed_identifiers_paging_and_values(self):
        database = Mock()
        database.read_rows.return_value = [{'id':'a','name':'A'}, {'id':'b','name':'B'}]
        repository = ResearchSourcesRepository(database)
        value = repository.search('folder', "'; DROP TABLE x;--", 1)
        args, kw = database.read_rows.call_args
        self.assertNotIn('DROP TABLE', args[1])
        self.assertEqual(args[2], ('', "'; DROP TABLE x;--", 2))
        self.assertEqual(kw['max_rows'], 2)
        self.assertEqual(value['next_cursor'], 'a')
        self.assertEqual(value['items'][0]['scope'], {'kind':'folder','id':'a'})
        with self.assertRaises(ValueError): repository.search('public.secrets', '', 20)


if __name__ == '__main__': unittest.main()
