-- Evidence retention19 current ACL regression; no durable changes.
begin;
SET LOCAL ROLE service_role;
DO $evidence_retention_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO checked FROM (
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
 THEN RAISE EXCEPTION 'evidence_retention_acl_invalid'; END IF;
END $evidence_retention_acl$;
SELECT recorded_at FROM public.channel_snapshots WHERE false FOR UPDATE;
SELECT recorded_at FROM public.typed_video_collection_receipts WHERE false FOR UPDATE;
DO $denied_evidence_writes$
DECLARE statement text;
BEGIN
 FOREACH statement IN ARRAY ARRAY[
 'UPDATE public.channel_snapshots SET views=views WHERE false',
 'UPDATE public.typed_video_collection_receipts SET lease_token=lease_token WHERE false',
 'INSERT INTO public.typed_video_collection_receipts(created_at) SELECT NULL::timestamptz WHERE false',
 'UPDATE public.video_type_review_cases SET first_seen_at=first_seen_at WHERE false',
 'UPDATE public.video_type_review_cases SET case_id=case_id WHERE false'
 ] LOOP
  BEGIN
   EXECUTE statement;
   RAISE EXCEPTION 'evidence_retention_forbidden_write_succeeded';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
 END LOOP;
END $denied_evidence_writes$;
rollback;
