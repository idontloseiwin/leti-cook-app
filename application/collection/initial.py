"""Initial page advancement and retained scope-zero 28-day statistics import."""

import uuid
from datetime import datetime, timezone, timedelta

from db import ChannelSyncRepository, TypedInventoryRepository
from channel_collection_scope import require_control_epoch
from collection.inventory import run_typed_inventory_batch
from collection.lifecycle import update_channel_lifecycle_status
from collection_outcome import collection_outcome_fields
from source_log import source_log_message


INITIAL_28D_STATS_DAYS = 28
INITIAL_28D_STATS_MAX_VIDEOS = 150


def collect_initial_inventory_chunk(
    channel_id, *, max_pages=1, page_token=None, video_type=None, expected_epoch,
    database, settings, worker_id, fetch_page, get_longform_playlist_id, parse_dt,
    playlist_not_found_error, daily_quota_exceeded_error,
    transition_id=None, before_dispatch=None,
    initial_job_id=None, initial_job_lease_token=None,
):
    """One addressed page; the durable caller binds it before provider dispatch.

    Omitting video_type keeps the old Long result adapter, not a second scan
    implementation. Cursors remain exclusively in the shared inventory scan.
    """
    expected_epoch = require_control_epoch(expected_epoch)
    if not isinstance(worker_id, str):
        raise TypeError("worker_id must be supplied as a string")
    if not worker_id.strip():
        raise ValueError("worker_id must not be empty")
    if database is None or settings is None:
        raise TypeError("database and settings are required")
    if not all(callable(value) for value in (fetch_page, get_longform_playlist_id, parse_dt)):
        raise TypeError("fetch_page, get_longform_playlist_id and parse_dt callables are required")
    if playlist_not_found_error is None or daily_quota_exceeded_error is None:
        raise TypeError("playlist and quota error classes are required")
    if type(max_pages) is not int or max_pages < 0:
        raise ValueError("initial inventory page limit must be a nonnegative integer")
    if (initial_job_id is None) != (initial_job_lease_token is None):
        raise ValueError("initial inventory job identity requires both job and lease")
    job_options = {}
    if initial_job_id is not None:
        job_options = dict(initial_job_id=str(uuid.UUID(str(initial_job_id))),
            initial_job_lease_token=str(uuid.UUID(str(initial_job_lease_token))))
        if video_type is None or max_pages not in (0, 1):
            raise ValueError("initial inventory job requires an explicit one-page source")
    legacy_shape = video_type is None
    video_type = "long" if legacy_shape else video_type
    if video_type not in {"long", "short", "stream"}:
        raise ValueError("initial inventory video type is invalid")
    if page_token is not None:
        raise ValueError("inventory cursor belongs to its existing scan")
    if (transition_id is None) != (before_dispatch is None):
        raise ValueError("initial inventory planned dispatch requires transition and callback")
    if before_dispatch is not None and not callable(before_dispatch):
        raise TypeError("before_dispatch must be callable")

    def adapt(result, ids=()):
        if not legacy_shape:
            return result
        return {**result, "longform_ids": list(ids), "longform_pages": result["pages"],
                "longform_inserted": result.get("inserted", 0),
                "longform_updated": result.get("updated", 0), "next_longform_page_token": None}

    if not max_pages:
        return adapt({"pages": 0, "items": 0, "outcome": "stored_inventory"})
    if not settings.typed_playlist_inventory_enabled:
        raise RuntimeError("initial inventory runtime is disabled")
    summary = run_typed_inventory_batch(
        repository=TypedInventoryRepository(database), fetch_page=fetch_page,
        limit=1, worker_id=worker_id, explicit_channel_ids=[channel_id],
        expected_channel_scopes=[{"channel_id": channel_id, "control_epoch": expected_epoch}],
        video_types=(video_type,), playlist_not_found_errors=(playlist_not_found_error,),
        prepare=False, transition_id=transition_id, before_dispatch=before_dispatch,
        **job_options,
    )
    # A missing ACK may have committed. Never replace that page with a failure.
    # HOT refusals are control outcomes, not page receipts. Preserve them before
    # the normal identity checks, including a failed/uncertain control release.
    if summary.get("hot_runtime_interrupted"):
        return adapt({"pages": 0, "items": 0, "outcome": "hot_runtime_interrupted",
                      "hot_runtime_interrupted": True, "infrastructure_error": True,
                      "settlement_unknown": bool(summary.get("settlement_unknown")),
                      "provider_attempts": int(summary.get("provider_attempts") or 0),
                      "stopped_reason": "hot_runtime_interrupted"})
    if summary.get("settlement_unknown"):
        return adapt({"pages": 0, "items": 0, "outcome": "await_receipt",
                      "settlement_unknown": True,
                      "provider_attempts": int(summary.get("provider_attempts") or 0),
                      "quota_exhausted": bool(summary.get("quota_exhausted"))})
    if before_dispatch is None:
        # Retained public callback compatibility outside durable jobs.
        if summary.get("quota_exhausted"):
            raise daily_quota_exceeded_error("initial inventory waits for the daily quota reset")
        if summary.get("rate_limited"):
            from youtube_errors import YouTubeRateLimitError
            raise YouTubeRateLimitError()
        if summary.get("errors"):
            raise RuntimeError("initial inventory chunk did not settle successfully")
    results = summary.get("results") or []
    if not results:
        if summary.get("errors") or summary.get("provider_attempts"):
            return adapt({"pages": 0, "items": 0, "outcome": "await_receipt",
                          "settlement_unknown": True})
        return adapt({"pages": 0, "items": 0, "outcome": "deferred"})
    if len(results) != 1:
        raise RuntimeError("initial inventory returned multiple page results")
    result = results[0]
    if result.get("channel_id") != channel_id or result.get("video_type") != video_type:
        raise RuntimeError("initial inventory returned an unexpected identity")
    if transition_id is not None and result.get("transition_id") != transition_id:
        raise RuntimeError("initial inventory returned an unexpected transition")
    outcome = str(result.get("status") or "")
    if outcome == "quota_deferred" and result.get("claim_release_status") != "quota_deferred":
        return adapt({"pages": 0, "items": 0, "outcome": "await_receipt",
                      "settlement_unknown": True, "quota_exhausted": True})
    return adapt({
        "pages": int(result.get("pages_processed") or 0),
        "items": int(result.get("page_items") or 0),
        "inserted": int(result.get("inserted") or 0),
        "updated": int(result.get("already_matching") or 0),
        "next_page_token": None, "outcome": outcome,
        "provider_attempts": int(result.get("provider_attempts") or 0),
        "error_code": ("initial_inventory_quota_exhausted" if outcome == "quota_deferred"
                       else result.get("error_type")),
        "quota_exhausted": bool(summary.get("quota_exhausted")),
        **({"rate_limited": True} if summary.get("rate_limited") else {}),
        **({"observed_video_ids": result["observed_video_ids"]} if "observed_video_ids" in result else {}),
    }, result.get("canonical_video_ids") or [])


