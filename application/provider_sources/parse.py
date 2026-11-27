"""Typed parsing of one provider page by the registry.

* JSON is read from the cached response bytes with ``parse_float=Decimal`` and
  duplicate keys refused: no float ever reaches a value.
* Envelope: ``kind`` equals the method's envelope kind; ``pageInfo`` is
  consistent; ``items`` may be missing only in the expected empty envelope
  (``channels/reconnect_source.py::_single_item``).
* Identity: for an ID filter every row ID was requested and appears once;
  foreign or repeated IDs reject the page. A missing requested ID is OMITTED.
* Values: the registry's wire type first, then the single owner of the value
  rule (``youtube_source_fields``, ``collection.video_payloads``,
  ``youtube_channel_source_fields``, ``typed_playlist``). HTML stays raw text.
* Unknown fields: values are dropped, only their path names are counted.
* An unknown enum value is UNKNOWN_ENUM; the row is kept unless the field is
  the row identity.
* Arrays of objects (``replies.comments[].id``) keep one FieldValue per element,
  so an element without the key stays ABSENT instead of shifting its neighbours.
* A method whose rows have no single identity path (search hits: the identity
  depends on ``id.kind``) gets it from its family hook; the hook may reject a row.
* A single-resource method (``row_path`` "") parses the whole answer as one row;
  proto-JSON lists (Reporting, Analytics groups) may omit an empty list.
"""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal
import json
import re

from typed_playlist import VIDEO_ID_RE
from youtube_source_fields import parse_dt, youtube_counter_integer, youtube_duration_seconds, youtube_unsigned_long

from .errors import ResponseInvalid
from .families import policy
from .plan import IDENTITIES
from .results import MAX_UNKNOWN_PATHS, ElementValues, FieldValue, MapValue, PageParse, Row, ValueState

UINT64_MAX = 2**64 - 1
INT64_MIN, INT64_MAX = -2**63, 2**63 - 1
INTEGER_BOUNDS = {"uint32": (0, 2**32 - 1), "int32": (-2**31, 2**31 - 1),
                  "uint64": (0, UINT64_MAX), "int64": (INT64_MIN, INT64_MAX)}
MAX_ENUM_TEXT = 128
MAX_DATETIME_TEXT = 64
_DIGITS = re.compile(r"[0-9]{1,20}\Z")
_SIGNED = re.compile(r"-?[0-9]{1,19}\Z")
_GOOGLE_DURATION = re.compile(r"-?[0-9]{1,12}(\.[0-9]{1,9})?s\Z")
_MAP_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
FOREIGN = {"video": VIDEO_ID_RE, "channel": IDENTITIES["channel"], "channel_uploader": IDENTITIES["channel"],
           "channel_who_added": IDENTITIES["channel"], "playlist": IDENTITIES["playlist"],
           "analytics_group": IDENTITIES["analytics_group"], "reporting_job": IDENTITIES["reporting_job"],
           "report_type": IDENTITIES["report_type"]}


def decode_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ResponseInvalid("response_duplicate_key")
            result[key] = value
        return result

    def constant(_):
        raise ResponseInvalid("response_json_invalid")
    if not isinstance(content, (bytes, bytearray)):
        raise ResponseInvalid("response_json_invalid")
    try:
        return json.loads(bytes(content).decode("utf-8"), parse_float=Decimal, object_pairs_hook=pairs,
                          parse_constant=constant)
    except ResponseInvalid:
        raise
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ResponseInvalid("response_json_invalid") from None


def known(state, value=None, absence=None):
    return FieldValue(state, value, absence)


INVALID = FieldValue(ValueState.INVALID)


