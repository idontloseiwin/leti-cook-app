"""Passive, bounded pipeline observations over the existing telemetry journal.

No database/provider effects, durable promises or scheduling decisions live here.
An unfinished span is unknown, not success. Thread handoffs must use capture/run;
context variables deliberately do not leak between pooled jobs.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
import time
import uuid
from types import MappingProxyType
from typing import Any, Callable, Mapping

import telemetry

_CURRENT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar("pipeline_trace", default={})
_CONTEXT_FIELDS = frozenset(("trace_id", "span_id", "parent_span_id", "operation_id", "sync_run_id", "job_id", "channel_id", "video_id", "batch_id", "request_attempt_id", "quota_bucket", "video_type", "video_ids", "reason"))


def _identity(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: tuple(value) if key == "video_ids" and isinstance(value, (list, tuple)) else value
            for key, value in values.items() if key in _CONTEXT_FIELDS}


def capture() -> Mapping[str, Any]:
    try:
        return MappingProxyType(_identity(_CURRENT.get()))
    except Exception:
        return MappingProxyType({})


@contextmanager
def bind(**identity: Any):
    """Attach identity without inventing a lifecycle (e.g. generator resumes)."""
    token = None
    try:
        current = dict(_CURRENT.get())
        current.update(_identity(identity))
        token = _CURRENT.set(current)
    except Exception:
        pass
    try:
        yield
    finally:
        if token is not None:
            try:
                _CURRENT.reset(token)
            except Exception:
                pass


def run(captured: Mapping[str, Any], fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    token = None
    try:
        token = _CURRENT.set(_identity(captured))
    except Exception:
        pass
    try:
        return fn(*args, **kwargs)
    finally:
        if token is not None:
            try:
                _CURRENT.reset(token)
            except Exception:
                pass


def event(stage: str, status: str, **facts: Any):
    """Submit a fact, returning the accepted envelope or None; never raise."""
    try:
        values = dict(_CURRENT.get())
        values.update(facts)
        values["monotonic_ns"] = time.monotonic_ns()
        return telemetry.emit_event("pipeline", "collector", "collector", stage,
                                    direction="internal", status=status, **values)
    except Exception:
        return None


class _Observation:
    def __init__(self, operation: str, identity: Mapping[str, Any]):
        self.operation = operation
        self.identity = dict(identity)
        self.token = None
        self.started = None
        self.status = "unknown"
        self.facts: dict[str, Any] = {}

    def __enter__(self):
        try:
            current = dict(_CURRENT.get())
            parent = current.get("span_id")
            current.update(_identity(self.identity))
            current.setdefault("trace_id", uuid.uuid4().hex)
            current["span_id"] = uuid.uuid4().hex
            if parent:
                current["parent_span_id"] = parent
            else:
                current.pop("parent_span_id", None)
            self.token = _CURRENT.set(current)
            self.started = time.monotonic_ns()
            event(self.operation, "started", **self.identity)
        except Exception:
            pass
        return self

    def finish(self, status: str, **facts: Any) -> None:
        try:
            self.status = status
            self.facts = dict(facts)
        except Exception:
            pass

    def __exit__(self, exception_type, exception, traceback):
        try:
            duration = None if self.started is None else max(0.0, (time.monotonic_ns() - self.started) / 1_000_000)
            facts = dict(self.facts)
            facts["duration_ms"] = duration
            event(self.operation, self.status if exception_type is None else "unknown", **facts)
        except Exception:
            pass
        finally:
            if self.token is not None:
                try:
                    _CURRENT.reset(self.token)
                except Exception:
                    pass
        return False


def scope(operation: str, **identity: Any) -> _Observation:
    return _Observation(operation, identity)


def observe_collection_phase(phase: str, callback: Callable[..., Any], **options: Any):
    """Describe the recorded phase result without changing collection decisions."""
    with scope("collection_phase", phase=phase) as observation:
        try:
            result = callback(**options)
        except BaseException:
            observation.finish("unknown", phase=phase)
            raise
        # A diagnostic projection must not turn a successful callback into a
        # collection failure, even if its result cannot be described.
        try:
            reason = result.get("stopped_reason")
            outcome = result.get("completion_outcome")
            if result.get("settlement_unknown") or result.get("completion_unknown"):
                status = "unknown"
            elif result.get("errors") or outcome == "error" or result.get("infrastructure_error"):
                status = "error"
            elif outcome == "partial" or result.get("incomplete"):
                status = "partial"
            elif result.get("yield_requested") or reason not in (None, "no_claimable_work", "sweep_exhausted"):
                status = "deferred"
            else:
                status = "completed"
            facts = {"phase": phase, "reason": reason}
            # Video phases report confirmed updates, not an absent processed
            # field defaulted to zero. Other phases retain their own unit.
            count = result.get("updated" if phase == "video_collection" else "processed")
            if type(count) is int and count >= 0:
                facts["count"] = count
            observation.finish(status, **facts)
        except Exception:
            observation.finish("unknown", phase=phase)
        return result
