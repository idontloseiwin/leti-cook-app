"""Stored channel profile and playlist evidence used for the unlisted notice."""

from __future__ import annotations

from typing import Any

from ..base.client import execute_recoverable_read
from ..base.postgres import Operation
from ..base.repository import DatabaseRepository
from ..base.sql_values import application_value, projection
from psycopg import sql


VIDEO_TYPES = frozenset({"long", "short", "stream"})


# The list proof is channel-scoped source evidence, separate from counter/hydration readiness.
# All source rows come from protected views; one bounded read returns only counts.
# Pass one channel into both Library views so PostgreSQL can restrict their
# receipt lookups before joining the three formats.
_LIST_INVENTORY = """WITH args AS MATERIALIZED(SELECT %s::text AS channel_id,%s::integer AS scan_limit,interval '696 hours' AS source_window),
 states AS MATERIALIZED(
 SELECT args.source_window,kind.video_type,state.playlist_id,state.inventory_status,state.source_checked_at,
 state.source_expires_at,completed.scan_id AS completed_scan_id,
 full_generation.source_expires_at AS full_source_expires_at
 FROM (VALUES('long'::text),('short'::text),('stream'::text)) kind(video_type)
 CROSS JOIN args
 LEFT JOIN readable.channel_video_inventory_state state
 ON state.channel_id=args.channel_id AND state.video_type=kind.video_type
 LEFT JOIN readable.channel_scan_sources completed
 ON completed.scan_id=state.last_completed_scan_id AND completed.channel_id=state.channel_id
 AND completed.video_type=state.video_type AND completed.playlist_id=state.playlist_id
 AND completed.status='completed' AND completed.coverage='complete'
 AND completed.completed_at<=statement_timestamp()
 LEFT JOIN LATERAL(SELECT scan.source_expires_at FROM readable.channel_scan_sources scan
 WHERE state.inventory_status IN('completed','running')
 AND scan.channel_id=args.channel_id AND scan.video_type=kind.video_type
 AND scan.playlist_id=state.playlist_id AND scan.status='completed' AND scan.coverage='complete'
 AND scan.scan_kind IN('initial_inventory','full_reconciliation')
 AND scan.completed_at<=statement_timestamp()
 ORDER BY scan.completed_at DESC,scan.scan_id DESC LIMIT 1) full_generation ON true),
 formats AS MATERIALIZED(
 SELECT states.*,CASE
 WHEN inventory_status IS NULL OR inventory_status IN('pending','not_started') THEN 'not_collected'
 WHEN source_checked_at IS NULL OR NOT isfinite(source_checked_at)
 OR source_checked_at NOT BETWEEN statement_timestamp()-source_window AND statement_timestamp() THEN
 CASE WHEN inventory_status IN('completed','playlist_not_found') THEN 'stale' ELSE 'incomplete' END
 WHEN inventory_status='playlist_not_found' THEN 'absent'
 WHEN inventory_status IN('completed','running') AND completed_scan_id IS NOT NULL
 AND full_source_expires_at IS NOT NULL THEN 'complete' ELSE 'incomplete' END AS status
 FROM states),
 members AS MATERIALIZED(
 SELECT formats.video_type,member.video_id,member.source_expires_at
 FROM formats CROSS JOIN args
 CROSS JOIN LATERAL(SELECT m.video_id,m.source_expires_at FROM readable.video_inventory_memberships m
 WHERE formats.status='complete' AND m.channel_id=args.channel_id
 AND m.video_type=formats.video_type AND m.playlist_id=formats.playlist_id
 AND m.membership_status='present' ORDER BY m.video_id LIMIT args.scan_limit+1) member),
 proofs AS MATERIALIZED(
 SELECT formats.video_type,
 CASE WHEN count(member.video_id)>args.scan_limit THEN 'incomplete' ELSE formats.status END AS status,
 CASE WHEN formats.status='absent' THEN 0
 WHEN formats.status='complete' AND count(member.video_id)<=args.scan_limit THEN count(member.video_id) END AS listed_video_count,
 least(formats.source_expires_at,formats.full_source_expires_at,min(member.source_expires_at)) AS source_expires_at
 FROM formats CROSS JOIN args LEFT JOIN members member ON member.video_type=formats.video_type
 GROUP BY formats.video_type,formats.status,formats.source_expires_at,formats.full_source_expires_at,args.scan_limit)
 SELECT bool_and(proofs.status IN('complete','absent')) AS complete,
 CASE WHEN bool_and(proofs.status IN('complete','absent')) THEN (SELECT count(DISTINCT video_id) FROM members) END AS unique_video_count,
 min(proofs.source_expires_at) AS source_expires_at,
 jsonb_object_agg(proofs.video_type,jsonb_build_object('status',proofs.status,'listed_video_count',listed_video_count,
 'source_expires_at',proofs.source_expires_at)) AS formats,
 jsonb_object_agg(proofs.video_type,jsonb_build_object(
 'status',coalesce(library.status,'not_checked'),
 'manual_check',jsonb_build_object('epoch',manual.control_epoch,'unavailable',coalesce(manual.unavailable_reason,
 CASE WHEN manual.control_epoch IS NULL THEN 'source_unavailable' END),
 'request',manual.request,'quota_retry_at',manual.quota_retry_at,'estimated_general',
 CASE WHEN library.absent_checked_at IS NOT NULL THEN 1 ELSE coalesce(manual.last_full_pages,
 CASE WHEN listed_video_count IS NOT NULL AND proofs.status='complete'
 THEN greatest(1,(listed_video_count+49)/50) END) END),
 'full_completed_at',library.full_completed_at,'absent_checked_at',library.absent_checked_at,
 'source_expires_at',least(library.full_expires_at,library.absent_checked_at+args.source_window))) AS library_formats
 FROM proofs CROSS JOIN args LEFT JOIN readable.library_format_coverage library
 ON library.channel_id=(SELECT channel_id FROM args) AND library.video_type=proofs.video_type
 LEFT JOIN readable.library_manual_list_check manual ON manual.channel_id=(SELECT channel_id FROM args) AND manual.video_type=proofs.video_type"""


