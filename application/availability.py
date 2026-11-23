"""Availability facts from YouTube Data API responses; no watch-page probing."""

AVAILABILITY_PUBLIC = "public"
AVAILABILITY_UNLISTED = "unlisted"
AVAILABILITY_PRIVATE = "private"
AVAILABILITY_UNKNOWN_UNAVAILABLE = "unknown_unavailable"


def availability_status_from_privacy_status(privacy_status):
    if privacy_status == AVAILABILITY_UNLISTED:
        return AVAILABILITY_UNLISTED
    if privacy_status == AVAILABILITY_PRIVATE:
        return AVAILABILITY_PRIVATE
    return AVAILABILITY_PUBLIC


def is_public_availability_status(availability_status):
    return availability_status in (None, "", AVAILABILITY_PUBLIC)


def video_api_availability_values(video, now):
    status = video.get("status") or {}
    privacy_status = status.get("privacyStatus")
    return {
        "availability_status": availability_status_from_privacy_status(privacy_status),
        "privacy_status": privacy_status,
        "upload_status": status.get("uploadStatus"),
        "availability_checked_at": now,
    }
