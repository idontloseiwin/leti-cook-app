"""Pure assembly of the bounded, read-only Channel Explorer contract."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from hot_storage import HOT_DATA_MAX_AGE, register_read_deadline, source_is_current, utc_instant
from hot_storage_fields import channel_source_fields

from catalogue_scope import CatalogueReadBudget, CatalogueScopeRepository

from catalogue_filters import (
    catalogue_time_zone,
    is_catalogue_eligible,
    is_public_catalogue_row,
    matches_catalogue_filters,
    normalize_catalogue_filters,
)
from source_expiry import source_expiry, minimum_expiry, captured_source_call
from youtube_categories import youtube_video_category_name
from youtube_source_fields import youtube_duration_seconds
from catalogue_static import stored_tags
from video_thumbnails import video_thumbnail_url


CHANNEL_COLUMNS = (
    "channel_id,title,subscribers,views,video_count,"
    "country_code,country_collected_at,channel_created_at,channel_metadata_checked_at,channel_metadata_status,is_hidden"
)
VIDEO_COLUMNS = (
    "video_id,title,published_at,checked_at,metadata_checked_at,duration,views,"
    "like_count,comment_count,category_id,availability_status,availability_checked_at"
)
STREAM_VIDEO_COLUMNS = VIDEO_COLUMNS + ",stream_actual_end_at"
FORMAT_TOTAL_VIDEO_COLUMNS = "video_id,checked_at,views,availability_status,availability_checked_at"
PUBLIC_CHANNEL_FIELDS = (
    "source_expires_at",
    "channel_id",
    "title",
    "subscribers",
    "views",
    "video_count",
    "country_code",
    "channel_metadata_checked_at",
)
PROFILE_CHANNEL_FIELDS = (
    "source_expires_at",
    "channel_id",
    "title",
    "subscribers",
    "views",
    "video_count",
    "country_code",
    "channel_created_at",
    "channel_metadata_checked_at",
)
PUBLIC_VIDEO_FIELDS = (
    "source_expires_at",
    "video_id",
    "title",
    "thumbnail_url",
    "published_at",
    "checked_at",
    "duration",
    "views",
    "like_count",
    "comment_count",
    "category_id",
)
SORT_FIELDS = {
    "published_at",
    "views",
    "like_count",
    "comment_count",
    "duration",
    "title",
}
SCAN_LIMIT = 5000
# Format totals add up a format's whole stored list. YouTube lists about 20,000
# uploads per list; this bound only stops a runaway read.
FORMAT_TOTAL_SCAN_LIMIT = 1_000_000
SAFE_JSON_INTEGER = 2**53 - 1
DB_BATCH_SIZE = 500
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 100
MAX_METRIC = 10**15
RAW_DATA_MAX_AGE = HOT_DATA_MAX_AGE
SOURCE_CLOCK_FUTURE_TOLERANCE = timedelta(0)


class ChannelExplorerNotFoundError(LookupError):
    pass


class ChannelExplorerQueryError(ValueError):
    pass


def build_channel_profile(
    repository: Any,
    channel_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the read-only stored profile consumed only by Updated Explorer."""

    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")
    channel_data_fresh = channel.get("channel_metadata_status") == "ok" and source_is_current(
        channel.get("channel_metadata_checked_at"), now=generated_at,
    )
    admitted_channel = channel_source_fields(channel, now=generated_at)
    admitted_channel["source_expires_at"] = source_expiry(admitted_channel,
        *( ["channel_metadata_checked_at"] if channel_data_fresh else []),
        *( ["country_collected_at"] if admitted_channel.get("country_code") is not None else []))
    public_channel = {field: admitted_channel.get(field) for field in PROFILE_CHANNEL_FIELDS}
    count_notice = None
    if channel_data_fresh:
        counts = repository.load_recent_public_video_counts(
            channel_id, recorded_through=channel["channel_metadata_checked_at"])
        if len(counts) == 2:
            latest, previous = counts
            if (all(type(row.get("video_count")) is int and row["video_count"] >= 0
                    and source_is_current(row.get("recorded_at"), now=generated_at) for row in counts)
                    and utc_instant(previous["recorded_at"]) < utc_instant(latest["recorded_at"])
                    and utc_instant(latest["recorded_at"]) == utc_instant(channel["channel_metadata_checked_at"])
                    and latest["video_count"] == channel.get("video_count")
                    and latest["video_count"] < previous["video_count"]):
                count_notice = {"code": "public_video_count_decreased",
                    "checked_at": latest["recorded_at"],
                    "source_expires_at": minimum_expiry(*(source_expiry(row, "recorded_at") for row in counts))}

    return {
        "contract_version": 1,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "channel": public_channel,
        "public_video_count_notice": count_notice,
        "freshness": {
            "maximum_age_days": RAW_DATA_MAX_AGE.days,
            "channel_data_fresh": channel_data_fresh,
        },
        "source": {
            "kind": "stored_channel_profile",
            "youtube_requests": False,
            "database_writes": False,
        },
    }


