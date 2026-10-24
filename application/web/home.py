"""Workspace summary and recent-video HTTP adapters."""
from db import FrontendRepository, SyncRunRepository
from fastapi import HTTPException, APIRouter
from datetime import datetime, timezone
import re
import ui_read_trace
from source_log import source_log_message
from video_thumbnails import video_thumbnail_url
from source_expiry import captured_source_call, minimum_expiry, source_expiry, stamp_aggregate
from .constants import (
    EXPLORER_RECENT_VIDEO_PAGE_SIZE,
    EXPLORER_RECENT_VIDEO_SCAN_SIZE,
    EXPLORER_RECENT_VIDEO_WINDOWS,
    FRONTEND_VIDEO_COLUMNS,
)


def create_home_router(*, cache, library, core, database):
    def utc_dt(value):
        parsed = core.parse_dt(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


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
        try:
            with ui_read_trace.scope("inventory_counts"):
                inventory_counts, count_expiry = captured_source_call(
                    FrontendRepository(database).count_inventory_by_type,
                    visible_channel_ids,
                    (
                        core.VIDEO_TYPE_LONG,
                        core.VIDEO_TYPE_SHORT,
                        core.VIDEO_TYPE_STREAM,
                    ),
                )
                saved_videos = stamp_aggregate({
                    "total": sum(inventory_counts.values()),
                    "long": inventory_counts[core.VIDEO_TYPE_LONG],
                    "short": inventory_counts[core.VIDEO_TYPE_SHORT],
                    "stream": inventory_counts[core.VIDEO_TYPE_STREAM],
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
            "last_successful_sync_at": last_successful_sync_at,
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


    def parse_explorer_recent_video_cursor(value):
        if value is None:
            return None
        raw = str(value).strip()
        try:
            published_at, video_id = raw.rsplit("|", 1)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid recent-video cursor") from error
        published_dt = utc_dt(published_at)
        if not published_dt or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", video_id):
            raise HTTPException(status_code=422, detail="Invalid recent-video cursor")
        return published_dt, video_id


    def explorer_recent_video_cursor(video):
        return f"{video['published_at']}|{video['video_id']}"


    def load_explorer_recent_videos(window="24h", cursor=None, now=None):
        if window not in EXPLORER_RECENT_VIDEO_WINDOWS:
            raise HTTPException(status_code=422, detail="window must be 24h, 7d, or 28d")
        cursor_key = parse_explorer_recent_video_cursor(cursor)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        published_from = now - EXPLORER_RECENT_VIDEO_WINDOWS[window]
        with ui_read_trace.scope("visible_channels"):
            visible_channel_ids = set(load_visible_channel_ids())
        repository = FrontendRepository(database)
        selected = []
        offset = 0
        scanned = pages = candidate_count = 0

        while len(selected) <= EXPLORER_RECENT_VIDEO_PAGE_SIZE:
            inventory_rows, fetched = repository.load_recent_long_inventory_page(
                published_from=published_from.isoformat(),
                published_through=cursor_key[0].isoformat() if cursor_key else None,
                offset=offset,
                page_size=EXPLORER_RECENT_VIDEO_SCAN_SIZE,
            )
            pages += 1
            scanned += fetched
            candidates = []
            for inventory in inventory_rows:
                video_id = str(inventory.get("video_id") or "").strip()
                channel_id = str(inventory.get("channel_id") or "").strip()
                published_dt = utc_dt(inventory.get("published_at"))
                if not video_id or channel_id not in visible_channel_ids or not published_dt:
                    continue
                candidate_key = (published_dt, video_id)
                if cursor_key and candidate_key >= cursor_key:
                    continue
                candidates.append((candidate_key, inventory))

            candidates.sort(key=lambda entry: entry[0], reverse=True)
            candidate_count += len(candidates)
            if candidates:
                video_rows = repository.load_videos_by_ids(
                    [entry[1]["video_id"] for entry in candidates],
                    columns=FRONTEND_VIDEO_COLUMNS,
                )
                videos_by_id = {
                    str(video.get("video_id")): video
                    for video in video_rows
                    if video.get("video_id")
                }
                for (published_dt, video_id), inventory in candidates:
                    video = videos_by_id.get(video_id)
                    if not video:
                        continue
                    selected.append({
                        "video_id": video_id,
                        "channel_id": inventory.get("channel_id"),
                        "title": video.get("title"),
                        "thumbnail_url": video_thumbnail_url(video.get("video_id")),
                        "published_at": published_dt.isoformat(),
                        "checked_at": video.get("checked_at"),
                        "views": video.get("views"),
                        "like_count": video.get("like_count"),
                        "comment_count": video.get("comment_count"),
                        "source_expires_at": minimum_expiry(
                            source_expiry(video, "checked_at", "metadata_checked_at", "availability_checked_at"),
                            source_expiry(inventory, "source_checked_at"),
                        ),
                    })
                    if len(selected) > EXPLORER_RECENT_VIDEO_PAGE_SIZE:
                        break

            if len(selected) > EXPLORER_RECENT_VIDEO_PAGE_SIZE:
                break
            if fetched < EXPLORER_RECENT_VIDEO_SCAN_SIZE:
                break
            offset += EXPLORER_RECENT_VIDEO_SCAN_SIZE

        has_more = len(selected) > EXPLORER_RECENT_VIDEO_PAGE_SIZE
        items = selected[:EXPLORER_RECENT_VIDEO_PAGE_SIZE]
        for operation, count in (
            ("recent_inventory_pages", pages), ("recent_inventory_scan", scanned),
            ("recent_inventory_candidates", candidate_count), ("recent_videos_result", len(items)),
        ):
            ui_read_trace.event(operation, phase="scan", status="ok", count=count)
        return {
            "contract_version": 1,
            "window": window,
            "generated_at": now.isoformat(),
            "published_from": published_from.isoformat(),
            "items": items,
            "has_more": has_more,
            "next_cursor": explorer_recent_video_cursor(items[-1]) if has_more else None,
            "source": {
                "kind": "stored_recent_long_form_videos",
                "format_source": "video_inventory.video_type",
                "scope": "visible_channels",
                "youtube_requests": False,
                "database_writes": False,
            },
        }


    @router.get("/explorer/recent-videos")
    def explorer_recent_videos(window: str = "24h", cursor: str | None = None):
        if cursor:
            return load_explorer_recent_videos(window=window, cursor=cursor)
        return cached_read(
            f"explorer-recent-videos:{window}:first",
            10,
            lambda: load_explorer_recent_videos(window=window),
        )


    return router
