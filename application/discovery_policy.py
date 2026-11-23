"""Pure discovery settings and clocks; no I/O, environment or runtime fallback.

Callers supply the captured settings and accepted observation clocks. Database
receipts own ordering, deduplication and persistence; these functions grant no
provider access and do not classify a video's format.
"""
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone


MAX_POLICY_SECONDS = 2_147_483_647
MAX_POLICY_LIMIT = 1_000_000


class DiscoveryPolicyError(ValueError):
    """Fixed error codes only; never include caller values."""


@dataclass(frozen=True, slots=True)
class DiscoverySettings:
    retry_first_seconds: int = 120
    retry_second_seconds: int = 600
    retry_third_seconds: int = 1800
    retry_sparse_seconds: int = 21600
    needs_review_seconds: int = 604800
    review_retry_seconds: int = 86400
    unresolved_pause_seconds: int = 2592000
    technical_retry_first_seconds: int = 120
    technical_retry_max_seconds: int = 21600
    activation_grace_seconds: int = 172800
    upcoming_max_seconds: int = 7200
    active_lifecycle_seconds: int = 900
    events_general_daily_limit: int = 2500
    events_general_window_limit: int = 100
    events_stats_daily_limit: int = 500
    events_stats_window_limit: int = 50
    full_general_daily_limit: int = 1000
    full_general_window_limit: int = 25
    repair_window_seconds: int = 604800
    repair_target_limit: int = 300
    repair_general_limit: int = 600
    repair_stats_limit: int = 50

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            maximum = MAX_POLICY_SECONDS if item.name.endswith('_seconds') else MAX_POLICY_LIMIT
            if type(value) is not int or not 1 <= value <= maximum:
                raise DiscoveryPolicyError('discovery_settings_invalid')
        retries = (self.retry_first_seconds, self.retry_second_seconds,
                   self.retry_third_seconds, self.retry_sparse_seconds)
        if retries != tuple(sorted(retries)):
            raise DiscoveryPolicyError('discovery_retry_order_invalid')
        if (self.technical_retry_first_seconds > self.technical_retry_max_seconds
                or self.review_retry_seconds < self.retry_sparse_seconds
                or self.unresolved_pause_seconds <= self.needs_review_seconds):
            raise DiscoveryPolicyError('discovery_retry_order_invalid')
        for kind in ('events_general', 'events_stats', 'full_general'):
            if getattr(self, kind + '_window_limit') > getattr(self, kind + '_daily_limit'):
                raise DiscoveryPolicyError('discovery_budget_window_invalid')


def _settings(value):
    if type(value) is not DiscoverySettings:
        raise DiscoveryPolicyError('discovery_settings_required')
    return value


def _clock(value):
    try:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise DiscoveryPolicyError('discovery_clock_invalid') from None


def _shift(clock, seconds):
    try:
        return clock + timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        raise DiscoveryPolicyError('discovery_clock_invalid') from None


def retry_due_at(last_checked_at, completed_checks, settings):
    """One completed unsuccessful check selects the first delay, not the second.

    Transport/quota failures do not advance this discovery-evidence ladder.
    The caller preserves its counter when a request was not admitted.
    """
    settings = _settings(settings)
    checked = _clock(last_checked_at)
    if type(completed_checks) is not int or not 1 <= completed_checks <= MAX_POLICY_SECONDS:
        raise DiscoveryPolicyError('discovery_attempt_count_invalid')
    delays = (settings.retry_first_seconds, settings.retry_second_seconds,
              settings.retry_third_seconds, settings.retry_sparse_seconds)
    return _shift(checked, delays[min(completed_checks - 1, 3)])


def technical_retry_due_at(last_failed_at, consecutive_failures, settings):
    """Phase-local exponential retry; it is not a completed evidence check."""
    settings = _settings(settings)
    failed = _clock(last_failed_at)
    if type(consecutive_failures) is not int or not 1 <= consecutive_failures <= MAX_POLICY_SECONDS:
        raise DiscoveryPolicyError('discovery_attempt_count_invalid')
    # A bounded exponent also handles an extremely old persistent failure streak.
    delay = min(settings.technical_retry_max_seconds,
                settings.technical_retry_first_seconds * (1 << min(consecutive_failures - 1, 31)))
    return _shift(failed, delay)


def lifecycle_due_at(observed_at, scheduled_start_at, settings):
    """A future date bounds the next check; it proves neither start nor format."""
    settings = _settings(settings)
    observed = _clock(observed_at)
    scheduled = None if scheduled_start_at is None else _clock(scheduled_start_at)
    if scheduled is not None and scheduled > observed:
        return min(scheduled, _shift(observed, settings.upcoming_max_seconds))
    return _shift(observed, settings.active_lifecycle_seconds)


@dataclass(frozen=True, slots=True)
class WaitClock:
    """Effective review-age anchor plus a bounded, confirmed future pause.

    opened_at remains the canonical obligation receipt supplied by its DB owner.
    Shifting unresolved_since accounts for completed pauses, not renewed data.
    """
    opened_at: datetime
    unresolved_since: datetime
    future_wait_started_at: datetime | None = None
    future_wait_until: datetime | None = None

    def __post_init__(self):
        for name in ('opened_at', 'unresolved_since', 'future_wait_started_at', 'future_wait_until'):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _clock(value))
        if (self.opened_at is None or self.unresolved_since is None
                or self.unresolved_since < self.opened_at
                or (self.future_wait_started_at is None) != (self.future_wait_until is None)):
            raise DiscoveryPolicyError('discovery_wait_clock_invalid')
        if self.future_wait_started_at is not None and (
                self.future_wait_started_at < self.unresolved_since
                or self.future_wait_until <= self.future_wait_started_at):
            raise DiscoveryPolicyError('discovery_wait_clock_invalid')


