"""Finite schema and readiness capabilities, without stored-check execution.

Each fact query returns exactly one row. Catalog predicates and aggregate counts
stay in PostgreSQL; Python owns rejection, dependencies and the public report.
Final schema retains only thirteen intrinsic trigger hooks; workflow belongs to Python.
No cross-call cache, transaction writes, runtime source parser or RPC fallback.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
import time

from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseError
from ..videos.collection_policy import (assert_legacy_cadence_defaults, cadence_report,
    LEGACY_COMPATIBILITY_SQL, validate_legacy_cadence_defaults)
from . import intrinsic_capabilities, user_work_capabilities, video_failure_capabilities, evidence_retention_capabilities, inventory_receipt_capabilities, operational_retention_capabilities, deleted_channel_retention_capabilities, collection_completion_capabilities
from ..reads import operational_read_queries
from . import schema_release_capabilities, inventory_chain_capabilities
from telemetry import emit_event


def _reject(name, message):
    # Match the old transport's existing finite error disclosure policy.
    allowed = get_operation(name).error_codes
    raise DatabaseError(message if message in allowed else "database_operation_failed", code="P0001")


def _validate(name, row):
    if (not isinstance(row, Mapping) or set(row) != {"violations", "value"}
            or not isinstance(row["violations"], list)
            or len(row["violations"]) != len(_ERRORS[name])
            or any(type(value) is not bool for value in row["violations"])
            or not isinstance(row["value"], Mapping)
            or set(row["value"]) != set(_FIELDS[name])):
        raise DatabaseError("database_result_shape_invalid")
    for failed, message in zip(row["violations"], _ERRORS[name]):
        if failed:
            _reject(name, message)
    return dict(row["value"])


_SCHEDULE_CHECKS = frozenset({
    'assert_video_collection_cadence_schema', 'assert_typed_inventory_quota_defer_schema',
    'assert_typed_video_collection_schema', 'assert_video_collection_common_pool_schema',
    'assert_video_collection_queue_schema',
})


class _Read:
    """One readiness invocation; shared dependencies execute once, never cache globally."""
    def __init__(self, database):
        self.database = database
        self.completed = {}
        self.final_schema_checked = False
        self.legacy_schedule_checked = False

    def facts(self, name):
        if not self.final_schema_checked:
            schema_release_capabilities.assert_schema_release_ready(self.database)
            intrinsic_capabilities.assert_final_schema_ready(self.database)
            inventory_chain_capabilities.assert_inventory_chains_ready(self.database)
            self.final_schema_checked = True
        if name not in self.completed:
            prerequisite = _PREREQUISITES.get(name)
            if prerequisite is not None:
                statement, errors = prerequisite
                facts = self.database.read_row("capability_prerequisite_" + name.removeprefix("assert_").removesuffix("_schema"), statement)
                if (not isinstance(facts, Mapping) or set(facts) != {"violations"}
                        or not isinstance(facts["violations"], list)
                        or len(facts["violations"]) != len(errors)
                        or any(type(value) is not bool for value in facts["violations"])):
                    raise DatabaseError("database_result_shape_invalid")
                for failed, message in zip(facts["violations"], errors):
                    if failed:
                        _reject(name, message)
            row = self.database.read_row("capability_" + name.removeprefix("assert_").removesuffix("_schema"),
                                         _QUERIES[name])
            result = _validate(name, row)
            if name in _SCHEDULE_CHECKS:
                report = cadence_report(self.database)
                if not self.legacy_schedule_checked:
                    assert_legacy_cadence_defaults(self.database)
                    self.legacy_schedule_checked = True
                # The SQL predicates above still audit the retained historical
                # table, relation, constraints and ACLs. Public policy diagnostics
                # describe the injected runtime values, not those legacy seeds.
                if name == 'assert_typed_video_collection_schema':
                    result.pop('fresh_cadence_days')
                    result.pop('old_cadence_days')
                    result['metadata_cadence_seconds'] = report['metadata_cadence_seconds']
                if name == 'assert_typed_inventory_quota_defer_schema':
                    result.pop('cadence_jitter_seconds')
                    result['typed_jitter_seconds'] = report['typed_jitter_seconds']
                if name == 'assert_video_collection_cadence_schema':
                    result.update(report)
                result['schedule_source'] = report['schedule_source']
            self.completed[name] = result
        return dict(self.completed[name])


def telemetry_check(database, check, *, phase=None):
    """Keep existing preflight selector event identities while SQL ownership moves."""
    if check not in CHECKS.values():
        raise ValueError("capability_check_invalid")
    name = check.__name__
    emit_event("rpc", "sync_engine", "db_selectors", name,
               phase=phase, direction="request", status="started", count=None)
    started = time.perf_counter()
    try:
        result = check(database)
    except Exception as error:
        emit_event("rpc", "db_selectors", "sync_engine", name, phase=phase,
                   direction="response", status="error",
                   duration_ms=round((time.perf_counter()-started)*1000), detail=type(error).__name__)
        raise
    emit_event("rpc", "db_selectors", "sync_engine", name, phase=phase,
               direction="response", status="ok", count=None,
               duration_ms=round((time.perf_counter()-started)*1000))
    return result


def _channel_discovery_paging(read):
    _inventory_switch(read)
    result = read.facts('assert_channel_discovery_paging_schema')
    return result

def assert_channel_discovery_paging_schema(database):
    return _channel_discovery_paging(_Read(database))


def _channel_discovery_queue(read):
    result = read.facts('assert_channel_discovery_queue_schema')
    return result

def assert_channel_discovery_queue_schema(database):
    return _channel_discovery_queue(_Read(database))


def _channel_sync_control_ready(read):
    result = read.facts('assert_channel_sync_control_ready')
    result["health"] = operational_read_queries.get_channel_sync_control_health(read.database)
    return result

def assert_channel_sync_control_ready(database):
    return _channel_sync_control_ready(_Read(database))


def _full_initial(read):
    result = read.facts('assert_full_initial_schema')
    return result

def assert_full_initial_schema(database):
    return _full_initial(_Read(database))


def _inventory_receipts(read):
    _inventory_switch(read)
    result = read.facts('assert_inventory_receipts_schema')
    return result

def assert_inventory_receipts_schema(database):
    return _inventory_receipts(_Read(database))


def _inventory_switch(read):
    result = read.facts('assert_inventory_switch_schema')
    return result

def assert_inventory_switch_schema(database):
    return _inventory_switch(_Read(database))


def _operator_jobs_ready(read):
    _inventory_switch(read)
    _full_initial(read)
    result = read.facts('assert_operator_jobs_ready')
    return result

def assert_operator_jobs_ready(database):
    return _operator_jobs_ready(_Read(database))


def _typed_inventory_canonical_repair(read):
    result = read.facts('assert_typed_inventory_canonical_repair_schema')
    return result

def assert_typed_inventory_canonical_repair_schema(database):
    return _typed_inventory_canonical_repair(_Read(database))


def _typed_inventory_foundation(read):
    _inventory_switch(read)
    result = read.facts('assert_typed_inventory_foundation_schema')
    return result

def assert_typed_inventory_foundation_schema(database):
    return _typed_inventory_foundation(_Read(database))


def _typed_inventory_quota_defer(read):
    _video_collection_cadence(read)
    _inventory_receipts(read)
    _inventory_switch(read)
    result = read.facts('assert_typed_inventory_quota_defer_schema')
    return result

def assert_typed_inventory_quota_defer_schema(database):
    return _typed_inventory_quota_defer(_Read(database))


def _typed_inventory_runtime(read):
    foundation = _typed_inventory_foundation(read)
    _inventory_receipts(read)
    _typed_inventory_quota_defer(read)
    _operator_jobs_ready(read)
    result = read.facts('assert_typed_inventory_runtime_schema')
    result["foundation"] = foundation
    return result

def assert_typed_inventory_runtime_schema(database):
    return _typed_inventory_runtime(_Read(database))


def _typed_video_collection(read):
    _video_collection_cadence(read)
    result = read.facts('assert_typed_video_collection_schema')
    result["health"] = operational_read_queries.get_typed_video_collection_health(read.database)
    return result

def assert_typed_video_collection_schema(database):
    return _typed_video_collection(_Read(database))


def _video_collection_cadence(read):
    result = read.facts('assert_video_collection_cadence_schema')
    return result

def assert_video_collection_cadence_schema(database):
    return _video_collection_cadence(_Read(database))


def _video_collection_common_pool(read):
    result = read.facts('assert_video_collection_common_pool_schema')
    return result

def assert_video_collection_common_pool_schema(database):
    return _video_collection_common_pool(_Read(database))


def _video_collection_queue(read):
    result = read.facts('assert_video_collection_queue_schema')
    return result

def assert_video_collection_queue_schema(database):
    return _video_collection_queue(_Read(database))


def assert_channel_discovery_queue_ready(database):
    read = _Read(database)
    _typed_inventory_runtime(read)
    result = _channel_discovery_queue(read)
    if result.get("status") != "ok" or result.get("reconciliation_required") is not False:
        _reject("assert_channel_discovery_queue_ready", "database_operation_failed")
    return {**result, "status": "ok"}


def assert_video_collection_queue_ready(database):
    result = _video_collection_queue(_Read(database))
    if result.get("status") != "ok" or result.get("reconciliation_required") is not False:
        _reject("assert_video_collection_queue_ready", "database_operation_failed")
    return {**result, "status": "ok"}


def get_hot_runtime_contract(database):
    # A driver-owned fixed query capability is the only maintenance path.
    row = database.read_maintenance_capability("hot_runtime")
    if not isinstance(row, Mapping) or set(row) != {"value"} or not isinstance(row["value"], Mapping):
        raise DatabaseError("database_result_shape_invalid")
    value = dict(row["value"])
    if (set(value) != {"version", "as_of", "schema_ready"} or type(value["version"]) is not int
            or value["version"] != 1 or type(value["schema_ready"]) is not bool
            or not isinstance(value["as_of"], str)):
        raise DatabaseError("database_result_shape_invalid")
    if value["schema_ready"]:
        schema_release_capabilities.validate_readiness(database.read_maintenance_capability("schema_release"))
        validate_legacy_cadence_defaults(database.read_maintenance_capability("collection_cadence"))
        _validate("assert_inventory_switch_schema", database.read_maintenance_capability("inventory_switch"))
        user_work_capabilities.validate_readiness(database.read_maintenance_capability("user_work_restore"))
        video_failure_capabilities.validate_readiness(database.read_maintenance_capability("video_failure"))
        evidence_retention_capabilities.validate_readiness(database.read_maintenance_capability("evidence_retention"))
        inventory_receipt_capabilities.validate_readiness(database.read_maintenance_capability("inventory_receipts"))
        operational_retention_capabilities.validate_readiness(database.read_maintenance_capability("operational_retention"))
        deleted_channel_retention_capabilities.validate_readiness(database.read_maintenance_capability("deleted_channels"))
        collection_completion_capabilities.validate_readiness(database.read_maintenance_capability("collection_completion"))
    return value

_FIELDS = MappingProxyType({
    'assert_channel_discovery_paging_schema': ('status', 'table', 'resume_page_token_type', 'resume_started_at_type', 'no_new_streak_type', 'no_new_streak_not_null', 'policy', 'max_periodic_hours'),
    'assert_channel_discovery_queue_schema': ('table', 'status', 'missing_or_wrong_type', 'reconciliation_required', 'claim_contract', 'completion_idempotent'),
    'assert_channel_sync_control_ready': ('ready', 'schema_version', 'quota_neutral', 'canonical_channel_control', 'scheduling_projection_is_diagnostic', 'health'),
    'assert_full_initial_schema': ('ready', 'initial_scope_version', 'workset', 'page_target_limit'),
    'assert_inventory_receipts_schema': ('status', 'inventory_receipt_scopes', 'service_direct_writes'),
    'assert_inventory_switch_schema': ('status', 'schema_version', 'shared_page_owner', 'lease_owner', 'durable_fairness', 'token_history', 'page_limit', 'continuation_proof_limit', 'initial_chunk_pages'),
    'assert_operator_jobs_ready': ('ready', 'schema_version', 'full_initial_scope_version', 'initial_total_limits_enforced', 'initial_inventory_scan_bridge', 'initial_chunk_pages', 'initial_quota_limit_enforced', 'full_profile_request_units_per_batch', 'unknown_completion_preserves_pending', 'job_table', 'target_table', 'retry_request_table', 'initial_inventory_job_type', 'initial_full_hydration', 'auto_hydration_max_items', 'legacy_auto_hydration_max_items', 'initial_video_types', 'channel_epoch_required', 'frozen_initial_scope', 'mixed_initial_claim', 'unfinished_stream_checked', 'verify_lease_seconds', 'idempotent_verify', 'atomic_workset_materialization', 'repairable_target_count_mismatch', 'retry_idempotency_key', 'mismatch_repair_source', 'quota_neutral_migration'),
    'assert_typed_inventory_canonical_repair_schema': ('status', 'schema_version', 'authoritative_evidence', 'canonical_channel_mutation', 'max_atomic_repairs', 'collection_state_lock_contract', 'minimum_postgres_version', 'repair_statement_timeout_seconds'),
    'assert_typed_inventory_foundation_schema': ('status', 'canonical_inventory_key', 'product_exposure_default', 'membership_conflicts', 'absence_requires', 'active_long_discovery_owner', 'typed_nonlong_runtime_enabled_rows', 'product_exposure_ready_rows'),
    'assert_typed_inventory_quota_defer_schema': ('status', 'schema_version', 'claim_limit', 'retry_floor_seconds', 'retry_until', 'lost_ack_receipts', 'active_scan_state_preserved', 'cadence_jitter_seconds', 'quota_claim_floor_guard'),
    'assert_typed_inventory_runtime_schema': ('status', 'schema_version', 'foundation', 'runtime_enabled_rows', 'product_exposure_ready_rows', 'zero_enabled_rows_allowed', 'runtime_video_types', 'canonical_identity', 'page_limit', 'continuation_delay_minutes', 'head_cadence_hours', 'full_reconciliation_days', 'product_exposure_independent_of_active_scan', 'statistics_collection', 'separate_current_data_collection', 'lost_ack_receipts', 'durable_fairness', 'initial_chunk_pages'),
    'assert_typed_video_collection_schema': ('channel_control_fenced', 'lease_channel_epoch', 'status', 'schema_version', 'repair_version', 'bounded_claim_batch', 'operational_eligibility_checked', 'runtime_video_types', 'max_request_batch', 'fresh_through_days_inclusive', 'fresh_cadence_days', 'old_cadence_days', 'full_metadata_each_refresh', 'completed_streams_only', 'exact_receipt_payload_fencing', 'stream_lifecycle_model', 'product_exposure_changed', 'product_exposure_ready_rows', 'runtime_enabled_rows', 'long_typed_drift', 'typed_long_clock_drift', 'identity_drift', 'disabled_active_drift', 'ineligible_active_drift', 'health'),
    'assert_video_collection_cadence_schema': ('status', 'selector', 'long_bounds', 'typed_bounds', 'typed_jitter_seconds'),
    'assert_video_collection_common_pool_schema': ('channel_control_fenced', 'lease_channel_epoch', 'unified_due_at', 'priority_storage', 'status', 'schema_version', 'common_pool_version', 'mixed_claim', 'sole_lease_owner', 'max_request_batch', 'max_claim_rows', 'max_inspected_rows', 'ordering_policy', 'priority_ordering', 'atomic_long_availability_restore', 'availability_incident_owner', 'profile_single_write', 'metadata_replay_noop', 'category_clock_retired'),
    'assert_video_collection_queue_schema': ('channel_control_fenced', 'lease_channel_epoch', 'status', 'table', 'readiness_mode', 'reconciliation_required', 'remaining_count', 'remaining_count_exact', 'invalidated_at', 'last_reconciled_at', 'cadence_rules', 'max_claim_size', 'policy_version', 'metadata_completion_contract_version', 'stream_completion_evidence'),
})

_ERRORS = MappingProxyType({
    'assert_channel_discovery_paging_schema': ('channel discovery schema mismatch: token=%, started_at=%, streak=%, streak_not_null=%', 'channel discovery schema contains a negative no-new streak'),
    'assert_channel_discovery_queue_schema': (),
    'assert_channel_sync_control_ready': ('channel sync safe error columns are incomplete', 'channel_collection_control_contract_missing', 'channel_collection_control_acl_invalid', 'channel_collection_control_reconciliation_incomplete'),
    'assert_full_initial_schema': ('full_initial_function_contract_invalid: %', 'full_initial_function_acl_invalid: %', 'full_initial_columns_invalid', 'full_initial_constraint_missing'),
    'assert_inventory_receipts_schema': ('inventory_receipts_storage_missing', 'inventory_receipts_storage_acl_invalid', 'inventory_receipts_constraints_invalid'),
    'assert_inventory_switch_schema': ('inventory_switch_old_claim_owner_present', 'inventory_dispatch_sequence_contract_invalid', 'inventory_switch_column_contract_invalid: %.%', 'inventory_dispatch_default_invalid', 'inventory_switch_constraint_missing: %', 'inventory_job_scan_fk_invalid', 'inventory_switch_index_missing', 'inventory_switch_table_security_invalid: %', 'inventory_constraint_fingerprint_invalid: %', 'inventory_constraint_fingerprint_invalid: %', 'inventory_constraint_fingerprint_invalid: %', 'inventory_constraint_fingerprint_invalid: %', 'inventory_constraint_fingerprint_invalid: %', 'inventory_constraint_fingerprint_invalid: %', 'inventory_index_fingerprint_invalid: %', 'inventory_index_fingerprint_invalid: %', 'retired_inventory_rpc_present', 'retired_inventory_rpc_present', 'retired_inventory_rpc_present', 'retired_inventory_rpc_present', 'retired_inventory_rpc_present', 'retired_inventory_rpc_present', 'retired_inventory_rpc_present', 'retired_inventory_rpc_present', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid'),
    'assert_operator_jobs_ready': ('durable initial hydration RPC contract is incomplete', 'durable initial hydration columns are incomplete', 'all_format_initial_hydration_columns_missing', 'operator_job_channel_scope_contract_missing'),
    'assert_typed_inventory_canonical_repair_schema': ('typed_inventory_canonical_repair_postgres_version_invalid', 'typed_inventory_canonical_repair_lock_schema_invalid', 'typed_inventory_canonical_repair_lock_view_invalid', 'typed_inventory_canonical_repair_lock_view_dependency_invalid', 'typed_inventory_canonical_repair_lock_view_acl_invalid', 'typed_inventory_canonical_repair_base_state_acl_invalid'),
    'assert_typed_inventory_foundation_schema': ('typed_inventory_table_missing_or_rls_disabled: %', 'typed_inventory_table_publicly_readable: %', 'typed_inventory_product_exposure_default_invalid', 'video_inventory_must_have_one_canonical_row', 'video_inventory_resolution_must_be_security_invoker'),
    'assert_typed_inventory_quota_defer_schema': ('typed_video_collection_cadence_or_jitter_invalid',),
    'assert_typed_inventory_runtime_schema': ('typed_inventory_active_scan_projection_drift',),
    'assert_typed_video_collection_schema': ('typed_video_collection_relation_missing', 'typed_video_collection_relation_security_contract_invalid: %', 'typed_video_collection_state_columns_missing', 'typed_video_collection_receipt_contract_invalid', 'typed_video_collection_cadence_invalid', 'typed_collection_bounded_repair_schema_missing', 'typed_video_collection_state_drift', 'typed_video_collection_product_exposure_must_remain_closed', 'video_channel_lease_epoch_contract_invalid'),
    'assert_video_collection_cadence_schema': (),
    'assert_video_collection_common_pool_schema': ('common_pool_index_contract_invalid', 'typed_availability_incident_owner_not_ready', 'video_queue_cleanup_schema_not_ready', 'video_category_clock_not_retired', 'video_channel_lease_epoch_contract_invalid'),
    'assert_video_collection_queue_schema': ('videos.metadata_checked_at must be timestamp with time zone', 'videos.stream_actual_end_at must be timestamp with time zone', 'videos stream completion evidence constraint is missing or invalid', 'video collection due/source clocks must be timestamp with time zone', 'video_collection_state.lease_token must be uuid', 'video_collection_state.last_claim_run_id must be uuid', 'video_snapshots.collection_attempt_id must be uuid', 'one or more video collection indexes are missing', 'legacy channel visibility collection trigger must be absent', 'video collection readiness control row is missing', 'video collection cadence expected 12 enabled v1 rules, found %', 'video_channel_lease_epoch_contract_invalid', 'video_unfenced_legacy_overload_present'),
})

# Final catalog/state predicates preserve original structural and live-value contracts.
# Cadence-table predicates now audit legacy compatibility only. Runtime values
# are taken from the process snapshot after these native facts are validated.

_ASSERT_CHANNEL_DISCOVERY_PAGING_SCHEMA_SQL = r"""
WITH observed_1 AS MATERIALIZED (SELECT pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS page_token_type FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS relation
        ON relation.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
        AND relation.relname = 'channel_playlist_state'
        AND attribute.attname = 'resume_page_token'
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped),
observed_2 AS MATERIALIZED (SELECT pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS started_at_type FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS relation
        ON relation.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
        AND relation.relname = 'channel_playlist_state'
        AND attribute.attname = 'resume_started_at'
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped),
observed_3 AS MATERIALIZED (SELECT pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS no_new_streak_type, attribute.attnotnull AS no_new_streak_not_null FROM pg_catalog.pg_attribute AS attribute
    JOIN pg_catalog.pg_class AS relation
        ON relation.oid = attribute.attrelid
    JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
        AND relation.relname = 'channel_playlist_state'
        AND attribute.attname = 'no_new_streak'
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped)
SELECT ARRAY[
((SELECT page_token_type FROM observed_1) IS DISTINCT FROM 'text'
        OR (SELECT started_at_type FROM observed_2) IS DISTINCT FROM 'timestamp with time zone'
        OR (SELECT no_new_streak_type FROM observed_3) IS DISTINCT FROM 'integer'
        OR (SELECT no_new_streak_not_null FROM observed_3) IS DISTINCT FROM true) IS TRUE,
(EXISTS (
        SELECT 1
        FROM public.channel_playlist_state
        WHERE no_new_streak < 0
    )) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'status', 'ok',
        'table', 'channel_playlist_state',
        'resume_page_token_type', (SELECT page_token_type FROM observed_1),
        'resume_started_at_type', (SELECT started_at_type FROM observed_2),
        'no_new_streak_type', (SELECT no_new_streak_type FROM observed_3),
        'no_new_streak_not_null', (SELECT no_new_streak_not_null FROM observed_3),
        'policy', 'balanced',
        'max_periodic_hours', 24
    ) AS value
