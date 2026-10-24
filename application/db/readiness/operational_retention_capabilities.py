"""Exact migration21 operational ledger retention authority, one catalog row.

Existing SELECT/INSERT/UPDATE plus DELETE; no MAINTAIN, TRUNCATE, REFERENCES,
TRIGGER, extra-role grants or grant options. No runtime/schema mutation.
"""
from .capability_facts import require_boolean_facts, TABLE_COLUMN_ACL_SQL_SUFFIX

READINESS_SQL = """
WITH required_tables(table_name,privileges) AS (VALUES
 ('youtube_api_request_attempts',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (SELECT NULL::text,NULL::text,NULL::text WHERE false
""" + TABLE_COLUMN_ACL_SQL_SUFFIX

def validate_readiness(row):
    require_boolean_facts(row, error_message='operational_retention_acl_invalid')
    return {'ready':True,'schema_version':21}


def assert_operational_retention_ready(database):
    return validate_readiness(database.read_row('operational_retention_readiness',READINESS_SQL))
