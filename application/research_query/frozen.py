"""Process-local frozen query results for consistency "frozen".

A frozen request reads the complete authorized matching population once, in its
final order, through the ordinary SQL compiler. The shaped rows (the selected
values with their states, clocks and provenance), coverage and context are kept
here, in memory only, until the result expires. Pages, totals, previous pages,
anchors and the matching-ID export are served from that copy, so rows cannot
move, repeat or vanish between pages. Newer collected data does not change it.

Lifetime: FROZEN_TTL_SECONDS. The SQL admits a source only when it stays
current until that end, so a row or value that would expire inside the session
is withheld and counted instead of shortening it. Restart (a new cursor key),
eviction, channel invalidation and runtime cache clears expire a result; a
changed access context or scope revision is refused on the next access.

Dependent reads (facets, Inspector history/launch/static details, the lifetime
baseline) take a frozen reference, the `context.frozen_ref` token of any page:

    reference = service.frozen_reference(token)      # same checks as a page
    reference.contains(video_id) / reference.member(video_id)
    member.row, member.evaluated_at, member.valid_before
    ... a bounded live read with the frozen clocks ...
    reference.require_current()                       # fence after that read

This module holds no SQL, no I/O and no credentials; nothing is persisted.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import threading
import time
from types import MappingProxyType

from .validation import QueryRejected, canonical

FROZEN_TTL_SECONDS = 600
# Like the browsing sessions they replace: two large channels fit together.
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_STORE_BYTES = 128 * 1024 * 1024
MAX_SESSIONS = 8
MAX_BUILDS = 2
# Retained Python objects per row besides its encoded JSON: the bytes object
# and its tuple slot, the ID string and its position-map entry. Measured with
# tracemalloc on 20,000 rows (see tests/test_research_frozen.py).
ROW_OVERHEAD_BYTES = 256
# A stored row is [id, [[state, value, provenance_ref, observed_at, expires_at,
# observation] per column]]: the wire cell without repeating its keys.
CELL_KEYS = ("state", "value", "provenance_ref", "observed_at", "expires_at", "observation")


def compact_row(columns, row):
    return canonical([row["id"], [[row["cells"][column][key] for key in CELL_KEYS] for column in columns]])


# validation.canonical() with one encoder for every row of a build.
_CANONICAL = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compact_cells(row_id, cells):
    """compact_row() of the same row from service.shape_cells, whose cells already are in CELL_KEYS order."""
    return _CANONICAL.encode([row_id, cells]).encode("utf-8")


def expand_row(columns, encoded):
    """The wire row (as QueryResult rows) of a stored compact row."""
    row_id, cells = json.loads(encoded)
    return {"id": row_id, "cells": {column: dict(zip(CELL_KEYS, cell)) for column, cell in zip(columns, cells)}}


@dataclass(frozen=True, eq=False)
class FrozenSession:
    session_id: str
    plan: object  # The ValidatedQuery that froze it: scope rechecks reuse its SQL.
    query_fingerprint: str
    conditions_key: str  # Without sort, projection and page: for facets later.
    context_fingerprint: str
    source_id: str
    video_type: str | None
    scope_kind: str
    scope_id: str
    scope_revision: str
    channel_ids: frozenset[str]  # Visible channels in scope when frozen.
    page_size: int
    evaluated_at: datetime
    valid_before: datetime
    columns: tuple[str, ...]
    # Canonical JSON {context (without frozen_ref), coverage, provenance}.
    metadata: bytes
    ids: tuple[str, ...]
    rows: tuple[bytes, ...]  # compact_row() per row, in the order of ids.
    positions: MappingProxyType  # id -> index
    size: int
    deadline: float = 0.0  # Monotonic expiry, set when stored.

    @property
    def total_count(self):
        return len(self.ids)

    def row(self, row_id):
        index = self.positions.get(row_id)
        return None if index is None else expand_row(self.columns, self.rows[index])


@dataclass(frozen=True)
class FrozenMember:
    """One frozen row with the clocks of the result it belongs to."""
    row: dict
    position: int
    evaluated_at: datetime
    valid_before: datetime
    query_fingerprint: str
    conditions_key: str
    source_id: str
    video_type: str | None


@dataclass(frozen=True)
class FrozenReference:
    """A checked handle on one frozen result, for a dependent read.

    Obtain it through QueryService.frozen_reference, which rechecks access,
    scope and lifetime first. Call require_current() after any further read and
    before answering: an expiry or invalidation in between voids the answer.
    """
    session: FrozenSession
    store: "FrozenStore"

    @property
    def evaluated_at(self):
        return self.session.evaluated_at

    @property
    def valid_before(self):
        return self.session.valid_before

    @property
    def query_fingerprint(self):
        return self.session.query_fingerprint

    @property
    def conditions_key(self):
        return self.session.conditions_key

    @property
    def video_type(self):
        return self.session.video_type

    @property
    def source_id(self):
        return self.session.source_id

    @property
    def total_count(self):
        return self.session.total_count

    def contains(self, row_id):
        return row_id in self.session.positions

    def ids(self):
        return self.session.ids

    def member(self, row_id):
        """The frozen row, or source_unavailable when it is not in the result."""
        index = self.session.positions.get(row_id)
        if index is None:
            raise QueryRejected("source_unavailable")
        session = self.session
        return FrozenMember(expand_row(session.columns, session.rows[index]), index, session.evaluated_at, session.valid_before,
                            session.query_fingerprint, session.conditions_key, session.source_id, session.video_type)

    def require_current(self):
        self.store.require(self.session)


class FrozenStore:
    """Bounded LRU of frozen results; one per API process."""

    def __init__(self, *, max_sessions=MAX_SESSIONS, max_bytes=MAX_STORE_BYTES,
                 max_session_bytes=MAX_SESSION_BYTES, max_builds=MAX_BUILDS,
                 monotonic=time.monotonic, clock=lambda: datetime.now(timezone.utc), auto_prune=True):
        # The lifetime itself comes from each result (SQL fresh_until, at most
        # FROZEN_TTL_SECONDS after evaluation); the store enforces it.
        if any(type(v) is not int or v < 1 for v in (max_sessions, max_bytes, max_session_bytes, max_builds)):
            raise ValueError("invalid_frozen_limits")
        self.max_sessions, self.max_bytes, self.max_session_bytes = max_sessions, max_bytes, max_session_bytes
        self.monotonic, self.clock, self.auto_prune = monotonic, clock, auto_prune
        self._sessions: OrderedDict[str, FrozenSession] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._builds = threading.BoundedSemaphore(max_builds)
        self._revision = 0
        self._timer = None

    @property
    def session_limit_bytes(self):
        return min(self.max_session_bytes, self.max_bytes)

    @contextmanager
    def build_slot(self):
        # A third concurrent build is refused at once, never queued: it would
        # hold memory for a result its caller may already have abandoned.
        if not self._builds.acquire(blocking=False):
            raise QueryRejected("query_busy")
        try:
            yield
        finally:
            self._builds.release()

    def generation(self):
        """Taken before a build reads; any invalidation since voids the build."""
        with self._lock:
            return self._revision

    def _expired(self, entry):
        return self.monotonic() >= entry.deadline or self.clock() >= entry.valid_before

    def _purge_locked(self):
        for key, entry in list(self._sessions.items()):
            if self._expired(entry):
                self._bytes -= self._sessions.pop(key).size

    def _schedule_locked(self):
        # Releases memory of an abandoned result; access also purges.
        if not self.auto_prune or self._timer is not None or not self._sessions:
            return
        delay = max(.01, min(60, min(e.deadline for e in self._sessions.values()) - self.monotonic()))
        self._timer = threading.Timer(delay, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        with self._lock:
            self._timer = None
            self._purge_locked()
            self._schedule_locked()

    def insert(self, session, generation):
        if session.size > self.session_limit_bytes:
            raise QueryRejected("query_budget_exceeded", "consistency", limit="memory")
        with self._lock:
            if generation != self._revision:
                raise QueryRejected("cursor_context_changed")
            self._purge_locked()
            remaining = (session.valid_before - self.clock()).total_seconds()
            if remaining <= 0:
                raise QueryRejected("cursor_expired")
            session = replace(session, deadline=self.monotonic() + remaining)
            while self._sessions and (len(self._sessions) >= self.max_sessions
                                      or self._bytes + session.size > self.max_bytes):
                _, removed = self._sessions.popitem(last=False)
                self._bytes -= removed.size
            self._sessions[session.session_id] = session
            self._bytes += session.size
            self._schedule_locked()
            return session

    def get(self, session_id):
        with self._lock:
            self._purge_locked()
            entry = self._sessions.get(session_id)
            if entry is None:
                raise QueryRejected("cursor_expired")
            self._sessions.move_to_end(session_id)
            return entry

    def require(self, session):
        """The same stored result, still unexpired (fence after a read)."""
        with self._lock:
            if self._sessions.get(session.session_id) is not session or self._expired(session):
                raise QueryRejected("cursor_expired")

    def discard(self, session):
        with self._lock:
            if self._sessions.get(session.session_id) is session:
                self._bytes -= self._sessions.pop(session.session_id).size

    def invalidate_channel(self, channel_id):
        """A visibility, trash or video-type decision on one channel.

        Results that included the channel expire; a build already reading is
        fenced by the shared revision and must be requested again.
        """
        with self._lock:
            self._revision += 1
            for key, entry in list(self._sessions.items()):
                if channel_id in entry.channel_ids:
                    self._bytes -= self._sessions.pop(key).size

    def invalidate_all(self):
        """Runtime suspension or cache clear: every frozen result expires."""
        with self._lock:
            self._revision += 1
            self._sessions.clear()
            self._bytes = 0
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def usage(self):
        with self._lock:
            self._purge_locked()
            return {"sessions": len(self._sessions), "bytes": self._bytes}
