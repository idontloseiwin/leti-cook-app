"""Process-local read caches with source deadlines and explicit invalidation."""
from catalogue_browse import CatalogueBrowseCache
from fastapi import HTTPException
from hot_storage import (
    HotSourceReadExpired,
    capture_read_lifetime,
    utc_now as hot_read_now,
    register_read_deadline,
)
from hot_read_cache import TransientReadCache
import copy
import threading
import time
from typing import Callable
from dataclasses import dataclass, field
import ui_read_trace


_CACHE_WAIT_SECONDS = 30.0


@dataclass
class _ReadFlight:
    revision: int
    completed: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    invalidated: bool = False


def _copy_flight_error(error):
    # Each waiting request owns its traceback. Do not propagate another
    # request's cancellation or retain a shared mutable exception traceback.
    if isinstance(error, Exception):
        try:
            cloned = copy.deepcopy(error)
            if cloned is not error:
                return cloned.with_traceback(None)
        except Exception:
            pass
    return HTTPException(status_code=503, detail="Concurrent read failed; retry.")


_OBSERVED_CACHE_KEYS = {
    "system-summary": "system_summary",
    "system-run-history": "run_history",
    "explorer-home-summary": "home_summary",
    "sidebar-data": "sidebar_data",
    "sidebar-user-data": "sidebar_user_data",
    "system-pipeline-health": "pipeline_health",
    "system-queue-health": "queue_health",
    "api-quota": "quota_summary",
    "video-type-review-summary": "video_type_reviews",
}


def _observed_cache_operation(key):
    # Keys may contain channel IDs or cursors. Emit fixed categories only.
    if type(key) is not str:
        return None
    if key.startswith("explorer-recent-videos:"):
        return "recent_videos"
    return _OBSERVED_CACHE_KEYS.get(key)

@dataclass(frozen=True)
class ReadCache:
    cached_read: Callable
    catalogue: CatalogueBrowseCache
    clear_frontend_read_cache: Callable
    clear_read_cache: Callable

