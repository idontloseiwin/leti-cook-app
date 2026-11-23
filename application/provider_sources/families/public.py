"""Parse responses for the supported public-key Data API families.

Most families need no hook: the registry describes every value rule. Notes per family:

* i18nLanguages / i18nRegions / videoCategories: reference lists, one request each; names
  follow ``hl``; categories depend on the region; ``assignable=false`` is a known value.
  A category label never decides a video's type (youtube_categories.py).
* activities: the contentDetails object depends on snippet.type; the other types' objects
  are absent by documentation (absent_means_not_applicable). Bulletins, likes and
  favourites are no longer returned (deprecated paths, never selectable).
* channelSections: one request, no pages; contentDetails lists only playlist and channel IDs.
* subscriptions: a row is a relation (unit ``subscription``), never a channel; private
  subscriptions answer 403 subscriptionForbidden (target ``access_denied``).
* commentThreads / comments: rows are threads and comments (personal data, on request only).
  A thread carries some replies only (``replies.comments[]``, one value state per reply); the
  full list is comments.list with ``parentId``. 403 commentsDisabled is the video's state
  (``disabled``). Text is HTML unless textFormat=plainText and is never rendered.
* search: rows are hits of mixed kinds (``SearchList.row_identity``); results are not
  exhaustive; the region the provider used is reported (an unsupported regionCode may be
  replaced).
* liveChatMessages.list: polled, not paged (policy.POLLED_METHODS): one request per
  acquisition, the next poll only through the continuation and not before
  pollingIntervalMillis; offlineAt ends the chat. Key access is not confirmed by the reference.
"""
import re

from youtube_source_fields import parse_dt

from ..parse import REJECT_ROW
from ..plan import IDENTITIES
from ..results import iso_utc
from .base import Hook

SEARCH_KINDS = {"youtube#video": ("video", "videoId"), "youtube#channel": ("channel", "channelId"),
                "youtube#playlist": ("playlist", "playlistId")}
_REGION = re.compile(r"[A-Z]{2}\Z")


class PlainList(Hook):
    """A public family whose rows the registry describes completely."""


class SearchList(Hook):
    def row_identity(self, method, item):
        hit = item.get("id")
        # Only a text kind can name a documented hit kind (a list or object is not one).
        kind = SEARCH_KINDS.get(hit["kind"]) if isinstance(hit, dict) and isinstance(hit.get("kind"), str) else None
        if kind is None:
            return REJECT_ROW          # an undocumented hit kind: the row-kind field cannot be unknown
        value = hit.get(kind[1])
        if not isinstance(value, str) or IDENTITIES[kind[0]].fullmatch(value) is None:
            return REJECT_ROW
        return kind[0], value

    def page_notes(self, method, payload):
        region = payload.get("regionCode")
        return (f"region:{region}",) if isinstance(region, str) and _REGION.fullmatch(region) else ()


class LiveChatMessagesList(Hook):
    def page_notes(self, method, payload):
        notes = []
        interval = payload.get("pollingIntervalMillis")
        if type(interval) is int and interval >= 0:
            notes.append(f"poll_interval_ms:{interval}")
        offline = payload.get("offlineAt")
        moment = parse_dt(offline) if isinstance(offline, str) and len(offline) <= 64 else None
        if moment is not None and moment.tzinfo is not None and moment.utcoffset() is not None:
            notes.append("chat_offline_at:" + iso_utc(moment))
        return tuple(notes)
