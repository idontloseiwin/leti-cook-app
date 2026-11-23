"""Short-lived encrypted continuation, bound to trusted context and query.

The key is process-owned and ephemeral. Restart invalidates continuations;
there is no DB write, persisted result, API payload log or shared global key.
Frozen cursors and references use the same key with their own associated data,
so no token of one kind is accepted as another.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .validation import QueryRejected, canonical, _pairs
from .contracts import MAX_CURSOR_CHARS, MAX_FROZEN_ROWS
import json

_FROZEN_PAGE = b"lic-query-frozen-page-v1"
_FROZEN_REF = b"lic-query-frozen-ref-v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SESSION = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class FrozenPosition:
    """A decoded frozen cursor (offset set) or reference (offset None)."""
    session_id: str
    query_fingerprint: str
    context_fingerprint: str
    offset: int | None


class CursorCodec:
    def __init__(self):
        self._cipher = AESGCM(AESGCM.generate_key(bit_length=256))

    def _seal(self, data, purpose):
        nonce = os.urandom(12)
        token = base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, canonical(data), purpose)).decode().rstrip("=")
        if len(token) > MAX_CURSOR_CHARS:
            raise QueryRejected("query_budget_exceeded", limit="response")
        return token

    def _open(self, token, purpose):
        if not isinstance(token, str) or not 1 <= len(token) <= MAX_CURSOR_CHARS:
            raise ValueError()
        blob = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        return json.loads(self._cipher.decrypt(blob[:12], blob[12:], purpose), object_pairs_hook=_pairs)

    def encode_frozen(self, session, offset):
        """A signed (session, offset) position inside one frozen result."""
        return self._seal({"v": 1, "f": session.session_id, "o": offset, "q": session.query_fingerprint,
                           "c": session.context_fingerprint}, _FROZEN_PAGE)

    def encode_frozen_ref(self, session):
        return self._seal({"v": 1, "f": session.session_id, "q": session.query_fingerprint,
                           "c": session.context_fingerprint}, _FROZEN_REF)

    def decode_frozen(self, token, *, reference=False):
        """Any malformed, foreign, tampered or pre-restart token is expired."""
        keys = {"v", "f", "q", "c"} if reference else {"v", "f", "o", "q", "c"}
        try:
            data = self._open(token, _FROZEN_REF if reference else _FROZEN_PAGE)
            if type(data) is not dict or set(data) != keys or data["v"] != 1:
                raise ValueError()
            if (type(data["f"]) is not str or not _SESSION.fullmatch(data["f"])
                    or any(type(data[k]) is not str or not _HEX64.fullmatch(data[k]) for k in ("q", "c"))):
                raise ValueError()
            offset = None if reference else data["o"]
            if not reference and (type(offset) is not int or not 0 <= offset < MAX_FROZEN_ROWS):
                raise ValueError()
            return FrozenPosition(data["f"], data["q"], data["c"], offset)
        except Exception:
            raise QueryRejected("cursor_expired") from None

    def encode(self, plan, *, scope_revision, keys, valid_before):
        data = canonical({"v": 1, "q": plan.fingerprint, "c": plan.context_fingerprint,
                          "s": scope_revision, "keys": keys, "until": valid_before})
        nonce = os.urandom(12)
        token = base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, data, b"lic-query-cursor-v1")).decode().rstrip("=")
        if len(token) > MAX_CURSOR_CHARS:
            raise QueryRejected("query_budget_exceeded", limit="response")
        return token

    def decode(self, token, plan, now):
        if token is None:
            return None
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= MAX_CURSOR_CHARS:
                raise ValueError()
            blob = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            data = json.loads(self._cipher.decrypt(blob[:12], blob[12:], b"lic-query-cursor-v1"), object_pairs_hook=_pairs)
            if set(data) != {"v", "q", "c", "s", "keys", "until"} or data["v"] != 1:
                raise ValueError()
            deadline = datetime.fromisoformat(data["until"])
            if deadline.tzinfo is None or deadline <= now.astimezone(timezone.utc):
                raise QueryRejected("cursor_expired")
            if data["q"] != plan.fingerprint or data["c"] != plan.context_fingerprint:
                raise QueryRejected("cursor_context_changed")
            if type(data["keys"]) is not list or not 1 <= len(data["keys"]) <= 4:
                raise ValueError()
            if any(v is not None and type(v) is not str for v in data["keys"]):
                raise ValueError()
            return data
        except QueryRejected:
            raise
        except Exception:
            raise QueryRejected("cursor_expired") from None
