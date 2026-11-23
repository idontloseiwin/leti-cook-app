"""Pure typed-playlist identity and bounded traversal primitives.

The YouTube transport and database state remain owned by their existing layers.
This module only normalizes the private-runtime UULF/UUSH/UULV convention and
turns one bounded playlist window into an explicit, resumable result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping


VIDEO_TYPE_LONG = "long"
VIDEO_TYPE_SHORT = "short"
VIDEO_TYPE_STREAM = "stream"
VIDEO_TYPES = frozenset({VIDEO_TYPE_LONG, VIDEO_TYPE_SHORT, VIDEO_TYPE_STREAM})
# YouTube video IDs are 11 characters; the database accepts nothing else.
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_PAGE_TOKEN_LENGTH = 2048
MAX_PROVIDER_TIMESTAMP_LENGTH = 64

PLAYLIST_PREFIX_BY_VIDEO_TYPE = {
    VIDEO_TYPE_LONG: "UULF",
    VIDEO_TYPE_SHORT: "UUSH",
    VIDEO_TYPE_STREAM: "UULV",
}

SCAN_COMPLETE = "complete"
SCAN_EMPTY = "empty"
SCAN_CONTINUATION = "continuation"
SCAN_PLAYLIST_NOT_FOUND = "playlist_not_found"
SCAN_ERROR = "error"


class PlaylistTraversalError(RuntimeError):
    """A playlist traversal stopped on an invalid page, item or continuation.

    The message is a fixed code with no provider data, so it is safe to store.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_video_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in VIDEO_TYPES:
        raise ValueError("unsupported video type")
    return normalized


def typed_playlist_id(channel_id: Any, video_type: Any) -> str | None:
    """Return the accepted private-runtime typed playlist identifier."""

    normalized_type = normalize_video_type(video_type)
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id.startswith("UC"):
        return None
    return PLAYLIST_PREFIX_BY_VIDEO_TYPE[normalized_type] + normalized_channel_id[2:]


def _is_valid_provider_timestamp(value: Any) -> bool:
    if value is None:
        return True
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_PROVIDER_TIMESTAMP_LENGTH
    ):
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def playlist_video_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    content_details = item.get("contentDetails")
    if not isinstance(content_details, Mapping):
        return None
    raw_video_id = content_details.get("videoId")
    if not isinstance(raw_video_id, str):
        return None
    video_id = raw_video_id.strip()
    if (
        raw_video_id != video_id
        or VIDEO_ID_RE.fullmatch(video_id) is None
    ):
        return None
    published_at = content_details.get("videoPublishedAt")
    if not _is_valid_provider_timestamp(published_at):
        return None
    return {
        "video_id": video_id,
        "published_at": published_at,
    }


