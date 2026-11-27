"""Typed acquisition results: value states, entity states, provenance.

Values live only in request memory. ``to_wire`` is the one JSON projection of a
result: large integers and decimals travel as strings, datetimes as UTC ISO.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

MAX_SAFE_INTEGER = 2**53 - 1
MAX_UNKNOWN_PATHS = 50


class ValueState(StrEnum):
    KNOWN = "known"                  # present and valid; 0 and false are known values
    KNOWN_EMPTY = "known_empty"      # "" or [] (or {}) sent explicitly
    NULL_REPORTED = "null_reported"  # explicit null; treated as unknown by default
    ABSENT = "absent"                # key missing although its part was requested
    NOT_REQUESTED = "not_requested"  # the part was not requested
    OWNER_ONLY = "owner_only"        # visible only to the owner; key context
    UNKNOWN_ENUM = "unknown_enum"    # outside the documented values; raw value in memory only
    INVALID = "invalid"              # failed the wire type or the owning normalizer


class EntityState(StrEnum):
    PRESENT = "present"
    OMITTED = "omitted"              # a requested ID did not come back: not deletion proof
    FAILED = "failed"                # videos.batchGetStats summary.failedVideoIds
    NOT_FOUND = "not_found"          # 404 with an exact reason (or the documented empty lookup)
    ACCESS_DENIED = "access_denied"  # 403 about this object
    DISABLED = "disabled"            # 403 commentsDisabled, liveChatDisabled
    ENDED = "ended"                  # 403 liveChatEnded: the chat existed and is no longer live


class MapValue(tuple):
    """Sorted (key, value) pairs of a map field such as localizations.{key}.title."""

    __slots__ = ()


class ElementValues(tuple):
    """One FieldValue per element of an array of objects (replies.comments[].id): states stay per element."""

    __slots__ = ()


class Completeness(StrEnum):
    COMPLETE = "complete"            # the provider reported the end
    PARTIAL = "partial"              # stopped earlier; see stop_reason


@dataclass(frozen=True)
class FieldValue:
    state: ValueState
    value: object | None = None
    absence_meaning: str | None = None


@dataclass(frozen=True)
class Row:
    unit: str
    identity: tuple[str, str] | None
    fields: Mapping[str, FieldValue]
    page_index: int
    source_position: int | None = None


@dataclass(frozen=True)
class PageProvenance:
    method_id: str
    registry_revision: int
    plan_fingerprint: str
    page_index: int
    token_chain_sha256: str
    parts: tuple[str, ...]
    fields_mask_sha256: str
    request_started_at: datetime
    response_date: datetime | None
    http_status: int
    quota_bucket: str
    quota_units: int
    request_attempt_id: str | None
    context: str
    unknown_field_paths: tuple[str, ...] = ()
    rejected_items: int = 0
    estimated_total_results: int | None = None


@dataclass(frozen=True)
class AcquisitionResult:
    method_id: str
    unit: str
    rows: tuple[Row, ...]
    entities: Mapping[str, EntityState]
    pages: tuple[PageProvenance, ...]
    completeness: Completeness
    stop_reason: str | None
    order: str
    cost: Mapping[str, int]
    continuation: str | None = None
    retry_after_seconds: int | None = None
    plan_fingerprint: str = ""
    requests_sent: int = 0
    duplicate_rows_dropped: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


class PageParse:
    """Rows of one provider page (JSON, Analytics table or media body) before budgets apply."""
    __slots__ = ("rows", "returned_ids", "unknown_field_paths", "rejected_items", "item_count", "payload")

    def __init__(self, rows, returned_ids, unknown, rejected, item_count, payload):
        self.rows, self.returned_ids = rows, returned_ids
        self.unknown_field_paths, self.rejected_items = unknown, rejected
        self.item_count, self.payload = item_count, payload


def frozen_mapping(values):
    return MappingProxyType(dict(values))


def iso_utc(value):
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def wire_value(value):
    """Exact JSON: never a float; integers beyond 2**53 - 1 become decimal strings."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER else str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, MapValue):
        return {key: wire_value(item) for key, item in value}
    if isinstance(value, ElementValues):
        return [field_wire(item) for item in value]
    if isinstance(value, (tuple, list)):
        return [wire_value(item) for item in value]
    raise TypeError("unsupported acquisition value")


def field_wire(value):
    result = {"state": value.state.value}
    if value.value is not None:
        result["value"] = wire_value(value.value)
    if value.absence_meaning is not None:
        result["absence"] = value.absence_meaning
    return result


def row_wire(row):
    return {"identity": None if row.identity is None else {"kind": row.identity[0], "id": row.identity[1]},
            "page": row.page_index,
            **({"source_position": row.source_position} if row.source_position is not None else {}),
            "fields": {path: field_wire(value) for path, value in sorted(row.fields.items())}}


def page_wire(page):
    return {
        "page": page.page_index, "registry_revision": page.registry_revision,
        "plan_fingerprint": page.plan_fingerprint, "token_chain_sha256": page.token_chain_sha256,
        "parts": list(page.parts), "fields_mask_sha256": page.fields_mask_sha256,
        "request_started_at": iso_utc(page.request_started_at), "response_date": iso_utc(page.response_date),
        "http_status": page.http_status, "quota_bucket": page.quota_bucket, "quota_units": page.quota_units,
        "request_attempt_id": page.request_attempt_id, "context": page.context,
        "unknown_field_paths": list(page.unknown_field_paths), "rejected_items": page.rejected_items,
        **({"estimated_total_results": page.estimated_total_results} if page.estimated_total_results is not None else {}),
    }


def to_wire(result, *, max_bytes=None):
    """One JSON object; rows are trimmed (and the result marked partial) to fit ``max_bytes``."""
    import json
    value = {
        "version": 1, "method": result.method_id, "unit": result.unit,
        "completeness": result.completeness.value, "stop_reason": result.stop_reason, "order": result.order,
        "cost": dict(sorted(result.cost.items())), "requests_sent": result.requests_sent,
        "plan_fingerprint": result.plan_fingerprint,
        "entities": {key: state.value for key, state in sorted(result.entities.items())},
        "pages": [page_wire(page) for page in result.pages],
        "rows": [row_wire(row) for row in result.rows],
        "continuation": result.continuation, "retry_after_seconds": result.retry_after_seconds,
        "duplicate_rows_dropped": result.duplicate_rows_dropped, "notes": list(result.notes),
    }
    if max_bytes is None:
        return value
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    rows, size = [], len(encoded) - sum(len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()) + 1
                                        for row in value["rows"]) + 256
    for row in value["rows"]:
        cost = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        if size + cost > max_bytes:
            break
        rows.append(row)
        size += cost
    value.update(rows=rows, completeness="partial", stop_reason="output_budget",
                 notes=value["notes"] + [f"rows_trimmed:{len(value['rows']) - len(rows)}"])
    return value
