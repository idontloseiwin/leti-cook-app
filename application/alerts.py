"""Owner alerts over confirmed local observations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import Annotated

from hot_storage import HOT_DATA_WINDOW, utc_instant
from research_query.contracts import WireModel, ChannelScope, FolderScope, LabelScope, VideoId
from research_query.registry import FIELDS, POSTGRES_BIGINT_MAX

COUNTERS = ("video.views", "video.likes", "video.comments", "channel.subscribers")
VIDEO_EVENTS = ("video.published", "video.unavailable", "video.restored")
EVENTS = COUNTERS + VIDEO_EVENTS


class VideoScope(WireModel):
    kind: Literal["video"]
    id: VideoId


class AlertRule(WireModel):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    scope: Annotated[ChannelScope | FolderScope | LabelScope | VideoScope, Field(discriminator="kind")]
    event: Literal["video.views", "video.likes", "video.comments", "channel.subscribers",
                   "video.published", "video.unavailable", "video.restored"]
    video_type: Literal["long", "short"] | None = None
    direction: Literal["up", "down"] | None = None
    threshold: Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")] | None = None
    min_duration: Annotated[int, Field(ge=0, le=2**53-1)] | None = None
    max_duration: Annotated[int, Field(ge=0, le=2**53-1)] | None = None

    @model_validator(mode="after")
    def check_semantics(self):
        if not self.name.strip() or any(ord(ch) < 32 for ch in self.name):
            raise ValueError("invalid_rule_name")
        if self.event in COUNTERS:
            if self.direction is None or self.threshold is None or int(self.threshold) > POSTGRES_BIGINT_MAX:
                raise ValueError("invalid_counter_threshold")
        elif self.threshold is not None or self.direction is not None:
            raise ValueError("event_has_no_threshold")
        if self.event == "channel.subscribers":
            if self.scope.kind == "video" or any(x is not None for x in (
                self.video_type, self.min_duration, self.max_duration,
            )):
                raise ValueError("subscribers_require_channel_scope")
        elif self.video_type is None:
            raise ValueError("select_one_video_format")
        if self.min_duration is not None and self.max_duration is not None and self.min_duration > self.max_duration:
            raise ValueError("invalid_duration_range")
        if self.event in ("video.unavailable", "video.restored") and (
            self.min_duration is not None or self.max_duration is not None
        ):
            raise ValueError("duration_may_be_unavailable_for_this_event")
        return self


def capabilities():
    return {
        "events": [{"id": key, "unit": FIELDS[key].unit if key in COUNTERS else None,
                    "threshold": key in COUNTERS} for key in EVENTS],
        "formats": ["long", "short"], "scopes": ["video", "channel", "folder", "label"],
        "additional_provider_requests": 0,
    }


@dataclass(frozen=True)
class Measurement:
    value: int
    at: datetime
    definition: str

    def __post_init__(self):
        if type(self.value) is not int or not 0 <= self.value <= POSTGRES_BIGINT_MAX:
            raise ValueError("invalid_counter_measurement")
        utc_instant(self.at)


def crossed(rule: AlertRule, before: Measurement | None, after: Measurement | None,
            *, started_at: datetime, as_of: datetime) -> bool:
    """Missing observations cannot invent a zero or a crossing."""
    if rule.event not in COUNTERS or before is None or after is None:
        return False
    if not before.definition or before.definition != after.definition:
        return False
    if not (as_of - HOT_DATA_WINDOW < before.at < after.at <= as_of and started_at < after.at):
        return False
    threshold = int(rule.threshold)
    if rule.direction == "up":
        return before.value < threshold <= after.value
    return before.value > threshold >= after.value
