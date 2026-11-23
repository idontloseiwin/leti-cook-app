"""Page strategies of one acquisition.

``id_batch``: one request per batch of at most the method's ID bound, every ID
accounted. ``page_token``: ``maxResults`` = min(requested, method maximum); the
continuation token is checked by ``typed_playlist.checked_next_page_token`` (a
string of at most 2048 characters, never repeated, never after an empty page).
``single``: one request. A shorter page than ``maxResults`` with a token simply
continues. Tokens are never logged; provenance carries a hash chain of them.
"""
from __future__ import annotations

import hashlib

from typed_playlist import PlaylistTraversalError, checked_next_page_token

from .errors import ResponseInvalid

CHAIN_START = hashlib.sha256(b"lic-page-chain-v1").hexdigest()


def chain(previous, token):
    """Provenance of the page reached with ``token``; the token itself is not kept."""
    return hashlib.sha256((previous + "\0" + token).encode("utf-8")).hexdigest()


def next_page_token(payload, item_count, seen, *, poll=False):
    """``poll``: a polling mode (members.list mode=updates) documents an empty page with a token."""
    try:
        return checked_next_page_token(payload, max(item_count, 1) if poll else item_count, seen)
    except PlaylistTraversalError as error:
        raise ResponseInvalid(error.code) from None


MAX_POLL_TOKEN = 2048
MAX_POLL_INTERVAL_MS = 3_600_000


def poll_position(payload):
    """(next token or None, polling interval ms or None, offline) of one live-chat poll.

    An empty poll with a token is normal for a chat (nothing new yet), unlike a page
    list. The interval is the documented wait before the next poll.
    """
    token = payload.get("nextPageToken")
    if token is not None and (not isinstance(token, str) or not 1 <= len(token) <= MAX_POLL_TOKEN
                              or not token.isascii() or not token.isprintable()):
        raise ResponseInvalid("response_page_token_invalid")
    interval = payload.get("pollingIntervalMillis")
    if interval is not None and (type(interval) is not int or not 0 <= interval <= MAX_POLL_INTERVAL_MS):
        raise ResponseInvalid("response_polling_invalid")
    offline = payload.get("offlineAt")
    if offline is not None and not isinstance(offline, str):
        raise ResponseInvalid("response_polling_invalid")
    return token, interval, offline is not None
