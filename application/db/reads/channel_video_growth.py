"""Read-only persistence boundary for fixed-window video view growth."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from hot_storage import iso_utc

from .analytics_read_queries import AnalyticsReadResultError, read_video_view_growth_28d
from .channel_explorer import ChannelExplorerRepository, VIDEO_TYPES


VIDEO_GROWTH_LIMIT = 5000
VIDEO_GROWTH_BATCH_SIZE = 500


class VideoGrowthResponseError(RuntimeError):
    """A successful transport response did not cover its exact requested IDs."""


class ChannelVideoGrowthRepository(ChannelExplorerRepository):
    """Own the bounded SQL read used by the growth catalogue."""

    def load_video_view_growth(
        self,
        channel_id: str,
        video_ids: Sequence[str],
        *,
        as_of: datetime,
        recorded_from: datetime | None = None,
        video_type: str = "long",
    ) -> list[dict[str, Any]]:
        if not isinstance(video_type, str) or video_type not in VIDEO_TYPES:
            raise ValueError("unsupported video type")
        clean_ids = list(dict.fromkeys(
            str(value).strip()
            for value in video_ids
            if value is not None and str(value).strip()
        ))
        if len(clean_ids) > VIDEO_GROWTH_LIMIT:
            raise ValueError(
                f"video view growth is limited to {VIDEO_GROWTH_LIMIT} video IDs"
            )
        if not clean_ids:
            return []
        as_of_text = iso_utc(as_of)
        rows: list[dict[str, Any]] = []
        for start in range(0, len(clean_ids), VIDEO_GROWTH_BATCH_SIZE):
            batch_ids = clean_ids[start:start + VIDEO_GROWTH_BATCH_SIZE]
            try:
                data = read_video_view_growth_28d(
                    self.database, channel_id=channel_id, video_ids=batch_ids,
                    as_of=as_of_text,
                    **({"recorded_from": iso_utc(recorded_from)} if recorded_from is not None else {}),
                    **({"video_type": video_type} if video_type != "long" else {}),
                )
            except AnalyticsReadResultError as error:
                raise VideoGrowthResponseError("Video growth response is incomplete or invalid") from error
            if not isinstance(data, list):
                raise VideoGrowthResponseError("Video growth response must be a row array")
            expected_ids = set(batch_ids)
            batch_rows: dict[str, dict[str, Any]] = {}
            for row in data:
                if not isinstance(row, dict):
                    raise VideoGrowthResponseError("Video growth response contains an invalid row")
                video_id = row.get("video_id")
                if not isinstance(video_id, str) or video_id not in expected_ids:
                    raise VideoGrowthResponseError("Video growth response contains an unexpected video ID")
                if video_id in batch_rows:
                    raise VideoGrowthResponseError("Video growth response contains duplicate video IDs")
                batch_rows[video_id] = dict(row)
            # SQL emits one row even for an unavailable/out-of-scope video.
            # Missing rows therefore mean a broken/truncated response, not
            # missing history. Never return a partial batch or partial read.
            if set(batch_rows) != expected_ids:
                raise VideoGrowthResponseError("Video growth response is incomplete")
            rows.extend(batch_rows[video_id] for video_id in batch_ids)
        return rows
