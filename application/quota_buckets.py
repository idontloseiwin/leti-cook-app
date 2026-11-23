"""Collector-scoped operational quota control, separate from observation.

``controlled`` binds one project for every thread in the collector process.
The durable daily row owns the ban; the quota journal grants no permission.
Unbound web requests are outside this contract. No pre-HTTP day-long fence is
created: a crash before the refusal write can lose that particular refusal.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import os
import time
from threading import Lock, RLock

from youtube_errors import YouTubeDailyQuotaExceededError

QUOTA_BUCKETS = ("general", "stats")
# Ledger names of the stop-table buckets.
LEDGER_BUCKETS = {"general": "general", "stats": "video_batch_get_stats"}
# The fixed ledger label of a daily quota refusal (operational_copy_policy).
QUOTA_REFUSAL_ERROR_TYPE = "quotaExceeded"

_lock = Lock()
_state = None  # (Pacific quota day, set of exhausted buckets) during a pass
_control = None
_discovery_grant = ContextVar('discovery_provider_grant', default=None)


class QuotaStateUnavailable(RuntimeError):
    """Control cannot be read; this is not a provider quotaExceeded."""
    reason = 'quota_state_unavailable'
    lic_http_attempted = False

    def __init__(self):
        super().__init__(self.reason)


class _Control:
    def __init__(self, repository):
        self.repository = repository
        self.pid = os.getpid()
        self.lock = RLock()
        self.latch_lock = RLock()
        self.day = quota_day()
        self.banned, self.pending = set(), set()
        self.unknown_since = {}

    def _roll(self):
        with self.latch_lock:
            today = quota_day()
            if today != self.day:
                self.day = today
                self.banned.clear()
                self.pending.clear()

    def _observe(self, bucket, unknown):
        import pipeline_trace as trace
        if unknown and bucket not in self.unknown_since:
            self.unknown_since[bucket] = time.monotonic()
            trace.event('quota_control', 'unknown', quota_bucket=bucket,
                        reason='quota_state_unavailable')
        elif not unknown and bucket in self.unknown_since:
            duration = min(10**12, max(0, (time.monotonic()-self.unknown_since.pop(bucket))*1000))
            trace.event('quota_control', 'recovered', quota_bucket=bucket,
                        reason='quota_state_available', duration_ms=round(duration))

    def _persist(self, bucket):
        try:
            self.repository.note_exhausted(bucket, quota_day=self.day)
        except Exception:
            self._observe(bucket, True)
            return False
        with self.latch_lock:
            self.pending.discard(bucket)
        self._observe(bucket, False)
        return True

    def note(self, bucket):
        # This short latch lock is independent of in-progress control I/O:
        # a concurrent slow read must not delay learning the actual refusal.
        with self.latch_lock:
            self._roll()
            # Latch before storage or optional diagnostic I/O.
            if bucket not in self.banned:
                self.banned.add(bucket)
                self.pending.add(bucket)
        with self.lock:
            if bucket in self.pending:
                return self._persist(bucket)
            return True

    def require(self, bucket):
        with self.lock:
            self._roll()
            if self.pid != os.getpid():
                raise QuotaStateUnavailable()
            # Retry uncertain writes even when another bucket is progressing.
            for pending in tuple(self.pending):
                self._persist(pending)
            if bucket in self.banned:
                raise remembered_refusal(bucket)
            try:
                snapshot = self.repository.snapshot()
                if snapshot['quota_day'] != self.day:
                    raise ValueError('quota_control_clock_mismatch')
            except Exception:
                if bucket in self.banned:
                    raise remembered_refusal(bucket) from None
                self._observe(bucket, True)
                raise QuotaStateUnavailable() from None
            with self.latch_lock:
                self.banned.update(snapshot['exhausted'])
            self._observe(bucket, False)
            if bucket in self.banned:
                raise remembered_refusal(bucket)


@contextmanager
def controlled(database, *, settings, interval_minutes, project_id=None):
    """Explicit collector lifetime, including all of its provider worker threads.

    Domain writes stay on their existing owner. These bounded control reads and
    refusal writes finish their own transaction before/after the HTTP attempt.
    """
    global _control
    from db.quota.provider_quota_control import ProviderQuotaControlRepository
    project = (project_id or os.getenv('YOUTUBE_QUOTA_PROJECT_ID', 'primary')).strip() or 'primary'
    with _lock:
        if _control is not None:
            raise RuntimeError('quota_control_scope_already_bound')
        control = _Control(ProviderQuotaControlRepository(database, project_id=project,
            settings=settings, interval_minutes=interval_minutes))
        _control = control
    try:
        yield control
    finally:
        with control.lock:
            control._roll()
            for bucket in tuple(control.pending):
                control._persist(bucket)
        with _lock:
            _control = None


def before_request(bucket):
    """Recheck after the provider permit, immediately before HTTP/accounting."""
    bucket = {value: key for key, value in LEDGER_BUCKETS.items()}.get(bucket, bucket)
    grant = _discovery_grant.get()
    if bucket not in QUOTA_BUCKETS:
        if grant is not None:
            from discovery_admission import DiscoveryAdmissionDenied
            raise DiscoveryAdmissionDenied(dict(reason='grant_mismatch'))
        return
    control = _control
    if control is not None:
        control.require(bucket)
    if is_exhausted(bucket):
        raise remembered_refusal(bucket)
    if grant is not None:
        from discovery_admission import DiscoveryAdmissionDenied
        if grant['used']:
            raise DiscoveryAdmissionDenied(dict(reason='grant_already_dispatched'))
        # An uncertain final read loses this small allowance, never creates
        # permission for a retry under the same token or a second HTTP attempt.
        grant['used'] = True
        try:
            decision = grant['budget'].validate_dispatch(grant['token'], bucket=bucket)
        except Exception:
            import pipeline_trace as trace
            trace.event('discovery_admission', 'unknown', quota_bucket=bucket,
                        reason='quota_state_unavailable')
            raise QuotaStateUnavailable() from None
        if not decision['allowed']:
            raise DiscoveryAdmissionDenied(decision)


@contextmanager
def discovery_grant(budget, token):
    """Bind one already-debited grant around exactly one provider callback.

    The callback runs on its provider thread, so this context reaches the
    transport after concurrency waiting. An owner must never reconstruct or
    reuse a dispatched grant in a later scope or after a process restart.
    """
    if _discovery_grant.get() is not None:
        raise RuntimeError('discovery_grant_scope_already_bound')
    context = _discovery_grant.set(dict(budget=budget, token=token, used=False))
    try:
        yield
    finally:
        _discovery_grant.reset(context)


def note_provider_refusal(bucket):
    bucket = {value: key for key, value in LEDGER_BUCKETS.items()}.get(bucket, bucket)
    if bucket in QUOTA_BUCKETS:
        control = _control
        if control is not None:
            control.note(bucket)
        else:
            note_exhausted(bucket)


def control_active():
    """Whether this collector explicitly bound the format-5 control contract."""
    return _control is not None


def quota_day(now=None):
    from collection_policy import YOUTUBE_QUOTA_TIMEZONE
    now = datetime.now(timezone.utc) if now is None else now
    return now.astimezone(YOUTUBE_QUOTA_TIMEZONE).date()


def _bucket(bucket):
    if bucket not in QUOTA_BUCKETS:
        raise ValueError("quota_bucket_invalid")
    return bucket


def start_pass(exhausted=(), *, now=None):
    """Begin one pass's memory from the buckets already refused today."""
    global _state
    buckets = {_bucket(bucket) for bucket in exhausted}
    with _lock:
        _state = (quota_day(now), buckets)


