"""Expiry witnesses for response items, independent of envelope refresh bounds."""
from __future__ import annotations

from datetime import timezone
from typing import Any

from hot_storage import HOT_DATA_WINDOW, capture_read_lifetime, register_read_deadline, utc_instant


def minimum_expiry(*values: Any) -> str | None:
    """Intersect actual source deadlines; never manufacture an acquisition time."""
    deadlines = [utc_instant(value) for value in values if value is not None]
    return min(deadlines).astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if deadlines else None


def source_expiry(row: dict[str, Any] | None, *clock_fields: str) -> str | None:
    """Preserve a view witness and named admitted acquisition clocks."""
    if not row:
        return None
    deadlines = [row.get("source_expires_at")]
    for field in clock_fields:
        value = row.get(field)
        if value is not None:
            deadlines.append(utc_instant(value) + HOT_DATA_WINDOW)
    return minimum_expiry(*deadlines)


def rows_expiry(rows, *clock_fields: str) -> str | None:
    return minimum_expiry(*(source_expiry(row, *clock_fields) for row in rows))


def stamp_aggregate(value: Any, expiry: str | None) -> Any:
    """Mark a deliberately atomic aggregate and its rows, not a mixed envelope."""
    if isinstance(value, dict):
        for item in list(value.values()):
            stamp_aggregate(item, expiry)
        value["source_expires_at"] = minimum_expiry(value.get("source_expires_at"), expiry)
    elif isinstance(value, list):
        for item in value:
            stamp_aggregate(item, expiry)
    return value


def captured_source_call(call, *args, **kwargs):
    """Capture one used label/projection and merge only its successful evidence."""
    with capture_read_lifetime() as lifetime:
        value = call(*args, **kwargs)
    if lifetime.expires_at is not None:
        register_read_deadline(lifetime.expires_at)
    return value, minimum_expiry(lifetime.expires_at)
