"""Read-only persistence boundary for fixed channel-counter dynamics."""

from __future__ import annotations

from typing import Any

from hot_storage import iso_utc, window_bounds

from ..base.client import execute_recoverable_read
from ..base.repository import DatabaseRepository
from psycopg import sql
from ..base.sql_values import identifier


CHANNEL_COUNTER_METRICS = frozenset({"subscribers", "views"})


class ChannelCounterDynamicsRepository(DatabaseRepository):
    """Own the fixed, bounded query shapes used by the 28-day counter contract."""

    def load_channel(self, channel_id: str) -> dict[str, Any] | None:
        row = execute_recoverable_read(
            self.database,
            lambda database: database.read_row(
                "channel_counter_dynamics_load_channel",
                "SELECT channel_id,is_hidden FROM readable.channels_with_control WHERE channel_id = %s LIMIT 1",
                (channel_id,),
            ),
            operation="channel_counter_dynamics.load_channel",
        )
        return self.admit_source_deadlines([dict(row)])[0] if row else None

    def load_latest_snapshot(
        self, channel_id: str, *, recorded_through: str,
    ) -> dict[str, Any] | None:
        bounds = window_bounds(now=self.read_now(), recorded_through=recorded_through)
        if bounds is None:
            return None
        start, end = bounds
        row = execute_recoverable_read(
            self.database,
            lambda database: database.read_row(
                "channel_counter_dynamics_load_latest_snapshot",
                "SELECT recorded_at,subscribers,views,source_expires_at FROM readable.channel_snapshots "
                "WHERE channel_id = %s AND recorded_at >= %s::timestamptz AND recorded_at <= %s::timestamptz "
                "ORDER BY recorded_at DESC LIMIT 1",
                (channel_id, iso_utc(start), iso_utc(end)),
            ),
            operation="channel_counter_dynamics.load_latest_snapshot",
        )
        return self.admit_source_deadlines([dict(row)])[0] if row else None

    def load_metric_baseline(
        self, channel_id: str, *, metric: str, recorded_from: str, recorded_through: str,
    ) -> dict[str, Any] | None:
        if metric not in CHANNEL_COUNTER_METRICS:
            raise ValueError("unsupported channel counter metric")
        bounds = window_bounds(now=self.read_now(), recorded_from=recorded_from,
                               recorded_through=recorded_through)
        if bounds is None:
            return None
        start, end = bounds
        row = execute_recoverable_read(
            self.database,
            lambda database: database.read_row(
                "channel_counter_dynamics_load_metric_baseline",
                sql.SQL(
                    "SELECT recorded_at,subscribers,views,source_expires_at FROM readable.channel_snapshots "
                    "WHERE channel_id = %s AND recorded_at >= %s::timestamptz AND recorded_at <= %s::timestamptz "
                    "AND {} IS NOT NULL ORDER BY recorded_at ASC LIMIT 1"
                ).format(identifier(metric)),
                (channel_id, iso_utc(start), iso_utc(end)),
            ),
            operation="channel_counter_dynamics.load_metric_baseline",
        )
        return self.admit_source_deadlines([dict(row)])[0] if row else None
