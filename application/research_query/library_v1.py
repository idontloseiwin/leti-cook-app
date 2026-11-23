"""Translate Library v1 saved views and video URLs to registry fields and operators.

Dates and publication years use the viewer's time zone. The selected Library tab
sets the video format. A condition that cannot be represented returns its missing
capability and leaves the complete saved definition available to the user."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re

from . import growth

LIBRARY_SELECT = ("video.id", "video.title", "video.thumbnail_url", "video.published_at",
                  "video.duration_seconds", "video.views", "video.likes", "video.comments",
                  "video.category_id")
PAGE_SIZE = 50
VIDEO_TYPES = ("long", "short", "stream")  # A Saved v1 view opens in the Long-form tab.

# Capabilities a v1 condition can still need: each result names it. Every v1
# condition has a registry field now; only an artificial address with more
# separate conditions than one shared query admits still needs one.
CAPABILITIES = {
    "query.where_leaf_budget": "more conditions than one shared query admits",
}

# Dates and years of publication are these two fields,
# evaluated in the request's time zone (time.timezone, the viewer's zone).
PUBLISHED_DATE, PUBLISHED_YEAR = "video.published_date", "video.published_year"

# v1 filter key -> (registry field, operator). Library keeps a row when
# min <= value <= max; an unknown value never matches.
BOUNDS = (
    ("min_views", "video.views", "gte"), ("max_views", "video.views", "lte"),
    ("min_duration_seconds", "video.duration_seconds", "gte"),
    ("max_duration_seconds", "video.duration_seconds", "lte"),
    ("min_likes", "video.likes", "gte"), ("max_likes", "video.likes", "lte"),
    ("min_comments", "video.comments", "gte"), ("max_comments", "video.comments", "lte"),
)
# The Library's calculated values (registry fields; Baseline grant at execution).
DERIVED_BOUNDS = (
    ("min_title_length", "video.title_length", "gte"), ("max_title_length", "video.title_length", "lte"),
    ("min_tag_count", "video.tag_count", "gte"), ("max_tag_count", "video.tag_count", "lte"),
    ("min_like_rate", "video.like_rate", "gte"), ("max_like_rate", "video.like_rate", "lte"),
    ("min_comment_rate", "video.comment_rate", "gte"), ("max_comment_rate", "video.comment_rate", "lte"),
)
# v1 facet key -> (preset group, registry field). A preset keeps
# min <= value < max; several presets of one group are alternatives.
PRESET_GROUPS = (
    ("view_buckets", "views", "video.views"), ("duration_buckets", "duration", "video.duration_seconds"),
    ("like_buckets", "likes", "video.likes"), ("comment_buckets", "comments", "video.comments"),
)
DERIVED_PRESETS = (("title_length_buckets", "title_length", "video.title_length"),
                   ("tag_count_buckets", "tag_count", "video.tag_count"))
SORTS = {"published_at": "video.published_at", "views": "video.views", "like_count": "video.likes",
         "comment_count": "video.comments", "duration": "video.duration_seconds", "title": "video.title"}
# Every v1 sort key: the Library's own and "Views gained" (partial included).
SORT_FIELDS = {**SORTS, **growth.LEGACY_SORT_FIELDS}
_PRESETS = json.loads((Path(__file__).resolve().parents[1] / "shared" / "catalogue-filter-presets.json")
                      .read_text(encoding="utf-8"))
# Duration presets per video type (the shared file's by_video_type sets); the
# other groups are shared. A v1 object keeps one set of conditions for every tab, so
# it may carry the presets of any type; the tab's translation accepts only its own.
DURATION_SETS = {kind: _PRESETS["duration"] if kind == "long"
                 else _PRESETS.get("by_video_type", {}).get(kind, {}).get("duration", _PRESETS["duration"])
                 for kind in VIDEO_TYPES}
DURATION_KEYS = tuple(dict.fromkeys(item["key"] for kind in VIDEO_TYPES for item in DURATION_SETS[kind]))
# Open ends of an instant range (an overview cohort never needs them).
BEFORE_ALL, AFTER_ALL = object(), object()
# Library's further filters. Optional v1 filter keys, absent when empty,
# so every older object and address keeps its exact form. Each one is a field
# of the registry; Description and declared paid promotion are offered only
# when those fields are collected for the tab's type, Stream ended on Streams.
MORE_FILTER_KEYS = ("captions", "description_q", "paid_promotion", "stream_ended_from", "stream_ended_before")
MORE_CHOICES = {"captions": ("video.caption_available", {"with": True, "without": False}),
                "paid_promotion": ("video.paid_promotion", {"declared": True, "not_declared": False})}
DESCRIPTION, STREAM_END = "video.description", "video.actual_end_at"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# JavaScript String.prototype.trim whitespace: the browser trims v1 text with it.
JS_WHITESPACE = ("\u0009\u000a\u000b\u000c\u000d          "
                 "        　﻿")
MAX_QUERY_LENGTH = 100  # Title search after the server's whitespace rule.
MAX_METRIC = 10**15  # Views and duration bounds.
MAX_BOUND = 10**15  # Static bounds and 28-day growth bounds.
MAX_FILTER_JSON_LENGTH = 8192  # The facets object as JSON text.
STATIC_FIELDS = ("likes", "comments", "title_length", "tag_count", "like_rate", "comment_rate")
STATIC_BOUND_KEYS = tuple(f"{side}_{field}" for field in STATIC_FIELDS for side in ("min", "max"))
STATIC_PRESETS = {"like_buckets": "likes", "comment_buckets": "comments",
                  "title_length_buckets": "title_length", "tag_count_buckets": "tag_count"}
GROWTH_FILTER_KEYS = frozenset({"min_views_gained", "max_views_gained", "min_tracked_days", "full_history"})
FACET_KEYS = frozenset({"version", "view_buckets", "duration_buckets", "published_years", "published_older_than",
                        "cohort", "bounds", "growth", "time_zone", *STATIC_PRESETS})
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


def normalize_static_bounds(value):
    """Static v1 bounds (filters min_/max_likes ... _comment_rate): canonical
    nonnegative integers, rates up to 6 decimals; ValueError otherwise."""
    if not isinstance(value, dict) or set(value) - set(STATIC_BOUND_KEYS):
        raise ValueError("unsupported static bounds")
    result = {}
    for key in STATIC_BOUND_KEYS:
        if key not in value:
            continue
        raw = value[key]
        if not isinstance(raw, str):
            raise ValueError(key)
        raw = raw.strip(JS_WHITESPACE)
        if not raw:
            continue
        pattern = r"[0-9]+(?:\.[0-9]{1,6})?" if key.endswith("_rate") else r"[0-9]+"
        if len(raw) > 32 or not re.fullmatch(pattern, raw):
            raise ValueError(key)
        number = Decimal(raw)
        if number > MAX_BOUND:
            raise ValueError(key)
        normalized = format(number, "f")
        result[key] = normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
    for field in STATIC_FIELDS:
        lower, upper = result.get(f"min_{field}"), result.get(f"max_{field}")
        if lower is not None and upper is not None and Decimal(lower) > Decimal(upper):
            raise ValueError(f"max_{field}")
    return result


def normalize_growth_filters(value):
    """facets.growth: signed integer gained bounds, whole tracked days, full
    history; an empty or false condition is absent (old objects keep their form)."""
    if not isinstance(value, dict) or set(value) - GROWTH_FILTER_KEYS:
        raise ValueError("unsupported growth conditions")
    result = {}
    for key in ("min_views_gained", "max_views_gained", "min_tracked_days"):
        if key not in value:
            continue
        raw = value[key]
        if not isinstance(raw, str):
            raise ValueError(key)
        raw = raw.strip(JS_WHITESPACE)
        if not raw:
            continue
        pattern = r"[0-9]+" if key == "min_tracked_days" else r"-?[0-9]+"
        if len(raw) > 32 or not re.fullmatch(pattern, raw) or abs(int(raw)) > MAX_BOUND:
            raise ValueError(key)
        result[key] = str(int(raw))
    if "full_history" in value:
        if type(value["full_history"]) is not bool:
            raise ValueError("full_history")
        if value["full_history"]:
            result["full_history"] = True
    lower, upper = result.get("min_views_gained"), result.get("max_views_gained")
    if lower is not None and upper is not None and int(lower) > int(upper):
        raise ValueError("max_views_gained")
    return result


def _cohort_instant(value):
    """A cohort end: an ISO instant with Z or a valid offset, as a UTC datetime."""
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise ValueError("cohort instant")
    # fromisoformat would turn an invalid offset such as +01:99 into another instant.
    if not value.endswith("Z") and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise ValueError("cohort offset")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _year(value):
    if type(value) is not int or not 1 <= value <= 9999:
        raise ValueError("year")
    return value


def _preset_keys(values, field):
    valid = [item["key"] for item in _PRESETS[field]]
    if not isinstance(values, list) or len(values) > len(valid) or any(
            not isinstance(item, str) or item not in valid for item in values):
        raise ValueError(f"{field} preset")
    return [item for item in valid if item in values]


def check_query(filters, facets):
    """The rest of the v1 check, after saved_work: the sort, ranges of counters,
    durations and dates, and the facets object, exactly as the Library server
    refused them; nothing is read or calculated. `filters` are saved_work's
    trimmed strings, `facets` carries the static bounds under "bounds". Returns
    the sort key and the canonical facets; ValueError when Library refused it.
    Search text and category are already within their limits here."""
    from .contracts import time_zone
    sort_by = str(filters["sort_by"] or "published_at").strip()
    if sort_by not in SORT_FIELDS:
        raise ValueError("sort_by")
    if filters["published_from"] and filters["published_before"] and (
            date.fromisoformat(filters["published_from"]) >= date.fromisoformat(filters["published_before"])):
        raise ValueError("published_before must be after published_from")
    custom = {}
    for key in ("min_views", "max_views", "min_duration_seconds", "max_duration_seconds"):
        custom[key] = int(filters[key]) if filters[key] else None
        if custom[key] is not None and custom[key] > MAX_METRIC:
            raise ValueError(key)
    for low, high in (("min_views", "max_views"), ("min_duration_seconds", "max_duration_seconds")):
        if custom[low] is not None and custom[high] is not None and custom[low] > custom[high]:
            raise ValueError(high)
    text = json.dumps(facets)
    if len(text) > MAX_FILTER_JSON_LENGTH:
        raise ValueError("facets exceed 8192 characters")
    parsed = json.loads(text)
    if not isinstance(parsed, dict) or set(parsed) - FACET_KEYS:
        raise ValueError("unsupported facets")
    if type(parsed.get("version")) is not int or parsed["version"] != 1:
        raise ValueError("facets version")
    result = {"version": 1}
    zone = time_zone(parsed.get("time_zone", "UTC"))
    if zone != "UTC":
        result["time_zone"] = zone
    growth_filters = normalize_growth_filters(parsed.get("growth", {}))
    if growth_filters:
        result["growth"] = growth_filters
    bounds = normalize_static_bounds(parsed.get("bounds", {}))
    for key, field, low, high in (("view_buckets", "views", "min_views", "max_views"),
                                  ("duration_buckets", "duration", "min_duration_seconds", "max_duration_seconds")):
        values = parsed.get(key, [])
        result[key] = _preset_keys(values, field)
        if values and (custom[low] is not None or custom[high] is not None):
            raise ValueError(f"{key} with custom bounds")
    for key, field in STATIC_PRESETS.items():
        values = parsed.get(key, [])
        selected = _preset_keys(values, field)
        if values:
            if f"min_{field}" in bounds or f"max_{field}" in bounds:
                raise ValueError(f"{key} with custom bounds")
            result[key] = selected
    if bounds:
        result["bounds"] = bounds
    years = parsed.get("published_years", [])
    if not isinstance(years, list) or len(years) > 100:
        raise ValueError("published_years")
    result["published_years"] = sorted({_year(year) for year in years}, reverse=True)
    older = parsed.get("published_older_than")
    result["published_older_than"] = None if older is None else _year(older)
    if (years or older is not None) and (filters["published_from"] or filters["published_before"]):
        raise ValueError("published presets with custom dates")
    cohort = parsed.get("cohort")
    if cohort is not None:
        if (not isinstance(cohort, dict) or set(cohort) != {"kind", "published_from", "published_through"}
                or cohort.get("kind") != "overview"):
            raise ValueError("cohort")
        start = None if cohort["published_from"] is None else _cohort_instant(cohort["published_from"])
        through = _cohort_instant(cohort["published_through"])
        if start is not None and start > through:
            raise ValueError("cohort.published_through must not precede published_from")
        cohort = {"kind": "overview", "published_from": None if start is None else start.isoformat().replace("+00:00", "Z"),
                  "published_through": through.isoformat().replace("+00:00", "Z")}
    result["cohort"] = cohort
    return sort_by, result


@dataclass(frozen=True)
class MissingCapability:
    capability: str
    conditions: tuple[str, ...]


@dataclass(frozen=True)
class LibraryTranslation:
    status: str  # "translated" or "needs_capability"
    query: dict | None
    time_zone: str
    missing: tuple[MissingCapability, ...] = ()

    def payload(self):
        return {"status": self.status, "query": self.query, "time_zone": self.time_zone,
                "missing": [{"capability": item.capability, "conditions": list(item.conditions)}
                            for item in self.missing]}


def _collapsed(value, maximum):
    """The server's text rule of a v1 field: Python strip, every whitespace run
    as one space, at most `maximum` characters."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) > maximum:
        raise ValueError(f"at most {maximum} characters")
    return text


