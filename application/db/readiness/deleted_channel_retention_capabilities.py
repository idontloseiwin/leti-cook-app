"""Exact schema22 deleted-channel private retry rights, one fixed catalog row.

Requires postgres ownership, RLS, private schema USAGE only and precise raw/effective
SELECT/INSERT/DELETE rights. No arbitrary blocked-maintenance SQL or stored checks.
"""
from .capability_facts import require_boolean_facts

READINESS_SQL = """
WITH required_tables(table_name,privileges) AS (VALUES
 ('operator_job_retry_requests',ARRAY['SELECT','INSERT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (SELECT NULL::text,NULL::text,NULL::text WHERE false
), roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid
), namespace AS MATERIALIZED (
 SELECT n.oid,n.nspowner,n.nspacl FROM pg_catalog.pg_namespace n
 WHERE n.oid=pg_catalog.to_regnamespace('operator_private')
), relations AS MATERIALIZED (
 SELECT expected.*,relation.oid,relation.relowner,relation.relacl,relation.relkind,relation.relrowsecurity
 FROM required_tables expected LEFT JOIN pg_catalog.pg_class relation
 ON relation.oid=pg_catalog.to_regclass('operator_private.'||expected.table_name)
), table_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')),
 column_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))
SELECT roles.valid AS roles_valid,
 EXISTS(SELECT 1 FROM namespace n WHERE pg_catalog.pg_get_userbyid(n.nspowner)='postgres')
 AND NOT EXISTS(SELECT 1 FROM relations r WHERE r.oid IS NULL OR r.relkind<>'r' OR NOT r.relrowsecurity
  OR pg_catalog.pg_get_userbyid(r.relowner)<>'postgres')
 AND NOT EXISTS(SELECT 1 FROM required_columns required LEFT JOIN relations r USING(table_name)
  WHERE NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=r.oid
   AND a.attnum>0 AND NOT a.attisdropped AND a.attname=required.column_name)) AS relations_valid,
 EXISTS(SELECT 1 FROM namespace n WHERE pg_catalog.has_schema_privilege(roles.service_oid,n.oid,'USAGE')
  AND NOT pg_catalog.has_schema_privilege(roles.service_oid,n.oid,'CREATE'))
 AND NOT EXISTS(SELECT 1 FROM namespace n CROSS JOIN LATERAL
  pg_catalog.aclexplode(coalesce(n.nspacl,pg_catalog.acldefault('n',n.nspowner))) acl
  WHERE acl.grantee<>n.nspowner AND (acl.grantee IS DISTINCT FROM roles.service_oid
   OR acl.is_grantable OR acl.privilege_type<>'USAGE'))
 AND NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
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
"""

def validate_readiness(row):
    require_boolean_facts(row, error_message='deleted_channel_retention_acl_invalid')
    return {'ready':True,'schema_version':22}


def assert_deleted_channel_retention_ready(database):
    return validate_readiness(database.read_row('deleted_channel_retention_readiness',READINESS_SQL))
