"""Bounded, source-free update-copy lifetime contract (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time

MARKER_FILENAME = 'marker.json'
CIPHER_FILENAME = 'copy.enc'
PARTIAL_FILENAME = 'copy.partial'
KEY_FILENAME = 'copy.key'
MAX_MARKER_BYTES = 512
MAX_LIFETIME_SECONDS = 86400
# The source-age read may take this long; its proof is taken at the start.
WINDOW_READ_SECONDS = 60
MAX_UTC_SECONDS = 253402300799


class UpdateCopyError(ValueError):
    """A fixed diagnostic code; never include paths, keys or source contents."""


@dataclass(frozen=True, slots=True)
class Marker:
    created_at: int
    expires_at: int

    def __post_init__(self):
        if (type(self.created_at) is not int or type(self.expires_at) is not int
                or not 0 < self.created_at < self.expires_at <= MAX_UTC_SECONDS
                or self.expires_at - self.created_at > MAX_LIFETIME_SECONDS):
            raise UpdateCopyError('update_copy_marker_invalid')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateCopyError('update_copy_marker_invalid')
        result[key] = value
    return result


def parse_marker(data: bytes) -> Marker:
    if type(data) is not bytes or not 0 < len(data) <= MAX_MARKER_BYTES:
        raise UpdateCopyError('update_copy_marker_invalid')
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object)
        if type(value) is not dict or set(value) != {'created_at', 'expires_at'}:
            raise ValueError
        return Marker(**value)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise UpdateCopyError('update_copy_marker_invalid') from exc


def marker_bytes(marker: Marker) -> bytes:
    if type(marker) is not Marker:
        raise UpdateCopyError('update_copy_marker_invalid')
    return (json.dumps({'created_at': marker.created_at, 'expires_at': marker.expires_at},
                       separators=(',', ':'), sort_keys=True) + '\n').encode('ascii')


def _source_timestamp(value):
    if type(value) is not str or not 20 <= len(value) <= 40 or 'T' not in value:
        raise UpdateCopyError('update_copy_window_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError
        timestamp = parsed.timestamp()
        if not math.isfinite(timestamp) or not 0 < timestamp <= MAX_UTC_SECONDS:
            raise ValueError
        return timestamp
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise UpdateCopyError('update_copy_window_invalid') from exc


def window_facts_version(window) -> int:
    """Validate fixed V1/V2 facts without granting an update-copy exception."""
    base = {'as_of', 'oldest_source_at', 'expired', 'invalid_source_clock'}
    if (type(window) is not dict or type(window.get('expired')) is not bool
            or type(window.get('invalid_source_clock')) is not bool):
        raise UpdateCopyError('update_copy_window_invalid')
    if set(window) == base:
        return 1
    if (set(window) != base | {'version', 'retained_profile_only'}
            or type(window['version']) is not int or window['version'] != 2
            or type(window['retained_profile_only']) is not bool
            or (window['retained_profile_only'] and not window['invalid_source_clock'])):
        raise UpdateCopyError('update_copy_window_invalid')
    return 2


def marker_from_window(window, *, created_at: int) -> Marker:
    """One conservative deadline from a fresh, authoritative stopped-writer proof."""
    window_facts_version(window)
    if type(created_at) is not int or not 0 < created_at <= MAX_UTC_SECONDS:
        raise UpdateCopyError('update_copy_window_invalid')
    # Distinct codes let the updater explain which condition to wait for.
    if window['invalid_source_clock']:
        raise UpdateCopyError('update_copy_window_unconfirmed')
    if window['expired']:
        raise UpdateCopyError('update_copy_window_expired')
    as_of = _source_timestamp(window['as_of'])
    # A proof from the future is a clock problem. An older proof is allowed for
    # the read's own duration: the expiry below is computed from the oldest
    # acquisition time, not from as_of, so a slow read cannot extend it.
    if as_of - created_at > 5 or created_at - as_of > WINDOW_READ_SECONDS + 5:
        raise UpdateCopyError('update_copy_window_stale')
    expires = created_at + MAX_LIFETIME_SECONDS
    if window['oldest_source_at'] is not None:
        oldest = _source_timestamp(window['oldest_source_at'])
        if oldest > as_of or as_of - oldest > 29 * 86400:
            raise UpdateCopyError('update_copy_window_invalid')
        expires = min(expires, math.floor(oldest) + 30 * 86400 - 3600)
    return Marker(created_at, expires)


class Deadline:
    """A restart never extends the marker; wall rollback cannot extend this process."""
    def __init__(self, marker: Marker, *, wall_time=time.time, monotonic=time.monotonic):
        if type(marker) is not Marker:
            raise UpdateCopyError('update_copy_marker_invalid')
        self.marker = marker
        self._wall = wall_time
        self._mono = monotonic
        wall = self._finite(self._wall())
        mono = self._finite(self._mono())
        if wall < marker.created_at:
            raise UpdateCopyError('update_copy_clock_invalid')
        self._started = mono
        self._limit = mono + max(0.0, marker.expires_at - wall)
        self.check()

    @staticmethod
    def _finite(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise UpdateCopyError('update_copy_clock_invalid')
        return value

    def check(self):
        wall = self._finite(self._wall())
        mono = self._finite(self._mono())
        if wall < self.marker.created_at or mono < self._started:
            raise UpdateCopyError('update_copy_clock_invalid')
        if wall >= self.marker.expires_at or mono >= self._limit:
            raise UpdateCopyError('update_copy_expired')
