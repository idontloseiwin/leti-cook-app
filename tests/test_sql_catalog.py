"""docs/SQL_CATALOG.md matches schema/schema.sql, and every source pin matches its function."""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import sql_catalog  # noqa: E402


class SqlCatalogTests(unittest.TestCase):
    def test_the_catalogue_is_current(self):
        relations, functions, _ = sql_catalog.build()
        self.assertEqual((ROOT / 'docs/SQL_CATALOG.md').read_bytes().decode('utf-8'),
                         sql_catalog.render(relations, functions),
                         'docs/SQL_CATALOG.md is out of date: run python3 tools/sql_catalog.py --write')

    def test_every_object_is_listed(self):
        schema = (ROOT / 'schema/schema.sql').read_bytes().decode('utf-8')
        relations, functions, _ = sql_catalog.build()
        self.assertEqual(len(functions), len(re.findall(r'(?m)^CREATE FUNCTION ', schema)))
        self.assertEqual(sum(r.kind == 'table' for r in relations), len(re.findall(r'(?m)^CREATE TABLE ', schema)))
        self.assertEqual(sum(r.kind == 'view' for r in relations), len(re.findall(r'(?m)^CREATE VIEW ', schema)))

    def test_every_source_pin_matches_its_function(self):
        _, functions, problems = sql_catalog.build()
        self.assertEqual(problems, [])
        self.assertEqual(len(functions),13)
        self.assertTrue(all(f.pinned_by=={'db.readiness.intrinsic_capabilities'} for f in functions))

    def test_a_changed_pinned_body_is_reported(self):
        schema = (ROOT / 'schema/schema.sql').read_bytes()
        marker = b'CREATE FUNCTION public.bind_channel_user_work_owner('
        body = schema.index(b'AS $$', schema.index(marker)) + len(b'AS $$')
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory, 'schema.sql')
            changed.write_bytes(schema[:body] + b'\n-- changed' + schema[body:])
            with mock.patch.object(sql_catalog, 'SCHEMA', changed):
                _, _, problems = sql_catalog.build()
        self.assertTrue(problems)
        self.assertTrue(all('public.bind_channel_user_work_owner()' in p for p in problems), problems)

    def test_full_current_source_inventory_needs_no_git(self):
        with mock.patch.object(sql_catalog.subprocess,'run',side_effect=AssertionError('git forbidden')):
            files=sql_catalog.python_source_inventory()
            _, functions, problems=sql_catalog.build()
        self.assertIn('application/db/readiness/intrinsic_capabilities.py',files)
        self.assertIn('tools/sql_catalog.py',files)
        self.assertTrue(all(not name.startswith('tests/') for name in files))
        expected={p.relative_to(ROOT).as_posix() for p in (ROOT/'application/db').rglob('*.py')}
        self.assertTrue(expected <= set(files))
        self.assertEqual(len(functions),13);self.assertEqual(problems,[])

    def test_view_grants_without_optional_table_keyword_are_recorded_per_role(self):
        schema = (ROOT/'schema/schema.sql').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory, 'schema.sql')
            changed.write_bytes(schema + b'\nGRANT SELECT ON readable.videos, readable.channels TO lic_reader;\n')
            with mock.patch.object(sql_catalog, 'SCHEMA', changed):
                relations, _, _ = sql_catalog.build()
        grants = {(row.schema, row.name): row.grants for row in relations}
        self.assertEqual(grants['readable', 'videos']['lic_reader'], 'select')
        self.assertEqual(grants['readable', 'channels']['lic_reader'], 'select')
        self.assertEqual(grants['readable', 'videos']['service_role'], 'select')


if __name__ == '__main__':
    unittest.main()
