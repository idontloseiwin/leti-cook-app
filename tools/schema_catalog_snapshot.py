"""Deterministic logical PostgreSQL 17 catalog facts for independent clusters.

Identifiers use names/signatures, never OIDs. Catalog OIDs are only join keys.
The administrative query callback returns one decoded JSON object; this module
never discovers a database, opens a connection, or changes its schema.
"""
from __future__ import annotations

import json
import re

NAMESPACES = "left(n.nspname,3)<>'pg_' AND n.nspname <> 'information_schema'"
SEED_CLOCKS = {
    "public.channel_labels": ("created_at",),
    "public.typed_video_collection_runtime": ("created_at", "updated_at"),
    "public.video_collection_cadence": ("created_at", "updated_at"),
    "public.video_collection_control": ("invalidated_at", "updated_at"),
    "public.channel_discovery_queue_control": ("updated_at",),
}
# schema.sql fresh bootstrap explicitly sets both clocks to now() while marking
# each empty queue reconciled. Preserve that relationship instead of dropping
# last_reconciled_at or comparing independent cluster wall-clock values.
BOOTSTRAP_RECONCILIATION_TABLES = frozenset({
    "public.video_collection_control", "public.channel_discovery_queue_control",
})
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def _acl(expression):
    return f"""coalesce((SELECT jsonb_agg(jsonb_build_object(
      'grantor',pg_get_userbyid(x.grantor),'grantee',CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,
      'privilege',x.privilege_type,'grantable',x.is_grantable)
      ORDER BY pg_get_userbyid(x.grantor),CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,x.privilege_type,x.is_grantable)
      FROM aclexplode({expression}) x),'[]'::jsonb)"""


