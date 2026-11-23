"""Database access used to assemble backend HTTP read models."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from hot_storage import iso_utc, window_bounds, source_is_current, HotSourceReadExpired
from hot_storage_fields import channel_source_fields

from ..base.repository import DatabaseRepository
from ..channel.channel_discovery_projection import CHANNEL_READ_RELATION
from psycopg import sql
from ..base.sql_values import projection
from ..base.client import execute_recoverable_read
from ..base.read_context import http_read_scope


# The schema enforces user-work limits; overflow raises instead of truncating.
CHANNEL_ROW_LIMIT = 10_000
FOLDER_ROW_LIMIT = 1000
# Technical safety bound for per-video snapshot reads.
SNAPSHOT_READ_LIMIT = 100_000

VIDEO_TYPES = frozenset({"long", "short", "stream"})

# Home's typed counts read the catalogue read model, as the
# research reads do: a row whose proof is current gives its stored
# type, clock and deadline; a row queued after a write or past its deadline is
# checked live in readable.hot_video_inventory, one video at a time. Every
# inventory row has its read-model row (insert trigger),
# so the counts and earliest clocks are those of the live view, without
# rebuilding type resolution over every membership. The read model's own
# columns come from readable.video_search_projection_types:
# the rows and proof of readable.video_search_projection without its join
# with readable.videos. Both are granted to the HTTP reader only.
INVENTORY_COUNT_SQL = """WITH p AS MATERIALIZED (
 SELECT p.video_id,p.channel_id,p.proof_current,p.inventory_type,p.hot_present,p.identity_clock,p.identity_expiry
 FROM readable.video_search_projection_types p WHERE p.channel_id=ANY(%(channels)s::text[]))
SELECT video_type,count(*) AS count,min(source_checked_at) AS source_checked_at,min(source_expires_at) AS source_expires_at FROM (
 SELECT p.inventory_type AS video_type,p.identity_clock AS source_checked_at,p.identity_expiry AS source_expires_at
 FROM p WHERE p.proof_current AND p.hot_present AND p.inventory_type=ANY(%(types)s::text[])
 UNION ALL
 SELECT h.video_type,h.source_checked_at,h.source_expires_at FROM p
 JOIN LATERAL (SELECT h0.video_type,h0.source_checked_at,h0.source_expires_at FROM readable.hot_video_inventory h0
  WHERE h0.video_id=p.video_id AND h0.channel_id=p.channel_id LIMIT 1) h ON true
 WHERE p.proof_current IS NOT TRUE AND h.video_type=ANY(%(types)s::text[])) counted GROUP BY video_type"""

# The notice of a channel YouTube no longer returns (docs/DATA.md, "A channel
# YouTube no longer returns"): the day a check first found it missing (NULL:
# unknown, no check recorded that day) and when its
# last stored YouTube data stops being shown.
# Readable views return a value only inside its 29-day window, so each value's
# clock plus the window is its own deadline; the latest one is the answer.
# Not counted: the profile (withdrawn with the status) and Leti Cook's own
# "unknown_unavailable" marker. NULL: nothing readable is left.
# Each part is a LATERAL on one channel: the views are security barriers, so a
# join condition would not reach their indexes. Present catalogue entries use
# the channel's partial index; absent ones are found through its identities.
GONE_CHANNEL_NOTICES_SQL = """WITH gone AS MATERIALIZED (
 SELECT c.channel_id,c.country_code,c.country_collected_at,c.initial_inventory_estimate,c.channel_metadata_not_found_since
 FROM readable.channels c
 WHERE c.channel_id=ANY(%(ids)s::text[]) AND c.channel_metadata_status='channel_not_found'
)
SELECT gone.channel_id,gone.channel_metadata_not_found_since AS not_found_since,greatest(
  CASE WHEN gone.country_code IS NOT NULL THEN gone.country_collected_at END,
  estimate.at,videos.at,present.at,absent.at,history.at)+interval '696 hours' AS data_expires_at
