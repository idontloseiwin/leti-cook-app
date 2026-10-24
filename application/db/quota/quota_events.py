"""Atomic quota journal delivery, preserving persist_youtube_quota_events.

The disk journal's _encode_event is intentionally not reused: it bounds each
event at 8 KiB and uses Python datetime/strict JSON validation. This boundary
retains the SQL protocol's raw, pre-normalization jsonb::text 256 KiB batch
bound and PostgreSQL casts (including calendar/range validation). Diagnostic
vocabulary has one Python owner, operational_copy_policy.quota_label. Source
writes carry those normalized labels; no transport or operation is retried.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import re

from operational_copy_policy import quota_label

from ..base.postgres import DatabaseError, DatabaseServerError, Operation


MAX_EVENTS = 100
MAX_BATCH_BYTES = 262_144
_IMMUTABLE = (
    "request_attempt_id", "project_id", "quota_bucket", "quota_day", "phase",
    "endpoint", "quota_units", "started_at", "retry_of_request_attempt_id",
)
_TERMINAL = ("status", "finished_at", "duration_ms", "http_status", "error_type")
_LABELS = ("phase", "endpoint", "quota_bucket", "error_type")
_ENVELOPE = frozenset(("kind", "request_attempt_id", "project_id", "values"))
_START = frozenset(_IMMUTABLE + ("status",))
_REQUIRED_START = _START - {"retry_of_request_attempt_id"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,119}\Z")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
_OPERATION = Operation("persist_youtube_quota_events", statement_timeout_ms=15_000,
                       lock_timeout_ms=2_000, total_timeout_ms=15_000)


def _projection(alias, fields):
    # Fixed internal columns only. Canonical PG JSON clock strings preserve
    # native equality without Python datetime's narrower year range (PG accepts
    # e.g. 9999-12-31T24:00:00). Both sides use the transaction's UTC timezone.
    return ", ".join(f"to_jsonb({alias}.{field}) #>> '{{}}' AS {field}"
                     if field in {"quota_day", "started_at", "finished_at"}
                     else f"{alias}.{field}" for field in fields)


# jsonb_populate_record forces native casts for EVERY event before any write,
# including terminal replays. The byte count uses the original, unredacted batch.
_PREPARE = """
    SELECT input.ordinality AS event_index, input.event->>'kind' AS event_kind,
           (input.event->>'request_attempt_id')::uuid AS event_id,
           input.event->>'project_id' AS event_project, {columns},
           octet_length(%s::jsonb::text) AS payload_bytes,
           proposed.quota_day =
             (proposed.started_at AT TIME ZONE 'America/Los_Angeles')::date AS day_matches
    FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS input(event, ordinality)
    CROSS JOIN LATERAL jsonb_populate_record(
        NULL::public.youtube_api_request_attempts, input.event->'values') AS proposed
    ORDER BY input.ordinality
""".format(columns=_projection("proposed", _IMMUTABLE + _TERMINAL))
_INSERT = """
    INSERT INTO public.youtube_api_request_attempts
        (request_attempt_id, project_id, quota_bucket, quota_day, phase, endpoint,
         quota_units, status, started_at, retry_of_request_attempt_id)
    SELECT proposed.request_attempt_id, proposed.project_id, proposed.quota_bucket,
           proposed.quota_day, proposed.phase, proposed.endpoint, proposed.quota_units,
           proposed.status, proposed.started_at, proposed.retry_of_request_attempt_id
    FROM jsonb_array_elements(%s::jsonb) AS input(event)
    CROSS JOIN LATERAL jsonb_populate_record(
        NULL::public.youtube_api_request_attempts, input.event->'values') AS proposed
    WHERE input.event->>'kind' = 'start'
    ORDER BY proposed.request_attempt_id
    ON CONFLICT (request_attempt_id) DO NOTHING
"""
_LOCK = """
    SELECT {columns}
    FROM public.youtube_api_request_attempts AS ledger
    WHERE ledger.request_attempt_id = ANY(%s::uuid[])
    ORDER BY ledger.request_attempt_id FOR UPDATE OF ledger
