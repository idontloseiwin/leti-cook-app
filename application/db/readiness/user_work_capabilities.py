"""Exact current user-work ACL readiness (restore17, evidence19, deletion22, decisions23, workflow24), with one bounded catalog read.

No stored-check execution, arbitrary maintenance SQL or cached admission result.
Missing relations, missing/extra effective privileges, public/other-role grants
and grant options fail closed. Table locks are tested separately under the actual
runtime login; startup does not lock or inspect user rows.
"""
from .capability_facts import require_boolean_facts, USER_WORK_ACL_SQL_SUFFIX

REVIEW_INSERT_COLUMNS = ('case_id', 'video_id', 'channel_id', 'source_detached_at', 'observed_canonical_type', 'observed_types', 'observed_channels', 'blocked_reason', 'state', 'revision', 'decision_type', 'decision_reason', 'decision_at', 'decision_revision', 'first_seen_at', 'last_seen_at', 'evidence_expires_at', 'restore_owner_ref', 'user_channel_ref')
HISTORY_INSERT_COLUMNS = ('case_id', 'revision', 'video_type', 'reason', 'decided_at', 'request_id', 'expected_revision')

READINESS_SQL = """
WITH required_tables(table_name,privileges) AS (VALUES
 ('channels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('folders',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_labels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_label_assignments',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_objects',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_collection_members',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('saved_collection_commands',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('video_type_review_cases',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('video_type_review_history',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('user_channel_restore_items',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_type_review_cases','case_id','INSERT'),
 ('video_type_review_cases','video_id','INSERT'),
 ('video_type_review_cases','channel_id','INSERT'),
 ('video_type_review_cases','source_detached_at','INSERT'),
 ('video_type_review_cases','observed_canonical_type','INSERT'),
 ('video_type_review_cases','observed_types','INSERT'),
 ('video_type_review_cases','observed_channels','INSERT'),
 ('video_type_review_cases','blocked_reason','INSERT'),
 ('video_type_review_cases','state','INSERT'),
 ('video_type_review_cases','revision','INSERT'),
 ('video_type_review_cases','decision_type','INSERT'),
 ('video_type_review_cases','decision_reason','INSERT'),
 ('video_type_review_cases','decision_at','INSERT'),
 ('video_type_review_cases','decision_revision','INSERT'),
 ('video_type_review_cases','first_seen_at','INSERT'),
 ('video_type_review_cases','last_seen_at','INSERT'),
 ('video_type_review_cases','evidence_expires_at','INSERT'),
 ('video_type_review_cases','restore_owner_ref','INSERT'),
 ('video_type_review_cases','user_channel_ref','INSERT'),
 ('video_type_review_history','case_id','INSERT'),
 ('video_type_review_history','revision','INSERT'),
 ('video_type_review_history','video_type','INSERT'),
 ('video_type_review_history','reason','INSERT'),
 ('video_type_review_history','decided_at','INSERT'),
 ('video_type_review_history','request_id','INSERT'),
 ('video_type_review_history','expected_revision','INSERT'),
 ('saved_collection_members','user_channel_ref','UPDATE'),
 ('saved_collection_members','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','user_channel_ref','UPDATE'),
 ('video_type_review_cases','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','observed_canonical_type','UPDATE'),
 ('video_type_review_cases','observed_types','UPDATE'),
 ('video_type_review_cases','observed_channels','UPDATE'),
 ('video_type_review_cases','blocked_reason','UPDATE'),
 ('video_type_review_cases','revision','UPDATE'),
 ('video_type_review_cases','decision_type','UPDATE'),
 ('video_type_review_cases','decision_reason','UPDATE'),
 ('video_type_review_cases','decision_at','UPDATE'),
 ('video_type_review_cases','decision_revision','UPDATE'),
 ('video_type_review_cases','state','UPDATE'),
 ('video_type_review_cases','last_seen_at','UPDATE'),
 ('video_type_review_cases','evidence_expires_at','UPDATE'),
 ('video_type_review_cases','video_id','UPDATE'),
 ('video_type_review_cases','channel_id','UPDATE'),
 ('video_type_review_cases','source_detached_at','UPDATE')
""" + USER_WORK_ACL_SQL_SUFFIX

def validate_readiness(row):
    require_boolean_facts(row, error_message='user_work_restore_acl_invalid')
    return {'ready':True,'schema_version':24}


def assert_user_work_restore_ready(database):
    return validate_readiness(database.read_row('user_work_restore_readiness',READINESS_SQL))
