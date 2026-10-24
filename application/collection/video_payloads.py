"""Video completion payloads with explicit parser, error identities and batch gate."""

import re
from datetime import datetime, timezone

from availability import video_api_availability_values
from youtube_source_fields import clean_optional_text, youtube_counter_integer, youtube_duration_seconds


MAX_YOUTUBE_TOPIC_CATEGORIES = 50
MAX_YOUTUBE_TOPIC_CATEGORY_URL_LENGTH = 512
YOUTUBE_TOPIC_CATEGORY_URL_RE = re.compile(
    r"https://[A-Za-z0-9-]+(?:[.][A-Za-z0-9-]+)*"
    r"[.]wikipedia[.]org/wiki/[^?#\s]+"
)

# Stored text bounds, well above YouTube's own limits; the schema CHECKs match.
VIDEO_TITLE_MAX_CHARS = 1000
VIDEO_TAGS_MAX_COUNT = 500
VIDEO_TAGS_MAX_BYTES = 25600


def _require_payload_dependencies(
    batch_stats_enabled, parse_dt, view_count_absent_error, view_count_invalid_error,
):
    if batch_stats_enabled is None:
        raise TypeError("batch_stats_enabled must be supplied explicitly")
    if not callable(parse_dt):
        raise TypeError("parse_dt must be callable")
    for error_class in (view_count_absent_error, view_count_invalid_error):
        if not isinstance(error_class, type) or not issubclass(error_class, Exception):
            raise TypeError("view-count errors must be exception classes")


def youtube_topic_categories(item):
    """Return normalized provider topic URLs while preserving missing vs empty."""

    if "topicDetails" not in item or item.get("topicDetails") is None:
        return []
    topic_details = item.get("topicDetails")
    if not isinstance(topic_details, dict):
        raise ValueError("invalid YouTube topicDetails object")
    if "topicCategories" not in topic_details:
        return []
    raw_categories = topic_details.get("topicCategories")
    if raw_categories is None:
        return []
    if not isinstance(raw_categories, list):
        raise ValueError("invalid YouTube topicCategories list")
    if len(raw_categories) > MAX_YOUTUBE_TOPIC_CATEGORIES:
        raise ValueError("YouTube topicCategories exceeds the supported bound")

    categories = []
    seen = set()
    for value in raw_categories:
        if not isinstance(value, str):
            raise ValueError("invalid YouTube topic category URL")
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > MAX_YOUTUBE_TOPIC_CATEGORY_URL_LENGTH
            or YOUTUBE_TOPIC_CATEGORY_URL_RE.fullmatch(normalized) is None
        ):
            raise ValueError("invalid YouTube topic category URL")
        if normalized not in seen:
            seen.add(normalized)
            categories.append(normalized)
    return categories


def youtube_optional_text_field(container, field, *, maximum_length=64):
    """Normalize one optional provider string without stringifying bad shapes."""

    if field not in container or container.get(field) is None:
        return None
    value = container.get(field)
    if not isinstance(value, str):
        raise ValueError(f"invalid YouTube {field} value")
    normalized = value.strip()
    if len(normalized) > maximum_length:
        raise ValueError(f"YouTube {field} exceeds the supported bound")
    return normalized or None


def youtube_caption_available(item):
    """Parse contentDetails.caption and keep an omitted value unknown."""

    content_details = item.get("contentDetails")
    if content_details is None:
        return None
    if not isinstance(content_details, dict):
        raise ValueError("invalid YouTube contentDetails object")
    if "caption" not in content_details or content_details.get("caption") is None:
        return None
    value = content_details.get("caption")
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError("invalid YouTube caption value")


def youtube_stream_actual_end_at(item, *, parse_dt):
    """Return affirmative completed-stream evidence from videos.list."""

    if not callable(parse_dt):
        raise TypeError("parse_dt must be callable")
    details = item.get("liveStreamingDetails")
    if details is None:
        return None
    if not isinstance(details, dict):
        raise ValueError("invalid YouTube liveStreamingDetails object")
    if (
        "actualEndTime" not in details
        or details.get("actualEndTime") is None
    ):
        return None
    value = details.get("actualEndTime")
    if not isinstance(value, str):
        raise ValueError("invalid YouTube actualEndTime value")
    normalized = value.strip()
    parsed = parse_dt(normalized)
    if not normalized or len(normalized) > 64 or parsed is None:
        raise ValueError("invalid YouTube actualEndTime value")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("YouTube actualEndTime must include a timezone")
    return normalized