def _json_integer(value: int) -> int | str:
    return str(value) if abs(value) > SAFE_JSON_INTEGER else value


def _build_typed_format_total(
    repository: Any,
    channel_id: str,
    *,
    video_type: str,
    now: datetime,
) -> dict[str, Any]:
    """Header-only sum; this does not activate a typed catalogue surface."""
    state = repository.load_inventory_state(channel_id, video_type=video_type)
    if not state or state.get("inventory_status") not in ("completed", "playlist_not_found"):
        status = "incomplete" if state and state.get("inventory_status") in ("running", "error") else "not_collected"
        return {"status": status, "views": None, "reason": "inventory_not_completed"}
    if not _is_current(state.get("source_checked_at"), now=now, maximum_age=RAW_DATA_MAX_AGE):
        return {"status": "stale", "views": None, "reason": "inventory_stale"}
    if state.get("inventory_status") == "playlist_not_found":
        return {"status": "absent", "views": None, "reason": "playlist_not_found",
                "source_expires_at": source_expiry(state, "source_checked_at")}

    inventory = repository.load_format_inventory(channel_id, video_type=video_type, limit=FORMAT_TOTAL_SCAN_LIMIT + 1)
    truncated = len(inventory) > FORMAT_TOTAL_SCAN_LIMIT
    video_ids = list(dict.fromkeys(str(row["video_id"]) for row in inventory[:FORMAT_TOTAL_SCAN_LIMIT] if row.get("video_id")))
    inventory_by_id = {str(row["video_id"]): row for row in inventory[:FORMAT_TOTAL_SCAN_LIMIT] if row.get("video_id")}
    admitted_ids = [key for key in video_ids if source_is_current(inventory_by_id[key].get("source_checked_at"), now=now)]
    scope_complete = repository.count_canonical_inventory(channel_id, video_type=video_type, exclude_waiting=True) == len(video_ids)
    requested_ids = set(admitted_ids)
    resolutions = repository.load_type_resolutions(admitted_ids, video_type=video_type) if admitted_ids else []
    resolved_ids = {
        str(row.get("video_id") or "")
        for row in resolutions
        if row.get("resolution_status") == "resolved"
        and row.get("canonical_video_type") == video_type
        and (
            row.get("manual_valid") is True
            or row.get("automatic_valid") is True
            or (
                row.get("present_membership_types") == [video_type]
                and row.get("present_membership_channels") == [channel_id]
            )
        )
    } & requested_ids
    resolution_expiries = [source_expiry(row) for row in resolutions if str(row.get("video_id") or "") in resolved_ids]
    resolved_video_ids = [video_id for video_id in video_ids if video_id in resolved_ids]
    columns = FORMAT_TOTAL_VIDEO_COLUMNS + ",channel_id"
    if video_type == "stream":
        columns += ",metadata_checked_at,stream_actual_end_at"
    rows_by_id = {}
    for start in range(0, len(resolved_video_ids), DB_BATCH_SIZE):
        for row in repository.load_videos_by_ids(resolved_video_ids[start:start + DB_BATCH_SIZE], columns=columns):
            video_id = str(row.get("video_id") or "")
            if video_id in resolved_ids and row.get("channel_id") == channel_id:
                rows_by_id[video_id] = row

    excluded = {
        "type_resolution_withheld": len(admitted_ids) - len(resolved_ids),
        "stale_inventory_count": len(video_ids) - len(admitted_ids),
        "availability_withheld_count": 0,
        "missing_video_row_count": 0,
        "stream_completion_withheld": 0,
        "stale_counter_count": 0,
        "stale_metadata_count": 0,
        "unknown_view_count": 0,
    }
    known_views = []
    # Only public views are known. A video confirmed as no longer public leaves
    # the sum; an availability we could not confirm withholds the total.
    unavailable_excluded = 0
    used_expiries = [source_expiry(state, "source_checked_at"), *resolution_expiries,
                     *(source_expiry(inventory_by_id[key], "source_checked_at") for key in admitted_ids)]
    for video_id in resolved_video_ids:
        row = rows_by_id.get(video_id)
        if row is None:
            excluded["missing_video_row_count"] += 1
            continue
        if not source_is_current(row.get("availability_checked_at"), now=now):
            excluded["availability_withheld_count"] += 1
            continue
        if row.get("availability_status") != "public":
            unavailable_excluded += 1
            continue
        if video_type == "stream":
            completed_at = _timestamp(row.get("stream_actual_end_at"))
            if completed_at is None or completed_at > now:
                excluded["stream_completion_withheld"] += 1
                continue
        if not _is_current(row.get("checked_at"), now=now, maximum_age=RAW_DATA_MAX_AGE):
            excluded["stale_counter_count"] += 1
            continue
        if video_type == "stream" and not _is_current(row.get("metadata_checked_at"), now=now, maximum_age=RAW_DATA_MAX_AGE):
            excluded["stale_metadata_count"] += 1
            continue
        views = _number(row.get("views"))
        if views is None or views < 0:
            excluded["unknown_view_count"] += 1
            continue
        known_views.append(views)
        used_expiries.append(source_expiry(row, "checked_at", "availability_checked_at",
                                           *(["metadata_checked_at"] if video_type == "stream" else [])))

    missing_count = sum(excluded.values())
    complete = not truncated and scope_complete and missing_count == 0
    only_stale = missing_count > 0 and missing_count == excluded["stale_counter_count"] + excluded["stale_metadata_count"] + excluded["stale_inventory_count"]
    return {
        "source_expires_at": minimum_expiry(*used_expiries),
        "status": "ready" if complete else "stale" if not truncated and scope_complete and only_stale else "incomplete",
        "views": _json_integer(sum(known_views)) if complete else None,
        **({"reason": "inventory_sources_incomplete"} if not scope_complete else {}),
        "scanned_inventory_count": len(video_ids),
        "known_view_count": len(known_views),
        "missing_view_count": missing_count,
        "coverage": {
            "scan_limit": FORMAT_TOTAL_SCAN_LIMIT,
            "inventory_complete": not truncated and scope_complete,
            "inventory_ready": True,
            "unavailable_excluded_count": unavailable_excluded,
            # Recorded catalogue exposure is disclosed, never changed or reused
            # as authorization for this separately accepted header-only sum.
            "product_exposure_ready": state.get("product_exposure_ready") is True,
            "transactional_snapshot": False,
            **excluded,
        },
    }


