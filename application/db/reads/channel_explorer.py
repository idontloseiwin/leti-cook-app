"""Read-only persistence boundary for the Channel Explorer product surface."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from catalogue_scope import CatalogueScopeError
from hot_storage import utc_instant

from ..base.client import execute_recoverable_read
from ..base.repository import DatabaseRepository
from ..base.postgres import Operation
from ..base.sql_values import application_value
from psycopg import sql
from ..base.sql_values import projection


INVENTORY_PAGE_SIZE = 1000
DB_BATCH_SIZE = 500
CURSOR_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
VIDEO_TYPES = frozenset({"long", "short", "stream"})


# RR18 is a channel-scoped source proof, separate from counter/hydration readiness.
# All source rows come from protected views; one bounded read returns only counts.
_LIST_INVENTORY = """WITH args AS MATERIALIZED(SELECT %s::text AS channel_id,%s::integer AS scan_limit),
 states AS MATERIALIZED(
 SELECT kind.video_type,state.playlist_id,state.inventory_status,state.source_checked_at,
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
 OR source_checked_at NOT BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN
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
 SELECT bool_and(status IN('complete','absent')) AS complete,
 CASE WHEN bool_and(status IN('complete','absent')) THEN (SELECT count(DISTINCT video_id) FROM members) END AS unique_video_count,
 min(source_expires_at) AS source_expires_at,
 jsonb_object_agg(video_type,jsonb_build_object('status',status,'listed_video_count',listed_video_count,
 'source_expires_at',source_expires_at)) AS formats FROM proofs"""


class ChannelExplorerRepository(DatabaseRepository):
    """Own the fixed, bounded query shapes used by Channel Explorer."""

    def _inventory_page(
        self, channel_id: str, *, video_type: str, page_size: int,
    ) -> list[dict[str, Any]]:
        statement = (
            "SELECT video_id,published_at,source_checked_at,source_expires_at FROM readable.hot_video_inventory "
            "WHERE channel_id = %s AND video_type = %s"
        )
        params = [channel_id, video_type]
        statement += " ORDER BY published_at DESC NULLS LAST, video_id ASC LIMIT %s"
        params.append(page_size)
        data = execute_recoverable_read(
            self.database,
            lambda database: database.read_rows(
                "channel_explorer_inventory_page", statement, params, max_rows=page_size,
            ),
            operation="channel_explorer.inventory_page",
        )
        return self.admit_source_deadlines([dict(row) for row in (data or [])])

    @contextmanager
    def _inventory_source(self, channel_id: str, video_type: str):
        # One finite read operation replaces repeated page operations. Full
        # catalogue callers retain their separate byte/time assembly budget.
        operation = Operation('channel_explorer_inventory_page', read_only=True,
                              total_timeout_ms=60_000)
        with self.database.transaction(operation) as tx:
            with tx.inventory_source_rows(channel_id, video_type) as stream:
                yield stream

    def _inventory_source_page(self, stream, size: int):
        data = application_value(stream.fetch(size))
        if (not isinstance(data, list) or len(data) > size
                or any(not isinstance(row, dict) for row in data)):
            raise CatalogueScopeError("catalogue_inventory_response_invalid")
        return [dict(row) for row in data]

    def iter_inventory_pages(
        self, channel_id: str, *, video_type: str = "long",
    ) -> Iterator[list[dict[str, Any]]]:
        """Enumerate one protected typed source through bounded fetches and EOF."""
        if not isinstance(video_type, str) or video_type not in VIDEO_TYPES:
            raise ValueError("unsupported video type")
        previous_id: str | None = None
        earliest = None
        with self._inventory_source(channel_id, video_type) as stream:
            while True:
                batch = self.admit_source_deadlines(
                    self._inventory_source_page(stream, INVENTORY_PAGE_SIZE))
                if not batch:
                    break
                page_ids = [row.get("video_id") for row in batch]
                if any(not isinstance(value, str) or not CURSOR_VIDEO_ID_RE.fullmatch(value)
                       for value in page_ids):
                    raise CatalogueScopeError("catalogue_inventory_cursor_invalid")
                if len(set(page_ids)) != len(page_ids):
                    raise CatalogueScopeError("catalogue_inventory_identity_invalid")
                if previous_id in page_ids:
                    raise CatalogueScopeError("catalogue_inventory_cursor_did_not_advance")
                for row in batch:
                    if row.get("source_expires_at") is not None:
                        deadline = utc_instant(row["source_expires_at"])
                        earliest = deadline if earliest is None else min(earliest, deadline)
                previous_id = page_ids[-1]
                yield batch
        # EOF, cursor close and transaction exit must all succeed first. The
        # first page may have expired while later pages were being fetched.
        if earliest is not None:
            self.admit_source_deadlines([{"source_expires_at": earliest}])

    def iter_long_inventory_pages(self, channel_id: str) -> Iterator[list[dict[str, Any]]]:
        """Compatibility alias for explicitly long-only callers."""
        return self.iter_inventory_pages(channel_id)

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

    def load_inventory(
        self,
        channel_id: str,
        *,
        video_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if video_type not in VIDEO_TYPES:
            raise ValueError("unsupported video type")
        requested = max(0, int(limit))
        if requested == 0:
            return []
        # One statement: the protected view cannot be read in pages by its order
        # key, so every 1000-row page rebuilt the channel's whole inventory. One
        # read is also one snapshot of the catalogue.
        return self._inventory_page(channel_id, video_type=video_type, page_size=requested)

    def load_format_inventory(
        self, channel_id: str, *, video_type: str, limit: int,
    ) -> list[dict[str, Any]]:
        """Internal aggregate bag; overflow retains the public publication prefix."""
        if video_type not in VIDEO_TYPES:
            raise ValueError("unsupported video type")
        requested = max(0, int(limit))
        if requested == 0:
            return []
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        complete = False
        with self._inventory_source(channel_id, video_type) as stream:
            while len(rows) < requested:
                page_size = min(INVENTORY_PAGE_SIZE, requested - len(rows))
                data = self._inventory_source_page(stream, page_size)
                if not data:
                    complete = True
                    break
                page_ids = [row.get("video_id") for row in data]
                if any(not isinstance(value, str) or not CURSOR_VIDEO_ID_RE.fullmatch(value)
                       for value in page_ids):
                    raise CatalogueScopeError("catalogue_inventory_cursor_invalid")
                if len(set(page_ids)) != len(page_ids) or seen_ids.intersection(page_ids):
                    raise CatalogueScopeError("catalogue_inventory_identity_invalid")
                seen_ids.update(page_ids)
                rows.extend(data)
        # Admit only the returned bag, after known cursor/transaction close.
        # Speculative overflow sources must not shorten the returned prefix.
        if complete:
            return self.admit_source_deadlines(rows)
        rows.clear()
        seen_ids.clear()
        del data, page_ids
        return self.load_inventory(channel_id, video_type=video_type, limit=requested)

    def count_canonical_inventory(self, channel_id: str, *, video_type: str, exclude_waiting: bool = False) -> int:
        """Completeness proof; raw stored counts retain waiting identities."""
        if type(exclude_waiting) is not bool:
            raise ValueError("invalid inventory completeness scope")
        relation = "readable.published_inventory_counts" if exclude_waiting else "readable.canonical_inventory_counts"
        if video_type not in VIDEO_TYPES:
            raise ValueError("unsupported video type")
        row = execute_recoverable_read(
            self.database,
            lambda database: database.read_row(
                "channel_explorer_count_canonical_inventory",
                "SELECT coalesce((SELECT canonical_count FROM " + relation + " "
                "WHERE channel_id = %s AND video_type = %s),0)::bigint AS count",
                (channel_id, video_type),
            ),
            operation="channel_explorer.count_canonical_inventory",
        )
        count = row.get("count") if isinstance(row, dict) else None
        if type(count) is not int or count < 0:
            raise RuntimeError("inventory scope count is unavailable")
        return count

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
        return self.admit_source_deadlines([row])[0]

    def load_inventory_state(
        self, channel_id: str, *, video_type: str,
    ) -> dict[str, Any] | None:
        row = execute_recoverable_read(
            self.database,
            lambda database: database.read_row(
                "channel_explorer_load_inventory_state",
                "SELECT channel_id,video_type,inventory_status,source_checked_at,"
                "inventory_completed_at,product_exposure_ready,last_error_code,last_error_at,source_expires_at "
                "FROM readable.channel_video_inventory_state "
                "WHERE channel_id = %s AND video_type = %s LIMIT 1",
                (channel_id, video_type),
            ),
            operation="channel_explorer.load_inventory_state",
        )
        return self.admit_source_deadlines([dict(row)])[0] if row else None

    def load_type_resolutions(
        self, video_ids: Sequence[str], *, video_type: str,
    ) -> list[dict[str, Any]]:
        if video_type not in VIDEO_TYPES:
            raise ValueError("unsupported video type")
        rows: list[dict[str, Any]] = []
        clean_ids = list(dict.fromkeys(str(video_id) for video_id in video_ids if video_id))
        for start in range(0, len(clean_ids), DB_BATCH_SIZE):
            batch_ids = clean_ids[start:start + DB_BATCH_SIZE]
            batch = execute_recoverable_read(
                self.database,
                lambda database: database.read_rows(
                    "channel_explorer_load_type_resolutions",
                    "SELECT resolution.* FROM (SELECT %s::text AS video_type) AS requested_type "
                    "CROSS JOIN unnest(%s::text[]) AS requested(video_id) "
                    "CROSS JOIN LATERAL ("
                    "SELECT proof.video_id,proof.canonical_channel_id,proof.canonical_video_type,proof.resolution_status,"
                    "proof.present_membership_types,proof.present_membership_channels,"
                    "proof.manual_valid,proof.automatic_valid,proof.source_expires_at "
                    "FROM readable.video_inventory_type_resolution AS proof "
                    "WHERE proof.video_id = requested.video_id "
                    "AND proof.canonical_video_type = requested_type.video_type OFFSET 0) AS resolution",
                    (video_type, batch_ids), max_rows=len(batch_ids),
                ),
                operation="channel_explorer.load_type_resolutions",
            )
            rows.extend(batch or [])
        return self.admit_source_deadlines([dict(row) for row in rows])

    def load_long_inventory(
        self,
        channel_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Compatibility wrapper for explicitly long-only callers."""

        return self.load_inventory(
            channel_id,
            video_type="long",
            limit=limit,
        )

    def load_videos_by_ids(
        self, video_ids: Sequence[str], *, columns: str,
    ) -> list[dict[str, Any]]:
        if columns != "*":
            columns = ",".join(dict.fromkeys([*columns.split(","), "source_expires_at"]))
        rows = []
        ids = list(dict.fromkeys(video_ids))
        if not ids:
            return []
        statement = sql.SQL("SELECT {} FROM readable.videos WHERE video_id = ANY(%s::text[])").format(projection(columns))
        for start in range(0, len(ids), DB_BATCH_SIZE):
            batch = ids[start:start + DB_BATCH_SIZE]
            rows.extend(execute_recoverable_read(
                self.database,
                lambda database: database.read_rows(
                    "channel_explorer_load_videos_by_ids", statement, (batch,), max_rows=len(batch),
                ),
                operation="channel_explorer.load_videos_by_ids",
            ) or [])
        return self.admit_source_deadlines(rows)
