"""Schema21 exact rights, coupled readiness and immutable source boundaries."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.readiness import capabilities,inventory_receipt_capabilities as receipt,operational_retention_capabilities as operational,video_failure_capabilities as queue
from db.base.postgres import DatabaseError,_reviewed_statement
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

class Facts:
    def __init__(self,value):self.value=value;self.calls=[]
    def read_row(self,*args):self.calls.append(args);return self.value

class DirectInventoryRetentionTests(unittest.TestCase):
    def test_new_fixed_catalog_owners_reject_all_negative_or_malformed_facts(self):
        good=dict(roles_valid=True,relations_valid=True,privileges_valid=True)
        for module,method in ((receipt,receipt.assert_inventory_receipts_ready),(operational,operational.assert_operational_retention_ready)):
            database=Facts(good);self.assertEqual(method(database),dict(ready=True,schema_version=23 if module is receipt else 21));self.assertEqual(len(database.calls),1)
            _reviewed_statement(module.READINESS_SQL,read=True,max_rows=1)
            PostgresQuery(Transformer()).convert(module.READINESS_SQL,())
            for key in good:
                with self.assertRaises(DatabaseError) as caught:method(Facts({**good,key:False}))
                self.assertEqual(caught.exception.code,'P0001')
            for row in (None,{},[],{**good,'extra':True},{**good,'roles_valid':1}):
                with self.assertRaisesRegex(DatabaseError,'database_result_shape_invalid'):method(Facts(row))
    def test_ledger_existing_writes_plus_delete_have_no_extra_authority(self):
        sql=operational.READINESS_SQL
        self.assertIn("('youtube_api_request_attempts',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])",sql)
        self.assertIn('SELECT NULL::text,NULL::text,NULL::text WHERE false',sql)
        for token in ('aclexplode(a.attacl)','acl.is_grantable','has_column_privilege','NOT rolcanlogin','relrowsecurity'):self.assertIn(token,sql)
    def test_initializer_literal_column_lists_exactly_match_grants(self):
        from db.videos import video_queue_initialization as initializer
        insert=set();updates=set()
        for key in ('_LONG_UPSERT','_TYPED_INSERT'):
            sql=getattr(initializer,key)
            matched=re.search(r'INSERT INTO public.video_collection_state(?: AS state)?\s*\((.*?)\)',sql,re.S)
            insert.update(x.strip() for x in matched[1].split(','))
            if 'DO UPDATE SET' in sql:
                segment=sql.split('DO UPDATE SET',1)[1].split('WHERE state.lease_token',1)[0]
                updates.update(re.findall(r'(?:^|,)\s*([a-z_]+)=',segment))
        self.assertEqual(insert,set(queue.INSERT_COLUMNS))
        self.assertEqual(updates,set(queue.UPDATE_COLUMNS)&updates)
        self.assertEqual(len(updates),12)
        self.assertNotIn('video_id',queue.UPDATE_COLUMNS);self.assertNotIn('lease_token',queue.INSERT_COLUMNS)
    def test_typed_receipt_delete_is_exact_exception_in_mirror(self):
        mirror=capabilities._ASSERT_TYPED_VIDEO_COLLECTION_SCHEMA_SQL
        for source,prefix in ((mirror,'expected.'),):
            self.assertIn("IS DISTINCT FROM ("+prefix+"v_relation='typed_video_collection_receipts')",source)
            self.assertIn("('typed_video_collection_receipts',ARRAY['SELECT','DELETE']::text[])",source)
            self.assertIn("('typed_video_collection_receipts','recorded_at','UPDATE')",source)
            self.assertIn('direct_typed_receipt_rights',source)
            self.assertIn('acl.is_grantable',source)
        self.assertEqual(capabilities._ERRORS['assert_typed_video_collection_schema'][1],'typed_video_collection_relation_security_contract_invalid: %')
    def test_hot_uses_only_fixed_new_capability_names(self):
        self.assertEqual(capabilities.MAINTENANCE_CAPABILITY_QUERIES['inventory_receipts'],receipt.READINESS_SQL)
        self.assertEqual(capabilities.MAINTENANCE_CAPABILITY_QUERIES['operational_retention'],operational.READINESS_SQL)
        from test_capabilities import FactsDatabase
        for name,token in (('inventory_receipts','inventory_receipts_storage_acl_invalid'),('operational_retention','operational_retention_acl_invalid')):
            for key in ('roles_valid','relations_valid','privileges_valid'):
                db=FactsDatabase();db.overrides[name]={x:x!=key for x in ('roles_valid','relations_valid','privileges_valid')}
                with self.assertRaisesRegex(DatabaseError,token):capabilities.get_hot_runtime_contract(db)
                self.assertEqual(db.calls[-1],('maintenance',name))
    def test_current_owner_is_python(self):
        current=(ROOT/'schema/schema.sql').read_text()
        self.assertIn('GRANT INSERT (video_id,channel_id,stats_due_at,metadata_due_at',current)
        self.assertNotIn('SELECT public.assert_typed_video_collection_schema();',current)

if __name__=='__main__':unittest.main()