def build_video_stat_payload(
    channel_id,
    video,
    now=None,
    *,
    batch_stats_enabled,
    parse_dt,
    view_count_absent_error,
    view_count_invalid_error,
):
    _require_payload_dependencies(
        batch_stats_enabled, parse_dt, view_count_absent_error, view_count_invalid_error,
    )
    now = now or datetime.now(timezone.utc).isoformat()
    statistics = video.get("statistics") or {}
    views = youtube_counter_integer(statistics.get("viewCount"))
    if statistics.get("viewCount") is None:
        raise view_count_absent_error()
    if views is None:
        raise view_count_invalid_error()
    published_at = video["snippet"]["publishedAt"]
    duration = video.get("contentDetails", {}).get("duration", "")
    like_count = youtube_counter_integer(statistics.get("likeCount"))
    comment_count = youtube_counter_integer(statistics.get("commentCount"))
    snippet = video.get("snippet") or {}
    tags = snippet.get("tags") or []
    category_id = clean_optional_text(snippet.get("categoryId"))
    availability_values = video_api_availability_values(video, now)
    topic_categories = youtube_topic_categories(video)
    default_audio_language = youtube_optional_text_field(snippet, "defaultAudioLanguage")
    default_language = youtube_optional_text_field(snippet, "defaultLanguage")
    caption_available = youtube_caption_available(video)
    stream_actual_end_at = youtube_stream_actual_end_at(video, parse_dt=parse_dt)

    row = {
        "video_id": video["id"],
        "channel_id": channel_id,
        "title": video["snippet"]["title"],
        "views": views,
        "checked_at": now,
        "published_at": published_at,
        "duration": duration,
        "like_count": like_count,
        "comment_count": comment_count,
        "tags": tags,
        "category_id": category_id,
        "youtube_topic_categories": topic_categories,
        "default_audio_language": default_audio_language,
        "default_language": default_language,
        "caption_available": caption_available,
    }
    if stream_actual_end_at is not None:
        row["stream_actual_end_at"] = stream_actual_end_at
        # Positive completion evidence came from this full metadata response,
        # so its source clock must travel with the completion payload.
        row["metadata_checked_at"] = now
    row.update(availability_values)
    if batch_stats_enabled:
        row["metadata_checked_at"] = now

    return {
        "video_id": video["id"],
        "row": row,
        "snapshot": {
            "video_id": video["id"],
            "views": views,
            "like_count": like_count,
            "comment_count": comment_count,
            "recorded_at": now,
        },
        "availability_values": availability_values,
    }


