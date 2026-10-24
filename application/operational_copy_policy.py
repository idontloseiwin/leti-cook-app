"""Finite diagnostics vocabulary; identifiers and provider text never become labels."""
from __future__ import annotations
from datetime import datetime
import math
import re

QUOTA_LABELS = {
    "quota_bucket": frozenset("general batch_stats video_batch_get_stats".split()),
    "endpoint": frozenset("channels playlistItems videos videos:batchGetStats videoCategories i18nLanguages search playlists subscriptions comments commentThreads activities".split()),
    "phase": frozenset('add_channel channel_discovery channel_inventory_count channel_inventory_renewal channel_metadata channel_resolve_video current_stats initial_inventory legacy_inventory_repair manual_channel_refresh verify'.split()) | frozenset("channel_metadata channel_discovery video_metadata video_stats batch_stats_pilot regular_video_stats new_video_stats initial_video_stats initial_inventory initial_hydration typed_inventory typed_video_metadata stream_lifecycle inventory_repair availability_recheck long_availability_loss video_collection youtube_categories youtube_category_languages".split())
             | frozenset("auto_stats_batch auto_video_collection category_metadata initial_28d_import initial_channel_hydration manual_channel_backfill playlist_inventory typed_inventory_long typed_inventory_short typed_inventory_stream typed_playlist_inventory".split()),
    "error_type": frozenset("HttpError HTTPError HTTPStatusError ConnectError ConnectTimeout ReadTimeout WriteTimeout PoolTimeout TimeoutError ConnectionError ConnectionResetError ConnectionAbortedError BrokenPipeError OSError ValueError TypeError RuntimeError TransportError RemoteProtocolError RequestError APIError QuotaExceeded quotaExceeded dailyLimitExceeded userRateLimitExceeded InvalidErrorType OtherError".split()),
}

def quota_label(field, value):
    if value is None and field == "error_type":
        return None
    return value if isinstance(value, str) and value in QUOTA_LABELS[field] else ("OtherError" if field == "error_type" else "other")

SUMMARY_SECTIONS = frozenset("channel_metadata channel_discovery new_video_stats regular_video_stats video_collection typed_inventory stream_lifecycle long_availability_loss availability_recheck routing timings selector_timings result_status_counts schedule_reason_counts streak_buckets reported_values".split())
SUMMARY_LABELS = frozenset("completed partial incomplete error queued running blocked skipped missing public private deleted updated unavailable no_new_videos new_videos no_new_backoff new_video leased_channel_discovery_queue leased_video_collection_queue due_at_video_id ready disabled not_run stopped quota_exhausted general batch_stats other 0_hot 1 2 3_5 6_plus".split())
SUMMARY_FIELDS = frozenset("summary_schema_version column_fields errors processed updated requested claimed skipped unavailable missing new_videos video_count_changed stats_updated metadata_processed metadata_updated discovery_processed new_video_stats_updated regular_stats_requested regular_stats_updated unavailable_marked failed deferred unresolved incomplete completion_outcome explicit_selected explicit_unclaimed metadata_requests batch_requests stop_collection stop_general_requests quota_exhausted daily_quota_exhausted infrastructure_error completion_unknown mode database_queue_enabled batch_stats_enabled enabled due_count selected candidate_count selected_count completed_count error_count total_seconds claim_seconds metadata_seconds stats_seconds discovery_seconds duration_seconds duration_ms quota_units quota_units_estimated quota_units_observed_estimate pages pages_processed items items_processed inserted updated_count skipped_count target_count processed_count unavailable_count unknown_count received accepted ignored released scheduled quota_remaining observed_quota_remaining next_due_at_count results_count new_video_ids_count video_ids_count missing_video_ids_count updated_video_ids_count skipped_video_ids_count unavailable_video_ids_count error_video_ids_count batch_timings_count error_messages_count initial_inventory_requests_count stale_run_recovered".split())

# Numeric work counters already consumed by Health; no provider values or payloads.
SUMMARY_FIELDS |= frozenset("yield_requested batch_recurring_requested batches degraded_batch_requests failed_id_fallbacks fallback_requests long_missing long_requested long_unavailable_marked long_updated metadata_deferred metadata_duplicate_items metadata_refresh_requested metadata_refresh_requests partial_counter_updates presence_fallback_ids short_missing short_requested short_unavailable_marked short_updated source_view_count_absent source_view_count_invalid stream_lifecycle_probed publication_waiting stream_missing stream_requested stream_unavailable_marked stream_updated".split())

# Existing cycle measurements, not provider data. Keep their finite names in
# both the API and persisted projection; compaction visits timings as a section.
SUMMARY_TIMING_FIELDS = frozenset("preflight_seconds lifecycle_recovery_seconds channel_metadata_seconds typed_inventory_seconds long_availability_loss_seconds channel_discovery_seconds collect_new_video_ids_seconds new_video_stats_seconds video_collection_seconds regular_video_stats_seconds short_video_collection_seconds completed_stream_collection_seconds elapsed_seconds total_seconds".split())
SUMMARY_FIELDS |= SUMMARY_TIMING_FIELDS

# Only the cycle's recorded root progress belongs in a run summary. These are
# separate from generic labels: a phase name must not admit arbitrary strings
# elsewhere, including nested reported_values or per-section diagnostics.
SUMMARY_PROGRESS_FIELDS = frozenset({"current_phase", "phase_state", "progress_updated_at"})
SUMMARY_PHASES = frozenset("preflight lifecycle_recovery video_collection channel_metadata typed_inventory channel_discovery collect_new_video_ids new_video_stats short_video_collection completed_stream_collection completed".split())
SUMMARY_PHASE_STATES = frozenset({"running", "completed", "error", "paused"})
_PROGRESS_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def summary_progress_value(key, value, depth=0):
    """Return a validated root progress value, or None without echoing input."""
    if depth != 0 or type(value) is not str:
        return None
    if key == "current_phase":
        return value if value in SUMMARY_PHASES else None
    if key == "phase_state":
        return value if value in SUMMARY_PHASE_STATES else None
    if key != "progress_updated_at" or not _PROGRESS_TIMESTAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.utcoffset() is not None else None


def safe_summary_copy(value, depth=0):
    """Project only finite operational names, finite numbers and known labels."""
    if not isinstance(value, dict) or depth > 3:
        return {}
    result = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if key in SUMMARY_PROGRESS_FIELDS:
            progress = summary_progress_value(key, item, depth)
            if progress is not None:
                result[key] = progress
        elif isinstance(item, dict) and key in SUMMARY_SECTIONS:
            nested = safe_summary_copy(item, depth + 1)
            if nested:
                result[key] = nested
        elif key in SUMMARY_FIELDS or key in SUMMARY_LABELS:
            if item is None or isinstance(item, bool):
                result[key] = item
            elif isinstance(item, int) or (isinstance(item, float) and math.isfinite(item)):
                result[key] = item
            elif isinstance(item, str) and item in SUMMARY_LABELS:
                result[key] = item
    return result
