"""Read-only persistence boundary for bounded Channel Derived Insights."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Sequence

from hot_storage import window_bounds

from ..base.repository import DatabaseRepository
from psycopg import sql
from ..base.sql_values import projection


INVENTORY_PAGE_SIZE = 1000
SNAPSHOT_PAGE_SIZE = 1000
CURSOR_TIMESTAMP_RE = re.compile(r"^[0-9T:+.Z-]+$")
CURSOR_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _inventory_cursor(row: dict[str, Any]) -> tuple[str | None, str]:
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


def _snapshot_cursor(row: dict[str, Any]) -> tuple[str, int]:
    recorded_at = str(row.get("recorded_at") or "").strip()
    try:
        row_id = int(row.get("id"))
    except (TypeError, ValueError) as error:
        raise RuntimeError("snapshot cursor has an invalid row ID") from error
    if not CURSOR_TIMESTAMP_RE.fullmatch(recorded_at):
        raise RuntimeError("snapshot cursor has an invalid observation timestamp")
    if row_id < 0:
        raise RuntimeError("snapshot cursor has an invalid row ID")
    return recorded_at, row_id


class ChannelDerivedInsightsRepository(DatabaseRepository):
    """Own the fixed query shapes used by snapshot-derived channel insights."""

    def load_channel(self, channel_id: str, *, columns: str) -> dict[str, Any] | None:
        columns = columns if columns == "*" else ",".join(dict.fromkeys([*columns.split(","), "source_expires_at"]))
        row = self.database.read_row(
            "channel_derived_insights_load_channel",
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
                "channel_derived_insights_load_long_inventory", statement, params, max_rows=page_size,
            ) or []
            rows.extend(dict(row) for row in batch)
            if len(batch) < page_size:
                break
            next_cursor = _inventory_cursor(dict(batch[-1]))
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
                "channel_derived_insights_load_videos_by_ids", statement, (batch,), max_rows=len(batch),
            ) or [])
        return self.admit_source_deadlines(rows)

    def load_snapshots_by_video_ids(
        self, video_ids: Sequence[str], *, recorded_from: datetime,
        recorded_through: datetime, limit: int, include_engagement: bool = True,
    ) -> list[dict[str, Any]]:
        if not video_ids or limit <= 0:
            return []
        bounds = window_bounds(now=self.read_now(), recorded_from=recorded_from,
                               recorded_through=recorded_through)
        if bounds is None:
            return []
        recorded_from, recorded_through = bounds
        # One global ordered snapshot stream preserves the shared row budget.
        columns = ("id,video_id,views,like_count,comment_count,recorded_at,source_expires_at"
                   if include_engagement else "id,video_id,views,recorded_at,source_expires_at")
        requested = int(limit)
        rows: list[dict[str, Any]] = []
        cursor_recorded_at: str | None = None
        cursor_id: int | None = None
        ids = list(dict.fromkeys(video_ids))
        while len(rows) < requested:
            page_size = min(SNAPSHOT_PAGE_SIZE, requested - len(rows))
            statement = sql.SQL(
                "SELECT {} FROM readable.video_snapshots WHERE video_id = ANY(%s::text[]) "
                "AND recorded_at >= %s::timestamptz AND recorded_at <= %s::timestamptz"
            ).format(projection(columns))
            params = [ids, _iso(recorded_from), _iso(recorded_through)]
            if cursor_recorded_at is not None and cursor_id is not None:
                statement += sql.SQL(
                    " AND (recorded_at < %s::timestamptz"
                    " OR (recorded_at = %s::timestamptz AND id < %s))"
                )
                params.extend((cursor_recorded_at, cursor_recorded_at, cursor_id))
            statement += sql.SQL(" ORDER BY recorded_at DESC, id DESC LIMIT %s")
            params.append(page_size)
            batch = self.database.read_rows(
                "channel_derived_insights_load_snapshots_by_video_ids", statement, params,
                max_rows=page_size,
            ) or []
            rows.extend(dict(row) for row in batch)
            if len(batch) < page_size:
                break
            next_cursor = _snapshot_cursor(dict(batch[-1]))
            if next_cursor == (cursor_recorded_at, cursor_id):
                raise RuntimeError("snapshot keyset cursor did not advance")
            cursor_recorded_at, cursor_id = next_cursor
        return self.admit_source_deadlines(rows)