def search_literal(value):
    """Library title search text: JavaScript trim in the browser, then the
    server strips and collapses Python whitespace runs to one space. The title
    itself is never collapsed; contains_casefold_v1 folds both sides."""
    return _collapsed(value.strip(JS_WHITESPACE), MAX_QUERY_LENGTH)


def _literal(value):
    return value if isinstance(value, str) else value.isoformat().replace("+00:00", "Z")


def _never(field):
    return {"all": [{"field": field, "op": "is_known"}, {"field": field, "op": "is_unknown"}]}


def _range(field, lower, upper, *, inclusive_upper=False):
    """lower <= value < upper (<= upper when inclusive); None is no bound."""
    if lower is AFTER_ALL or upper is BEFORE_ALL:
        return _never(field)
    leaves = []
    if lower is not None and lower is not BEFORE_ALL:
        leaves.append({"field": field, "op": "gte", "value": _literal(lower)})
    if upper is not None and upper is not AFTER_ALL:
        leaves.append({"field": field, "op": "lte" if inclusive_upper else "lt", "value": _literal(upper)})
    if not leaves:
        return {"field": field, "op": "is_known"}
    return leaves[0] if len(leaves) == 1 else {"all": leaves}


def _alternatives(field, intervals):
    """Union of half-open intervals; touching neighbours merge (same set)."""
    merged = []
    for lower, upper in intervals:
        if merged and merged[-1][1] is not None and merged[-1][1] is not AFTER_ALL and merged[-1][1] == lower:
            merged[-1] = (merged[-1][0], upper)
        else:
            merged.append((lower, upper))
    nodes = [_range(field, lower, upper) for lower, upper in merged]
    return nodes[0] if len(nodes) == 1 else {"any": nodes}


