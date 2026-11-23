"""Owner-OAuth families of the source layer: Data API owner families, YouTube Analytics, YouTube Reporting.

These families support documented response formats. Over HTTP every
owner context answers ``owner_auth_unavailable``. Their rows belong to the owner
binding (context ``owner:<binding>``): they never join public rows, never feed
storage, and a continuation of one context is refused in another. The
transport refuses the Analytics/Reporting hosts without an owner token.

Data API owner families need no hook except:

* ``captions.download``: a bounded media reader. The body is a caption file
  (sbv, scc, srt, ttml, vtt), never JSON. It must be UTF-8 text without NUL; a
  requested ``tfmt`` must match the file's own signature. The row carries the
  format, byte length, SHA-256, line and cue counts and the text (in memory).
* ``members.list`` ``mode=updates``: a poll position, never "complete" (plan
  and acquire, see ``policy.POLLING_MODES``).

Analytics ``reports.query``: a request is planned only when at least one report
table of ``registry/youtube.owner_reports.json`` accepts it whole: every group
rule of its dimensions, metrics and filters, required filter values, sort and
maxResults rules, single-value filters and the monetary scope. There is no
cartesian product of metric and dimension lists. ``ids`` names the owner's own
channel only. Rows are ``report_row``: positional values typed by
``columnHeaders``; no ``rows`` means a complete answer without data.

Reporting: reportTypes/jobs/jobs.reports are ordinary JSON families. The media
download is a chain: ``jobs.reports.get`` gives ``downloadUrl`` (a capability:
read in memory only, never returned, logged or stored), whose path must be
exactly ``https://youtubereporting.googleapis.com/v1/media/...``; the CSV is read
with byte and row bounds and its header is checked against the report type.
``jobs.create`` is a control operation outside reads (not implemented here).

Planning functions are pure (the preview imports nothing that sends); readers
never import the generic page parser.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
import functools
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from .errors import PlanRejected, ResponseInvalid
from .families.base import Hook
from .plan import IDENTITIES
from .results import FieldValue, PageParse, Row, ValueState

REPORTS_FILE = Path(__file__).resolve().parent / "registry" / "youtube.owner_reports.json"
REPORTING_HOST = "youtubereporting.googleapis.com"
MEDIA_PREFIX = "/v1/media/"
MAX_DOWNLOAD_URL = 1000
MAX_CSV_CELL = 4096
MAX_CSV_COLUMNS = 256
_COLUMN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MEDIA_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.~-]{0,255}\Z")
_INTEGER = re.compile(r"-?[0-9]{1,19}\Z")
_DECIMAL = re.compile(r"-?[0-9]{1,19}\.[0-9]{1,18}([eE][+-]?[0-9]{1,3})?\Z")
_CSV_DATE = re.compile(r"[0-9]{8}\Z")
ENUM_VALUE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
FILTER_VALUES = {
    "video": IDENTITIES["video"], "playlist": IDENTITIES["playlist"], "group": IDENTITIES["analytics_group"],
    "country": re.compile(r"[A-Z]{2}\Z"), "province": re.compile(r"US-[A-Z]{2}\Z"),
    "continent": re.compile(r"[0-9]{3}\Z"), "subContinent": re.compile(r"[0-9]{3}\Z"),
    "subscribedStatus": re.compile(r"(SUBSCRIBED|UNSUBSCRIBED)\Z"), "liveOrOnDemand": re.compile(r"(LIVE|ON_DEMAND)\Z"),
    "youtubeProduct": re.compile(r"(CORE|GAMING|KIDS|UNKNOWN)\Z"),
    "audienceType": re.compile(r"(ORGANIC|AD_INSTREAM|AD_INDISPLAY)\Z"),
}
COLUMN_TYPES = {"DIMENSION", "METRIC"}
PST = timezone(timedelta(hours=-8))      # Reporting days are fixed UTC-8, not Pacific time


@functools.lru_cache(maxsize=1)
def owner_reports():
    """The hand-maintained report tables and types; the checksum is verified like the registry."""
    from .registry import RegistryError
    try:
        document = json.loads(REPORTS_FILE.read_bytes().decode("utf-8"))
        body = {key: value for key, value in document.items() if key != "content_sha256"}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                                .encode("utf-8")).hexdigest()
    except (OSError, ValueError, UnicodeError) as error:
        raise RegistryError("provider_owner_reports_unreadable") from error
    if document.get("content_sha256") != digest:
        raise RegistryError("provider_owner_reports_checksum_mismatch")
    return document


# ------------------------------------------------------------------ Analytics planning

def _names(text, name):
    names = text.split(",") if text else []
    if len(names) != len(set(names)):
        raise PlanRejected("analytics_names_repeated", name)
    return names


def _filters(text):
    filters = {}
    rules = owner_reports()["analytics"]
    for item in text.split(";") if text else []:
        name, _, raw = item.partition("==")
        if name in filters:
            raise PlanRejected("analytics_filter_repeated", "filters")
        values = raw.split(",")
        pattern = FILTER_VALUES.get(name, ENUM_VALUE)
        if (not values or any(pattern.fullmatch(value) is None for value in values)
                or len(values) != len(set(values))):
            raise PlanRejected("parameter_invalid", "filters")
        if len(values) > 1 and (name not in rules["multi_value_filters"] or len(values) > rules["max_filter_ids"]):
            raise PlanRejected("analytics_filter_single_value", "filters")
        filters[name] = tuple(values)
    return filters


def _groups_accept(groups, names):
    allowed = {value for group in groups for value in group["values"]}
    if not set(names) <= allowed:
        return False
    for group in groups:
        found = len(set(group["values"]) & set(names))
        rule = group["rule"]
        if ((rule == "required" and found != len(group["values"])) or (rule == "exactly_one" and found != 1)
                or (rule == "one_or_more" and found < 1) or (rule == "zero_or_one" and found > 1)):
            return False
    return True


def table_accepts(table, dimensions, metrics, filters, sort, max_results):
    """One report table accepts the whole request: no partial or combined tables."""
    if not (_groups_accept(table["dimensions"], dimensions) and _groups_accept(table["metrics"], metrics)
            and _groups_accept(table["filters"], list(filters))):
        return False
    for name, values in table["filter_values"].items():
        if name in filters and set(filters[name]) - set(values):
            return False
    for name, values in table["filter_values_excluded"].items():
        if name in filters and set(filters[name]) & set(values):
            return False
    if any(len(filters.get(name, ())) > 1 for name in table["single_value_filters"]):
        return False
    if table["sort"]["required"]:
        if not sort or any(key not in table["sort"]["allowed"] or key.removeprefix("-") not in metrics for key in sort):
            return False
    elif any(key.removeprefix("-") not in set(dimensions) | set(metrics) for key in sort):
        return False
    if table["max_results"] is not None and (max_results is None or max_results > table["max_results"]):
        return False
    return True


def plan_report_query(values, *, binding, max_rows):
    """Options of one reports.query plan; refuses anything no listed table accepts."""
    rules = owner_reports()["analytics"]
    channel = values["ids"][0].split("==", 1)[1]
    if channel != "MINE" and (binding is None or channel != binding.channel_id):
        raise PlanRejected("analytics_ids_not_owner")     # never another channel's report
    if values["startDate"][0] > values["endDate"][0]:
        raise PlanRejected("analytics_dates_invalid")
    metrics = _names(values["metrics"][0], "metrics")
    dimensions = _names(values["dimensions"][0], "dimensions") if "dimensions" in values else []
    filters = _filters(values["filters"][0]) if "filters" in values else {}
    sort = _names(values["sort"][0], "sort") if "sort" in values else []
    if len({key.removeprefix("-") for key in sort}) != len(sort):
        raise PlanRejected("analytics_names_repeated", "sort")
    max_results = int(values["maxResults"][0]) if "maxResults" in values else None
    if "currency" in values and not set(metrics) & set(rules["currency_metrics"]):
        raise PlanRejected("parameter_needs_filter", "currency")
    tables = sorted(key for key, table in rules["tables"].items()
                    if table_accepts(table, dimensions, metrics, filters, sort, max_results))
    if not tables:
        raise PlanRejected("analytics_combination_not_listed")
    monetary = bool(set(metrics) & set(rules["monetary_metrics"]))
    bounded = any(rules["tables"][key]["max_results"] is not None for key in tables)
    groups = [[rules["monetary_scope"]] if monetary else list(rules["scope_any"]), list(rules["scope_also_any"])]
    options = {"dimensions": dimensions, "metrics": metrics, "tables": tables, "monetary": monetary,
               "scope_groups": groups}
    if max_results is not None and not bounded:
        options["page_size"] = min(max_results, max_rows)
    return options


# ------------------------------------------------------------------ media planning and selection

def plan_media(method, values, chain):
    if method.id == "youtube.captions.download":
        return {"reader": "caption_file", "requests": 1, "format": values.get("tfmt", (None,))[0],
                "columns": list(CAPTION_FIELDS)}
    types = owner_reports()["reporting"]["report_types"]
    report_type = types.get(chain["reportTypeId"])
    if report_type is None:
        raise PlanRejected("report_type_not_listed")
    return {"reader": "report_csv", "requests": 2, "report_type": chain["reportTypeId"],
            "dimensions": list(report_type["dimensions"]), "metrics": list(report_type["metrics"]),
            "columns": list(report_type["dimensions"]) + list(report_type["metrics"]),
            "first_endpoint": f"jobs/{quote(chain['jobId'], safe='')}/reports/{quote(chain['reportId'], safe='')}",
            "first_label": "reporting.reports"}


def resolve_special_selection(method, selection, options):
    """Analytics columns come from metrics/dimensions; media fields are the reader's columns."""
    from .selection import FieldSelection, ResolvedSelection
    if not isinstance(selection, FieldSelection):
        raise PlanRejected("selection_invalid")
    if selection.parts:
        raise PlanRejected("parts_not_supported")
    if method.id == "youtubeAnalytics.reports.query":
        if selection.fields:
            raise PlanRejected("fields_from_metrics_and_dimensions")
        return ResolvedSelection(parts=(), fields=tuple(options["dimensions"] + options["metrics"]), mask="")
    columns = options["columns"]
    unknown = sorted(set(selection.fields) - set(columns))
    if unknown:
        raise PlanRejected("field_unknown", unknown[0] if len(unknown[0]) <= 64 and _COLUMN.fullmatch(unknown[0]) else None)
    chosen = [column for column in columns if column in selection.fields] or list(columns)
    for dimension in options.get("dimensions", ()):
        if dimension not in chosen:
            chosen.append(dimension)        # the primary key of a report row is always kept
    return ResolvedSelection(parts=(), fields=tuple(chosen), mask="")


