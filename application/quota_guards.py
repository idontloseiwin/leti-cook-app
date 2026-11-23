"""In-process guards for the YouTube quota spent by owner actions.

Lookup cache and add-rate windows live in this process and reset on restart.
Backfill cooldown uses the latest durable job already stored by the application;
this module writes nothing.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime, timezone
import math
import threading
import time

from hot_storage import utc_instant

# A handle can move to another channel, so found answers are kept for hours.
FOUND_TTL_SECONDS = 6 * 60 * 60
# A missing handle may be claimed later; asking again after a pause costs one unit.
NOT_FOUND_TTL_SECONDS = 15 * 60
MAX_REMEMBERED_LOOKUPS = 4096
# (seconds, calls): a person adding channels one at a time never reaches these.
CHANNEL_ADD_WINDOWS = ((60, 20), (24 * 60 * 60, 300))
CURRENT_STATS_BACKFILL_COOLDOWN_SECONDS = 6 * 60 * 60


class ChannelLookupCache:
    """Definite YouTube answers: locator -> channel ID, or None for "not found"."""

    def __init__(self, *, found_ttl=FOUND_TTL_SECONDS, not_found_ttl=NOT_FOUND_TTL_SECONDS,
                 max_entries=MAX_REMEMBERED_LOOKUPS, clock=time.monotonic):
        self._entries = OrderedDict()
        self._lock = threading.Lock()
        self._found_ttl = found_ttl
        self._not_found_ttl = not_found_ttl
        self._max_entries = max_entries
        self._clock = clock

    @staticmethod
    def _key(kind, value):
        # Handles are case-insensitive on YouTube; channel and video IDs are not.
        return kind, value.casefold() if kind == "handle" else value

    def get(self, kind, value):
        """Return (known, channel_id); channel_id is None for a remembered "not found"."""
        key = self._key(kind, value)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            expires_at, channel_id = entry
            if expires_at <= self._clock():
                del self._entries[key]
                return False, None
            return True, channel_id

    def put(self, kind, value, channel_id):
        key = self._key(kind, value)
        ttl = self._found_ttl if channel_id else self._not_found_ttl
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (self._clock() + ttl, channel_id)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


class RateLimiter:
    """Sliding windows of (seconds, calls); every admitted call counts in each window."""

    def __init__(self, windows, *, clock=time.monotonic):
        self._windows = tuple((seconds, calls, deque()) for seconds, calls in windows)
        self._lock = threading.Lock()
        self._clock = clock

    def acquire(self):
        """Admit and record one call (0), or return whole seconds until one would be admitted."""
        now = self._clock()
        with self._lock:
            wait = 0
            for seconds, limit, admitted in self._windows:
                while admitted and admitted[0] <= now - seconds:
                    admitted.popleft()
                if len(admitted) >= limit:
                    wait = max(wait, admitted[0] + seconds - now)
            if wait:
                return max(1, math.ceil(wait))
            for _, _, admitted in self._windows:
                admitted.append(now)
            return 0


def backfill_cooldown_seconds(job, *, now=None, cooldown=CURRENT_STATS_BACKFILL_COOLDOWN_SECONDS):
    """Seconds left before this channel's current stats may be backfilled again.

    Only a completed job with videos counts: a failed or cancelled job may be
    retried at once, and an empty job spent no quota.
    """
    if not job or job.get("status") != "completed" or not job.get("target_count"):
        return 0
    elapsed = ((now or datetime.now(timezone.utc)) - utc_instant(job["finished_at"])).total_seconds()
    return min(cooldown, max(0, math.ceil(cooldown - elapsed)))


def wait_text(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 90:
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    return f"{math.ceil(minutes / 60)} hours"