def _preset_intervals(group, keys, video_type="long"):
    items = DURATION_SETS[video_type] if group == "duration" else _PRESETS[group]
    selected = set(keys)
    if selected - {item["key"] for item in items}:
        raise ValueError("preset_of_another_video_type")
    return [(None if item["min"] is None else str(item["min"]), None if item["max"] is None else str(item["max"]))
            for item in items if item["key"] in selected]


def split_typed_duration(keys):
    """(Long duration presets, presets of the other types) of a v1 list; ValueError
    for a key of no type. Long keys go through the Library's own validation."""
    long_keys = {item["key"] for item in DURATION_SETS["long"]}
    other = [key for key in keys if key not in long_keys]
    if any(key not in DURATION_KEYS for key in other):
        raise ValueError("duration_buckets contains an unsupported preset")
    return [key for key in keys if key in long_keys], other


def ordered_duration(keys):
    """Duration preset keys in one fixed order (Long, Shorts, Streams), as the browser codec."""
    selected = set(keys)
    return [key for key in DURATION_KEYS if key in selected]


def publication_date_conditions(filters, facets):
    """Publication dates and years as calendar fields of the request's time zone
    From is the local date itself, the API's exclusive before date a
    lt bound; selected years and "older than" are local years."""
    nodes = []
    if filters["published_from"]:
        nodes.append({"field": PUBLISHED_DATE, "op": "gte", "value": filters["published_from"]})
    if filters["published_before"]:
        nodes.append({"field": PUBLISHED_DATE, "op": "lt", "value": filters["published_before"]})
    years, older = facets["published_years"], facets["published_older_than"]
    alternatives = ([{"field": PUBLISHED_YEAR, "op": "in", "value": [str(year) for year in sorted(years)]}] if years else []) + (
        [{"field": PUBLISHED_YEAR, "op": "lt", "value": str(older)}] if older is not None else [])
    if alternatives:
        nodes.append(alternatives[0] if len(alternatives) == 1 else {"any": alternatives})
    return nodes


