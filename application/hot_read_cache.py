"""Bounded idle eviction for the existing transient server read cache."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time


class TransientReadCache(dict):
    def __init__(self, lock, *, max_entries=256, clock=lambda: datetime.now(timezone.utc),
                 monotonic=time.monotonic, auto_prune=True):
        super().__init__()
        self.lock, self.max_entries = lock, max_entries
        self.clock, self.monotonic, self.auto_prune = clock, monotonic, auto_prune
        self._timer = None

    def purge_expired(self):
        with self.lock:
            wall_now, tick = self.clock(), self.monotonic()
            for key, entry in list(self.items()):
                deadline = entry.get("source_expires_at")
                if tick >= entry["expires_at"] or (deadline is not None and wall_now >= deadline):
                    self.pop(key, None)

    def _schedule(self):
        if self.auto_prune and self._timer is None and self:
            delay = max(0.01, min(60, min(entry["expires_at"] for entry in self.values()) - self.monotonic()))
            self._timer = threading.Timer(delay, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _expire(self):
        with self.lock:
            self._timer = None
            self.purge_expired()
            self._schedule()

    def __setitem__(self, key, value):
        with self.lock:
            self.purge_expired()
            while len(self) >= self.max_entries and key not in self:
                self.pop(next(iter(self)))
            super().__setitem__(key, value)
            self._schedule()