# ------------------------------------------------------------------ Analytics parsing

def _cell(data_type, raw):
    if raw is None:
        return FieldValue(ValueState.NULL_REPORTED)
    if data_type == "STRING":
        if not isinstance(raw, str) or len(raw) > MAX_CSV_CELL:
            return FieldValue(ValueState.INVALID)
        return FieldValue(ValueState.KNOWN_EMPTY, "") if raw == "" else FieldValue(ValueState.KNOWN, raw)
    if data_type == "INTEGER":
        return FieldValue(ValueState.KNOWN, raw) if type(raw) is int else FieldValue(ValueState.INVALID)
    if data_type == "FLOAT":
        if type(raw) is bool or not isinstance(raw, (int, Decimal)):
            return FieldValue(ValueState.INVALID)
        return FieldValue(ValueState.KNOWN, Decimal(raw))
    # A data type the reference does not name: the raw value stays in memory only.
    if isinstance(raw, (str, int, Decimal)) and type(raw) is not bool:
        return FieldValue(ValueState.UNKNOWN_ENUM, raw if not isinstance(raw, str) else raw[:MAX_CSV_CELL])
    return FieldValue(ValueState.INVALID)


def parse_report_page(method, plan, payload, *, page_index):
    """reports.query: columnHeaders must be the requested dimensions then metrics, in request order."""
    if not isinstance(payload, dict) or "error" in payload:
        raise ResponseInvalid("response_envelope_invalid")
    if payload.get("kind") != method.envelope_kind:
        raise ResponseInvalid("response_kind_invalid")
    dimensions, metrics = list(plan.options["dimensions"]), list(plan.options["metrics"])
    expected = [(name, "DIMENSION") for name in dimensions] + [(name, "METRIC") for name in metrics]
    headers = payload.get("columnHeaders")
    if not isinstance(headers, list) or len(headers) != len(expected):
        raise ResponseInvalid("analytics_columns_mismatch")
    types = []
    for header, (name, column_type) in zip(headers, expected):
        if (not isinstance(header, dict) or header.get("name") != name or header.get("columnType") != column_type
                or not isinstance(header.get("dataType"), str) or len(header["dataType"]) > 32):
            raise ResponseInvalid("analytics_columns_mismatch")
        types.append(header["dataType"])
    rows_raw = payload.get("rows", [])
    if not isinstance(rows_raw, list) or not all(isinstance(row, list) and len(row) == len(expected) for row in rows_raw):
        raise ResponseInvalid("analytics_rows_invalid")
    known = {"kind", "columnHeaders", "rows", "etag"}
    unknown = tuple(sorted(key[:160] for key in payload if isinstance(key, str) and key not in known))[:50]
    rows = []
    for raw in rows_raw:
        fields = {name: _cell(data_type, value) for (name, _), data_type, value in zip(expected, types, raw)}
        identity = None
        if dimensions:
            parts = []
            for name in dimensions:
                value = fields[name]
                if value.state not in (ValueState.KNOWN, ValueState.KNOWN_EMPTY):
                    raise ResponseInvalid("analytics_dimension_invalid")
                parts.append(f"{name}={value.value}")
            identity = ("report_row", "|".join(parts))
        rows.append(Row(unit=method.row_unit, identity=identity, fields=fields, page_index=page_index))
    return PageParse(tuple(rows), (), unknown, 0, len(rows), payload)