# Stable object columns are explicit so adding a system-catalog column does not
# silently change the comparison. Arrays sort independently below, except column
# order, argument order, constraint definitions and policy expressions.
QUERIES = {
    "namespaces": f"""SELECT n.nspname AS name,pg_get_userbyid(n.nspowner) AS owner,
      n.nspacl IS NULL AS acl_default,{_acl('n.nspacl')} AS acl FROM pg_namespace n WHERE {NAMESPACES}""",
    "relations": f"""SELECT n.nspname AS schema,c.relname AS name,c.relkind AS kind,pg_get_userbyid(c.relowner) AS owner,
      c.relpersistence AS persistence,c.relrowsecurity AS rls,c.relforcerowsecurity AS force_rls,
      c.relreplident AS replica_identity,c.reloptions AS options,c.relispartition AS partition,
      CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound,c.oid,false) END AS partition_bound,
      CASE WHEN c.relkind='p' THEN pg_get_partkeydef(c.oid) END AS partition_key,
      c.relacl IS NULL AS acl_default,{_acl('c.relacl')} AS acl,
      t.spcname AS tablespace,a.amname AS access_method
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      LEFT JOIN pg_tablespace t ON t.oid=c.reltablespace LEFT JOIN pg_am a ON a.oid=c.relam
      WHERE {NAMESPACES} AND c.relkind IN ('r','p','v','m','S','f')""",
    "columns": f"""SELECT n.nspname AS schema,c.relname AS relation,row_number() OVER(PARTITION BY c.oid ORDER BY a.attnum) AS position,a.attname AS name,
      format_type(a.atttypid,a.atttypmod) AS type,a.attnotnull AS not_null,a.attidentity AS identity,
      a.attgenerated AS generated,a.attisdropped AS dropped,a.attstorage AS storage,a.attcompression AS compression,
      a.attstattarget AS statistics_target,a.attoptions AS options,
      CASE WHEN a.attcollation<>0 THEN format('%I.%I',cn.nspname,co.collname) END AS collation,
      pg_get_expr(d.adbin,d.adrelid,false) AS default_expression,
      a.attacl IS NULL AS acl_default,{_acl('a.attacl')} AS acl
      FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
      LEFT JOIN pg_collation co ON co.oid=a.attcollation LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace
      WHERE {NAMESPACES} AND a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m','S','f')""",
    "constraints": f"""SELECT n.nspname AS schema,con.conname AS name,
      CASE WHEN con.conrelid<>0 THEN format('%I.%I',rn.nspname,r.relname) END AS relation,
      CASE WHEN con.contypid<>0 THEN format_type(con.contypid,NULL) END AS domain,
      con.contype AS type,con.condeferrable AS deferrable,con.condeferred AS deferred,
      con.convalidated AS validated,con.conislocal AS local,con.coninhcount AS inherited_count,
      con.connoinherit AS no_inherit,pg_get_constraintdef(con.oid,false) AS definition
      FROM pg_constraint con JOIN pg_namespace n ON n.oid=con.connamespace
      LEFT JOIN pg_class r ON r.oid=con.conrelid LEFT JOIN pg_namespace rn ON rn.oid=r.relnamespace WHERE {NAMESPACES}""",
    "indexes": f"""SELECT n.nspname AS schema,c.relname AS name,r.relname AS relation,
      pg_get_indexdef(c.oid) AS definition,i.indisunique AS unique,i.indisprimary AS primary,
      i.indisexclusion AS exclusion,i.indimmediate AS immediate,i.indisclustered AS clustered,
      i.indisvalid AS valid,i.indisready AS ready,i.indislive AS live,i.indisreplident AS replica_identity,
      i.indnullsnotdistinct AS nulls_not_distinct,c.reloptions AS options,pg_get_userbyid(c.relowner) AS owner,
      t.spcname AS tablespace FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
      JOIN pg_class r ON r.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      LEFT JOIN pg_tablespace t ON t.oid=c.reltablespace WHERE {NAMESPACES}""",
    "views": f"""SELECT n.nspname AS schema,c.relname AS name,pg_get_viewdef(c.oid,false) AS definition,
      c.reloptions AS options,c.relispopulated AS populated FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE {NAMESPACES} AND c.relkind IN ('v','m')""",
    "functions": f"""SELECT n.nspname AS schema,p.proname AS name,pg_get_function_identity_arguments(p.oid) AS signature,
      pg_get_functiondef(p.oid) AS definition,l.lanname AS language,pg_get_userbyid(p.proowner) AS owner,
      p.prokind AS kind,p.prosecdef AS security_definer,p.proleakproof AS leakproof,p.provolatile AS volatility,
      p.proisstrict AS strict,p.proretset AS returns_set,p.proparallel AS parallel,p.proconfig AS config,
      p.procost AS cost,p.prorows AS rows,p.proacl IS NULL AS acl_default,{_acl('p.proacl')} AS acl
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
      WHERE {NAMESPACES} AND p.prokind<>'a'""",
    "triggers": f"""SELECT n.nspname AS schema,c.relname AS relation,CASE WHEN NOT t.tgisinternal THEN t.tgname END AS name,
      CASE WHEN NOT t.tgisinternal THEN pg_get_triggerdef(t.oid,false) END AS definition,
      t.tgtype AS type,t.tgfoid::regprocedure::text AS function,
      (SELECT jsonb_agg(a.attname ORDER BY x.position) FROM unnest(t.tgattr::smallint[]) WITH ORDINALITY x(attnum,position)
        JOIN pg_attribute a ON a.attrelid=t.tgrelid AND a.attnum=x.attnum) AS columns,
      encode(t.tgargs,'hex') AS arguments,t.tgoldtable AS old_table,t.tgnewtable AS new_table,
      t.tgenabled AS enabled,t.tgisinternal AS internal,
      t.tgdeferrable AS deferrable,t.tginitdeferred AS initially_deferred,
      CASE WHEN t.tgconstraint<>0 THEN con.conname END AS constraint_name
      FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      LEFT JOIN pg_constraint con ON con.oid=t.tgconstraint WHERE {NAMESPACES}""",
    "policies": f"""SELECT n.nspname AS schema,c.relname AS relation,p.polname AS name,p.polcmd AS command,
      p.polpermissive AS permissive,(SELECT jsonb_agg(CASE WHEN r=0 THEN 'PUBLIC' ELSE pg_get_userbyid(r) END
        ORDER BY CASE WHEN r=0 THEN 'PUBLIC' ELSE pg_get_userbyid(r) END) FROM unnest(p.polroles) r) AS roles,
      pg_get_expr(p.polqual,p.polrelid,false) AS using_expression,
      pg_get_expr(p.polwithcheck,p.polrelid,false) AS check_expression
      FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {NAMESPACES}""",
    "sequences": f"""SELECT n.nspname AS schema,c.relname AS name,format_type(s.seqtypid,NULL) AS type,
      s.seqstart AS start,s.seqincrement AS increment,s.seqmax AS maximum,s.seqmin AS minimum,
      s.seqcache AS cache,s.seqcycle AS cycle,
      (SELECT jsonb_agg(jsonb_build_object('relation',format('%I.%I',rn.nspname,r.relname),
        'column',a.attname,'dependency',d.deptype) ORDER BY rn.nspname,r.relname,a.attname,d.deptype)
        FROM pg_depend d JOIN pg_class r ON r.oid=d.refobjid JOIN pg_namespace rn ON rn.oid=r.relnamespace
        JOIN pg_attribute a ON a.attrelid=r.oid AND a.attnum=d.refobjsubid
        WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.refclassid='pg_class'::regclass AND d.deptype IN('a','i')) AS owned_by
      FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {NAMESPACES}""",
    "inheritance": f"""SELECT n.nspname AS schema,c.relname AS child,pn.nspname AS parent_schema,p.relname AS parent,
      i.inhseqno AS position,i.inhdetachpending AS detach_pending FROM pg_inherits i
      JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      JOIN pg_class p ON p.oid=i.inhparent JOIN pg_namespace pn ON pn.oid=p.relnamespace WHERE {NAMESPACES}""",
    "types": f"""SELECT n.nspname AS schema,t.typname AS name,t.typtype AS kind,pg_get_userbyid(t.typowner) AS owner,
      t.typnotnull AS not_null,CASE WHEN t.typbasetype<>0 THEN format_type(t.typbasetype,t.typtypmod) END AS base,
      t.typdefault AS default_expression,CASE WHEN t.typcollation<>0 THEN t.typcollation::regcollation::text END AS collation,t.typacl IS NULL AS acl_default,{_acl('t.typacl')} AS acl,
      (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid=t.oid) AS enum_labels
      FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE {NAMESPACES} AND t.typtype IN('e','d','r','m')""",
    "roles": """SELECT rolname AS name,rolsuper AS superuser,rolinherit AS inherit,rolcreaterole AS create_role,
      rolcreatedb AS create_db,rolcanlogin AS login,rolreplication AS replication,rolbypassrls AS bypass_rls,
      rolconnlimit AS connection_limit,rolvaliduntil::text AS valid_until,rolconfig AS config
      FROM pg_roles WHERE left(rolname,3)<>'pg_'""",
    "memberships": """SELECT role.rolname AS role,member.rolname AS member,grantor.rolname AS grantor,
      m.admin_option,m.inherit_option,m.set_option FROM pg_auth_members m
      JOIN pg_roles role ON role.oid=m.roleid JOIN pg_roles member ON member.oid=m.member JOIN pg_roles grantor ON grantor.oid=m.grantor
      WHERE left(role.rolname,3)<>'pg_' OR left(member.rolname,3)<>'pg_'""",
    "database": f"""SELECT d.datname AS name,pg_get_userbyid(d.datdba) AS owner,pg_encoding_to_char(d.encoding) AS encoding,
      d.datcollate AS collation,d.datctype AS ctype,d.datlocprovider AS locale_provider,
      d.datlocale AS locale,d.daticurules AS icu_rules,d.datcollversion AS collation_version,d.datconnlimit AS connection_limit,
      d.datallowconn AS allow_connections,d.datacl IS NULL AS acl_default,{_acl('d.datacl')} AS acl
      FROM pg_database d WHERE d.datname=current_database()""",
    "role_database_settings": """SELECT coalesce(d.datname,'ALL') AS database,coalesce(r.rolname,'ALL') AS role,s.setconfig AS config
      FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid=s.setdatabase LEFT JOIN pg_roles r ON r.oid=s.setrole
      WHERE s.setdatabase=0 OR d.datname=current_database()""",
    "default_privileges": f"""SELECT pg_get_userbyid(d.defaclrole) AS owner,coalesce(n.nspname,'ALL') AS schema,
      d.defaclobjtype AS object_type,d.defaclacl IS NULL AS acl_default,{_acl('d.defaclacl')} AS acl
      FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace""",
    "extensions": """SELECT e.extname AS name,e.extversion AS version,n.nspname AS schema,
      pg_get_userbyid(e.extowner) AS owner,e.extrelocatable AS relocatable
      FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace""",
    "schema_state": """SELECT baseline_id,current_version FROM operator_private.schema_state WHERE singleton""",
}