def _string(field, raw):
    kind = field.value_kind
    if kind == "string":
        if field.enum and raw not in field.enum:
            return known(ValueState.UNKNOWN_ENUM, raw) if len(raw) <= MAX_ENUM_TEXT else INVALID
        pattern = FOREIGN.get(field.role_value("foreign_key"))
        if pattern is not None and pattern.fullmatch(raw) is None:
            return INVALID
        return known(ValueState.KNOWN, raw)
    if kind == "uint64_decimal_string":
        number = youtube_counter_integer(raw, maximum=UINT64_MAX) if _DIGITS.fullmatch(raw) else None
        return INVALID if number is None else known(ValueState.KNOWN, number)
    if kind == "int64_decimal_string":
        if field.has_role("counter"):
            number = youtube_counter_integer(raw) if _DIGITS.fullmatch(raw) else None
            return INVALID if number is None else known(ValueState.KNOWN, number)
        if _SIGNED.fullmatch(raw) is None or not INT64_MIN <= int(raw) <= INT64_MAX:
            return INVALID
        return known(ValueState.KNOWN, int(raw))
    if kind == "rfc3339_datetime":
        moment = parse_dt(raw) if len(raw) <= MAX_DATETIME_TEXT and raw == raw.strip() and "T" in raw else None
        if moment is None or moment.tzinfo is None or moment.utcoffset() is None:
            return INVALID
        return known(ValueState.KNOWN, moment.astimezone(timezone.utc))
    if kind == "iso8601_duration":
        seconds = (youtube_duration_seconds(raw)
                   if raw.startswith("P") and len(raw) <= 64 and raw == raw.strip() else None)
        return INVALID if seconds is None else known(ValueState.KNOWN, seconds)
    if kind == "boolean_string":
        from collection.video_payloads import youtube_caption_available
        if raw not in ("true", "false"):
            return INVALID
        try:
            return known(ValueState.KNOWN, youtube_caption_available({"contentDetails": {"caption": raw}}))
        except ValueError:
            return INVALID
    if kind == "google_duration":
        return known(ValueState.KNOWN, Decimal(raw[:-1])) if _GOOGLE_DURATION.fullmatch(raw) else INVALID
    if kind == "duration_format_unverified":
        # Discovery says google-duration, videos.list uses ISO 8601: accept either, strictly.
        if _GOOGLE_DURATION.fullmatch(raw):
            return known(ValueState.KNOWN, Decimal(raw[:-1]))
        seconds = youtube_duration_seconds(raw) if raw.startswith("P") and len(raw) <= 64 else None
        return INVALID if seconds is None else known(ValueState.KNOWN, Decimal(seconds))
    return INVALID


def scalar(field, raw):
    """One value by the registry wire type and its owning rule."""
    if raw is None:
        return known(ValueState.NULL_REPORTED)
    if field.value_kind == "uint64_milliseconds":
        millis = youtube_unsigned_long(raw)
        return INVALID if millis is None else known(ValueState.KNOWN, millis)
    if field.json_type == "string":
        if not isinstance(raw, str):
            return INVALID
        if raw == "":
            return known(ValueState.KNOWN_EMPTY, "")
        return _string(field, raw)
    if field.json_type == "integer":
        if type(raw) is not int:
            return INVALID
        low, high = INTEGER_BOUNDS.get(field.format, (INT64_MIN, UINT64_MAX))
        return known(ValueState.KNOWN, raw) if low <= raw <= high else INVALID
    if field.json_type == "boolean":
        return known(ValueState.KNOWN, raw) if type(raw) is bool else INVALID
    if field.json_type == "number":
        if type(raw) is bool or not isinstance(raw, (int, Decimal)):
            return INVALID
        return known(ValueState.KNOWN, Decimal(raw))
    return INVALID


def _collect(field, raws):
    values, unknown = [], False
    for raw in raws:
        value = scalar(field, raw)
        if value.state in (ValueState.INVALID, ValueState.NULL_REPORTED):
            return INVALID
        unknown = unknown or value.state is ValueState.UNKNOWN_ENUM
        values.append(value.value)
    return values, unknown


def field_value(field, item):
    """The FieldValue of one registry path in one row; absence keeps the registry meaning."""
    return _walk(field, item, field.path.split("."))


