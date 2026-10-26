"""Read-only persistence boundary for long-form Channel Baseline Analytics."""

from __future__ import annotations

from typing import Any, Sequence

from ..base.repository import DatabaseRepository
from psycopg import sql
from ..base.sql_values import projection
from .channel_explorer import ChannelExplorerRepository


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
        if requested == 0:
            return []
        # One statement: the protected view cannot be read in pages by its order
        # key, so every 1000-row page rebuilt the channel's whole inventory. One
        # read is also one snapshot of the catalogue.
        batch = self.database.read_rows(
            "channel_baseline_load_long_inventory",
            "SELECT video_id,published_at,source_checked_at,source_expires_at FROM readable.hot_video_inventory "
            "WHERE channel_id = %s AND video_type = %s ORDER BY published_at DESC NULLS LAST, video_id ASC LIMIT %s",
            [channel_id, "long", requested], max_rows=requested,
        ) or []
        return self.admit_source_deadlines([dict(row) for row in batch])

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
