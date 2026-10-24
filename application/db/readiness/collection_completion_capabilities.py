"""Exact schema24 runtime/control progress writes, one finite catalog read.

Requires postgres ownership, RLS, SELECT, three runtime UPDATE columns and four control
UPDATE columns. Extra effective/raw grants or grant options fail closed. No user rows,
activation authority or generic maintenance SQL.
"""
from .capability_facts import require_boolean_facts, TABLE_COLUMN_ACL_SQL_SUFFIX

RUNTIME_UPDATE_COLUMNS = ('reconcile_queue_cursor','reconcile_inventory_cursor','updated_at')
CONTROL_UPDATE_COLUMNS = ('reconciliation_required', 'last_reconciled_at', 'updated_at', 'invalidated_at')

READINESS_SQL = """
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_video_collection_runtime',ARRAY['SELECT']::text[]),
 ('video_collection_control',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_video_collection_runtime','reconcile_queue_cursor','UPDATE'),
 ('typed_video_collection_runtime','reconcile_inventory_cursor','UPDATE'),
 ('typed_video_collection_runtime','updated_at','UPDATE'),
 ('video_collection_control','reconciliation_required','UPDATE'),
 ('video_collection_control','last_reconciled_at','UPDATE'),
 ('video_collection_control','updated_at','UPDATE'),
 ('video_collection_control','invalidated_at','UPDATE')
""" + TABLE_COLUMN_ACL_SQL_SUFFIX

def validate_readiness(row):
    require_boolean_facts(row, error_message='collection_completion_acl_invalid')
    return {'ready':True,'schema_version':24}


def assert_collection_completion_ready(database):
    return validate_readiness(database.read_row('collection_completion_readiness',READINESS_SQL))
