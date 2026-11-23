-- User-work ACL after migrations17/19; rollback-only current regression.
-- Source-only regression: normal schema test harness owns execution/rollback.
begin;
SET LOCAL ROLE service_role;
DO $user_work_restore_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
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
), roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='lic_reader') AS reader_oid,
 coalesce((SELECT NOT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin AND NOT rolinherit
  AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members membership WHERE membership.member=reader.oid)
  FROM pg_catalog.pg_roles reader WHERE rolname='lic_reader'),false) AS reader_valid,
 (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid
), relations AS MATERIALIZED (
 SELECT expected.*,relation.oid,relation.relowner,relation.relacl,relation.relkind,relation.relrowsecurity
 FROM required_tables expected LEFT JOIN pg_catalog.pg_class relation
 ON relation.oid=pg_catalog.to_regclass('public.'||expected.table_name)
), table_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')),
 column_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))
SELECT roles.valid AND roles.reader_valid AS roles_valid,
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
  WHERE acl.grantee<>r.relowner AND (acl.is_grantable OR NOT (
   (acl.grantee=roles.service_oid AND acl.privilege_type=ANY(r.privileges))
   OR (acl.grantee=roles.reader_oid AND r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history')
       AND acl.privilege_type='SELECT'))))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
  WHERE a.attnum>0 AND NOT a.attisdropped AND acl.grantee<>r.relowner
  AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable OR NOT EXISTS(
   SELECT 1 FROM required_columns required WHERE required.table_name=r.table_name
    AND required.column_name=a.attname AND required.privilege=acl.privilege_type)))
 AND NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
  WHERE pg_catalog.has_table_privilege(roles.reader_oid,r.oid,p.privilege)
   IS DISTINCT FROM (r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history') AND p.privilege='SELECT'))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN column_privileges p WHERE a.attnum>0 AND NOT a.attisdropped
  AND pg_catalog.has_column_privilege(roles.reader_oid,r.oid,a.attnum,p.privilege)
   IS DISTINCT FROM (r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history') AND p.privilege='SELECT'))
 AND NOT EXISTS(SELECT 1 FROM relations r WHERE r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history')
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_policy policy WHERE policy.polrelid=r.oid
    AND policy.polname='lic_reader_select' AND policy.polcmd='r' AND policy.polpermissive
    AND policy.polroles=ARRAY[roles.reader_oid]::oid[]
    AND pg_catalog.pg_get_expr(policy.polqual,policy.polrelid)='true' AND policy.polwithcheck IS NULL))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_policy policy ON policy.polrelid=r.oid
  WHERE (roles.reader_oid=ANY(policy.polroles) OR 0=ANY(policy.polroles))
   AND NOT (r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history') AND policy.polname='lic_reader_select'
    AND policy.polcmd='r' AND policy.polpermissive AND policy.polroles=ARRAY[roles.reader_oid]::oid[]
    AND pg_catalog.pg_get_expr(policy.polqual,policy.polrelid)='true' AND policy.polwithcheck IS NULL)) AS privileges_valid
FROM roles
 ) facts;
 IF checked.roles_valid IS DISTINCT FROM true OR checked.relations_valid IS DISTINCT FROM true
  OR checked.privileges_valid IS DISTINCT FROM true
 THEN RAISE EXCEPTION 'user_work_restore_acl_invalid'; END IF;
END $user_work_restore_acl$;

LOCK TABLE public.channels,public.folders,public.channel_labels,public.channel_label_assignments,
 public.saved_objects,public.saved_collection_members,public.saved_collection_commands,
 public.video_type_review_cases,public.video_type_review_history,public.user_channel_restore_items
 IN SHARE ROW EXCLUSIVE MODE NOWAIT;
DO $denied_user_work_writes$
DECLARE statement text;
BEGIN
 FOREACH statement IN ARRAY ARRAY[
 'UPDATE public.video_type_review_cases SET case_id=case_id WHERE false',
 'UPDATE public.video_type_review_history SET expected_revision=expected_revision WHERE false',
 'UPDATE public.video_type_review_history SET revision=revision WHERE false',
 'UPDATE public.video_type_review_history SET request_id=request_id WHERE false',
 'UPDATE public.video_type_review_history SET reason=reason WHERE false',
 'UPDATE public.video_type_review_cases SET first_seen_at=first_seen_at WHERE false',
 'UPDATE public.saved_collection_members SET video_id=video_id WHERE false',
 'UPDATE public.channel_collection_state SET user_work_id=user_work_id WHERE false',
 'INSERT INTO public.channel_collection_state(control_epoch) SELECT NULL::bigint WHERE false'
 ] LOOP
  BEGIN
   EXECUTE statement;
   RAISE EXCEPTION 'user_work_restore_forbidden_write_succeeded';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
 END LOOP;
END $denied_user_work_writes$;
rollback;
