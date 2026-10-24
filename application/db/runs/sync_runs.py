"""Database boundary for durable sync-run history.

The repository owns every Python query against ``sync_runs``. Runtime callers
keep their existing fail-soft policy; this class deliberately raises database
errors so the caller can either continue auto-sync or report a component
failure without losing the underlying signal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import sql

from ..base.repository import DatabaseRepository
from ..base.postgres import Operation
from .sync_run_insert_normalization import start_sync_run, finish_insert_sync_run
from .sync_run_write_normalization import update_sync_run
from .sync_run_recovery import recover_stale_sync_runs
from ..base.sql_values import assignments, insert_values, projection
from .sync_run_summary import project_sync_run_summary
from operational_copy_policy import safe_summary_copy


SYNC_RUNS_TABLE = "sync_runs"
SYNC_RUN_COLUMNS = ",".join(
    (
        "id",
        "run_type",
        "status",
        "started_at",
        "finished_at",
        "duration_seconds",
        "errors",
        "metadata_processed",
        "metadata_updated",
        "video_count_changed",
        "discovery_processed",
        "new_videos",
        "new_video_stats_updated",
        "regular_stats_requested",
        "regular_stats_updated",
        "stats_updated",
        "unavailable_marked",
        "missing",
        "summary_json",
    )
)
SYNC_RUN_PAGE_SIZE = 1_000
MAX_SYNC_RUN_ROWS = 10_000
SYNC_RUN_CHANGE_COLUMNS = (
    "metadata_updated",
    "video_count_changed",
    "new_videos",
    "new_video_stats_updated",
    "regular_stats_updated",
    "stats_updated",
    "unavailable_marked",
)


def _response_rows(data: Any) -> list[dict[str, Any]]:
    data = data or []
    if not isinstance(data, list):
        raise TypeError("sync_runs query returned non-list data")
    rows = []
    for row in data:
        if not isinstance(row, Mapping):
            raise TypeError("sync_runs query returned a non-object row")
        projected = dict(row)
        if "summary_json" in projected:
            projected["summary_json"] = safe_summary_copy(project_sync_run_summary(projected))
        rows.append(projected)
    return rows


def _bounded_limit(value: int, *, maximum: int = MAX_SYNC_RUN_ROWS) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("sync run limit must be an integer") from None
    return max(0, min(parsed, maximum))


class SyncRunRepository(DatabaseRepository):
    """Named owner of sync-run writes and read models."""

    def start(self, values: Mapping[str, Any]) -> int | None:
        values = dict(values)
        insert_values([values], json_columns={"summary_json"})
        with self.database.transaction(Operation("sync_run_start", total_timeout_ms=60_000)) as tx:
            return start_sync_run(tx, values)

    def _update(self, run_id, values, operation):
        values = dict(values)
        from deployment_profile import selected_deployment_profile
        if selected_deployment_profile() == "self-hosted":
            values.pop("started_at", None)
        if not values:
            return
        assignments(values, json_columns={"summary_json"})
        with self.database.transaction(Operation(operation, total_timeout_ms=60_000)) as tx:
            update_sync_run(tx, run_id, values)

    def update_progress(self, run_id: int | None, values: Mapping[str, Any]) -> bool:
        if not run_id:
            return False
        self._update(run_id, values, "sync_run_progress")
        return True

    def finish(self, run_id: int | None, values: Mapping[str, Any]) -> bool:
        if not run_id:
            values = dict(values)
            insert_values([values], json_columns={"summary_json"})
            with self.database.transaction(Operation("sync_run_finish_insert", total_timeout_ms=60_000)) as tx:
                finish_insert_sync_run(tx, values)
        else:
            self._update(run_id, values, "sync_run_finish")
        return True

    def recover_stale_running(self, before: str, values: Mapping[str, Any]) -> int:
        before = str(before or "").strip()
        if not before:
            raise ValueError("stale sync run cutoff is required")
        finished_at = values.get("finished_at")
        with self.database.transaction(Operation("recover_stale_sync_runs", total_timeout_ms=60_000)) as tx:
            return recover_stale_sync_runs(tx, before=before, finished_at=finished_at)

    def _paged(self, statement, parameters=(), *, limit: int) -> list[dict[str, Any]]:
        limit = _bounded_limit(limit)
        rows: list[dict[str, Any]] = []
        while len(rows) < limit:
            page_size = min(SYNC_RUN_PAGE_SIZE, limit - len(rows))
            page = _response_rows(self.admit_source_deadlines(self.database.read_rows("sync_run_list",
                statement + sql.SQL(" ORDER BY started_at DESC,id DESC LIMIT %s OFFSET %s"),
                tuple(parameters) + (page_size, len(rows)), max_rows=page_size,
            )))
            rows.extend(page)
            if len(page) < page_size:
                break
        return rows

    def _select(self):
        return sql.SQL("SELECT {} FROM readable.sync_runs").format(projection(SYNC_RUN_COLUMNS+",source_expires_at"))

    def list_recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._paged(self._select(), limit=limit)

    def list_since(self, since: str, *, limit: int = MAX_SYNC_RUN_ROWS) -> list[dict[str, Any]]:
        since = str(since or "").strip()
        if not since:
            raise ValueError("sync run since timestamp is required")
        return self._paged(self._select() + sql.SQL(" WHERE started_at>=%s::timestamptz"), (since,), limit=limit)

    def latest(self) -> dict[str, Any] | None:
        rows = self.list_recent(limit=1)
        return rows[0] if rows else None

    def latest_completed(self) -> dict[str, Any] | None:
        # Historical completed rows retain their previous eligibility.
        rows = self._paged(self._select() + sql.SQL(" WHERE status='completed'"), limit=1)
        return rows[0] if rows else None

    def latest_completed_with_changes(self) -> dict[str, Any] | None:
        # A partial run may have durable changes without being a complete sync.
        changes = sql.SQL(" OR ").join(sql.SQL("{}>0").format(projection(column)) for column in SYNC_RUN_CHANGE_COLUMNS)
        rows = self._paged(self._select() + sql.SQL(" WHERE status=ANY(%s::text[]) AND errors=0 AND ({})").format(changes),
                           (["completed", "partial"],), limit=1)
        return rows[0] if rows else None
