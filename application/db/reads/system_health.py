"""Finite operational-health reads; no general query-builder interface."""
from __future__ import annotations

from typing import Any, Mapping, Sequence
from psycopg import sql

from ..base.repository import DatabaseRepository
from ..channel.channel_discovery_projection import CHANNEL_READ_RELATION
from . import operational_read_queries as operational_reads
from ..base.sql_values import projection

# The single paginated caller is youtube_pipeline_health.build_report's channel
# health scan. New scenarios require a named reviewed query here.
CHANNEL_HEALTH_COLUMNS = ",".join((
    "channel_id", "is_hidden", "collection_permission", "collection_control_epoch",
    "collection_mode", "initial_long_inventory_status", "channel_discovery_status",
    "channel_discovery_next_after", "channel_discovery_started_at", "channel_discovery_resume_page_token",
    "channel_discovery_resume_started_at", "channel_discovery_no_new_streak", "channel_metadata_status",
    "channel_metadata_next_after", "channel_metadata_started_at",
))
CHANNEL_HEALTH_ORDER = (("order", ("channel_id",), {"desc": False}),)


class SystemHealthRepository(DatabaseRepository):
    TABLE_NAMES = frozenset({"channels"})
    RPC_NAMES = frozenset({"get_channel_coverage_summary", "get_video_collection_health"})

    def fetch_paginated(self, table_name: str, columns: str, *,
                        operations: Sequence[tuple[str, tuple[Any, ...], Mapping[str, Any]]] = (),
                        page_size: int = 1000, max_rows: int = 100_000) -> tuple[list[dict[str, Any]], bool]:
        if table_name != "channels" or columns != CHANNEL_HEALTH_COLUMNS or tuple(operations) != CHANNEL_HEALTH_ORDER:
            raise ValueError("unsupported system health read scenario")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("system health page_size must be between 1 and 1000")
        if type(max_rows) is not int or max_rows < 0 or max_rows > 100_000:
            raise ValueError("system health max_rows must be between 0 and 100000")
        rows = []
        offset = 0
        truncated = False
        while len(rows) < max_rows:
            current_size = min(page_size, max_rows - len(rows))
            page = self.database.read_rows("system_health_channels",
                sql.SQL("SELECT {} FROM {} ORDER BY channel_id ASC LIMIT %s OFFSET %s").format(projection(CHANNEL_HEALTH_COLUMNS+",source_expires_at"), sql.SQL(CHANNEL_READ_RELATION)),
                (current_size, offset), max_rows=current_size,
            ) or []
            rows.extend(self.admit_source_deadlines(page))
            if len(page) < current_size:
                break
            offset += current_size
            if len(rows) >= max_rows:
                truncated = True
                break
        return rows, truncated

    def fetch_rpc_rows(self, rpc_name: str, params: Mapping[str, Any] | None = None) -> Any:
        if rpc_name not in self.RPC_NAMES:
            raise ValueError(f"unsupported system health RPC: {rpc_name}")
        # Compatibility method name; only these two fixed SQL readers dispatch.
        if params is not None and (not isinstance(params, Mapping) or params):
            raise ValueError("database_function_parameters_invalid")
        reader = (operational_reads.get_channel_coverage_summary if rpc_name == "get_channel_coverage_summary"
                  else operational_reads.get_video_collection_health)
        return reader(self.database) or []
