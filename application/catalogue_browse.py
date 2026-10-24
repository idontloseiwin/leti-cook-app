"""Short-lived, bounded browsing generations; never durable saved work.

The caller checks live visibility and feature gates before every access. This
cache is process-local, like the existing private single-process API runtime.
Restart/eviction/expiry is explicit, never a silent replacement of later pages.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from source_expiry import source_expiry, minimum_expiry, rows_expiry
from typing import Any, Callable

from hot_storage import capture_read_lifetime, register_read_deadline, HOT_DATA_WINDOW, utc_instant, source_is_current
from hot_storage_fields import channel_source_fields

from channel_explorer import _normalized_query, _timestamp, RAW_DATA_MAX_AGE, build_channel_explorer
from channel_video_growth import _growth_query
from catalogue_static import static_details
from video_lifetime_baseline import build_video_lifetime_baseline


PAGE_SIZE = 50
SESSION_TTL_SECONDS = 600
# A browse session of 20,000 videos encodes to about 24 MiB in the
# capacity fixture; the cache keeps room for two such sessions.
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_SESSIONS = 8
CURSOR_PATTERN = re.compile(r"([a-f0-9]{32})\.([0-9]{1,12})\.([a-f0-9]{24})")


class CatalogueBrowseError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False, default=_date_json).encode("utf-8")


def _date_json(value: Any) -> str:
    if not isinstance(value, datetime):
        raise TypeError("Unsupported catalogue value")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_browse_query(source: str, values: dict[str, Any]) -> dict[str, Any]:
    defaults = dict(q="", category_id="", published_from="", published_before="",
                    min_views=None, max_views=None, min_duration_seconds=None,
                    max_duration_seconds=None, sort_by="published_at", sort_order="desc",
                    limit=PAGE_SIZE, offset=0, catalogue_filters='{"version":1}')
    defaults.update(values)
    defaults["catalogue_filters"] = defaults["catalogue_filters"] or '{"version":1}'
    if defaults["limit"] != PAGE_SIZE or defaults["offset"] != 0:
        raise CatalogueBrowseError("catalogue_browse_requires_cursor_pagination", 422)
    return (_growth_query if source == "growth" else _normalized_query)(**defaults)


def _video_type(value: str) -> str:
    if not isinstance(value, str) or value not in ("long", "short", "stream"):
        raise CatalogueBrowseError("catalogue_browse_unsupported_video_type", 422)
    return value


def _fingerprint(query: dict[str, Any], *, conditions_only: bool = False,
                 video_type: str = "long") -> str:
    ignored = {"limit", "offset"}
    if conditions_only:
        ignored.update(("sort_by", "sort_order"))
    return hashlib.sha256(_json({"video_type": _video_type(video_type),
        "query": {key: value for key, value in query.items() if key not in ignored}})).hexdigest()


class RecordingCatalogueRepository:
    """Capture current rows and opt-in growth for same-generation facets."""

    def __init__(self, repository: Any, *, capture_growth: bool = False,
                 now: datetime | None = None, source_valid_until: datetime | None = None,
                 video_type: str = "long"):
        self.video_type = _video_type(video_type)
        self.repository = repository
        self.capture_growth = capture_growth
        self.now = now
        self.source_valid_until = source_valid_until
        self.snapshot: dict[str, Any] = {"channel": None, "inventory": [], "videos": [],
                                         "video_type": self.video_type}

    def _admitted(self, row, fields):
        return self.source_valid_until is None or all(source_is_current(
            row.get(field), now=self.now, valid_until=self.source_valid_until,
        ) for field in fields)

    def load_channel(self, channel_id: str, *, columns: str):
        row = self.repository.load_channel(channel_id, columns=columns)
        self.snapshot["channel"] = (channel_source_fields(
            row, now=self.now, valid_until=self.source_valid_until,
        ) if self.source_valid_until is not None else dict(row)) if row else None
        if row and self.source_valid_until is not None and not (
            row.get("channel_metadata_status") == "ok"
            and self._admitted(row, ("channel_metadata_checked_at",))
        ):
            # No profile values were admitted; its discarded clock must
            # not shorten an otherwise valid frozen catalogue generation.
            self.snapshot["channel"]["channel_metadata_checked_at"] = None
        if row and self.source_valid_until is not None and not self._admitted(row, ("country_collected_at",)):
            self.snapshot["channel"]["country_collected_at"] = None
        return row

    def _require_type(self, video_type):
        if _video_type(video_type) != self.video_type:
            raise CatalogueBrowseError("catalogue_browse_scope_mismatch")

    def iter_long_inventory_pages(self, channel_id: str):
        return self.iter_inventory_pages(channel_id, video_type="long")

    def iter_inventory_pages(self, channel_id: str, *, video_type: str = "long"):
        self._require_type(video_type)
        # Preserve the explicitly long alias for existing repository adapters.
        if video_type == "long" and hasattr(self.repository, "iter_long_inventory_pages"):
            iterator = iter(self.repository.iter_long_inventory_pages(channel_id))
        else:
            iterator = iter(self.repository.iter_inventory_pages(channel_id, video_type=video_type))
        try:
            for page in iterator:
                self.snapshot["inventory"].extend(dict(row) for row in page
                    if self._admitted(row, ("source_checked_at",)))
                yield page
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    def load_inventory_state(self, channel_id, *, video_type):
        self._require_type(video_type)
        row = self.repository.load_inventory_state(channel_id, video_type=video_type)
        self.snapshot["inventory_state"] = dict(row) if row else None
        return row

    def load_type_resolutions(self, video_ids, *, video_type):
        self._require_type(video_type)
        rows = self.repository.load_type_resolutions(video_ids, video_type=video_type)
        self.snapshot.setdefault("type_resolutions", []).extend(dict(row) for row in rows)
        return rows

    def load_videos_by_ids(self, video_ids, *, columns):
        rows = self.repository.load_videos_by_ids(video_ids, columns=columns)
        self.snapshot["videos"].extend(dict(row) for row in rows
            if self._admitted(row, ("checked_at", "metadata_checked_at", "availability_checked_at"))
            and (self.source_valid_until is None or row.get("availability_status") == "public"))
        return rows

    def load_video_view_growth(self, channel_id, video_ids, *, as_of, recorded_from=None, video_type="long"):
        self._require_type(video_type)
        rows = self.repository.load_video_view_growth(channel_id, video_ids, as_of=as_of,
            **({"video_type": video_type} if video_type != "long" else {}),
            **({"recorded_from": recorded_from} if recorded_from is not None else {}))
        if self.capture_growth:
            self.snapshot.setdefault("growth", []).extend(dict(row) for row in rows)
        return rows


class FrozenCatalogueRepository:
    def __init__(self, snapshot: dict[str, Any]):
        self.video_type = _video_type(snapshot.get("video_type", "long"))
        self.snapshot = snapshot
        self.videos = {row["video_id"]: row for row in snapshot["videos"]}

    def load_channel(self, channel_id, *, columns):
        row = self.snapshot["channel"]
        return dict(row) if row and row.get("channel_id") == channel_id else None

    def _require_type(self, video_type):
        if _video_type(video_type) != self.video_type:
            raise CatalogueBrowseError("catalogue_browse_scope_mismatch")

    def iter_long_inventory_pages(self, channel_id):
        return self.iter_inventory_pages(channel_id, video_type="long")

    def iter_inventory_pages(self, channel_id, *, video_type="long"):
        self._require_type(video_type)
        if self.load_channel(channel_id, columns="channel_id") is None:
            return
        rows = self.snapshot["inventory"]
        for start in range(0, len(rows), 500):
            yield [dict(row) for row in rows[start:start + 500]]

    def load_inventory_state(self, channel_id, *, video_type):
        self._require_type(video_type)
        if self.load_channel(channel_id, columns="channel_id") is None:
            return None
        row = self.snapshot.get("inventory_state")
        return dict(row) if row else None

    def load_type_resolutions(self, video_ids, *, video_type):
        self._require_type(video_type)
        requested = set(video_ids)
        return [dict(row) for row in self.snapshot.get("type_resolutions", [])
                if row.get("video_id") in requested]

    def load_videos_by_ids(self, video_ids, *, columns):
        return [dict(self.videos[key]) for key in video_ids if key in self.videos]

    def load_frozen_video_growth(self, video_ids):
        # Only captured observations; this object cannot call a live repository.
        evidence = {row["video_id"]: row for row in self.snapshot.get("growth", [])}
        if any(video_id not in evidence for video_id in video_ids):
            raise CatalogueBrowseError("catalogue_growth_evidence_incomplete", 503)
        return [dict(evidence[video_id]) for video_id in video_ids]


@dataclass(frozen=True)
class _Session:
    session_id: str
    channel_id: str
    source: str
    video_type: str
    query_key: str
    conditions_key: str
    created_at: datetime
    expires_at: datetime
    deadline: float
    metadata: bytes
    rows: tuple[bytes, ...]
    snapshot: bytes
    size: int


class CatalogueBrowseCache:
    def __init__(self, *, ttl_seconds=SESSION_TTL_SECONDS, max_entries=MAX_SESSIONS,
                 max_bytes=MAX_CACHE_BYTES, max_session_bytes=MAX_SESSION_BYTES,
                 max_builds=2, monotonic=time.monotonic,
                 clock=lambda: datetime.now(timezone.utc), auto_prune=True):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_session_bytes = max_session_bytes
        self.monotonic = monotonic
        self.clock = clock
        self.auto_prune = auto_prune
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self._builds = threading.BoundedSemaphore(max_builds)
        self._secret = secrets.token_bytes(32)
        self._timer: threading.Timer | None = None
        self._invalidation_revision = 0

    def invalidate_all(self):
        """Drop source copies and fence in-flight builds after runtime suspension."""
        with self._lock:
            self._invalidation_revision += 1
            self._sessions.clear()
            self._bytes = 0
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def invalidate_channel(self, channel_id: str):
        """Retire this channel's frozen rows and fence builds already in flight.

        A bounded global epoch avoids an unbounded map of edited channels. An
        overlapping build may need a retry; other retained channels stay valid.
        """
        with self._lock:
            self._invalidation_revision += 1
            for key, entry in list(self._sessions.items()):
                if entry.channel_id == channel_id:
                    self._bytes -= self._sessions.pop(key).size

    @contextmanager
    def build_slot(self):
        if not self._builds.acquire(blocking=False):
            raise CatalogueBrowseError("catalogue_browse_busy", 503)
        try:
            yield
        finally:
            self._builds.release()

    def _purge_locked(self):
        now, wall_now = self.monotonic(), self.clock()
        for key, entry in list(self._sessions.items()):
            if now >= entry.deadline or wall_now >= entry.expires_at:
                self._bytes -= self._sessions.pop(key).size

    def _schedule_locked(self):
        if not self.auto_prune or self._timer is not None or not self._sessions:
            return
        delay = max(0.01, min(60, min(entry.deadline for entry in self._sessions.values()) - self.monotonic()))
        self._timer = threading.Timer(delay, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self):
        with self._lock:
            self._timer = None
            self._purge_locked()
            self._schedule_locked()

    def _cursor(self, entry: _Session, offset: int | None) -> str | None:
        if offset is None or offset < 0 or offset >= len(entry.rows):
            return None
        value = f"{entry.session_id}.{offset}"
        signature = hmac.new(self._secret, value.encode(), hashlib.sha256).hexdigest()[:24]
        return f"{value}.{signature}"

    def _decode_cursor(self, cursor: str) -> tuple[str, int]:
        match = CURSOR_PATTERN.fullmatch(cursor)
        if not match:
            raise CatalogueBrowseError("catalogue_browse_invalid_cursor")
        session_id, offset, signature = match.groups()
        expected = hmac.new(self._secret, f"{session_id}.{offset}".encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(signature, expected):
            # A cursor from a restarted/different worker cannot be resumed.
            raise CatalogueBrowseError("catalogue_browse_session_expired", 410)
        return session_id, int(offset)

    def _lookup(self, session_id: str, channel_id: str, *, video_type: str = "long") -> _Session:
        video_type = _video_type(video_type)
        with self._lock:
            self._purge_locked()
            entry = self._sessions.get(session_id)
            if entry is None:
                raise CatalogueBrowseError("catalogue_browse_session_expired", 410)
            if entry.channel_id != channel_id or entry.video_type != video_type:
                raise CatalogueBrowseError("catalogue_browse_scope_mismatch")
            self._sessions.move_to_end(session_id)
            return entry

    def _page(self, entry: _Session, offset: int, *, anchor_video_id=None) -> dict[str, Any]:
        with self._lock:
            if entry.session_id not in self._sessions:
                raise CatalogueBrowseError("catalogue_browse_session_expired", 410)
        if self.monotonic() >= entry.deadline or self.clock() >= entry.expires_at:
            raise CatalogueBrowseError("catalogue_browse_session_expired", 410)
        if offset < 0 or offset % PAGE_SIZE or (offset >= len(entry.rows) and offset != 0):
            raise CatalogueBrowseError("catalogue_browse_invalid_cursor")
        result = json.loads(entry.metadata)
        result["videos"] = [json.loads(row) for row in entry.rows[offset:offset + PAGE_SIZE]]
        result["filters"].update(limit=PAGE_SIZE, offset=offset)
        result["pagination"].update(limit=PAGE_SIZE, offset=offset,
                                    returned_count=len(result["videos"]), matched_count=len(entry.rows),
                                    has_more_in_scan=offset + PAGE_SIZE < len(entry.rows))
        result["browse"] = {
            "contract_version": 1, "session_id": entry.session_id, "video_type": entry.video_type,
            "created_at": _date_json(entry.created_at), "expires_at": _date_json(entry.expires_at),
            "next_cursor": self._cursor(entry, offset + PAGE_SIZE),
            "previous_cursor": self._cursor(entry, offset - PAGE_SIZE),
            "start_index": offset, "total_count": len(entry.rows),
            "anchor_video_id": anchor_video_id, "consistency": "frozen_assembled_rows",
        }
        # Decoding a page must not release an entry that expired or was invalidated.
        self._lookup(entry.session_id, entry.channel_id, video_type=entry.video_type)
        register_read_deadline(entry.expires_at)
        return result

    def begin(self, channel_id: str, source: str, query: dict[str, Any],
              build: Callable, *, anchor_video_id: str | None = None,
              video_type: str = "long") -> dict[str, Any]:
        video_type = _video_type(video_type)
        with self.build_slot():
            with self._lock:
                generation = self._invalidation_revision
            created_at, started = self.clock(), self.monotonic()
            with capture_read_lifetime() as lifetime:
                payload, snapshot = build(created_at)
            if (payload.get("video_type", "long") != video_type
                    or payload.get("coverage", {}).get("video_type", video_type) != video_type
                    or snapshot.get("video_type", "long") != video_type):
                raise CatalogueBrowseError("catalogue_browse_scope_mismatch")
            if payload["coverage"].get("scan_mode") != "full_catalogue" or payload["coverage"].get("truncated"):
                raise CatalogueBrowseError("catalogue_browse_incomplete_scope", 503)
            expires_at = created_at + timedelta(seconds=self.ttl_seconds)
            if lifetime.expires_at is not None:
                expires_at = min(expires_at, lifetime.expires_at)
            # Even immutable pages cannot extend any live source's display clock.
            for row in [snapshot.get("channel") or {}, *snapshot.get("videos", [])]:
                for field in ("checked_at", "metadata_checked_at", "availability_checked_at", "channel_metadata_checked_at", "country_collected_at"):
                    stamp = _timestamp(row.get(field))
                    if stamp is not None and stamp <= created_at <= stamp + RAW_DATA_MAX_AGE:
                        expires_at = min(expires_at, stamp + RAW_DATA_MAX_AGE)
            for row in snapshot.get("inventory", []):
                value = row.get("source_checked_at")
                if value is None:
                    continue
                try:
                    stamp = utc_instant(value)
                except (ValueError, TypeError) as error:
                    raise CatalogueBrowseError("catalogue_browse_source_expired", 503) from error
                if not created_at - HOT_DATA_WINDOW <= stamp <= created_at:
                    raise CatalogueBrowseError("catalogue_browse_source_expired", 503)
                expires_at = min(expires_at, stamp + HOT_DATA_WINDOW)
            # Typed admission depends on state and resolution witnesses too.
            # Preserve their actual expiry, including copies served by frozen repositories.
            for proof in [snapshot.get("inventory_state") or {}, *snapshot.get("type_resolutions", [])]:
                try:
                    value = source_expiry(proof, "source_checked_at", "inventory_completed_at")
                except (ValueError, TypeError, OverflowError) as error:
                    raise CatalogueBrowseError("catalogue_browse_source_expired", 503) from error
                if value is None:
                    continue
                try:
                    expiry = utc_instant(value)
                except (ValueError, TypeError, OverflowError) as error:
                    raise CatalogueBrowseError("catalogue_browse_source_expired", 503) from error
                if expiry <= created_at:
                    raise CatalogueBrowseError("catalogue_browse_source_expired", 503)
                expires_at = min(expires_at, expiry)
            # Growth is an observation too: a short cache TTL cannot preserve
            # a boundary point after it leaves the hot window. Check both the
            # visible response and captured evidence used by frozen facets.
            observations = [*snapshot.get("growth", []), *(
                row.get("view_growth") or {} for row in payload["videos"]
            )]
            for observation in observations:
                for field in ("observed_start_at", "observed_end_at"):
                    value = observation.get(field)
                    if value is None:
                        continue
                    try:
                        stamp = utc_instant(value)
                    except (ValueError, TypeError) as error:
                        raise CatalogueBrowseError("catalogue_browse_source_expired", 503) from error
                    if not created_at - HOT_DATA_WINDOW <= stamp <= created_at:
                        raise CatalogueBrowseError("catalogue_browse_source_expired", 503)
                    expires_at = min(expires_at, stamp + HOT_DATA_WINDOW)
            if self.clock() >= expires_at:
                raise CatalogueBrowseError("catalogue_browse_source_expired", 503)
            values = payload["videos"]
            ids = [str(row["video_id"]) for row in values]
            if len(ids) != len(set(ids)):
                raise CatalogueBrowseError("catalogue_browse_duplicate_video", 503)
            metadata = _json({key: value for key, value in payload.items() if key != "videos"})
            captured = _json(snapshot)
            size = len(metadata) + len(captured)
            encoded_rows = []
            for row in values:
                encoded = _json(row)
                size += len(encoded) + 64  # Account for retained bytes/tuple overhead too.
                if size > min(self.max_session_bytes, self.max_bytes):
                    raise CatalogueBrowseError("catalogue_browse_capacity_exceeded", 503)
                encoded_rows.append(encoded)
            if size > min(self.max_session_bytes, self.max_bytes):
                raise CatalogueBrowseError("catalogue_browse_capacity_exceeded", 503)
            entry = _Session(secrets.token_hex(16), channel_id, source, video_type, _fingerprint(query, video_type=video_type),
                             _fingerprint(query, conditions_only=True, video_type=video_type), created_at, expires_at,
                             started + (expires_at - created_at).total_seconds(),
                             metadata, tuple(encoded_rows), captured, size)
            with self._lock:
                if generation != self._invalidation_revision:
                    raise CatalogueBrowseError("catalogue_browse_session_expired", 410)
                self._purge_locked()
                if self.monotonic() >= entry.deadline or self.clock() >= entry.expires_at:
                    raise CatalogueBrowseError("catalogue_browse_source_expired", 503)
                while self._sessions and (len(self._sessions) >= self.max_entries or self._bytes + size > self.max_bytes):
                    _, removed = self._sessions.popitem(last=False)
                    self._bytes -= removed.size
                self._sessions[entry.session_id] = entry
                self._bytes += size
                self._schedule_locked()
            offset = (ids.index(anchor_video_id) // PAGE_SIZE) * PAGE_SIZE if anchor_video_id in ids else 0
            return self._page(entry, offset, anchor_video_id=anchor_video_id if anchor_video_id in ids else None)

    def resume(self, channel_id: str, source: str, query: dict[str, Any], cursor: str, *, video_type: str = "long") -> dict[str, Any]:
        session_id, offset = self._decode_cursor(cursor)
        entry = self._lookup(session_id, channel_id, video_type=video_type)
        if entry.source != source or entry.query_key != _fingerprint(query, video_type=video_type):
            raise CatalogueBrowseError("catalogue_browse_scope_mismatch")
        return self._page(entry, offset)

    def facet_source(self, session_id: str, channel_id: str, query: dict[str, Any], *, growth_enabled: bool, require_growth: bool = False, video_type: str = "long"):
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise CatalogueBrowseError("catalogue_browse_invalid_session")
        entry = self._lookup(session_id, channel_id, video_type=video_type)
        if entry.source == "growth" and not growth_enabled:
            raise CatalogueBrowseError("catalogue_browse_unavailable", 404)
        if require_growth and entry.source != "growth":
            raise CatalogueBrowseError("catalogue_growth_generation_required", 422)
        if entry.conditions_key != _fingerprint(query, conditions_only=True, video_type=video_type):
            raise CatalogueBrowseError("catalogue_browse_scope_mismatch")
        return FrozenCatalogueRepository(json.loads(entry.snapshot)), entry.created_at

    def _inspected_video(self, session_id: str, channel_id: str, video_id: str, *, growth_enabled: bool, require_growth: bool = False, video_type: str = "long"):
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise CatalogueBrowseError("catalogue_browse_invalid_session")
        entry = self._lookup(session_id, channel_id, video_type=video_type)
        if entry.source == "growth" and not growth_enabled:
            raise CatalogueBrowseError("catalogue_browse_unavailable", 404)
        if require_growth and entry.source != "growth":
            raise CatalogueBrowseError("catalogue_growth_generation_required", 422)
        # Only a row admitted to this exact visible catalogue can be inspected.
        # The captured inventory can include withheld or nonmatching rows.
        if not any(json.loads(row).get("video_id") == video_id for row in entry.rows):
            raise CatalogueBrowseError("catalogue_video_not_available", 404)
        snapshot = json.loads(entry.snapshot)
        matches = [row for row in snapshot["videos"] if row.get("video_id") == video_id]
        if len(matches) != 1:
            raise CatalogueBrowseError("catalogue_static_source_unavailable", 503)
        video = dict(matches[0])
        membership = [row for row in snapshot["inventory"] if row.get("video_id") == video_id]
        video["source_expires_at"] = minimum_expiry(
            source_expiry(video, "checked_at", "metadata_checked_at", "availability_checked_at"),
            rows_expiry(membership, "source_checked_at"),
            source_expiry(snapshot.get("inventory_state"), "source_checked_at", "inventory_completed_at"),
            rows_expiry([row for row in snapshot.get("type_resolutions", [])
                         if row.get("video_id") == video_id]))
        return entry, video

    def video_history_context(self, session_id: str, channel_id: str, video_id: str, *, growth_enabled: bool, video_type: str = "long"):
        if _video_type(video_type) != "long":
            raise CatalogueBrowseError("catalogue_browse_unavailable", 404)
        entry, video = self._inspected_video(session_id, channel_id, video_id,
                                             growth_enabled=growth_enabled, require_growth=True, video_type=video_type)
        return {"channel_id": channel_id, "video_id": video_id, "browse_session": session_id,
                "browse_created_at": _date_json(entry.created_at), "expires_at": _date_json(entry.expires_at),
                "video": video}

    def validate_inspector_context(self, session_id: str, channel_id: str, *, growth_enabled: bool, video_type: str = "long"):
        entry = self._lookup(session_id, channel_id, video_type=video_type)
        if entry.source == "growth" and not growth_enabled:
            raise CatalogueBrowseError("catalogue_browse_unavailable", 404)

    def video_launch_context(self, session_id: str, channel_id: str, video_id: str, *, growth_enabled: bool, read_budget=None, video_type: str = "long"):
        if _video_type(video_type) != "long":
            raise CatalogueBrowseError("catalogue_browse_unavailable", 404)
        entry, _video = self._inspected_video(
            session_id, channel_id, video_id, growth_enabled=growth_enabled, video_type=video_type,
        )
        # Canonical source admission over every captured row, not the displayed
        # page or filter result. This does not open a live DB path.
        catalogue = build_channel_explorer(
            FrozenCatalogueRepository(json.loads(entry.snapshot)), channel_id,
            full_scope=True, all_rows=True, catalogue_filters='{"version":1}',
            now=entry.created_at, read_budget=read_budget,
        )
        self._lookup(session_id, channel_id, video_type=video_type)
        return {"channel_id": channel_id, "video_id": video_id, "browse_session": session_id,
                "browse_created_at": _date_json(entry.created_at), "expires_at": _date_json(entry.expires_at),
                "catalogue": catalogue}

    def video_lifetime_baseline(self, session_id: str, channel_id: str, video_id: str, *, growth_enabled: bool, video_type: str = "long"):
        if _video_type(video_type) != "long":
            raise CatalogueBrowseError("catalogue_browse_unavailable", 404)
        entry, _video = self._inspected_video(
            session_id, channel_id, video_id, growth_enabled=growth_enabled, video_type=video_type,
        )
        # Captured source precedes browsing filters and pagination. Canonical
        # Baseline admission is reapplied; withheld rows are not auto-admitted.
        repository = FrozenCatalogueRepository(json.loads(entry.snapshot))
        result = {
            "contract_version": 1, "channel_id": channel_id, "video_id": video_id,
            "browse_session": session_id, "generated_at": _date_json(entry.created_at),
            "expires_at": _date_json(entry.expires_at),
            **build_video_lifetime_baseline(repository, channel_id, now=entry.created_at),
        }
        self._lookup(session_id, channel_id, video_type=video_type)
        return result

    def validate_video_history_context(self, session_id: str, channel_id: str, *, video_type: str = "long"):
        if _video_type(video_type) != "long":
            raise CatalogueBrowseError("catalogue_browse_unavailable", 404)
        # The caller rechecks live visibility and the gate too. Never release
        # snapshots after expiry, invalidation or eviction during the DB read.
        self._lookup(session_id, channel_id, video_type=video_type)

    def video_static_details(self, session_id: str, channel_id: str, video_id: str, *, growth_enabled: bool, video_type: str = "long"):
        entry, video = self._inspected_video(session_id, channel_id, video_id, growth_enabled=growth_enabled, video_type=video_type)
        result = {
            "contract_version": 1, "channel_id": channel_id, "video_id": video_id,
            "browse_session": session_id, "generated_at": _date_json(entry.created_at),
            "expires_at": _date_json(entry.expires_at), **static_details(video),
            "source_expires_at": source_expiry(video),
        }
        # Stored offsets must use the same UTC wire format as session clocks.
        metadata_clock = _timestamp(result["raw"]["metadata_checked_at"])
        result["raw"]["metadata_checked_at"] = _date_json(metadata_clock) if metadata_clock is not None else None
        # Expiry/invalidation during projection must not extend source access.
        self._lookup(session_id, channel_id, video_type=video_type)
        return result