def build_channel_format_totals(
    repository: Any,
    channel_id: str,
    *,
    include_typed: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build exact long views; opt-in adds header-only typed totals without writes."""

    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")

    inventory = repository.load_format_inventory(channel_id, video_type="long", limit=FORMAT_TOTAL_SCAN_LIMIT + 1)
    truncated = len(inventory) > FORMAT_TOTAL_SCAN_LIMIT
    inventory = inventory[:FORMAT_TOTAL_SCAN_LIMIT]
    video_ids = list(dict.fromkeys(
        str(row.get("video_id"))
        for row in inventory
        if row.get("video_id")
    ))
    inventory_by_id = {str(row["video_id"]): row for row in inventory if row.get("video_id")}
    admitted_ids = [key for key in video_ids if source_is_current(inventory_by_id[key].get("source_checked_at"), now=generated_at)]
    stale_inventory_count = len(video_ids) - len(admitted_ids)
    scope_complete = repository.count_canonical_inventory(channel_id, video_type="long", exclude_waiting=True) == len(video_ids)
    requested_ids = set(admitted_ids)

    rows: list[dict[str, Any]] = []
    for start in range(0, len(admitted_ids), DB_BATCH_SIZE):
        rows.extend(repository.load_videos_by_ids(
            admitted_ids[start:start + DB_BATCH_SIZE],
            columns=FORMAT_TOTAL_VIDEO_COLUMNS,
        ))
    rows_by_id = {
        str(row.get("video_id")): dict(row)
        for row in rows
        if str(row.get("video_id") or "") in requested_ids
    }

    missing_video_row_count = sum(video_id not in rows_by_id for video_id in admitted_ids)
    stale_counter_count = 0
    availability_withheld_count = 0
    unavailable_excluded_count = 0
    unknown_view_count = 0
    known_views: list[int] = []
    used_expiries = [source_expiry(inventory_by_id[key], "source_checked_at") for key in admitted_ids]
    for video_id in admitted_ids:
        row = rows_by_id.get(video_id)
        if row is None:
            continue
        # Only public views are known: a confirmed non-public video leaves the
        # sum, an unconfirmed availability withholds the total.
        if not source_is_current(row.get("availability_checked_at"), now=generated_at):
            availability_withheld_count += 1
            continue
        if row.get("availability_status") != "public":
            unavailable_excluded_count += 1
            continue
        if not _is_current(
            row.get("checked_at"),
            now=generated_at,
            maximum_age=RAW_DATA_MAX_AGE,
        ):
            stale_counter_count += 1
            continue
        views = _number(row.get("views"))
        if views is None or views < 0:
            unknown_view_count += 1
            continue
        known_views.append(views)
        used_expiries.append(source_expiry(row, "checked_at", "availability_checked_at"))

    inventory_complete = not truncated and scope_complete
    complete = (
        inventory_complete
        and missing_video_row_count == 0
        and stale_counter_count == 0
        and stale_inventory_count == 0
        and availability_withheld_count == 0
        and unknown_view_count == 0
    )
    if complete:
        long_status = "ready"
    elif (stale_counter_count + stale_inventory_count > 0 and inventory_complete
          and missing_video_row_count == 0 and unknown_view_count == 0 and availability_withheld_count == 0):
        long_status = "stale"
    else:
        long_status = "incomplete"

    result = {
        "contract_version": 1,
        "channel_id": channel_id,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "formats": {
            "long": {
                "source_expires_at": minimum_expiry(*used_expiries),
                "status": long_status,
                "views": _json_integer(sum(known_views)) if complete else None,
                **({"reason": "inventory_sources_incomplete"} if not scope_complete else {}),
                "scanned_inventory_count": len(video_ids),
                "known_view_count": len(known_views),
                "missing_view_count": max(0, len(video_ids) - len(known_views) - unavailable_excluded_count),
            },
            "short": {"status": "not_collected", "views": None},
            "stream": {"status": "not_collected", "views": None},
        },
        "coverage": {
            "scan_limit": FORMAT_TOTAL_SCAN_LIMIT,
            "inventory_complete": inventory_complete,
            "unavailable_excluded_count": unavailable_excluded_count,
            "missing_video_row_count": missing_video_row_count,
            "unknown_view_count": unknown_view_count,
            "stale_counter_count": stale_counter_count,
            "stale_inventory_count": stale_inventory_count,
            "availability_withheld_count": availability_withheld_count,
            "transactional_snapshot": False,
        },
        "source": {
            "kind": "derived_from_stored_typed_video_inventory",
            "youtube_requests": False,
            "database_writes": False,
            "snapshots_used": False,
            "freshness_enforced": True,
        },
    }
    if include_typed is True:
        inventory_proof = dict(repository.load_list_inventory(channel_id, limit=FORMAT_TOTAL_SCAN_LIMIT))
        profile_fresh = channel.get("channel_metadata_status") == "ok" and source_is_current(
            channel.get("channel_metadata_checked_at"), now=generated_at)
        inventory_proof["youtube_video_count"] = _number(channel.get("video_count")) if profile_fresh else None
        inventory_proof["source_expires_at"] = minimum_expiry(
            inventory_proof.get("source_expires_at"),
            source_expiry(channel, "channel_metadata_checked_at") if profile_fresh else None)
        result["inventory"] = inventory_proof
        for video_type in ("short", "stream"):
            result["formats"][video_type] = _build_typed_format_total(
                repository, channel_id, video_type=video_type, now=generated_at,
            )
    return result


def _text(value: Any, *, field: str, maximum: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    if len(normalized) > maximum:
        raise ChannelExplorerQueryError(
            f"{field} must be at most {maximum} characters"
        )
    return normalized


def _date(value: Any, *, field: str) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        raise ChannelExplorerQueryError(f"{field} must use YYYY-MM-DD")
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ChannelExplorerQueryError(f"{field} is not a valid date") from error


def _metric(value: Any, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ChannelExplorerQueryError(f"{field} must be an integer") from error
    if parsed < 0 or parsed > MAX_METRIC:
        raise ChannelExplorerQueryError(f"{field} is outside the supported range")
    return parsed


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_current(value: Any, *, now: datetime, maximum_age: timedelta) -> bool:
    if not source_is_current(value, now=now):
        return False
    return utc_instant(now) - utc_instant(value) <= maximum_age


def _normalized_query(
    *,
    q: Any,
    category_id: Any,
    published_from: Any,
    published_before: Any,
    min_views: Any,
    max_views: Any,
    min_duration_seconds: Any,
    max_duration_seconds: Any,
    sort_by: Any,
    sort_order: Any,
    limit: Any,
    offset: Any,
    catalogue_filters: Any = None,
    allow_growth_filters: bool = False,
) -> dict[str, Any]:
    query = _text(q, field="q", maximum=MAX_QUERY_LENGTH)
    category = _text(category_id, field="category_id", maximum=3)
    if category and not category.isdigit():
        raise ChannelExplorerQueryError("category_id must contain digits only")
    start = _date(published_from, field="published_from")
    before = _date(published_before, field="published_before")
    if start and before and start >= before:
        raise ChannelExplorerQueryError("published_before must be after published_from")

    minimum_views = _metric(min_views, field="min_views")
    maximum_views = _metric(max_views, field="max_views")
    if minimum_views is not None and maximum_views is not None and minimum_views > maximum_views:
        raise ChannelExplorerQueryError("max_views must be greater than or equal to min_views")

    minimum_duration = _metric(min_duration_seconds, field="min_duration_seconds")
    maximum_duration = _metric(max_duration_seconds, field="max_duration_seconds")
    if minimum_duration is not None and maximum_duration is not None and minimum_duration > maximum_duration:
        raise ChannelExplorerQueryError(
            "max_duration_seconds must be greater than or equal to min_duration_seconds"
        )

    order_field = str(sort_by or "published_at").strip()
    if order_field not in SORT_FIELDS:
        raise ChannelExplorerQueryError("unsupported sort_by")
    order_direction = str(sort_order or "desc").strip().lower()
    if order_direction not in {"asc", "desc"}:
        raise ChannelExplorerQueryError("sort_order must be asc or desc")

    page_size = _metric(limit, field="limit")
    page_offset = _metric(offset, field="offset")
    page_size = DEFAULT_PAGE_SIZE if page_size is None else page_size
    page_offset = 0 if page_offset is None else page_offset
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ChannelExplorerQueryError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    if page_offset > SCAN_LIMIT:
        raise ChannelExplorerQueryError(f"offset must not exceed {SCAN_LIMIT}")

    normalized = {
        "q": query,
        "category_id": category,
        "published_from": start,
        "published_before": before,
        "min_views": minimum_views,
        "max_views": maximum_views,
        "min_duration_seconds": minimum_duration,
        "max_duration_seconds": maximum_duration,
        "sort_by": order_field,
        "sort_order": order_direction,
        "limit": page_size,
        "offset": page_offset,
    }
    if catalogue_filters is not None:
        try:
            normalized["catalogue_filters"] = normalize_catalogue_filters(catalogue_filters, normalized)
            if normalized["catalogue_filters"].get("growth") and not allow_growth_filters:
                raise ValueError("Growth conditions require the Video Growth catalogue")
        except ValueError as error:
            raise ChannelExplorerQueryError(str(error)) from error
        # Calendar dates start at midnight in the zone publication days are counted in.
        zone = catalogue_time_zone(normalized["catalogue_filters"])
        for field in ("published_from", "published_before"):
            if normalized[field] is not None:
                normalized[field] = normalized[field].replace(tzinfo=zone)
    return normalized


def _matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    if query["q"] and query["q"].casefold() not in str(row.get("title") or "").casefold():
        return False
    if query["category_id"] and str(row.get("category_id") or "") != query["category_id"]:
        return False

    published = (
        _timestamp(row.get("published_at"))
        if query["published_from"] or query["published_before"] else None
    )
    if query["published_from"] and (published is None or published < query["published_from"]):
        return False
    if query["published_before"] and (published is None or published >= query["published_before"]):
        return False

    views = (
        _number(row.get("views"))
        if query["min_views"] is not None or query["max_views"] is not None else None
    )
    if query["min_views"] is not None and (views is None or views < query["min_views"]):
        return False
    if query["max_views"] is not None and (views is None or views > query["max_views"]):
        return False

    duration = (
        youtube_duration_seconds(row.get("duration"))
        if query["min_duration_seconds"] is not None or query["max_duration_seconds"] is not None else None
    )
    if query["min_duration_seconds"] is not None and (
        duration is None or duration < query["min_duration_seconds"]
    ):
        return False
    if query["max_duration_seconds"] is not None and (
        duration is None or duration > query["max_duration_seconds"]
    ):
        return False
    return matches_catalogue_filters(row, query.get("catalogue_filters"))


def _sort_rows(rows: list[dict[str, Any]], *, field: str, order: str) -> list[dict[str, Any]]:
    present = []
    missing = []
    for row in rows:
        value = row.get(field)
        if field == "published_at":
            normalized = _timestamp(value)
        elif field == "title":
            normalized = str(value or "").casefold()
        elif field == "duration":
            normalized = youtube_duration_seconds(value)
        else:
            normalized = _number(value)
        target = missing if normalized is None or normalized == "" else present
        target.append((normalized, str(row.get("video_id") or ""), row))
    present.sort(key=lambda item: item[1])
    present.sort(key=lambda item: item[0], reverse=order == "desc")
    missing.sort(key=lambda item: item[1])
    return [item[2] for item in present + missing]


VIDEO_TYPES = frozenset({"long", "short", "stream"})


def catalogue_video_type(value: Any) -> str:
    selected = str(value).strip().lower()
    if selected not in VIDEO_TYPES:
        raise ChannelExplorerQueryError("unsupported video_type")
    return selected


def _current_proof_deadline(row: dict[str, Any], *, now: datetime,
                            source_valid_until: datetime | None) -> bool:
    try:
        deadline = utc_instant(row.get("source_expires_at"))
        if deadline <= now or (source_valid_until is not None and deadline < utc_instant(source_valid_until)):
            return False
    except (TypeError, ValueError, OverflowError):
        return False
    register_read_deadline(deadline)
    return True


def catalogue_inventory_state(repository: Any, channel_id: str, video_type: str, *,
                              typed_catalogue_read: bool, now: datetime,
                              source_valid_until: datetime | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """An accepted typed API read does not change the stored UI rollout flag."""
    state = None if video_type == "long" else repository.load_inventory_state(channel_id, video_type=video_type)
    status = "canonical_long_inventory" if video_type == "long" else str((state or {}).get("inventory_status") or "not_started")
    exposed = video_type == "long" or (state or {}).get("product_exposure_ready") is True
    ready = video_type == "long" or (status == "completed" and exposed)
    if video_type != "long" and typed_catalogue_read:
        ready = bool(state and status == "completed"
            and state.get("channel_id") == channel_id and state.get("video_type") == video_type
            and source_is_current(state.get("source_checked_at"), now=now, valid_until=source_valid_until)
            and source_is_current(state.get("inventory_completed_at"), now=now, valid_until=source_valid_until)
            and _current_proof_deadline(state, now=now, source_valid_until=source_valid_until))
    return state, {"inventory_status": status, "inventory_ready": ready,
        "catalogue_read_ready": ready, "product_exposure_ready": exposed,
        "inventory_source_checked_at": (state or {}).get("source_checked_at")}


def load_catalogue_inventory(repository: Any, channel_id: str, video_type: str, *, full_scope: bool) -> list[dict[str, Any]]:
    if full_scope:
        return (repository.load_full_long_inventory(channel_id) if video_type == "long"
                else repository.load_full_inventory(channel_id, video_type=video_type))
    if video_type == "long" and hasattr(repository, "load_long_inventory"):
        return repository.load_long_inventory(channel_id, limit=SCAN_LIMIT + 1)
    return repository.load_inventory(channel_id, video_type=video_type, limit=SCAN_LIMIT + 1)


def catalogue_type_resolutions(repository: Any, channel_id: str, video_type: str, video_ids: list[str], *,
                               typed_catalogue_read: bool, now: datetime,
                               source_valid_until: datetime | None = None) -> tuple[list[str], dict[str, Any], int]:
    if video_type == "long" or not video_ids:
        return video_ids, {}, 0
    proofs = repository.load_type_resolutions(video_ids, video_type=video_type)
    admitted = {}
    for proof in proofs:
        video_id = str(proof.get("video_id") or "")
        if not video_id or proof.get("resolution_status") != "resolved" or proof.get("canonical_video_type") != video_type:
            continue
        if typed_catalogue_read and (proof.get("canonical_channel_id") != channel_id
                or not _current_proof_deadline(proof, now=now, source_valid_until=source_valid_until)):
            continue
        admitted[video_id] = source_expiry(proof)
    return [video_id for video_id in video_ids if video_id in admitted], admitted, len(set(video_ids) - set(admitted))


def catalogue_lifecycle_ready(row: dict[str, Any], video_type: str, *, now: datetime) -> bool:
    if row.get("publication_status") is not None:
        return False
    if video_type != "stream":
        return True
    end = _timestamp(row.get("stream_actual_end_at"))
    counters = _timestamp(row.get("checked_at"))
    metadata = _timestamp(row.get("metadata_checked_at"))
    return bool(end is not None and counters is not None and metadata is not None
                and end <= min(now, counters, metadata))


def build_channel_explorer(
    repository: Any,
    channel_id: str,
    *,
    q: Any = "",
    category_id: Any = "",
    published_from: Any = "",
    published_before: Any = "",
    min_views: Any = None,
    max_views: Any = None,
    min_duration_seconds: Any = None,
    max_duration_seconds: Any = None,
    sort_by: Any = "published_at",
    sort_order: Any = "desc",
    limit: Any = DEFAULT_PAGE_SIZE,
    offset: Any = 0,
    video_type: str = "long",
    typed_catalogue_read: bool = False,
    catalogue_filters: Any = None,
    full_scope: bool = False,
    all_rows: bool = False,
    read_budget: CatalogueReadBudget | None = None,
    now: datetime | None = None,
    source_valid_until: datetime | None = None,
) -> dict[str, Any]:
    """Build one honest page from one bounded typed inventory scan."""

    selected_video_type = catalogue_video_type(video_type)
    if all_rows and not full_scope:
        raise ChannelExplorerQueryError("all_rows requires full_scope")
    if full_scope:
        repository = CatalogueScopeRepository(repository, read_budget)

    query = _normalized_query(
        q=q,
        category_id=category_id,
        published_from=published_from,
        published_before=published_before,
        min_views=min_views,
        max_views=max_views,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
        catalogue_filters=catalogue_filters,
    )
    if all_rows and query["offset"] != 0:
        raise ChannelExplorerQueryError("all_rows requires offset zero")
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")

    inventory_state, type_coverage = catalogue_inventory_state(repository, channel_id, selected_video_type,
        typed_catalogue_read=typed_catalogue_read, now=generated_at, source_valid_until=source_valid_until)
    inventory_ready = type_coverage["inventory_ready"]
    inventory = load_catalogue_inventory(repository, channel_id, selected_video_type, full_scope=full_scope) if inventory_ready else []
    truncated = not full_scope and len(inventory) > SCAN_LIMIT
    if not full_scope:
        inventory = inventory[:SCAN_LIMIT]
    inventory_before_admission = len(inventory)
    inventory = [row for row in inventory if source_is_current(
        row.get("source_checked_at"), now=generated_at, valid_until=source_valid_until,
    )]
    inventory_source_withheld = inventory_before_admission - len(inventory)
    video_ids = [str(row.get("video_id")) for row in inventory if row.get("video_id")]

    video_ids, resolution_expiries, type_resolution_withheld = catalogue_type_resolutions(
        repository, channel_id, selected_video_type, video_ids, typed_catalogue_read=typed_catalogue_read,
        now=generated_at, source_valid_until=source_valid_until)

    rows = []
    selected_video_columns = (
        STREAM_VIDEO_COLUMNS
        if selected_video_type == "stream"
        else VIDEO_COLUMNS
    )
    if catalogue_filters is not None:
        selected_video_columns += ",tags"
    if selected_video_type != "long":
        selected_video_columns += ",channel_id,publication_status"
    for start in range(0, len(video_ids), DB_BATCH_SIZE):
        rows.extend(
            repository.load_videos_by_ids(
                video_ids[start:start + DB_BATCH_SIZE],
                columns=selected_video_columns,
            )
        )

    inventory_ids = set(video_ids)
    rows = [dict(row) for row in rows if str(row.get("video_id") or "") in inventory_ids]
    loaded_video_ids = {
        str(row.get("video_id"))
        for row in rows
        if row.get("video_id")
    }
    missing_video_row_count = len(inventory_ids - loaded_video_ids)
    loaded_video_count = len(loaded_video_ids)
    identity_withheld = 0
    if selected_video_type != "long" and typed_catalogue_read:
        same_channel = [row for row in rows if row.get("channel_id") == channel_id]
        identity_withheld = len(rows) - len(same_channel)
        rows = same_channel
    stream_completion_withheld = 0
    if selected_video_type != "long":
        completed_rows = []
        for row in rows:
            if not catalogue_lifecycle_ready(row, selected_video_type, now=generated_at):
                stream_completion_withheld += 1
                continue
            completed_rows.append(row)
        rows = completed_rows
    stale_counter_rows = sum(
        1
        for row in rows
        if not source_is_current(row.get("checked_at"), now=generated_at, valid_until=source_valid_until)
    )
    stale_metadata_rows = sum(
        1
        for row in rows
        if not source_is_current(row.get("metadata_checked_at"), now=generated_at, valid_until=source_valid_until)
    )
    stale_video_rows = sum(
        1
        for row in rows
        if not (
            source_is_current(row.get("checked_at"), now=generated_at, valid_until=source_valid_until)
            and source_is_current(row.get("metadata_checked_at"), now=generated_at, valid_until=source_valid_until)
        )
    )
    rows = [
        row
        for row in rows
        if (
            source_is_current(row.get("checked_at"), now=generated_at, valid_until=source_valid_until)
            and source_is_current(row.get("metadata_checked_at"), now=generated_at, valid_until=source_valid_until)
        )
    ]
    current_rows_complete = bool(
        inventory_ready
        and not truncated
        and inventory_source_withheld == 0
        and type_resolution_withheld == 0
        and missing_video_row_count == 0
        and identity_withheld == 0
        and stream_completion_withheld == 0
        and stale_video_rows == 0
    )
    # A stale/null availability status must not regain public admission through
    # legacy compatibility helpers that accept an unknown status.
    public_rows = [row for row in rows
        if row.get("availability_status") == "public"
        and source_is_current(row.get("availability_checked_at"), now=generated_at,
                              valid_until=source_valid_until)]
    catalogue_exclusions = {"availability_excluded_count": len(rows) - len(public_rows)}
    current_rows_complete = current_rows_complete and len(rows) == len(public_rows)
    rows = public_rows
    if catalogue_filters is not None or selected_video_type != "long":
        eligible_rows = [row for row in rows if is_catalogue_eligible(row, now=generated_at)]
        catalogue_exclusions["future_excluded_count"] = len(rows) - len(eligible_rows)
        rows = eligible_rows
    rows = [row for row in rows if _matches(row, query)]
    rows = _sort_rows(rows, field=query["sort_by"], order=query["sort_order"])

    inventory_expiries = {str(item.get("video_id")): source_expiry(item, "source_checked_at") for item in inventory}
    for row in rows:
        row["source_expires_at"] = minimum_expiry(
            source_expiry(row, "checked_at", "metadata_checked_at", "availability_checked_at"),
            inventory_expiries.get(str(row.get("video_id"))),
            resolution_expiries.get(str(row.get("video_id"))), source_expiry(inventory_state))
    present_category_names = {}
    category_expiries = {}
    for row in rows:
        category_id = str(row.get("category_id") or "").strip()
        if category_id and category_id not in present_category_names:
            present_category_names[category_id], category_expiries[category_id] = captured_source_call(youtube_video_category_name, category_id)

    category_option_expiries = {key: minimum_expiry(category_expiries[key],
        *(source_expiry(row) for row in rows if str(row.get("category_id") or "").strip() == key))
        for key in present_category_names}
    matched_count = len(rows)
    page = rows if all_rows else rows[query["offset"]:query["offset"] + query["limit"]]
    public_rows = []
    for row in page:
        public_row = {field: row.get(field) for field in PUBLIC_VIDEO_FIELDS}
        public_row["thumbnail_url"] = video_thumbnail_url(row.get("video_id"))
        if catalogue_filters is not None:
            public_row["tags"] = stored_tags(row)
        category = str(public_row.get("category_id") or "").strip()
        public_row["category_name"] = (
            present_category_names.get(category)
            if category
            else None
        )
        public_row["source_expires_at"] = minimum_expiry(
            source_expiry(row, "checked_at", "metadata_checked_at", "availability_checked_at"),
            inventory_expiries.get(str(row.get("video_id"))), category_expiries.get(category))
        public_rows.append(public_row)

    channel_data_fresh = channel.get("channel_metadata_status") == "ok" and source_is_current(
        channel.get("channel_metadata_checked_at"), now=generated_at, valid_until=source_valid_until,
    )
    admitted_channel = channel_source_fields(channel, now=generated_at, valid_until=source_valid_until)
    admitted_channel["source_expires_at"] = source_expiry(admitted_channel,
        *( ["channel_metadata_checked_at"] if channel_data_fresh else []),
        *( ["country_collected_at"] if admitted_channel.get("country_code") is not None else []))
    public_channel = {field: admitted_channel.get(field) for field in PUBLIC_CHANNEL_FIELDS}

    result = {
        "contract_version": 1,
        "video_type": selected_video_type,
        "channel": public_channel,
        "filters": {
            key: value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value
            for key, value in query.items()
        },
        "coverage": {
            "scope": f"typed_{selected_video_type}_inventory",
            "video_type": selected_video_type,
            "scan_limit": None if full_scope else SCAN_LIMIT,
            **({"scan_mode": "full_catalogue"} if full_scope else {}),
            "scanned_count": len(inventory),
            **type_coverage,
            "inventory_complete": inventory_ready and not truncated,
            "inventory_source_checked_at": (
                (inventory_state or {}).get("source_checked_at")
            ),
            "type_resolution_withheld": type_resolution_withheld,
            "video_rows_withheld_identity": identity_withheld,
            "inventory_sources_withheld": inventory_source_withheld,
            "source_valid_until": source_valid_until.isoformat() if source_valid_until is not None else None,
            "loaded_video_count": loaded_video_count,
            "missing_video_row_count": missing_video_row_count,
            "inventory_pagination": "published_at_video_id_keyset",
            "transactional_snapshot": False,
            "complete": current_rows_complete,
            "truncated": truncated,
            "category_cache_available": False,  # Retained wire fields; labels use the static registry.
            "category_cache_fresh": False,
            "category_cache_refreshed_at": None,
            "current_data_max_age_days": RAW_DATA_MAX_AGE.days,
            "channel_data_fresh": channel_data_fresh,
            "video_rows_withheld_stale": stale_video_rows,
            "video_rows_withheld_without_stream_completion_evidence": (
                stream_completion_withheld if selected_video_type == "stream" else 0
            ),
            "video_rows_withheld_lifecycle": stream_completion_withheld,
            "video_rows_with_stale_counters": stale_counter_rows,
            "video_rows_with_stale_metadata": stale_metadata_rows,
            **catalogue_exclusions,
        },
        "pagination": {
            "limit": query["limit"],
            "offset": query["offset"],
            "returned_count": len(public_rows),
            "has_more_in_scan": query["offset"] + len(public_rows) < matched_count,
            **({"matched_count": matched_count} if catalogue_filters is not None else {}),
        },
        "options": {
            "categories": sorted(
                [
                    {"category_id": category_id, "category_name": category_name, "source_expires_at": category_option_expiries.get(category_id)}
                    for category_id, category_name in present_category_names.items()
                ],
                key=lambda row: (row["category_name"], row["category_id"]),
            ),
            "sort_fields": sorted(SORT_FIELDS),
        },
        "videos": public_rows,
        "source": {
            "kind": "stored_current_youtube_fields",
            "youtube_requests": False,
            "database_writes": False,
            "snapshots_used": False,
            "derived_performance_metrics": False,
            "freshness_enforced": True,
        },
    }
    return repository.finish(result) if full_scope else result
