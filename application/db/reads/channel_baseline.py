"""Read-only persistence boundary for long-form Channel Baseline Analytics."""

from __future__ import annotations

import re
from typing import Any, Sequence

from ..base.repository import DatabaseRepository
from psycopg import sql
from ..base.sql_values import projection
from .channel_explorer import ChannelExplorerRepository


INVENTORY_PAGE_SIZE = 1000
CURSOR_TIMESTAMP_RE = re.compile(r"^[0-9T:+.Z-]+$")
CURSOR_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _cursor_values(row: dict[str, Any]) -> tuple[str | None, str]:
    published_at = row.get("published_at")
    published = str(published_at).strip() if published_at is not None else None
    video_id = str(row.get("video_id") or "").strip()
    if published == "":
        published = None
    if published is not None and not CURSOR_TIMESTAMP_RE.fullmatch(published):
        raise RuntimeError("inventory cursor has an invalid publication timestamp")
    if not CURSOR_VIDEO_ID_RE.fullmatch(video_id):
        raise RuntimeError("inventory cursor has an invalid video ID")
    return published, video_id


class ChannelBaselineRepository(DatabaseRepository):
    """Own the fixed, bounded query shapes used by Baseline Analytics."""

    def iter_long_inventory_pages(self, channel_id: str):
        return ChannelExplorerRepository(self.database).iter_long_inventory_pages(channel_id)

    def load_channel(self, channel_id: str, *, columns: str) -> dict[str, Any] | None:
        columns = columns if columns == "*" else ",".join(dict.fromkeys([*columns.split(","), "source_expires_at"]))
        row = self.database.read_row(
            "channel_baseline_load_channel",
            sql.SQL("SELECT {} FROM readable.channels_with_control WHERE channel_id = %s LIMIT 1").format(projection(columns)),
            (channel_id,),
        )
        return self.admit_source_deadlines([dict(row)])[0] if row else None

    def load_long_inventory(self, channel_id: str, *, limit: int) -> list[dict[str, Any]]:
        requested = max(0, int(limit))
        rows: list[dict[str, Any]] = []
        cursor_published: str | None = None
        cursor_video_id: str | None = None
        while len(rows) < requested:
            page_size = min(INVENTORY_PAGE_SIZE, requested - len(rows))
            statement = (
                "SELECT video_id,published_at,source_checked_at,source_expires_at FROM readable.hot_video_inventory "
                "WHERE channel_id = %s AND video_type = %s"
            )
            params = [channel_id, "long"]
            if cursor_video_id is not None:
                if cursor_published is None:
                    statement += " AND published_at IS NULL AND video_id > %s"
                    params.append(cursor_video_id)
                else:
                    statement += (
                        " AND (published_at < %s::timestamptz"
                        " OR (published_at = %s::timestamptz AND video_id > %s)"
                        " OR published_at IS NULL)"
                    )
                    params.extend((cursor_published, cursor_published, cursor_video_id))
            statement += " ORDER BY published_at DESC NULLS LAST, video_id ASC LIMIT %s"
            params.append(page_size)
            batch = self.database.read_rows(
                "channel_baseline_load_long_inventory", statement, params, max_rows=page_size,
            ) or []
            rows.extend(dict(row) for row in batch)
            if len(batch) < page_size:
                break
            next_cursor = _cursor_values(dict(batch[-1]))
            if next_cursor == (cursor_published, cursor_video_id):
                raise RuntimeError("inventory keyset cursor did not advance")
            cursor_published, cursor_video_id = next_cursor
        return self.admit_source_deadlines(rows)

    def load_videos_by_ids(self, video_ids: Sequence[str], *, columns: str) -> list[dict[str, Any]]:
        columns = columns if columns == "*" else ",".join(dict.fromkeys([*columns.split(","), "source_expires_at"]))
        rows = []
        ids = list(dict.fromkeys(video_ids))
        if not ids:
            return []
        statement = sql.SQL("SELECT {} FROM readable.videos WHERE video_id = ANY(%s::text[])").format(projection(columns))
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            rows.extend(self.database.read_rows(
                "channel_baseline_load_videos_by_ids", statement, (batch,), max_rows=len(batch),
            ) or [])
        return self.admit_source_deadlines(rows)
