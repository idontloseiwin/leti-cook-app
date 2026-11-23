"""Side-effect-free normalization of stored YouTube channel counters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from youtube_source_fields import youtube_counter_integer


def _subscriber_count_hidden(statistics: Mapping[str, Any]) -> bool:
    value = statistics.get("hiddenSubscriberCount")
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() == "true"


def youtube_channel_counter_values(
    statistics: Mapping[str, Any] | None,
) -> dict[str, int | None]:
    """Return honest nullable counters from one ``channels.list`` item.

    All three keys are present so a successful metadata observation cannot
    silently reuse an older value when YouTube omitted the current source field.
    Hidden subscriber counts are explicitly unknown. A real reported zero stays
    zero; malformed, missing, or negative values stay ``None``.
    """

    source = statistics if isinstance(statistics, Mapping) else {}
    subscribers = (
        None
        if _subscriber_count_hidden(source)
        else youtube_counter_integer(source.get("subscriberCount"))
    )
    return {
        "subscribers": subscribers,
        "views": youtube_counter_integer(source.get("viewCount")),
        "video_count": youtube_counter_integer(source.get("videoCount")),
    }
