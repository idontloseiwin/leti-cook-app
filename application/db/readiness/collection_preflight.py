"""Fixed empty collection preflights with current direct-access/integrity proof.

No retained empty RPC, source row read, grant, new HOT capability or retry.
Metadata checks its concrete writer fields; forced claims retain native empty
casts/identity-lock statements before catalog facts. Existing global schema and
trigger capabilities remain separate and authoritative.
"""
import time

from .capability_facts import require_boolean_facts

from telemetry import emit_event
from ..channel import channel_metadata_batch
from ..videos import video_claims
from . import video_failure_capabilities, inventory_chain_capabilities
from .intrinsic_capabilities import READINESS_SQL as _INTRINSIC_READINESS_SQL
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseError, Operation

_CONTROL_INSERT = ('channel_id','user_work_id','owner_permission','is_hidden','hidden_at')
_CONTROL_UPDATE = ('owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id')
_MODE_JOB_READ = tuple('id channel_id job_type accepted_epoch error_count status phase initial_video_types initial_scope_version targets_frozen_at'.split())
_METADATA_READ = {
    'channels': tuple(sorted(channel_metadata_batch._CHANNEL_FIELDS | {'channel_id'})),
    'channel_playlist_state': tuple('channel_id video_type source_checked_at source_next_after last_result discovery_started_at resume_page_token resume_started_at no_new_streak discovery_error discovery_error_count next_due_at priority status lease_channel_epoch lease_token lease_owner lease_run_id lease_expires_at updated_at'.split()),
    'channel_collection_state': ('channel_id','owner_permission','is_hidden','control_epoch','initial_job_id'),
    'channel_collection_control': ('channel_id','collection_mode'),
    'channel_snapshots': ('channel_id','recorded_at','views','subscribers','video_count','source'),
    'operator_jobs': _MODE_JOB_READ,
}
# These are the entire currently accepted table ACLs, not requests for broader
# rights. Required-column proofs below include every metadata SET field.
_METADATA_TABLES = (
    ('channels','r',('SELECT','INSERT','UPDATE','DELETE')),
    ('channel_playlist_state','r',('SELECT','INSERT','UPDATE','DELETE')),
    ('channel_collection_state','r',('SELECT',)),
    ('channel_collection_control','v',('SELECT',)),
    ('channel_snapshots','r',('SELECT','INSERT','DELETE')),
    ('operator_jobs','r',('SELECT','INSERT','UPDATE','DELETE')),
)
_FORCED_TABLES = (
    ('channels','r',('SELECT','INSERT','UPDATE','DELETE')),
    ('operator_jobs','r',('SELECT','INSERT','UPDATE','DELETE')),
    ('video_inventory','r',('SELECT','INSERT','UPDATE','DELETE')),
    ('channel_collection_state','r',('SELECT',)),
    ('channel_collection_control','v',('SELECT',)),
)
_FORCED_READ = {
    'channels': ('channel_id','initial_long_inventory_status'),
    'operator_jobs': _MODE_JOB_READ,
    'video_inventory': ('video_id','channel_id'),
    'channel_collection_state': ('channel_id','user_work_id','owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id'),
    'channel_collection_control': ('channel_id','collection_mode'),
}
_CATALOG_TEMPLATE = """
WITH required_tables(table_name,kind,privileges) AS (VALUES {tables}),
required_columns(table_name,column_name,privilege) AS (VALUES {columns}),
required_reads(table_name,column_name) AS (VALUES {reads}),
roles AS (SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid),
relations AS MATERIALIZED (
 SELECT expected.*,relation.oid,relation.relowner,relation.relacl,relation.relkind,relation.relrowsecurity,relation.reloptions
 FROM required_tables expected LEFT JOIN pg_catalog.pg_class relation ON relation.oid=pg_catalog.to_regclass('public.'||expected.table_name)),
table_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')),
column_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))
SELECT roles.valid AS roles_valid,
 NOT EXISTS(SELECT 1 FROM relations r WHERE r.oid IS NULL OR r.relkind::text<>r.kind OR pg_catalog.pg_get_userbyid(r.relowner)<>'postgres'
  OR (r.kind='r' AND NOT r.relrowsecurity) OR (r.kind='v' AND NOT coalesce(r.reloptions @> ARRAY['security_invoker=true'],false)))
 AND NOT EXISTS(SELECT 1 FROM required_reads required LEFT JOIN relations r USING(table_name)
  WHERE NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=r.oid AND a.attnum>0 AND NOT a.attisdropped AND a.attname=required.column_name))
 AND NOT EXISTS(SELECT 1 FROM required_columns required LEFT JOIN relations r USING(table_name)
  WHERE NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=r.oid AND a.attnum>0 AND NOT a.attisdropped AND a.attname=required.column_name)) AS relations_valid,
 NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
  WHERE pg_catalog.has_table_privilege(roles.service_oid,r.oid,p.privilege) IS DISTINCT FROM (p.privilege=ANY(r.privileges)))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid CROSS JOIN column_privileges p
  WHERE a.attnum>0 AND NOT a.attisdropped AND pg_catalog.has_column_privilege(roles.service_oid,r.oid,a.attnum,p.privilege)
   IS DISTINCT FROM (p.privilege=ANY(r.privileges) OR EXISTS(SELECT 1 FROM required_columns required
    WHERE required.table_name=r.table_name AND required.column_name=a.attname AND required.privilege=p.privilege)))
 AND NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce(r.relacl,pg_catalog.acldefault('r',r.relowner))) acl
  WHERE acl.grantee<>r.relowner AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable OR NOT(acl.privilege_type=ANY(r.privileges))))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
  WHERE a.attnum>0 AND NOT a.attisdropped AND acl.grantee<>r.relowner AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable
   OR NOT EXISTS(SELECT 1 FROM required_columns required WHERE required.table_name=r.table_name AND required.column_name=a.attname
     AND required.privilege=acl.privilege_type))) AS privileges_valid,
 {extra} AS dependencies_valid
FROM roles
"""
_SEQUENCE_FACT = """EXISTS(SELECT 1 FROM pg_catalog.pg_class c WHERE c.oid=to_regclass('public.channel_collection_epoch_seq')
 AND c.relkind='S' AND pg_catalog.pg_get_userbyid(c.relowner)='postgres'
 AND pg_catalog.has_sequence_privilege(roles.service_oid,c.oid,'USAGE')
 AND NOT pg_catalog.has_sequence_privilege(roles.service_oid,c.oid,'SELECT,UPDATE')
 AND NOT EXISTS(SELECT 1 FROM pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('S',c.relowner))) acl
  WHERE acl.grantee<>c.relowner AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable OR acl.privilege_type<>'USAGE'))
 AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_roles denied WHERE denied.rolname IN ('anon','authenticated')
  AND pg_catalog.has_sequence_privilege(denied.oid,c.oid,'USAGE,SELECT,UPDATE')))"""
