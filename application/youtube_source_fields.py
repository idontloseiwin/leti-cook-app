"""Side-effect-free normalization for stored YouTube source fields."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)
_NONNEGATIVE_INTEGER_RE = re.compile(r"^\d+$")
# PostgreSQL bigint: a larger counter could never be stored, so it is invalid.
_MAX_COUNTER = 9_223_372_036_854_775_807


def youtube_unsigned_long(value: Any) -> int | None:
    """A nonnegative integer or its ASCII decimal encoding in the uint64 range."""
    if isinstance(value, str) and 1 <= len(value) <= 20 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 0 <= value < 2**64 else None


def youtube_counter_integer(value: Any, *, maximum: int = _MAX_COUNTER) -> int | None:
    """Normalize one public counter without inventing or truncating a value.

    ``maximum`` defaults to the stored bigint bound. A caller that never stores
    the value (one-off acquisition) may pass the documented uint64 bound.
    """

    if type(maximum) is not int or maximum < 0:
        raise ValueError("counter_maximum_invalid")
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= maximum else None
    if isinstance(value, float):
        return int(value) if 0 <= value <= maximum and value.is_integer() else None
    normalized = str(value).strip()
    if not _NONNEGATIVE_INTEGER_RE.fullmatch(normalized):
        return None
    number = int(normalized)
    return number if number <= maximum else None


def youtube_duration_seconds(value: Any) -> int | None:
    """Return a stored YouTube ISO-8601 duration as seconds without guessing.

    Numeric values are also accepted. Missing, negative, fractional, or
    malformed values stay unknown instead of becoming zero.
    """

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None

    normalized = str(value).strip().upper()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)

    match = _DURATION_RE.fullmatch(normalized)
    if not match or not any(match.groupdict().values()):
        return None
    if "T" in normalized and not any(
        match.group(name) is not None for name in ("hours", "minutes", "seconds")
    ):
        return None
    return (
        int(match.group("days") or 0) * 86400
        + int(match.group("hours") or 0) * 3600
        + int(match.group("minutes") or 0) * 60
        + int(match.group("seconds") or 0)
    )


def clean_optional_text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_dt(value):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    if "T" in text:
        text = re.sub(r'([+-]\d{2})$', r'\1:00', text)
        text = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', text)

    m = re.match(r'^(.*?\.)(\d+)(.*)$', text)
    if m:
        text = m.group(1) + (m.group(2) + "000000")[:6] + m.group(3)

    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def public_video_without_view_count(item):
    """Recognize an explicit processed, non-live public profile without views."""
    status = item.get('status')
    snippet = item.get('snippet')
    statistics = item.get('statistics', {})
    return (isinstance(status, dict) and status.get('privacyStatus') == 'public'
            and status.get('uploadStatus') == 'processed' and isinstance(snippet, dict)
            and snippet.get('liveBroadcastContent') == 'none'
            and 'liveStreamingDetails' not in item
            and isinstance(statistics, dict) and 'viewCount' not in statistics)


def profile_engagement(item):
    statistics = item.get('statistics') or {}
    likes = youtube_counter_integer(statistics.get('likeCount'))
    comments = youtube_counter_integer(statistics.get('commentCount'))
    return dict(profile_likes=likes, profile_comments=comments,
                engagement_report_mask=(int(likes is not None) | (int(comments is not None) << 1)))
