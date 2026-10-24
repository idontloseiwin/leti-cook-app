"""Versioned, privacy-bounded telemetry event contract.

Domain code must use :mod:`telemetry`, not this module directly.  The contract
is intentionally small: telemetry is diagnostic and must never become a raw
payload or exception transport.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from deployment_profile import selected_deployment_profile


SCHEMA_VERSION = 2
DEFAULT_EVENT_MAX_BYTES = 4096
MIN_EVENT_MAX_BYTES = 1024
MAX_EVENT_MAX_BYTES = 16384

REQUIRED_FIELDS = ("id", "ts", "kind", "source", "target", "operation")
OPTIONAL_FIELDS = (
    "phase",
    "direction",
    "status",
    "count",
    "quota",
    "duration_ms",
    "http_status",
    "detail",
)
TRACE_FIELDS = (
    "trace_id", "span_id", "parent_span_id", "operation_id", "sync_run_id", "job_id",
    "video_id", "channel_id", "batch_id", "request_attempt_id", "retry_of_request_attempt_id",
    "reason", "error_code", "error_type", "due_at", "observed_at", "requested_count", "received_count", "missing_count",
    "quota_bucket", "video_type", "attempt", "inflight", "video_ids", "item_index", "endpoint", "retry_after_seconds",
    "writer_id", "sequence", "dropped_total", "invalid_total", "sink_errors_total", "monotonic_ns",
)
ALLOWED_FIELDS = frozenset(("schema_version",) + REQUIRED_FIELDS + OPTIONAL_FIELDS + TRACE_FIELDS)
_TRACE_INTEGER_FIELDS = frozenset((
    "sync_run_id", "requested_count", "received_count", "missing_count", "attempt",
    "inflight", "item_index", "sequence", "dropped_total", "invalid_total", "sink_errors_total", "monotonic_ns", "retry_after_seconds",
))
_TRACE_UUID_FIELDS = frozenset(("trace_id", "span_id", "parent_span_id", "writer_id"))
_TRACE_CODE_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,96}\Z")
_TRACE_SECRET_RE = re.compile(r"(?i)(?:AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]+\.|bearer|password|api[_-]?key|secret|token[=:])")
_TRACE_ENDPOINTS = frozenset(("videos", "videos.list", "channels", "channels.list", "playlistItems", "playlistItems.list", "playlists", "playlists.list", "search", "search.list", "videos:batchGetStats", "videoCategories", "videoCategories.list", "i18nLanguages", "i18nLanguages.list"))

_IDENTIFIER_MAX_CHARS = 160
_DETAIL_MAX_CHARS = 640
_NUMBER_ABS_MAX = 10**15
_SPACE_RE = re.compile(r"[\r\n\t]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|key|authorization|password|secret|token)"
    r"[A-Za-z0-9_]*)\b"
    r"\s*([:=])\s*([^\s,;&]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


class TelemetryContractError(ValueError):
    """The producer attempted to emit data outside the telemetry contract."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


def event_max_bytes(value: Any = None) -> int:
    return _bounded_int(
        value,
        default=DEFAULT_EVENT_MAX_BYTES,
        minimum=MIN_EVENT_MAX_BYTES,
        maximum=MAX_EVENT_MAX_BYTES,
    )


def redact_text(value: str, *, max_chars: int) -> str:
    text = _SPACE_RE.sub(" ", value).strip()
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)] + "…"
    return text