class ChannelExplorerRepository(DatabaseRepository):
    """Own the fixed, bounded query shapes of the channel header."""

    def load_channel(self, channel_id: str, *, columns: str) -> dict[str, Any] | None:
        if columns != "*":
            columns = ",".join(dict.fromkeys([*columns.split(","), "source_expires_at"]))
        row = execute_recoverable_read(
            self.database,
            lambda database: database.read_row(
                "channel_explorer_load_channel",
                sql.SQL("SELECT {} FROM readable.channels_with_control WHERE channel_id = %s LIMIT 1").format(projection(columns)),
                (channel_id,),
            ),
            operation="channel_explorer.load_channel",
        )
        return self.admit_source_deadlines([dict(row)])[0] if row else None

    def load_recent_public_video_counts(self, channel_id: str, *, recorded_through: str):
        """Two stored observations, not a new provider check or disappearance scan."""
        rows = execute_recoverable_read(
            self.database,
            lambda database: database.read_rows(
                "channel_explorer_public_video_counts",
                "SELECT recorded_at,video_count,source_expires_at FROM readable.channel_snapshots "
                "WHERE channel_id = %s AND recorded_at <= %s::timestamptz "
                "ORDER BY recorded_at DESC LIMIT 2",
                (channel_id, recorded_through), max_rows=2,
            ),
            operation="channel_explorer.public_video_counts",
        )
        return self.admit_source_deadlines([dict(row) for row in rows])

    def load_list_inventory(self, channel_id: str, *, limit: int) -> dict[str, Any]:
        """Fresh list proof/counts without transferring IDs or reading counters."""
        if type(limit) is not int or not 1 <= limit <= 1_000_000:
            raise ValueError("invalid inventory proof limit")
        operation = Operation("channel_explorer_list_inventory", read_only=True,
                              statement_timeout_ms=5_000, total_timeout_ms=5_000)
        with self.database.transaction(operation) as transaction:
            row = application_value(transaction.fetch_one(_LIST_INVENTORY, (channel_id, limit)))
        if (not isinstance(row, dict) or type(row.get("complete")) is not bool
                or not isinstance(row.get("formats"), dict)
                or set(row["formats"]) != VIDEO_TYPES
                or (row["complete"] and (type(row.get("unique_video_count")) is not int
                                         or row["unique_video_count"] < 0
                                         or row.get("source_expires_at") is None))
                or (not row["complete"] and row.get("unique_video_count") is not None)):
            raise RuntimeError("inventory list proof is unavailable")
        formats = row.get("library_formats")
        if not isinstance(formats, dict) or set(formats) != VIDEO_TYPES:
            raise RuntimeError("library format coverage is unavailable")
        self.admit_source_deadlines(formats.values())
        return self.admit_source_deadlines([row])[0]
