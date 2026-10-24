"""Explicit source-free application log messages for the self-hosted profile.

Call sites choose a finite event and operational counts. Private messages are
lazy so provider payloads and exception messages are never formatted here in
self-hosted mode. This is not an output interceptor or a domain error handler.
"""
from __future__ import annotations

import math

from deployment_profile import selected_deployment_profile
from operational_copy_policy import quota_label, safe_summary_copy


SOURCE_LOG_EVENTS = frozenset("""
sync_recovery_error sync_start_error sync_finish_error sync_progress_error
lifecycle_columns_missing lifecycle_update_error inventory_error_compatibility
inventory_error_clear_failed availability_timing_error availability_no_candidates
availability_completed long_loss_no_candidates long_loss_completed long_loss_error
metadata_reconcile_error metadata_response_reconciled metadata_error_state_failed
metadata_batch_error metadata_item_error metadata_update_error metadata_initial_error
metadata_reactivation_defer_error
lifecycle_recovery_error lifecycle_recovery_update_error
initial_inventory_started initial_inventory_playlist_missing initial_inventory_completed
initial_inventory_skipped initial_inventory_error initial_stats_started
initial_stats_empty initial_stats_over_limit initial_stats_incomplete
initial_stats_completed initial_stats_error
auto_preflight_error auto_video_collection_error auto_metadata_error
auto_typed_inventory_error auto_long_loss_error auto_discovery_error
auto_new_video_stats_error auto_short_collection_error auto_stream_collection_error
coverage_fallback_error summary_sync_runs_error summary_queue_health_error
summary_typed_count_error availability_history_error category_summary_error
typical_range_error initial_typed_inventory_error operator_job_wake_error
initial_hydration_enqueue_error initial_counts_error home_coverage_error
home_typed_inventory_error home_sync_run_error home_change_run_error
""".split())


def source_log_message(event, private_message, *, error=None,
                       secondary_error=None, counts=None):
    """Return a fixed-vocabulary line without copying source values to logs."""
    if selected_deployment_profile() != "self-hosted":
        return private_message()
    event = event if isinstance(event, str) and event in SOURCE_LOG_EVENTS else "other"
    parts = ["[operation]", "event=" + event]
    for name, value in (("error_type", error), ("secondary_error_type", secondary_error)):
        if value is not None:
            label = value if isinstance(value, str) else type(value).__name__
            parts.append(name + "=" + quota_label("error_type", label))
    for name, value in safe_summary_copy(counts).items():
        if type(value) is int or (type(value) is float and math.isfinite(value)):
            parts.append(name + "=" + str(value))
    return " ".join(parts)
