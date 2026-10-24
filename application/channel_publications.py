"""Raw publication observations; never a countdown or inferred video type."""

import re

from db.reads.channel_publications import PUBLICATION_PAGE_SIZE
from hot_storage import HOT_DATA_WINDOW, iso_utc, source_is_current, utc_instant, utc_now


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
PUBLICATION_STATUSES = frozenset({"upcoming", "live", "awaiting_completion"})


class PublicationReadError(ValueError):
    pass


def build_channel_publications(repository, channel_id, *, cursor=None, now=None):
    now = utc_instant(now or utc_now())
    rows = repository.load_page(channel_id, cursor=cursor)
    if len(rows) > PUBLICATION_PAGE_SIZE + 1:
        raise PublicationReadError("Publication page exceeds its bound")
    videos = []
    previous = cursor
    for row in rows:
        video_id = row.get("video_id")
        if (not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id)
                or previous is not None and video_id <= previous
                or row.get("publication_status") not in PUBLICATION_STATUSES
                or row.get("video_type") not in (None, "long", "short", "stream")
                or not isinstance(row.get("title"), (str, type(None)))
                or len(row.get("title") or "") > 1000
                or not source_is_current(row.get("checked_at"), now=now)):
            raise PublicationReadError("Publication observation is invalid")
        try:
            checked_at = utc_instant(row["checked_at"])
            expiry = utc_instant(row["source_expires_at"])
            scheduled = row.get("scheduled_start_at")
            scheduled = iso_utc(utc_instant(scheduled)) if scheduled is not None else None
        except (ValueError, TypeError, OverflowError) as error:
            raise PublicationReadError("Publication observation has an invalid date") from error
        if not now < expiry <= checked_at + HOT_DATA_WINDOW:
            raise PublicationReadError("Publication observation has expired")
        videos.append({
            "video_id": video_id, "video_type": row.get("video_type"),
            "title": row.get("title"), "publication_status": row["publication_status"],
            "scheduled_start_at": scheduled, "checked_at": iso_utc(checked_at),
            "source_expires_at": iso_utc(expiry),
        })
        previous = video_id
    more = len(videos) > PUBLICATION_PAGE_SIZE
    videos = videos[:PUBLICATION_PAGE_SIZE]
    return {
        "contract_version": 1, "channel_id": channel_id, "videos": videos,
        "next_cursor": videos[-1]["video_id"] if more else None,
        "source": {"kind": "stored_publication_observations", "youtube_requests": False,
                   "database_writes": False},
    }