"""

_ASSERT_CHANNEL_DISCOVERY_QUEUE_SCHEMA_SQL = r"""
WITH schema_columns AS MATERIALIZED (
    SELECT ns.nspname AS table_schema, relation.relname AS table_name,
           attribute.attname AS column_name,
           pg_catalog.format_type(CASE WHEN type_row.typtype='d' THEN type_row.typbasetype
               ELSE attribute.atttypid END, NULL) AS data_type,
           base_type.typname AS udt_name,
           CASE WHEN attribute.attnotnull OR (type_row.typtype='d' AND type_row.typnotnull)
               THEN 'NO' ELSE 'YES' END AS is_nullable
    FROM pg_catalog.pg_attribute attribute
    JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
    JOIN pg_catalog.pg_namespace ns ON ns.oid=relation.relnamespace
    JOIN pg_catalog.pg_type type_row ON type_row.oid=attribute.atttypid
    JOIN pg_catalog.pg_type base_type ON base_type.oid=CASE WHEN type_row.typtype='d'
        THEN type_row.typbasetype ELSE type_row.oid END
    WHERE ns.nspname IN ('public','operator_private') AND attribute.attnum>0
      AND NOT attribute.attisdropped AND relation.relkind IN ('r','v','f','p')
),
observed_1 AS MATERIALIZED (SELECT array_agg(required.column_name ORDER BY required.column_name) AS v_missing FROM (
        VALUES
            ('channel_id', 'text'),
            ('next_due_at', 'timestamp with time zone'),
            ('lease_token', 'uuid'),
            ('lease_channel_epoch', 'bigint'),
            ('lease_expires_at', 'timestamp with time zone'),
            ('resume_pending', 'boolean'),
            ('no_new_streak', 'integer')
    ) AS required(column_name, data_type)
    WHERE NOT EXISTS (
        SELECT 1
        FROM schema_columns AS schema_column
        WHERE schema_column.table_schema = 'public'
          AND schema_column.table_name = 'channel_playlist_state'
          AND schema_column.column_name = required.column_name
          AND schema_column.data_type = required.data_type
    )),
