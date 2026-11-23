"""Column-backed sync summary codec (storage schema 3).

Writer helpers own normalization inside their existing transaction. The wire
codec accepts decoded JSON values, not native datetime/Decimal objects;
SQL_NULL distinguishes a SQL NULL parameter from JSON null (None). The separate
native text route preserves stored JSONB numbers and uses the caller's
transaction only when PostgreSQL timestamp comparisons are needed. The bit
positions below are a versioned storage contract, not counters.
"""

from copy import deepcopy
from decimal import Decimal
import json
import math
import re

from psycopg.types.json import Jsonb

from operational_copy_policy import (
    SUMMARY_FIELDS, SUMMARY_LABELS, SUMMARY_SECTIONS,
    SUMMARY_PROGRESS_FIELDS, summary_progress_value,
)
from ..base.postgres import DatabaseError, DatabaseServerError

COLUMN_FIELDS = (
    (("errors",), "errors"),
    (("started_at",), "started_at"),
    (("finished_at",), "finished_at"),
    (("channel_metadata", "processed"), "metadata_processed"),
    (("channel_metadata", "updated"), "metadata_updated"),
    (("channel_metadata", "video_count_changed"), "video_count_changed"),
    (("channel_discovery", "processed"), "discovery_processed"),
    (("new_video_stats", "updated"), "new_video_stats_updated"),
    (("regular_video_stats", "requested"), "regular_stats_requested"),
    (("regular_video_stats", "updated"), "regular_stats_updated"),
)


def project_sync_run_summary(row):
    """Restore the existing sparse API shape without storing duplicate totals."""
    summary = row.get("summary_json")
    if not isinstance(summary, dict) or summary.get("summary_schema_version") != 3:
        return deepcopy(summary)
    mask = summary.get("column_fields")
    if type(mask) is not int or not 0 <= mask < (1 << len(COLUMN_FIELDS)):
        raise ValueError("invalid sync-run summary column mask")
    result = deepcopy(summary)
    del result["column_fields"]
    for bit, (path, column) in enumerate(COLUMN_FIELDS):
        if not mask & (1 << bit):
            continue
        if column not in row:
            raise ValueError(f"sync-run projection missing column: {column}")
        target = result
        for key in path[:-1]:
            target = target.setdefault(key, {})
            if not isinstance(target, dict):
                raise ValueError("invalid sync-run summary section")
        target[path[-1]] = row[column]
    return result


class _SqlNull:
    def __repr__(self):
        return "SQL_NULL"


SQL_NULL = _SqlNull()
PERSISTED_SUMMARY_FIELDS = SUMMARY_FIELDS - {"yield_requested"}
_NORMALIZATION_ERRORS = frozenset({
    "sync_run_summary_shape_unsupported", "sync_run_summary_invalid_column_mask",
    "sync_run_summary_missing_column_mask", "sync_run_summary_invalid_reported_values",
})


def _error(message, *, code="P0001"):
    # The retained function's formatted missing-column/conflict errors are private.
    public = message if message in _NORMALIZATION_ERRORS else "database_operation_failed"
    raise DatabaseError(public, code=code)


def _wire_value(value):
    """Reject native objects rather than guessing their PostgreSQL JSON encoding."""
    if value is SQL_NULL:
        return
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError("sync_run_summary_wire_value_invalid") from None
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("sync_run_summary_wire_value_invalid")
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif item is None or type(item) in (str, bool, int):
            continue
        elif type(item) is float and math.isfinite(item):
            continue
        else:
            raise ValueError("sync_run_summary_wire_value_invalid")


def _number_text(value):
    # PostgreSQL jsonb expands JSON exponents but retains fractional scale.
    return format(Decimal(str(value)), "f")


