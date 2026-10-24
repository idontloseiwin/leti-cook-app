"""Database boundary for observe-only YouTube quota accounting.

The repository owns per-request attempt evidence. It does not reserve quota,
reject requests, or change scheduler behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from psycopg import sql

from ..base.repository import DatabaseRepository
from ..base.postgres import Operation
from .quota_write_normalization import start_quota_attempt, finish_quota_attempt
from .quota_events import persist_events as persist_quota_events
from ..reads.operational_read_queries import read_youtube_quota_day
from ..base.sql_values import assignments, insert_values, projection
from operational_copy_policy import quota_label


YOUTUBE_API_REQUEST_ATTEMPTS_TABLE = "youtube_api_request_attempts"
YOUTUBE_QUOTA_ATTEMPT_COLUMNS = ",".join(
    (
        "request_attempt_id",
        "project_id",
        "quota_bucket",
        "quota_day",
        "phase",
        "endpoint",
        "quota_units",
        "status",
        "started_at",
        "finished_at",
        "duration_ms",
        "http_status",
        "error_type",
        "retry_of_request_attempt_id",
    )
)
DEFAULT_QUOTA_PAGE_SIZE = 1_000
MAX_QUOTA_ATTEMPT_ROWS = 100_000
MAX_QUOTA_EVENT_BATCH = 100


def _response_rows(data: Any) -> list[dict[str, Any]]:
    data = data or []
    if not isinstance(data, list):
        raise TypeError("YouTube quota query returned non-list data")
    rows = []
    for row in data:
        if not isinstance(row, Mapping):
            raise TypeError("YouTube quota query returned a non-object row")
        copied = dict(row)
        for field in ("phase", "endpoint", "quota_bucket", "error_type"):
            if field in copied:
                copied[field] = quota_label(field, copied[field])
        rows.append(copied)
    return rows


def _bounded_positive(value: int, *, name: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return min(parsed, maximum)


class YouTubeQuotaRepository(DatabaseRepository):
    """Sole Python owner of quota ledger queries."""

    def __init__(self, database, *, project_id: str = "primary"):
        super().__init__(database)
        project_id = str(project_id or "").strip()
        if not project_id:
            raise ValueError("YouTube quota project_id is required")
        self.project_id = project_id

    def persist_events(self, events: list[Mapping[str, Any]]) -> bool:
        """Acknowledge only a complete atomic replay of this project's events.

        The caller keeps its disk journal until this returns True. A transport
        error or malformed acknowledgement must retain the identical batch.
        The transaction protocol validates the whole batch and preserves
        immutable/terminal replay protection before committing its ACK.
        """
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_QUOTA_EVENT_BATCH:
            raise ValueError("YouTube quota event batch must contain 1 to 100 events")
        payload = []
        for event in events:
            if not isinstance(event, Mapping) or event.get("project_id") != self.project_id:
                raise ValueError("YouTube quota event project mismatch")
            payload.append(dict(event))
        accepted = persist_quota_events(self.database, payload)
        return type(accepted) is int and accepted == len(payload)

    def start_attempt(self, values: Mapping[str, Any]) -> bool:
        payload = dict(values)
        payload["project_id"] = self.project_id
        insert_values([payload])
        with self.database.transaction(Operation("youtube_quota_start", total_timeout_ms=60_000)) as tx:
            start_quota_attempt(tx, payload)
        return True

    def finish_attempt(
        self,
        request_attempt_id: str,
        values: Mapping[str, Any],
    ) -> bool:
        attempt_id = str(request_attempt_id or "").strip()
        if not attempt_id:
            return False
        payload = dict(values)
        assignments(payload)
        with self.database.transaction(Operation("youtube_quota_finish", total_timeout_ms=60_000)) as tx:
            finish_quota_attempt(tx, attempt_id, payload)
        return True

    def _list_for_day(
        self,
        quota_day: str,
        *,
        columns: str,
        page_size: int = DEFAULT_QUOTA_PAGE_SIZE,
        max_rows: int = MAX_QUOTA_ATTEMPT_ROWS,
    ) -> tuple[list[dict[str, Any]], bool]:
        quota_day = str(quota_day or "").strip()
        if not quota_day:
            raise ValueError("YouTube quota day is required")
        page_size = _bounded_positive(
            page_size,
            name="YouTube quota page_size",
            maximum=DEFAULT_QUOTA_PAGE_SIZE,
        )
        max_rows = _bounded_positive(
            max_rows,
            name="YouTube quota max_rows",
            maximum=MAX_QUOTA_ATTEMPT_ROWS,
        )

        rows: list[dict[str, Any]] = []
        offset = 0
        truncated = False
        while len(rows) < max_rows:
            current_size = min(page_size, max_rows - len(rows))
            page = _response_rows(self.database.read_rows(
                "youtube_quota_day_attempts",
                sql.SQL("SELECT {} FROM public.youtube_api_request_attempts WHERE project_id=%s AND quota_day=%s::date ORDER BY started_at ASC,request_attempt_id ASC LIMIT %s OFFSET %s").format(projection(columns)),
                (self.project_id, quota_day, current_size, offset), max_rows=current_size,
            ))
            rows.extend(page)
            if len(page) < current_size:
                break
            offset += current_size
            if len(rows) >= max_rows:
                truncated = True
                break
        return rows, truncated

    def list_attempts_for_day(
        self,
        quota_day: str,
        *,
        page_size: int = DEFAULT_QUOTA_PAGE_SIZE,
        max_rows: int = MAX_QUOTA_ATTEMPT_ROWS,
    ) -> tuple[list[dict[str, Any]], bool]:
        return self._list_for_day(
            quota_day,
            columns=YOUTUBE_QUOTA_ATTEMPT_COLUMNS,
            page_size=page_size,
            max_rows=max_rows,
        )

    def summarize_day(
        self,
        quota_day: str,
        *,
        now=None,
        stale_attempt_minutes: float = 10,
        max_rows: int = MAX_QUOTA_ATTEMPT_ROWS,
    ) -> dict[str, Any]:
        """Read daily aggregates without transferring individual attempts."""
        quota_day = str(quota_day or "").strip()
        if not quota_day:
            raise ValueError("YouTube quota day is required")
        maximum = _bounded_positive(max_rows, name="YouTube quota max_rows",
                                    maximum=MAX_QUOTA_ATTEMPT_ROWS)
        stale_minutes = float(stale_attempt_minutes)
        if not math.isfinite(stale_minutes) or stale_minutes < 0:
            raise ValueError("YouTube stale attempt minutes must be finite and non-negative")
        params = {"p_project_id": self.project_id, "p_quota_day": quota_day,
                  "p_max_rows": maximum, "p_stale_attempt_minutes": stale_minutes}
        if now is not None:
            params["p_now"] = now.isoformat()
        data = read_youtube_quota_day(self.database, params)
        if not isinstance(data, Mapping) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("Invalid YouTube quota summary")

        def count(value):
            if type(value) is not int or value < 0:
                raise ValueError("Invalid YouTube quota summary count")
            return value

        attempts = count(data.get("attempts"))
        if attempts > maximum or type(data.get("truncated")) is not bool:
            raise ValueError("Invalid YouTube quota summary bound")
        result = {"attempts": attempts, "quota_units": count(data.get("quota_units")),
                  "truncated": data["truncated"]}
        for field in ("bucket_attempt_counts", "bucket_units", "phase_attempt_counts",
                      "phase_units", "endpoint_attempt_counts", "status_counts",
                      "http_status_counts", "error_type_counts"):
            values = data.get(field)
            if not isinstance(values, Mapping) or any(not isinstance(k, str) for k in values):
                raise ValueError("Invalid YouTube quota summary dimension")
            dimension = {
                "bucket_attempt_counts": "quota_bucket", "bucket_units": "quota_bucket",
                "phase_attempt_counts": "phase", "phase_units": "phase",
                "endpoint_attempt_counts": "endpoint", "error_type_counts": "error_type",
            }.get(field)
            result[field] = {}
            for key, value in sorted(values.items()):
                safe_key = quota_label(dimension, key) if dimension else key
                result[field][safe_key] = result[field].get(safe_key, 0) + count(value)
        for field in ("bucket_attempt_counts", "phase_attempt_counts", "endpoint_attempt_counts", "status_counts"):
            if sum(result[field].values()) != attempts:
                raise ValueError("Inconsistent YouTube quota summary attempts")
        for field in ("bucket_units", "phase_units"):
            if sum(result[field].values()) != result["quota_units"]:
                raise ValueError("Inconsistent YouTube quota summary units")

        def measurement(value, *, allow_none):
            if value is None and allow_none:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("Invalid YouTube quota summary measurement")
            return round(float(value), 3)

        duration, unfinished = data.get("duration_ms"), data.get("unfinished")
        if not isinstance(duration, Mapping) or not isinstance(unfinished, Mapping):
            raise ValueError("Invalid YouTube quota summary measurements")
        duration_count, unfinished_count = count(duration.get("count")), count(unfinished.get("count"))
        stale_count = count(unfinished.get("stale_count"))
        if max(duration_count, unfinished_count) > attempts or stale_count > unfinished_count:
            raise ValueError("Inconsistent YouTube quota summary measurements")
        result["duration_ms"] = {"count": duration_count, **{
            key: measurement(duration.get(key), allow_none=duration_count == 0)
            for key in ("min", "avg", "median", "max")}}
        result["unfinished"] = {"count": unfinished_count, "stale_count": stale_count,
            "oldest_age_minutes": measurement(unfinished.get("oldest_age_minutes"), allow_none=unfinished_count == 0),
            "stale_after_minutes": stale_attempt_minutes}
        return result
