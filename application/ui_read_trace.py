"""Bounded observations of selected UI reads; never a request or database owner.

Only fixed operations and codes enter this context. HTTP traces are generated
locally, not accepted from headers. Durations are nested observations, not
additive SQL execution time. The response body is never inspected or buffered.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import math
import re
import threading
import time
import uuid

import telemetry


_clock = time.monotonic
MAX_CHILD_EVENTS = 128
TRACE_HEADER = b"x-leti-read-trace"
HTTP_ROUTES = {
    "/research/queries/execute": "research_query",
    "/system-summary": "system_summary",
    "/system/run-history": "run_history",
    "/explorer/home-summary": "home_summary",
    "/explorer/recent-videos": "recent_videos",
    "/sidebar-data": "sidebar_data",
    "/sidebar-user-data": "sidebar_user_data",
    "/system/pipeline-health": "pipeline_health",
}
COMPONENTS = frozenset({
    *HTTP_ROUTES.values(), "recent_sync_runs", "queue_health", "quota_summary",
    "video_type_reviews", "inventory_counts", "latest_completed_sync",
    "latest_data_change", "visible_channels", "update_copy_status",
    "recent_inventory_scan", "recent_inventory_candidates",
    "recent_inventory_pages", "recent_videos_result", "source_envelope",
    "response_serialization", "request_guard",
})
READ_DATABASE_OPERATIONS = frozenset({
    "research_query_page", "research_query_scope",
    "frontend_count_inventory_by_type", "frontend_load_channel_rows",
    "frontend_load_visible_channel_user_work",
    "frontend_load_visible_channel_user_work_identity", "frontend_load_folder_rows",
    "frontend_load_recent_long_inventory_page", "frontend_load_videos_by_ids",
    "frontend_load_channel", "frontend_load_channel_inventory_page",
    "frontend_load_inventory_membership", "frontend_load_video_snapshots",
    "sync_run_list", "has_open_video_type_reviews", "get_video_collection_health",
    "youtube_quota_day_attempts", "system_health_channels",
    "operational_video_collection_health", "operational_typed_video_collection_health",
    "operational_quota_day", "operational_channel_coverage",
    "operational_channel_discovery_health", "operational_channel_sync_health",
    "channel_labels_list", "channel_labels_assignments",
})
_OPERATIONS = COMPONENTS | READ_DATABASE_OPERATIONS | {"database_read"}
_PHASES = frozenset({
    "component", "cache", "loader", "scan", "db_transaction", "db_pool_wait",
    "db_execution", "http", "cache_wait",
})
_STATUSES = frozenset({
    "started", "ok", "error", "unavailable", "hit", "miss", "retry",
    "invalidated", "cancelled", "degraded", "partial", "incomplete",
})
_REASONS = frozenset({
    "source_expired", "cache_invalidated", "component_unavailable",
    "unexpected_error", "database_error", "database_unavailable", "pool_timeout",
    "pool_queue_full", "pool_closed", "connection_failed", "cancelled",
    "http_error", "delivery_incomplete", "observations_truncated",
    "invalid_response_envelope", "cache_wait_timeout",
})
_SQLSTATE = re.compile(r"[A-Z0-9]{5}\Z")


@dataclass
class _Request:
    operation: str
    trace_id: str
    root_span_id: str
    started: float
    emitted: int = 0
    omitted: int = 0
    degraded: bool = False
    active: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class _Context:
    request: _Request
    span_id: str
    parent_span_id: str | None = None


_CURRENT: ContextVar[_Context | None] = ContextVar("ui_read_trace", default=None)
_AVAILABILITY: ContextVar[tuple] = ContextVar("ui_read_availability", default=())


class _AvailabilityCapture:
    """Private component codes for one cache-loader attempt, even without tracing."""
    def __init__(self):
        self._codes = set()
        self._lock = threading.Lock()
        self._token = None

    def __enter__(self):
        try:
            self._token = _AVAILABILITY.set((*_AVAILABILITY.get(), self))
        except Exception:
            pass
        return self

    def __exit__(self, *exception):
        if self._token is not None:
            try:
                _AVAILABILITY.reset(self._token)
            except Exception:
                pass
        # Do not propagate an abandoned attempt. Its cache owner propagates
        # codes only when the corresponding value passes its existing guards.
        return False

    @property
    def codes(self):
        try:
            with self._lock:
                return tuple(sorted(self._codes))
        except Exception:
            return ()

    def add(self, operation):
        try:
            if type(operation) is str and operation in COMPONENTS:
                with self._lock:
                    self._codes.add(operation)
        except Exception:
            pass


def capture_unavailability():
    return _AvailabilityCapture()


def _record_unavailability(operation):
    try:
        if type(operation) is not str or operation not in COMPONENTS:
            return
        captures = _AVAILABILITY.get()
        if captures:
            captures[-1].add(operation)
        elif is_active():
            _degrade()
    except Exception:
        pass


def replay_unavailability(codes):
    """Replay known returned component availability, never its former exception."""
    try:
        if type(codes) not in (list, tuple) or len(codes) > len(COMPONENTS):
            return
        for operation in codes:
            if type(operation) is str and operation in COMPONENTS:
                unavailable(operation)
    except Exception:
        pass


def is_active():
    """A disabled recorder or a completed HTTP response has no active scope."""
    try:
        current = _CURRENT.get()
        return current is not None and current.request.active
    except Exception:
        return False


def _error_facts(error):
    """Classify known objects without inspecting exception text or payload."""
    try:
        if isinstance(error, asyncio.CancelledError):
            return {"reason": "cancelled"}
        from hot_storage import HotSourceReadExpired
        if isinstance(error, HotSourceReadExpired):
            return {"reason": "source_expired"}
        from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
        if isinstance(error, (PoolClosed, PoolTimeout, TooManyRequests)):
            return {"reason": "pool_closed" if isinstance(error, PoolClosed) else (
                "pool_timeout" if isinstance(error, PoolTimeout) else "pool_queue_full"
            )}
        import psycopg
        if isinstance(error, psycopg.Error):
            facts = {"reason": "database_error"}
            if type(error.sqlstate) is str and _SQLSTATE.fullmatch(error.sqlstate):
                facts["error_code"] = error.sqlstate
            return facts
        # Lazy imports avoid a transport -> observer -> transport import cycle.
        from db.base.postgres import DatabaseError, DatabaseUnavailable
        if isinstance(error, DatabaseError):
            facts = {"reason": "database_error"}
            if isinstance(error, DatabaseUnavailable):
                reason = error.reason
                facts["reason"] = reason if reason in _REASONS else "database_unavailable"
            if type(error.code) is str and _SQLSTATE.fullmatch(error.code):
                facts["error_code"] = error.code
            return facts
    except Exception:
        pass
    return {"reason": "unexpected_error"}


def _degrade():
    try:
        current = _CURRENT.get()
        if current is not None:
            with current.request.lock:
                current.request.degraded = True
    except Exception:
        pass


def _emit(context, operation, *, phase, status, target="backend", reason=None,
          count=None, duration_ms=None, http_status=None, error_code=None,
          terminal=False, root=False):
    try:
        if (operation not in _OPERATIONS or phase not in _PHASES
                or status not in _STATUSES or target not in {"backend", "postgres"}
                or (reason is not None and reason not in _REASONS)):
            return
        request = context.request
        if not root:
            with request.lock:
                if not request.active:
                    return
                if request.emitted >= MAX_CHILD_EVENTS:
                    request.omitted += 1
                    return
                request.emitted += 1
        facts = {
            "trace_id": request.trace_id, "span_id": context.span_id,
            "phase": phase, "status": status,
            "direction": "response" if terminal else "internal",
        }
        if context.parent_span_id:
            facts["parent_span_id"] = context.parent_span_id
        if reason:
            facts["reason"] = reason
        if type(count) is int and 0 <= count <= 10**15:
            facts["count"] = count
        if (type(duration_ms) in (int, float) and math.isfinite(duration_ms)
                and 0 <= duration_ms <= 10**15):
            facts["duration_ms"] = duration_ms
        if type(http_status) is int and 100 <= http_status <= 599:
            facts["http_status"] = http_status
        if type(error_code) is str and _SQLSTATE.fullmatch(error_code):
            facts["error_code"] = error_code
        if root and terminal:
            # A count of omitted child observations, never missing domain work.
            facts["missing_count"] = request.omitted
        telemetry.emit_event(
            "ui_read", "ui" if root else "backend", target, operation, **facts,
        )
    except Exception:
        pass


def event(operation, *, phase="component", status="ok", reason=None, count=None,
          duration_ms=None, target="backend"):
    try:
        context = _CURRENT.get()
        if context is not None and context.request.active:
            standalone = _Context(context.request, uuid.uuid4().hex, context.span_id)
            _emit(standalone, operation, phase=phase, status=status, reason=reason,
                  count=count, duration_ms=duration_ms, target=target)
    except Exception:
        pass


def failure(operation, error):
    """Record a caught read failure; preserve the caller's fail-soft response."""
    try:
        _record_unavailability(operation)
        if not is_active():
            return
        context = _CURRENT.get()
        standalone = _Context(context.request, uuid.uuid4().hex, context.span_id)
        _emit(standalone, operation, phase="component", status="error",
              **_error_facts(error))
    except Exception:
        pass


