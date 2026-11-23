"""Fail-soft telemetry facade used by application producers.

Emission is bounded and non-blocking: callers only validate an allowlisted
event and enqueue it with ``put_nowait``.  The JSONL sink and its file locks run
on a daemon writer thread.  Reading is served here for the telemetry
routes of the core application.

Switched off until telemetry is redesigned: while
``pipeline_telemetry_on()`` is false every function here returns at once,
without an emitter, lock, thread or journal file.
"""

from __future__ import annotations

from typing import Any

from telemetry_contract import utc_now_iso
import telemetry_sink as _sink
from telemetry_sink import (
    JsonlTelemetryJournal,
    close_emitters,
    configured_max_bytes,
    configured_path,
    configured_recent_limit,
    default_emitter,
    fcntl,
)


def _off() -> bool:
    try:
        return not _sink.pipeline_telemetry_on()
    except Exception:
        return True


def _off_snapshot():
    # The journal is neither read nor created while telemetry is off.
    return {"events": [], "position": 0, "generation": None, "size": 0,
            "read_error": False, "off": True}


def telemetry_path():
    return configured_path()


def _journal() -> JsonlTelemetryJournal:
    # Configuration is deliberately lazy.  Several CLI paths import telemetry
    # before calling load_dotenv().
    return JsonlTelemetryJournal(
        configured_path(),
        max_bytes=configured_max_bytes(),
    )


def emit_event(
    kind,
    source,
    target,
    operation,
    *,
    phase=None,
    direction=None,
    status=None,
    count=None,
    quota=None,
    duration_ms=None,
    http_status=None,
    detail=None,
    **trace,
):
    """Submit one event without allowing telemetry failure into domain work."""

    if _off():
        return None
    try:
        return default_emitter().emit(
            kind,
            source,
            target,
            operation,
            phase=phase,
            direction=direction,
            status=status,
            count=count,
            quota=quota,
            duration_ms=duration_ms,
            http_status=http_status,
            detail=detail,
            **trace,
        )
    except Exception:
        return None


def emit_phase(phase, status, *, detail=None):
    return emit_event(
        "phase",
        "sync_engine",
        "sync_engine",
        phase,
        phase=phase,
        direction="internal",
        status=status,
        detail=detail,
    )


def is_enabled() -> bool:
    """Cheap fail-soft gate for optional observers; never starts the writer."""
    if _off():
        return False
    try:
        return default_emitter().enabled is True
    except Exception:
        return False


def flush_telemetry(timeout: float = 1.0) -> bool:
    if _off():
        return True
    try:
        return default_emitter().flush(timeout)
    except Exception:
        return False


def _empty_metrics(sink_errors: int) -> dict[str, Any]:
    return {
        "emitted": 0,
        "written": 0,
        "dropped": 0,
        "evicted_low_priority": 0,
        "invalid": 0,
        "sink_errors": sink_errors,
        "queued": 0,
        "queue_limit": 0,
    }


def telemetry_metrics() -> dict[str, Any]:
    if _off():
        return {**_empty_metrics(0), "off": True}
    try:
        return default_emitter().metrics()
    except Exception:
        return _empty_metrics(1)


def telemetry_file_state():
    if _off():
        return {key: value for key, value in _off_snapshot().items() if key != "events"}
    flush_telemetry(1.0)
    return _journal().state()


def telemetry_tail_snapshot(limit=None, *, flush=True):
    # A live stream polls again shortly, so it does not wait up to a second for a flush.
    if _off():
        return _off_snapshot()
    if flush:
        flush_telemetry(1.0)
    limit = max(1, int(limit or configured_recent_limit()))
    return _journal().tail_snapshot(limit=limit)


def read_events_since(position, generation, limit=None, *, flush=True):
    if _off():
        return {**_off_snapshot(), "rotated": False}
    if flush:
        flush_telemetry(1.0)
    return _journal().read_since(position, generation, limit=limit)


def recent_events(limit=None):
    return telemetry_tail_snapshot(limit=limit)["events"]


def shutdown_telemetry(timeout: float = 0.75) -> None:
    close_emitters(timeout)