# ------------------------------------------------------------------ Reporting media

def report_resource_name(download_url):
    """The media path of a Reporting downloadUrl; anything but the documented host and path is refused."""
    if not isinstance(download_url, str) or not 1 <= len(download_url) <= MAX_DOWNLOAD_URL:
        raise ResponseInvalid("report_download_url_invalid")
    try:
        parts = urlsplit(download_url)
        port = parts.port
    except ValueError:
        raise ResponseInvalid("report_download_url_invalid") from None
    if (parts.scheme != "https" or parts.hostname != REPORTING_HOST or parts.netloc != REPORTING_HOST
            or port is not None or parts.fragment or parts.query not in ("", "alt=media")
            or not parts.path.startswith(MEDIA_PREFIX)):
        raise ResponseInvalid("report_download_url_invalid")
    name = parts.path[len(MEDIA_PREFIX):]
    segments = name.split("/")
    if not name or len(segments) > 12 or any(_MEDIA_SEGMENT.fullmatch(segment) is None for segment in segments):
        raise ResponseInvalid("report_download_url_invalid")
    return name


def _text_body(content):
    if not isinstance(content, (bytes, bytearray)):
        raise ResponseInvalid("media_invalid")
    data = bytes(content)
    if data[:2] == b"\x1f\x8b":
        raise ResponseInvalid("media_compressed_unexpected")   # transfer gzip is decoded by the transport
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ResponseInvalid("media_not_utf8") from None
    if "\x00" in text:
        raise ResponseInvalid("media_binary")
    return text.removeprefix("﻿")


