"""Safe user-authored channel locators, independent of YouTube response fields.

Normalize syntax only. This is not proof that a channel/video exists. Keep the
first locator after successful add/restore; provider refreshes must not author it.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlsplit

_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_BASE = "https://www.youtube.com/"
_TABS = {"videos", "shorts", "streams", "featured", "about", "playlists", "community"}


class UnsupportedLegacyChannelLink(ValueError):
    """A recognized old YouTube URL, not a failed provider lookup."""


def _handle(value: str) -> str:
    if (not 1 <= len(value) <= 100
            or any(not (unicodedata.category(char)[0] in {"L", "N", "M"}
                        or char in "_.-·") for char in value)
            or value[0] in "_.-·" or value[-1] in "_.-·"):
        raise ValueError("user_channel_input_invalid")
    return value


def parse_user_channel_input(raw: str) -> tuple[str, str, str]:
    """Return (safe user locator, lookup kind, lookup value), with no I/O.

    Channel tab suffixes and all non-identity query/fragment fields are discarded.
    Watch links retain exactly one `v`; Shorts/live/share links retain their video
    identity. URLs containing credentials/ports or unsupported hosts are refused.
    """
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("user_channel_input_invalid")
    value = raw.strip()
    if not value or any(unicodedata.category(char)[0] == "C" for char in value):
        raise ValueError("user_channel_input_invalid")
    if _CHANNEL.fullmatch(value):
        return _BASE + "channel/" + value, "id", value
    if "://" not in value and "/" not in value and "?" not in value and "#" not in value:
        handle = _handle(value[1:] if value.startswith("@") else value)
        return _BASE + "@" + quote(handle, safe="_.-"), "handle", "@" + handle
    if value.split("/", 1)[0].lower() in _HOSTS | _SHORT_HOSTS:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or parsed.username is not None
                or parsed.password is not None or parsed.port is not None
                or parsed.hostname not in _HOSTS | _SHORT_HOSTS):
            raise ValueError("user_channel_input_invalid")
        path = unquote(parsed.path, errors="strict")
        if any(unicodedata.category(char)[0] == "C" for char in path):
            raise ValueError("user_channel_input_invalid")
        pieces = path.strip("/").split("/")
        if parsed.hostname in _SHORT_HOSTS:
            if len(pieces) != 1 or not _VIDEO.fullmatch(pieces[0]):
                raise ValueError("user_channel_input_invalid")
            return _BASE + "watch?v=" + pieces[0], "video", pieces[0]
        if pieces[0] in {"c", "user"} and len(pieces) >= 2 and pieces[1]:
            raise UnsupportedLegacyChannelLink("unsupported_legacy_channel_link")
        if len(pieces) == 1 and pieces[0] == "watch":
            identities = [v for key, v in parse_qsl(parsed.query, keep_blank_values=True,
                          strict_parsing=False, max_num_fields=64) if key == "v"]
            if len(identities) != 1 or not _VIDEO.fullmatch(identities[0]):
                raise ValueError("user_channel_input_invalid")
            return _BASE + "watch?v=" + identities[0], "video", identities[0]
        if len(pieces) == 2 and pieces[0] in {"shorts", "live"} and _VIDEO.fullmatch(pieces[1]):
            return _BASE + "watch?v=" + pieces[1], "video", pieces[1]
        if pieces[0] == "channel" and len(pieces) in {2, 3} and _CHANNEL.fullmatch(pieces[1]):
            if len(pieces) == 3 and pieces[2] not in _TABS:
                raise ValueError("user_channel_input_invalid")
            return _BASE + "channel/" + pieces[1], "id", pieces[1]
        if pieces[0].startswith("@") and len(pieces) in {1, 2}:
            if len(pieces) == 2 and pieces[1] not in _TABS:
                raise ValueError("user_channel_input_invalid")
            handle = _handle(pieces[0][1:])
            return _BASE + "@" + quote(handle, safe="_.-"), "handle", "@" + handle
    except UnsupportedLegacyChannelLink:
        raise
    except (ValueError, UnicodeError):
        raise ValueError("user_channel_input_invalid") from None
    raise ValueError("user_channel_input_invalid")


def normalize_user_channel_input(raw: str) -> str:
    return parse_user_channel_input(raw)[0]