def _walk(field, node, segments):
    for index, segment in enumerate(segments):
        if segment == "{key}":
            return _map_value(field, node, segments[index + 1:])
        name = segment.removesuffix("[]")
        if not isinstance(node, dict):
            return INVALID
        if name not in node:
            return known(ValueState.ABSENT, absence=field.absence)
        node = node[name]
        last = index == len(segments) - 1
        if node is None:
            return known(ValueState.NULL_REPORTED)
        if segment.endswith("[]"):
            if not isinstance(node, list):
                return INVALID
            if not last:
                return _element_values(field, node, segments[index + 1:])
            if not node:
                return known(ValueState.KNOWN_EMPTY, ())
            collected = _collect(field, node)
            if collected is INVALID:
                return INVALID
            values, unknown = collected
            return known(ValueState.UNKNOWN_ENUM if unknown else ValueState.KNOWN, tuple(values))
    return scalar(field, node)


def _element_values(field, elements, rest):
    """An array of objects: one state per element; a broken element or value invalidates the field."""
    if not elements:
        return known(ValueState.KNOWN_EMPTY, ElementValues())
    values, unknown = [], False
    for element in elements:
        if not isinstance(element, dict):
            return INVALID
        value = _walk(field, element, rest)
        if value.state is ValueState.INVALID:
            return INVALID
        unknown = unknown or value.state is ValueState.UNKNOWN_ENUM
        values.append(value)
    return known(ValueState.UNKNOWN_ENUM if unknown else ValueState.KNOWN, ElementValues(values))


def _map_value(field, node, rest):
    if not isinstance(node, dict) or len(rest) != 1:
        return INVALID
    if not node:
        return known(ValueState.KNOWN_EMPTY, MapValue())
    pairs, unknown = [], False
    for key, entry in node.items():
        if not isinstance(key, str) or _MAP_KEY.fullmatch(key) is None or not isinstance(entry, dict):
            return INVALID
        if rest[0] not in entry:
            continue
        value = scalar(field, entry[rest[0]])
        if value.state in (ValueState.INVALID, ValueState.NULL_REPORTED):
            return INVALID
        unknown = unknown or value.state is ValueState.UNKNOWN_ENUM
        pairs.append((key, value.value))
    if not pairs:
        return known(ValueState.ABSENT, absence=field.absence)
    return known(ValueState.UNKNOWN_ENUM if unknown else ValueState.KNOWN, MapValue(sorted(pairs)))


def _patterns(paths):
    known_paths, prefixes = set(paths), set()
    for path in paths:
        parts = path.split(".")
        prefixes.update(".".join(parts[:index]) for index in range(1, len(parts)))
    return known_paths, prefixes


def unknown_paths(node, known_paths, prefixes, prefix="", out=None):
    """Names of paths the registry does not describe; values are never kept."""
    out = set() if out is None else out
    if not isinstance(node, dict) or len(out) >= MAX_UNKNOWN_PATHS:
        return out
    for key, value in node.items():
        if not isinstance(key, str):
            continue
        mapped = f"{prefix}.{{key}}" if prefix else None
        if mapped and mapped in prefixes:
            unknown_paths(value, known_paths, prefixes, mapped, out)
            continue
        name = f"{prefix}.{key}" if prefix else key
        if name in known_paths or name + "[]" in known_paths:
            continue
        if name in prefixes:
            unknown_paths(value, known_paths, prefixes, name, out)
        elif name + "[]" in prefixes and isinstance(value, list):
            for element in value[:50]:
                unknown_paths(element, known_paths, prefixes, name + "[]", out)
        elif len(out) < MAX_UNKNOWN_PATHS:
            out.add(name[:160])
    return out


REJECT_ROW = object()   # returned by a family hook: the row breaks its row-kind contract