def _csv_value(name, raw, dimensions):
    if len(raw) > MAX_CSV_CELL:
        return FieldValue(ValueState.INVALID)
    if name in dimensions:
        if raw == "":
            return FieldValue(ValueState.KNOWN_EMPTY, "")   # documented: e.g. video_id of channel-level rows
        if name == "date":
            if _CSV_DATE.fullmatch(raw) is None:
                return FieldValue(ValueState.INVALID)
            return FieldValue(ValueState.KNOWN, f"{raw[:4]}-{raw[4:6]}-{raw[6:]}")
        return FieldValue(ValueState.KNOWN, raw)
    if raw == "":
        return FieldValue(ValueState.NULL_REPORTED)
    if _INTEGER.fullmatch(raw):
        return FieldValue(ValueState.KNOWN, int(raw))
    if _DECIMAL.fullmatch(raw):
        return FieldValue(ValueState.KNOWN, Decimal(raw))
    return FieldValue(ValueState.INVALID)


def read_report_csv(method, plan, content, *, page_index):
    """Bounded CSV reader: (PageParse, rows truncated by the row budget)."""
    import csv
    import io
    text = _text_body(content)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header is None:
            raise ResponseInvalid("report_header_missing")
        if (len(header) > MAX_CSV_COLUMNS or len(header) != len(set(header))
                or any(_COLUMN.fullmatch(name) is None for name in header)):
            raise ResponseInvalid("report_header_invalid")
        dimensions, metrics = set(plan.options["dimensions"]), set(plan.options["metrics"])
        if not dimensions <= set(header):
            raise ResponseInvalid("report_columns_mismatch")   # not the declared report type
        unknown = tuple(sorted(name for name in header if name not in dimensions | metrics))[:50]
        selected = [name for name in plan.fields]
        rows, truncated = [], False
        for record in reader:
            if not record:
                continue
            if len(record) != len(header):
                raise ResponseInvalid("report_row_width")
            if len(rows) >= plan.max_rows:
                truncated = True
                break
            cells = dict(zip(header, record))
            fields = {name: (_csv_value(name, cells[name], dimensions) if name in cells else
                             FieldValue(ValueState.ABSENT, None, "absent_unknown")) for name in selected}
            rows.append(Row(unit=method.row_unit, identity=None, fields=fields, page_index=page_index))
    except csv.Error:
        raise ResponseInvalid("report_csv_invalid") from None
    return PageParse(tuple(rows), (), unknown, 0, len(rows), None), truncated