def normalize_more_filters(raw):
    """The further filters of a v1 object: strings, trimmed like the
    browser (JavaScript whitespace); an empty value is absent. ValueError for
    anything else, so a stored object never carries an unknown choice."""
    result = {}
    for key in MORE_FILTER_KEYS:
        if key not in raw:
            continue
        if not isinstance(raw[key], str):
            raise ValueError(key)
        value = raw[key].strip(JS_WHITESPACE)
        if not value:
            continue
        if key in MORE_CHOICES:
            if value not in MORE_CHOICES[key][1]:
                raise ValueError(key)
        elif key == "description_q":
            # The browser counts UTF-16 units, like the title search (at most 100).
            if len(value.encode("utf-16-le")) // 2 > 100 or "\x00" in value:
                raise ValueError(key)
        else:
            if _DATE.fullmatch(value) is None or value.startswith("0000"):
                raise ValueError(key)
            parsed = date.fromisoformat(value)
            if key == "stream_ended_before":
                parsed - timedelta(days=1)  # The exclusive end of an existing inclusive day.
        result[key] = value
    return result


def _local_midnight(text, zone):
    """Midnight of a calendar day in the zone (Python datetime, fold 0) as a UTC
    instant; outside 0001..9999 an open end."""
    if text is None:
        return None
    day = date.fromisoformat(text)
    try:
        return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return BEFORE_ALL if day.year < 5000 else AFTER_ALL


