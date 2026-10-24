"""Exact schema23 evidence and typed-completion receipt authority.

Review evidence column rights are owned by user_work_capabilities. This query
checks snapshot storage and eight receipt INSERT columns; no rows or writes occur.
"""
from .capability_facts import require_boolean_facts, TABLE_COLUMN_ACL_SQL_SUFFIX

RECEIPT_INSERT_COLUMNS = ('video_id', 'collection_attempt_id', 'channel_id', 'video_type', 'lease_token', 'receipt_kind', 'recorded_at', 'payload_fingerprint')

READINESS_SQL = """
WITH required_tables(table_name,privileges) AS (VALUES
 ('channel_snapshots',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('typed_video_collection_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('channel_snapshots','recorded_at','UPDATE'),
 ('typed_video_collection_receipts','video_id','INSERT'),
 ('typed_video_collection_receipts','collection_attempt_id','INSERT'),
 ('typed_video_collection_receipts','channel_id','INSERT'),
 ('typed_video_collection_receipts','video_type','INSERT'),
 ('typed_video_collection_receipts','lease_token','INSERT'),
 ('typed_video_collection_receipts','receipt_kind','INSERT'),
 ('typed_video_collection_receipts','recorded_at','INSERT'),
 ('typed_video_collection_receipts','payload_fingerprint','INSERT'),
 ('typed_video_collection_receipts','recorded_at','UPDATE')
""" + TABLE_COLUMN_ACL_SQL_SUFFIX


def validate_readiness(row):
    require_boolean_facts(row, error_message='evidence_retention_acl_invalid')
    return {'ready':True,'schema_version':23}


def assert_evidence_retention_ready(database):
    return validate_readiness(database.read_row('evidence_retention_readiness',READINESS_SQL))
