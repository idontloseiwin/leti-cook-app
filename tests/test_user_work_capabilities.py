"""Offline exact ACL/readiness contracts for the prepared migration17."""
from pathlib import Path
from collections.abc import Mapping
import hashlib
import json
import sys
import unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import user_work_capabilities as checks
from db.base.postgres import DatabaseError,_reviewed_statement


class Database:
    def __init__(self,row):self.row=row;self.calls=[]
    def read_row(self,operation,statement,params=()):
        self.calls.append((operation,statement,params));return self.row


class ReadinessTests(unittest.TestCase):
    def test_one_fixed_bounded_catalog_read_with_exact_shape(self):
        db=Database({'roles_valid':True,'relations_valid':True,'privileges_valid':True})
        self.assertEqual(checks.assert_user_work_restore_ready(db),{'ready':True,'schema_version':24})
        self.assertEqual(db.calls,[('user_work_restore_readiness',checks.READINESS_SQL,())])
        _reviewed_statement(checks.READINESS_SQL,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(checks.READINESS_SQL,())
        self.assertNotIn('information_schema',checks.READINESS_SQL)
        self.assertNotRegex(checks.READINESS_SQL,r'public\.(assert_|get_)')

    def test_each_missing_or_extra_capability_fails_closed_with_safe_error(self):
        for key in ('roles_valid','relations_valid','privileges_valid'):
            row={field:field!=key for field in ('roles_valid','relations_valid','privileges_valid')}
            with self.assertRaisesRegex(DatabaseError,'user_work_restore_acl_invalid') as caught:
                checks.assert_user_work_restore_ready(Database(row))
            self.assertEqual(caught.exception.code,'P0001');self.assertFalse(caught.exception.outcome_unknown)

    def test_null_numeric_extra_or_missing_facts_are_not_truthy_admission(self):
        good={'roles_valid':True,'relations_valid':True,'privileges_valid':True}
        for row in (None,[],{},[True]*3,{**good,'private':'secret'},
                    {**good,'privileges_valid':1},{**good,'relations_valid':None}):
            with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid') as caught:
                checks.validate_readiness(row)
            self.assertNotIn('secret',str(caught.exception))

    def test_effective_and_raw_acls_are_both_checked_without_missing_oid_casts(self):
        query=checks.READINESS_SQL
        for requirement in ('has_table_privilege','has_column_privilege','aclexplode(a.attacl)',
                            'acl.is_grantable','relrowsecurity','relkind',"NOT rolsuper",'NOT rolcanlogin',
                            'required.column_name=a.attname','pg_catalog.to_regclass','IS DISTINCT FROM'):
            self.assertIn(requirement,query)
        self.assertNotIn('::regclass',query)
        self.assertIn("('video_type_review_history',ARRAY['SELECT','MAINTAIN','DELETE']::text[])",query)
        self.assertIn("('video_type_review_cases',ARRAY['SELECT','MAINTAIN','DELETE']::text[])",query)


class TransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8')
        cls.fixture=(ROOT/'tests/sql/02_user_work_restore_acl.sql').read_text(encoding='utf-8')

    def test_identical_acl_predicate_in_schema_and_regression(self):
        for source in (self.schema,self.fixture):
            self.assertIn(checks.READINESS_SQL.strip(),source)
            self.assertIn("RAISE EXCEPTION 'user_work_restore_acl_invalid'",source)

    def test_review_and_history_insert_columns(self):
        self.assertEqual(len(checks.REVIEW_INSERT_COLUMNS),19)
        self.assertEqual(len(checks.HISTORY_INSERT_COLUMNS),7)
        self.assertEqual(checks.REVIEW_INSERT_COLUMNS[-1],'user_channel_ref')
        self.assertNotIn('user_channel_ref',checks.REVIEW_INSERT_COLUMNS[:-1])

    def test_manifest_and_sidecar_match_the_schema(self):
        manifest=json.loads((ROOT/'schema/manifest.json').read_text(encoding='utf-8'))
        schema_hash=hashlib.sha256((ROOT/'schema/schema.sql').read_bytes()).hexdigest()
        self.assertEqual(manifest['files']['schema.sql'],schema_hash)
        self.assertEqual((ROOT/'schema/schema.sql.sha256').read_text(encoding='utf-8').strip(),schema_hash+'  schema.sql')

    def test_fixture_denies_unused_rights_without_dml_or_persistent_acl_mutation(self):
        self.assertTrue(self.fixture.startswith('-- User-work ACL'))
        self.assertIn('SET LOCAL ROLE service_role;',self.fixture)
        self.assertIn('IN SHARE ROW EXCLUSIVE MODE NOWAIT;',self.fixture)
        self.assertIn('EXCEPTION WHEN insufficient_privilege THEN NULL;',self.fixture)
        self.assertIn("RAISE EXCEPTION 'user_work_restore_forbidden_write_succeeded'",self.fixture)
        self.assertNotRegex(self.fixture,r'(?im)^GRANT |^REVOKE |^COMMIT;')
        self.assertTrue(self.fixture.rstrip().lower().endswith('rollback;'))


if __name__=='__main__':unittest.main()
