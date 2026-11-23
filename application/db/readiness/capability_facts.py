"""Shared strict catalog facts; callers own their checks and when they run.

The SQL suffix consumes only each caller's static required_tables and
required_columns CTEs. It keeps exact raw and effective ACL checks, including
unexpected privileges and grant options. No query execution or caching lives here.
"""
from collections.abc import Mapping

from ..base.postgres import DatabaseError

CATALOG_FIELDS = frozenset(('roles_valid', 'relations_valid', 'privileges_valid'))

TABLE_COLUMN_ACL_SQL_SUFFIX = """), roles AS (
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
"""


# Mixed source tables keep the original strict service-only suffix. Only the
# user-work capability admits the reader's SELECT on these eight pure tables.
READER_USER_WORK_TABLES = (
    'folders', 'channel_labels', 'channel_label_assignments', 'saved_objects',
    'saved_collection_members', 'saved_collection_commands',
    'user_channel_restore_items', 'video_type_review_history',
)
_READER_TABLES_SQL = ','.join("'" + name + "'" for name in READER_USER_WORK_TABLES)
_READER_ROLE_SQL = """ SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='lic_reader') AS reader_oid,
 coalesce((SELECT NOT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin AND NOT rolinherit
  AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members membership WHERE membership.member=reader.oid)
  FROM pg_catalog.pg_roles reader WHERE rolname='lic_reader'),false) AS reader_valid,"""
_READER_ACL_SQL = """ WHERE acl.grantee<>r.relowner AND (acl.is_grantable OR NOT (
   (acl.grantee=roles.service_oid AND acl.privilege_type=ANY(r.privileges))
   OR (acl.grantee=roles.reader_oid AND r.table_name IN (""" + _READER_TABLES_SQL + """)
       AND acl.privilege_type='SELECT'))))"""
_READER_FACTS_SQL = """
 AND NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
  WHERE pg_catalog.has_table_privilege(roles.reader_oid,r.oid,p.privilege)
   IS DISTINCT FROM (r.table_name IN (""" + _READER_TABLES_SQL + """) AND p.privilege='SELECT'))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN column_privileges p WHERE a.attnum>0 AND NOT a.attisdropped
  AND pg_catalog.has_column_privilege(roles.reader_oid,r.oid,a.attnum,p.privilege)
   IS DISTINCT FROM (r.table_name IN (""" + _READER_TABLES_SQL + """) AND p.privilege='SELECT'))
 AND NOT EXISTS(SELECT 1 FROM relations r WHERE r.table_name IN (""" + _READER_TABLES_SQL + """)
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_policy policy WHERE policy.polrelid=r.oid
    AND policy.polname='lic_reader_select' AND policy.polcmd='r' AND policy.polpermissive
    AND policy.polroles=ARRAY[roles.reader_oid]::oid[]
    AND pg_catalog.pg_get_expr(policy.polqual,policy.polrelid)='true' AND policy.polwithcheck IS NULL))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_policy policy ON policy.polrelid=r.oid
  WHERE (roles.reader_oid=ANY(policy.polroles) OR 0=ANY(policy.polroles))
   AND NOT (r.table_name IN (""" + _READER_TABLES_SQL + """) AND policy.polname='lic_reader_select'
    AND policy.polcmd='r' AND policy.polpermissive AND policy.polroles=ARRAY[roles.reader_oid]::oid[]
    AND pg_catalog.pg_get_expr(policy.polqual,policy.polrelid)='true' AND policy.polwithcheck IS NULL))"""

USER_WORK_ACL_SQL_SUFFIX = TABLE_COLUMN_ACL_SQL_SUFFIX.replace(
    ' SELECT (SELECT oid FROM pg_catalog.pg_roles',
    _READER_ROLE_SQL + '\n (SELECT oid FROM pg_catalog.pg_roles', 1,
).replace(
    'SELECT roles.valid AS roles_valid,',
    'SELECT roles.valid AND roles.reader_valid AS roles_valid,', 1,
).replace(
    " WHERE acl.grantee<>r.relowner AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable\n"
    "   OR NOT(acl.privilege_type=ANY(r.privileges))))", _READER_ACL_SQL, 1,
).replace(' AS privileges_valid\nFROM roles', _READER_FACTS_SQL + ' AS privileges_valid\nFROM roles', 1)


def require_boolean_facts(row, fields=CATALOG_FIELDS, *, error_message='database_operation_failed'):
    """Reject malformed or negative facts without accepting truthy non-booleans."""
    if (not isinstance(row, Mapping) or set(row) != set(fields)
            or any(type(value) is not bool for value in row.values())):
        raise DatabaseError('database_result_shape_invalid')
    if not all(row.values()):
        raise DatabaseError(error_message, code='P0001')
