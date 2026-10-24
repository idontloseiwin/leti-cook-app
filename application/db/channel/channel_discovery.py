"""Readiness, inventory reads and health over shared playlist state.

The former leased Long writer RPCs are retired; the shared inventory owner
handles claims, page settlement and retries for every video type.
The unused legacy Long-only backfill entry is retired; shared typed preparation
owns queue initialization and reconciliation.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from telemetry import emit_event
from ..base.repository import DatabaseRepository
from .channel_discovery_projection import CHANNEL_DISCOVERY_RELATION
from ..readiness import capabilities
from ..reads.operational_read_queries import get_channel_discovery_queue_health


MAX_DISCOVERY_RPC_ROWS = 1000




def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if value))


def _chunks(values: Sequence[Any], size: int = MAX_DISCOVERY_RPC_ROWS):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)




class ChannelDiscoveryRepository(DatabaseRepository):
    """Retained read/readiness contracts over shared inventory state."""

    def _execute(self, operation: str, callback: Callable[[], Any]) -> Any:
        emit_event(
            "database",
            "channel_discovery_repository",
            "postgres",
            operation,
            direction="request",
            status="started",
        )
        started = time.perf_counter()
        try:
            response = callback()
        except Exception as error:
            emit_event(
                "database",
                "channel_discovery_repository",
                "postgres",
                operation,
                direction="response",
                status="error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                detail=error.__class__.__name__,
            )
            raise
        data = response
        emit_event(
            "database",
            "channel_discovery_repository",
            "postgres",
            operation,
            direction="response",
            status="success",
            count=len(data) if isinstance(data, list) else None,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return response

    def schema_status(self) -> Mapping[str, Any]:
        data = self._execute("rpc:assert_channel_discovery_queue_schema", lambda: capabilities.assert_channel_discovery_queue_schema(self.database))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, Mapping):
            raise TypeError("channel discovery schema assertion returned invalid data")
        return dict(data)

    def assert_ready(self) -> Mapping[str, Any]:
        data = self._execute("rpc:assert_channel_discovery_queue_ready", lambda: capabilities.assert_channel_discovery_queue_ready(self.database))
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, Mapping):
            raise TypeError("channel discovery readiness assertion returned invalid data")
        return dict(data)

    def assert_paging_schema(self) -> Any:
        """Verify the legacy paged-discovery contract used in either route."""

        return capabilities.telemetry_check(
            self.database, capabilities.assert_channel_discovery_paging_schema, phase="preflight",
        )

    def preflight_queue_ready(self) -> Any:
        return capabilities.telemetry_check(
            self.database, capabilities.assert_channel_discovery_queue_ready, phase="preflight",
        )

    def load_inventory_state(
        self,
        channel_id: str,
        video_ids: Iterable[str],
        *,
        discovered_before: str | None = None,
    ) -> Mapping[str, set[str]]:
        clean_ids = _unique(video_ids)
        if not clean_ids:
            return {"existing_ids": set(), "stop_ids": set()}
        rows: list[Mapping[str, Any]] = []
        for batch in _chunks(clean_ids, 500):
            response = self._execute(
                "select:long_inventory_discovery_state",
                lambda batch=batch: self.database.read_rows(
                    "discovery_inventory_state",
                    "SELECT video_id, channel_id, video_type, discovered_at "
                    "FROM public.video_inventory WHERE video_id = ANY(%s::text[]) LIMIT %s",
                    (batch, len(batch)), max_rows=len(batch),
                ),
            )
            data = response or []
            if not isinstance(data, list):
                raise TypeError("long inventory state query returned invalid data")
            rows.extend(data)

        existing_ids = {
            str(row["video_id"]) for row in rows if row.get("video_id")
        }
        cutoff = _parse_datetime(discovered_before)
        if cutoff is None:
            return {"existing_ids": existing_ids, "stop_ids": existing_ids}

        stop_ids = set()
        for row in rows:
            if (
                str(row.get("channel_id") or "") != str(channel_id)
                or row.get("video_type") != "long"
            ):
                continue
            discovered_at = _parse_datetime(row.get("discovered_at"))
            if discovered_at is None or discovered_at < cutoff:
                stop_ids.add(str(row["video_id"]))
        return {"existing_ids": existing_ids, "stop_ids": stop_ids}

    def load_long_inventory_rows(
        self,
        channel_id: str,
        video_ids: Iterable[str],
    ) -> list[Mapping[str, Any]]:
        clean_ids = _unique(video_ids)
        if not clean_ids:
            return []
        rows = []
        for batch in _chunks(clean_ids, 1000):
            data = self.database.read_rows(
                "discovery_long_inventory",
                "SELECT video_id, discovered_at FROM public.video_inventory "
                "WHERE channel_id = %s AND video_type = 'long' "
                "AND video_id = ANY(%s::text[]) LIMIT %s",
                (channel_id, batch, len(batch)), max_rows=len(batch),
            )
            if not isinstance(data, list):
                raise TypeError("long inventory row query returned invalid data")
            rows.extend(data)
        if not isinstance(rows, list):
            raise TypeError("long inventory row query returned invalid data")
        return [dict(row) for row in rows]


    def load_existing_inventory(
        self,
        video_ids: Iterable[str],
    ) -> dict[str, Mapping[str, Any]]:
        clean_ids = _unique(video_ids)
        if not clean_ids:
            return {}
        rows = []
        for batch in _chunks(clean_ids, 1000):
            data = self.database.read_rows(
                "discovery_existing_inventory",
                "SELECT video_id FROM public.video_inventory "
                "WHERE video_id = ANY(%s::text[]) LIMIT %s",
                (batch, len(batch)), max_rows=len(batch),
            )
            if not isinstance(data, list):
                raise TypeError("existing inventory query returned invalid data")
            rows.extend(data)
        if not isinstance(rows, list):
            raise TypeError("existing inventory query returned invalid data")
        return {
            str(row["video_id"]): dict(row)
            for row in rows
            if row.get("video_id")
        }


    def latest_long_inventory_published_at(self, channel_id: str) -> Any:
        row = self.database.read_row(
            "discovery_latest_long_publication",
            "SELECT published_at FROM public.video_inventory "
            "WHERE channel_id = %s AND video_type = 'long' "
            "ORDER BY published_at DESC NULLS FIRST, video_id ASC LIMIT 1",
            (channel_id,),
        )
        return row.get("published_at") if row else None


    def load_initial_inventory_status(self, channel_id: str) -> Mapping[str, Any] | None:
        row = self.database.read_row(
            "discovery_initial_inventory_status",
            "SELECT initial_long_inventory_status, deep_inventory_status "
            "FROM public.channels_with_control WHERE channel_id = %s LIMIT 1",
            (channel_id,),
        )
        return dict(row) if row else None

    def load_timing(self, channel_id: str) -> Mapping[str, Any]:
        row = self.database.read_row(
            "discovery_channel_timing",
            "SELECT channel_video_count_changed_at, channel_discovery_checked_at "
            "FROM " + CHANNEL_DISCOVERY_RELATION + " WHERE channel_id = %s LIMIT 1",
            (channel_id,),
        )
        return dict(row) if row else {}

    def health(self) -> Mapping[str, Any]:
        data = self._execute("rpc:get_channel_discovery_queue_health",
                             lambda: get_channel_discovery_queue_health(self.database))
        if not isinstance(data, list) or len(data) != 1:
            raise TypeError("channel discovery health returned invalid data")
        return dict(data[0])
