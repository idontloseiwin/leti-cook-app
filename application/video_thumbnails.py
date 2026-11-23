"""Pure presentation of the existing public-video thumbnail convention."""

from __future__ import annotations

import re


def video_thumbnail_url(video_id: object) -> str | None:
    """Derive a URL for an already admitted video; this does not admit its data."""
    if not isinstance(video_id, str) or re.fullmatch(r"[A-Za-z0-9_-]+", video_id) is None:
        return None
    return f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