def check_envelope(method, payload, *, id_batch):
    if not isinstance(payload, dict) or "error" in payload:
        raise ResponseInvalid("response_envelope_invalid")
    if method.envelope_kind:
        kind = payload.get("kind")
        if kind != method.envelope_kind and kind not in method.envelope_kind_aliases:
            raise ResponseInvalid("response_kind_invalid")
    if method.row_path == "":
        return [payload]     # a single resource (Reporting jobs.get, jobs.reports.get)
    container = (method.row_path or "").removesuffix("[]")
    if container in payload:
        items = payload[container]
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ResponseInvalid("response_items_invalid")
    elif ("pageInfo" in payload or method.pagination.style == "none"
          or method.id in policy.EMPTY_LIST_OMITTED):
        items = []   # the documented empty list envelope (proto JSON omits an empty list)
    else:
        raise ResponseInvalid("response_items_missing")
    page = payload.get("pageInfo")
    if page is not None:
        total, per_page = (page.get("totalResults"), page.get("resultsPerPage")) if isinstance(page, dict) else (None, None)
        if (not isinstance(page, dict) or (total is not None and (type(total) is not int or total < 0))
                or (per_page is not None and (type(per_page) is not int or per_page < 0))
                or (id_batch and total is not None and total != len(items))):
            raise ResponseInvalid("response_page_info_invalid")
    return items


def parse_page(method, plan, payload, *, page_index, hook, targets=()):
    """Rows of one page; raises ResponseInvalid for a page that breaks the contract."""
    items = check_envelope(method, payload, id_batch=plan.strategy == "id_batch")
    identity = method.identity_field
    kind = identity.role_value("identity") if identity is not None else None
    pattern = IDENTITIES.get(kind)
    selected = [method.fields[path] for path in plan.fields]
    known_paths, prefixes = _patterns(list(method.fields) + method.envelope_paths("_row_envelope"))
    envelope_known, envelope_prefixes = _patterns(method.envelope_paths("_envelope"))
    envelope_known.add((method.row_path or "").removesuffix("[]"))
    unknown = set() if method.row_path == "" else unknown_paths(
        {key: value for key, value in payload.items() if key != (method.row_path or "").removesuffix("[]")},
        envelope_known, envelope_prefixes)
    rows, returned, rejected = [], [], 0
    requested = set(targets)
    hooked = set()
    for source_position, item in enumerate(items, start=1):
        if identity is None:
            # Rows without one identity path (search hits): the family hook decides.
            resolved = hook.row_identity(method, item)
            if resolved is REJECT_ROW:
                rejected += 1
                continue
            if resolved is not None:
                if resolved in hooked:
                    raise ResponseInvalid("response_identity_invalid")   # a repeated row in one page
                hooked.add(resolved)
            fields = {field.path: field_value(field, item) for field in selected}
            fields = hook.finish_row(method, item, fields)
            unknown_paths(item, known_paths, prefixes, out=unknown)
            rows.append(Row(unit=method.row_unit, identity=resolved, fields=fields, page_index=page_index,
                            source_position=source_position if method.id == "youtube.search.list" else None))
            continue
        raw_id = item.get(identity.path)
        valid_id = isinstance(raw_id, str) and (pattern is None or pattern.fullmatch(raw_id) is not None)
        if plan.strategy == "id_batch" or plan.tracks_requested_ids:
            if not valid_id or raw_id not in requested or raw_id in returned:
                raise ResponseInvalid("response_identity_invalid")
        elif not valid_id or raw_id in returned:
            if valid_id:
                raise ResponseInvalid("response_identity_invalid")   # a repeated row in one page
            rejected += 1
            continue
        fields = {field.path: field_value(field, item) for field in selected}
        fields = hook.finish_row(method, item, fields)
        unknown_paths(item, known_paths, prefixes, out=unknown)
        rows.append(Row(unit=method.row_unit, identity=(kind, raw_id), fields=fields, page_index=page_index))
        returned.append(raw_id)
    return PageParse(tuple(rows), tuple(returned), tuple(sorted(unknown))[:MAX_UNKNOWN_PATHS], rejected,
                     len(items), payload)
