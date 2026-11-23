"""Minimal evidence ACL transition and fail-closed catalog readiness."""
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import evidence_retention_capabilities as checks
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

class EvidenceReadinessTests(unittest.TestCase):
    def test_exact_boolean_facts_fail_closed(self):
        good=dict(roles_valid=True,relations_valid=True,privileges_valid=True)
        self.assertEqual(checks.validate_readiness(good),dict(ready=True,schema_version=23))
        for key in good:
            with self.assertRaisesRegex(DatabaseError,'evidence_retention_acl_invalid'):checks.validate_readiness(dict(good,**{key:False}))
        for value in (None,{},dict(good,privileges_valid=1),dict(good,relations_valid=None),dict(good,secret='private')):
            with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):checks.validate_readiness(value)
    def test_query_is_fixed_native_bounded_and_no_stored_check(self):
        _reviewed_statement(checks.READINESS_SQL,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(checks.READINESS_SQL,())
        for term in ('has_table_privilege','has_column_privilege','aclexplode(a.attacl)','acl.is_grantable','relrowsecurity','NOT rolsuper','NOT rolcanlogin'):
            self.assertIn(term,checks.READINESS_SQL)
        self.assertNotIn('::regclass',checks.READINESS_SQL)
        self.assertNotRegex(checks.READINESS_SQL,r'public\.(assert_|get_)')
    def test_identical_current_guard_in_schema_and_sql_fixture(self):
        for path in ('schema/schema.sql','tests/sql/04_evidence_retention_acl.sql'):
            self.assertIn(checks.READINESS_SQL.strip(),(ROOT/path).read_text(encoding='utf-8'))
    def test_sql_fixture_is_rollback_only_and_denies_manual_decision_writes(self):
        source=(ROOT/'tests/sql/04_evidence_retention_acl.sql').read_text(encoding='utf-8')
        self.assertIn('SET LOCAL ROLE service_role;',source)
        self.assertIn('first_seen_at=first_seen_at WHERE false',source)
        self.assertNotRegex(source,r'(?im)^GRANT |^REVOKE |^COMMIT;')
        self.assertTrue(source.rstrip().endswith('rollback;'))
if __name__=='__main__':unittest.main()
