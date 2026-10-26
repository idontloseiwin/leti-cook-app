"""Short-lived encrypted continuation, bound to trusted context and query.

The key is process-owned and ephemeral. Restart invalidates continuations;
there is no DB write, persisted result, API payload log or shared global key.
"""
import base64
from datetime import datetime, timezone
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .validation import QueryRejected, canonical, _pairs
from .contracts import MAX_CURSOR_CHARS
import json


class CursorCodec:
    def __init__(self):
        self._cipher = AESGCM(AESGCM.generate_key(bit_length=256))

    def encode(self, plan, *, scope_revision, keys, valid_before):
        data = canonical({"v": 1, "q": plan.fingerprint, "c": plan.context_fingerprint,
                          "s": scope_revision, "keys": keys, "until": valid_before})
        nonce = os.urandom(12)
        token = base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, data, b"lic-query-cursor-v1")).decode().rstrip("=")
        if len(token) > MAX_CURSOR_CHARS:
            raise QueryRejected("query_budget_exceeded")
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