def more_conditions(filters, zone_name):
    """Query nodes of the further filters, in their fixed order (after every
    older Library filter, so older translations keep their exact shape)."""
    from zoneinfo import ZoneInfo
    more, nodes = normalize_more_filters(filters), []
    for key in ("captions", "paid_promotion"):
        if key in more:
            field, choices = MORE_CHOICES[key]
            nodes.append({"field": field, "op": "eq", "value": choices[more[key]]})
    if "description_q" in more:
        literal = search_literal(more["description_q"])
        if literal:
            nodes.append({"field": DESCRIPTION, "op": "contains_casefold_v1", "value": literal})
    if "stream_ended_from" in more or "stream_ended_before" in more:
        zone = ZoneInfo(zone_name)
        nodes.append(_range(STREAM_END, _local_midnight(more.get("stream_ended_from"), zone),
                            _local_midnight(more.get("stream_ended_before"), zone)))
    return nodes


def leaf_count(node):
    if not isinstance(node, dict):
        return 0
    if "field" in node:
        return 1
    return sum(leaf_count(child) for key in ("all", "any") for child in node.get(key, ())) + leaf_count(node.get("not"))


def translate(conditions, *, channel_id, time_zone="UTC", video_type="long"):
    """Translate one v1 object for one Library tab (video type); invalid input
    raises SavedWorkError or ValueError."""
    from saved_work import channel_id as valid_channel, normalize_conditions
    from .contracts import MAX_LEAVES, time_zone as valid_time_zone
    from .operations import OPERATIONS
    from .validation import AccessContext, QueryRejected, canonical, validate

    channel = valid_channel(channel_id)
    if video_type not in VIDEO_TYPES:
        raise ValueError("unsupported_video_type")
    value = normalize_conditions(conditions)
    if value.get("version") != 1:
        raise ValueError("not_library_v1")
    filters, facets = value["filters"], value["facets"]
    # A stored zone (the Library codec never writes one) wins over the viewer's.
    zone_name = valid_time_zone(facets.get("time_zone", time_zone))
    missing, leaves = {}, []

    def need(capability, condition):
        missing.setdefault(capability, []).append(condition)

    literal = search_literal(filters["q"])
    if literal:
        leaves.append({"field": "video.title", "op": "contains_casefold_v1", "value": literal})
    if filters["category_id"]:
        leaves.append({"field": "video.category_id", "op": "eq", "value": filters["category_id"]})
    leaves.extend(publication_date_conditions(filters, facets))
    for key, field, op in BOUNDS:
        if filters.get(key):
            leaves.append({"field": field, "op": op, "value": filters[key]})
    for key, field, op in DERIVED_BOUNDS:
        if filters.get(key):
            leaves.append({"field": field, "op": op, "value": filters[key]})
    for key, group, field in PRESET_GROUPS:
        if facets.get(key):
            leaves.append(_alternatives(field, _preset_intervals(group, facets[key], video_type)))
    for key, group, field in DERIVED_PRESETS:
        if facets.get(key):
            leaves.append(_alternatives(field, _preset_intervals(group, facets[key])))
    cohort = facets["cohort"]
    if cohort is not None:
        window = _range("video.published_at", cohort["published_from"], cohort["published_through"], inclusive_upper=True)
        # Library keeps an unknown publication date only in a cohort without a start.
        leaves.append(window if cohort["published_from"] is not None
                      else {"any": [{"field": "video.published_at", "op": "is_unknown"}, window]})
    leaves.extend(more_conditions(filters, zone_name))
    # 28-day growth: one conjunction for saved growth filters.
    growth_node = growth.legacy_conditions(facets.get("growth"))
    if growth_node is not None:
        leaves.append(growth_node)
    if sum(leaf_count(node) for node in leaves) > MAX_LEAVES:
        need("query.where_leaf_budget", "conditions")
    if missing:
        return LibraryTranslation("needs_capability", None, zone_name, tuple(
            MissingCapability(capability, tuple(found)) for capability, found in sorted(missing.items())))
    query = {"query_version": 1, "source": {"id": "public.videos.current.v1"},
             "scope": {"kind": "channel", "id": channel}, "video_types": [video_type],
             "select": list(LIBRARY_SELECT), "time": {"mode": "current", "timezone": zone_name},
             "order_by": [{"field": SORT_FIELDS[filters["sort_by"]], "direction": filters["sort_order"]}],
             "page": {"size": PAGE_SIZE}}
    if leaves:
        query["where"] = leaves[0] if len(leaves) == 1 else {"all": leaves}
    try:
        plan = validate(canonical(query), AccessContext("definition", "v1", enabled=True,
                                                        allowed_operations=frozenset(OPERATIONS)))
    except QueryRejected as error:
        raise ValueError("library_translation_rejected:" + error.code) from None
    return LibraryTranslation("translated", json.loads(plan.encoded), zone_name)
