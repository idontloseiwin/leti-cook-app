"""Base contract for domain-specific database repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from hot_storage import utc_now, utc_instant, register_read_deadline, HotSourceReadExpired

from .client import DatabaseGateway


class DatabaseRepository:
    """Small shared base; business queries stay in named domain repositories."""

    def __init__(
        self, database: DatabaseGateway, *, read_clock: Callable[[], datetime] | None = None,
    ):
        self.database = database
        self._read_clock = read_clock

    def read_now(self) -> datetime:
        """Trusted wall clock for source expiry, independent from request cursors."""
        return utc_instant(self._read_clock() if self._read_clock is not None else utc_now())

    def admit_source_deadlines(self, rows):
        """Keep a view's earliest witness deadline through response/cache assembly."""
        now = self.read_now()
        for row in rows:
            value = row.get("source_expires_at")
            if value is not None:
                deadline = utc_instant(value)
                if deadline <= now:
                    raise HotSourceReadExpired("Stored source expired during read")
                register_read_deadline(deadline)
        return rows
