"""Reviewed contract snapshot, fail-closed lookup and SQL-registry independence."""
import ast
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.base.operation_contracts import OPERATIONS, get_operation


class PythonOperationContracts(unittest.TestCase):
    def test_operation_contracts_match_the_reviewed_snapshot(self):
        # Any change to an operation's limits or error codes must update this snapshot.
        expected = json.loads((ROOT / 'tests/fixtures/operation_contracts.json').read_text(encoding='utf8'))
        actual = {name: dict(name=op.name, read_only=op.read_only,
                  statement_timeout_ms=op.statement_timeout_ms, lock_timeout_ms=op.lock_timeout_ms,
                  error_codes=sorted(op.error_codes)) for name, op in OPERATIONS.items()}
        self.assertEqual(len(actual), 93)
        self.assertEqual(actual, expected)
        for op in OPERATIONS.values():
            totals={'metadata_candidate_page':15000,'capture_metadata_candidates':15000,'complete_initial_video_batch':60000,'operator_candidate_page':15000,'claim_operator_job_id':5000,'drain_hot_collection_claims':5000,'abort_hot_reconnect_claim':5000,'drain_hot_channel_metadata':5000,'inventory_candidate_page':15000}
            self.assertEqual(op.total_timeout_ms,totals.get(op.name))
            self.assertFalse(op.repeatable_read)
            self.assertFalse(op.cache_statement_timeout)
            self.assertFalse(hasattr(op, 'statement'))

    def test_shared_scan_operation_includes_the_importing_page_writer(self):
        from db.inventory.inventory_scan_pages import _operation
        expected = get_operation('settle_typed_inventory_page')
        actual = _operation(expected.name)
        self.assertEqual(actual.name, expected.name)
        self.assertEqual(actual.statement_timeout_ms, expected.statement_timeout_ms)
        self.assertEqual(actual.lock_timeout_ms, expected.lock_timeout_ms)
        self.assertEqual(actual.error_codes, expected.error_codes)

    def test_unknown_unhashable_or_sql_looking_names_fail_closed(self):
        for value in ('absent_operation', 'public.cleanup_hot_current_source()', 'select 1', None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '^database_operation_not_registered$'):
                get_operation(value)

    def test_contracts_cannot_change_in_place(self):
        name = next(iter(OPERATIONS))
        with self.assertRaises(TypeError):
            OPERATIONS[name] = None
        with self.assertRaises(FrozenInstanceError):
            get_operation(name).statement_timeout_ms = 1

    def test_repositories_import_without_sql_function_registry(self):
        code = ("import importlib,sys;sys.path.insert(0,sys.argv[1]);"
                "sys.modules['db.function_contracts']=None;import db;"
                "from pathlib import Path;"
                "[importlib.import_module('.'.join(p.relative_to(sys.argv[1]).with_suffix('').parts)) for p in Path(sys.argv[1],'db').rglob('*.py') "
                "if p.stem not in ('__init__','function_contracts')]")
        result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'application')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_runtime_module_can_import_legacy_sql_registry(self):
        for path in (ROOT / 'application/db').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, 'function_contracts', str(path))
                if isinstance(node, ast.Name):
                    self.assertNotEqual(node.id, 'get_function', str(path))


if __name__ == '__main__':
    unittest.main()