def playlist_page_video_items(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Parse every item of one playlist page.

    An invalid item raises PlaylistTraversalError rather than being skipped:
    skipping would drop a video while the traversal still reports success.
    """

    raw_items = response.get("items", [])
    if not isinstance(raw_items, list):
        raise PlaylistTraversalError("invalid_items")
    items = [playlist_video_item(item) for item in raw_items]
    if any(item is None for item in items):
        raise PlaylistTraversalError("invalid_playlist_item")
    return items


def checked_next_page_token(
    response: Mapping[str, Any],
    page_item_count: int,
    seen_page_tokens: set[str],
) -> str | None:
    """Return a page's continuation token, or None at the end of the playlist.

    A token that is malformed, repeats, or follows a page without items
    raises PlaylistTraversalError, so provider data cannot make a traversal
    loop. The returned token is added to seen_page_tokens.
    """

    raw_next_page_token = response.get("nextPageToken")
    if raw_next_page_token is None or raw_next_page_token == "":
        return None
    if (
        not isinstance(raw_next_page_token, str)
        or raw_next_page_token != raw_next_page_token.strip()
        or len(raw_next_page_token) > MAX_PAGE_TOKEN_LENGTH
    ):
        raise PlaylistTraversalError("invalid_next_page_token")
    if page_item_count <= 0:
        raise PlaylistTraversalError("empty_page_with_continuation")
    if raw_next_page_token in seen_page_tokens:
        raise PlaylistTraversalError("repeated_page_token")
    seen_page_tokens.add(raw_next_page_token)
    return raw_next_page_token


@dataclass(frozen=True)
class TypedPlaylistScanResult:
    video_type: str
    playlist_id: str | None
    status: str
    items: tuple[dict[str, Any], ...]
    pages_processed: int
    next_page_token: str | None
    error_type: str | None = None

    @property
    def scan_complete(self) -> bool:
        return self.status in {SCAN_COMPLETE, SCAN_EMPTY}


def scan_typed_playlist_window(
    channel_id: Any,
    video_type: Any,
    *,
    fetch_page: Callable[..., Mapping[str, Any]],
    max_pages: int = 10,
    page_token: str | None = None,
    max_results: int = 50,
    phase: str = "typed_playlist_inventory",
    playlist_not_found_errors: tuple[type[BaseException], ...] = (),
    reject_page_duplicates: bool = False,
) -> TypedPlaylistScanResult:
    """Read one bounded, resumable playlist window without hiding outcomes."""

    normalized_type = normalize_video_type(video_type)
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    if max_results < 1 or max_results > 50:
        raise ValueError("max_results must be between 1 and 50")

    playlist_id = typed_playlist_id(channel_id, normalized_type)
    if playlist_id is None:
        return TypedPlaylistScanResult(
            video_type=normalized_type,
            playlist_id=None,
            status=SCAN_ERROR,
            items=(),
            pages_processed=0,
            next_page_token=None,
            error_type="invalid_channel_id",
        )

    current_page_token = str(page_token) if page_token else None
    seen_page_tokens = {current_page_token} if current_page_token else set()
    unique_items: dict[str, dict[str, Any]] = {}
    pages_processed = 0

    try:
        for _ in range(max_pages):
            response = fetch_page(
                playlist_id,
                page_token=current_page_token,
                max_results=max_results,
                phase=phase,
            )
            pages_processed += 1
            if not isinstance(response, Mapping):
                return TypedPlaylistScanResult(
                    normalized_type,
                    playlist_id,
                    SCAN_ERROR,
                    tuple(unique_items.values()),
                    pages_processed,
                    current_page_token,
                    "invalid_response",
                )

            raw_items = response.get("items") if "items" in response else None
            if not isinstance(raw_items, list):
                return TypedPlaylistScanResult(
                    normalized_type,
                    playlist_id,
                    SCAN_ERROR,
                    tuple(unique_items.values()),
                    pages_processed,
                    current_page_token,
                    "invalid_items",
                )
            parsed_items = [
                parsed
                for parsed in (playlist_video_item(item) for item in raw_items)
                if parsed is not None
            ]
            if len(parsed_items) != len(raw_items):
                return TypedPlaylistScanResult(
                    normalized_type,
                    playlist_id,
                    SCAN_ERROR,
                    tuple(unique_items.values()),
                    pages_processed,
                    current_page_token,
                    "invalid_playlist_item",
                )
            if reject_page_duplicates:
                page_video_ids = [item["video_id"] for item in parsed_items]
                if len(page_video_ids) != len(set(page_video_ids)):
                    return TypedPlaylistScanResult(
                        normalized_type,
                        playlist_id,
                        SCAN_ERROR,
                        tuple(unique_items.values()),
                        pages_processed,
                        current_page_token,
                        "duplicate_playlist_item",
                    )
            for item in parsed_items:
                unique_items.setdefault(item["video_id"], item)

            raw_next_page_token = response.get("nextPageToken")
            if raw_next_page_token is not None and (
                not isinstance(raw_next_page_token, str)
                or raw_next_page_token != raw_next_page_token.strip()
                or not raw_next_page_token
                or len(raw_next_page_token) > MAX_PAGE_TOKEN_LENGTH
            ):
                return TypedPlaylistScanResult(
                    normalized_type, playlist_id, SCAN_ERROR,
                    tuple(unique_items.values()), pages_processed,
                    current_page_token, "invalid_next_page_token",
                )
            next_page_token = (
                raw_next_page_token.strip()
                if raw_next_page_token is not None
                else None
            )
            if not next_page_token:
                return TypedPlaylistScanResult(
                    normalized_type,
                    playlist_id,
                    SCAN_COMPLETE if unique_items else SCAN_EMPTY,
                    tuple(unique_items.values()),
                    pages_processed,
                    None,
                )
            if not parsed_items:
                return TypedPlaylistScanResult(
                    normalized_type,
                    playlist_id,
                    SCAN_ERROR,
                    tuple(unique_items.values()),
                    pages_processed,
                    current_page_token,
                    "empty_page_with_continuation",
                )
            if next_page_token in seen_page_tokens:
                return TypedPlaylistScanResult(
                    normalized_type,
                    playlist_id,
                    SCAN_ERROR,
                    tuple(unique_items.values()),
                    pages_processed,
                    current_page_token,
                    "repeated_page_token",
                )
            seen_page_tokens.add(next_page_token)
            current_page_token = next_page_token

        return TypedPlaylistScanResult(
            normalized_type,
            playlist_id,
            SCAN_CONTINUATION,
            tuple(unique_items.values()),
            pages_processed,
            current_page_token,
        )
    except playlist_not_found_errors as error:
        return TypedPlaylistScanResult(
            normalized_type,
            playlist_id,
            SCAN_PLAYLIST_NOT_FOUND,
            tuple(unique_items.values()),
            pages_processed,
            current_page_token,
            error.__class__.__name__,
        )
    except Exception as error:
        return TypedPlaylistScanResult(
            normalized_type,
            playlist_id,
            SCAN_ERROR,
            tuple(unique_items.values()),
            pages_processed,
            current_page_token,
            error.__class__.__name__,
        )
