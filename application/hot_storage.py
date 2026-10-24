"""Fixed source/read lifetime for the standalone self-hosted distribution.

This module owns read boundaries only. It performs no refresh, deletion, I/O,
or change to a source observation clock.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re


# YouTube allows stored API data for 30 days. We keep it for 29: the last day
# is slack for cleanup. Every SQL literal of this window equals RETENTION_HOURS
# (tests/test_retention_window.py).
RETENTION_HOURS = 696
# Startup catch-up is reserved for sources expired by more than this grace.
STARTUP_CATCHUP_GRACE_HOURS = 1
HOT_DATA_WINDOW = timedelta(hours=RETENTION_HOURS)
HOT_DATA_MAX_AGE = HOT_DATA_WINDOW
# Analytics compare over 28 days, as YouTube does, always inside the retention window.
ANALYTICS_WINDOW = timedelta(days=28)
# YouTube's own limit for stored API data.
YOUTUBE_STORAGE_LIMIT = timedelta(days=30)
_POSTGRES_FRACTIONAL_TIMESTAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\.([0-9]{1,6})(Z|[+-]\d{2}:\d{2})"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_instant(value: datetime | str) -> datetime:
    """Parse a timezone-bearing instant; ambiguous source clocks fail closed."""
    # Trusted read clocks are already builtin UTC datetimes. Keep subclasses
    # on the validating path: they can override timezone behaviour.
    if type(value) is datetime and value.tzinfo is timezone.utc:
        return value
    if isinstance(value, str):
        # PostgreSQL omits trailing fractional zeros. Python 3.10 accepts only
        # three or six digits; padding preserves the exact original instant.
        fractional = _POSTGRES_FRACTIONAL_TIMESTAMP.fullmatch(value)
        if fractional:
            value = fractional[1] + "." + fractional[2].ljust(6, "0") + fractional[3]
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid hot storage timestamp") from error
        if value.tzinfo is timezone.utc:
            return value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("hot storage timestamp requires a timezone")
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime | str) -> str:
    return utc_instant(value).isoformat().replace("+00:00", "Z")


def window_bounds(
    *,
    now: datetime | None = None,
    recorded_from: datetime | str | None = None,
    recorded_through: datetime | str | None = None,
) -> tuple[datetime, datetime] | None:
    """Intersect requested observations with the current 28-day UTC window.

    ``now`` is a trusted server/test clock, never a browser-provided as-of
    value. Requested endpoints may narrow the window, but cannot move it.
    An entirely expired or future request has no readable interval.
    """
    reference = utc_instant(now if now is not None else utc_now())
    lower, upper = reference - HOT_DATA_WINDOW, reference
    if recorded_from is not None:
        lower = max(lower, utc_instant(recorded_from))
    if recorded_through is not None:
        upper = min(upper, utc_instant(recorded_through))
    return (lower, upper) if lower <= upper else None


@dataclass
class ReadLifetime:
    """Earliest source deadline used by one transient read, never a source clock."""
    expires_at: datetime | None = None


_read_lifetime: ContextVar[ReadLifetime | None] = ContextVar("hot_read_lifetime", default=None)


def register_read_deadline(value: datetime) -> None:
    lifetime = _read_lifetime.get()
    if lifetime is not None:
        deadline = utc_instant(value)
        if lifetime.expires_at is None or deadline < lifetime.expires_at:
            lifetime.expires_at = deadline


@contextmanager
def capture_read_lifetime():
    lifetime = ReadLifetime()
    token = _read_lifetime.set(lifetime)
    try:
        yield lifetime
    finally:
        _read_lifetime.reset(token)


def source_is_current(value, *, now: datetime | None = None,
                      valid_until: datetime | None = None) -> bool:
    """Admit a real observation; an optional cache horizon only narrows admission."""
    reference = utc_instant(now if now is not None else utc_now())
    try:
        source = utc_instant(value)
        deadline = source + HOT_DATA_WINDOW
    except (TypeError, ValueError, OverflowError):
        return False
    if not reference - HOT_DATA_WINDOW <= source <= reference:
        return False
    if valid_until is not None and deadline < utc_instant(valid_until):
        return False
    register_read_deadline(deadline)
    return True


class HotSourceReadExpired(RuntimeError):
    """An admitted DB result crossed its deadline before application exposure."""
