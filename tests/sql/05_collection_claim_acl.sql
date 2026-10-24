-- Disposable regression only. No domain rows are created.
begin;
SET LOCAL ROLE service_role;
DO $receipt_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
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
 IF NOT(checked.roles_valid AND checked.relations_valid AND checked.privileges_valid)
 THEN RAISE EXCEPTION 'inventory_receipts_storage_acl_invalid'; END IF;
END $receipt_acl$;
-- Empty insert still exercises exact column authority and cannot create a receipt.
INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,scan_id,transition_kind,result,source_checked_at,created_at)
 SELECT NULL::text,NULL::uuid,NULL::uuid,NULL::text,NULL::jsonb,NULL::timestamptz,NULL::timestamptz WHERE false;
UPDATE public.video_collection_state SET attempt_count=attempt_count,last_claim_run_id=last_claim_run_id,last_attempt_at=last_attempt_at WHERE false;
DO $denied_unrelated_writes$
DECLARE command text;
BEGIN
 FOREACH command IN ARRAY ARRAY[
  'UPDATE public.typed_inventory_receipts SET source_checked_at=source_checked_at WHERE false',
  'UPDATE public.typed_inventory_receipts SET scan_id=scan_id WHERE false',
  'UPDATE public.typed_inventory_receipts SET receipt_scope=receipt_scope WHERE false',
  'UPDATE public.video_collection_state SET created_at=created_at WHERE false',
  'INSERT INTO public.video_collection_state(lease_token) SELECT NULL::uuid WHERE false'
 ] LOOP
  BEGIN
   EXECUTE command;
   RAISE EXCEPTION 'unrelated_collection_write_allowed';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
 END LOOP;
END $denied_unrelated_writes$;
rollback;
