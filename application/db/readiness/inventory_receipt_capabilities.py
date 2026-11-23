"""Exact migration23 page/failure receipt and retention storage authority.

Nine INSERT columns, three UPDATE columns and DELETE; no table-wide write,
source-clock UPDATE, receipt identity UPDATE or unlisted INSERT authority.
"""
from .capability_facts import require_boolean_facts, TABLE_COLUMN_ACL_SQL_SUFFIX

INSERT_COLUMNS = ('receipt_scope', 'transition_id', 'scan_id', 'transition_kind', 'result', 'source_checked_at', 'created_at', 'next_page_token_hash', 'claims')
UPDATE_COLUMNS = ('claims', 'result', 'next_page_token_hash')

READINESS_SQL = """
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_inventory_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_inventory_receipts','receipt_scope','INSERT'),
 ('typed_inventory_receipts','transition_id','INSERT'),
 ('typed_inventory_receipts','scan_id','INSERT'),
 ('typed_inventory_receipts','transition_kind','INSERT'),
 ('typed_inventory_receipts','result','INSERT'),
 ('typed_inventory_receipts','source_checked_at','INSERT'),
 ('typed_inventory_receipts','created_at','INSERT'),
 ('typed_inventory_receipts','next_page_token_hash','INSERT'),
 ('typed_inventory_receipts','claims','INSERT'),
 ('typed_inventory_receipts','claims','UPDATE'),
 ('typed_inventory_receipts','result','UPDATE'),
 ('typed_inventory_receipts','next_page_token_hash','UPDATE')
""" + TABLE_COLUMN_ACL_SQL_SUFFIX

def validate_readiness(row):
    require_boolean_facts(row, error_message='inventory_receipts_storage_acl_invalid')
    return {'ready':True,'schema_version':23}


def assert_inventory_receipts_ready(database):
    return validate_readiness(database.read_row('inventory_receipt_readiness',READINESS_SQL))
