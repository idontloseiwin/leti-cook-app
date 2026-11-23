"""Workspace summary HTTP adapter (Home's latest videos read the shared research backend)."""
from db import FrontendRepository, SyncRunRepository
from fastapi import APIRouter
import ui_read_trace
from source_log import source_log_message
from source_expiry import captured_source_call, stamp_aggregate


def create_home_router(*, cache, library, core, database):
    router = APIRouter()
    cached_read = cache.cached_read
    load_visible_channel_ids = library.load_visible_channel_ids

    @router.get("/explorer/home-summary")
    def explorer_home_summary():
        value = cached_read("explorer-home-summary", 20, load_explorer_home_summary)
        if value["saved_videos"] is None:
            ui_read_trace.unavailable("inventory_counts")
        return value


    def load_explorer_home_summary():
        with ui_read_trace.scope("visible_channels"):
            visible_channel_ids = load_visible_channel_ids()
        saved_videos = None
        # A turned-off format is neither counted nor read:
        # its count is 0 and turned_off names it; absent while all are on.
        from collection_formats import disabled_formats, shown_formats
        turned_off = disabled_formats(shown_formats())
        try:
            with ui_read_trace.scope("inventory_counts"):
                counted = tuple(kind for kind in (core.VIDEO_TYPE_LONG, core.VIDEO_TYPE_SHORT, core.VIDEO_TYPE_STREAM)
                                if kind not in turned_off)
                inventory_counts, count_expiry = captured_source_call(
                    FrontendRepository(database).count_inventory_by_type,
                    visible_channel_ids,
                    counted,
                )
                inventory_counts = {**dict.fromkeys(turned_off, 0), **inventory_counts}
                saved_videos = stamp_aggregate({
                    "total": sum(inventory_counts.values()),
                    "long": inventory_counts[core.VIDEO_TYPE_LONG],
                    "short": inventory_counts[core.VIDEO_TYPE_SHORT],
                    "stream": inventory_counts[core.VIDEO_TYPE_STREAM],
                    **({"turned_off": list(turned_off)} if turned_off else {}),
                }, count_expiry)
        except Exception as error:
            ui_read_trace.failure("inventory_counts", error)
            print(
                source_log_message('home_typed_inventory_error', lambda: ("[explorer-home-summary] typed inventory counts skipped: "
                + error.__class__.__name__), error=error),
                flush=True,
            )
        last_successful_sync_at = None
        try:
            with ui_read_trace.scope("latest_completed_sync"):
                successful_run = SyncRunRepository(database).latest_completed()
            if successful_run:
                last_successful_sync_at = (
                    successful_run.get("finished_at") or successful_run.get("started_at")
                )
        except Exception as error:
            ui_read_trace.failure("latest_completed_sync", error)
            print(
                source_log_message('home_sync_run_error', lambda: ("[explorer-home-summary] successful sync run skipped: "
                + error.__class__.__name__), error=error),
                flush=True,
            )

        # "Sync N ago": a partial run without errors is normal work.
        last_sync_without_errors_at = None
        try:
            with ui_read_trace.scope("latest_clean_sync"):
                clean_run = SyncRunRepository(database).latest_without_errors()
            if clean_run:
                last_sync_without_errors_at = clean_run.get("finished_at") or clean_run.get("started_at")
        except Exception as error:
            ui_read_trace.failure("latest_clean_sync", error)
            print(
                source_log_message('home_clean_run_error', lambda: ("[explorer-home-summary] run without errors skipped: "
                + error.__class__.__name__), error=error),
                flush=True,
            )

        last_data_change_at = None
        try:
            with ui_read_trace.scope("latest_data_change"):
                change_run = SyncRunRepository(database).latest_completed_with_changes()
            if change_run:
                last_data_change_at = (
                    change_run.get("finished_at") or change_run.get("started_at")
                )
        except Exception as error:
            ui_read_trace.failure("latest_data_change", error)
            print(
                source_log_message('home_change_run_error', lambda: ("[explorer-home-summary] data-change run skipped: "
                + error.__class__.__name__), error=error),
                flush=True,
            )
        return {
            "contract_version": 1,
            # The last full pass; kept with its meaning for existing readers.
            "last_successful_sync_at": last_successful_sync_at,
            "last_sync_without_errors_at": last_sync_without_errors_at,
            "last_data_change_at": last_data_change_at,
            "saved_videos": saved_videos,
            "source": {
                "kind": "stored_typed_video_inventory",
                "format_source": "video_inventory.video_type",
                "scope": "visible_channels",
                "youtube_requests": False,
                "database_writes": False,
            },
        }


    return router
