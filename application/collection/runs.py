"""Durable run accounting with an explicit borrowed collection scope."""

from datetime import datetime, timezone, timedelta

from collection.context import CollectionContext
from collection_outcome import sync_completion_fields
from db import SyncRunRepository
from pipeline_trace import event
from source_log import source_log_message
from sync_run_summary import SYNC_RUN_SUMMARY_SCHEMA_VERSION, compact_sync_run_summary


def new_video_count_from_summary(summary):
    """Count committed inserts once, independently of bounded priority hints."""
    discovery = summary.get("channel_discovery") or {}
    inventory = summary.get("typed_inventory") or {}
    shared = (discovery.get("routing") or {}).get("mode") == "shared_inventory_long_projection"
    return (0 if shared else len(summary.get("new_video_ids") or [])) + int(inventory.get("inserted") or 0)


def sync_run_counts_from_summary(summary):
    channel_metadata = summary.get("channel_metadata") or {}
    channel_discovery = summary.get("channel_discovery") or {}
    long_availability_loss = summary.get("long_availability_loss") or {}
    new_video_stats = summary.get("new_video_stats") or {}
    video_collection = summary.get("video_collection") or {}
    regular_video_stats = summary.get("regular_video_stats") or {}
    short_video_collection = summary.get("short_video_collection") or {}
    completed_stream_collection = summary.get("completed_stream_collection") or {}
    timings = summary.get("timings") or {}
    new_video_stats_updated = int(new_video_stats.get("updated") or 0)
    long_loss_updated = int(long_availability_loss.get("updated") or 0)
    regular_stats_updated = int(regular_video_stats.get("updated") or 0)
    short_stats_updated = int(short_video_collection.get("updated") or 0)
    stream_stats_updated = int(completed_stream_collection.get("updated") or 0)
    collection_stats_updated = (
        int(video_collection.get("updated") or 0)
        if video_collection
        else regular_stats_updated + short_stats_updated + stream_stats_updated
    )
    availability_recheck_restored = int(
        channel_discovery.get("availability_recheck_restored") or 0
    )
    return {
        "finished_at": summary.get("finished_at"),
        "duration_seconds": (
            timings.get("total_seconds")
            if timings.get("total_seconds") is not None
            else timings.get("elapsed_seconds")
        ),
        "errors": int(summary.get("errors") or 0),
        "metadata_processed": int(channel_metadata.get("processed") or 0),
        "metadata_updated": int(channel_metadata.get("updated") or 0),
        "video_count_changed": int(channel_metadata.get("video_count_changed") or 0),
        "discovery_processed": int(channel_discovery.get("processed") or 0),
        "new_videos": new_video_count_from_summary(summary),
        "new_video_stats_updated": new_video_stats_updated,
        "regular_stats_requested": int(regular_video_stats.get("requested") or 0),
        "regular_stats_updated": regular_stats_updated,
        "stats_updated": (
            new_video_stats_updated
            + long_loss_updated
            + collection_stats_updated
            + availability_recheck_restored
        ),
        "unavailable_marked": int(new_video_stats.get("unavailable_marked") or 0)
        + int(long_availability_loss.get("unavailable_marked") or 0)
        + (
            int(video_collection.get("unavailable_marked") or 0)
            if video_collection
            else (
                int(regular_video_stats.get("unavailable_marked") or 0)
                + int(short_video_collection.get("unavailable_marked") or 0)
                + int(completed_stream_collection.get("unavailable_marked") or 0)
            )
        ),
        "missing": int(new_video_stats.get("missing") or 0)
        + int(long_availability_loss.get("missing") or 0)
        + (
            int(video_collection.get("missing") or 0)
            if video_collection
            else (
                int(regular_video_stats.get("missing") or 0)
                + int(short_video_collection.get("missing") or 0)
                + int(completed_stream_collection.get("missing") or 0)
            )
        ),
        "summary_json": compact_sync_run_summary(summary),
    }


