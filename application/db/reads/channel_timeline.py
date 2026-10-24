"""Read-only persistence boundary for stored channel-counter timelines."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .analytics_read_queries import read_channel_timeline_series

from hot_storage import iso_utc, source_is_current, window_bounds

from ..base.repository import DatabaseRepository
from psycopg import sql
from ..base.sql_values import identifier


TITLE_BATCH_SIZE = 500
CHANNEL_TIMELINE_METRICS = frozenset({"views", "subscribers"})

class ChannelTimelineRepository(DatabaseRepository):
    """Own fixed and bounded query shapes used by the timeline contract."""

    def load_channel(self, channel_id: str) -> dict[str, Any] | None:
        row = self.database.read_row(
            "channel_timeline_load_channel",
            "SELECT channel_id,is_hidden FROM readable.channels_with_control WHERE channel_id = %s LIMIT 1",
            (channel_id,),
        )
        return dict(row) if row else None

    def load_snapshot_edge_candidates(
        self, channel_id: str, *, metric: str, at_or_before: str,
        descending: bool, limit: int,
    ) -> list[dict[str, Any]]:
        if metric not in CHANNEL_TIMELINE_METRICS:
            raise ValueError("unsupported channel timeline metric")
        bounds = window_bounds(now=self.read_now(), recorded_through=at_or_before)
        if bounds is None:
            return []
        start, end = bounds
        requested = max(1, int(limit))
        rows = self.database.read_rows(
            "channel_timeline_load_snapshot_edge_candidates",
            sql.SQL(
                "SELECT recorded_at,views,subscribers,source_expires_at FROM readable.channel_snapshots "
                "WHERE channel_id = %s AND recorded_at >= %s::timestamptz AND recorded_at <= %s::timestamptz "
                "AND {} IS NOT NULL ORDER BY recorded_at {} LIMIT %s"
            ).format(identifier(metric), sql.SQL("DESC" if descending else "ASC")),
            (channel_id, iso_utc(start), iso_utc(end), requested), max_rows=requested,
        ) or []
        return self.admit_source_deadlines([dict(row) for row in rows if source_is_current(row.get("recorded_at"), now=self.read_now())])

    def load_snapshot_window(
        self,
        channel_id: str,
        *,
        metric: str,
        recorded_from: str | None,
        recorded_through: str,
        bucket_seconds: int,
        scan_limit: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        """Read one bounded, database-bucketed metric series."""

        if metric not in CHANNEL_TIMELINE_METRICS:
            raise ValueError("unsupported channel timeline metric")
        bounds = window_bounds(
            now=self.read_now(),
            recorded_from=recorded_from,
            recorded_through=recorded_through,
        )
        if bounds is None:
            return [], False, 0
        start, end = bounds
        requested = max(1, int(limit))
        data = read_channel_timeline_series(
            self.database, channel_id=channel_id, metric=metric,
            recorded_from=iso_utc(start), recorded_through=iso_utc(end),
            bucket_seconds=max(300, int(bucket_seconds)),
            scan_limit=max(1, int(scan_limit)), return_limit=requested,
        )
        rows: list[dict[str, Any]] = []
        scan_truncated = False
        snapshot_count = 0
        for raw_row in data or []:
            if not source_is_current(raw_row.get("recorded_at"), now=self.read_now()):
                continue
            row = dict(raw_row)
            scan_truncated = scan_truncated or bool(row.pop("scan_truncated", False))
            try:
                snapshot_count = max(
                    snapshot_count,
                    int(row.pop("snapshot_count", 0) or 0),
                )
            except (TypeError, ValueError):
                pass
            rows.append(row)
        return rows, scan_truncated, snapshot_count

    def load_publication_events(
        self, channel_id: str, *, published_from: str | None,
        published_through: str, limit: int,
    ) -> list[dict[str, Any]]:
        statement = (
            "SELECT video_id,published_at,source_checked_at,source_expires_at FROM readable.hot_video_inventory "
            "WHERE channel_id = %s AND video_type = %s AND published_at <= %s::timestamptz"
        )
        params = [channel_id, "long", published_through]
        if published_from is not None:
            statement += " AND published_at >= %s::timestamptz"
            params.append(published_from)
        requested = max(1, int(limit))
        statement += " ORDER BY published_at DESC NULLS FIRST, video_id ASC LIMIT %s"
        params.append(requested)
        rows = self.database.read_rows(
            "channel_timeline_load_publication_events", statement, params, max_rows=requested,
        ) or []
        return list(reversed([
            {"video_id": row["video_id"], "published_at": row.get("published_at"),
             "source_expires_at": row.get("source_expires_at")}
            for row in self.admit_source_deadlines(rows) if source_is_current(row.get("source_checked_at"), now=self.read_now())
        ]))

    def load_video_titles(self, video_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        clean_ids = list(dict.fromkeys(
            str(value).strip() for value in video_ids if value is not None and str(value).strip()
        ))
        titles: dict[str, dict[str, Any]] = {}
        for start in range(0, len(clean_ids), TITLE_BATCH_SIZE):
            batch = clean_ids[start:start + TITLE_BATCH_SIZE]
            rows = self.database.read_rows(
                "channel_timeline_load_video_titles",
                "SELECT video_id,title,metadata_checked_at,availability_status,availability_checked_at,"
                "least(metadata_checked_at,availability_checked_at)+interval '696 hours' AS source_expires_at FROM readable.videos "
                "WHERE video_id = ANY(%s::text[])",
                (batch,), max_rows=len(batch),
            ) or []
            for row in rows:
                video_id = str(row.get("video_id") or "").strip()
                if not video_id:
                    continue
                if (row.get("availability_status") not in {"public"}
                        or not source_is_current(row.get("metadata_checked_at"), now=self.read_now())
                        or not source_is_current(row.get("availability_checked_at"), now=self.read_now())):
                    continue
                self.admit_source_deadlines([row])
                title = row.get("title")
                titles[video_id] = {"title": str(title) if title is not None else None,
                                    "source_expires_at": row.get("source_expires_at")}
        return titles