# ------------------------------------------------------------------ Reporting report selection

def _moment(row, path):
    value = row.fields.get(path)
    return value.value if value is not None and value.state is ValueState.KNOWN else None


def superseded_reports(rows):
    """IDs of reports replaced by a backfill: same (startTime, endTime), an older createTime."""
    latest = {}
    for row in rows:
        window = (_moment(row, "startTime"), _moment(row, "endTime"))
        created = _moment(row, "createTime")
        if None in window or created is None or row.identity is None:
            continue
        current = latest.get(window)
        if current is None or created > current[0]:
            latest[window] = (created, row.identity[1])
    keep = {identity for _, identity in latest.values()}
    return sorted(row.identity[1] for row in rows if row.identity is not None and None not in
                  (_moment(row, "startTime"), _moment(row, "endTime"), _moment(row, "createTime"))
                  and row.identity[1] not in keep)


def pst_report_day(row):
    """The PST (UTC-8) day a report covers, or None when its window is not one such day."""
    start, end = _moment(row, "startTime"), _moment(row, "endTime")
    if start is None or end is None or end - start != timedelta(hours=24):
        return None
    local = start.astimezone(PST)
    if (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0):
        return None
    return local.date().isoformat()


class ReportsList(Hook):
    """jobs.reports.list: backfills and non-PST windows are counted in the result notes."""

    def finish_result(self, method, rows):
        notes = []
        replaced = superseded_reports(rows)
        if replaced:
            notes.append(f"superseded_reports:{len(replaced)}")
        odd = sum(1 for row in rows if pst_report_day(row) is None)
        if odd:
            notes.append(f"report_window_not_pst_day:{odd}")
        return rows, tuple(notes)