def stale_sync_run_cutoff(now, *, environ, stale_after_hours):
    """Runs that started before this instant are known to have exited without finishing.

    The self-hosted supervisor starts one collection worker at a time and only
    after the previous one exited, so for its worker every earlier running row
    is dead: a killed or timed-out worker is closed at the next start instead
    of showing as running for SYNC_RUN_STALE_AFTER_HOURS.
    """
    values = environ
    if values.get("LIC_COLLECTION_SUPERVISED") == "1":
        return now.isoformat()
    return (now - timedelta(hours=stale_after_hours)).isoformat()


def start_sync_run(run_type='auto_sync', limits=None, *, started_at=None, context: CollectionContext):
    now_dt = datetime.now(timezone.utc)
    now = started_at or now_dt.isoformat()
    values = {
        "run_type": run_type,
        "status": "running",
        "started_at": now,
        "summary_json": {
            "summary_schema_version": SYNC_RUN_SUMMARY_SCHEMA_VERSION,
            "limits": limits or {},
        },
    }
    try:
        repository = SyncRunRepository(context.database)
        try:
            recovered = repository.recover_stale_running(
                stale_sync_run_cutoff(now_dt, environ=context.supervised_environ(), stale_after_hours=context.policy.sync_run_stale_after_hours),
                {"finished_at": now},
            )
            if recovered:
                event("sync_run_recovery", "confirmed", count=recovered, reason="stale_running")
                print(f"[sync-runs] recovered stale rows={recovered}", flush=True)
        except Exception as error:
            print(
                source_log_message('sync_recovery_error', lambda: (f"[sync-runs] recovery skipped error_type={type(error).__name__}"), error=error),
                flush=True,
            )
        run_id = repository.start(values)
        event("sync_run_receipt", "started" if run_id else "unconfirmed", sync_run_id=run_id, reason=run_type)
        return run_id
    except Exception as error:
        event("sync_run_receipt", "unknown", reason="start_unconfirmed")
        print(source_log_message('sync_start_error', lambda: (f"[sync-runs] start skipped: {error}"), error=error), flush=True)
        return None


def finish_sync_run(run_id, summary, run_type='auto_sync', *, started_at=None, context: CollectionContext):
    summary.update(sync_completion_fields(summary))
    status = summary["completion_outcome"]
    values = sync_run_counts_from_summary(summary)
    values.update({
        "run_type": run_type,
        "status": status,
        "started_at": summary.get("started_at") or datetime.now(timezone.utc).isoformat(),
        "finished_at": summary.get("finished_at") or datetime.now(timezone.utc).isoformat(),
    })
    try:
        receipt = SyncRunRepository(context.database).finish(run_id, values)
        event("sync_run_receipt", "confirmed" if receipt else "unconfirmed", sync_run_id=run_id, reason=status)
        return receipt
    except Exception as error:
        event("sync_run_receipt", "unknown", sync_run_id=run_id, reason="finish_unconfirmed")
        from hot_runtime import HotDatabaseAdmissionDenied
        if isinstance(error, HotDatabaseAdmissionDenied) and run_id and started_at:
            summary['errors'] = max(int(summary.get('errors') or 0), 1)
            summary['completion_outcome'] = 'error'
            summary['hot_runtime_interrupted'] = True
            try:
                from db.hot.hot_collection_control import HotCollectionControlRepository
                HotCollectionControlRepository(context.database).interrupt_run(run_id, started_at)
            except Exception as control_error:
                print('[sync-runs] interruption receipt unavailable error_type=' + type(control_error).__name__, flush=True)
            # A terminal interruption is not a successful collection finish.
            return False
        print(source_log_message('sync_finish_error', lambda: (f"[sync-runs] finish skipped: {error}"), error=error), flush=True)
        return False


def update_sync_run_progress(run_id, summary, run_type='auto_sync', *, context: CollectionContext):
    if not run_id:
        return False
    values = sync_run_counts_from_summary(summary)
    values.update({
        "run_type": run_type,
        "status": "running",
        "started_at": summary.get("started_at") or datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    })
    try:
        return SyncRunRepository(context.database).update_progress(run_id, values)
    except Exception as error:
        print(source_log_message('sync_progress_error', lambda: (f"[sync-runs] progress skipped: {error}"), error=error), flush=True)
        return False


