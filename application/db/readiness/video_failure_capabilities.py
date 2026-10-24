"""Exact migration23 queue claim/failure column rights, one bounded catalog read.

Only the reviewed initializer/claim/failure/retention columns are writable. Missing/extra effective
rights, raw ACL grants, owners and RLS fail closed before HOT admission. This
capability neither reads user rows nor grants ordinary SQL while blocked.
"""
from .capability_facts import require_boolean_facts, TABLE_COLUMN_ACL_SQL_SUFFIX

INSERT_COLUMNS = ('video_id', 'channel_id', 'stats_due_at', 'metadata_due_at', 'stats_source_checked_at', 'metadata_source_checked_at', 'next_due_at', 'policy_version', 'hydration_needed', 'paused_reason', 'updated_at', 'video_type', 'typed_source_published_at', 'typed_source_published_acquired_at', 'typed_collection_status', 'availability_first_missing_at', 'availability_schedule_type', 'availability_resume_observed_at')
UPDATE_COLUMNS = ('next_due_at', 'lease_channel_epoch', 'lease_token', 'lease_owner', 'lease_started_at', 'lease_expires_at', 'consecutive_failures', 'last_error_type', 'updated_at', 'metadata_due_at', 'stats_due_at', 'attempt_count', 'last_claim_run_id', 'last_attempt_at', 'channel_id', 'stats_source_checked_at', 'metadata_source_checked_at', 'policy_version', 'hydration_needed', 'paused_reason', 'video_type', 'typed_source_published_at', 'typed_source_published_acquired_at', 'typed_stream_actual_end_at', 'typed_source_stream_end_acquired_at', 'last_success_at', 'profile_metadata_version', 'typed_collection_status', 'typed_collection_checked_at', 'availability_first_missing_at', 'availability_schedule_type', 'availability_resume_observed_at')

READINESS_SQL = """
WITH required_tables(table_name,privileges) AS (VALUES
 ('video_collection_state',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_collection_state','video_id','INSERT'),
 ('video_collection_state','channel_id','INSERT'),
 ('video_collection_state','stats_due_at','INSERT'),
 ('video_collection_state','metadata_due_at','INSERT'),
 ('video_collection_state','stats_source_checked_at','INSERT'),
 ('video_collection_state','metadata_source_checked_at','INSERT'),
 ('video_collection_state','next_due_at','INSERT'),
 ('video_collection_state','policy_version','INSERT'),
 ('video_collection_state','hydration_needed','INSERT'),
 ('video_collection_state','paused_reason','INSERT'),
 ('video_collection_state','updated_at','INSERT'),
 ('video_collection_state','video_type','INSERT'),
 ('video_collection_state','typed_source_published_at','INSERT'),
 ('video_collection_state','typed_source_published_acquired_at','INSERT'),
 ('video_collection_state','typed_collection_status','INSERT'),
 ('video_collection_state','availability_first_missing_at','INSERT'),
 ('video_collection_state','availability_schedule_type','INSERT'),
 ('video_collection_state','availability_resume_observed_at','INSERT'),
 ('video_collection_state','next_due_at','UPDATE'),
 ('video_collection_state','lease_channel_epoch','UPDATE'),
 ('video_collection_state','lease_token','UPDATE'),
 ('video_collection_state','lease_owner','UPDATE'),
 ('video_collection_state','lease_started_at','UPDATE'),
 ('video_collection_state','lease_expires_at','UPDATE'),
 ('video_collection_state','consecutive_failures','UPDATE'),
 ('video_collection_state','last_error_type','UPDATE'),
 ('video_collection_state','updated_at','UPDATE'),
 ('video_collection_state','metadata_due_at','UPDATE'),
 ('video_collection_state','stats_due_at','UPDATE'),
 ('video_collection_state','attempt_count','UPDATE'),
 ('video_collection_state','last_claim_run_id','UPDATE'),
 ('video_collection_state','last_attempt_at','UPDATE'),
 ('video_collection_state','channel_id','UPDATE'),
 ('video_collection_state','stats_source_checked_at','UPDATE'),
 ('video_collection_state','metadata_source_checked_at','UPDATE'),
 ('video_collection_state','policy_version','UPDATE'),
 ('video_collection_state','hydration_needed','UPDATE'),
 ('video_collection_state','paused_reason','UPDATE'),
 ('video_collection_state','video_type','UPDATE'),
 ('video_collection_state','typed_source_published_at','UPDATE'),
 ('video_collection_state','typed_source_published_acquired_at','UPDATE'),
 ('video_collection_state','typed_stream_actual_end_at','UPDATE'),
 ('video_collection_state','typed_source_stream_end_acquired_at','UPDATE'),
 ('video_collection_state','last_success_at','UPDATE'),
 ('video_collection_state','profile_metadata_version','UPDATE'),
 ('video_collection_state','typed_collection_status','UPDATE'),
 ('video_collection_state','typed_collection_checked_at','UPDATE'),
 ('video_collection_state','availability_first_missing_at','UPDATE'),
 ('video_collection_state','availability_schedule_type','UPDATE'),
 ('video_collection_state','availability_resume_observed_at','UPDATE')
""" + TABLE_COLUMN_ACL_SQL_SUFFIX

def validate_readiness(row):
    require_boolean_facts(row, error_message='video_failure_acl_invalid')
    return {'ready':True,'schema_version':23}


def assert_video_failure_ready(database):
    return validate_readiness(database.read_row('video_failure_readiness',READINESS_SQL))