# These object kinds do not exist in the reviewed application schema. Adding one
# requires extending the comparator first; equality must not silently ignore it.
QUERIES["unsupported_objects"] = f"""
SELECT 'aggregate' AS kind,format('%I.%I(%s)',n.nspname,p.proname,pg_get_function_identity_arguments(p.oid)) AS name
 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE {NAMESPACES} AND p.prokind='a'
UNION ALL SELECT 'relation-kind-'||c.relkind::text,format('%I.%I',n.nspname,c.relname)
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {NAMESPACES} AND c.relkind NOT IN('r','p','v','m','S','i','I')
UNION ALL SELECT 'range-type',format('%I.%I',n.nspname,t.typname)
 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE {NAMESPACES} AND t.typtype IN('r','m')
UNION ALL SELECT 'collation',format('%I.%I',n.nspname,c.collname)
 FROM pg_collation c JOIN pg_namespace n ON n.oid=c.collnamespace WHERE {NAMESPACES}
UNION ALL SELECT 'operator',format('%I.%I',n.nspname,o.oprname)
 FROM pg_operator o JOIN pg_namespace n ON n.oid=o.oprnamespace WHERE {NAMESPACES}
UNION ALL SELECT 'operator-class',format('%I.%I',n.nspname,o.opcname)
 FROM pg_opclass o JOIN pg_namespace n ON n.oid=o.opcnamespace WHERE {NAMESPACES}
UNION ALL SELECT 'operator-family',format('%I.%I',n.nspname,o.opfname)
 FROM pg_opfamily o JOIN pg_namespace n ON n.oid=o.opfnamespace WHERE {NAMESPACES}
UNION ALL SELECT 'rewrite-rule',format('%I.%I.%I',n.nspname,c.relname,r.rulename)
 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace
 WHERE {NAMESPACES} AND r.rulename<>'_RETURN'
UNION ALL SELECT 'event-trigger',evtname::text FROM pg_event_trigger
UNION ALL SELECT 'foreign-data-wrapper',fdwname::text FROM pg_foreign_data_wrapper
UNION ALL SELECT 'foreign-server',srvname::text FROM pg_foreign_server
UNION ALL SELECT 'publication',pubname::text FROM pg_publication
UNION ALL SELECT 'subscription',subname::text FROM pg_subscription
UNION ALL SELECT 'language',lanname::text FROM pg_language WHERE lanname NOT IN('internal','c','sql','plpgsql')
UNION ALL SELECT 'tablespace',spcname::text FROM pg_tablespace WHERE spcname NOT IN('pg_default','pg_global')
"""