observed_2 AS MATERIALIZED (SELECT control.reconciliation_required AS v_reconciliation_required FROM public.channel_discovery_queue_control AS control
    WHERE control.singleton)
SELECT ARRAY[

]::boolean[] AS violations,
 jsonb_build_object(
        'table', 'channel_playlist_state',
        'status', CASE
            WHEN COALESCE(array_length((SELECT v_missing FROM observed_1), 1), 0) = 0 THEN 'ok'
            ELSE 'error'
        END,
        'missing_or_wrong_type', COALESCE(to_jsonb((SELECT v_missing FROM observed_1)), '[]'::jsonb),
        'reconciliation_required', COALESCE((SELECT v_reconciliation_required FROM observed_2), true),
        'claim_contract', 'for_update_skip_locked',
        'completion_idempotent', true
    ) AS value
"""

_ASSERT_CHANNEL_SYNC_CONTROL_READY_SQL = r"""
WITH schema_columns AS MATERIALIZED (
    SELECT ns.nspname AS table_schema, relation.relname AS table_name,
           attribute.attname AS column_name,
           pg_catalog.format_type(CASE WHEN type_row.typtype='d' THEN type_row.typbasetype
               ELSE attribute.atttypid END, NULL) AS data_type,
           base_type.typname AS udt_name,
           CASE WHEN attribute.attnotnull OR (type_row.typtype='d' AND type_row.typnotnull)
               THEN 'NO' ELSE 'YES' END AS is_nullable
    FROM pg_catalog.pg_attribute attribute
    JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
    JOIN pg_catalog.pg_namespace ns ON ns.oid=relation.relnamespace
    JOIN pg_catalog.pg_type type_row ON type_row.oid=attribute.atttypid
    JOIN pg_catalog.pg_type base_type ON base_type.oid=CASE WHEN type_row.typtype='d'
        THEN type_row.typbasetype ELSE type_row.oid END
    WHERE ns.nspname IN ('public','operator_private') AND attribute.attnum>0
      AND NOT attribute.attisdropped AND relation.relkind IN ('r','v','f','p')
)
SELECT ARRAY[
(not exists (
        select 1
        from schema_columns
        where table_schema = 'public'
          and table_name = 'channels'
          and column_name = 'initial_long_inventory_error_code'
    )) IS TRUE,
(to_regclass('public.channel_collection_state') is null
      or to_regclass('public.channel_collection_control') is null
      or to_regclass('public.channels_with_control') is null
      or to_regclass('public.channel_collection_epoch_seq') is null) IS TRUE,
(not exists(select 1 from pg_catalog.pg_class
         where oid=to_regclass('public.channel_collection_state') and relkind='r' and relrowsecurity
           and pg_catalog.pg_get_userbyid(relowner)='postgres')
      or not exists(select 1 from pg_catalog.pg_roles where rolname='service_role' and rolbypassrls and not rolsuper and not rolcanlogin)
      or not pg_catalog.has_table_privilege('service_role','public.channel_collection_state','SELECT')
      or pg_catalog.has_table_privilege('service_role','public.channel_collection_state','INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
      or pg_catalog.has_any_column_privilege('service_role','public.channel_collection_state','REFERENCES')
      or exists(select 1 from unnest(ARRAY['channel_id','user_work_id','owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id']) required(column_name)
           where not exists(select 1 from pg_catalog.pg_attribute a
             where a.attrelid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped and a.attname=required.column_name))
      or exists(select 1 from pg_catalog.pg_attribute a
           where a.attrelid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped
             and pg_catalog.has_column_privilege('service_role',a.attrelid,a.attnum,'INSERT')
                 is distinct from (a.attname=ANY(ARRAY['channel_id','user_work_id','owner_permission','is_hidden','hidden_at'])))
      or exists(select 1 from pg_catalog.pg_attribute a
           where a.attrelid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped
             and pg_catalog.has_column_privilege('service_role',a.attrelid,a.attnum,'UPDATE')
                 is distinct from (a.attname=ANY(ARRAY['owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id'])))
      or exists(select 1 from (VALUES ('anon'),('authenticated')) denied(role_name)
           where pg_catalog.has_table_privilege(denied.role_name,'public.channel_collection_state','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
              or pg_catalog.has_any_column_privilege(denied.role_name,'public.channel_collection_state','SELECT,INSERT,UPDATE,REFERENCES'))
      or exists(select 1 from pg_catalog.pg_class c
           cross join lateral pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('r',c.relowner))) acl
           where c.oid=to_regclass('public.channel_collection_state') and acl.grantee<>c.relowner
             and (acl.grantee<>'service_role'::regrole or acl.privilege_type<>'SELECT' or acl.is_grantable))
      or exists(select 1 from pg_catalog.pg_attribute a join pg_catalog.pg_class c on c.oid=a.attrelid
           cross join lateral pg_catalog.aclexplode(a.attacl) acl
           where c.oid=to_regclass('public.channel_collection_state') and a.attnum>0 and not a.attisdropped
             and acl.grantee<>c.relowner
             and (acl.grantee<>'service_role'::regrole or acl.is_grantable
               or not ((acl.privilege_type='UPDATE' and a.attname=ANY(ARRAY['owner_permission','is_hidden','hidden_at','control_epoch','initial_job_id']))
                    or (acl.privilege_type='INSERT' and a.attname=ANY(ARRAY['channel_id','user_work_id','owner_permission','is_hidden','hidden_at'])))))
      or not exists(select 1 from pg_catalog.pg_class c
           where c.oid=to_regclass('public.channel_collection_epoch_seq') and c.relkind='S'
             and pg_catalog.pg_get_userbyid(c.relowner)='postgres')
      or not pg_catalog.has_sequence_privilege('service_role','public.channel_collection_epoch_seq','USAGE')
      or pg_catalog.has_sequence_privilege('service_role','public.channel_collection_epoch_seq','SELECT,UPDATE')
      or exists(select 1 from (VALUES ('anon'),('authenticated')) denied(role_name)
           where pg_catalog.has_sequence_privilege(denied.role_name,'public.channel_collection_epoch_seq','USAGE,SELECT,UPDATE'))
      or exists(select 1 from pg_catalog.pg_class c
           cross join lateral pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('S',c.relowner))) acl
           where c.oid=to_regclass('public.channel_collection_epoch_seq') and acl.grantee<>c.relowner
             and (acl.grantee<>'service_role'::regrole or acl.privilege_type<>'USAGE' or acl.is_grantable))) IS TRUE,
(exists(select 1 from public.channels c left join public.channel_collection_state control
         on control.channel_id=c.channel_id
       where control.channel_id is null or control.user_work_id is distinct from c.user_work_id
         or control.control_epoch is null or control.control_epoch<=0)) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'ready', true,
        'schema_version', 3,
        'quota_neutral', true,
        'canonical_channel_control', true,
        'scheduling_projection_is_diagnostic', true,
        'health', NULL::jsonb
    ) AS value
"""

_ASSERT_FULL_INITIAL_SCHEMA_SQL = r"""
WITH requirements_1(signature,body_md5,security_definer,service_access) AS (VALUES
('public.guard_hot_operator_target_scope()','6d224d5075e65a755c8d21f5ecc3efb6',true,true)),
requirements_2(rel,name) AS (VALUES('public.operator_jobs','operator_jobs_initial_scope_check'),
   ('public.operator_jobs','operator_jobs_phase_check'),('public.operator_jobs','operator_jobs_counts_check'),
   ('public.operator_job_targets','operator_job_targets_unresolved_check'),
   ('operator_private.operator_job_retry_requests','operator_job_retry_requests_scope_check'))
SELECT ARRAY[
(EXISTS (SELECT 1 FROM requirements_1 AS x WHERE ( (to_regprocedure(x.signature)) IS NULL OR NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid=(to_regprocedure(x.signature)) AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname='postgres')
    AND p.prosecdef=x.security_definer AND md5(replace(replace(p.prosrc,E'\r\n',E'\n'),E'\r',E'\n'))=x.body_md5)
  ) IS TRUE)) IS TRUE,
(EXISTS (SELECT 1 FROM requirements_1 AS x WHERE ( EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
     WHERE p.oid=(to_regprocedure(x.signature)) AND (a.grantee=0 OR (a.grantee<>p.proowner AND (a.grantee<>(SELECT oid FROM pg_roles WHERE rolname='service_role') OR NOT x.service_access OR a.is_grantable))))
    OR has_function_privilege('anon',(to_regprocedure(x.signature)),'EXECUTE') OR has_function_privilege('authenticated',(to_regprocedure(x.signature)),'EXECUTE')
    OR (x.service_access AND NOT has_function_privilege('service_role',(to_regprocedure(x.signature)),'EXECUTE'))
  ) IS TRUE)) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=pg_catalog.to_regclass('public.operator_jobs') AND attname='initial_scope_version' AND attnotnull AND atttypid='int2'::regtype)
  OR NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=pg_catalog.to_regclass('public.operator_jobs') AND attname='targets_frozen_at' AND NOT attnotnull AND atttypid='timestamptz'::regtype)
  OR EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=pg_catalog.to_regclass('public.operator_jobs') AND attname IN('max_items','max_pages','max_quota_units') AND attnotnull)
  OR NOT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=pg_catalog.to_regclass('public.operator_job_targets') AND attname='video_type' AND NOT attnotnull)
  OR NOT EXISTS(SELECT 1 FROM pg_index WHERE indexrelid=pg_catalog.to_regclass('public.operator_job_targets_unresolved_idx') AND indisvalid AND indisready)) IS TRUE,
