"""Database access used to assemble backend HTTP read models."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from hot_storage import iso_utc, window_bounds, source_is_current, HotSourceReadExpired
from hot_storage_fields import channel_source_fields, video_source_fields

from ..base.repository import DatabaseRepository
from ..channel.channel_discovery_projection import CHANNEL_READ_RELATION
from psycopg import sql
from ..base.sql_values import projection
from ..base.client import execute_recoverable_read


# The schema enforces user-work limits; overflow raises instead of truncating.
CHANNEL_ROW_LIMIT = 10_000
FOLDER_ROW_LIMIT = 1000
# Technical safety bound for formerly unbounded per-video snapshot reads.
SNAPSHOT_READ_LIMIT = 100_000
VIDEO_ID_BATCH_SIZE = 500

VIDEO_TYPES = frozenset({"long", "short", "stream"})


def _validated_video_type(value: str) -> str:
    video_type = str(value).strip().lower()
    if video_type not in VIDEO_TYPES:
        raise ValueError("unsupported video type")
    return video_type


class FrontendRepository(DatabaseRepository):
    """Own the catalog queries needed by the installed frontend and API."""

    def count_inventory_by_type(
        self, channel_ids: Iterable[str], video_types: Sequence[str],
    ) -> dict[str, int]:
        """Count every requested type and its oldest source in one snapshot."""
        unique_channel_ids = sorted({str(channel_id) for channel_id in channel_ids if channel_id})
        unique_video_types = list(dict.fromkeys(str(video_type) for video_type in video_types if video_type))
        counts = {video_type: 0 for video_type in unique_video_types}
        if not unique_channel_ids or not unique_video_types:
            return counts
        rows = self.database.read_rows(
            "frontend_count_inventory_by_type",
            "SELECT video_type,count(*) AS count,min(source_checked_at) AS source_checked_at,"
            "min(source_expires_at) AS source_expires_at FROM readable.hot_video_inventory "
            "WHERE channel_id = ANY(%s::text[]) AND video_type = ANY(%s::text[]) GROUP BY video_type",
            (unique_channel_ids, unique_video_types), max_rows=len(unique_video_types),
        )
        if rows is None:
            raise RuntimeError("Exact typed inventory count is unavailable")
        for row in rows:
            exact_count = row.get("count")
            self._count_source_is_current([row], exact_count)
            if exact_count is None:
                raise RuntimeError("Exact typed inventory count is unavailable")
            counts[row["video_type"]] = int(exact_count)
        return counts

    def load_channel_rows(self, columns: str) -> list[dict[str, Any]]:
        if columns != "*":
            fields = [*columns.split(","), "source_expires_at"]
            columns = ",".join(dict.fromkeys(fields))
            provider_fields = {
                "title", "views", "subscribers", "avatar_url", "video_count",
                "country_code", "long_video_count", "uploads_playlist_id", "channel_created_at",
            }
            if provider_fields.intersection(fields):
                extra = ["channel_metadata_status", "channel_metadata_checked_at"]
                if "country_code" in fields:
                    extra.append("country_collected_at")
                if "long_video_count" in fields:
                    extra.append("initial_inventory_estimate")
                columns = ",".join(dict.fromkeys([*fields, *extra]))
        rows = execute_recoverable_read(
            self.database,
            lambda database: database.read_rows(
                "frontend_load_channel_rows",
                sql.SQL("SELECT {} FROM {}").format(projection(columns), sql.SQL(CHANNEL_READ_RELATION)),
                max_rows=CHANNEL_ROW_LIMIT,
            ) or [],
            operation="frontend.load_channel_rows",
        )
        return [channel_source_fields(row, now=self.read_now()) for row in self.admit_source_deadlines(rows)]

    def load_visible_channel_user_work(self) -> list[dict[str, Any]]:
        """Local identities, tracking dates and folders; no API clock or payload."""
        return self.database.read_rows(
            "frontend_load_visible_channel_user_work",
            "SELECT channel_id,folder_id,added_at FROM readable.channel_user_work "
            "WHERE NOT is_hidden ORDER BY channel_id",
            max_rows=CHANNEL_ROW_LIMIT,
        ) or []

    def load_visible_channel_user_work_identity(self, channel_id: str) -> dict | None:
        return self.database.read_row(
            "frontend_load_visible_channel_user_work_identity",
            "SELECT channel_id FROM readable.channel_user_work WHERE channel_id=%s AND NOT is_hidden",
            (channel_id,),
        )

    def load_channel_id_for_video(self, video_id: str) -> str | None:
        """Return a stored channel mapping without starting provider work."""
        row = self.database.read_row(
            "frontend_load_channel_id_for_video",
            "SELECT channel_id,source_checked_at,source_expires_at FROM readable.hot_video_inventory WHERE video_id = %s LIMIT 1",
            (video_id,),
        )
        admitted = self._admitted_inventory([row] if row else [])
        return str(admitted[0]["channel_id"]) if admitted and admitted[0].get("channel_id") else None

    def load_folder_rows(self) -> list[dict[str, Any]]:
        return self.database.read_rows(
            "frontend_load_folder_rows", "SELECT id,name FROM public.folders",
            max_rows=FOLDER_ROW_LIMIT,
        ) or []

    def _hot_snapshot_query(self, *, columns: str):
        bounds = window_bounds(now=self.read_now())
        assert bounds is not None
        lower, upper = bounds
        return (
            sql.SQL("SELECT {} FROM readable.video_snapshots "
                    "WHERE recorded_at >= %s::timestamptz AND recorded_at <= %s::timestamptz").format(projection(columns)),
            [iso_utc(lower), iso_utc(upper)],
        )

    def _count_source_is_current(self, rows, count):
        if count is not None and int(count) > 0 and not self._admitted_inventory(rows):
            raise HotSourceReadExpired("Inventory count source expired during read")

    def _admitted_inventory(self, rows):
        now = self.read_now()
        return self.admit_source_deadlines([row for row in rows if source_is_current(row.get("source_checked_at"), now=now)])

    def _admitted_snapshots(self, rows):
        now = self.read_now()
        return self.admit_source_deadlines([row for row in rows if source_is_current(row.get("recorded_at"), now=now)])

    def load_video_snapshots(
        self, video_id: str, *, order_desc: bool = False, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        statement, params = self._hot_snapshot_query(columns="*")
        statement += sql.SQL(" AND video_id = %s ORDER BY recorded_at {}").format(sql.SQL("DESC" if order_desc else "ASC"))
        params.append(video_id)
        max_rows = SNAPSHOT_READ_LIMIT
        if limit is not None:
            # The previous inclusive range(0, max(0, limit-1)) read at least one row.
            requested = max(1, int(limit))
            statement += sql.SQL(" LIMIT %s")
            params.append(requested)
            max_rows = min(requested, SNAPSHOT_READ_LIMIT)
        return self._admitted_snapshots(self.database.read_rows(
            "frontend_load_video_snapshots", statement, params, max_rows=max_rows,
        ) or [])

    def load_inventory_membership(
        self, video_id: str, *, video_type: str,
    ) -> dict[str, Any] | None:
        row = self.database.read_row(
            "frontend_load_inventory_membership",
            "SELECT video_id,channel_id,video_type,source_checked_at,source_expires_at FROM readable.hot_video_inventory "
            "WHERE video_id = %s AND video_type = %s ORDER BY source_checked_at ASC LIMIT 1",
            (video_id, _validated_video_type(video_type)),
        )
        admitted = self._admitted_inventory([row] if row else [])
        return dict(admitted[0]) if admitted else None

    def load_channel(self, channel_id: str) -> list[dict[str, Any]]:
        rows = execute_recoverable_read(
            self.database,
            lambda database: database.read_rows(
                "frontend_load_channel",
                "SELECT * FROM " + CHANNEL_READ_RELATION + " WHERE channel_id = %s",
                (channel_id,), max_rows=1,
            ) or [],
            operation="frontend.load_channel",
        )
        return [channel_source_fields(row, now=self.read_now()) for row in self.admit_source_deadlines(rows)]

    def load_videos_by_ids(
        self, video_ids: Sequence[str], *, columns: str,
    ) -> list[dict[str, Any]]:
        if columns != "*":
            columns = ",".join(dict.fromkeys([*columns.split(","), "checked_at", "metadata_checked_at", "availability_status", "availability_checked_at", "source_expires_at"]))
        rows = []
        ids = list(dict.fromkeys(video_ids))
        statement = sql.SQL("SELECT {} FROM readable.videos WHERE video_id = ANY(%s::text[])").format(projection(columns))
        for start in range(0, len(ids), VIDEO_ID_BATCH_SIZE):
            batch = ids[start:start + VIDEO_ID_BATCH_SIZE]
            rows.extend(self.database.read_rows(
                "frontend_load_videos_by_ids", statement, (batch,), max_rows=len(batch),
            ) or [])
        now = self.read_now()
        return self.admit_source_deadlines([video_source_fields(row, now=now) for row in rows
                if source_is_current(row.get("checked_at"), now=now)
                and source_is_current(row.get("metadata_checked_at"), now=now)
                and row.get("availability_status") == "public"
                and source_is_current(row.get("availability_checked_at"), now=now)])

    def load_channel_inventory_page(
        self, channel_id: str, *, video_type: str, offset: int, page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Current rows plus the raw page count, including rows expired in transit."""
        rows = self.database.read_rows(
            "frontend_load_channel_inventory_page",
            "SELECT video_id,video_type,published_at,source_checked_at,source_expires_at FROM readable.hot_video_inventory "
            "WHERE channel_id = %s AND video_type = %s "
            "ORDER BY published_at DESC NULLS FIRST, video_id DESC LIMIT %s OFFSET %s",
            (channel_id, _validated_video_type(video_type), page_size, offset), max_rows=page_size,
        ) or []
        return self._admitted_inventory(rows), len(rows)

    def load_recent_long_inventory_page(
        self, *, published_from: str, published_through: str | None,
        offset: int, page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Current rows of one page, and how many rows the page read."""
        statement = (
            "SELECT video_id,channel_id,video_type,published_at,source_checked_at,source_expires_at "
            "FROM readable.hot_video_inventory WHERE video_type = %s AND published_at >= %s::timestamptz"
        )
        params = ["long", published_from]
        if published_through:
            statement += " AND published_at <= %s::timestamptz"
            params.append(published_through)
        statement += " ORDER BY published_at DESC NULLS FIRST, video_id DESC LIMIT %s OFFSET %s"
        params.extend((page_size, offset))
        rows = execute_recoverable_read(
            self.database,
            lambda database: database.read_rows(
                "frontend_load_recent_long_inventory_page", statement, params, max_rows=page_size,
            ) or [],
            operation="frontend.load_recent_long_inventory_page",
        )
        return self._admitted_inventory(rows), len(rows)

    def load_channel_backfill_page(
        self, channel_id: str, *, video_type: str, offset: int, page_size: int,
    ) -> list[dict[str, Any]]:
        return self.database.read_rows(
            "frontend_load_channel_backfill_page",
            "SELECT video_id,published_at FROM public.video_inventory WHERE channel_id = %s AND video_type = %s "
            "ORDER BY published_at DESC NULLS FIRST, video_id DESC LIMIT %s OFFSET %s",
            (channel_id, _validated_video_type(video_type), page_size, offset), max_rows=page_size,
        ) or []