class VideoMetadataValidationError(ValueError):
    """A fixed field code, never provider values or parser exception text."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _optional_metadata_value(reason, parser, *args, **kwargs):
    try:
        value = parser(*args, **kwargs)
        for text in value if isinstance(value, list) else (value,):
            if isinstance(text, str):
                if "\x00" in text:
                    raise ValueError("invalid stored text")
                text.encode("utf-8")
        return value
    except (TypeError, ValueError, OverflowError):
        raise VideoMetadataValidationError(reason) from None


def build_video_optional_metadata(video):
    """Validate all optional profile fields before choosing a write owner."""
    snippet = video.get("snippet")
    if not isinstance(snippet, dict):
        raise VideoMetadataValidationError("snippet_missing_or_invalid")
    tags = snippet.get("tags")
    if tags is not None and (not isinstance(tags, list)
            or len(tags) > VIDEO_TAGS_MAX_COUNT or any(not isinstance(tag, str) for tag in tags)):
        raise VideoMetadataValidationError("tags_invalid")
    tags = _optional_metadata_value("tags_invalid", lambda: list(tags or []))
    if len("".join(tags).encode("utf-8")) > VIDEO_TAGS_MAX_BYTES:
        raise VideoMetadataValidationError("tags_invalid")
    details = video.get("topicDetails")
    if details is not None and not isinstance(details, dict):
        raise VideoMetadataValidationError("topic_details_invalid")
    return {
        "tags": tags,
        "category_id": _optional_metadata_value("category_id_invalid", youtube_optional_text_field,
                                                snippet, "categoryId"),
        "youtube_topic_categories": _optional_metadata_value(
            "topic_categories_invalid", youtube_topic_categories, video),
        "default_audio_language": _optional_metadata_value(
            "default_audio_language_invalid", youtube_optional_text_field, snippet, "defaultAudioLanguage"),
        "default_language": _optional_metadata_value(
            "default_language_invalid", youtube_optional_text_field, snippet, "defaultLanguage"),
        "caption_available": _optional_metadata_value("caption_invalid", youtube_caption_available, video),
    }


def build_video_metadata_payload(channel_id, video, recorded_at, *, parse_dt,
                                 allow_missing_duration=False):
    """Shared metadata; only a validated waiting caller may omit duration."""
    snippet = video.get("snippet")
    if not isinstance(snippet, dict) or snippet.get("channelId") != channel_id:
        raise ValueError("invalid YouTube recovery identity")
    title, published_at = snippet.get("title"), snippet.get("publishedAt")
    if (not isinstance(title, str) or len(title) > VIDEO_TITLE_MAX_CHARS
            or not isinstance(published_at, str) or parse_dt(published_at) is None):
        raise ValueError("invalid YouTube recovery metadata")
    content_details = video.get("contentDetails")
    if not isinstance(content_details, dict):
        raise VideoMetadataValidationError("content_details_missing_or_invalid")
    duration = content_details.get("duration")
    if duration is None:
        if allow_missing_duration is not True:
            raise VideoMetadataValidationError("duration_missing")
    elif (not isinstance(duration, str) or duration != duration.strip() or len(duration) > 64
          or not duration.startswith("P") or youtube_duration_seconds(duration) is None):
        raise VideoMetadataValidationError("duration_invalid")
    return {
        "title": title, "published_at": published_at, "duration": duration,
        **build_video_optional_metadata(video),
        "stream_actual_end_at": youtube_stream_actual_end_at(video, parse_dt=parse_dt),
        **video_api_availability_values(video, recorded_at),
    }


def build_video_identity_payload(channel_id, video, recorded_at, *, parse_dt):
    """Strict metadata recovery; absent counters never invent zero or a type."""
    return build_video_metadata_payload(channel_id, video, recorded_at, parse_dt=parse_dt)


def build_video_collection_metadata_payload(
    channel_id, video, now=None, *, batch_stats_enabled, parse_dt,
    view_count_absent_error, view_count_invalid_error,
):
    """Build one queue completion payload without admitting poison rows."""

    _require_payload_dependencies(
        batch_stats_enabled, parse_dt, view_count_absent_error, view_count_invalid_error,
    )
    snippet = video.get("snippet")
    if not isinstance(snippet, dict):
        raise ValueError("invalid YouTube snippet object")
    title = snippet.get("title")
    published_at = snippet.get("publishedAt")
    tags = snippet.get("tags")
    if not isinstance(title, str) or len(title) > VIDEO_TITLE_MAX_CHARS:
        raise ValueError("invalid YouTube video title")
    if not isinstance(published_at, str) or parse_dt(published_at) is None:
        raise ValueError("invalid YouTube publishedAt value")
    if tags is not None and (
        not isinstance(tags, list)
        or any(not isinstance(tag, str) for tag in tags)
        or len(tags) > VIDEO_TAGS_MAX_COUNT
        or len("".join(tags).encode("utf-8")) > VIDEO_TAGS_MAX_BYTES
    ):
        raise ValueError("invalid YouTube video tags")

    statistics = video.get("statistics")
    if statistics is None:
        raise view_count_absent_error()
    if not isinstance(statistics, dict):
        raise view_count_invalid_error()
    if "viewCount" not in statistics or statistics.get("viewCount") is None:
        raise view_count_absent_error()
    if youtube_counter_integer(statistics.get("viewCount")) is None:
        raise view_count_invalid_error()
    return build_video_stat_payload(
        channel_id, video, now=now, batch_stats_enabled=batch_stats_enabled,
        parse_dt=parse_dt, view_count_absent_error=view_count_absent_error,
        view_count_invalid_error=view_count_invalid_error,
    )


def build_video_counter_payload(video, now=None):
    now = now or datetime.now(timezone.utc).isoformat()
    video_id = clean_optional_text(video.get("id"))
    views = video.get("views")
    if not video_id:
        raise ValueError("Batch statistics item is missing video ID")
    if views is None:
        raise ValueError("Batch statistics item is missing views")

    row = {
        "video_id": video_id,
        "views": int(views),
        "checked_at": now,
        "like_count": video.get("like_count"),
        "comment_count": video.get("comment_count"),
    }
    return {
        "video_id": video_id,
        "row": row,
        "snapshot": {
            "video_id": video_id,
            "views": int(views),
            "like_count": video.get("like_count"),
            "comment_count": video.get("comment_count"),
            "recorded_at": now,
        },
    }