(EXISTS (SELECT 1 FROM requirements_2 AS q WHERE ( NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=(SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname=split_part(q.rel,'.',1) AND relation.relname=split_part(q.rel,'.',2)) AND conname=q.name AND convalidated)
  ) IS TRUE)) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object('ready',true,'initial_scope_version',1,'workset','observed_three_scan_union','page_target_limit',50) AS value
"""

_ASSERT_INVENTORY_RECEIPTS_SCHEMA_SQL = r"""
SELECT ARRAY[
(not exists(select 1 from pg_catalog.pg_class c
   where c.oid=to_regclass('public.typed_inventory_receipts') and c.relkind='r'
     and c.relrowsecurity and pg_catalog.pg_get_userbyid(c.relowner)='postgres')) IS TRUE,
(NOT coalesce((SELECT roles_valid AND relations_valid AND privileges_valid FROM (
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
) direct_receipt_rights),false)) IS TRUE,
(exists(select 1 from (values
   ('typed_inventory_receipts_pkey','b6374e13a4908286ff9d295f17621e70'),
   ('typed_inventory_receipts_scan_id_fkey','d806295b1cac711d0f801d131112ce05'),
   ('typed_inventory_receipts_result_check','b8558f8e9fd998ff27ee4488f6723c81'),
   ('typed_inventory_receipts_scope_check','83f32fefa4a6436d7fce31e469361cfd')
 ) expected(name,definition_md5) left join pg_catalog.pg_constraint c
   on c.conrelid=pg_catalog.to_regclass('public.typed_inventory_receipts') and c.conname=expected.name
 where c.oid is null or not c.convalidated
   or md5(pg_catalog.pg_get_constraintdef(c.oid))<>expected.definition_md5)) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object('status','ok','inventory_receipt_scopes',2,'service_direct_writes',true) AS value
"""

_ASSERT_INVENTORY_SWITCH_SCHEMA_SQL = r"""
WITH
requirements_2(table_name,column_name,type_name,not_null) AS (VALUES
  ('channel_playlist_state','dispatch_ordinal','bigint',true),
  ('channel_video_inventory_scans','token_history_verified','boolean',true),
  ('typed_inventory_receipts','next_page_token_hash','bytea',false),
  ('operator_jobs','inventory_scan_id','uuid',false),('operator_jobs','inventory_checkpoint','jsonb',true)
 ),
requirements_3(table_name,constraint_name,kind) AS (VALUES
  ('channel_playlist_state','channel_playlist_dispatch_ordinal_check','c'),
  ('channel_playlist_state','channel_playlist_inventory_active_scan_check','c'),
  ('typed_inventory_receipts','typed_inventory_receipt_token_hash_check','c'),
  ('operator_jobs','operator_jobs_inventory_scan_fkey','f'),
  ('operator_jobs','operator_jobs_inventory_checkpoint_check','c'),
  ('operator_jobs','operator_jobs_inventory_ack_check','c')
 )
SELECT ARRAY[
((SELECT function.oid FROM pg_catalog.pg_proc function JOIN pg_catalog.pg_namespace namespace ON namespace.oid=function.pronamespace WHERE namespace.nspname='operator_private' AND function.proname='claim_inventory_states' AND function.proargtypes='23 1009 1009 25 25 23 3802 16'::pg_catalog.oidvector) IS NOT NULL) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_sequence s ON s.seqrelid=c.oid
   WHERE c.oid=(SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='inventory_dispatch_seq') AND c.relkind='S'
     AND c.relowner=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname='postgres') AND s.seqtypid='int8'::regtype AND s.seqmin=1 AND NOT s.seqcycle)
   OR EXISTS(SELECT 1 FROM pg_catalog.pg_class c CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce(c.relacl,pg_catalog.acldefault('S',c.relowner))) a
      WHERE c.oid=(SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='inventory_dispatch_seq') AND a.grantee<>c.relowner AND (a.grantee IS DISTINCT FROM (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role')
       OR a.is_grantable OR a.privilege_type<>'USAGE'))
   OR has_sequence_privilege('service_role',(SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='inventory_dispatch_seq'),'USAGE') IS DISTINCT FROM true
   OR has_sequence_privilege('service_role',(SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='inventory_dispatch_seq'),'SELECT,UPDATE')
   OR has_sequence_privilege('anon',(SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='inventory_dispatch_seq'),'USAGE,SELECT,UPDATE')
   OR has_sequence_privilege('authenticated',(SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='inventory_dispatch_seq'),'USAGE,SELECT,UPDATE')) IS TRUE,
(EXISTS (SELECT 1 FROM requirements_2 AS cols WHERE ( NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=to_regclass('public.'||cols.table_name)
    AND a.attname=cols.column_name AND NOT a.attisdropped AND a.atttypid=to_regtype(cols.type_name) AND a.attnotnull=cols.not_null)
  ) IS TRUE)) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attrdef d JOIN pg_catalog.pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum
    WHERE a.attrelid=pg_catalog.to_regclass('public.channel_playlist_state') AND a.attname='dispatch_ordinal'
      AND pg_get_expr(d.adbin,d.adrelid)='nextval(''operator_private.inventory_dispatch_seq''::regclass)')) IS TRUE,
(EXISTS (SELECT 1 FROM requirements_3 AS checks WHERE ( NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=to_regclass('public.'||checks.table_name)
    AND c.conname=checks.constraint_name AND c.contype::text=checks.kind AND c.convalidated)
  ) IS TRUE)) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('public.operator_jobs')
    AND c.conname='operator_jobs_inventory_scan_fkey' AND c.confrelid=pg_catalog.to_regclass('public.channel_video_inventory_scans')
    AND c.condeferrable AND c.condeferred AND c.confdeltype='a' AND c.confupdtype='a')) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_index i WHERE i.indexrelid=to_regclass('public.channel_playlist_dispatch_idx') AND i.indisvalid AND i.indisready)
   OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_index i WHERE i.indexrelid=to_regclass('public.typed_inventory_receipt_token_hash_idx') AND i.indisvalid AND i.indisready)) IS TRUE,
(EXISTS (SELECT 1 FROM unnest(ARRAY['channel_playlist_state','channel_video_inventory_scans','typed_inventory_receipts','operator_jobs']::text[]) AS expected(relation_name) WHERE ( NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c WHERE c.oid=to_regclass('public.'||expected.relation_name) AND c.relrowsecurity
    AND c.relowner=(SELECT oid FROM pg_catalog.pg_roles WHERE rolname='postgres'))
    OR has_table_privilege('anon',to_regclass('public.'||expected.relation_name),'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
    OR has_table_privilege('authenticated',to_regclass('public.'||expected.relation_name),'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
  ) IS TRUE)) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('public.channel_playlist_state') AND c.conname='channel_playlist_dispatch_ordinal_check' AND c.convalidated AND md5(pg_get_constraintdef(c.oid))='6b727ba118f653ba42af9e4c6f77177a')) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('public.channel_playlist_state') AND c.conname='channel_playlist_inventory_active_scan_check' AND c.convalidated AND md5(pg_get_constraintdef(c.oid))='6d57efe928d80e3eeb1baa8e63c86660')) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('public.operator_jobs') AND c.conname='operator_jobs_inventory_ack_check' AND c.convalidated AND md5(pg_get_constraintdef(c.oid))='103c34979dda5b770f6e18c68b35943c')) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('public.operator_jobs') AND c.conname='operator_jobs_inventory_checkpoint_check' AND c.convalidated AND md5(pg_get_constraintdef(c.oid))='f2ea0a95a8e10462831bea1b5e9aa2e5')) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('public.operator_jobs') AND c.conname='operator_jobs_inventory_scan_fkey' AND c.convalidated AND md5(pg_get_constraintdef(c.oid))='949cf0bd7b23b580da53b41533a5c7a4')) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conrelid=pg_catalog.to_regclass('public.typed_inventory_receipts') AND c.conname='typed_inventory_receipt_token_hash_check' AND c.convalidated AND md5(pg_get_constraintdef(c.oid))='74d52b1736e2557a1eac58dcb646731e')) IS TRUE,
(md5(pg_get_indexdef(pg_catalog.to_regclass('public.channel_playlist_dispatch_idx'))) IS DISTINCT FROM '8613c7218c442144edffec8349ba2797') IS TRUE,
(md5(pg_get_indexdef(pg_catalog.to_regclass('public.typed_inventory_receipt_token_hash_idx'))) IS DISTINCT FROM 'f77b30c9be62453f1024b96dd757d233') IS TRUE,
(to_regprocedure('public.begin_long_inventory_reconciliation(text,uuid)') IS NOT NULL) IS TRUE,
(to_regprocedure('public.settle_long_inventory_reconciliation_page(text,uuid,uuid,uuid,text,text,text,jsonb,timestamp with time zone,boolean,text)') IS NOT NULL) IS TRUE,
(to_regprocedure('public.claim_due_channel_discovery(integer,text,text,integer)') IS NOT NULL) IS TRUE,
(to_regprocedure('public.claim_channel_discovery_ids(text[],text,text,integer)') IS NOT NULL) IS TRUE,
(to_regprocedure('public.complete_channel_discovery_claim(text,uuid,uuid,jsonb)') IS NOT NULL) IS TRUE,
(to_regprocedure('public.fail_channel_discovery_claim(text,uuid,uuid,text,timestamp with time zone)') IS NOT NULL) IS TRUE,
(to_regprocedure('public.update_channel_discovery_state(text,jsonb,jsonb,bigint)') IS NOT NULL) IS TRUE,
(to_regprocedure('public.insert_long_video_inventory_if_absent(text,jsonb,timestamp with time zone,bigint)') IS NOT NULL) IS TRUE,
(EXISTS(SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid=pg_catalog.to_regclass('public.channels')
   AND attname='deep_inventory_page_token' AND NOT attisdropped)) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid=pg_catalog.to_regclass('public.channels_with_control')
   AND attname='deep_inventory_page_token' AND NOT attisdropped AND atttypid='text'::regtype)
   OR position('NULL::text AS deep_inventory_page_token' in pg_get_viewdef(pg_catalog.to_regclass('public.channels_with_control'),true))=0) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object('status','ok','schema_version',2,'shared_page_owner',true,'lease_owner','channel_playlist_state',
   'durable_fairness','dispatch_ordinal','token_history','retained_receipts','page_limit',50,'continuation_proof_limit',500,'initial_chunk_pages',1) AS value