def create_read_cache():
    READ_CACHE_LOCK = threading.RLock()

    READ_CACHE = TransientReadCache(READ_CACHE_LOCK)

    READ_CACHE_REVISION = 0
    READ_FLIGHTS = {}
    LOADER_CONTEXT = threading.local()

    CATALOGUE_BROWSE_CACHE = CatalogueBrowseCache()

    def cached_read(key, ttl_seconds, loader):
        operation = _observed_cache_operation(key)
        if operation is None or not ui_read_trace.is_active():
            return read_cached_value(key, ttl_seconds, loader, None)
        with ui_read_trace.scope(operation, phase="cache"):
            return read_cached_value(key, ttl_seconds, loader, operation)

    def read_cached_value(key, ttl_seconds, loader, operation):
        # Concurrent top-level reads share one loader, not one mutable response.
        # A nested loader must never wait for a different loader: A -> B and
        # B -> A would otherwise deadlock. Its ordinary independent read keeps
        # the same source checks and invalidation fence.
        flight = None
        wait_deadline = None
        while ttl_seconds > 0:
            with READ_CACHE_LOCK:
                cached = current_cached_entry(key)
                if cached is not None:
                    return consume_cached_entry(cached, operation)
                pending = READ_FLIGHTS.get(key)
                if pending is None:
                    flight = _ReadFlight(READ_CACHE_REVISION)
                    READ_FLIGHTS[key] = flight
                    break
                if getattr(LOADER_CONTEXT, "depth", 0):
                    break
            if wait_deadline is None:
                wait_deadline = time.monotonic() + _CACHE_WAIT_SECONDS
            remaining = max(0, wait_deadline - time.monotonic())
            if operation:
                with ui_read_trace.scope(operation, phase="cache_wait") as observation:
                    completed = pending.completed.wait(remaining)
                    if not completed:
                        observation.mark(status="error", reason="cache_wait_timeout")
                    elif not pending.invalidated and pending.error is not None:
                        raise _copy_flight_error(pending.error)
            else:
                completed = pending.completed.wait(remaining)
                if completed and not pending.invalidated and pending.error is not None:
                    raise _copy_flight_error(pending.error)
            if not completed:
                raise HTTPException(status_code=503, detail="Concurrent read timed out; retry.")
            # A successful loader populated the ordinary cache. Recheck its
            # TTL/source deadline; invalidation wakes us to claim a fresh read.
        depth = getattr(LOADER_CONTEXT, "depth", 0)
        LOADER_CONTEXT.depth = depth + 1
        try:
            return load_cached_value(key, ttl_seconds, loader, operation, flight)
        except BaseException as error:
            if flight is not None:
                with READ_CACHE_LOCK:
                    flight.error = error
            raise
        finally:
            LOADER_CONTEXT.depth = depth
            if flight is not None:
                with READ_CACHE_LOCK:
                    if READ_FLIGHTS.get(key) is flight:
                        READ_FLIGHTS.pop(key)
                    flight.completed.set()

    def current_cached_entry(key):
        cached = READ_CACHE.get(key)
        if cached and cached["expires_at"] > time.monotonic():
            deadline = cached.get("source_expires_at")
            if deadline is None or deadline > hot_read_now():
                return cached
        READ_CACHE.pop(key, None)
        return None

    def consume_cached_entry(cached, operation):
        deadline = cached.get("source_expires_at")
        if deadline is not None:
            register_read_deadline(deadline)
        if operation:
            ui_read_trace.event(operation, phase="cache", status="hit")
        ui_read_trace.replay_unavailability(cached.get("unavailable_components", ()))
        return copy.deepcopy(cached["value"])

    def load_cached_value(key, ttl_seconds, loader, operation, flight):
        for attempt in range(2):
            with READ_CACHE_LOCK:
                revision = READ_CACHE_REVISION
                cached = current_cached_entry(key)
                if cached is not None:
                    return consume_cached_entry(cached, operation)
            if operation:
                ui_read_trace.event(operation, phase="cache", status="miss")

            # Sources and aggregates share their earliest real observation deadline.
            # Work remains outside the cache lock; never republish invalidated work.
            try:
                with capture_read_lifetime() as lifetime, ui_read_trace.capture_unavailability() as availability:
                    if operation:
                        with ui_read_trace.scope(operation, phase="loader"):
                            value = loader()
                    else:
                        value = loader()
            except HotSourceReadExpired:
                if operation:
                    ui_read_trace.event(operation, phase="cache", status="retry" if attempt == 0 else "error",
                                        reason="source_expired")
                if attempt == 0:
                    continue
                raise HTTPException(status_code=503, detail="Source data changed during this read; retry.")
            deadline = lifetime.expires_at
            if deadline is not None and deadline <= hot_read_now():
                if operation:
                    ui_read_trace.event(operation, phase="cache", status="retry" if attempt == 0 else "error",
                                        reason="source_expired")
                if attempt == 0:
                    continue
                raise HTTPException(status_code=503, detail="Source data expired during this read; retry.")
            with READ_CACHE_LOCK:
                if revision == READ_CACHE_REVISION and (flight is None or not flight.invalidated):
                    remaining = ttl_seconds if deadline is None else min(
                        ttl_seconds, max(0, (deadline - hot_read_now()).total_seconds()),
                    )
                    if remaining > 0:
                        READ_CACHE[key] = {
                            "expires_at": time.monotonic() + remaining,
                            "source_expires_at": deadline,
                            "value": copy.deepcopy(value),
                            "unavailable_components": availability.codes,
                        }
                elif operation:
                    ui_read_trace.event(operation, phase="cache", status="invalidated",
                                        reason="cache_invalidated")
            if deadline is not None:
                register_read_deadline(deadline)
            ui_read_trace.replay_unavailability(availability.codes)
            return value


    def clear_read_cache(*prefixes):
        nonlocal READ_CACHE_REVISION
        with READ_CACHE_LOCK:
            READ_CACHE_REVISION += 1
            # Revision fencing already applies globally, including work for
            # unrelated keys. Wake old waiters now, without waiting for a stale
            # loader to finish; it cannot remove or publish over a new flight.
            for flight in READ_FLIGHTS.values():
                flight.invalidated = True
                flight.completed.set()
            READ_FLIGHTS.clear()
            if not prefixes:
                READ_CACHE.clear()
                return
            for key in list(READ_CACHE):
                if any(str(key).startswith(prefix) for prefix in prefixes):
                    READ_CACHE.pop(key, None)


    def clear_frontend_read_cache(channel_id=None):
        prefixes = [
            "sidebar-data",
            "sidebar-user-data",
            "explorer-home-summary",
            "explorer-recent-videos:",
            "system-summary",
            "system-run-history",
            "video-type-review-summary",
            "system-pipeline-health",
            "system-queue-health",
            "api-quota",
        ]
        clear_read_cache(*prefixes)


    catalogue = CATALOGUE_BROWSE_CACHE
    return ReadCache(cached_read, catalogue, clear_frontend_read_cache, clear_read_cache)