""".format(columns=_projection("ledger", _IMMUTABLE + _TERMINAL))
_PARENTS = """
    SELECT request_attempt_id, project_id FROM public.youtube_api_request_attempts
    WHERE request_attempt_id = ANY(%s::uuid[])
    ORDER BY request_attempt_id
"""
_UPDATE = """
    UPDATE public.youtube_api_request_attempts AS ledger
    SET status = proposed.status, finished_at = proposed.finished_at,
        duration_ms = proposed.duration_ms, http_status = proposed.http_status,
        error_type = proposed.error_type, phase = proposed.phase,
        endpoint = proposed.endpoint, quota_bucket = proposed.quota_bucket
    FROM jsonb_populate_recordset(NULL::public.youtube_api_request_attempts,
                                 %s::jsonb) AS proposed
    WHERE ledger.request_attempt_id = proposed.request_attempt_id
      AND ledger.status = 'started'
"""
_READ_UPDATED = """
    SELECT {columns} FROM public.youtube_api_request_attempts AS ledger
    WHERE ledger.request_attempt_id = ANY(%s::uuid[])
    ORDER BY ledger.request_attempt_id
""".format(columns=_projection("ledger", ("request_attempt_id",) + _TERMINAL))


def _reject(code="quota_events_invalid"):
    # Same public message and SQLSTATE as the old RAISE EXCEPTION contract.
    raise DatabaseServerError(code, code="P0001")


def quota_event_copy(event):
    """Copy only eligible label values, retaining invalid types/extra fields.

    Temporary location until operational-copy helpers are consolidated. This is
    the hot_quota_event_copy shape contract, not another label vocabulary.
    """
    if not isinstance(event, Mapping):
        return event
    result = dict(event)
    if not isinstance(result.get("values"), Mapping):
        return result
    values = result["values"] = dict(result["values"])
    for field in _LABELS:
        if field in values and (isinstance(values[field], str)
                                or (field == "error_type" and values[field] is None)):
            values[field] = quota_label(field, values[field])
    return result


def _matches(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _integer(value, lower, upper=9_999_999_999):
    # The protocol checks JSON number spelling first; native int32 range is PG's.
    return type(value) is int and lower <= value <= upper


def _validate(event):
    if (not isinstance(event, dict) or set(event) != _ENVELOPE
            or not isinstance(event["kind"], str) or event["kind"] not in {"start", "finish"}
            or not _matches(event["request_attempt_id"], _UUID)
            or not _matches(event["project_id"], _PROJECT)
            or not isinstance(event["values"], dict)):
        _reject()
    values = event["values"]
    if event["kind"] == "start":
        if (not _REQUIRED_START <= set(values) <= _START
                or any(values.get(field) != event[field] for field in ("request_attempt_id", "project_id"))
                or values.get("status") != "started"
                or any(not _matches(values.get(field), _LABEL) for field in ("quota_bucket", "phase", "endpoint"))
                or not _integer(values.get("quota_units"), 1)
                or not _matches(values.get("quota_day"), _DAY)
                or not _matches(values.get("started_at"), _STAMP)):
            _reject()
        retry = values.get("retry_of_request_attempt_id")
        if retry is not None and (not _matches(retry, _UUID) or retry == event["request_attempt_id"]):
            _reject()
    elif (set(values) != set(_TERMINAL)
          or values.get("status") not in ("success", "http_error", "transport_error")
          or not _matches(values.get("finished_at"), _STAMP)
          or not _integer(values.get("duration_ms"), 0)
          or (values.get("http_status") is not None and not _integer(values["http_status"], 100, 599))
          or (values.get("error_type") is not None and not _matches(values["error_type"], _LABEL))):
        _reject()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        _reject()


def _canonical_row(row):
    row = dict(row)
    for field in _LABELS:
        if field in row:
            row[field] = quota_label(field, row[field])
    return row


def persist_events(database, events) -> int:
    """Return the full event count only after a successful transaction COMMIT."""
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
        _reject()
    # Mapping inputs are accepted by the public repository, but arbitrary JSON
    # coercion and omitted envelope fields are not. Copy without mutating callers.
    raw = [dict(event, values=dict(event["values"]))
           if isinstance(event, Mapping) and isinstance(event.get("values"), Mapping)
           else event for event in events]
    raw_json = _json(raw)
    canonical = [quota_event_copy(event) for event in raw]
    for event in canonical:
        _validate(event)
    canonical_json = _json(canonical)
    try:
        with database.transaction(_OPERATION) as tx:
            proposed = tx.fetch_all(_PREPARE, (raw_json, canonical_json), max_rows=len(events))
            if len(proposed) != len(events):
                raise DatabaseError("database_result_shape_invalid")
            for index, row in enumerate(proposed, 1):
                if row["event_index"] != index:
                    raise DatabaseError("database_result_shape_invalid")
                if row["payload_bytes"] > MAX_BATCH_BYTES or (
                        row["event_kind"] == "start" and row["day_matches"] is not True):
                    _reject()
            # One statement is essential: an immediate self-FK accepts a retry
            # whose parent start occurs later in this same input batch.
            tx.execute(_INSERT, (canonical_json,))
            ids = sorted({row["event_id"] for row in proposed})
            locked = tx.fetch_all(_LOCK, (ids,), max_rows=len(ids))
            existing = {row["request_attempt_id"]: _canonical_row(row) for row in locked}
            # Read parent identity AFTER lock acquisition, matching the old
            # post-wait snapshot; a join in _LOCK could see pre-wait parent data.
            parents = sorted({existing[row["event_id"]]["retry_of_request_attempt_id"]
                              for row in proposed if row["event_kind"] == "start"
                              and row["event_id"] in existing
                              and existing[row["event_id"]]["retry_of_request_attempt_id"] is not None})
            parent_projects = {row["request_attempt_id"]: row["project_id"]
                               for row in tx.fetch_all(_PARENTS, (parents,), max_rows=len(parents))} if parents else {}
            updates = {}
            expected = {}
            for event, row in zip(canonical, proposed):
                current = existing.get(row["event_id"])
                if current is None:
                    _reject("quota_events_missing_start")
                if current["project_id"] != row["event_project"]:
                    _reject("quota_events_conflict")
                if row["event_kind"] == "start":
                    if (any(current[field] != row[field] for field in _IMMUTABLE)
                            or (current["retry_of_request_attempt_id"] is not None
                                and parent_projects.get(current["retry_of_request_attempt_id"]) != current["project_id"])):
                        _reject("quota_events_conflict")
                elif current["status"] == "started":
                    current.update({field: row[field] for field in _TERMINAL})
                    # The old BEFORE UPDATE trigger normalizes the entire NEW
                    # row, including immutable labels inherited from storage.
                    # Carry those already-canonical locked values only on an
                    # actual started->terminal update; replays must stay no-op.
                    updates[row["event_id"]] = dict(
                        event["values"], request_attempt_id=event["request_attempt_id"],
                        **{field: current[field] for field in ("phase", "endpoint", "quota_bucket")})
                    expected[row["event_id"]] = tuple(current[field] for field in _TERMINAL)
                elif any(current[field] != row[field] for field in _TERMINAL):
                    _reject("quota_events_conflict")
            if updates:
                ordered = sorted(updates)
                changed = tx.execute(_UPDATE, (_json([updates[key] for key in ordered]),))
                if changed != len(updates):
                    _reject("quota_events_conflict")
                # Observe actual retained-trigger output before acknowledging.
                stored = tx.fetch_all(_READ_UPDATED, (ordered,), max_rows=len(ordered))
                actual = {row["request_attempt_id"]: tuple(_canonical_row(row)[field] for field in _TERMINAL)
                          for row in stored}
                if actual != expected:
                    _reject("quota_events_conflict")
    except DatabaseServerError as error:
        if error.code and (error.code.startswith("22") or error.code in {"23514", "23502"}):
            _reject()
        if error.code == "23503":
            _reject("quota_events_missing_start")
        raise
    return len(events)
