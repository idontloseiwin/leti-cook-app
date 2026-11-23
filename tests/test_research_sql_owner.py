"""The research backend builds no SQL outside application/db/ (CONTRIBUTING.md: database queries in db/).

research_query holds what queries and operations mean; db/reads/research_sql.py and
db/reads/research_operations/ hold the database text. A SQL fragment in a string
constant under research_query fails this test (docstrings may describe SQL).
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application')]
SQL = re.compile(r'\bSELECT\b[\s\S]{0,400}\bFROM\b|\bjsonb_build_object\b|\bMATERIALIZED\b|\bINSERT INTO\b'
                 r'|\bUPDATE public\.|\bDELETE FROM\b|\bCASE WHEN\b')


def sql_constants(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body
                  and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and SQL.search(node.value)]


class ResearchSqlOwnerTests(unittest.TestCase):
    def test_research_query_has_no_sql_text(self):
        found = {str(path.relative_to(ROOT)): lines for path in sorted((ROOT / 'application/research_query').rglob('*.py'))
                 if (lines := sql_constants(path))}
        self.assertEqual(found, {})

    def test_the_probe_sees_sql_where_it_lives(self):
        # Guards the guard: the patterns do match the moved operation SQL.
        self.assertTrue(sql_constants(ROOT / 'application/db/reads/research_operations/summary.py'))
        self.assertTrue(sql_constants(ROOT / 'application/db/reads/research_operations/facets.py'))

    def test_every_registered_operation_has_its_sql_in_db(self):
        from db.reads.research_operations import COMPILERS
        from research_query.operation_kinds import OPERATION_KINDS
        self.assertEqual(set(COMPILERS), set(OPERATION_KINDS))
        for identity, kind in OPERATION_KINDS.items():
            with self.subTest(operation=identity):
                self.assertFalse(hasattr(kind, 'compile'))
                self.assertTrue(COMPILERS[identity].__module__.startswith('db.reads.research_operations.'))


if __name__ == '__main__':
    unittest.main()