_INTRINSIC_FACT = "(SELECT roles_valid AND functions_valid AND triggers_valid FROM (" + _INTRINSIC_READINESS_SQL + ") final_integrity)"


def _catalog(tables,reads,*,snapshot=False):
    # Every identifier originates in the finite definitions above, never input.
    table_rows = ','.join("('%s','%s',ARRAY[%s]::text[])" % (name,kind,','.join("'"+p+"'" for p in privileges))
                          for name,kind,privileges in tables)
    columns = [('channel_collection_state',name,'UPDATE') for name in _CONTROL_UPDATE]
    columns.extend(('channel_collection_state',name,'INSERT') for name in _CONTROL_INSERT)
    if snapshot:
        columns.append(('channel_snapshots','recorded_at','UPDATE'))
    column_rows = ','.join("('%s','%s','%s')" % item for item in columns)
    read_rows = ','.join("('%s','%s')" % (table,name) for table,names in reads.items() for name in names)
    return _CATALOG_TEMPLATE.format(tables=table_rows,columns=column_rows,reads=read_rows,
                                   extra="("+_SEQUENCE_FACT+") AND "+_INTRINSIC_FACT if snapshot else _INTRINSIC_FACT)


METADATA_SQL = _catalog(_METADATA_TABLES,_METADATA_READ,snapshot=True)
FORCED_SQL = _catalog(_FORCED_TABLES,_FORCED_READ)


def _validate(row):
    require_boolean_facts(row, ('roles_valid', 'relations_valid', 'privileges_valid', 'dependencies_valid'))


def _operation(name,*,read_only=False):
    spec = get_operation(name)
    return Operation(name,read_only=read_only,statement_timeout_ms=spec.statement_timeout_ms,
        lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)


def assert_metadata_batch_ready(database):
    """Fixed empty readiness: prove the direct metadata writer before source work."""
    with database.transaction(_operation('update_channel_metadata_batch',read_only=True)) as transaction:
        _validate(transaction.fetch_one(METADATA_SQL))
        inventory_chain_capabilities.assert_inventory_chains_ready_in_transaction(transaction)
    return None


def _forced_ready(database,worker_id,run_id,lease_seconds):
    with database.transaction(_operation('claim_video_metadata_collection_ids')) as transaction:
        # The exact native empty path casts UUID/integer, resolves the empty
        # identity locks, then returns before worker/null-run/lease-range checks.
        # Do not replace it with validation appropriate only to nonempty claims.
        result = video_claims.claim_video_metadata_collection_ids(database,[],worker_id,run_id,[],lease_seconds,
                                                                  transaction=transaction)
        if result != []:
            raise DatabaseError('database_result_shape_invalid')
        _validate(transaction.fetch_one(FORCED_SQL))
        video_failure_capabilities.validate_readiness(transaction.fetch_one(video_failure_capabilities.READINESS_SQL))
        inventory_chain_capabilities.assert_inventory_chains_ready_in_transaction(transaction)
        return []


def assert_forced_metadata_claim_ready(database,*,worker_id,run_id,lease_seconds=300):
    """Keep the original public preflight telemetry identity and exact zero count."""
    name = 'claim_video_metadata_collection_ids'
    emit_event('rpc','sync_engine','db_selectors',name,phase='preflight',direction='request',status='started',count=0)
    started = time.perf_counter()
    try:
        result = _forced_ready(database,worker_id,run_id,lease_seconds)
    except Exception as error:
        emit_event('rpc','db_selectors','sync_engine',name,phase='preflight',direction='response',status='error',
                   duration_ms=round((time.perf_counter()-started)*1000),detail=type(error).__name__)
        raise
    emit_event('rpc','db_selectors','sync_engine',name,phase='preflight',direction='response',status='ok',count=0,
               duration_ms=round((time.perf_counter()-started)*1000))
    return result
