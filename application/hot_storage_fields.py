"""Current-source projections for browser reads; operational fields are preserved."""
from __future__ import annotations

from hot_storage import source_is_current

CHANNEL_SOURCE_FIELDS = (
    "title", "avatar_url", "subscribers", "views", "video_count",
    "channel_created_at", "uploads_playlist_id",
)
VIDEO_METADATA_FIELDS = (
    "title", "published_at", "duration", "tags", "category_id", "youtube_topic_categories",
    "default_audio_language", "default_language", "caption_available", "stream_actual_end_at",
    "publication_status", "scheduled_start_at",
)
VIDEO_COUNTER_FIELDS = ("views", "like_count", "comment_count")
VIDEO_AVAILABILITY_FIELDS = (
    "availability_status", "privacy_status", "upload_status",
)


def channel_source_fields(row, *, now=None, valid_until=None):
    result = dict(row)
    current = row.get("channel_metadata_status") == "ok" and source_is_current(
        row.get("channel_metadata_checked_at"), now=now, valid_until=valid_until,
    )
    if not current:
        for field in CHANNEL_SOURCE_FIELDS:
            if field in result:
                result[field] = None
    if "country_code" in result and not source_is_current(
        row.get("country_collected_at"), now=now, valid_until=valid_until,
    ):
        result["country_code"] = None
    estimate = row.get("initial_inventory_estimate")
    if isinstance(estimate, dict):
        result["initial_inventory_estimate"] = admitted_inventory_estimate(estimate, now=now, valid_until=valid_until)
    elif "initial_inventory_estimate" in row:
        result["initial_inventory_estimate"] = None
    if "long_video_count" in result:
        admitted = result.get("initial_inventory_estimate") or {}
        result["long_video_count"] = (admitted.get("counts") or {}).get("long")
    return result


def admitted_inventory_estimate(estimate, *, now=None, valid_until=None):
    counts = estimate.get("counts") if isinstance(estimate.get("counts"), dict) else {}
    statuses = estimate.get("statuses") if isinstance(estimate.get("statuses"), dict) else {}
    clocks = estimate.get("checked_at_by_type")
    kinds = [kind for kind in ("long", "short", "stream") if kind in (estimate.get("video_types") or [])]
    result = {"scope": estimate.get("scope"), "video_types": kinds, "counts": {}, "statuses": {}, "checked_at_by_type": {}}
    for kind in kinds:
        clock = clocks.get(kind) if isinstance(clocks, dict) else None if "checked_at_by_type" in estimate else estimate.get("checked_at")
        count, status = counts.get(kind), statuses.get(kind)
        valid = (type(count) is int and 0 <= count <= 2147483647
                 and (status == "ok" or (status == "playlist_not_found" and count == 0))
                 and source_is_current(clock, now=now, valid_until=valid_until))
        result["counts"][kind] = count if valid else None
        result["statuses"][kind] = status if valid else "error"
        result["checked_at_by_type"][kind] = clock if valid else None
    return result


def video_source_fields(row, *, now=None, valid_until=None):
    result = dict(row)
    for clock, fields in (("metadata_checked_at", VIDEO_METADATA_FIELDS),
                          ("checked_at", VIDEO_COUNTER_FIELDS),
                          ("availability_checked_at", VIDEO_AVAILABILITY_FIELDS)):
        if not any(field in row for field in fields):
            continue
        if not source_is_current(row.get(clock), now=now, valid_until=valid_until):
            for field in fields:
                if field in result:
                    result[field] = None
    return result


def coverage_source_fields(rows, channels):
    """Keep current operational coverage, admit provider labels/counts separately.

    `channels` is already projected by channel_source_fields at repository read.
    Old RPC payloads cannot supply replacement source values or their own clock.
    """
    admitted = {row["channel_id"]: row for row in channels if row.get("channel_id")}
    result = []
    for row in rows:
        channel = admitted.get(row.get("channel_id"))
        if not channel or channel.get("is_hidden"):
            continue
        value = dict(row)
        for field in ("title", "video_count", "long_video_count"):
            value[field] = channel.get(field)
        count = value["long_video_count"]
        if type(count) is int and count >= 0:
            found = value.get("inventory_found")
            expected = max(count, found) if type(found) is int and found >= 0 else count
            loaded = value.get("videos_in_db")
            value["expected_long_count"] = expected
            value["missing_estimate"] = max(expected-loaded, 0) if type(loaded) is int else None
            value["coverage_percent"] = round(loaded/expected*100, 2) if type(loaded) is int and expected > 0 else None
        else:
            value.update(expected_long_count=None, missing_estimate=None, coverage_percent=None)
        result.append(value)
    return result