"""

_ASSERT_OPERATOR_JOBS_READY_SQL = r"""
WITH schema_columns AS MATERIALIZED (
    SELECT ns.nspname AS table_schema, relation.relname AS table_name,
           attribute.attname AS column_name,
           pg_catalog.format_type(CASE WHEN type_row.typtype='d' THEN type_row.typbasetype
               ELSE attribute.atttypid END, NULL) AS data_type,
           base_type.typname AS udt_name,
           CASE WHEN attribute.attnotnull OR (type_row.typtype='d' AND type_row.typnotnull)
               THEN 'NO' ELSE 'YES' END AS is_nullable
    FROM pg_catalog.pg_attribute attribute
    JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
    JOIN pg_catalog.pg_namespace ns ON ns.oid=relation.relnamespace
    JOIN pg_catalog.pg_type type_row ON type_row.oid=attribute.atttypid
    JOIN pg_catalog.pg_type base_type ON base_type.oid=CASE WHEN type_row.typtype='d'
        THEN type_row.typbasetype ELSE type_row.oid END
    WHERE ns.nspname IN ('public','operator_private') AND attribute.attnum>0
      AND NOT attribute.attisdropped AND relation.relkind IN ('r','v','f','p')
)
SELECT ARRAY[
(to_regclass('public.operator_jobs') is null
        or to_regclass('public.operator_job_targets') is null
        or (SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='operator_job_retry_requests') is null) IS TRUE,
(not exists (
        select 1 from schema_columns
        where table_schema = 'public' and table_name = 'operator_jobs'
            and column_name = 'resume_page_token'
    )) IS TRUE,
((select count(*) from schema_columns where table_schema='public'
        and ((table_name='operator_jobs' and column_name in ('initial_video_types','inventory_video_type'))
          or (table_name='operator_job_targets' and column_name='video_type')
          or (table_name='channels' and column_name='initial_inventory_estimate')))<>4) IS TRUE,
((select count(*) from schema_columns
        where table_schema='public' and table_name='operator_jobs'
          and column_name in ('accepted_epoch','lease_channel_epoch')
          and data_type='bigint') <> 2
      or not exists(select 1 from schema_columns
        where table_schema='operator_private' and table_name='operator_job_retry_requests'
          and column_name='request_control_epoch' and data_type='bigint')) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'ready', true,
        'schema_version', 9,
        'full_initial_scope_version',1,'initial_total_limits_enforced',false,
        'initial_inventory_scan_bridge',true,'initial_chunk_pages',1,
        'initial_quota_limit_enforced', false,
        'full_profile_request_units_per_batch', 2,
        'unknown_completion_preserves_pending', true,
        'job_table', 'operator_jobs',
        'target_table', 'operator_job_targets',
        'retry_request_table', 'operator_private.operator_job_retry_requests',
        'initial_inventory_job_type', true,
        'initial_full_hydration', true,
        'auto_hydration_max_items',10000,
        'legacy_auto_hydration_max_items',2500,
        'initial_video_types',jsonb_build_array('long','short','stream'),
        'channel_epoch_required',true,'frozen_initial_scope',true,'mixed_initial_claim',true,'unfinished_stream_checked',true,
        'verify_lease_seconds', 60,
        'idempotent_verify', true,
        'atomic_workset_materialization', true,
        'repairable_target_count_mismatch', true,
        'retry_idempotency_key', true,
        'mismatch_repair_source', 'verified_frozen_operator_job_targets',
        'quota_neutral_migration', true
    ) AS value
"""

_ASSERT_TYPED_INVENTORY_CANONICAL_REPAIR_SCHEMA_SQL = r"""
WITH observed_1 AS MATERIALIZED (SELECT relation_row.oid AS v_lock_relation, relation_row.reloptions AS v_lock_relation_options from pg_catalog.pg_class as relation_row
    where relation_row.oid = (SELECT relation.oid FROM pg_catalog.pg_class relation JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace WHERE namespace.nspname='operator_private' AND relation.relname='video_collection_state_canonical_repair_lock')
      and relation_row.relkind = 'v'
      and pg_catalog.pg_get_userbyid(relation_row.relowner) = 'postgres')
SELECT ARRAY[
(pg_catalog.current_setting('server_version_num')::integer < 170000) IS TRUE,
((SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private') is null
       or pg_catalog.pg_get_userbyid(
           (
               select namespace_row.nspowner
               from pg_catalog.pg_namespace as namespace_row
               where namespace_row.oid = (SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private')
           )
       ) <> 'postgres'
       or not pg_catalog.has_schema_privilege(
           'service_role', (SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private'), 'usage'
       )
       or pg_catalog.has_schema_privilege(
           'service_role', (SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private'), 'create'
       )
       or pg_catalog.has_schema_privilege(
           'anon', (SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private'), 'usage'
       )
       or pg_catalog.has_schema_privilege(
           'anon', (SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private'), 'create'
       )
       or pg_catalog.has_schema_privilege(
           'authenticated', (SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private'), 'usage'
       )
       or pg_catalog.has_schema_privilege(
           'authenticated', (SELECT namespace.oid FROM pg_catalog.pg_namespace namespace WHERE namespace.nspname='operator_private'), 'create'
       )) IS TRUE,
((SELECT v_lock_relation FROM observed_1) is null
       or pg_catalog.pg_relation_is_updatable((SELECT v_lock_relation FROM observed_1), false) <> 0
       or pg_catalog.pg_relation_is_updatable((SELECT v_lock_relation FROM observed_1), true) <> 0
       or not coalesce(
           'security_invoker=false' = any((SELECT v_lock_relation_options FROM observed_1)::text[]),
           false
       )) IS TRUE,
(not exists (
        select 1
        from pg_catalog.pg_rewrite as rewrite_row
        join pg_catalog.pg_depend as dependency
          on dependency.classid = pg_catalog.to_regclass('pg_catalog.pg_rewrite')
         and dependency.objid = rewrite_row.oid
         and dependency.refclassid = pg_catalog.to_regclass('pg_catalog.pg_class')
        where rewrite_row.ev_class = (SELECT v_lock_relation FROM observed_1)
          and dependency.refobjid = pg_catalog.to_regclass('public.video_collection_state')
    )
       or exists (
           select 1
           from pg_catalog.pg_rewrite as rewrite_row
           join pg_catalog.pg_depend as dependency
             on dependency.classid = pg_catalog.to_regclass('pg_catalog.pg_rewrite')
            and dependency.objid = rewrite_row.oid
            and dependency.refclassid = pg_catalog.to_regclass('pg_catalog.pg_class')
           where rewrite_row.ev_class = (SELECT v_lock_relation FROM observed_1)
             and dependency.refobjid <> (SELECT v_lock_relation FROM observed_1)
             and dependency.refobjid <>
                   pg_catalog.to_regclass('public.video_collection_state')
       )) IS TRUE,
(not pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'maintain'
       )
       or pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'select'
       )
       or pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'insert'
       )
       or pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'update'
       )
       or pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'delete'
       )
       or pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'truncate'
       )
       or pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'references'
       )
       or pg_catalog.has_table_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'trigger'
       )
       or pg_catalog.has_any_column_privilege(
           'service_role', (SELECT v_lock_relation FROM observed_1), 'select,insert,update,references'
       )
       or pg_catalog.has_table_privilege(
           'anon', (SELECT v_lock_relation FROM observed_1), 'maintain'
       )
       or pg_catalog.has_table_privilege(
           'anon', (SELECT v_lock_relation FROM observed_1),
           'select,insert,update,delete,truncate,references,trigger'
       )
       or pg_catalog.has_any_column_privilege(
           'anon', (SELECT v_lock_relation FROM observed_1), 'select,insert,update,references'
       )
       or pg_catalog.has_table_privilege(
           'authenticated', (SELECT v_lock_relation FROM observed_1), 'maintain'
       )
       or pg_catalog.has_table_privilege(
           'authenticated', (SELECT v_lock_relation FROM observed_1),
           'select,insert,update,delete,truncate,references,trigger'
       )
       or pg_catalog.has_any_column_privilege(
           'authenticated', (SELECT v_lock_relation FROM observed_1),
           'select,insert,update,references'
       )) IS TRUE,
(not pg_catalog.has_table_privilege('postgres',to_regclass('public.video_collection_state'),'maintain') OR NOT coalesce((SELECT roles_valid AND relations_valid AND privileges_valid FROM (
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
) direct_queue_rights),false)) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'status', 'ok',
        'schema_version', 2,
        'authoritative_evidence', 'unique_present_typed_playlist_membership',
        'canonical_channel_mutation', false,
        'max_atomic_repairs', 5000,
        'collection_state_lock_contract',
            'private_owner_aggregate_view_maintain_only',
        'minimum_postgres_version', 17,
        'repair_statement_timeout_seconds', 60
    ) AS value
"""

_ASSERT_TYPED_INVENTORY_FOUNDATION_SCHEMA_SQL = r"""
WITH observed_1 AS MATERIALIZED (SELECT pg_catalog.pg_get_constraintdef(constraint_row.oid) AS v_primary_key from pg_catalog.pg_constraint as constraint_row
    join pg_catalog.pg_class as relation
      on relation.oid = constraint_row.conrelid
    join pg_catalog.pg_namespace as namespace
      on namespace.oid = relation.relnamespace
    where namespace.nspname = 'public'
      and relation.relname = 'video_inventory'
      and constraint_row.contype = 'p'),
observed_2 AS MATERIALIZED (SELECT count(*) AS v_enabled_count from public.channel_playlist_state as discovery
    where discovery.runtime_enabled and discovery.video_type in ('short','stream')),
observed_3 AS MATERIALIZED (SELECT count(*) AS v_product_exposure_count from public.channel_video_inventory_state as inventory_state
    where inventory_state.product_exposure_ready)
SELECT ARRAY[
(EXISTS (SELECT 1 FROM unnest(array[
        'channel_video_inventory_scans',
        'video_inventory_memberships',
        'channel_playlist_state'
    ]::text[]) AS expected(v_table_name) WHERE ( not exists (
            select 1
            from pg_catalog.pg_class as relation
            join pg_catalog.pg_namespace as namespace
              on namespace.oid = relation.relnamespace
            where namespace.nspname = 'public'
              and relation.relname = expected.v_table_name
              and relation.relkind = 'r'
              and relation.relrowsecurity
        ) ) IS TRUE)) IS TRUE,
(EXISTS (SELECT 1 FROM unnest(array[
        'channel_video_inventory_scans',
        'video_inventory_memberships',
        'channel_playlist_state'
    ]::text[]) AS expected(v_table_name) WHERE ( pg_catalog.has_table_privilege(
            'anon',
            to_regclass('public.' || expected.v_table_name),
            'select'
        ) or pg_catalog.has_table_privilege(
            'authenticated',
            to_regclass('public.' || expected.v_table_name),
            'select'
        ) ) IS TRUE)) IS TRUE,
(not exists (
        select 1
        from pg_catalog.pg_attribute as attribute
        join pg_catalog.pg_class as relation
          on relation.oid = attribute.attrelid
        join pg_catalog.pg_namespace as namespace
          on namespace.oid = relation.relnamespace
        join pg_catalog.pg_attrdef as attribute_default
          on attribute_default.adrelid = attribute.attrelid
         and attribute_default.adnum = attribute.attnum
        where namespace.nspname = 'public'
          and relation.relname = 'channel_playlist_state'
          and attribute.attname = 'product_exposure_ready'
          and attribute.atttypid = 'pg_catalog.bool'::regtype
          and attribute.attnotnull
          and pg_catalog.pg_get_expr(
              attribute_default.adbin,
              attribute_default.adrelid
          ) = 'false'
    )) IS TRUE,
((SELECT v_primary_key FROM observed_1) is distinct from 'PRIMARY KEY (video_id)') IS TRUE,
(not exists (
        select 1
        from pg_catalog.pg_class as relation
        join pg_catalog.pg_namespace as namespace
          on namespace.oid = relation.relnamespace
        where namespace.nspname = 'public'
          and relation.relname = 'video_inventory_type_resolution'
          and relation.relkind = 'v'
          and coalesce(relation.reloptions, array[]::text[])
              @> array['security_invoker=true']::text[]
    )) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'status', 'ok',
        'canonical_inventory_key', 'video_id',
        'product_exposure_default', false,
        'membership_conflicts', 'explicit',
        'absence_requires', 'completed_complete_scan_generation',
        'active_long_discovery_owner', 'channel_playlist_state',
        'typed_nonlong_runtime_enabled_rows', (SELECT v_enabled_count FROM observed_2),
        'product_exposure_ready_rows', (SELECT v_product_exposure_count FROM observed_3)
    ) AS value
