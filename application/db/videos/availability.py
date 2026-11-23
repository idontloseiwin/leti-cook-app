"""Database boundary for video availability state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
import time
from typing import Any

from collection_policy import MAX_INTERVAL_SECONDS
from .collection_policy import require_cadence_snapshot
from ..base.repository import DatabaseRepository
from ..base.postgres import DatabaseError
from telemetry import emit_event


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if value))


class AvailabilityRepository(DatabaseRepository):
    """Owns availability incidents, events, and unavailable-video state."""

    def load_recheck_candidate_ids(
        self,
        channel_id: str,
        statuses: Sequence[str],
        *,
        limit: int,
        recheck_after: timedelta | None = None,
    ) -> list[str]:
        """Select open long unavailable videos to recheck, oldest miss first.

        With ``recheck_after``, only videos whose last miss is at least that
        old are selected; the interval doubles with each further miss, up to
        the configured maximum from the process snapshot (enforced in SQL).
        """
        parsed_channel = str(channel_id or "").strip()
        if not parsed_channel:
            raise ValueError("channel_id is required")
        if len(parsed_channel) > 128:
            raise DatabaseError("long_availability_recheck_input_invalid", code="P0001")
        if isinstance(statuses, (str, bytes)):
            raise ValueError("availability statuses must be a sequence")
        parsed_statuses = [str(status or "").strip() for status in statuses]
        if any(not status for status in parsed_statuses):
            raise ValueError("availability statuses cannot be blank")
        if any(len(status) > 120 for status in parsed_statuses):
            raise DatabaseError("long_availability_recheck_input_invalid", code="P0001")
        parsed_statuses = list(dict.fromkeys(parsed_statuses))
        if not parsed_statuses:
            return []
        if len(parsed_statuses) > 20:
            raise ValueError("availability recheck accepts at most 20 statuses")
        parsed_limit = int(limit)
        if parsed_limit < 0 or parsed_limit > 1000:
            raise ValueError("availability recheck limit must be 0..1000")
        params = {
            "p_channel_id": parsed_channel,
            "p_statuses": parsed_statuses,
            "p_limit": parsed_limit,
        }
        if recheck_after is not None:
            if not isinstance(recheck_after, timedelta) or not (
                timedelta(hours=1) <= recheck_after <= timedelta(seconds=MAX_INTERVAL_SECONDS)
            ):
                raise ValueError("availability recheck interval must be 1 hour..28 days")
            params["p_recheck_after"] = f"{int(recheck_after.total_seconds())} seconds"
        if parsed_limit == 0:
            return []
        params["p_max_recheck_seconds"] = require_cadence_snapshot(
            self.database).rechecks.unavailable_long_max_seconds
        response = self._read_recheck_candidates(params, expected_count=parsed_limit)
        rows = response or []
        if not isinstance(rows, list):
            raise TypeError(
                "long availability recheck selector returned invalid data"
            )
        selected: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise TypeError(
                    "long availability recheck selector returned invalid data"
                )
            video_id = str(row.get("video_id") or "").strip()
            if video_id and video_id not in seen:
                seen.add(video_id)
                selected.append(video_id)
        if len(selected) > parsed_limit:
            raise RuntimeError(
                "long availability recheck selector exceeded its limit"
            )
        return selected

    def _read_recheck_candidates(self, params, *, expected_count):
        """Preserve the selector's telemetry contract around one ordinary read."""
        name, phase = "get_long_availability_recheck_candidate_ids", "availability_recheck"
        emit_event("rpc", "sync_engine", "db_selectors", name, phase=phase,
                   direction="request", status="started", count=expected_count)
        started = time.perf_counter()
        try:
            data = self.database.read_rows(
                "availability_recheck_candidates", AVAILABILITY_RECHECK_SQL,
                {"p_recheck_after": None, **params}, max_rows=expected_count,
            )
            emit_event("rpc", "db_selectors", "sync_engine", name, phase=phase,
                       direction="response", status="ok", count=len(data) if isinstance(data, list) else None,
                       duration_ms=round((time.perf_counter() - started) * 1000))
            return data
        except Exception as error:
            emit_event("rpc", "db_selectors", "sync_engine", name, phase=phase,
                       direction="response", status="error", detail=error.__class__.__name__,
                       duration_ms=round((time.perf_counter() - started) * 1000))
            raise

    def load_long_inventory_candidate_ids(
        self,
        channel_id: str,
        *,
        limit: int,
    ) -> list[str]:
        inventory_ids: list[str] = []
        page = 0
        page_size = min(1000, limit)
        while len(inventory_ids) < limit:
            remaining = limit - len(inventory_ids)
            batch_size = min(page_size, remaining)
            rows = self.database.read_rows(
                "availability_long_inventory",
                "SELECT video_id, published_at FROM public.video_inventory "
                "WHERE channel_id = %s AND video_type = 'long' "
                "ORDER BY published_at DESC NULLS FIRST, video_id ASC "
                "LIMIT %s OFFSET %s",
                (channel_id, batch_size, page * page_size), max_rows=batch_size,
            )
            inventory_ids.extend(
                str(row["video_id"]) for row in rows if row.get("video_id")
            )
            if len(rows) < batch_size:
                break
            page += 1
        return _unique(inventory_ids)

    def load_video_availability_rows(
        self,
        video_ids: Iterable[str],
    ) -> list[dict[str, Any]]:
        clean_ids = _unique(video_ids)
        rows: list[dict[str, Any]] = []
        for start in range(0, len(clean_ids), 100):
            batch = clean_ids[start:start + 100]
            data = self.database.read_rows(
                "availability_video_statuses",
                "SELECT video_id, availability_status FROM public.videos "
                "WHERE video_id = ANY(%s::text[]) LIMIT %s",
                (batch, len(batch)), max_rows=len(batch),
            )
            rows.extend(dict(row) for row in data)
        return rows


# Keep the retained selector's per-predicate clock_timestamp calls and interval
# arithmetic. This operational queue reader uses canonical inventory, not HOT
# product visibility; a missing/nonfinite/future miss clock is eligible now.
AVAILABILITY_RECHECK_SQL = """
with requested_statuses as materialized (
        select
            btrim(requested.status) as status,
            min(requested.ordinality) as status_order
        from unnest(%(p_statuses)s::text[]) with ordinality
            as requested(status, ordinality)
        group by btrim(requested.status)
    )
    select unavailable.video_id
    from requested_statuses as requested
    join public.open_video_availability as unavailable
      on unavailable.availability_status = requested.status
     and unavailable.channel_id = btrim(%(p_channel_id)s::text)
    join public.video_inventory as inventory
      on inventory.video_id = unavailable.video_id
     and inventory.channel_id = unavailable.channel_id
     and inventory.video_type = 'long'
    where %(p_recheck_after)s::interval is null
       or unavailable.last_seen_at is null
       or not isfinite(unavailable.last_seen_at)
       or unavailable.last_seen_at > clock_timestamp()
       or unavailable.last_seen_at <= clock_timestamp() - least(
              %(p_recheck_after)s::interval * power(2, least(greatest(coalesce(unavailable.attempt_count, 1) - 1, 0), 5)),
              make_interval(secs => %(p_max_recheck_seconds)s::integer))
    order by
        requested.status_order,
        unavailable.last_seen_at ASC NULLS LAST,
        unavailable.video_id
    limit %(p_limit)s::integer
"""
