"""Load the stored channel profile and evidence for its unlisted status.
Format totals and changes in reported counters use the shared query service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from collection_formats import VIDEO_FORMATS, shown_formats
from hot_storage import HOT_DATA_MAX_AGE, source_is_current, utc_instant
from hot_storage_fields import channel_source_fields
from source_expiry import source_expiry, minimum_expiry


CHANNEL_COLUMNS = (
    "channel_id,title,subscribers,views,video_count,"
    "country_code,country_collected_at,channel_created_at,channel_metadata_checked_at,channel_metadata_status,is_hidden"
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
# The list proof counts a channel's whole uploads lists. YouTube lists about
# 20,000 uploads per list; this bound only stops a runaway read.
LIST_SCAN_LIMIT = 1_000_000
RAW_DATA_MAX_AGE = HOT_DATA_MAX_AGE


class ChannelExplorerNotFoundError(LookupError):
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


def build_channel_list_inventory(
    repository: Any,
    channel_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The list proof of the header's unlisted note: the
    channel's complete uploads lists and YouTube's public video count of a fresh
    profile. Counts only: no video row, counter or view total is read."""

    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")
    inventory = dict(repository.load_list_inventory(channel_id, limit=LIST_SCAN_LIMIT))
    profile_fresh = channel.get("channel_metadata_status") == "ok" and source_is_current(
        channel.get("channel_metadata_checked_at"), now=generated_at)
    formats = inventory.pop("library_formats")
    enabled = shown_formats()
    for kind in VIDEO_FORMATS:
        if kind not in enabled:
            formats[kind] = {"status": "turned_off", "full_completed_at": None,
                             "absent_checked_at": None, "source_expires_at": None}
    library = {
        "status": "ready" if profile_fresh and all(
            formats[kind]["status"] in ("complete", "absent") for kind in enabled
        ) else "partial",
        "formats": formats,
    }
    inventory["youtube_video_count"] = _number(channel.get("video_count")) if profile_fresh else None
    inventory["source_expires_at"] = minimum_expiry(
        inventory.get("source_expires_at"),
        source_expiry(channel, "channel_metadata_checked_at") if profile_fresh else None)
    return {
        "contract_version": 1,
        "channel_id": channel_id,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "inventory": inventory,
        "library": library,
        "source": {
            "kind": "stored_channel_list_inventory",
            "youtube_requests": False,
            "database_writes": False,
        },
    }


def _number(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