"""

_ASSERT_TYPED_INVENTORY_QUOTA_DEFER_SCHEMA_SQL = r"""
SELECT ARRAY[
((
        select count(*)
        from public.video_collection_cadence as policy
        where policy.collection_kind = 'metadata'
          and policy.video_type in ('short', 'stream')
          and policy.enabled
          and policy.policy_version = 1
          and policy.jitter_window_seconds = 21600
          and (
              (
                  policy.video_type in ('short', 'stream')
                  and policy.age_bucket = 'age_0_90d'
                  and policy.min_age_seconds = 0
                  and policy.max_age_seconds = 7776000
                  and policy.cadence_seconds = 604800
              )
              or (
                  policy.video_type in ('short', 'stream')
                  and policy.age_bucket = 'age_91d_plus'
                  and policy.min_age_seconds = 7776000
                  and policy.max_age_seconds is null
                  and policy.cadence_seconds = 2419200
              )
          )
    ) <> 4) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'status', 'ok',
        'schema_version', 1,
        'claim_limit', 500,
        'retry_floor_seconds', 3600,
        'retry_until', 'youtube_quota_reset',
        'lost_ack_receipts', true,
        'active_scan_state_preserved', true,
        'cadence_jitter_seconds', 21600,
        'quota_claim_floor_guard', true
    ) AS value
"""

_ASSERT_TYPED_INVENTORY_RUNTIME_SCHEMA_SQL = r"""
WITH observed_1 AS MATERIALIZED (SELECT count(*) AS enabled_count FROM public.channel_playlist_state WHERE runtime_enabled),
observed_2 AS MATERIALIZED (SELECT count(*) AS exposed_count FROM public.channel_playlist_state WHERE product_exposure_ready)
SELECT ARRAY[
(EXISTS(SELECT 1 FROM public.channel_playlist_state d LEFT JOIN public.channel_video_inventory_scans s ON s.scan_id=d.active_scan_id
   WHERE d.active_scan_id IS NOT NULL AND (s.scan_id IS NULL OR s.status<>'running' OR s.channel_id IS DISTINCT FROM d.channel_id
      OR s.video_type IS DISTINCT FROM d.video_type OR s.playlist_id IS DISTINCT FROM d.playlist_id
      OR s.resume_page_token IS DISTINCT FROM d.resume_page_token OR s.started_at IS DISTINCT FROM d.resume_started_at
      OR s.scan_boundary_at IS DISTINCT FROM d.scan_boundary_at OR NOT s.token_history_verified))) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object('status','ok','schema_version',2,'foundation',NULL::jsonb,'runtime_enabled_rows',(SELECT enabled_count FROM observed_1),
  'product_exposure_ready_rows',(SELECT exposed_count FROM observed_2),'zero_enabled_rows_allowed',true,'runtime_video_types',jsonb_build_array('long','short','stream'),
  'canonical_identity','insert_only','page_limit',50,'continuation_delay_minutes',30,'head_cadence_hours',jsonb_build_array(1,2,4,8,24),
  'full_reconciliation_days',21,'product_exposure_independent_of_active_scan',true,'statistics_collection',false,
  'separate_current_data_collection',true,'lost_ack_receipts',true,'durable_fairness',true,'initial_chunk_pages',1) AS value
"""

_ASSERT_TYPED_VIDEO_COLLECTION_SCHEMA_SQL = r"""
WITH schema_columns AS MATERIALIZED (
    SELECT ns.nspname AS table_schema, relation.relname AS table_name,
           attribute.attname AS column_name,
           pg_catalog.format_type(CASE WHEN type_row.typtype='d' THEN type_row.typbasetype
               ELSE attribute.atttypid END, NULL) AS data_type,
           base_type.typname AS udt_name,
           CASE WHEN attribute.attnotnull OR (type_row.typtype='d' AND type_row.typnotnull)
               THEN 'NO' ELSE 'YES' END AS is_nullable
    FROM pg_catalog.pg_attribute attribute
    JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
    JOIN pg_catalog.pg_namespace ns ON ns.oid=relation.relnamespace
    JOIN pg_catalog.pg_type type_row ON type_row.oid=attribute.atttypid
    JOIN pg_catalog.pg_type base_type ON base_type.oid=CASE WHEN type_row.typtype='d'
        THEN type_row.typbasetype ELSE type_row.oid END
    WHERE ns.nspname IN ('public','operator_private') AND attribute.attnum>0
      AND NOT attribute.attisdropped AND relation.relkind IN ('r','v','f','p')
),
observed_1 AS MATERIALIZED (SELECT count(*) AS v_long_typed_drift from public.video_collection_state as state
    where state.video_type = 'long'
      and (
          state.typed_source_published_at is not null
          or state.typed_stream_actual_end_at is not null
          or state.typed_collection_status is not null
          or state.typed_collection_checked_at is not null
      )),
observed_2 AS MATERIALIZED (SELECT count(*) AS v_typed_long_clock_drift from public.video_collection_state as state
    where state.video_type in ('short', 'stream')
      and (
          state.stats_due_at is not null
          or state.metadata_due_at is not null
      )),
observed_3 AS MATERIALIZED (SELECT count(*) AS v_identity_drift from public.video_collection_state as state
    join public.video_inventory as inventory
      on inventory.video_id = state.video_id




    where state.video_type is distinct from coalesce(inventory.video_type, 'long')
       or state.channel_id is distinct from inventory.channel_id),
observed_4 AS MATERIALIZED (SELECT count(*) AS v_disabled_active_drift from public.video_collection_state as state
    join public.typed_video_collection_runtime as runtime
      on runtime.video_type = state.video_type
    where state.video_type in ('short', 'stream')
      and not runtime.runtime_enabled
      and (
          state.next_due_at is not null
          or state.lease_token is not null
      )),
observed_5 AS MATERIALIZED (SELECT count(*) AS v_product_exposure_rows from public.channel_video_inventory_state as inventory_state
    where inventory_state.video_type in ('short', 'stream')
      and inventory_state.product_exposure_ready)
SELECT ARRAY[
(to_regclass('public.video_collection_cadence') is null
       or to_regclass('public.typed_video_collection_runtime') is null
       or to_regclass('public.typed_video_collection_receipts') is null
       or to_regclass('public.video_collection_state_format_due_idx') is null) IS TRUE,
(EXISTS (SELECT 1 FROM unnest(array[
        'video_collection_cadence',
        'typed_video_collection_runtime',
        'typed_video_collection_receipts'
    ]::text[]) AS expected(v_relation) WHERE ( not exists (
               select 1
               from pg_catalog.pg_class as relation
               join pg_catalog.pg_namespace as namespace
                 on namespace.oid = relation.relnamespace
               where namespace.nspname = 'public'
                 and relation.relname = expected.v_relation
                 and pg_catalog.pg_get_userbyid(relation.relowner) = 'postgres'
                 and relation.relkind in ('r', 'p')
                 and relation.relrowsecurity
           )
           or exists (
               select 1
               from pg_catalog.pg_class as relation
               join pg_catalog.pg_namespace as namespace
                 on namespace.oid = relation.relnamespace
               cross join lateral pg_catalog.aclexplode(coalesce(
                   relation.relacl,
                   pg_catalog.acldefault('r', relation.relowner)
               )) as acl
               where namespace.nspname = 'public'
                 and relation.relname = expected.v_relation
                 and acl.grantee = 0
           )
           or pg_catalog.has_table_privilege(
               'anon', to_regclass('public.' || expected.v_relation), 'select'
           )
           or pg_catalog.has_table_privilege(
               'authenticated', to_regclass('public.' || expected.v_relation), 'select'
           )
           or not pg_catalog.has_table_privilege(
               'service_role', to_regclass('public.' || expected.v_relation), 'select'
           )
           or pg_catalog.has_table_privilege(
               'service_role', to_regclass('public.' || expected.v_relation), 'insert'
           )
           or pg_catalog.has_table_privilege(
               'service_role', to_regclass('public.' || expected.v_relation), 'update'
           )
           or (pg_catalog.has_table_privilege('service_role',to_regclass('public.'||expected.v_relation),'delete') IS DISTINCT FROM (expected.v_relation='typed_video_collection_receipts'))
           or (expected.v_relation='typed_video_collection_runtime' AND NOT coalesce((SELECT roles_valid AND relations_valid AND privileges_valid FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_video_collection_runtime',ARRAY['SELECT']::text[]),
 ('video_collection_control',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_video_collection_runtime','reconcile_queue_cursor','UPDATE'),
 ('typed_video_collection_runtime','reconcile_inventory_cursor','UPDATE'),
 ('typed_video_collection_runtime','updated_at','UPDATE'),
 ('video_collection_control','reconciliation_required','UPDATE'),
 ('video_collection_control','last_reconciled_at','UPDATE'),
 ('video_collection_control','updated_at','UPDATE'),
 ('video_collection_control','invalidated_at','UPDATE')
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
) direct_completion_rights),false))
           or (expected.v_relation='typed_video_collection_receipts' AND NOT coalesce((SELECT roles_valid AND relations_valid AND privileges_valid FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_video_collection_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
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
) direct_typed_receipt_rights),false))
           or pg_catalog.has_table_privilege(
               'service_role', to_regclass('public.' || expected.v_relation), 'truncate'
           )
           or pg_catalog.has_table_privilege(
               'service_role', to_regclass('public.' || expected.v_relation), 'references'
           )
           or pg_catalog.has_table_privilege(
               'service_role', to_regclass('public.' || expected.v_relation), 'trigger'
           )
           or pg_catalog.has_table_privilege(
               'service_role', to_regclass('public.' || expected.v_relation), 'maintain'
           )
        ) IS TRUE)) IS TRUE,
((
        select count(*)
        from schema_columns as column_row
        where column_row.table_schema = 'public'
          and column_row.table_name = 'video_collection_state'
          and column_row.column_name in (
              'video_type',
              'typed_source_published_at',
              'typed_stream_actual_end_at',
              'typed_collection_status',
              'typed_collection_checked_at'
          )
    ) <> 5) IS TRUE,
((
        select count(*)
        from schema_columns as column_row
        where column_row.table_schema = 'public'
          and column_row.table_name = 'typed_video_collection_receipts'
          and column_row.is_nullable = 'NO'
          and column_row.column_name in (
              'video_id',
              'collection_attempt_id',
              'channel_id',
              'lease_token',
              'receipt_kind',
              'recorded_at',
              'payload_fingerprint',
              'created_at'
          )
    ) <> 8
       or not exists (
            select 1 from schema_columns column_row
            where column_row.table_schema='public'
              and column_row.table_name='typed_video_collection_receipts'
              and column_row.column_name='video_type' and column_row.is_nullable='YES'
       )
       or not exists (
            select 1 from pg_catalog.pg_constraint c
            where c.conrelid=to_regclass('public.typed_video_collection_receipts')
              and c.conname='typed_video_collection_receipts_type_check'
              and c.contype='c' and c.convalidated
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%identity_refresh%%'
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%stream_probe%%'
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%long%%'
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%short%%'
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%stream%%'
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%video_type IS NULL%%'
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%video_type IS NOT NULL%%'
              and pg_catalog.pg_get_constraintdef(c.oid) like '%%IS TRUE%%'
       )
       or not exists (
            select 1
            from pg_catalog.pg_constraint as constraint_row
            where constraint_row.conrelid =
                pg_catalog.to_regclass('public.typed_video_collection_receipts')
              and constraint_row.conname =
                  'typed_video_collection_receipts_fingerprint_check'
              and pg_catalog.pg_get_constraintdef(constraint_row.oid)
                  like '%%[0-9a-f]{32}%%'
       )) IS TRUE,
((
        select count(*)
        from public.video_collection_cadence as policy
        where policy.collection_kind = 'metadata'
          and policy.video_type in ('short', 'stream')
          and policy.enabled
          and policy.policy_version = 1
          and (
              (
                  policy.age_bucket = 'age_0_90d'
                  and policy.min_age_seconds = 0
                  and policy.max_age_seconds = 7776000
                  and policy.cadence_seconds = 604800
              )
              or (
                  policy.age_bucket = 'age_91d_plus'
                  and policy.min_age_seconds = 7776000
                  and policy.max_age_seconds is null
                  and policy.cadence_seconds = 2419200
              )
          )
    ) <> 4) IS TRUE,
(not exists (
        select 1 from pg_catalog.pg_index
        where indexrelid=to_regclass('public.video_collection_state_typed_reconcile_idx')
          and indrelid=pg_catalog.to_regclass('public.video_collection_state') and indisvalid
    ) or not exists (
        select 1 from pg_catalog.pg_index
        where indexrelid=to_regclass('public.video_inventory_typed_reconcile_idx')
          and indrelid=pg_catalog.to_regclass('public.video_inventory') and indisvalid
    ) or (
        select count(*) from pg_catalog.pg_attribute
        where attrelid=pg_catalog.to_regclass('public.typed_video_collection_runtime')
          and attname in ('reconcile_queue_cursor','reconcile_inventory_cursor')
          and not attisdropped and atttypid='text'::regtype
    ) <> 2) IS TRUE,
((SELECT v_long_typed_drift FROM observed_1) > 0
       or (SELECT v_typed_long_clock_drift FROM observed_2) > 0
       or (SELECT v_identity_drift FROM observed_3) > 0
       or (SELECT v_disabled_active_drift FROM observed_4) > 0) IS TRUE,
((SELECT v_product_exposure_rows FROM observed_5) > 0) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid=pg_catalog.to_regclass('public.video_collection_state') AND attname='lease_channel_epoch'
          AND attnum>0 AND NOT attisdropped AND atttypid='bigint'::regtype)
       OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c
        WHERE c.conrelid=pg_catalog.to_regclass('public.video_collection_state') AND c.contype='c' AND c.convalidated
          AND pg_catalog.pg_get_constraintdef(c.oid) LIKE '%%lease_channel_epoch > 0%%')) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'channel_control_fenced',true,'lease_channel_epoch',true,
        'status', 'ok',
        'schema_version', 1,
        'repair_version', 2,
        'bounded_claim_batch', true,
        'operational_eligibility_checked', false,
        'runtime_video_types', jsonb_build_array('short', 'stream'),
        'max_request_batch', 50,
        'fresh_through_days_inclusive', 90,
        'fresh_cadence_days', 7,
        'old_cadence_days', 28,
        'full_metadata_each_refresh', true,
        'completed_streams_only', true,
        'exact_receipt_payload_fencing', true,
        'stream_lifecycle_model', false,
        'product_exposure_changed', false,
        'product_exposure_ready_rows', (SELECT v_product_exposure_rows FROM observed_5),
        'runtime_enabled_rows', (
            select count(*)
            from public.typed_video_collection_runtime as runtime
            where runtime.runtime_enabled
        ),
        'long_typed_drift', (SELECT v_long_typed_drift FROM observed_1),
        'typed_long_clock_drift', (SELECT v_typed_long_clock_drift FROM observed_2),
        'identity_drift', (SELECT v_identity_drift FROM observed_3),
        'disabled_active_drift', (SELECT v_disabled_active_drift FROM observed_4),
        'ineligible_active_drift', ( null),
        'health', NULL::jsonb
    ) AS value
