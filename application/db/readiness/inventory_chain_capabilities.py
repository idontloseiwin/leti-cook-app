"""Closed inventory-chain catalog contract with reviewed PostgreSQL definitions.

Missing or changed release pins deny collection and HOT maintenance admission.
"""
import re
from .capability_facts import TABLE_COLUMN_ACL_SQL_SUFFIX,require_boolean_facts
from ..base.postgres import DatabaseError

INSERT_COLUMNS=('job_id','channel_id','video_type','scan_id','checkpoint','last_ack','terminal_outcome')
UPDATE_COLUMNS=('scan_id','checkpoint','last_ack','terminal_outcome','updated_at')
CONSTRAINT_NAMES=(
 'inventory_chains_pkey','inventory_chains_parent_fkey','inventory_chains_scan_fkey',
 'inventory_chains_type_check','inventory_chains_terminal_check','inventory_chains_terminal_source_check',
 'inventory_chains_checkpoint_check','inventory_chains_ack_object_check','inventory_chains_updated_check',
 'inventory_chains_checkpoint_size','inventory_chains_last_ack_exact')
PARENT_CONSTRAINT_NAMES=('operator_jobs_id_channel_key','operator_jobs_inventory_chain_version_check')
INDEX_NAMES=('inventory_chains_pkey','inventory_chains_scan_pin_idx','inventory_chains_ack_scan_pin_idx',
             'inventory_chains_bound_transition_pin_idx','inventory_chains_ack_transition_pin_idx')
# Source-code-only closed keys: constraint:<name> and index:<name>. No env override.
# Captured from PostgreSQL 17 after independent DDL-oracle and fresh/upgrade parity.
DEFINITION_PINS={
    'constraint:inventory_chains_ack_object_check':'3ca5b1c1a79ce50945ad49cfd382948a',
    'constraint:inventory_chains_checkpoint_check':'25133130239ca220b9c4a45b66f55a76',
    'constraint:inventory_chains_checkpoint_size':'db32d0cfd9a60baccfa3ed663757d949',
    'constraint:inventory_chains_last_ack_exact':'4ee59f307c3a2ecacd89127450f60197',
    'constraint:inventory_chains_parent_fkey':'ee3ab59bd129a7133d437caae32e54e0',
    'constraint:inventory_chains_pkey':'cefe20ba15ca781c08f879ceaa33a881',
    'constraint:inventory_chains_scan_fkey':'427253505c1332600f93c27631374ffc',
    'constraint:inventory_chains_terminal_check':'5f3157930d70970a7877925c66fc5d33',
    'constraint:inventory_chains_terminal_source_check':'ca56ccf5c787d4a5e44013ae462cf9dd',
    'constraint:inventory_chains_type_check':'1b62bd450d6b4122cfeba62bbc8d0b63',
    'constraint:inventory_chains_updated_check':'dcb719d90262517412d1357688ab5b5b',
    'constraint:operator_jobs_id_channel_key':'ebfd99494936210d11632cfa34db9c3f',
    'constraint:operator_jobs_inventory_chain_version_check':'e60dfd5b8092583eb8e4a4feb72d99fc',
    'index:inventory_chains_ack_scan_pin_idx':'49ab35f17ddbf3ca1ac5acf9774c42c6',
    'index:inventory_chains_ack_transition_pin_idx':'63f445fd218dfa5f109f7514e9df8e8c',
    'index:inventory_chains_bound_transition_pin_idx':'5a2d04687c061627076e02a28cfc4adb',
    'index:inventory_chains_pkey':'f5b554d4c24b7009e38571807bf5a015',
    'index:inventory_chains_scan_pin_idx':'9a60383ddcc5981cf75bf42315d6b251',
}

_ACL_ROWS=','.join("('operator_job_inventory_chains','"+name+"','"+privilege+"')"
    for privilege,columns in (('INSERT',INSERT_COLUMNS),('UPDATE',UPDATE_COLUMNS)) for name in columns)
ACL_SQL="""WITH required_tables(table_name,privileges) AS (VALUES
 ('operator_job_inventory_chains',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES """+_ACL_ROWS+TABLE_COLUMN_ACL_SQL_SUFFIX

CATALOG_SQL="""WITH expected_columns(name,type_name,required,default_value,position) AS (VALUES
 ('job_id','uuid',true,NULL::text,1),('channel_id','text',true,NULL,2),('video_type','text',true,NULL,3),
 ('scan_id','uuid',false,NULL,4),('checkpoint','jsonb',true,chr(39)||'{}'||chr(39)||'::jsonb',5),
 ('last_ack','jsonb',true,chr(39)||'{}'||chr(39)||'::jsonb',6),('terminal_outcome','text',false,NULL,7),
 ('updated_at','timestamp with time zone',true,'now()',8)
), relation AS (SELECT c.* FROM pg_catalog.pg_class c
 WHERE c.oid=to_regclass('public.operator_job_inventory_chains'))
SELECT
 EXISTS(SELECT 1 FROM relation WHERE relkind='r' AND relrowsecurity AND NOT relforcerowsecurity
 AND relowner=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname='postgres'))
 AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_policy WHERE polrelid=to_regclass('public.operator_job_inventory_chains')) AS table_valid,
 (SELECT count(*)=8 FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass('public.operator_job_inventory_chains')
 AND attnum>0 AND NOT attisdropped)
 AND NOT EXISTS(SELECT 1 FROM expected_columns e LEFT JOIN pg_catalog.pg_attribute a
 ON a.attrelid=to_regclass('public.operator_job_inventory_chains') AND a.attname=e.name AND NOT a.attisdropped
 LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
 WHERE a.attnum IS DISTINCT FROM e.position OR pg_catalog.format_type(a.atttypid,a.atttypmod) IS DISTINCT FROM e.type_name
 OR a.attnotnull IS DISTINCT FROM e.required OR a.attidentity<>'' OR a.attgenerated<>''
 OR pg_catalog.pg_get_expr(d.adbin,d.adrelid) IS DISTINCT FROM e.default_value) AS columns_valid,
 NOT EXISTS(SELECT 1 FROM pg_catalog.pg_roles role WHERE role.rolname IN('anon','authenticated','lic_reader')
 AND (pg_catalog.has_table_privilege(role.oid,to_regclass('public.operator_job_inventory_chains'),
 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
 OR pg_catalog.has_any_column_privilege(role.oid,to_regclass('public.operator_job_inventory_chains'),
 'SELECT,INSERT,UPDATE,REFERENCES'))) AS readers_denied"""

