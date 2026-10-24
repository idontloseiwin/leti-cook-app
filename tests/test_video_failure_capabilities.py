"""Offline exact ACL/readiness contracts for the prepared migration23."""
from pathlib import Path
from collections.abc import Mapping
import sys
import unittest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import video_failure_capabilities as checks
from db.base.postgres import DatabaseError,_reviewed_statement


class Database:
    def __init__(self,row):self.row=row;self.calls=[]
    def read_row(self,operation,statement,params=()):
        self.calls.append((operation,statement,params));return self.row


class ReadinessTests(unittest.TestCase):
    def test_one_fixed_bounded_catalog_read_with_exact_shape(self):
        db=Database({'roles_valid':True,'relations_valid':True,'privileges_valid':True})
        self.assertEqual(checks.assert_video_failure_ready(db),{'ready':True,'schema_version':23})
        self.assertEqual(db.calls,[('video_failure_readiness',checks.READINESS_SQL,())])
        _reviewed_statement(checks.READINESS_SQL,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(checks.READINESS_SQL,())
        self.assertNotIn('information_schema',checks.READINESS_SQL)
        self.assertNotRegex(checks.READINESS_SQL,r'public\.(assert_|get_)')

    def test_each_missing_or_extra_capability_fails_closed_with_safe_error(self):
        for key in ('roles_valid','relations_valid','privileges_valid'):
            row={field:field!=key for field in ('roles_valid','relations_valid','privileges_valid')}
            with self.assertRaisesRegex(DatabaseError,'video_failure_acl_invalid') as caught:
                checks.assert_video_failure_ready(Database(row))
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
        self.assertIn("('video_collection_state',ARRAY['SELECT','DELETE']::text[])",query)


class TransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema=(ROOT/'schema/schema.sql').read_bytes().decode('utf-8').replace('\r\n','\n')
        cls.fixture=(ROOT/'tests/sql/03_video_failure_acl.sql').read_text(encoding='utf-8')

    def test_acl_predicate_identical_in_schema_and_regression(self):
        for source in (self.schema,self.fixture):
            self.assertIn(checks.READINESS_SQL.strip(),source)
            self.assertIn("RAISE EXCEPTION 'video_failure_acl_invalid'",source)

    def test_update_column_count(self):
        self.assertEqual(len(checks.UPDATE_COLUMNS),32)

    def test_canonical_repair_mirror_preserves_private_lock_and_exact_base_rights(self):
        from db.readiness import capabilities
        sql=capabilities._ASSERT_TYPED_INVENTORY_CANONICAL_REPAIR_SCHEMA_SQL
        for source in (sql,):
            self.assertIn(checks.READINESS_SQL.strip(),source)
            self.assertIn('aclexplode(a.attacl)',source)
            self.assertIn('video_collection_state_canonical_repair_lock',source)
            self.assertIn("'maintain'",source)
            for column in checks.UPDATE_COLUMNS:self.assertIn("'"+column+"'",source)
        for source in (capabilities._ASSERT_CHANNEL_SYNC_CONTROL_READY_SQL,):
            self.assertEqual(source.count("ARRAY['owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id']"),2)
        self.assertEqual(capabilities.MAINTENANCE_CAPABILITY_QUERIES['video_failure'],checks.READINESS_SQL)

    def test_fixture_is_rollback_only_and_rejects_unrelated_dml(self):
        self.assertIn('SET LOCAL ROLE service_role;',self.fixture)
        self.assertIn('EXCEPTION WHEN insufficient_privilege THEN NULL;',self.fixture)
        self.assertNotRegex(self.fixture,r'(?im)^GRANT |^REVOKE |^COMMIT;')
        self.assertTrue(self.fixture.rstrip().lower().endswith('rollback;'))


if __name__=='__main__':unittest.main()