"""

_ASSERT_VIDEO_COLLECTION_CADENCE_SCHEMA_SQL = r"""
SELECT ARRAY[

]::boolean[] AS violations,
 jsonb_build_object('status','ok','selector','shared','long_bounds','closed_open_rounded_seconds',
  'typed_bounds','open_closed_unrounded_days','typed_jitter_seconds',21600) AS value
"""

_ASSERT_VIDEO_COLLECTION_COMMON_POOL_SCHEMA_SQL = r"""
SELECT ARRAY[
(not exists(select 1 from pg_index where indexrelid=
        to_regclass('public.video_collection_state_common_due_idx') and indisvalid and indisready
        and indrelid=pg_catalog.to_regclass('public.video_collection_state') and indnkeyatts=2)) IS TRUE,
(to_regclass('public.video_stats_unavailable') is not null
       or not exists(select 1 from pg_class where oid=to_regclass('public.open_video_availability')
           and relkind='v' and reloptions @> array['security_invoker=true'])
       or has_table_privilege('anon',to_regclass('public.open_video_availability'),'SELECT')
       or has_table_privilege('authenticated',to_regclass('public.open_video_availability'),'SELECT')
       or not has_table_privilege('service_role',to_regclass('public.open_video_availability'),'SELECT')
       or has_table_privilege('service_role',to_regclass('public.open_video_availability'),'INSERT,UPDATE,DELETE,TRUNCATE')) IS TRUE,
(exists(select 1 from pg_attribute where attrelid=pg_catalog.to_regclass('public.video_collection_state')
        and attnum>0 and not attisdropped and attname in('priority','typed_next_due_at'))
       or exists(select 1 from pg_attribute where attrelid=pg_catalog.to_regclass('public.video_collection_cadence')
        and attnum>0 and not attisdropped and attname='priority')) IS TRUE,
(exists(select 1 from pg_attribute where attrelid=pg_catalog.to_regclass('public.videos')
        and attnum>0 and not attisdropped and attname='category_collected_at')) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid=pg_catalog.to_regclass('public.video_collection_state') AND attname='lease_channel_epoch'
          AND attnum>0 AND NOT attisdropped AND atttypid='bigint'::regtype)
       OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c
        WHERE c.conrelid=pg_catalog.to_regclass('public.video_collection_state') AND c.contype='c' AND c.convalidated
          AND pg_catalog.pg_get_constraintdef(c.oid) LIKE '%%lease_channel_epoch > 0%%')) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'channel_control_fenced',true,'lease_channel_epoch',true,'unified_due_at',true,'priority_storage',false,'status','ok','schema_version',1,'common_pool_version',1,
        'mixed_claim',true,'sole_lease_owner','video_collection_state','max_request_batch',50,
        'max_claim_rows',50,'max_inspected_rows',1000,
        'ordering_policy','due_at_video_id','priority_ordering',false,
        'atomic_long_availability_restore',true,'availability_incident_owner',true,
        'profile_single_write',true,'metadata_replay_noop',true,'category_clock_retired',true) AS value
"""

_ASSERT_VIDEO_COLLECTION_QUEUE_SCHEMA_SQL = r"""
WITH schema_columns AS MATERIALIZED (
    SELECT ns.nspname AS table_schema, relation.relname AS table_name,
           attribute.attname AS column_name,
           pg_catalog.format_type(CASE WHEN type_row.typtype='d' THEN type_row.typbasetype
               ELSE attribute.atttypid END, NULL) AS data_type,
           base_type.typname AS udt_name,
           CASE WHEN attribute.attnotnull OR (type_row.typtype='d' AND type_row.typnotnull)
               THEN 'NO' ELSE 'YES' END AS is_nullable
    FROM pg_catalog.pg_attribute attribute
    JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
    JOIN pg_catalog.pg_namespace ns ON ns.oid=relation.relnamespace
    JOIN pg_catalog.pg_type type_row ON type_row.oid=attribute.atttypid
    JOIN pg_catalog.pg_type base_type ON base_type.oid=CASE WHEN type_row.typtype='d'
        THEN type_row.typbasetype ELSE type_row.oid END
    WHERE ns.nspname IN ('public','operator_private') AND attribute.attnum>0
      AND NOT attribute.attisdropped AND relation.relkind IN ('r','v','f','p')
),
observed_3 AS MATERIALIZED (SELECT control.reconciliation_required AS control_required, control.invalidated_at AS control_invalidated_at, control.last_reconciled_at AS control_reconciled_at from public.video_collection_control as control
    where control.singleton = true),
observed_4 AS MATERIALIZED (SELECT count(*) AS cadence_count from public.video_collection_cadence
    where video_type = 'long' and enabled = true and policy_version = 1)
SELECT ARRAY[
((
        select col.data_type
        from schema_columns as col
        where col.table_schema = 'public'
            and col.table_name = 'videos'
            and col.column_name = 'metadata_checked_at'
    ) is distinct from 'timestamp with time zone') IS TRUE,
((
        select col.data_type
        from schema_columns as col
        where col.table_schema = 'public'
            and col.table_name = 'videos'
            and col.column_name = 'stream_actual_end_at'
    ) is distinct from 'timestamp with time zone') IS TRUE,
(not exists (
        select 1
        from pg_catalog.pg_constraint as constraint_row
        where constraint_row.conrelid = pg_catalog.to_regclass('public.videos')
            and constraint_row.conname = 'videos_stream_actual_end_requires_metadata_clock'
            and constraint_row.contype = 'c'
            and constraint_row.convalidated
            and pg_catalog.pg_get_constraintdef(constraint_row.oid)
                ~* 'stream_actual_end_at IS NULL'
            and pg_catalog.pg_get_constraintdef(constraint_row.oid)
                ~* 'metadata_checked_at IS NOT NULL'
    )) IS TRUE,
((
        select count(*)
        from schema_columns as col
        where col.table_schema = 'public'
            and col.table_name = 'video_collection_state'
            and col.column_name in (
                'next_due_at',
                'stats_source_checked_at',
                'metadata_source_checked_at'
            )
            and col.data_type = 'timestamp with time zone'
    ) <> 3) IS TRUE,
((
        select col.udt_name
        from schema_columns as col
        where col.table_schema = 'public'
            and col.table_name = 'video_collection_state'
            and col.column_name = 'lease_token'
    ) is distinct from 'uuid') IS TRUE,
((
        select col.udt_name
        from schema_columns as col
        where col.table_schema = 'public'
            and col.table_name = 'video_collection_state'
            and col.column_name = 'last_claim_run_id'
    ) is distinct from 'uuid') IS TRUE,
((
        select col.udt_name
        from schema_columns as col
        where col.table_schema = 'public'
            and col.table_name = 'video_snapshots'
            and col.column_name = 'collection_attempt_id'
    ) is distinct from 'uuid') IS TRUE,
(to_regclass('public.video_collection_state_format_due_idx') is null
        or to_regclass('public.video_collection_state_channel_idx') is null
        or to_regclass('public.video_snapshots_collection_attempt_uidx') is null) IS TRUE,
(exists (
        select 1
        from pg_trigger as trigger
        where trigger.tgrelid = pg_catalog.to_regclass('public.channels')
            and trigger.tgname = 'channel_visibility_collection_state_trigger'
            and not trigger.tgisinternal
    )) IS TRUE,
((SELECT control_required FROM observed_3) is null) IS TRUE,
((SELECT cadence_count FROM observed_4) <> 12) IS TRUE,
(NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute
        WHERE attrelid=pg_catalog.to_regclass('public.video_collection_state') AND attname='lease_channel_epoch'
          AND attnum>0 AND NOT attisdropped AND atttypid='bigint'::regtype)
       OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint c
        WHERE c.conrelid=pg_catalog.to_regclass('public.video_collection_state') AND c.contype='c' AND c.convalidated
          AND pg_catalog.pg_get_constraintdef(c.oid) LIKE '%%lease_channel_epoch > 0%%')) IS TRUE,