# ------------------------------------------------------------------ Data API owner families

CAPTION_FIELDS = ("format", "byte_length", "sha256", "line_count", "cue_count", "text")
MAX_CAPTION_LINES = 200_000
_SRT_TIMING = re.compile(r"[0-9]{2,}:[0-9]{2}:[0-9]{2},[0-9]{3} --> [0-9]{2,}:[0-9]{2}:[0-9]{2},[0-9]{3}")
_VTT_TIMING = re.compile(r"([0-9]{2,}:)?[0-9]{2}:[0-9]{2}\.[0-9]{3} --> ([0-9]{2,}:)?[0-9]{2}:[0-9]{2}\.[0-9]{3}")
_SBV_TIMING = re.compile(r"[0-9]+:[0-9]{2}:[0-9]{2}\.[0-9]{3},[0-9]+:[0-9]{2}:[0-9]{2}\.[0-9]{3}\Z")
CUE_FORMATS = ("srt", "vtt", "sbv")


def caption_format(lines):
    """The format a caption file declares by its own first lines, or None."""
    content = [line.strip() for line in lines[:20] if line.strip()]
    if not content:
        return None
    first = content[0]
    if first.startswith("WEBVTT"):
        return "vtt"
    if first.startswith("Scenarist_SCC"):
        return "scc"
    if first.startswith("<?xml") or first.startswith("<tt"):
        return "ttml" if any("<tt" in line for line in content) else None
    if first.isdigit() and len(content) > 1 and _SRT_TIMING.match(content[1]):
        return "srt"
    if _SBV_TIMING.match(first):
        return "sbv"
    return None


def cue_count(fmt, lines):
    if fmt in ("srt", "vtt"):
        pattern = _SRT_TIMING if fmt == "srt" else _VTT_TIMING
        return sum(1 for line in lines if pattern.match(line.strip()))
    if fmt == "sbv":
        return sum(1 for line in lines if _SBV_TIMING.match(line.strip()))
    return None


def read_caption_file(method, plan, content, *, page_index):
    """captions.download: one caption_file row; raises ResponseInvalid for a body that is not caption text."""
    text = _text_body(content)
    lines = text.splitlines()
    if len(lines) > MAX_CAPTION_LINES:
        raise ResponseInvalid("media_too_many_lines")
    detected = caption_format(lines)
    requested = plan.options.get("format")
    if requested is not None and text and detected != requested:
        raise ResponseInvalid("media_format_mismatch")
    if detected is not None:
        fmt = FieldValue(ValueState.KNOWN, detected)
    elif text:
        fmt = FieldValue(ValueState.UNKNOWN_ENUM, "unrecognized")
    else:
        fmt = FieldValue(ValueState.KNOWN_EMPTY, "")
    cues = cue_count(detected, lines)
    values = {
        "format": fmt,
        "byte_length": FieldValue(ValueState.KNOWN, len(content)),
        "sha256": FieldValue(ValueState.KNOWN, hashlib.sha256(bytes(content)).hexdigest()),
        "line_count": FieldValue(ValueState.KNOWN, len(lines)),
        "cue_count": (FieldValue(ValueState.KNOWN, cues) if cues is not None else
                      FieldValue(ValueState.ABSENT, None, "absent_means_not_applicable")),
        "text": FieldValue(ValueState.KNOWN, text) if text else FieldValue(ValueState.KNOWN_EMPTY, ""),
    }
    fields = {name: values[name] for name in plan.fields}
    target = plan.filter_values[0] if plan.filter_values else None
    row = Row(unit=method.row_unit, identity=("caption", target) if target else None, fields=fields,
              page_index=page_index)
    return PageParse((row,), (target,) if target else (), (), 0, 1, None)


class OwnerFamily(Hook):
    """Registry-only owner families: no semantics beyond the registry."""
