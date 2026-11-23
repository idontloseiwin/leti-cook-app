"""Exact release admission from a source-free view and its catalog privileges."""
from collections.abc import Mapping

from schema_release import load_schema_release
from ..base.postgres import DatabaseError

READINESS_SQL = """
WITH roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='postgres') AS owner_oid,
        (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
        (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='lic_reader') AS reader_oid
), version_view AS MATERIALIZED (
 SELECT c.* FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='readable' AND c.relname='schema_version'
), ledger AS MATERIALIZED (
 SELECT c.* FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='operator_private' AND c.relname IN ('schema_state','schema_migrations')
), privileges(privilege) AS (
 VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')
)
SELECT value.baseline_id, value.current_version,
 coalesce((SELECT v.relkind='v' AND v.relowner=roles.owner_oid
  AND v.reloptions @> ARRAY['security_barrier=true','security_invoker=false']
  AND (SELECT array_agg(a.attname::text||':'||a.atttypid::regtype::text ORDER BY a.attnum)
       FROM pg_catalog.pg_attribute a WHERE a.attrelid=v.oid AND a.attnum>0 AND NOT a.attisdropped)
      =ARRAY['baseline_id:text','current_version:integer']
  AND NOT EXISTS(SELECT 1 FROM privileges p CROSS JOIN (VALUES(roles.service_oid),(roles.reader_oid)) permitted(oid)
      WHERE pg_catalog.has_table_privilege(permitted.oid,v.oid,p.privilege) IS DISTINCT FROM (p.privilege='SELECT'))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.aclexplode(coalesce(v.relacl,pg_catalog.acldefault('r',v.relowner))) acl
      WHERE acl.grantee<>roles.owner_oid AND (acl.grantee NOT IN (roles.service_oid,roles.reader_oid)
       OR acl.is_grantable OR acl.privilege_type<>'SELECT'))
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
      WHERE a.attrelid=v.oid AND a.attnum>0 AND NOT a.attisdropped AND acl.grantee<>roles.owner_oid)
  FROM version_view v),false) AS view_valid,
 (SELECT count(*) FROM ledger)=2 AND NOT EXISTS(SELECT 1 FROM ledger l
  WHERE l.relkind<>'r' OR l.relowner IS DISTINCT FROM roles.owner_oid OR NOT l.relrowsecurity
   OR EXISTS(SELECT 1 FROM pg_catalog.aclexplode(coalesce(l.relacl,pg_catalog.acldefault('r',l.relowner))) acl
       WHERE acl.grantee<>roles.owner_oid)
   OR EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
       WHERE a.attrelid=l.oid AND a.attnum>0 AND NOT a.attisdropped AND acl.grantee<>roles.owner_oid)
 ) AS ledger_private
FROM readable.schema_version value CROSS JOIN roles
"""


def validate_readiness(row):
    if (not isinstance(row, Mapping) or set(row) != {"baseline_id", "current_version", "view_valid", "ledger_private"}
            or type(row["baseline_id"]) is not str or type(row["current_version"]) is not int
            or type(row["view_valid"]) is not bool or type(row["ledger_private"]) is not bool):
        raise DatabaseError("schema_release_result_invalid")
    if not row["view_valid"] or not row["ledger_private"]:
        raise DatabaseError("schema_release_acl_invalid")
    try:
        expected = load_schema_release()
    except (OSError, ValueError):
        raise DatabaseError("schema_release_metadata_invalid") from None
    if row["baseline_id"] != expected.baseline_id or row["current_version"] != expected.schema_version:
        raise DatabaseError("schema_release_mismatch")
    return expected


def assert_schema_release_ready(database):
    return validate_readiness(database.read_row("capability_schema_release", READINESS_SQL))