CAPTURE_SQL="""SELECT 'constraint:'||c.conname AS identity,
 md5(pg_catalog.pg_get_constraintdef(c.oid,false)) AS definition_md5,
 pg_catalog.pg_get_constraintdef(c.oid,false) AS definition
 FROM pg_catalog.pg_constraint c
 WHERE c.contype IN('p','f','u','c') AND (c.conrelid=to_regclass('public.operator_job_inventory_chains')
 OR (c.conrelid=to_regclass('public.operator_jobs')
 AND c.conname IN('operator_jobs_id_channel_key','operator_jobs_inventory_chain_version_check')))
 UNION ALL SELECT 'index:'||r.relname,md5(pg_catalog.pg_get_indexdef(i.indexrelid)),pg_catalog.pg_get_indexdef(i.indexrelid)
 FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class r ON r.oid=i.indexrelid
 WHERE i.indrelid=to_regclass('public.operator_job_inventory_chains')
 ORDER BY identity"""

def definition_sql():
    """Build only from closed release constants; missing/partial pins deny ready."""
    expected={'constraint:'+name for name in CONSTRAINT_NAMES+PARENT_CONSTRAINT_NAMES}
    expected|={'index:'+name for name in INDEX_NAMES}
    if set(DEFINITION_PINS)!=expected or any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value)
                                              for value in DEFINITION_PINS.values()):
        raise DatabaseError('inventory_chain_definition_pins_missing',code='P0001')
    rows=','.join("('"+key+"','"+DEFINITION_PINS[key]+"')" for key in sorted(expected))
    return """WITH expected(identity,definition_md5) AS (VALUES """+rows+"""), actual AS ("""+CAPTURE_SQL+""")
 SELECT (SELECT count(*) FROM actual)=18 AND NOT EXISTS(
 SELECT 1 FROM expected e LEFT JOIN actual a USING(identity)
 WHERE a.definition_md5 IS DISTINCT FROM e.definition_md5) AS definitions_valid,
 (SELECT count(*)=11 FROM pg_catalog.pg_constraint
 WHERE conrelid=to_regclass('public.operator_job_inventory_chains') AND contype IN('p','f','u','c') AND convalidated)
 AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint WHERE conrelid=to_regclass('public.operator_job_inventory_chains') AND contype IN('p','f','u','c')
 AND ((conname='inventory_chains_scan_fkey' AND (contype<>'f' OR NOT condeferrable OR NOT condeferred
 OR confdeltype<>'a' OR confupdtype<>'a' OR confrelid<>to_regclass('public.channel_video_inventory_scans')))
 OR (conname='inventory_chains_parent_fkey' AND (contype<>'f' OR condeferrable OR condeferred
 OR confdeltype<>'c' OR confupdtype<>'a' OR confrelid<>to_regclass('public.operator_jobs')))
 OR (conname NOT IN('inventory_chains_parent_fkey','inventory_chains_scan_fkey') AND (condeferrable OR condeferred))))
 AS constraints_valid,
 (SELECT count(*)=5 FROM pg_catalog.pg_index WHERE indrelid=to_regclass('public.operator_job_inventory_chains')
 AND indisvalid AND indisready AND indislive) AS indexes_valid"""

def _assert_ready(fetch):
    definitions=definition_sql()
    require_boolean_facts(fetch('inventory_chain_acl',ACL_SQL))
    require_boolean_facts(fetch('inventory_chain_catalog',CATALOG_SQL),
                          ('table_valid','columns_valid','readers_denied'))
    require_boolean_facts(fetch('inventory_chain_definitions',definitions),
                          ('definitions_valid','constraints_valid','indexes_valid'))
    return {'ready':True,'chain_version':1}


def assert_inventory_chains_ready(database):
    return _assert_ready(database.read_row)


def assert_inventory_chains_ready_in_transaction(transaction):
    """Reuse the caller's preflight transaction; no second connection or writes."""
    return _assert_ready(lambda name,statement:transaction.fetch_one(statement))


def hot_readiness_sql():
    """Finite catalog-only fragment for the existing HOT maintenance capability."""
    try:
        definitions=definition_sql()
    except DatabaseError:
        # A release with absent/invalid native pins cannot authorize cleanup.
        return 'SELECT false AS inventory_chains_valid'
    return """SELECT coalesce(
     (SELECT roles_valid AND relations_valid AND privileges_valid FROM ("""+ACL_SQL+""") acl)
     AND (SELECT table_valid AND columns_valid AND readers_denied FROM ("""+CATALOG_SQL+""") columns)
     AND (SELECT definitions_valid AND constraints_valid AND indexes_valid FROM ("""+definitions+""") definitions),
     false) AS inventory_chains_valid"""


# Source constants are immutable for the process; reviewed pins require a release.
HOT_READINESS_SQL=hot_readiness_sql()