(to_regprocedure('public.ensure_video_collection_state(text[])') IS NOT NULL
       OR to_regprocedure('public.prioritize_typed_video_collection_ids(text,text[])') IS NOT NULL
       OR to_regprocedure('public.claim_video_collection_ids(text[],text,uuid,integer)') IS NOT NULL
       OR to_regprocedure('public.claim_video_metadata_collection_ids(text[],text,uuid,integer)') IS NOT NULL
       OR to_regprocedure('public.enqueue_video_collection_ids(text[])') IS NOT NULL) IS TRUE
]::boolean[] AS violations,
 jsonb_build_object(
        'channel_control_fenced',true,'lease_channel_epoch',true,
        'status', case
            when (SELECT control_required FROM observed_3) then 'backfill_required'
            else 'ok'
        end,
        'table', 'video_collection_state',
        'readiness_mode', 'sealed_reconciliation',
        'reconciliation_required', (SELECT control_required FROM observed_3),
        'remaining_count', case
            when (SELECT control_required FROM observed_3) then null
            else 0
        end,
        'remaining_count_exact', not (SELECT control_required FROM observed_3),
        'invalidated_at', (SELECT control_invalidated_at FROM observed_3),
        'last_reconciled_at', (SELECT control_reconciled_at FROM observed_3),
        'cadence_rules', (SELECT cadence_count FROM observed_4),
        'max_claim_size', 1000,
        'policy_version', 1,
        'metadata_completion_contract_version', 2,
        'stream_completion_evidence', true
    ) AS value
"""

_HOT_RUNTIME_SQL = """SELECT pg_catalog.jsonb_build_object(
 'version',1,'as_of',pg_catalog.clock_timestamp(),
 'schema_ready',coalesce(roles_valid AND functions_valid AND triggers_valid AND inventory_chains_valid,false)
) AS value FROM (""" + intrinsic_capabilities.READINESS_SQL + ") final_schema CROSS JOIN (" + inventory_chain_capabilities.HOT_READINESS_SQL + ") inventory_chains"

_QUERIES = MappingProxyType({
    'assert_channel_discovery_paging_schema': _ASSERT_CHANNEL_DISCOVERY_PAGING_SCHEMA_SQL,
    'assert_channel_discovery_queue_schema': _ASSERT_CHANNEL_DISCOVERY_QUEUE_SCHEMA_SQL,
    'assert_channel_sync_control_ready': _ASSERT_CHANNEL_SYNC_CONTROL_READY_SQL,
    'assert_full_initial_schema': _ASSERT_FULL_INITIAL_SCHEMA_SQL,
    'assert_inventory_receipts_schema': _ASSERT_INVENTORY_RECEIPTS_SCHEMA_SQL,
    'assert_inventory_switch_schema': _ASSERT_INVENTORY_SWITCH_SCHEMA_SQL,
    'assert_operator_jobs_ready': _ASSERT_OPERATOR_JOBS_READY_SQL,
    'assert_typed_inventory_canonical_repair_schema': _ASSERT_TYPED_INVENTORY_CANONICAL_REPAIR_SCHEMA_SQL,
    'assert_typed_inventory_foundation_schema': _ASSERT_TYPED_INVENTORY_FOUNDATION_SCHEMA_SQL,
    'assert_typed_inventory_quota_defer_schema': _ASSERT_TYPED_INVENTORY_QUOTA_DEFER_SCHEMA_SQL,
    'assert_typed_inventory_runtime_schema': _ASSERT_TYPED_INVENTORY_RUNTIME_SCHEMA_SQL,
    'assert_typed_video_collection_schema': _ASSERT_TYPED_VIDEO_COLLECTION_SCHEMA_SQL,
    'assert_video_collection_cadence_schema': _ASSERT_VIDEO_COLLECTION_CADENCE_SCHEMA_SQL,
    'assert_video_collection_common_pool_schema': _ASSERT_VIDEO_COLLECTION_COMMON_POOL_SCHEMA_SQL,
    'assert_video_collection_queue_schema': _ASSERT_VIDEO_COLLECTION_QUEUE_SCHEMA_SQL,
})

MAINTENANCE_CAPABILITY_QUERIES = MappingProxyType({
    "schema_release": schema_release_capabilities.READINESS_SQL,
    "collection_cadence": LEGACY_COMPATIBILITY_SQL,
    "inventory_switch": _ASSERT_INVENTORY_SWITCH_SCHEMA_SQL,
    "hot_runtime": _HOT_RUNTIME_SQL,
    "user_work_restore": user_work_capabilities.READINESS_SQL,
    "video_failure": video_failure_capabilities.READINESS_SQL,
    "evidence_retention": evidence_retention_capabilities.READINESS_SQL,
    "inventory_receipts": inventory_receipt_capabilities.READINESS_SQL,
    "operational_retention": operational_retention_capabilities.READINESS_SQL,
    "deleted_channels": deleted_channel_retention_capabilities.READINESS_SQL,
    "collection_completion": collection_completion_capabilities.READINESS_SQL,
})

CHECKS = MappingProxyType({
    'assert_channel_discovery_paging_schema': assert_channel_discovery_paging_schema,
    'assert_channel_discovery_queue_schema': assert_channel_discovery_queue_schema,
    'assert_channel_sync_control_ready': assert_channel_sync_control_ready,
    'assert_full_initial_schema': assert_full_initial_schema,
    'assert_inventory_receipts_schema': assert_inventory_receipts_schema,
    'assert_inventory_switch_schema': assert_inventory_switch_schema,
    'assert_operator_jobs_ready': assert_operator_jobs_ready,
    'assert_typed_inventory_canonical_repair_schema': assert_typed_inventory_canonical_repair_schema,
    'assert_typed_inventory_foundation_schema': assert_typed_inventory_foundation_schema,
    'assert_typed_inventory_quota_defer_schema': assert_typed_inventory_quota_defer_schema,
    'assert_typed_inventory_runtime_schema': assert_typed_inventory_runtime_schema,
    'assert_typed_video_collection_schema': assert_typed_video_collection_schema,
    'assert_video_collection_cadence_schema': assert_video_collection_cadence_schema,
    'assert_video_collection_common_pool_schema': assert_video_collection_common_pool_schema,
    'assert_video_collection_queue_schema': assert_video_collection_queue_schema,
    'assert_channel_discovery_queue_ready': assert_channel_discovery_queue_ready,
    'assert_video_collection_queue_ready': assert_video_collection_queue_ready,
})

# These catalog reads must finish before parsing state queries on partial schemas.
_PAGING_PREREQUISITE_SQL = """
SELECT ARRAY[EXISTS (
 SELECT 1 FROM (VALUES ('resume_page_token','text',false),
 ('resume_started_at','timestamp with time zone',false),('no_new_streak','integer',true)) required(name,type_name,not_null)
 LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=to_regclass('public.channel_playlist_state')
  AND a.attname=required.name AND a.attnum>0 AND NOT a.attisdropped
 WHERE a.attnum IS NULL OR pg_catalog.format_type(a.atttypid,a.atttypmod)<>required.type_name
  OR (required.not_null AND NOT a.attnotnull)
)] AS violations
"""

_CONTROL_PREREQUISITE_SQL = """
SELECT ARRAY[
 NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass('public.channels')
  AND attname='initial_long_inventory_error_code' AND attnum>0 AND NOT attisdropped),
 (to_regclass('public.channel_collection_state') IS NULL
  OR to_regclass('public.channel_collection_control') IS NULL
  OR to_regclass('public.channels_with_control') IS NULL
  OR to_regclass('public.channel_collection_epoch_seq') IS NULL
)
] AS violations
"""

_FOUNDATION_PREREQUISITE_SQL = """
SELECT ARRAY[EXISTS(SELECT 1 FROM (VALUES ('channel_video_inventory_scans'),
 ('video_inventory_memberships'),('channel_playlist_state')) required(name)
 LEFT JOIN pg_catalog.pg_class c ON c.oid=to_regclass('public.'||required.name)
 WHERE c.oid IS NULL OR c.relkind<>'r' OR NOT c.relrowsecurity)] AS violations
"""

_TYPED_VIDEO_PREREQUISITE_SQL = """
SELECT ARRAY[
 (to_regclass('public.video_collection_cadence') IS NULL
 OR to_regclass('public.typed_video_collection_runtime') IS NULL
 OR to_regclass('public.typed_video_collection_receipts') IS NULL
 OR to_regclass('public.video_collection_state_format_due_idx') IS NULL),
 EXISTS(SELECT 1 FROM (VALUES('video_collection_cadence'),('typed_video_collection_runtime'),
 ('typed_video_collection_receipts')) required(name)
 WHERE to_regclass('public.'||required.name) IS NULL),
 (SELECT count(*) FROM pg_catalog.pg_attribute WHERE attrelid=to_regclass('public.video_collection_state')
 AND attnum>0 AND NOT attisdropped AND attname IN ('video_type','typed_source_published_at',
 'typed_stream_actual_end_at','typed_collection_status','typed_collection_checked_at'))<>5
] AS violations
"""

_PREREQUISITES = MappingProxyType({
 'assert_channel_discovery_paging_schema': (_PAGING_PREREQUISITE_SQL, (_ERRORS['assert_channel_discovery_paging_schema'][0],)),
 'assert_channel_sync_control_ready': (_CONTROL_PREREQUISITE_SQL, _ERRORS['assert_channel_sync_control_ready'][:2]),
 'assert_typed_inventory_foundation_schema': (_FOUNDATION_PREREQUISITE_SQL, (_ERRORS['assert_typed_inventory_foundation_schema'][0],)),
 'assert_typed_video_collection_schema': (_TYPED_VIDEO_PREREQUISITE_SQL, _ERRORS['assert_typed_video_collection_schema'][:3]),
})