def _identifier(value):
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise ValueError("snapshot_identifier_invalid")
    return '"' + value + '"'


def snapshot_sql(seed_clocks=None):
    """One statement, so a drift can be compared inside a rollback-only session."""
    seed_clocks = SEED_CLOCKS if seed_clocks is None else seed_clocks
    queries = dict(QUERIES)
    seeds = []
    for table, clocks in seed_clocks.items():
        schema, name = table.split(".")
        relation = f"{_identifier(schema)}.{_identifier(name)}"
        excluded = "ARRAY[" + ",".join("'" + _identifier(clock)[1:-1] + "'" for clock in clocks) + "]::text[]"
        validity = " AND ".join(f"t.{_identifier(clock)} IS NOT NULL AND isfinite(t.{_identifier(clock)})" for clock in clocks) or "true"
        values = f"to_jsonb(t)-{excluded}"
        if table in BOOTSTRAP_RECONCILIATION_TABLES:
            values = ("CASE WHEN t.last_reconciled_at IS NOT NULL AND isfinite(t.last_reconciled_at) "
                      "AND t.last_reconciled_at=t.updated_at AND t.reconciliation_required IS FALSE "
                      f"THEN jsonb_set({values},'{{last_reconciled_at}}','\"initial-bootstrap-clock\"'::jsonb) "
                      f"ELSE {values} END")
        seeds.append(f"SELECT '{table}'::text AS relation,({values}) AS values,({validity}) AS clock_valid FROM {relation} t")
    queries["seeds"] = " UNION ALL ".join(seeds) if seeds else "SELECT NULL::text AS relation,NULL::jsonb AS values,true AS clock_valid WHERE false"
    pieces = [f"'{key}',coalesce((SELECT jsonb_agg(to_jsonb(q)) FROM ({sql}) q),'[]'::jsonb)" for key, sql in queries.items()]
    return "SELECT jsonb_build_object(" + ",\n".join(pieces) + ")::text;"