def advance_wait_clock(previous, *, observed_at, opened_at=None, scheduled_start_at=None):
    """Preserve accumulated problem age across repeated or rescheduled premieres.

    Only a currently confirmed future date pauses the clock. After downtime,
    the pause ends at the last confirmed date, not at the next worker wake.
    Call only for an accepted new observation; receipt ordering lives in DB.
    """
    observed = _clock(observed_at)
    if previous is None:
        opened = _clock(opened_at)
        previous = WaitClock(opened, opened)
    elif type(previous) is not WaitClock:
        raise DiscoveryPolicyError('discovery_wait_clock_invalid')
    elif opened_at is not None and _clock(opened_at) != previous.opened_at:
        raise DiscoveryPolicyError('discovery_wait_clock_invalid')
    if observed < previous.unresolved_since or (
            previous.future_wait_started_at is not None
            and observed < previous.future_wait_started_at):
        raise DiscoveryPolicyError('discovery_observation_order_invalid')
    scheduled = None if scheduled_start_at is None else _clock(scheduled_start_at)
    future = scheduled is not None and scheduled > observed
    anchor = previous.unresolved_since
    started = previous.future_wait_started_at
    if started is not None:
        if future and observed < previous.future_wait_until:
            # Same ongoing pause; no new opening time and no review-age reset.
            return WaitClock(previous.opened_at, anchor, started, scheduled)
        elapsed = min(observed, previous.future_wait_until) - started
        anchor += elapsed
    return WaitClock(previous.opened_at, anchor,
                     observed if future else None, scheduled if future else None)


def needs_review(clock, as_of, settings):
    """Future intervals pause existing age; they never erase previous waiting."""
    settings = _settings(settings)
    if type(clock) is not WaitClock:
        raise DiscoveryPolicyError('discovery_wait_clock_invalid')
    return _unresolved_age(clock, as_of) >= timedelta(seconds=settings.needs_review_seconds)


def _unresolved_age(clock, as_of):
    if type(clock) is not WaitClock:
        raise DiscoveryPolicyError('discovery_wait_clock_invalid')
    now = _clock(as_of)
    if now < clock.unresolved_since or (
            clock.future_wait_started_at is not None and now < clock.future_wait_started_at):
        raise DiscoveryPolicyError('discovery_observation_order_invalid')
    age = now - clock.unresolved_since
    if clock.future_wait_started_at is not None:
        age -= min(now, clock.future_wait_until) - clock.future_wait_started_at
    return age


def unresolved_status(clock, as_of, settings):
    """Persistent pause preserves the obligation; it never declares absence."""
    settings = _settings(settings)
    age = _unresolved_age(clock, as_of)
    if age >= timedelta(seconds=settings.unresolved_pause_seconds):
        return 'paused'
    return 'review' if age >= timedelta(seconds=settings.needs_review_seconds) else 'active'


@dataclass(frozen=True, slots=True)
class RepairWindow:
    start_at: datetime
    end_at: datetime

    def __post_init__(self):
        object.__setattr__(self, 'start_at', _clock(self.start_at))
        object.__setattr__(self, 'end_at', _clock(self.end_at))
        if self.start_at >= self.end_at:
            raise DiscoveryPolicyError('discovery_repair_window_invalid')

    def contains(self, received_at):
        """Our event receipt clock only, never the provider publication date."""
        return self.start_at <= _clock(received_at) < self.end_at


def repair_window(cutover_at, settings):
    """Immutable [cutover - lookback, cutover); campaign launch does not move it."""
    settings = _settings(settings)
    cutover = _clock(cutover_at)
    return RepairWindow(_shift(cutover, -settings.repair_window_seconds), cutover)


def repair_preparation_deadline(window, journal_retention_seconds):
    """The journal owner supplies retention; do not duplicate its 90-day policy."""
    if (type(window) is not RepairWindow or type(journal_retention_seconds) is not int
            or not 1 <= journal_retention_seconds <= MAX_POLICY_SECONDS
            or timedelta(seconds=journal_retention_seconds) <= window.end_at - window.start_at):
        raise DiscoveryPolicyError('discovery_repair_retention_invalid')
    return _shift(window.start_at, journal_retention_seconds)


def prepare_repair_window(cutover_at, prepared_at, settings, *, journal_retention_seconds):
    """Refuse expired selection history; never silently shift the approved window.

    Existing already-captured campaign targets do not need this selection step.
    """
    window = repair_window(cutover_at, settings)
    prepared = _clock(prepared_at)
    if prepared < window.end_at:
        raise DiscoveryPolicyError('discovery_repair_before_cutover')
    if prepared >= repair_preparation_deadline(window, journal_retention_seconds):
        raise DiscoveryPolicyError('discovery_repair_history_expired')
    return window
