-- Migration21 exact queue rights and denial of unrelated writes, rollback only.
begin;
SET LOCAL ROLE service_role;
DO $video_failure_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
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
), roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid
), relations AS MATERIALIZED (
 SELECT expected.*,relation.oid,relation.relowner,relation.relacl,relation.relkind,relation.relrowsecurity
 FROM required_tables expected LEFT JOIN pg_catalog.pg_class relation
 ON relation.oid=pg_catalog.to_regclass('public.'||expected.table_name)
), table_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')),
 column_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))
SELECT roles.valid AS roles_valid,
 NOT EXISTS(SELECT 1 FROM relations r WHERE r.oid IS NULL OR r.relkind<>'r' OR NOT r.relrowsecurity
  OR pg_catalog.pg_get_userbyid(r.relowner)<>'postgres')
 AND NOT EXISTS(SELECT 1 FROM required_columns required LEFT JOIN relations r USING(table_name)
  WHERE NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=r.oid
   AND a.attnum>0 AND NOT a.attisdropped AND a.attname=required.column_name)) AS relations_valid,
 NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
  WHERE pg_catalog.has_table_privilege(roles.service_oid,r.oid,p.privilege)
    IS DISTINCT FROM (p.privilege=ANY(r.privileges)))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN column_privileges p WHERE a.attnum>0 AND NOT a.attisdropped
  AND pg_catalog.has_column_privilege(roles.service_oid,r.oid,a.attnum,p.privilege)
   IS DISTINCT FROM (p.privilege=ANY(r.privileges) OR EXISTS(SELECT 1 FROM required_columns required
    WHERE required.table_name=r.table_name AND required.column_name=a.attname AND required.privilege=p.privilege)))
 AND NOT EXISTS(SELECT 1 FROM relations r
  CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce(r.relacl,pg_catalog.acldefault('r',r.relowner))) acl
  WHERE acl.grantee<>r.relowner AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable
   OR NOT(acl.privilege_type=ANY(r.privileges))))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
  WHERE a.attnum>0 AND NOT a.attisdropped AND acl.grantee<>r.relowner
  AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable OR NOT EXISTS(
   SELECT 1 FROM required_columns required WHERE required.table_name=r.table_name
    AND required.column_name=a.attname AND required.privilege=acl.privilege_type))) AS privileges_valid
FROM roles
 ) facts;
 IF checked.roles_valid IS DISTINCT FROM true OR checked.relations_valid IS DISTINCT FROM true
  OR checked.privileges_valid IS DISTINCT FROM true
 THEN RAISE EXCEPTION 'video_failure_acl_invalid'; END IF;
END $video_failure_acl$;
DO $denied_video_failure_writes$
DECLARE statement text;
BEGIN
 FOREACH statement IN ARRAY ARRAY[
 'UPDATE public.video_collection_state SET video_id=video_id WHERE false',
 'UPDATE public.video_collection_state SET created_at=created_at WHERE false',
 'INSERT INTO public.video_collection_state(created_at) SELECT NULL::timestamptz WHERE false',
 'INSERT INTO public.video_collection_state(profile_metadata_version) SELECT NULL::smallint WHERE false',
 'INSERT INTO public.video_collection_state(lease_token) SELECT NULL::uuid WHERE false',
 'UPDATE public.channel_collection_state SET user_work_id=user_work_id WHERE false'
 ] LOOP
  BEGIN
   EXECUTE statement;
   RAISE EXCEPTION 'video_failure_forbidden_write_succeeded';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
 END LOOP;
END $denied_video_failure_writes$;
rollback;