FROM gone
CROSS JOIN LATERAL (SELECT greatest(
  max(CASE WHEN v.views IS NOT NULL OR v.like_count IS NOT NULL OR v.comment_count IS NOT NULL THEN v.checked_at END),
  max(CASE WHEN v.title IS NOT NULL OR v.published_at IS NOT NULL OR v.duration IS NOT NULL OR v.tags IS NOT NULL
   OR v.category_id IS NOT NULL OR v.youtube_topic_categories IS NOT NULL OR v.default_audio_language IS NOT NULL
   OR v.default_language IS NOT NULL OR v.caption_available IS NOT NULL OR v.stream_actual_end_at IS NOT NULL
   OR v.publication_status IS NOT NULL OR v.scheduled_start_at IS NOT NULL THEN v.metadata_checked_at END),
  max(CASE WHEN v.privacy_status IS NOT NULL OR v.upload_status IS NOT NULL
   OR v.availability_status<>'unknown_unavailable' THEN v.availability_checked_at END),
  max(snapshot.at),max(optional.at)) AS at
 FROM readable.videos v
 LEFT JOIN LATERAL (SELECT max(s.recorded_at) AS at FROM readable.video_snapshots s
  WHERE s.video_id=v.video_id) snapshot ON true
 LEFT JOIN LATERAL (SELECT max(greatest(CASE WHEN o.description IS NOT NULL THEN o.description_checked_at END,
   CASE WHEN o.paid_promotion IS NOT NULL THEN o.paid_promotion_checked_at END)) AS at
  FROM readable.video_optional_metadata o WHERE o.video_id=v.video_id) optional ON true
 WHERE v.channel_id=gone.channel_id) videos
CROSS JOIN LATERAL (SELECT max(least(m.last_seen_at,m.last_present_at)) AS at
 FROM readable.video_inventory_memberships m
 WHERE m.channel_id=gone.channel_id AND m.membership_status='present') present
CROSS JOIN LATERAL (SELECT max(entry.at) AS at FROM readable.retained_video_identities i
 CROSS JOIN LATERAL (SELECT least(m.last_seen_at,m.absent_since) AS at FROM readable.video_inventory_memberships m
  WHERE m.video_id=i.video_id AND m.channel_id=gone.channel_id AND m.membership_status='absent' OFFSET 0) entry
 WHERE i.channel_id=gone.channel_id) absent
CROSS JOIN LATERAL (SELECT max(s.recorded_at) AS at FROM readable.channel_snapshots s
 WHERE s.channel_id=gone.channel_id) history
CROSS JOIN LATERAL (SELECT max(clock.value::timestamptz) AS at
 FROM jsonb_each_text(CASE WHEN jsonb_typeof(gone.initial_inventory_estimate->'checked_at_by_type')='object'
  THEN gone.initial_inventory_estimate->'checked_at_by_type' END) clock(kind,value)
 WHERE clock.value IS NOT NULL) estimate
ORDER BY gone.channel_id"""


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
        # Always as the HTTP reader, the only role the read model is granted to.
        # Without JIT: on 625,000 videos its compilation took 0.8 s of 2.2 s.
        with http_read_scope():
            rows = self.database.read_rows(
                "frontend_count_inventory_by_type", INVENTORY_COUNT_SQL,
                {"channels": unique_channel_ids, "types": unique_video_types}, max_rows=len(unique_video_types),
                disable_jit=True,
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

    def load_gone_channel_notices(self, channel_ids: Iterable[str]) -> dict[str, dict[str, str | None]]:
        """For channels YouTube no longer returns: since when, and when their stored YouTube data is removed.

        Leti Cook's own check date and a deadline, not source values: they register
        no read lifetime. Channels whose status changed meanwhile are absent.
        """
        ids = sorted({str(channel_id) for channel_id in channel_ids if channel_id})
        if not ids:
            return {}
        rows = self.database.read_rows(
            "frontend_load_gone_channel_notices", GONE_CHANNEL_NOTICES_SQL,
            {"ids": ids}, max_rows=len(ids),
        ) or []
        return {
            str(row["channel_id"]): {
                "channel_metadata_not_found_since": iso_utc(row["not_found_since"]) if row.get("not_found_since") else None,
                "youtube_data_expires_at": iso_utc(row["data_expires_at"]) if row.get("data_expires_at") else None,
            }
            for row in rows
        }

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

    def load_channel_backfill_page(
        self, channel_id: str, *, video_type: str, offset: int, page_size: int,
    ) -> list[dict[str, Any]]:
        return self.database.read_rows(
            "frontend_load_channel_backfill_page",
            "SELECT video_id,published_at FROM public.video_inventory WHERE channel_id = %s AND video_type = %s "
            "ORDER BY published_at DESC NULLS FIRST, video_id DESC LIMIT %s OFFSET %s",
            (channel_id, _validated_video_type(video_type), page_size, offset), max_rows=page_size,
        ) or []
