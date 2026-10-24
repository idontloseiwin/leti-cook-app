"""No public repository path can fall back to a retained SQL workflow."""
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.channel.channel_sync import ChannelSyncRepository
from db.jobs.operator_jobs import OperatorJobRepository
from db.inventory.typed_inventory import TypedInventoryRepository
from db.videos.video_collection import VideoCollectionRepository
from db.base.repository import DatabaseRepository


class DispatchClosure(unittest.TestCase):
    def test_unknown_operations_never_reach_database(self):
        for cls in (OperatorJobRepository, TypedInventoryRepository, VideoCollectionRepository):
            with self.subTest(repository=cls.__name__):
                database = Mock()
                with self.assertRaisesRegex(ValueError, '^database_operation_not_registered$'):
                    cls(database)._rpc('unregistered_workflow', {})
                self.assertEqual(database.mock_calls, [])

    def test_unused_entrypoints_removed(self):
        self.assertFalse(hasattr(ChannelSyncRepository, '_rpc'))
        self.assertFalse(hasattr(DatabaseRepository, '_telemetry_rpc'))

    def test_every_public_dispatch_has_a_named_python_branch(self):
        # F6 adds the exact operator candidate/claim branches and advisory inventory page.
        for filename, count in (('jobs/operator_jobs.py', 23), ('inventory/typed_inventory.py', 15), ('videos/video_collection.py', 14)):
            with self.subTest(filename=filename):
                tree = ast.parse((ROOT / 'application/db' / filename).read_bytes())
                branches, called = set(), set()
                wrappers = {'_complete', '_complete_typed'} if filename == 'videos/video_collection.py' else set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == 'name':
                        for value in node.comparators:
                            values = value.elts if isinstance(value, (ast.Tuple, ast.List, ast.Set)) else [value]
                            self.assertTrue(all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in values))
                            branches.update(item.value for item in values)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self' and node.func.attr in {'_rpc'} | wrappers:
                        value = node.args[0]
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            called.add(value.value)
                        else:
                            self.assertEqual(filename, 'videos/video_collection.py')
                            self.assertIsInstance(value, ast.Name)
                            self.assertEqual(value.id, 'rpc_name')
                self.assertEqual(len(branches), count)
                self.assertEqual(called, branches)

    def test_repository_source_has_no_native_workflow_fallback(self):
        for path in (ROOT / 'application/db').rglob('*.py'):
            if path.name == 'postgres.py':
                # Transitional driver stays for isolated SQL parity tooling;
                # installed workflows no longer dispatch through this entry.
                continue
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, ('call', 'call_function', '_telemetry_rpc'), str(path))


if __name__ == '__main__':
    unittest.main()
