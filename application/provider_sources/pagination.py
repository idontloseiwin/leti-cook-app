"""Page strategies of one acquisition.

``id_batch``: one request per batch of at most the method's ID bound, every ID
accounted. ``page_token``: ``maxResults`` = min(requested, method maximum); the
continuation token is checked by ``typed_playlist.checked_next_page_token`` (a
string of at most 2048 characters, never repeated). Search can return an empty
intermediate page after filtering, and a members update poll can be empty.
``single``: one request. A shorter page than ``maxResults`` with a token simply
continues. Tokens are never logged; provenance carries a hash chain of them.
"""
from __future__ import annotations

import base64
import hashlib

from typed_playlist import PlaylistTraversalError, checked_next_page_token

from .errors import ResponseInvalid

CHAIN_START = hashlib.sha256(b"lic-page-chain-v1").hexdigest()


def chain(previous, token):
    """Provenance of the page reached with ``token``; the token itself is not kept."""
    return hashlib.sha256((previous + "\0" + token).encode("utf-8")).hexdigest()


def next_page_token(payload, item_count, seen, *, allow_empty=False):
    """Validate the token; only selected provider methods allow an empty intermediate page."""
    try:
        return checked_next_page_token(payload, max(item_count, 1) if allow_empty else item_count, seen)
    except PlaylistTraversalError as error:
        raise ResponseInvalid(error.code) from None


class SearchPageHistory:
    """Detect search cycles across separately confirmed pages with bounded cursor storage."""

    def __init__(self, position):
        self.recent = list(position.get("search_recent", ()))
        self.anchor = position.get("search_anchor")

    @staticmethod
    def resume_token(position):
        if "search_token" in position:
            return base64.b64decode(position["search_token"]).decode("utf-8")
        return position.get("token")

    def observe(self, token, page_number):
        digest_bytes = hashlib.sha256(token.encode("utf-8")).digest()[:16]
        digest = base64.urlsafe_b64encode(digest_bytes).decode("ascii").rstrip("=")
        if digest in self.recent or digest == self.anchor:
            raise ResponseInvalid("repeated_page_token")
        # A short cycle is caught on its first repeat. For a longer cycle, comparing
        # against one anchor for increasingly long spans also guarantees detection.
        if page_number.bit_count() == 1:  # checkpoint on pages 1, 2, 4, 8, ...
            self.anchor = digest
        self.recent = (self.recent + [digest])[-32:]  # covers a full Find channels action session

    def position(self, token):
        # Encoding the opaque token once prevents JSON escaping every control or
        # Unicode character inside the length-limited encrypted continuation.
        position = {"search_recent": self.recent, "search_anchor": self.anchor}
        if token is not None:
            position["search_token"] = base64.b64encode(token.encode("utf-8")).decode("ascii")
        return position


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