def active():
    with _lock:
        return _state is not None or _control is not None


def end_pass():
    global _state
    with _lock:
        _state = None


@contextmanager
def remembered():
    """Keep a memory for this block: the active pass's, or one of its own.

    Collection outside a cycle (a dispatch window, the compatibility runner)
    still sends nothing more to a bucket once YouTube has refused it.
    """
    global _state
    with _lock:
        own = _state is None
        if own:
            _state = (quota_day(), set())
    try:
        yield
    finally:
        if own:
            end_pass()


def note_exhausted(bucket, *, now=None):
    """Format-4 pass observation; the bound scope has its HTTP-boundary owner.

    A completed response or summary may resume on the next Pacific day. It
    cannot repeat the boundary write and turn yesterday's refusal into a new
    day's ban. Bound collection learns refusals only in note_provider_refusal.
    """
    global _state
    _bucket(bucket)
    today = quota_day(now)
    if control_active():
        return
    with _lock:
        if _state is None:
            return
        day, buckets = _state
        if day != today:
            # The Pacific reset passed during this pass: start the new day.
            _state = (today, set())
        _state[1].add(bucket)


def exhausted(*, now=None):
    """Buckets exhausted for the rest of today's Pacific quota day."""
    today = quota_day(now)
    control = _control
    controlled_buckets = set()
    if control is not None:
        with control.latch_lock:
            if control.day == today:
                controlled_buckets.update(control.banned)
    with _lock:
        if _state is not None and _state[0] == today:
            controlled_buckets.update(_state[1])
        return tuple(bucket for bucket in QUOTA_BUCKETS if bucket in controlled_buckets)


def is_exhausted(bucket, *, now=None):
    return _bucket(bucket) in exhausted(now=now)


def remembered_refusal(bucket):
    """The refusal a request would have met; nothing was sent."""
    error = YouTubeDailyQuotaExceededError("provider_quota_exceeded")
    error.lic_quota_remembered = _bucket(bucket)
    error.lic_http_attempted = False
    return error


def is_remembered_refusal(error):
    return getattr(error, "lic_quota_remembered", None) in QUOTA_BUCKETS


def guarded(bucket, fetch, is_quota_exhausted):
    """A direct provider call (full details, initial hydration) under the memory.

    An exhausted bucket refuses at once. A daily quota refusal from YouTube is
    remembered here, on the provider thread, so calls that start later in the
    same pass send nothing. Provider steps check the memory before they are
    yielded instead, keeping their own fetch callable.
    """
    _bucket(bucket)

    def call(*args, **kwargs):
        if is_exhausted(bucket):
            raise remembered_refusal(bucket)
        try:
            return fetch(*args, **kwargs)
        except Exception as error:
            try:
                refused = bool(is_quota_exhausted(error))
            except Exception:
                refused = False
            if refused:
                note_exhausted(bucket)
            raise
    return call


def read_remembered(database, *, project_id=None):
    """Explicit choice: bound format-5 control or the format-4 pass.

    Process composition binds ``controlled`` only with an immutable format-5
    discovery settings snapshot. An operational read error never selects the
    format-4 ledger. Unbound format-4 collection keeps its own ledger behavior
    and neither creates a budget snapshot nor acquires the new control scope.
    """
    control = _control
    if control is not None:
        if project_id is not None and project_id != control.repository.project:
            raise ValueError('quota_control_project_mismatch')
        return control.repository.snapshot()['exhausted']
    from db.quota.youtube_quota import YouTubeQuotaRepository
    project = (project_id or os.getenv("YOUTUBE_QUOTA_PROJECT_ID", "primary")).strip() or "primary"
    return YouTubeQuotaRepository(database, project_id=project).exhausted_buckets()
