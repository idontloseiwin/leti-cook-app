"""Unwired bounded collection coordinator; no database or provider adapters.

Only the creating thread may offer, prepare, settle or reconcile work. An
injected Executor runs fetch callbacks only. Work credit stays held until the
domain returns Ack, including completed responses and uncertain operations.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from contextlib import nullcontext
from concurrent.futures import Executor, Future
from contextvars import copy_context
from dataclasses import dataclass, replace
import math
import threading
import time
from typing import Any, Callable

from pipeline_trace import capture, event, run as trace_run, scope

LANES = ("auto", "operator")
FORMATS = ("long", "short", "stream", "channel")
DEFAULT_CONCURRENCY = 8
MAX_CONCURRENCY = 50


def _empty_fetch_scope(lane, cancel, *, deadline):
    return nullcontext()


def _text(value, maximum=128):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _clock_value(value):
    return (type(value) in (int, float) and math.isfinite(value) and value >= 0)


@dataclass(frozen=True)
class Ack:
    """Domain settlement/receipt is known durable; this credit can be released."""


@dataclass(frozen=True)
class Deferred:
    """Prepare did not dispatch; domain released/deferred any acquired claim."""
    until: float

    def __post_init__(self):
        if not _clock_value(self.until):
            raise ValueError("dispatch_defer_invalid")


@dataclass(frozen=True)
class Skipped:
    """Prepare confirmed that this candidate needs no provider work."""


@dataclass(frozen=True)
class Prepared:
    # Only this callback runs in the Executor. It must not own a DB transaction.
    fetch: Callable[[threading.Event], Any]
    deadline: float
    batch_size: int = 1

    def __post_init__(self):
        if (not callable(self.fetch) or not _clock_value(self.deadline)
                or type(self.batch_size) is not int or not 1 <= self.batch_size <= 50):
            raise ValueError("dispatch_prepared_invalid")


@dataclass(frozen=True)
class Completion:
    # phase is response, fetch_error, cancelled, expired, prepare_error or submit_error.
    phase: str
    value: Any = None
    error: BaseException | None = None
    fetch_started: bool = False
    cancel_requested: bool = False


@dataclass(frozen=True)
class Work:
    key: str
    lane: str
    channel: str
    format: str
    prepare: Callable[[], Prepared | Deferred | Skipped]
    settle: Callable[[Completion], Ack | Prepared]
    not_before: float = 0.0
    # Playlist adapters MUST use one stable exclusion key per (channel, format).
    # Independent hydration batches leave it None or use a batch-specific key.
    # Format is only a fairness group; it does not serialize independent batches.
    exclusion_key: str | None = None

    def __post_init__(self):
        if (not _text(self.key) or self.lane not in LANES or not _text(self.channel)
                or self.format not in FORMATS or not callable(self.prepare)
                or not callable(self.settle) or not _clock_value(self.not_before)
                or (self.exclusion_key is not None and not _text(self.exclusion_key))):
            raise ValueError("dispatch_work_invalid")


@dataclass(frozen=True)
class Snapshot:
    ready: int
    inflight: int
    unacked: int
    held: int
    paused: bool
    stopped: bool
    blocked: bool


@dataclass(frozen=True)
class Turn:
    acknowledged: tuple[str, ...]
    skipped: tuple[str, ...]
    submitted: int
    snapshot: Snapshot


class QueueFull(RuntimeError):
    pass


class DuplicateWork(RuntimeError):
    pass


class DispatchBlocked(RuntimeError):
    """Fixed message only; callers must not log cause/payload as public text."""
    def __init__(self, phase, key, cause):
        super().__init__("dispatch_" + phase + "_requires_reconciliation")
        self.phase, self.key, self.cause = phase, key, cause


@dataclass
class _Held:
    work: Work
    cancel: threading.Event
    future: Future | None = None
    completion: Completion | None = None


class Coordinator:
    """Finite RAM scheduler, not a durable queue or a new lease/ACK owner.

    Executor lifetime, bounded HTTP/response bytes and durable candidate paging
    belong to adapters. No submit/prepare/settle retry is inferred from an error.
    The parent must keep the executor alive until held=0 (or use its bounded
    process shutdown); cancelling a running Future does not release its credit.
    """
    def __init__(self, executor: Executor, concurrency=DEFAULT_CONCURRENCY, *,
                 clock=time.monotonic, fetch_scope=_empty_fetch_scope):
        if (type(concurrency) is not int or not 1 <= concurrency <= MAX_CONCURRENCY
                or not callable(getattr(executor, "submit", None)) or not callable(clock)
                or not callable(fetch_scope)):
            raise ValueError("dispatch_config_invalid")
        self.concurrency, self.executor, self.clock = concurrency, executor, clock
        self.fetch_scope = fetch_scope
        self._thread = threading.get_ident()
        self._callback_active = False
        self._ready = {lane: OrderedDict() for lane in LANES}
        self._lanes = deque(LANES)
        self._known = {}
        self._trace_contexts = {}
        self._exclusions = {}
        self._held = {}
        self._failures = OrderedDict()
        self._blocked = None
        self._paused = self._stopped = False
        self._wakeup = threading.Event()

    def _owner(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError("dispatch_coordinator_thread_required")
        if self._callback_active:
            raise RuntimeError("dispatch_callback_reentry_forbidden")

    def _callback(self, callback, *args):
        self._callback_active = True
        try:
            return callback(*args)
        finally:
            self._callback_active = False

    def _ready_size(self):
        return sum(len(queue) for channels in self._ready.values()
                   for formats in channels.values() for queue in formats.values())

    def offer(self, work: Work):
        self._owner()
        if not isinstance(work, Work):
            raise ValueError("dispatch_work_required")
        if self._stopped:
            event("dispatch_offer", "rejected", operation_id=work.key, reason="coordinator_stopped")
            raise RuntimeError("dispatch_stopped")
        if (work.key in self._known
                or (work.exclusion_key is not None and work.exclusion_key in self._exclusions)):
            event("dispatch_offer", "rejected", operation_id=work.key, reason="already_owned")
            raise DuplicateWork("dispatch_work_already_owned")
        if self._ready_size() >= 2 * self.concurrency:
            event("dispatch_offer", "rejected", operation_id=work.key, reason="ready_full")
            raise QueueFull("dispatch_ready_full")
        self._known[work.key] = work
        self._trace_contexts[work.key] = capture()
        event("dispatch_offered", "queued", operation_id=work.key, video_type=work.format if work.format != "channel" else None, reason=work.lane)
        if work.exclusion_key is not None:
            self._exclusions[work.exclusion_key] = work.key
        self._insert(work)
        self.wake()

    def _insert(self, work):
        formats = self._ready[work.lane].setdefault(work.channel, OrderedDict())
        formats.setdefault(work.format, deque()).append(work)

    def _remove_ready(self, work):
        channels = self._ready[work.lane]
        formats = channels[work.channel]
        queue = formats[work.format]
        queue.remove(work)
        if not queue:
            del formats[work.format]
        if not formats:
            del channels[work.channel]

    def _next(self, now):
        # All scans are over resident descriptors only (at most 2C).
        for _ in LANES:
            lane = self._lanes[0]
            self._lanes.rotate(-1)
            channels = self._ready[lane]
            for channel, formats in list(channels.items()):
                for kind, queue in list(formats.items()):
                    work = next((row for row in queue if row.not_before <= now), None)
                    if work is None:
                        continue
                    self._remove_ready(work)
                    if channel in channels:
                        channels.move_to_end(channel)
                        if kind in formats:
                            formats.move_to_end(kind)
                    return work
        return None

    def _forget(self, work):
        self._held.pop(work.key, None)
        self._known.pop(work.key)
        self._trace_contexts.pop(work.key, None)
        if work.exclusion_key is not None:
            del self._exclusions[work.exclusion_key]

    def _block(self, phase, held, error):
        trace_run(self._trace_contexts.get(held.work.key), event, "dispatch_blocked", "unknown",
                  operation_id=held.work.key, phase=phase, reason="reconciliation_required")
        failure = DispatchBlocked(phase, held.work.key, error)
        self._failures[held.work.key] = failure
        self._blocked = next(iter(self._failures.values()))
        raise failure from error

    def _observed_callback(self, work, phase, callback, *args):
        def invoke():
            with scope("dispatch_" + phase, operation_id=work.key,
                       video_type=work.format if work.format != "channel" else None, reason=work.lane) as trace:
                result = self._callback(callback, *args)
                status = ("deferred" if isinstance(result, Deferred) else
                          "skipped" if isinstance(result, Skipped) else
                          "acknowledged" if isinstance(result, Ack) else "prepared")
                trace.finish(status)
                return result
        return trace_run(self._trace_contexts.get(work.key), invoke)

    @staticmethod
    def _observed_fetch(prepared, cancel, clock, work, fetch_scope):
        with scope("dispatch_fetch", operation_id=work.key,
                   video_type=work.format if work.format != "channel" else None, requested_count=prepared.batch_size) as trace:
            result = Coordinator._fetch(prepared, cancel, clock, work.lane, fetch_scope)
            trace.finish(result.phase)
            return result

    @staticmethod
    def _fetch(prepared, cancel, clock, lane, fetch_scope):
        if cancel.is_set():
            return Completion("cancelled", cancel_requested=True)
        if clock() >= prepared.deadline:
            return Completion("expired")
        try:
            with fetch_scope(lane, cancel, deadline=prepared.deadline):
                value = prepared.fetch(cancel)
            return Completion("response", value=value, fetch_started=True,
                              cancel_requested=cancel.is_set())
        except Exception as error:
            # Preserve the error for the domain classifier; never log it here.
            return Completion("fetch_error", error=error, fetch_started=True,
                              cancel_requested=cancel.is_set())

    def _collect(self):
        for held in self._held.values():
            if held.future is None or not held.future.done():
                continue
            if held.future.cancelled():
                held.completion = Completion("cancelled", cancel_requested=True)
            else:
                try:
                    held.completion = held.future.result()
                except BaseException as error:
                    held.completion = Completion("fetch_error", error=error, fetch_started=True,
                                                 cancel_requested=held.cancel.is_set())
            held.future = None

    def _submit(self, held, prepared):
        # A continuation retains the same credit and domain lease. It never
        # passes through prepare again or competes for a second resident slot.
        held.completion = None
        try:
            context = copy_context()
            trace_run(self._trace_contexts.get(held.work.key), event, "dispatch_submitted", "submitted",
                      operation_id=held.work.key, requested_count=prepared.batch_size)
            held.future = self.executor.submit(
                context.run, trace_run, self._trace_contexts.get(held.work.key),
                self._observed_fetch, prepared, held.cancel, self.clock,
                held.work, self.fetch_scope)
            held.future.add_done_callback(lambda unused: self.wake())
        except Exception as error:
            held.completion = Completion("submit_error", error=error)
            self._block("submit", held, error)

    def _settle(self, held):
        completion = replace(held.completion, cancel_requested=held.cancel.is_set())
        held.completion = completion
        try:
            ack = self._observed_callback(held.work, "settle", held.work.settle, completion)
            if not isinstance(ack, (Ack, Prepared)):
                raise TypeError("dispatch_durable_ack_required")
        except Exception as error:
            self._block("settle", held, error)
        if isinstance(ack, Ack):
            self._forget(held.work)
            return True, 0
        if self._paused or self._stopped or held.cancel.is_set():
            # The domain is now prepared for the next step, but that step has
            # definitely not been submitted. Its next callback must acknowledge
            # an exact-token release; never infer ACK from cancellation alone.
            held.cancel.set()
            held.completion = Completion("cancelled", cancel_requested=True)
            return False, 0
        self._submit(held, ack)
        return False, 1

    def step(self):
        """Collect -> settle -> prepare/submit. All domain callbacks run here."""
        self._owner()
        self._wakeup.clear()
        self._collect()
        if self._blocked is not None:
            raise self._blocked
        acknowledged, skipped = [], []
        submitted = 0
        for held in list(self._held.values()):
            if held.completion is None:
                continue
            acknowledged_now, submitted_now = self._settle(held)
            submitted += submitted_now
            if acknowledged_now:
                acknowledged.append(held.work.key)
        if not self._paused and not self._stopped:
            # No callbacks can re-enter offer; this pass is bounded by resident ready.
            for _ in range(self._ready_size()):
                if len(self._held) >= self.concurrency:
                    break
                work = self._next(self.clock())
                if work is None:
                    break
                held = _Held(work, threading.Event())
                self._held[work.key] = held
                try:
                    prepared = self._observed_callback(work, "prepare", work.prepare)
                    if isinstance(prepared, Deferred):
                        if prepared.until <= self.clock():
                            raise ValueError("dispatch_defer_must_be_future")
                        deferred = replace(work, not_before=prepared.until)
                        del self._held[work.key]
                        self._known[work.key] = deferred
                        self._insert(deferred)
                        continue
                    if isinstance(prepared, Skipped):
                        skipped.append(work.key)
                        self._forget(work)
                        continue
                    if not isinstance(prepared, Prepared):
                        raise TypeError("dispatch_prepare_result_invalid")
                except Exception as error:
                    held.completion = Completion("prepare_error", error=error)
                    self._block("prepare", held, error)
                self._submit(held, prepared)
                submitted += 1
        return Turn(tuple(acknowledged), tuple(skipped), submitted, self.snapshot())

    def drain_known(self, keys):
        """Drain explicitly selected independent units without dispatch or replay.

        The domain owner, not this RAM scheduler, must establish that these
        units can settle despite another uncertain operation. Failed units are
        refused: their callbacks are never retried. New failures retain their
        own credit and remain visible through failures(), even when another
        unit was already blocked. Each call performs at most one callback per
        selected unit. A cancelled continuation needs a subsequent drain/step.
        """
        self._owner()
        if (not isinstance(keys, tuple) or len(keys) > self.concurrency
                or any(not _text(key) for key in keys) or len(set(keys)) != len(keys)
                or any(key not in self._held or key in self._failures for key in keys)):
            raise ValueError("dispatch_known_drain_keys_required")
        self._paused = True
        skipped = tuple(key for key in self._known if key not in self._held)
        for key in skipped:
            self.cancel(key)
        for key in keys:
            self.cancel(key)
        self._collect()
        acknowledged = []
        for key in keys:
            held = self._held[key]
            if held.completion is None:
                continue
            try:
                acknowledged_now, submitted = self._settle(held)
            except DispatchBlocked:
                # Capture each new uncertainty without stopping independent
                # known siblings or replaying the uncertain callback.
                continue
            if submitted:
                raise AssertionError("dispatch_drain_must_not_submit")
            if acknowledged_now:
                acknowledged.append(key)
        return Turn(tuple(acknowledged), skipped, 0, self.snapshot())

    def failures(self):
        """Bounded private reconciliation evidence; causes may contain secrets."""
        self._owner()
        return tuple(self._failures.values())

    def keys(self):
        """Snapshot only resident identities (at most 2C ready plus C held)."""
        self._owner()
        return tuple(self._known)

    def contains(self, key):
        """Read-only resident-key proof for a bounded producer's own descriptors."""
        self._owner()
        if not _text(key):
            raise ValueError("dispatch_work_key_invalid")
        return key in self._known

    def pending(self, key):
        """Original completion for explicit receipt reconciliation; no callback."""
        self._owner()
        self._collect()
        return self._held[key].completion

    def reconcile(self, key, ack: Ack):
        """Caller already proved a durable terminal receipt; never retries writes.

        A lost executor-submit ACK additionally requires proof no fetch can still
        be running. For an ordinary running Future, reconciliation is refused.
        """
        self._owner()
        if not isinstance(ack, Ack) or key not in self._failures:
            raise ValueError("dispatch_blocked_receipt_required")
        held = self._held[key]
        if held.future is not None and not held.future.done():
            raise RuntimeError("dispatch_fetch_still_running")
        trace_run(self._trace_contexts.get(key), event, "dispatch_reconciled", "confirmed", operation_id=key)
        self._forget(held.work)
        del self._failures[key]
        self._blocked = next(iter(self._failures.values()), None)
        self.wake()

    def cancel(self, key):
        self._owner()
        work = self._known.get(key)
        if work is None:
            return False
        held = self._held.get(key)
        trace_run(self._trace_contexts.get(key), event, "dispatch_cancel", "requested",
                  operation_id=key, reason="unprepared" if held is None else "held")
        if held is None:
            self._remove_ready(work)
            self._forget(work)
        else:
            held.cancel.set()
            if held.future is not None:
                held.future.cancel()
        self.wake()
        return True

    def pause(self, paused=True):
        self._owner()
        if type(paused) is not bool:
            raise ValueError("dispatch_pause_invalid")
        self._paused = paused
        self.wake()

    def stop(self):
        self._owner()
        self._stopped = True
        for key in list(self._known):
            self.cancel(key)
        self.wake()

    def snapshot(self):
        self._owner()
        inflight = sum(row.future is not None and not row.future.done() for row in self._held.values())
        return Snapshot(self._ready_size(), inflight, len(self._held) - inflight, len(self._held),
                        self._paused, self._stopped, self._blocked is not None)

    def wake(self):
        """Only cross-thread method; never runs domain callbacks."""
        self._wakeup.set()

    def wait(self, maximum=30.0):
        """Wait for completion/new durable work/due time; no empty polling loop."""
        self._owner()
        if not _clock_value(maximum) or not 0 < maximum <= 60:
            raise ValueError("dispatch_wait_invalid")
        if self._blocked is None and any(
                row.completion is not None or (row.future is not None and row.future.done())
                for row in self._held.values()):
            return
        delay = maximum
        if (self._blocked is None and not self._paused and not self._stopped
                and len(self._held) < self.concurrency):
            due = [work.not_before for work in self._known.values() if work.key not in self._held]
            if due:
                delay = min(delay, max(0.0, min(due) - self.clock()))
        self._wakeup.wait(delay)
        self._wakeup.clear()