def load_recent_long_inventory_rows(channel_id, days=INITIAL_28D_STATS_DAYS, *, database):
    if database is None:
        raise TypeError("database is required")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # Fetch one extra row so callers can distinguish "at limit" from "too many".
    limit_for_detection = INITIAL_28D_STATS_MAX_VIDEOS + 1
    return ChannelSyncRepository(database).load_recent_long_inventory(
        channel_id,
        published_at_gte=cutoff,
        limit=limit_for_detection,
    )


def initial_28d_stats_import(
    channel_id, *, database, settings, run_video_collection_queue_batch, copy_collection_stop, expected_epoch,
):
    expected_epoch = require_control_epoch(expected_epoch)
    if database is None or settings is None:
        raise TypeError("database and settings are required")
    if not callable(run_video_collection_queue_batch) or not callable(copy_collection_stop):
        raise TypeError("video collection and stop projection callbacks are required")
    print(source_log_message('initial_stats_started', lambda: (f"Initial 28d stats import started for {channel_id}")), flush=True)
    try:
        rows = load_recent_long_inventory_rows(channel_id, database=database)
        if not rows:
            update_channel_lifecycle_status(channel_id, {
                "initial_28d_stats_status": "skipped_no_recent_videos",
                "initial_28d_stats_started_at": None,
            }, database=database, expected_epoch=expected_epoch)
            print(
                source_log_message('initial_stats_empty', lambda: (f"Initial 28d stats import skipped for {channel_id}: no recent long videos")),
                flush=True,
            )
            return {"status": "skipped_no_recent_videos", "channel_id": channel_id, "imported": 0}

        if len(rows) > INITIAL_28D_STATS_MAX_VIDEOS:
            update_channel_lifecycle_status(channel_id, {
                "initial_28d_stats_status": "skipped_too_many_recent_videos",
                "initial_28d_stats_started_at": None,
            }, database=database, expected_epoch=expected_epoch)
            print(
                source_log_message('initial_stats_over_limit', lambda: ("Initial 28d stats import skipped for "
                f"{channel_id}: more than {INITIAL_28D_STATS_MAX_VIDEOS} recent long videos")),
                flush=True,
            )
            return {
                "status": "skipped_too_many_recent_videos",
                "channel_id": channel_id,
                "imported": 0,
                "recent_count": len(rows),
            }

        update_channel_lifecycle_status(channel_id, {
            "initial_28d_stats_status": "running",
            "initial_28d_stats_started_at": datetime.now(timezone.utc).isoformat(),
        }, database=database, expected_epoch=expected_epoch)

        video_ids = [row["video_id"] for row in rows]
        sync_summary = run_video_collection_queue_batch(
            limit=len(video_ids),
            explicit_video_ids=video_ids,
            reason="initial_28d_import",
            channel_scopes=[{"channel_id": channel_id, "control_epoch": expected_epoch}],
            force_metadata=True,
            database=database,
            settings=settings,
        )
        sync_summary.update(collection_outcome_fields(sync_summary, explicit_selected=len(set(video_ids))))
        imported = sync_summary.get("updated", 0)
        stopped_result = {
            "status": "error", "channel_id": channel_id,
            "imported": imported, "sync_summary": sync_summary,
        }
        if copy_collection_stop(stopped_result, sync_summary):
            # Leave lifecycle/lease recovery to its existing owner; do not write
            # a successful initial result after an uncertain video settlement.
            return stopped_result
        if sync_summary.get("errors", 0) or sync_summary.get("incomplete"):
            update_channel_lifecycle_status(channel_id, {
                "initial_28d_stats_status": "error",
                "initial_28d_stats_started_at": None,
            }, database=database, expected_epoch=expected_epoch)
            print(
                source_log_message('initial_stats_incomplete', lambda: ("Initial 28d stats import incomplete for "
                f"{channel_id}: {imported} videos imported, "
                f"{sync_summary.get('errors', 0)} errors, "
                f"{sync_summary.get('unresolved', 0)} unresolved"), counts={'updated': imported, 'errors': sync_summary.get('errors', 0), 'unresolved': sync_summary.get('unresolved', 0)}),
                flush=True,
            )
            return {
                "status": "error",
                "channel_id": channel_id,
                "imported": imported,
                "sync_summary": sync_summary,
                "completion_outcome": sync_summary["completion_outcome"],
                "incomplete": sync_summary["incomplete"],
            }

        update_channel_lifecycle_status(channel_id, {
            "initial_28d_stats_status": "completed",
            "initial_28d_stats_started_at": None,
        }, database=database, expected_epoch=expected_epoch)
        print(
            source_log_message('initial_stats_completed', lambda: (f"Initial 28d stats import completed for {channel_id}: {imported} videos imported"), counts={'updated': imported}),
            flush=True,
        )
        return {
            "status": "completed",
            "channel_id": channel_id,
            "imported": imported,
            "sync_summary": sync_summary,
        }
    except Exception as error:
        update_channel_lifecycle_status(channel_id, {
            "initial_28d_stats_status": "error",
            "initial_28d_stats_started_at": None,
        }, database=database, expected_epoch=expected_epoch)
        print(source_log_message('initial_stats_error', lambda: (f"Initial 28d stats import failed for {channel_id}: {error}"), error=error), flush=True)
        return {"status": "error", "channel_id": channel_id, "imported": 0, "error": str(error)}