def canonicalize(value):
    """Only line endings and unordered catalog rows are normalized."""
    if not isinstance(value, dict) or set(value) != set(QUERIES) | {"seeds"}:
        raise ValueError("snapshot_shape_invalid")
    if value.get("unsupported_objects"):
        raise ValueError("snapshot_unsupported_objects")
    result = {}
    for section, rows in value.items():
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("snapshot_rows_invalid")
        if section == "seeds" and any(row.get("clock_valid") is not True for row in rows):
            raise ValueError("snapshot_seed_clock_invalid")
        normalized = []
        for row in rows:
            row = dict(row)
            if section == "functions" and isinstance(row.get("definition"), str):
                row["definition"] = row["definition"].replace("\r\n", "\n").replace("\r", "\n")
            normalized.append(row)
        result[section] = sorted(normalized, key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
    return result


def capture(query, *, seed_clocks=None):
    return canonicalize(query(snapshot_sql(seed_clocks)))


def serialize(value):
    result = (json.dumps(canonicalize(value), sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    if len(result) > MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot_size_exceeded")
    return result


def differences(first, second):
    first, second = canonicalize(first), canonicalize(second)
    return [key for key in sorted(first) if first[key] != second[key]]


def seed_difference_fields(first, second):
    """Bounded diagnostics list table/field names, never seed values."""
    result = []
    a, b = canonicalize(first)["seeds"], canonicalize(second)["seeds"]
    for table in sorted({row["relation"] for row in a + b}):
        left = [row["values"] for row in a if row["relation"] == table]
        right = [row["values"] for row in b if row["relation"] == table]
        if len(left) != len(right):
            result.append(table + ".<row-count>")
        for field in sorted({key for row in left + right for key in row}):
            left_values = sorted(json.dumps(row.get(field), sort_keys=True) for row in left)
            right_values = sorted(json.dumps(row.get(field), sort_keys=True) for row in right)
            if left_values != right_values:
                result.append(table + "." + field)
    return result[:64]
