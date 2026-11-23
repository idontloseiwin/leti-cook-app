"""The browser's workshop block registry and the server's operation blocks are one list.

tests/fixtures/research_operation_blocks.json is pinned here against graph.OPERATION_TYPES (the server
derives calculation blocks from its operation registry) and in tests/test_workshop_blocks.mjs against the
browser registry (workshopModel.ts). A new operation with a block fails one side until both know it."""
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from research_query.graph import NODE_TYPES, OPERATION_TYPES, OUTPUT_TYPES, ROW_TYPES

FIXTURE = pathlib.Path(__file__).with_name('fixtures') / 'research_operation_blocks.json'


class OperationBlockRegistryTests(unittest.TestCase):
    def test_fixture_equals_server_operation_blocks(self):
        self.assertEqual(json.loads(FIXTURE.read_text(encoding='utf-8')), dict(OPERATION_TYPES))

    def test_block_types_known_to_the_browser(self):
        # researchGraph.NODE_KINDS / OUTPUT_KINDS / ROW_KINDS in the browser.
        self.assertEqual(NODE_TYPES, {'source', 'filter', 'selection', 'table', 'gallery', *OPERATION_TYPES})
        self.assertEqual(OUTPUT_TYPES, {'table', 'gallery', *OPERATION_TYPES})
        self.assertEqual(ROW_TYPES, {'table', 'gallery'})


if __name__ == '__main__':
    unittest.main()
