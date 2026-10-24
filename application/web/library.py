"""Library HTTP reads through existing repository owners."""
from db.user_work.channel_labels import ChannelLabelRepository
from db import FrontendRepository
from fastapi import HTTPException, APIRouter
from .constants import VISIBLE_CHANNEL_COLUMNS
from typing import Callable
from dataclasses import dataclass

@dataclass(frozen=True)
class LibraryHttp:
    router: APIRouter
    load_folders: Callable
    load_visible_channel_ids: Callable
    load_visible_channel_or_404: Callable
    require_visible_channel_user_work: Callable

def create_library_http(*, cache, database):
    router = APIRouter()
    cached_read = cache.cached_read

    def load_visible_channel_ids():
        rows = FrontendRepository(database).load_visible_channel_user_work()
        return {
            row["channel_id"]
            for row in rows
            if row.get("channel_id") and not row.get("is_hidden")
        }


    def load_visible_channels():
        rows = FrontendRepository(database).load_channel_rows(VISIBLE_CHANNEL_COLUMNS)
        visible = [
            row for row in rows
            if row.get("channel_id") and not row.get("is_hidden")
        ]
        return sorted(
            visible,
            key=lambda row: (
                str(row.get("title") or "").lower(),
                str(row.get("channel_id") or ""),
            ),
        )


    def load_folders():
        rows = FrontendRepository(database).load_folder_rows()
        return sorted(
            rows,
            key=lambda row: (
                str(row.get("name") or "").lower(),
                str(row.get("id") or ""),
            ),
        )


    def load_channel_label_data():
        repository = ChannelLabelRepository(database)
        labels = [dict(row) for row in repository.load_labels()]
        assignments = {}
        for row in repository.load_assignments():
            channel_id = str(row.get("channel_id") or "")
            label_key = str(row.get("label_key") or "")
            if channel_id and label_key:
                assignments.setdefault(channel_id, []).append(label_key)
        for keys in assignments.values():
            keys.sort()
        return labels, assignments


    def load_sidebar_data():
        channels = load_visible_channels()
        labels, assignments = load_channel_label_data()
        for channel in channels:
            channel["label_keys"] = assignments.get(channel.get("channel_id"), [])
        return {
            "channels": channels,
            "folders": load_folders(),
            "labels": labels,
        }


    def load_sidebar_user_data():
        labels, assignments = load_channel_label_data()
        channels = [
            {"channel_id": row["channel_id"], "folder_id": row.get("folder_id"), "added_at": row.get("added_at"),
             "label_keys": assignments.get(row["channel_id"], [])}
            for row in FrontendRepository(database).load_visible_channel_user_work()
        ]
        return {"channels": channels, "folders": load_folders(), "labels": labels}

    def load_video_snapshots(video_id, *, video_type, order_desc=False, limit=None):
        repository = FrontendRepository(database)
        membership = repository.load_inventory_membership(
            video_id,
            video_type=video_type,
        )
        if not membership or not membership.get("channel_id"):
            raise HTTPException(status_code=404, detail="Video not found")
        load_visible_channel_or_404(str(membership["channel_id"]))
        return repository.load_video_snapshots(
            video_id,
            order_desc=order_desc,
            limit=limit,
        )


    def require_visible_channel_user_work(channel_id):
        row = FrontendRepository(database).load_visible_channel_user_work_identity(channel_id)
        if not row:
            raise HTTPException(status_code=404, detail="Channel not found")

    def load_visible_channel_or_404(channel_id):
        channel_rows = FrontendRepository(database).load_channel(channel_id)
        if not channel_rows or channel_rows[0].get("is_hidden"):
            raise HTTPException(status_code=404, detail="Channel not found")
        return channel_rows[0]


    @router.get("/sidebar-user-data")
    def sidebar_user_data():
        return cached_read("sidebar-user-data", 20, load_sidebar_user_data)

    @router.get("/sidebar-data")
    def sidebar_data():
        return cached_read("sidebar-data", 20, load_sidebar_data)


    return LibraryHttp(router, load_folders, load_visible_channel_ids, load_visible_channel_or_404, require_visible_channel_user_work)