def _json_equal(left, right):
    """JSONB numbers compare numerically; booleans are a different JSON type."""
    if type(left) in (int, float, _NativeJsonNumber) and type(right) in (int, float, _NativeJsonNumber):
        return Decimal(str(left)) == Decimal(str(right))
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_json_equal(value, right[key]) for key, value in left.items())
    if type(left) is list:
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def _get_path(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return SQL_NULL
        value = value[key]
    return value


def _column(columns, key):
    if columns is SQL_NULL:
        return SQL_NULL
    if isinstance(columns, dict):
        exists = key in columns
    elif isinstance(columns, list):
        exists = any(type(item) is str and item == key for item in columns)
    else:
        exists = type(columns) is str and columns == key
    if not exists:
        _error("missing_column")
    return columns.get(key, SQL_NULL) if isinstance(columns, dict) else SQL_NULL


def normalize_sync_run_summary(summary, columns, *, compare_timestamps=None):
    """Pure codec, with at most one optional batch of two native comparisons.

    ``compare_timestamps`` returns booleans or deferred DatabaseServerError values
    in pair order. Errors are raised only at their original field position. Use
    ``compare_summary_timestamps(transaction, pairs)`` inside the
    writer's existing transaction, with its final persisted column values.
    Python never parses PostgreSQL's timestamp grammar. Storage writers use the
    separate native-text route to preserve opaque JSONB numeric values.
    """
    _wire_value(summary)
    _wire_value(columns)
    return _normalize_sync_run_summary_value(summary, columns, compare_timestamps=compare_timestamps)


def _normalize_sync_run_summary_value(summary, columns, *, compare_timestamps=None):
    # Both transports share field/error order, but only native text admits opaque
    # numeric tokens. The public wire boundary above remains unchanged.
    value = {} if summary is SQL_NULL else deepcopy(summary)
    version = value.get("summary_schema_version") if isinstance(value, dict) else None
    version_text = ("2" if version is None else version if type(version) is str
                    else _number_text(version) if type(version) in (int, float, _NativeJsonNumber) else "unsupported")
    if not isinstance(value, dict) or version_text not in {"2", "3"}:
        _error("sync_run_summary_shape_unsupported")
    mask = 0
    if "column_fields" in value:
        raw = value["column_fields"]
        text = _number_text(raw) if type(raw) in (int, float, _NativeJsonNumber) else ""
        if not text or not text.isascii() or not text.isdigit():
            _error("sync_run_summary_invalid_column_mask")
        if Decimal(text) > 2147483647:
            _error("mask_overflow", code="22003")
        mask = int(text)
        if mask >= 1 << len(COLUMN_FIELDS):
            _error("sync_run_summary_invalid_column_mask")
    elif version_text == "3":
        _error("sync_run_summary_missing_column_mask")
    reported = deepcopy(value.get("reported_values", {}))
    if not isinstance(reported, dict):
        _error("sync_run_summary_invalid_reported_values")

    entries, pairs = [], []
    for bit, (path, column) in enumerate(COLUMN_FIELDS):
        original = _get_path(value, path)
        if original is SQL_NULL:
            continue
        try:
            actual = _column(columns, column)
        except DatabaseError as error:
            entries.append((bit, path, original, False, None, error))
            break
        equal = actual is not SQL_NULL and _json_equal(original, actual)
        pair_index = None
        if column in {"started_at", "finished_at"} and not equal and actual is not SQL_NULL:
            pair_index = len(pairs)
            pairs.append((original, actual))
        error = None
        key = ".".join(path)
        if not equal and pair_index is None and key in reported and not _json_equal(reported[key], original):
            error = DatabaseError("database_operation_failed", code="P0001")
        entries.append((bit, path, original, equal, pair_index, error))
        if error is not None:
            break
    comparisons = []
    if pairs:
        if compare_timestamps is None:
            raise ValueError("sync_run_summary_timestamp_comparator_required")
        comparisons = compare_timestamps(pairs)
        if (not isinstance(comparisons, (list, tuple)) or len(comparisons) != len(pairs)
                or any(type(item) is not bool and not (
                    isinstance(item, DatabaseServerError) and item.message == "database_operation_failed"
                    and isinstance(item.code, str) and re.fullmatch(r"[A-Z0-9]{5}", item.code)
                ) for item in comparisons)):
            raise ValueError("sync_run_summary_timestamp_comparison_invalid")
    for bit, path, original, equal, pair_index, error in entries:
        if error is not None:
            raise error
        if pair_index is not None:
            equal = comparisons[pair_index]
            if isinstance(equal, DatabaseServerError):
                raise equal
        if not equal:
            key = ".".join(path)
            if key in reported and not _json_equal(reported[key], original):
                _error("reported_value_conflict")
            reported[key] = original
        target = value
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        mask |= 1 << bit
    if reported:
        value["reported_values"] = reported
    return {**value, "summary_schema_version": 3, "column_fields": mask}


_TIMESTAMP_COMPARISONS_SQL = """
WITH pairs AS (
    SELECT item.ordinality AS position,
           item.value ->> 0 AS left_value, item.value ->> 1 AS right_value
    FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS item(value, ordinality)
), diagnostics AS MATERIALIZED (
    SELECT *, (pg_input_error_info(left_value, 'timestamptz')).sql_error_code AS left_code,
              (pg_input_error_info(right_value, 'timestamptz')).sql_error_code AS right_code
    FROM pairs
)
SELECT position,
    CASE WHEN left_code IN ('22007', '22008') THEN NULL
         WHEN left_code IS NOT NULL THEN left_code
         WHEN right_code IN ('22007', '22008') THEN NULL
         ELSE right_code END AS error_code,
    CASE WHEN left_code IS NULL AND right_code IS NULL
         THEN COALESCE(left_value::timestamptz = right_value::timestamptz, false)
         ELSE false END AS equal
FROM diagnostics ORDER BY position LIMIT 3
"""


def compare_summary_timestamps(transaction, pairs):
    """One bounded native SELECT on the supplied transaction; never a new pool.

    Values stay JSONB until PostgreSQL applies the same #>>/->> text conversion
    and timestamptz casts as the retained function. Its two handled datetime
    SQLSTATEs mean unequal; other native input codes become deferred, sanitized
    errors so a later timestamp never outranks an earlier field failure.
    """
    if not isinstance(pairs, (list, tuple)) or len(pairs) > 2:
        raise ValueError("sync_run_summary_timestamp_comparison_invalid")
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("sync_run_summary_timestamp_comparison_invalid")
        _wire_value(list(pair))
    if not pairs:
        return []
    return _compare_summary_timestamp_parameter(transaction, Jsonb(pairs), len(pairs))


def _compare_summary_timestamp_parameter(transaction, parameter, pair_count):
    rows = transaction.fetch_all(_TIMESTAMP_COMPARISONS_SQL, (parameter,), max_rows=2)
    if (len(rows) != pair_count or any(row.get("position") != i or type(row.get("equal")) is not bool
            or (row.get("error_code") is not None and (not isinstance(row["error_code"], str)
                or not re.fullmatch(r"[A-Z0-9]{5}", row["error_code"])))
            for i, row in enumerate(rows, 1))):
        raise ValueError("sync_run_summary_timestamp_comparison_invalid")
    return [DatabaseServerError("database_operation_failed", code=row["error_code"])
            if row.get("error_code") is not None else row["equal"] for row in rows]


def persisted_summary_copy(value, depth=0):
    """Bounded persisted projection with validated root progress metadata.

    ``yield_requested`` remains allowed by safe_summary_copy for the live API,
    but persisted summaries exclude it. Root progress uses the shared policy;
    the historical numeric-token and depth handling remain unchanged.
    Inputs are decoded JSON values. None depth mirrors SQL NULL (no depth cap).
    """
    _wire_value(value)
    if depth is not None and type(depth) is not int:
        raise ValueError("sync_run_summary_depth_invalid")
    return _persisted_copy(value, depth)


def _persisted_copy(value, depth):
    if not isinstance(value, dict) or (depth is not None and depth > 3):
        return {}
    result = {}
    for key, item in value.items():
        if key in SUMMARY_PROGRESS_FIELDS:
            progress = summary_progress_value(key, item, depth)
            if progress is not None:
                result[key] = progress
        elif isinstance(item, dict) and key in SUMMARY_SECTIONS:
            nested = _persisted_copy(item, None if depth is None else depth + 1)
            if nested:
                result[key] = nested
        elif key in PERSISTED_SUMMARY_FIELDS or key in SUMMARY_LABELS:
            if item is None or type(item) in (bool, int, float, _NativeJsonNumber) or (type(item) is str and item in SUMMARY_LABELS):
                result[key] = item
    return result


class _NativeJsonNumber(str):
    """JSONB's exact numeric token, never a user-facing string or Python float."""


def _decode_native_summary_text(value):
    if value is None:
        return SQL_NULL
    if type(value) is not str:
        raise ValueError("sync_run_summary_native_text_invalid")

    def invalid_constant(value):
        raise ValueError("sync_run_summary_native_text_invalid")

    try:
        return json.loads(value, parse_int=_NativeJsonNumber, parse_float=_NativeJsonNumber,
                          parse_constant=invalid_constant)
    except (ValueError, RecursionError):
        raise ValueError("sync_run_summary_native_text_invalid") from None


def _encode_native_summary_text(value):
    if type(value) is _NativeJsonNumber:
        return str(value)
    if type(value) is dict:
        return "{" + ",".join(json.dumps(key) + ":" + _encode_native_summary_text(item)
                              for key, item in value.items()) + "}"
    if type(value) in (list, tuple):
        return "[" + ",".join(_encode_native_summary_text(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def normalize_sync_run_summary_text(summary_text, columns_text, *, transaction=None):
    """Normalize native JSONB text losslessly before the persisted projection.

    Both inputs must come from PostgreSQL JSONB text after native casts/defaults
    have formed the proposed row. None represents SQL NULL; the text ``null``
    represents JSON null. This is not a replacement for incoming wire encoding
    or PostgreSQL input validation. Numeric tokens retain magnitude and scale,
    including unrelated values and any reported originals, without float/int
    decoding. Numeric equality is exact and independent of Decimal precision.

    Unequal timestamp fields use one SELECT of at most two pairs on the supplied
    writer transaction. That transaction retains native clocks/casts, admission,
    deadlines and locks; this helper opens no transaction and performs no write
    or retry. A transaction is unnecessary when no timestamp cast is required.
    Compose with persisted_summary_text only after normalization; do not use the
    API safe-summary policy for storage. No consumer uses this route yet.
    """
    summary = _decode_native_summary_text(summary_text)
    columns = _decode_native_summary_text(columns_text)

    def compare(pairs):
        # Encoding tokens directly prevents JSON string coercion and preserves
        # PostgreSQL's own ->> conversion before its native timestamp grammar.
        return _compare_summary_timestamp_parameter(
            transaction, _encode_native_summary_text(pairs), len(pairs))

    normalized = _normalize_sync_run_summary_value(
        summary, columns, compare_timestamps=compare if transaction is not None else None)
    return _encode_native_summary_text(normalized)


def persisted_summary_text(value):
    """Project native JSONB text without rounding or integer-decoder limits.

    Cleanup reads stored JSONB as text: historical numeric values may exceed
    Python float range or its integer decoding limit. The same persisted-name
    policy above applies; only number transport differs from incoming wire JSON.
    SQL NULL and JSON null both project to an empty object, as in PostgreSQL.
    """
    if value is None:return '{}'
    if type(value) is not str:raise ValueError('sync_run_summary_native_text_invalid')
    def invalid_constant(value):raise ValueError('sync_run_summary_native_text_invalid')
    parsed=json.loads(value,parse_int=_NativeJsonNumber,parse_float=_NativeJsonNumber,parse_constant=invalid_constant)
    projected=_persisted_copy(parsed,0)
    def encode(item):
        if type(item) is _NativeJsonNumber:return str(item)
        if isinstance(item,dict):return '{'+','.join(json.dumps(key)+':'+encode(child) for key,child in item.items())+'}'
        return json.dumps(item,ensure_ascii=True,allow_nan=False)
    return encode(projected)