def unavailable(operation):
    """A returned/cached unavailable component is not a new underlying error."""
    try:
        _record_unavailability(operation)
        if not is_active():
            return
        event(operation, status="unavailable", reason="component_unavailable")
    except Exception:
        pass


class _Observation:
    def __init__(self, operation, phase, target):
        self.operation, self.phase, self.target = operation, phase, target
        self.context = self.token = self.started = None
        self.status, self.reason = "ok", None

    def __enter__(self):
        try:
            parent = _CURRENT.get()
            if (parent is None or not parent.request.active or self.operation not in _OPERATIONS
                    or self.phase not in _PHASES or self.target not in {"backend", "postgres"}):
                return self
            self.context = _Context(parent.request, uuid.uuid4().hex, parent.span_id)
            self.token = _CURRENT.set(self.context)
            self.started = _clock()
            _emit(self.context, self.operation, phase=self.phase, status="started", target=self.target)
        except Exception:
            pass
        return self

    def mark(self, *, status="ok", reason=None):
        try:
            if status in _STATUSES and (reason is None or reason in _REASONS):
                self.status, self.reason = status, reason
                if status in {"error", "unavailable", "degraded"}:
                    _record_unavailability(self.operation)
        except Exception:
            pass

    def __exit__(self, exception_type, error, traceback):
        try:
            if self.context is not None:
                facts = {"reason": self.reason} if self.reason else {}
                status = self.status
                if exception_type is not None:
                    facts = _error_facts(error)
                    status = "cancelled" if facts["reason"] == "cancelled" else "error"
                    # A caller may retry this read successfully. Only an
                    # explicit suppressed failure/unavailable result degrades
                    # the final response; a failed attempt alone does not.
                elapsed = max(0, (_clock() - self.started) * 1000) if self.started is not None else None
                _emit(self.context, self.operation, phase=self.phase, status=status,
                      target=self.target, duration_ms=elapsed, terminal=True, **facts)
        except Exception:
            pass
        finally:
            if self.token is not None:
                try:
                    _CURRENT.reset(self.token)
                except Exception:
                    pass
        return False


