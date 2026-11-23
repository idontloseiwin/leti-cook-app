"""Credentials the shared YouTube transport accepts besides the installation key.

The installation key context is ``credential=None``: the transport obtains the
installation's key exactly as collection does, and only a refusal of that key
marks it refused. An ``OwnerBearer`` is a channel owner's OAuth access token for
one request. It travels as ``Authorization: Bearer``, never together with the
installation key, and its 401 is returned to the caller instead of marking the
installation key refused. There is no sign-in flow here: an
owner credential provider supplies the token (02 design, section 6.2).
"""
import re

# RFC 6750 b64token characters; length bounds keep headers and logs finite.
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{16,4096}\Z")


class OwnerBearer:
    """An owner access token; its value never appears in repr, str or pickles."""

    __slots__ = ("_token",)

    def __init__(self, token):
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise ValueError("owner_bearer_invalid")
        self._token = token

    def authorization_header(self):
        return "Bearer " + self._token

    def __repr__(self):
        return "OwnerBearer(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("owner_bearer_not_serializable")
