"""Versioned catalogue navigation and separately gated contextual facets.

No query construction, persistence, live snapshot or provider work belongs here.
Dynamic facets may reuse only authorized same-generation captured growth.
The checked-in JSON registry is consumed by both Python and the frontend.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from source_expiry import source_expiry, minimum_expiry, rows_expiry, stamp_aggregate
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from youtube_source_fields import youtube_duration_seconds
from catalogue_static import STATIC_PRESETS, normalize_static_bounds, matches_static_bounds, static_value, JS_WHITESPACE


PRESETS = json.loads(
    (Path(__file__).resolve().parent / "shared" / "catalogue-filter-presets.json").read_text(encoding="utf-8")
)
PUBLIC_AVAILABILITY = frozenset({"", "public"})
MAX_FILTER_JSON_LENGTH = 8192
GROWTH_FILTER_KEYS = frozenset({"min_views_gained", "max_views_gained", "min_tracked_days", "full_history"})
MAX_GROWTH_FILTER_BOUND = 10**15


def normalize_growth_filters(value: Any) -> dict[str, Any]:
    """Additive v1 conditions; no-op extensions never alter old Saved identity."""
    if not isinstance(value, dict) or set(value) - GROWTH_FILTER_KEYS:
        raise ValueError("Unsupported catalogue growth conditions")
    result = {}
    for key in ("min_views_gained", "max_views_gained", "min_tracked_days"):
        if key not in value:
            continue
        raw = value[key]
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be a string")
        raw = raw.strip(JS_WHITESPACE)
        if not raw:
            continue
        signed = key != "min_tracked_days"
        if len(raw) > 32 or not re.fullmatch(r"-?[0-9]+" if signed else r"[0-9]+", raw):
            raise ValueError(f"{key} must be a {'signed' if signed else 'nonnegative'} integer")
        number = int(raw)
        if abs(number) > MAX_GROWTH_FILTER_BOUND:
            raise ValueError(f"{key} exceeds the supported range")
        result[key] = str(number)
    if "full_history" in value:
        if type(value["full_history"]) is not bool:
            raise ValueError("full_history must be a boolean")
        if value["full_history"]:
            result["full_history"] = True
    lower, upper = result.get("min_views_gained"), result.get("max_views_gained")
    if lower is not None and upper is not None and int(lower) > int(upper):
        raise ValueError("max_views_gained must be greater than or equal to min_views_gained")
    return result


def matches_growth_filters(row: dict[str, Any], filters: dict[str, Any]) -> bool:
    if not filters:
        return True
    growth = row.get("view_growth")
    if not isinstance(growth, dict) or growth.get("status") not in {"ready", "partial"}:
        return False
    delta, interval = growth.get("delta_views"), growth.get("actual_interval_seconds")
    if type(interval) is not int or interval <= 0 or isinstance(delta, bool):
        return False
    if type(delta) is not int and not (isinstance(delta, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", delta)):
        return False
    value = int(delta)
    return (
        ("min_views_gained" not in filters or value >= int(filters["min_views_gained"]))
        and ("max_views_gained" not in filters or value <= int(filters["max_views_gained"]))
        and ("min_tracked_days" not in filters or interval >= int(filters["min_tracked_days"]) * 86400)
        and (not filters.get("full_history") or growth["status"] == "ready")
    )


def bucket_definitions(field: str) -> tuple:
    return tuple((item["key"], item["label"], item["min"], item["max"]) for item in PRESETS[field])


def _instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _strict_instant(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError(f"{field} must be a timezone-qualified ISO timestamp")
    if not value.endswith("Z"):
        # datetime.fromisoformat normalizes invalid minute offsets such as
        # +01:99. The wire contract must reject them, not change the instant.
        zone = value[-6:]
        if int(zone[1:3]) > 23 or int(zone[4:6]) > 59:
            raise ValueError(f"{field} contains an invalid timezone offset")
    parsed = _instant(value)
    if parsed is None:
        raise ValueError(f"{field} is not a valid timestamp")
    return parsed.isoformat().replace("+00:00", "Z")


# Publication years, days, weekdays and hours are counted in this zone: the
# viewer's own by default, or UTC when they choose it. Moments stay UTC.
TIME_ZONE_RE = re.compile(r"(?:UTC|[A-Za-z]+(?:/[A-Za-z0-9_+-]+){1,2})")


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def catalogue_time_zone(filters: dict[str, Any] | None) -> ZoneInfo:
    return _zone((filters or {}).get("time_zone", "UTC"))


def _time_zone(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64 or not TIME_ZONE_RE.fullmatch(value):
        raise ValueError("time_zone must be an IANA time zone name")
    try:
        _zone(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("time_zone must be an IANA time zone name") from error
    return value


def _year(value: Any, field: str) -> int:
    if type(value) is not int or not 1 <= value <= 9999:
        raise ValueError(f"{field} must contain years between 1 and 9999")
    return value


def normalize_catalogue_filters(value: Any, custom: dict[str, Any]) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_FILTER_JSON_LENGTH:
        raise ValueError("catalogue_filters must be a JSON string of at most 8192 characters")
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError) as error:
        raise ValueError("catalogue_filters must be valid JSON") from error
    keys = {"version", "view_buckets", "duration_buckets", "published_years", "published_older_than", "cohort", "bounds", "growth",
            "time_zone", *STATIC_PRESETS}
    if not isinstance(parsed, dict) or set(parsed) - keys:
        raise ValueError("catalogue_filters contains unsupported fields")
    if type(parsed.get("version")) is not int or parsed["version"] != 1:
        raise ValueError("unsupported catalogue_filters version")
    result: dict[str, Any] = {"version": 1}
    zone = _time_zone(parsed.get("time_zone", "UTC"))
    if zone != "UTC":
        result["time_zone"] = zone
    growth = normalize_growth_filters(parsed.get("growth", {}))
    if growth:
        result["growth"] = growth
    bounds = normalize_static_bounds(parsed.get("bounds", {}))
    for key, field, custom_keys in (
        ("view_buckets", "views", ("min_views", "max_views")),
        ("duration_buckets", "duration", ("min_duration_seconds", "max_duration_seconds")),
    ):
        values = parsed.get(key, [])
        valid = [item["key"] for item in PRESETS[field]]
        if not isinstance(values, list) or len(values) > len(valid) or any(not isinstance(item, str) or item not in valid for item in values):
            raise ValueError(f"{key} contains an unsupported preset")
        result[key] = [item for item in valid if item in values]
        if values and any(custom.get(name) is not None for name in custom_keys):
            raise ValueError(f"{key} cannot be combined with custom bounds of the same field")
    for key, field in STATIC_PRESETS.items():
        values = parsed.get(key, [])
        valid = [item["key"] for item in PRESETS[field]]
        if not isinstance(values, list) or len(values) > len(valid) or any(not isinstance(item, str) or item not in valid for item in values):
            raise ValueError(f"{key} contains an unsupported preset")
        if values:
            if any(f"{side}_{field}" in bounds for side in ("min", "max")):
                raise ValueError(f"{key} cannot be combined with custom bounds of the same field")
            result[key] = [item for item in valid if item in values]
    if bounds:
        result["bounds"] = bounds
    years = parsed.get("published_years", [])
    if not isinstance(years, list) or len(years) > 100:
        raise ValueError("published_years must be an array of at most 100 years")
    result["published_years"] = sorted({_year(year, "published_years") for year in years}, reverse=True)
    older = parsed.get("published_older_than")
    result["published_older_than"] = None if older is None else _year(older, "published_older_than")
    if (years or older is not None) and (custom.get("published_from") is not None or custom.get("published_before") is not None):
        raise ValueError("published presets cannot be combined with custom publication dates")
    cohort = parsed.get("cohort")
    if cohort is not None:
        if not isinstance(cohort, dict) or set(cohort) != {"kind", "published_from", "published_through"} or cohort.get("kind") != "overview":
            raise ValueError("unsupported catalogue cohort")
        start = None if cohort["published_from"] is None else _strict_instant(cohort["published_from"], "cohort.published_from")
        through = _strict_instant(cohort["published_through"], "cohort.published_through")
        if start is not None and _instant(start) > _instant(through):
            raise ValueError("cohort.published_through must not precede published_from")
        cohort = {"kind": "overview", "published_from": start, "published_through": through}
    result["cohort"] = cohort
    return result


def is_public_catalogue_row(row: dict[str, Any]) -> bool:
    return str(row.get("availability_status") or "").strip().lower() in PUBLIC_AVAILABILITY


def matches_publication_window(row: dict[str, Any], start: datetime | None, through: datetime) -> bool:
    published = _instant(row.get("published_at"))
    if published is None:
        return start is None
    return published <= through and (start is None or published >= start)


def is_catalogue_eligible(row: dict[str, Any], *, now: datetime) -> bool:
    # Freshness is enforced by the caller; missing publication remains eligible
    # in a lifetime scope exactly as in Overview MAX.
    return is_public_catalogue_row(row) and matches_publication_window(row, None, now)


def _bucket_matches(value: int | None, bucket: dict[str, Any]) -> bool:
    return value is not None and value >= 0 and (bucket["min"] is None or value >= bucket["min"]) and (bucket["max"] is None or value < bucket["max"])


def _views(row: dict[str, Any]) -> int | None:
    try:
        value = row.get("views")
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError, OverflowError):
        return None


def matches_catalogue_filters(row: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    if filters is None:
        return True
    if not matches_growth_filters(row, filters.get("growth", {})):
        return False
    if filters.get("bounds") and not matches_static_bounds(row, filters["bounds"]):
        return False
    for key, field in STATIC_PRESETS.items():
        selected = filters.get(key, [])
        if selected and not any(_bucket_matches(static_value(row, field), bucket) for bucket in PRESETS[field] if bucket["key"] in selected):
            return False
    for key, field in (
        ("view_buckets", "views"),
        ("duration_buckets", "duration"),
    ):
        selected = filters[key]
        if selected:
            value = _views(row) if field == "views" else youtube_duration_seconds(row.get("duration"))
            if not any(_bucket_matches(value, bucket) for bucket in PRESETS[field] if bucket["key"] in selected):
                return False
    years, older = filters["published_years"], filters["published_older_than"]
    if years or older is not None:
        published = _instant(row.get("published_at"))
        year = None if published is None else published.astimezone(catalogue_time_zone(filters)).year
        if year is None or not (year in years or (older is not None and year < older)):
            return False
    cohort = filters["cohort"]
    return cohort is None or matches_publication_window(row, _instant(cohort["published_from"]), _instant(cohort["published_through"]))


def build_channel_filter_facets(repository: Any, channel_id: str, *, video_type: str = "long", typed_catalogue_read: bool = False, catalogue_filters: Any = None, full_scope: bool = False, frozen_growth: bool = False, read_budget: Any = None, now: datetime | None = None, source_valid_until: datetime | None = None, **custom: Any) -> dict[str, Any]:
    # Late import avoids a cycle: Explorer uses the pure functions above, while
    # this separate descriptive read composes Explorer's existing validators.
    from channel_explorer import (
        CHANNEL_COLUMNS, VIDEO_COLUMNS, STREAM_VIDEO_COLUMNS, DB_BATCH_SIZE, SCAN_LIMIT,
        ChannelExplorerNotFoundError, _matches, _normalized_query,
        catalogue_video_type, catalogue_inventory_state, load_catalogue_inventory,
        catalogue_type_resolutions, catalogue_lifecycle_ready,
    )
    from catalogue_scope import CatalogueScopeRepository
    from hot_storage import source_is_current

    selected_video_type = catalogue_video_type(video_type)
    if full_scope:
        repository = CatalogueScopeRepository(repository, read_budget)

    query = _normalized_query(
        q=custom.get("q", ""), category_id=custom.get("category_id", ""),
        published_from=custom.get("published_from", ""), published_before=custom.get("published_before", ""),
        min_views=custom.get("min_views"), max_views=custom.get("max_views"),
        min_duration_seconds=custom.get("min_duration_seconds"), max_duration_seconds=custom.get("max_duration_seconds"),
        sort_by="published_at", sort_order="desc", limit=1, offset=0,
        catalogue_filters=catalogue_filters if catalogue_filters is not None else '{"version":1}',
        allow_growth_filters=frozen_growth,
    )
    generated_at = now or datetime.now(timezone.utc)
    generated_at = generated_at.replace(tzinfo=timezone.utc) if generated_at.tzinfo is None else generated_at.astimezone(timezone.utc)
    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")
    inventory_state, type_coverage = catalogue_inventory_state(repository, channel_id, selected_video_type,
        typed_catalogue_read=typed_catalogue_read, now=generated_at, source_valid_until=source_valid_until)
    inventory = load_catalogue_inventory(repository, channel_id, selected_video_type, full_scope=full_scope) if type_coverage["inventory_ready"] else []
    truncated = not full_scope and len(inventory) > SCAN_LIMIT
    if not full_scope:
        inventory = inventory[:SCAN_LIMIT]
    before_admission = len(inventory)
    inventory = [row for row in inventory if source_is_current(row.get("source_checked_at"), now=generated_at, valid_until=source_valid_until)]
    inventory_withheld = before_admission - len(inventory)
    video_ids = list(dict.fromkeys(str(item["video_id"]) for item in inventory if item.get("video_id")))
    video_ids, resolution_expiries, type_resolution_withheld = catalogue_type_resolutions(
        repository, channel_id, selected_video_type, video_ids, typed_catalogue_read=typed_catalogue_read,
        now=generated_at, source_valid_until=source_valid_until)
    rows_by_id = {}
    inventory_ids = set(video_ids)
    inventory_expiries = {str(item.get("video_id")): source_expiry(item, "source_checked_at") for item in inventory}
    for start in range(0, len(video_ids), DB_BATCH_SIZE):
        columns = (STREAM_VIDEO_COLUMNS if selected_video_type == "stream" else VIDEO_COLUMNS) + ",availability_status,tags"
        if selected_video_type != "long":
            columns += ",channel_id,publication_status"
        for row in repository.load_videos_by_ids(video_ids[start:start + DB_BATCH_SIZE], columns=columns):
            if str(row.get("video_id") or "") in inventory_ids:
                rows_by_id[str(row["video_id"])] = dict(row)
    same_channel = list(rows_by_id.values()) if selected_video_type == "long" or not typed_catalogue_read else [
        row for row in rows_by_id.values() if row.get("channel_id") == channel_id]
    identity_withheld = len(rows_by_id) - len(same_channel)
    lifecycle_rows = same_channel if selected_video_type == "long" else [row for row in same_channel
        if catalogue_lifecycle_ready(row, selected_video_type, now=generated_at)]
    lifecycle_withheld = len(same_channel) - len(lifecycle_rows)
    current = [row for row in lifecycle_rows if all(source_is_current(row.get(clock), now=generated_at, valid_until=source_valid_until) for clock in ("checked_at", "metadata_checked_at", "availability_checked_at"))]
    public = [row for row in current if row.get("availability_status") == "public"]
    eligible = [row for row in public if matches_publication_window(row, None, generated_at)]
    for row in eligible:
        row["source_expires_at"] = minimum_expiry(source_expiry(row, "checked_at", "metadata_checked_at", "availability_checked_at"), inventory_expiries.get(str(row.get("video_id"))), resolution_expiries.get(str(row.get("video_id"))), source_expiry(inventory_state))
    dynamic_filters = bool(query["catalogue_filters"].get("growth"))
    if dynamic_filters:
        from channel_video_growth import normalize_growth_for_video
        evidence = repository.load_frozen_video_growth([str(row["video_id"]) for row in eligible])
        evidence_by_id = {row["video_id"]: row for row in evidence}
        for row in eligible:
            row["view_growth"] = normalize_growth_for_video(row, evidence_by_id.get(row["video_id"]), now=generated_at, source_valid_until=source_valid_until)
    matched = [row for row in eligible if _matches(row, query)]

    def without(field: str) -> dict[str, Any]:
        other = dict(query)
        extra = dict(query["catalogue_filters"])
        other["catalogue_filters"] = extra
        if field == "views":
            other.update(min_views=None, max_views=None)
            extra["view_buckets"] = []
        elif field == "duration":
            other.update(min_duration_seconds=None, max_duration_seconds=None)
            extra["duration_buckets"] = []
        elif field in STATIC_PRESETS.values():
            extra.pop(next(key for key, value in STATIC_PRESETS.items() if value == field), None)
            bounds = {key: value for key, value in extra.get("bounds", {}).items() if key not in {f"min_{field}", f"max_{field}"}}
            if bounds:
                extra["bounds"] = bounds
            else:
                extra.pop("bounds", None)
        else:
            other.update(published_from=None, published_before=None)
            extra.update(published_years=[], published_older_than=None)
        return other

    facets = {}
    for field, getter in (("views", _views), ("duration", lambda row: youtube_duration_seconds(row.get("duration")))):
        other = without(field)
        # Reuse only an identical predicate within this response. Source expiry
        # and final session admission are still checked by their existing owners.
        sample = matched if other == query else [row for row in eligible if _matches(row, other)]
        expiry = minimum_expiry(rows_expiry(sample), *(source_expiry(row.get("view_growth")) for row in sample))
        values = [getter(row) for row in sample]
        facets[field] = [{**bucket, "source_expires_at": expiry, "count": sum(_bucket_matches(value, bucket) for value in values)} for bucket in PRESETS[field]]
    for field in STATIC_PRESETS.values():
        other = without(field)
        sample = matched if other == query else [row for row in eligible if _matches(row, other)]
        expiry = minimum_expiry(rows_expiry(sample), *(source_expiry(row.get("view_growth")) for row in sample))
        values = [static_value(row, field) for row in sample]
        facets[field] = [{**bucket, "source_expires_at": expiry, "count": sum(_bucket_matches(value, bucket) for value in values)} for bucket in PRESETS[field]]
    # Year options come from full eligible channel rows, never the active query
    # or loaded page. Counts still apply the other active fields.
    zone = catalogue_time_zone(query.get("catalogue_filters"))
    all_years = sorted({published.astimezone(zone).year for row in eligible if (published := _instant(row.get("published_at"))) is not None}, reverse=True)
    latest_years = all_years[:3]
    older_than = latest_years[-1] if len(all_years) > 3 else None
    year_query = without("published")
    year_sample = matched if year_query == query else [row for row in eligible if _matches(row, year_query)]
    year_values = [published.astimezone(zone).year for row in year_sample if (published := _instant(row.get("published_at"))) is not None]
    facets["published"] = {
        "years": [{"year": year, "count": year_values.count(year)} for year in latest_years],
        "older": None if older_than is None else {"before_year": older_than, "count": sum(year < older_than for year in year_values)},
    }
    stamp_aggregate(facets["published"], minimum_expiry(rows_expiry(eligible), *(source_expiry(row.get("view_growth")) for row in eligible)))
    missing_count = len(inventory_ids - set(rows_by_id))
    stale_count = len(lifecycle_rows) - len(current)
    result = {
        "contract_version": 1, "video_type": selected_video_type, "generated_at": generated_at.isoformat().replace("+00:00", "Z"), "channel_id": channel_id,
        "filters": {key: value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value for key, value in query.items() if key not in {"limit", "offset", "sort_by", "sort_order"}},
        "count_scope": "applied", "matched_count": len(matched), "facets": facets,
        "coverage": {
            "scope": f"typed_{selected_video_type}_inventory", "video_type": selected_video_type, **type_coverage,
            "inventory_sources_withheld": inventory_withheld, "type_resolution_withheld": type_resolution_withheld,
            "video_rows_withheld_lifecycle": lifecycle_withheld,
            "video_rows_withheld_identity": identity_withheld,
            "inventory_complete": type_coverage["inventory_ready"] and not truncated,
            "scan_limit": None if full_scope else SCAN_LIMIT, "scanned_count": len(inventory), "eligible_count": len(eligible),
            **({"scan_mode": "full_catalogue"} if full_scope else {}),
            "missing_video_row_count": missing_count, "video_rows_withheld_stale": stale_count,
            "availability_excluded_count": len(current) - len(public), "future_excluded_count": len(public) - len(eligible),
            "truncated": truncated, "complete": type_coverage["inventory_ready"] and not truncated and inventory_withheld == 0 and type_resolution_withheld == 0 and identity_withheld == 0 and lifecycle_withheld == 0 and missing_count == 0 and stale_count == 0 and len(current) == len(public), "transactional_snapshot": False,
        },
        "source": {
            "kind": "derived_catalogue_filter_facets", "youtube_requests": False, "database_writes": False,
            "snapshots_used": dynamic_filters,
            **({"growth_evidence": "same_browse_generation"} if dynamic_filters else {}),
        },
    }
    return repository.finish(result) if full_scope else result