def _text(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise TelemetryContractError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise TelemetryContractError(f"{field} must be a string")
    limit = _DETAIL_MAX_CHARS if field == "detail" else _IDENTIFIER_MAX_CHARS
    normalized = redact_text(value, max_chars=limit)
    if required and not normalized:
        raise TelemetryContractError(f"{field} cannot be empty")
    return normalized


def _number(value: Any, field: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TelemetryContractError(f"{field} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise TelemetryContractError(f"{field} must be finite")
    if abs(value) > _NUMBER_ABS_MAX:
        raise TelemetryContractError(f"{field} is out of range")
    if field == "http_status":
        if not isinstance(value, int) or not 100 <= value <= 599:
            raise TelemetryContractError("http_status must be between 100 and 599")
    return value


def trace_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Accept operational identities, codes and counts; never payloads or exceptions."""
    result = {}
    for key, value in values.items():
        if key not in TRACE_FIELDS:
            raise TelemetryContractError("unknown trace field")
        if value is None:
            continue
        if key in _TRACE_INTEGER_FIELDS:
            if isinstance(value, str) and value.isascii() and value.isdecimal():
                value = int(value)
            maximum = 2**53 - 1 if key == "retry_after_seconds" else 2**63 - 1
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise TelemetryContractError("trace integer outside bounds")
        elif key == "job_id":
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
                pass
            elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}", value):
                pass
            else:
                raise TelemetryContractError("invalid job identifier")
        elif key in _TRACE_UUID_FIELDS:
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
                raise TelemetryContractError("invalid trace identifier")
        elif key == "video_ids":
            if not isinstance(value, (list, tuple)) or len(value) > 50:
                raise TelemetryContractError("invalid video manifest")
            value = list(value)
            if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", item) for item in value):
                raise TelemetryContractError("invalid video identity")
        elif key == "video_id":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
                raise TelemetryContractError("invalid video identity")
        elif key == "channel_id":
            if not isinstance(value, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
                raise TelemetryContractError("invalid channel identity")
        elif key in {"due_at", "observed_at"}:
            if isinstance(value, datetime):
                value = value.isoformat()
            if not isinstance(value, str) or len(value) > 40:
                raise TelemetryContractError("invalid trace timestamp")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise TelemetryContractError("invalid trace timestamp") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise TelemetryContractError("trace timestamp requires timezone")
            value = parsed.astimezone(timezone.utc).isoformat()
        else:
            if not isinstance(value, str) or not _TRACE_CODE_RE.fullmatch(value) or _TRACE_SECRET_RE.search(value):
                raise TelemetryContractError("invalid trace code")
            if key == "endpoint" and value not in _TRACE_ENDPOINTS:
                raise TelemetryContractError("unknown provider endpoint")
            if key == "quota_bucket" and value not in {"general", "stats", "General", "Stats", "video_batch_get_stats"}:
                raise TelemetryContractError("unknown quota bucket")
            if key == "video_type" and value not in {"long", "short", "shorts", "stream", "streams", "unknown"}:
                raise TelemetryContractError("unknown video type")
        result[key] = value
    return result


def event_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"


def encode_event(event: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(event),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_event(
    kind: Any,
    source: Any,
    target: Any,
    operation: Any,
    *,
    phase: Any = None,
    direction: Any = None,
    status: Any = None,
    count: Any = None,
    quota: Any = None,
    duration_ms: Any = None,
    http_status: Any = None,
    detail: Any = None,
    event_id_value: Any = None,
    timestamp: Any = None,
    max_bytes: Any = None,
    **trace: Any,
) -> dict[str, Any]:
    """Build one schema-v2 event or raise ``TelemetryContractError``.

    Arbitrary mappings, lists, exceptions, and objects are rejected rather than
    stringified.  This is the privacy boundary for every producer.
    """

    # Self-hosted diagnostics cannot become an unclocked source-payload copy.
    # Keep operational envelope/counters; free-form detail is private-only.
    if detail is not None and selected_deployment_profile() == "self-hosted":
        detail = None

    event = {
        "schema_version": SCHEMA_VERSION,
        "id": _text(event_id_value or event_id(), "id", required=True),
        "ts": _text(timestamp or utc_now_iso(), "ts", required=True),
        "kind": _text(kind, "kind", required=True),
        "source": _text(source, "source", required=True),
        "target": _text(target, "target", required=True),
        "operation": _text(operation, "operation", required=True),
    }
    for key, value in (
        ("phase", phase),
        ("direction", direction),
        ("status", status),
        ("detail", detail),
    ):
        normalized = _text(value, key)
        if normalized is not None:
            event[key] = normalized
    for key, value in (
        ("count", count),
        ("quota", quota),
        ("duration_ms", duration_ms),
        ("http_status", http_status),
    ):
        normalized = _number(value, key)
        if normalized is not None:
            event[key] = normalized

    if event["kind"] == "pipeline":
        for key in ("operation", "phase", "status"):
            value = event.get(key)
            if value is not None and (not _TRACE_CODE_RE.fullmatch(value) or _TRACE_SECRET_RE.search(value)):
                raise TelemetryContractError("invalid pipeline code")
        event.pop("detail", None)
    event.update(trace_fields(trace))
    encoded = encode_event(event)
    byte_limit = event_max_bytes(max_bytes)
    if len(encoded) > byte_limit and "detail" in event:
        # Detail is the only descriptive field.  Shrink it deterministically
        # before rejecting an otherwise valid event.
        overage = len(encoded) - byte_limit
        detail_value = str(event["detail"])
        keep = max(0, len(detail_value.encode("utf-8")) - overage - 4)
        event["detail"] = redact_text(detail_value, max_chars=keep)
        if not event["detail"]:
            event.pop("detail", None)
        encoded = encode_event(event)
    if len(encoded) > byte_limit:
        raise TelemetryContractError("event exceeds TELEMETRY_EVENT_MAX_BYTES")
    return event


def normalize_stored_event(value: Any, *, max_bytes: Any = None) -> dict[str, Any] | None:
    """Normalize v0/v1/v2 journal rows into the current bounded envelope."""

    if not isinstance(value, Mapping):
        return None
    if any(key not in ALLOWED_FIELDS for key in value):
        return None
    version = value.get("schema_version", SCHEMA_VERSION)
    if version not in (None, 0, 1, SCHEMA_VERSION):
        return None
    try:
        return build_event(
            value.get("kind"),
            value.get("source"),
            value.get("target"),
            value.get("operation"),
            phase=value.get("phase"),
            direction=value.get("direction"),
            status=value.get("status"),
            count=value.get("count"),
            quota=value.get("quota"),
            duration_ms=value.get("duration_ms"),
            http_status=value.get("http_status"),
            # A standalone reader has no runtime profile and must never revive
            # free-form source content from historical rows.
            detail=None,
            event_id_value=value.get("id"),
            timestamp=value.get("ts"),
            max_bytes=max_bytes,
            **{key: value[key] for key in TRACE_FIELDS if key in value},
        )
    except TelemetryContractError:
        return None
