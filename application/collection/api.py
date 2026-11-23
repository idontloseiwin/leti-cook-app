"""Collection scenario entrypoints; callers supply their owned runtime scope.

Imports below expose existing scenarios directly. This module owns no resource,
configuration, scheduler, or fallback service registry.
"""

from collection.context import CollectionContext
from channel_collection_scope import require_control_epoch
from collection import initial
from collection.channel_metadata import (
    bounded_avatar_url, channel_country_code, channel_created_at,
    channel_metadata_update_values, run_channel_metadata_sync_batch,
)
from collection.cycle import (
    require_video_collection_enabled,
    run_auto_sync_cycle,
    run_typed_playlist_inventory_batch,
    run_typed_video_collection_queue_batch,
    run_video_stats_sync_batch,
    validate_required_auto_sync_rpcs,
    validate_required_stats_queue_rpcs,
)
from collection.runs import start_sync_run, finish_sync_run, update_sync_run_progress
from collection_outcome import copy_collection_stop
from youtube_errors import YouTubePlaylistNotFoundError, YouTubeDailyQuotaExceededError


def initial_inventory_for_channel(channel_id, *, context: CollectionContext, expected_epoch):
    """Schedule existing durable work; this entrypoint never fetches pages."""
    return context.enqueue_initial_inventory(
        channel_id, expected_epoch=require_control_epoch(expected_epoch),
    )


def run_video_collection_queue_batch(
    limit=50, explicit_video_ids=None, reason="auto_stats_batch",
    progress_callback=None, force_metadata=False, common_preflight_checked=False,
    initial_job=None, channel_scopes=None, *, context: CollectionContext, initial_load_concurrency=8,
):
    return context.video.run_batch(
        limit=limit, explicit_video_ids=explicit_video_ids, reason=reason,
        progress_callback=progress_callback, force_metadata=force_metadata,
        common_preflight_checked=common_preflight_checked, initial_job=initial_job, channel_scopes=channel_scopes,
        initial_load_concurrency=initial_load_concurrency,
    )


def collect_initial_inventory_chunk(
    channel_id, *, max_pages=1, page_token=None, video_type=None, expected_epoch,
    context: CollectionContext,
):
    return initial.collect_initial_inventory_chunk(
        channel_id, max_pages=max_pages, page_token=page_token, video_type=video_type, expected_epoch=expected_epoch,
        database=context.database, settings=context.settings,
        worker_id=context.video_worker_id() + ":typed-inventory",
        fetch_page=context.sources.fetch_playlist_items_page,
        get_longform_playlist_id=context.sources.get_longform_playlist_id,
        parse_dt=context.sources.parse_dt,
        playlist_not_found_error=YouTubePlaylistNotFoundError,
        daily_quota_exceeded_error=YouTubeDailyQuotaExceededError,
    )


def initial_28d_stats_import(channel_id, *, context: CollectionContext, expected_epoch):
    return initial.initial_28d_stats_import(
        channel_id, database=context.database, settings=context.settings, expected_epoch=expected_epoch,
        run_video_collection_queue_batch=context.video.run_batch,
        copy_collection_stop=copy_collection_stop,
    )


from collection.hydration import (
    completed_initial_hydration_has_errors,
    initial_hydration_has_target_count_mismatch,
    initial_hydration_verified_target_count,
    initial_hydration_verified_target_error_count,
    initial_hydration_scope_expired,
    initial_hydration_job_is_retryable,
    initial_hydration_estimate_int,
    normalized_initial_hydration_estimate,
    project_initial_hydration_retry_estimate,
    initial_hydration_payload,
    initial_hydration_error_payload,
    require_full_hydration_schema,
    load_initial_hydration_context,
    load_initial_hydration_state,
    with_initial_format_readiness
)
from collection.commands import (
    normalize_completed_initial_hydration_failure,
    retry_initial_hydration_job,
    prepare_initial_typed_inventory,
    enqueue_initial_hydration,
    confirm_initial_hydration
)
from collection.hydration import (
    FULL_HYDRATION_SCHEMA_VERSION, FULL_HYDRATION_PUBLIC_STATUSES,
    FULL_HYDRATION_TERMINAL_JOB_STATUSES, FULL_HYDRATION_RETRYABLE_JOB_STATUSES,
)

from collection.cycle import unique_video_ids
