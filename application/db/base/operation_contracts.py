"""Python operation budgets and public error codes.

Runtime policy is independent of retained SQL functions. Initial values preserve
the reviewed schema24 migration baseline; this module is maintained with the
Python protocols, not generated from the SQL catalogue.
"""
from types import MappingProxyType

from .postgres import Operation

HOT_ABORT_ERRORS = frozenset(('hot_abort_duplicate_claim', 'hot_abort_input_bound',
    'hot_abort_invalid_input', 'hot_abort_invalid_operator_claim', 'hot_abort_invalid_playlist_claim',
    'hot_abort_invalid_token', 'hot_abort_invalid_video_claim', 'hot_storage_active_work',
    'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready',
    'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable',
    'operator_job_source_scope_expired'))

OPERATIONS = MappingProxyType({
    'metadata_candidate_page': Operation(
        name='metadata_candidate_page', read_only=True,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=15000,
        error_codes=frozenset(),
    ),
    'capture_metadata_candidates': Operation(
        name='capture_metadata_candidates', read_only=True,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=15000,
        error_codes=frozenset(),
    ),
    'drain_hot_channel_metadata': Operation(
        name='drain_hot_channel_metadata', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=5000,
        error_codes=frozenset(),
    ),
    'inventory_candidate_page': Operation(
        name='inventory_candidate_page', read_only=True,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=15000,
        error_codes=frozenset(),
    ),
    'drain_hot_collection_claims': Operation(
        name='drain_hot_collection_claims', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=5000,
        error_codes=HOT_ABORT_ERRORS,
    ),
    'abort_hot_reconnect_claim': Operation(
        name='abort_hot_reconnect_claim', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=5000,
        error_codes=frozenset(),
    ),
    'complete_video_identity_refresh': Operation(
        name='complete_video_identity_refresh', read_only=False,
        statement_timeout_ms=8000, lock_timeout_ms=250,
        error_codes=frozenset(('video_identity_refresh_invalid', 'video_identity_refresh_stale',
            'video_identity_refresh_receipt_mismatch', 'video_identity_refresh_clock_regression',
            'hot_source_clock_out_of_window', 'hot_storage_active_work', 'channel_collection_scope_invalid',
            'channel_collection_scope_lost', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'abort_hot_collection_claims': Operation(
        name='abort_hot_collection_claims', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=250,
        error_codes=HOT_ABORT_ERRORS,
    ),
    'assert_channel_discovery_paging_schema': Operation(
        name='assert_channel_discovery_paging_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present')),
    ),
    'assert_channel_discovery_queue_ready': Operation(
        name='assert_channel_discovery_queue_ready', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('all_format_initial_hydration_columns_missing', 'full_initial_columns_invalid', 'full_initial_constraint_missing', 'inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_receipts_constraints_invalid', 'inventory_receipts_storage_acl_invalid', 'inventory_receipts_storage_missing', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'operator_job_channel_scope_contract_missing', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present', 'typed_inventory_active_scan_projection_drift', 'typed_inventory_product_exposure_default_invalid', 'typed_inventory_quota_claim_floor_guard_missing', 'typed_inventory_quota_claim_floor_truth_table_invalid', 'typed_inventory_quota_defer_preservation_contract_invalid', 'typed_inventory_quota_defer_rpc_security_invalid', 'typed_video_collection_cadence_or_jitter_invalid', 'video_inventory_must_have_one_canonical_row', 'video_inventory_resolution_must_be_security_invoker')),
    ),
    'assert_channel_discovery_queue_schema': Operation(
        name='assert_channel_discovery_queue_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(()),
    ),
    'assert_channel_sync_control_ready': Operation(
        name='assert_channel_sync_control_ready', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_control_acl_invalid', 'channel_collection_control_contract_missing', 'channel_collection_control_reconciliation_incomplete', 'channel_collection_scope_writer_acl_invalid')),
    ),
    'assert_full_initial_schema': Operation(
        name='assert_full_initial_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('full_initial_columns_invalid', 'full_initial_constraint_missing')),
    ),
    'assert_inventory_receipts_schema': Operation(
        name='assert_inventory_receipts_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_receipts_constraints_invalid', 'inventory_receipts_storage_acl_invalid', 'inventory_receipts_storage_missing', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present')),
    ),
    'assert_inventory_switch_schema': Operation(
        name='assert_inventory_switch_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present')),
    ),
    'assert_operator_jobs_ready': Operation(
        name='assert_operator_jobs_ready', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('all_format_initial_hydration_columns_missing', 'full_initial_columns_invalid', 'full_initial_constraint_missing', 'inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'operator_job_channel_scope_contract_missing', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present')),
    ),
    'assert_typed_inventory_canonical_repair_schema': Operation(
        name='assert_typed_inventory_canonical_repair_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('typed_inventory_canonical_repair_base_state_acl_invalid', 'typed_inventory_canonical_repair_collection_trigger_missing', 'typed_inventory_canonical_repair_function_contract_invalid', 'typed_inventory_canonical_repair_lock_schema_invalid', 'typed_inventory_canonical_repair_lock_view_acl_invalid', 'typed_inventory_canonical_repair_lock_view_dependency_invalid', 'typed_inventory_canonical_repair_lock_view_invalid', 'typed_inventory_canonical_repair_postgres_version_invalid')),
    ),
    'assert_typed_inventory_foundation_schema': Operation(
        name='assert_typed_inventory_foundation_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present', 'typed_inventory_product_exposure_default_invalid', 'video_inventory_must_have_one_canonical_row', 'video_inventory_resolution_must_be_security_invoker')),
    ),
    'assert_typed_inventory_quota_defer_schema': Operation(
        name='assert_typed_inventory_quota_defer_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_receipts_constraints_invalid', 'inventory_receipts_storage_acl_invalid', 'inventory_receipts_storage_missing', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present', 'typed_inventory_quota_claim_floor_guard_missing', 'typed_inventory_quota_claim_floor_truth_table_invalid', 'typed_inventory_quota_defer_preservation_contract_invalid', 'typed_inventory_quota_defer_rpc_security_invalid', 'typed_video_collection_cadence_or_jitter_invalid')),
    ),
    'assert_typed_inventory_runtime_schema': Operation(
        name='assert_typed_inventory_runtime_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('all_format_initial_hydration_columns_missing', 'full_initial_columns_invalid', 'full_initial_constraint_missing', 'inventory_dispatch_default_invalid', 'inventory_dispatch_sequence_contract_invalid', 'inventory_job_scan_fk_invalid', 'inventory_receipts_constraints_invalid', 'inventory_receipts_storage_acl_invalid', 'inventory_receipts_storage_missing', 'inventory_switch_index_missing', 'inventory_switch_old_claim_owner_present', 'operator_job_channel_scope_contract_missing', 'retired_inventory_column_present', 'retired_inventory_null_projection_invalid', 'retired_inventory_rpc_present', 'typed_inventory_active_scan_projection_drift', 'typed_inventory_product_exposure_default_invalid', 'typed_inventory_quota_claim_floor_guard_missing', 'typed_inventory_quota_claim_floor_truth_table_invalid', 'typed_inventory_quota_defer_preservation_contract_invalid', 'typed_inventory_quota_defer_rpc_security_invalid', 'typed_video_collection_cadence_or_jitter_invalid', 'video_inventory_must_have_one_canonical_row', 'video_inventory_resolution_must_be_security_invoker')),
    ),
    'assert_typed_video_collection_schema': Operation(
        name='assert_typed_video_collection_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('typed_collection_bounded_repair_schema_missing', 'typed_video_candidates_canonical_control_missing', 'typed_video_collection_cadence_invalid', 'typed_video_collection_product_exposure_must_remain_closed', 'typed_video_collection_receipt_contract_invalid', 'typed_video_collection_relation_missing', 'typed_video_collection_state_columns_missing', 'typed_video_collection_state_drift', 'video_channel_lease_epoch_contract_invalid')),
    ),
    'assert_video_collection_cadence_schema': Operation(
        name='assert_video_collection_cadence_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(()),
    ),
    'assert_video_collection_common_pool_schema': Operation(
        name='assert_video_collection_common_pool_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('common_pool_index_contract_invalid', 'typed_availability_incident_owner_not_ready', 'typed_long_availability_restore_capability_mismatch', 'video_category_clock_not_retired', 'video_channel_lease_epoch_contract_invalid', 'video_profile_single_write_capability_mismatch', 'video_queue_cleanup_schema_not_ready')),
    ),
    'assert_video_collection_queue_ready': Operation(
        name='assert_video_collection_queue_ready', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('video_channel_lease_epoch_contract_invalid', 'video_unfenced_legacy_overload_present')),
    ),
    'assert_video_collection_queue_schema': Operation(
        name='assert_video_collection_queue_schema', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('video_channel_lease_epoch_contract_invalid', 'video_unfenced_legacy_overload_present')),
    ),
    'backfill_video_collection_state': Operation(
        name='backfill_video_collection_state', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=5000,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'begin_typed_inventory_scan': Operation(
        name='begin_typed_inventory_scan', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'inventory_token_history_invalid', 'typed_inventory_active_scan_conflict', 'typed_inventory_active_scan_not_owned', 'typed_inventory_channel_not_eligible', 'typed_inventory_discovery_requires_full_generation', 'typed_inventory_discovery_state_missing', 'typed_inventory_format_state_missing', 'typed_inventory_scan_identity_conflict', 'typed_inventory_scan_identity_invalid', 'typed_inventory_scan_lease_lost', 'typed_inventory_scan_lease_not_owned', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'bind_initial_inventory_scan': Operation(
        name='bind_initial_inventory_scan', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_fresh_generation_required', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'initial_inventory_bind_conflict', 'initial_inventory_bind_identity_invalid', 'initial_inventory_playlist_lease_lost', 'initial_inventory_scan_identity_invalid', 'initial_inventory_scan_kind_invalid', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_channel_initial_hydration_job': Operation(
        name='claim_channel_initial_hydration_job', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'operator_job_channel_not_visible', 'operator_job_channel_required', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_seconds_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_worker_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_due_typed_inventory': Operation(
        name='claim_due_typed_inventory', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_storage_active_work', 'inventory_continuations_invalid', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'typed_inventory_channel_ids_invalid', 'typed_inventory_claim_lease_invalid', 'typed_inventory_claim_limit_invalid', 'typed_inventory_claim_owner_invalid', 'typed_inventory_video_types_invalid')),
    ),
    'claim_due_typed_video_collection': Operation(
        name='claim_due_typed_video_collection', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_storage_active_work', 'typed_video_collection_claim_input_invalid')),
    ),
    'claim_due_typed_video_collection_batch': Operation(
        name='claim_due_typed_video_collection_batch', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_storage_active_work', 'typed_video_collection_claim_input_invalid')),
    ),
    'claim_due_video_collection_batch': Operation(
        name='claim_due_video_collection_batch', read_only=False,
        statement_timeout_ms=8000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'common_video_collection_claim_input_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_initial_inventory_source': Operation(
        name='claim_initial_inventory_source', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_foreign_scan_invalid', 'full_initial_owned_scan_unavailable', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'initial_inventory_discovery_state_missing', 'inventory_continuations_invalid', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'typed_inventory_channel_ids_invalid', 'typed_inventory_claim_lease_invalid', 'typed_inventory_claim_limit_invalid', 'typed_inventory_claim_owner_invalid', 'typed_inventory_video_types_invalid', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_initial_video_collection_batch': Operation(
        name='claim_initial_video_collection_batch', read_only=False,
        statement_timeout_ms=8000, lock_timeout_ms=3000,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'initial_video_collection_channel_ineligible', 'initial_video_collection_claim_input_invalid', 'initial_video_collection_job_lease_lost', 'initial_video_collection_job_missing', 'initial_video_collection_target_scope_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_inventory_pass': Operation(
        name='claim_inventory_pass', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_storage_active_work', 'inventory_continuations_invalid', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'typed_inventory_channel_ids_invalid', 'typed_inventory_claim_lease_invalid', 'typed_inventory_claim_limit_invalid', 'typed_inventory_claim_owner_invalid', 'typed_inventory_video_types_invalid')),
    ),
    'operator_candidate_page': Operation(
        name='operator_candidate_page', read_only=True,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=15000,
    ),
    'claim_operator_job_id': Operation(
        name='claim_operator_job_id', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=250, total_timeout_ms=5000,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_lease_seconds_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_worker_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_operator_job': Operation(
        name='claim_operator_job', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_seconds_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_worker_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_typed_inventory_channels': Operation(
        name='claim_typed_inventory_channels', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_storage_active_work', 'inventory_continuations_invalid', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'typed_inventory_channel_ids_invalid', 'typed_inventory_claim_lease_invalid', 'typed_inventory_claim_limit_invalid', 'typed_inventory_claim_owner_invalid', 'typed_inventory_video_types_invalid')),
    ),
    'claim_video_collection_ids': Operation(
        name='claim_video_collection_ids', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'claim_video_metadata_collection_ids': Operation(
        name='claim_video_metadata_collection_ids', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'clear_channel_initial_inventory_error': Operation(
        name='clear_channel_initial_inventory_error', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'channel_sync_channel_not_found', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'complete_stream_lifecycle_probe': Operation(
        name='complete_stream_lifecycle_probe', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'stream_lifecycle_completion_input_invalid', 'stream_lifecycle_completion_receipt_mismatch', 'stream_lifecycle_completion_requires_array', 'stream_lifecycle_completion_stale_or_ineligible', 'typed_completion_observation_clock_regression', 'typed_completion_owned_settlement_count_mismatch', 'typed_video_collection_cadence_missing', 'typed_video_collection_due_input_invalid')),
    ),
    'complete_typed_video_metadata_collection': Operation(
        name='complete_typed_video_metadata_collection', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'typed_completion_observation_clock_regression', 'typed_completion_owned_settlement_count_mismatch', 'typed_long_availability_restore_stale_observation', 'typed_video_collection_cadence_missing', 'typed_video_collection_due_input_invalid', 'typed_video_metadata_completion_count_mismatch', 'typed_video_metadata_completion_input_invalid', 'typed_video_metadata_completion_receipt_mismatch', 'typed_video_metadata_completion_requires_array', 'typed_video_metadata_completion_stale_or_ineligible', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'complete_typed_video_missing': Operation(
        name='complete_typed_video_missing', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'typed_completion_observation_clock_regression', 'typed_completion_owned_settlement_count_mismatch', 'typed_video_collection_cadence_missing', 'typed_video_collection_due_input_invalid', 'typed_video_missing_completion_input_invalid', 'typed_video_missing_completion_receipt_mismatch', 'typed_video_missing_completion_requires_array', 'typed_video_missing_completion_stale_or_ineligible', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'complete_video_counter_collection': Operation(
        name='complete_video_counter_collection', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid')),
    ),
    'complete_video_metadata_collection': Operation(
        name='complete_video_metadata_collection', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid')),
    ),
    'complete_video_profile_metadata_collection': Operation(
        name='complete_video_profile_metadata_collection', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'typed_long_availability_restore_stale_observation', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'create_channel_for_collection': Operation(
        name='create_channel_for_collection', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_create_invalid', 'channel_collection_state_missing', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'decide_video_type_review': Operation(
        name='decide_video_type_review', read_only=False,
        statement_timeout_ms=8000, lock_timeout_ms=1000,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'invalid_video_type_review_decision', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'defer_typed_inventory_claims_after_daily_quota': Operation(
        name='defer_typed_inventory_claims_after_daily_quota', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_inventory_receipt_source_expired', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'typed_inventory_quota_defer_checked_at_invalid', 'typed_inventory_quota_defer_claim_duplicate', 'typed_inventory_quota_defer_claim_invalid', 'typed_inventory_quota_defer_claims_invalid', 'typed_inventory_quota_defer_transition_id_invalid', 'typed_inventory_quota_defer_transition_id_reused')),
    ),
    'enqueue_channel_current_stats_backfill': Operation(
        name='enqueue_channel_current_stats_backfill', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_targets_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_has_active_job', 'operator_job_channel_not_visible', 'operator_job_channel_required', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_max_items_required', 'operator_job_max_quota_units_required', 'operator_job_max_target_attempts_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'enqueue_channel_initial_inventory_job': Operation(
        name='enqueue_channel_initial_inventory_job', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_limits_must_be_null', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'initial_typed_channel_id_invalid', 'operator_job_channel_has_active_job', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_confirmation_required', 'operator_job_estimate_changed', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_max_target_attempts_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'enqueue_video_collection_ids': Operation(
        name='enqueue_video_collection_ids', read_only=False,
        statement_timeout_ms=8000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'common_pool_enqueue_input_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'typed_video_collection_priority_input_invalid', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'ensure_video_collection_state': Operation(
        name='ensure_video_collection_state', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'estimate_channel_initial_inventory_job': Operation(
        name='estimate_channel_initial_inventory_job', read_only=True,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('operator_job_channel_not_visible',)),
    ),
    'fail_initial_inventory_job_chunk': Operation(
        name='fail_initial_inventory_job_chunk', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('all_format_playlist_absence_requires_inventory_settlement', 'channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'channel_sync_channel_not_found', 'channel_sync_channel_required', 'channel_sync_error_code_invalid', 'full_initial_job_checkpoint_invalid', 'full_initial_page_coverage_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_target_count_mismatch', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'initial_inventory_absence_receipt_required', 'initial_inventory_failure_code_mismatch', 'initial_inventory_failure_receipt_invalid', 'initial_inventory_job_ack_conflict', 'initial_inventory_job_checkpoint_mismatch', 'initial_inventory_job_receipt_counts_mismatch', 'initial_inventory_job_receipt_required', 'initial_inventory_outcome_unresolved', 'initial_inventory_page_receipt_invalid', 'initial_inventory_page_requires_job_settlement', 'initial_inventory_quota_failure_mismatch', 'initial_inventory_quota_receipt_invalid', 'initial_inventory_receipt_conflict', 'initial_inventory_receipt_expired', 'initial_inventory_receipt_identity_invalid', 'initial_inventory_scan_identity_invalid', 'initial_inventory_state_missing', 'initial_inventory_terminal_evidence_missing', 'initial_inventory_unbound_failure_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_error_code_invalid', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_inventory_settlement_invalid', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'fail_typed_inventory_scan': Operation(
        name='fail_typed_inventory_scan', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'hot_inventory_receipt_source_expired', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'inventory_policy_input_invalid', 'inventory_quota_defer_required', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'typed_inventory_discovery_state_missing', 'typed_inventory_expected_token_invalid', 'typed_inventory_failure_checkpoint_mismatch', 'typed_inventory_failure_identity_invalid', 'typed_inventory_failure_lease_lost', 'typed_inventory_failure_lease_not_owned', 'typed_inventory_format_state_missing', 'typed_inventory_receipt_legacy_unverifiable', 'typed_inventory_scan_not_owned', 'typed_inventory_transition_id_reused')),
    ),
    'get_channel_initial_inventory_job': Operation(
        name='get_channel_initial_inventory_job', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(()),
    ),
    'get_channel_initial_typed_hydration': Operation(
        name='get_channel_initial_typed_hydration', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=0,
        error_codes=frozenset(('initial_typed_channel_id_invalid',)),
    ),
    'get_operator_job_status': Operation(
        name='get_operator_job_status', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(()),
    ),
    'get_video_type_review_case': Operation(
        name='get_video_type_review_case', read_only=True,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(()),
    ),
    'has_open_video_type_reviews': Operation(
        name='has_open_video_type_reviews', read_only=True,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(()),
    ),
    'inspect_typed_inventory_canonical_repair': Operation(
        name='inspect_typed_inventory_canonical_repair', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('typed_inventory_canonical_repair_example_limit_invalid',)),
    ),
    'interrupt_hot_sync_run': Operation(
        name='interrupt_hot_sync_run', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=250,
        error_codes=frozenset(('hot_interrupt_invalid_run', 'sync_run_summary_invalid_column_mask', 'sync_run_summary_invalid_reported_values', 'sync_run_summary_missing_column_mask', 'sync_run_summary_shape_unsupported')),
    ),
    'list_active_operator_jobs': Operation(
        name='list_active_operator_jobs', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(()),
    ),
    'list_video_type_review_cases': Operation(
        name='list_video_type_review_cases', read_only=True,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('invalid_video_type_review_page',)),
    ),
    'list_video_type_review_history': Operation(
        name='list_video_type_review_history', read_only=True,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('invalid_video_type_review_history_page',)),
    ),
    'mark_completed_initial_hydration_failed': Operation(
        name='mark_completed_initial_hydration_failed', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired')),
    ),
    'pause_ineligible_typed_inventory_runtime': Operation(
        name='pause_ineligible_typed_inventory_runtime', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'prepare_channel_initial_typed_inventory': Operation(
        name='prepare_channel_initial_typed_inventory', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'initial_typed_channel_id_invalid', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'prepare_initial_hydration_job_chunk': Operation(
        name='prepare_initial_hydration_job_chunk', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_chunk_limit_invalid', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_initial_phase_invalid', 'operator_job_lease_lost', 'operator_job_lease_seconds_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'prepare_initial_inventory_job_chunk': Operation(
        name='prepare_initial_inventory_job_chunk', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'channel_sync_channel_not_found', 'channel_sync_channel_required', 'channel_sync_error_code_invalid', 'full_initial_page_coverage_invalid', 'full_initial_reconciliation_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_target_count_mismatch', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'initial_inventory_active_scan_invalid', 'initial_inventory_failure_receipt_invalid', 'initial_inventory_page_receipt_invalid', 'initial_inventory_quota_receipt_invalid', 'initial_inventory_receipt_conflict', 'initial_inventory_receipt_expired', 'initial_inventory_receipt_identity_invalid', 'initial_inventory_receipt_reconciliation_required', 'initial_inventory_scan_identity_invalid', 'initial_inventory_state_missing', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_inventory_page_limit_invalid', 'operator_job_lease_lost', 'operator_job_lease_seconds_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'prepare_operator_job_chunk': Operation(
        name='prepare_operator_job_chunk', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_chunk_limit_invalid', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_initial_phase_invalid', 'operator_job_lease_lost', 'operator_job_lease_seconds_invalid', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'prepare_typed_inventory_runtime': Operation(
        name='prepare_typed_inventory_runtime', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'typed_inventory_prepare_enable_required', 'typed_inventory_prepare_limit_invalid', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'prioritize_typed_inventory_channels': Operation(
        name='prioritize_typed_inventory_channels', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'typed_inventory_priority_channel_id_invalid', 'typed_inventory_priority_channel_ids_invalid', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'reconcile_typed_video_collection_state': Operation(
        name='reconcile_typed_video_collection_state', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'common_pool_reconcile_cursor_missing', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'typed_video_collection_cadence_missing', 'typed_video_collection_due_input_invalid', 'typed_video_collection_reconcile_input_invalid', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'record_channel_initial_inventory_error': Operation(
        name='record_channel_initial_inventory_error', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'channel_sync_channel_not_found', 'channel_sync_channel_required', 'channel_sync_error_code_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'release_operator_job': Operation(
        name='release_operator_job', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired')),
    ),
    'repair_typed_inventory_canonical_types': Operation(
        name='repair_typed_inventory_canonical_types', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=5000,
        error_codes=frozenset(('channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'typed_inventory_canonical_repair_candidate_drift', 'typed_inventory_canonical_repair_collection_state_remains', 'typed_inventory_canonical_repair_example_limit_invalid', 'typed_inventory_canonical_repair_expected_count_invalid', 'typed_inventory_canonical_repair_fingerprint_invalid', 'typed_inventory_canonical_repair_not_ready', 'typed_inventory_canonical_repair_plan_changed', 'typed_inventory_canonical_repair_postcondition_failed', 'typed_inventory_canonical_repair_update_count_mismatch', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'retry_channel_initial_inventory_job_v5': Operation(
        name='retry_channel_initial_inventory_job_v5', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_limits_must_be_null', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'initial_hydration_workset_changed_after_verify', 'initial_hydration_workset_evidence_missing', 'initial_hydration_workset_repair_incomplete', 'initial_inventory_restart_required', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_has_active_job', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_confirmation_required', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_retry_cap_below_observed', 'operator_job_retry_cap_invalid', 'operator_job_retry_legacy_scope_unverifiable', 'operator_job_retry_not_available', 'operator_job_retry_request_conflict', 'operator_job_retry_request_id_required', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'set_channel_collection_permission': Operation(
        name='set_channel_collection_permission', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost')),
    ),
    'set_channel_trash_state': Operation(
        name='set_channel_trash_state', read_only=False,
        statement_timeout_ms=5000, lock_timeout_ms=250,
        error_codes=frozenset(('channel_trash_invalid_request', 'hot_storage_active_work', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired')),
    ),
    'settle_initial_hydration_job_chunk': Operation(
        name='settle_initial_hydration_job_chunk', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('operator_job_unstarted_scope_invalid', 'channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_chunk_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_error_code_invalid', 'operator_job_hydration_settlement_invalid', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_initial_phase_invalid', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'settle_initial_inventory_job_chunk': Operation(
        name='settle_initial_inventory_job_chunk', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'channel_sync_channel_not_found', 'channel_sync_channel_required', 'channel_sync_error_code_invalid', 'full_initial_job_checkpoint_invalid', 'full_initial_page_coverage_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_target_count_mismatch', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'initial_inventory_failure_receipt_invalid', 'initial_inventory_job_ack_conflict', 'initial_inventory_job_checkpoint_mismatch', 'initial_inventory_job_receipt_counts_mismatch', 'initial_inventory_job_receipt_required', 'initial_inventory_page_receipt_invalid', 'initial_inventory_quota_receipt_invalid', 'initial_inventory_receipt_conflict', 'initial_inventory_receipt_expired', 'initial_inventory_receipt_identity_invalid', 'initial_inventory_scan_identity_invalid', 'initial_inventory_state_missing', 'initial_inventory_terminal_evidence_missing', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_inventory_settlement_invalid', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'settle_initial_inventory_stats': Operation(
        name='settle_initial_inventory_stats', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_initial_stats_settlement_invalid', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired')),
    ),
    'settle_operator_job_chunk': Operation(
        name='settle_operator_job_chunk', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('operator_job_unstarted_scope_invalid', 'channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_chunk_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_initial_phase_invalid', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'update_channel_collection_fields': Operation(
        name='update_channel_collection_fields', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_fields_invalid', 'channel_collection_scope_invalid', 'channel_collection_scope_lost')),
    ),
    'update_channel_metadata_batch': Operation(
        name='update_channel_metadata_batch', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_discovery_state_missing', 'channel_discovery_update_invalid', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'channel_metadata_batch_field_invalid', 'channel_metadata_batch_invalid', 'channel_metadata_control_action_state_changed', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'verify_initial_hydration_job': Operation(
        name='verify_initial_hydration_job', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'full_initial_scope_invalid', 'full_initial_source_clock_invalid', 'full_initial_source_coverage_invalid', 'full_initial_source_identity_invalid', 'full_initial_targets_frozen', 'full_initial_targets_not_frozen', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'operator_job_channel_not_visible', 'operator_job_channel_sync_not_ready', 'operator_job_initial_inventory_status_not_eligible', 'operator_job_lease_lost', 'operator_job_source_clock_immutable', 'operator_job_source_scope_expired', 'operator_job_target_identity_immutable', 'operator_job_target_type_required', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'settle_typed_inventory_page': Operation(
        name='settle_typed_inventory_page', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=0,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'hot_inventory_receipt_source_expired', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'inventory_policy_input_invalid', 'inventory_token_cycle', 'inventory_token_history_invalid', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'typed_inventory_boundary_requires_discovery', 'typed_inventory_channel_not_eligible', 'typed_inventory_continuation_invalid', 'typed_inventory_discovery_state_missing', 'typed_inventory_empty_page_has_items', 'typed_inventory_empty_page_with_continuation', 'typed_inventory_expected_token_invalid', 'typed_inventory_format_state_missing', 'typed_inventory_page_checkpoint_mismatch', 'typed_inventory_page_identity_invalid', 'typed_inventory_page_item_invalid', 'typed_inventory_page_items_invalid', 'typed_inventory_page_items_not_unique', 'typed_inventory_page_lease_lost', 'typed_inventory_page_lease_not_owned', 'typed_inventory_page_too_large', 'typed_inventory_receipt_legacy_unverifiable', 'typed_inventory_scan_not_owned', 'typed_inventory_source_before_scan', 'typed_inventory_terminal_token_invalid', 'typed_inventory_transition_id_reused', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
    'complete_initial_video_batch': Operation(
        name='complete_initial_video_batch', read_only=False,
        statement_timeout_ms=60000, lock_timeout_ms=3000, total_timeout_ms=60000,
        error_codes=frozenset(('channel_collection_scope_invalid', 'channel_collection_scope_lost', 'channel_inventory_estimate_clock_conflict', 'channel_inventory_estimate_invalid', 'common_video_collection_fail_input_invalid', 'common_video_collection_fail_limit', 'common_video_collection_fail_row_invalid', 'common_video_collection_fail_stale_ownership', 'hot_source_clock_out_of_window', 'hot_storage_active_work', 'hot_video_refresh_as_of_invalid', 'initial_video_batch_incomplete', 'manual_video_type_collection_busy', 'manual_video_type_decision_protected', 'manual_video_type_queue_decision_missing', 'membership_absence_requires_completed_full_generation', 'membership_first_seen_scan_scope_mismatch', 'membership_last_seen_scan_scope_mismatch', 'stream_lifecycle_completion_input_invalid', 'stream_lifecycle_completion_receipt_mismatch', 'stream_lifecycle_completion_requires_array', 'stream_lifecycle_completion_stale_or_ineligible', 'typed_completion_observation_clock_regression', 'typed_completion_owned_settlement_count_mismatch', 'typed_long_availability_restore_stale_observation', 'typed_video_collection_cadence_missing', 'typed_video_collection_due_input_invalid', 'typed_video_collection_fail_input_invalid', 'typed_video_collection_fail_stale_claim', 'typed_video_collection_fail_too_many_rows', 'typed_video_metadata_completion_count_mismatch', 'typed_video_metadata_completion_input_invalid', 'typed_video_metadata_completion_receipt_mismatch', 'typed_video_metadata_completion_requires_array', 'typed_video_metadata_completion_stale_or_ineligible', 'typed_video_missing_completion_input_invalid', 'typed_video_missing_completion_receipt_mismatch', 'typed_video_missing_completion_requires_array', 'typed_video_missing_completion_stale_or_ineligible', 'user_work_identity_immutable', 'user_work_limit_reached')),
    ),
})


def get_operation(name: str) -> Operation:
    """Return a fixed repository-owned protocol contract."""
    try:
        return OPERATIONS[name]
    except (KeyError, TypeError):
        raise ValueError("database_operation_not_registered") from None