def scope(operation, *, phase="component", target="backend"):
    return _Observation(operation, phase, target)


def database_scope(operation_name, phase="db_transaction"):
    # Unknown names remain measurable without turning dynamic input into logs.
    operation = operation_name if type(operation_name) is str and operation_name in READ_DATABASE_OPERATIONS else "database_read"
    return scope(operation, phase=phase, target="postgres")


async def observe_http(app, scope, receive, send):
    """Observe selected finite reads, including the reviewed catalogue POST."""
    operation = None
    try:
        if scope.get("type") == "http" and (scope.get("method") == "GET"
                or (scope.get("method") == "POST" and scope.get("path") == "/research/queries/execute")):
            operation = HTTP_ROUTES.get(scope.get("path"))
        enabled = operation is not None and telemetry.is_enabled()
    except Exception:
        enabled = False
    if not enabled:
        await app(scope, receive, send)
        return
    token = None
    try:
        request = _Request(operation, uuid.uuid4().hex, uuid.uuid4().hex, _clock())
        context = _Context(request, request.root_span_id)
        token = _CURRENT.set(context)
        _emit(context, operation, phase="http", status="started", root=True)
    except Exception:
        if token is not None:
            _CURRENT.reset(token)
        await app(scope, receive, send)
        return
    response_status, delivered = None, False

    def finish(status, reason=None, error_code=None):
        try:
            if not request.active:
                return
            request.active = False
            _emit(context, operation, phase="http", status=status, reason=reason,
                  error_code=error_code, duration_ms=max(0, (_clock() - request.started) * 1000),
                  http_status=response_status, terminal=True, root=True)
        except Exception:
            pass

    async def observed_send(message):
        nonlocal response_status, delivered
        outgoing = message
        try:
            if message.get("type") == "http.response.start":
                candidate = message.get("status")
                if type(candidate) is int and 100 <= candidate <= 599:
                    response_status = candidate
                # Copy only the header container; never inspect/retain its values.
                headers = [(key, value) for key, value in message.get("headers", ())
                           if key.lower() != TRACE_HEADER]
                headers.append((TRACE_HEADER, request.trace_id.encode("ascii")))
                outgoing = {**message, "headers": headers}
        except Exception:
            outgoing = message
        await send(outgoing)
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            delivered = True
            status = "error" if response_status is not None and response_status >= 400 else (
                "partial" if request.degraded else "ok"
            )
            finish(status, "http_error" if status == "error" else (
                "component_unavailable" if status == "partial" else None
            ))

    try:
        await app(scope, receive, observed_send)
    except BaseException as error:
        facts = _error_facts(error)
        finish("cancelled" if facts["reason"] == "cancelled" else "error", **facts)
        raise
    finally:
        if not delivered:
            finish("incomplete", "delivery_incomplete")
        try:
            _CURRENT.reset(token)
        except Exception:
            pass
