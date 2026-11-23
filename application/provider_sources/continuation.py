"""Short-lived encrypted continuation of a partial acquisition.

Like research_query.cursor: an AES-GCM key owned by the process, so a restart
invalidates every continuation. The token binds the plan fingerprint and the
context and lives 10 minutes. It carries the provider page token or the next ID
batch; nothing is stored, logged or shown in clear.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import PlanRejected

LIFETIME = timedelta(minutes=10)
MAX_CHARS = 8192
_AAD = b"lic-acquisition-continuation-v1"


class ContinuationCodec:
    def __init__(self):
        self._cipher = AESGCM(AESGCM.generate_key(bit_length=256))

    def encode(self, *, fingerprint, context, position, now):
        data = json.dumps({"v": 1, "f": fingerprint, "c": context, "p": position,
                           "u": (now + LIFETIME).astimezone(timezone.utc).isoformat()},
                          sort_keys=True, separators=(",", ":")).encode("utf-8")
        nonce = os.urandom(12)
        token = base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, data, _AAD)).decode("ascii").rstrip("=")
        if len(token) > MAX_CHARS:
            raise PlanRejected("continuation_too_large")
        return token

    def decode(self, token, *, fingerprint, context, now):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= MAX_CHARS:
                raise ValueError()
            blob = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            data = json.loads(self._cipher.decrypt(blob[:12], blob[12:], _AAD))
            if set(data) != {"v", "f", "c", "p", "u"} or data["v"] != 1 or not isinstance(data["p"], dict):
                raise ValueError()
            until = datetime.fromisoformat(data["u"])
        except Exception:
            raise PlanRejected("continuation_invalid") from None
        if until.tzinfo is None or until <= now.astimezone(timezone.utc):
            raise PlanRejected("continuation_expired")
        if data["f"] != fingerprint or data["c"] != context:
            raise PlanRejected("continuation_plan_changed")
        return data["p"]
