"""Exact collection claim/receipt rights and immutable upgrade boundaries."""
from pathlib import Path
import re,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import inventory_receipt_capabilities as receipt,video_failure_capabilities as queue,capabilities
from db.base.postgres import _reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

class ReceiptCapabilitiesTests(unittest.TestCase):
    def test_nine_insert_three_update_and_delete_exact_native_receipt_rights(self):
        self.assertEqual(receipt.INSERT_COLUMNS,('receipt_scope','transition_id','scan_id','transition_kind','result','source_checked_at','created_at','next_page_token_hash','claims'))
        self.assertEqual(re.findall(r"\('typed_inventory_receipts','([^']+)','INSERT'\)",receipt.READINESS_SQL),list(receipt.INSERT_COLUMNS))
        self.assertIn("('typed_inventory_receipts',ARRAY['SELECT','DELETE']::text[])",receipt.READINESS_SQL)
        self.assertEqual(receipt.UPDATE_COLUMNS,('claims','result','next_page_token_hash'))
        self.assertIn('has_column_privilege',receipt.READINESS_SQL)
        self.assertIn('aclexplode(a.attacl)',receipt.READINESS_SQL)
        self.assertIn('acl.is_grantable',receipt.READINESS_SQL)
    def test_one_native_catalog_query_checks_role_owner_rls_and_raw_effective_acls(self):
        _reviewed_statement(receipt.READINESS_SQL,read=True,max_rows=1)
        PostgresQuery(Transformer()).convert(receipt.READINESS_SQL,())
        self.assertNotIn('::regclass',receipt.READINESS_SQL)
        for fact in ('roles.valid AS roles_valid','NOT rolsuper','NOT rolcanlogin','relrowsecurity',"<>'postgres'",'IS DISTINCT FROM'):
            self.assertIn(fact,receipt.READINESS_SQL)
    def test_schema_and_python_readiness_have_same_receipt_rights(self):
        schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
        for source in (schema,capabilities._ASSERT_INVENTORY_RECEIPTS_SCHEMA_SQL):
            self.assertIn(receipt.READINESS_SQL.strip(),source)
        self.assertIn("'service_direct_writes',true",capabilities._ASSERT_INVENTORY_RECEIPTS_SCHEMA_SQL)
        self.assertNotIn("CREATE FUNCTION public.assert_inventory_receipts_schema(",schema)
        self.assertIn('NOT coalesce((SELECT roles_valid AND relations_valid AND privileges_valid',capabilities._ASSERT_INVENTORY_RECEIPTS_SCHEMA_SQL)
    def test_guards_follow_the_new_grants_in_the_fresh_schema(self):
        schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
        new=schema.index('GRANT INSERT (video_id,channel_id,stats_due_at,metadata_due_at')
        self.assertGreater(schema.index('DO $direct_initializer_acl$'),new)
        self.assertTrue(queue.READINESS_SQL.strip() in schema[new:])
        self.assertEqual(queue.UPDATE_COLUMNS[14:20],('channel_id','stats_source_checked_at','metadata_source_checked_at','policy_version','hydration_needed','paused_reason'))
    def test_actual_repository_accepts_only_new_explicit_receipt_writer_contract(self):
        from unittest.mock import patch
        from db.inventory.typed_inventory import TypedInventoryRepository
        repository=object.__new__(TypedInventoryRepository)
        repository.database=object()
        repository._execute=lambda operation,callback:callback()
        good=dict(status='ok',inventory_receipt_scopes=2,service_direct_writes=True)
        with patch('db.inventory.typed_inventory.capabilities.assert_inventory_receipts_schema',return_value=good):
            self.assertEqual(repository.assert_receipts_ready(),good)
        for bad in (False,None,1):
            with patch('db.inventory.typed_inventory.capabilities.assert_inventory_receipts_schema',return_value=dict(good,service_direct_writes=bad)):
                with self.assertRaisesRegex(RuntimeError,'inventory receipts schema is not ready'):repository.assert_receipts_ready()

    def test_regression_sql_uses_actual_service_role_and_rolls_back(self):
        fixture=(ROOT/'tests/sql/05_collection_claim_acl.sql').read_text(encoding='utf-8')
        self.assertIn(receipt.READINESS_SQL.strip(),fixture)
        self.assertIn('SET LOCAL ROLE service_role;',fixture)
        self.assertIn('EXCEPTION WHEN insufficient_privilege THEN NULL;',fixture)
        self.assertNotRegex(fixture,r'(?im)^GRANT |^REVOKE |^COMMIT;')
        self.assertTrue(fixture.rstrip().endswith('rollback;'))

if __name__=='__main__':unittest.main()
