"""Pure collection policies; cadence values come only from an explicit snapshot.

These functions prepare F6-F9 writers; existing SQL writers remain authoritative
until their transaction protocols migrate. Clocks are explicit and normalized to
UTC, matching the direct database transaction timezone. Infinity is represented
explicitly, never replaced by datetime.max. No policy reads a clock or database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
from zoneinfo import ZoneInfo

from collection_schedule_model import (
    MAX_INTERVAL_SECONDS, InventorySchedule, InventorySchedules, RecheckSchedules,
    UnavailableSchedule, UnavailableSchedules,
)


class PolicyError(ValueError):
    def __init__(self, code, *, sqlstate="P0001"):
        self.code, self.sqlstate = code, sqlstate
        super().__init__(code)


class Infinity(Enum):
    NEGATIVE = -1
    POSITIVE = 1


Clock = datetime | Infinity
_DAY_SECONDS = 86_400
YOUTUBE_QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
STATS_AVAILABILITY_CHECK_REQUIRED = "youtube_stats_counters_unavailable"
_BAD_SCAN_ERRORS = frozenset({
    "repeated_page_token", "inventory_token_cycle", "inventory_token_history_invalid",
    "invalid_next_page_token", "empty_page_with_continuation", "invalid_playlist_item",
    "duplicate_playlist_item", "invalid_items", "invalid_response",
})
_QUOTA_ERRORS = frozenset({"youtube_daily_quota_exceeded_error", "youtube_quota_exhausted"})


def clock(value) -> Clock | None:
    """Accept an aware datetime or an explicit PostgreSQL infinity token."""
    if value is None or isinstance(value, Infinity):
        return value
    if value == "infinity":
        return Infinity.POSITIVE
    if value == "-infinity":
        return Infinity.NEGATIVE
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PolicyError("collection_policy_clock_invalid")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise PolicyError("collection_policy_clock_out_of_range", sqlstate="22008") from None


def _reference_clock(value):
    value = clock(value)
    if not isinstance(value, datetime):
        raise PolicyError("collection_policy_db_clock_invalid")
    return value


def _key(value):
    if value is Infinity.NEGATIVE:
        return (0,)
    if value is Infinity.POSITIVE:
        return (2,)
    return (1, value)


def _le(left, right):
    # SQL comparisons with NULL do not satisfy a predicate.
    return left is not None and right is not None and _key(left) <= _key(right)


def _lt(left, right):
    return left is not None and right is not None and _key(left) < _key(right)


def _add(value, seconds):
    if value is None or isinstance(value, Infinity):
        return value
    try:
        return value + timedelta(seconds=seconds)
    except OverflowError:
        # Extended finite PG years need a reviewed representation before writer
        # cutover; never silently wrap, clamp or choose a different schedule.
        raise PolicyError("collection_policy_clock_out_of_range", sqlstate="22008") from None


@dataclass(frozen=True)
class CadenceRule:
    video_type: str
    collection_kind: str
    availability_class: str
    min_age_seconds: int
    max_age_seconds: int | None
    cadence_seconds: int
    policy_version: int
    enabled: bool
    age_bucket: str | None
    jitter_window_seconds: int

    @property
    def priority(self):
        return 0

    def __post_init__(self):
        integers = (self.min_age_seconds, self.cadence_seconds, self.policy_version, self.jitter_window_seconds)
        valid = (all(type(value) is int for value in integers)
                 and all(isinstance(value, str) for value in
                         (self.video_type, self.collection_kind, self.availability_class))
                 and (self.age_bucket is None or isinstance(self.age_bucket, str))
                 and type(self.enabled) is bool and self.min_age_seconds >= 0
                 and 0 < self.cadence_seconds <= MAX_INTERVAL_SECONDS and self.policy_version > 0
                 and 0 <= self.jitter_window_seconds <= self.cadence_seconds
                 and (self.max_age_seconds is None or type(self.max_age_seconds) is int)
                 and self.collection_kind in {"counters", "metadata"})
        if self.video_type == "long" or (isinstance(self.video_type, str)
                and self.video_type in {"short", "stream"} and self.collection_kind == "counters"):
            valid = (valid and self.availability_class in {"public", "unlisted"}
                     and self.age_bucket is None and self.jitter_window_seconds == 0
                     and (self.max_age_seconds is None or self.max_age_seconds > self.min_age_seconds))
        elif isinstance(self.video_type, str) and self.video_type in {"short", "stream"}:
            valid = (valid and self.collection_kind == "metadata" and self.availability_class == "all"
                     and self.age_bucket in {"age_0_90d", "age_91d_plus"}
                     and self.min_age_seconds % _DAY_SECONDS == 0
                     and (self.max_age_seconds is None or (self.max_age_seconds >= self.min_age_seconds
                          and self.max_age_seconds % _DAY_SECONDS == 0)))
        else:
            valid = False
        if not valid:
            raise PolicyError("collection_cadence_invalid")


@dataclass(frozen=True)
class CadenceSnapshot:
    rules: tuple[CadenceRule, ...]
    captured_at: datetime | None = None
    rechecks: RecheckSchedules = field(default_factory=RecheckSchedules)
    unavailable_by_type: UnavailableSchedules = field(default_factory=UnavailableSchedules)
    inventory_by_type: InventorySchedules = field(default_factory=InventorySchedules)

    def __post_init__(self):
        if (type(self.rechecks) is not RecheckSchedules
                or type(self.unavailable_by_type) is not UnavailableSchedules
                or type(self.inventory_by_type) is not InventorySchedules
                or type(self.rules) is not tuple or not 1 <= len(self.rules) <= 1000
                or any(not isinstance(rule, CadenceRule) for rule in self.rules)):
            raise PolicyError("collection_cadence_invalid")
        keys = [(r.video_type, r.collection_kind, r.availability_class, r.min_age_seconds, r.policy_version)
                for r in self.rules]
        if len(set(keys)) != len(keys):
            raise PolicyError("collection_cadence_invalid")
        if self.captured_at is not None:
            captured = clock(self.captured_at)
            if not isinstance(captured, datetime):
                raise PolicyError("collection_cadence_invalid")
            object.__setattr__(self, "captured_at", captured)


def availability_class(status):
    if status == "unlisted":
        return "unlisted"
    if status is None or status == "public":
        return "public"
    return None


def _long_age_seconds(as_of, anchor):
    # PG GREATEST(NULL, 0) yields 0. Numeric::bigint rounds half away from zero.
    if as_of is None or anchor is None:
        return 0
    if isinstance(as_of, Infinity) or isinstance(anchor, Infinity):
        raise PolicyError("collection_policy_infinite_age_invalid", sqlstate="22008")
    delta = as_of - anchor
    micros = (delta.days * _DAY_SECONDS + delta.seconds) * 1_000_000 + delta.microseconds
    rounded = ((micros + 500_000) // 1_000_000 if micros >= 0
               else -((-micros + 500_000) // 1_000_000))
    return max(rounded, 0)


# Finite cadence ranking vocabulary shared with bounded native fact readers.
CADENCE_PRIORITY_FIELDS = ("policy_version", "min_age_seconds")


def cadence_priority(rule):
    return tuple(getattr(rule, field) for field in CADENCE_PRIORITY_FIELDS)


def typed_jittered_seconds(video_id, cadence_seconds, jitter_window_seconds):
    """Exact native MD5 first32-bit offset; all inputs are caller-validated facts."""
    hash_value = int(hashlib.md5(video_id.encode("utf-8")).hexdigest()[:8], 16)
    return cadence_seconds - jitter_window_seconds + hash_value % (jitter_window_seconds + 1)


def select_policy(snapshot, video_type, collection_kind, availability_status,
                  age_anchor_at, observed_at, as_of) -> CadenceRule | None:
    anchor, observed, at = map(clock, (age_anchor_at, observed_at, as_of))
    if video_type == "long" or collection_kind == "counters":
        return _select_video_policy_at_age(snapshot, video_type, collection_kind,
                                          availability_status, _long_age_seconds(at, anchor))
    typed_anchor = anchor if anchor is not None else observed
    candidates = []
    for rule in snapshot.rules:
        if (not rule.enabled or rule.video_type != video_type or rule.collection_kind != collection_kind
                or rule.availability_class != "all"):
            continue
        if video_type in {"short", "stream"}:
            # UTC transaction timezone: subtraction is by whole calendar days;
            # the lower bound is open, upper bound closed, unlike Long seconds.
            matches = ((rule.min_age_seconds == 0 or _lt(typed_anchor, _add(observed, -rule.min_age_seconds)))
                       and (rule.max_age_seconds is None or _le(_add(observed, -rule.max_age_seconds), typed_anchor)))
        else:
            matches = False
        if matches:
            candidates.append(rule)
    return max(candidates, key=cadence_priority, default=None)


def select_long_policy_at_age(snapshot, collection_kind, availability_status, age_seconds) -> CadenceRule | None:
    """Select from DB-owned cadence after native PG age rounding/clamping.

    The caller may project an age from BC/extended-year timestamps without
    converting those timestamps into Python datetime. This function owns the
    same thresholds used by select_policy; it never samples a clock.
    """
    return _select_video_policy_at_age(snapshot, "long", collection_kind, availability_status, age_seconds)


def select_counter_policy_at_age(snapshot, video_type, availability_status, age_seconds) -> CadenceRule | None:
    """Select one explicit video's counter rule; no inferred or default type."""
    return _select_video_policy_at_age(snapshot, video_type, "counters", availability_status, age_seconds)


def _select_video_policy_at_age(snapshot, video_type, collection_kind, availability_status, age_seconds):
    if type(age_seconds) is not int or age_seconds < 0:
        raise PolicyError("collection_policy_integer_out_of_range", sqlstate="22003")
    available = availability_class(availability_status)
    candidates = [rule for rule in snapshot.rules if rule.enabled and rule.video_type == video_type
        and rule.collection_kind == collection_kind and rule.availability_class == available
        and rule.min_age_seconds <= age_seconds and (rule.max_age_seconds is None or rule.max_age_seconds > age_seconds)]
    return max(candidates, key=cadence_priority, default=None)


def policy_value(snapshot, collection_kind, availability_status, published_at, as_of, value):
    rule = select_policy(snapshot, "long", collection_kind, availability_status, published_at, None, as_of)
    if rule is None or value not in {"cadence_seconds", "priority", "policy_version"}:
        return None
    return getattr(rule, value)


def long_due_at(snapshot, collection_kind, availability_status, published_at,
                last_checked_at, as_of, *, db_now):
    return _video_due_at(snapshot, "long", collection_kind, availability_status,
                         published_at, last_checked_at, as_of, db_now=db_now)


def counters_due_at(snapshot, video_type, availability_status, published_at,
                    last_checked_at, as_of, *, db_now):
    if video_type not in ("long", "short", "stream"):
        raise PolicyError("video_counter_type_invalid")
    return _video_due_at(snapshot, video_type, "counters", availability_status,
                         published_at, last_checked_at, as_of, db_now=db_now)


def _video_due_at(snapshot, video_type, collection_kind, availability_status,
                  published_at, last_checked_at, as_of, *, db_now):
    published, checked, at = map(clock, (published_at, last_checked_at, as_of))
    now = _reference_clock(db_now)
    if published is None or isinstance(published, Infinity):
        return at
    if availability_class(availability_status) is None:
        return None
    if checked is None or isinstance(checked, Infinity) or _lt(now, checked):
        return at
    rule = select_policy(snapshot, video_type, collection_kind, availability_status, published, None, at)
    seconds = rule.cadence_seconds if rule is not None else None
    return None if seconds is None else _add(checked, seconds)


@dataclass(frozen=True)
class UnavailableRetryPolicy:
    next_due_at: datetime | None
    stop_at: datetime
    stopped: bool


def unavailable_retry_policy(snapshot, video_type, first_confirmed_missing_at, now):
    """Schedule after a confirmed miss; retain the original episode boundary.

    A deadline at stop_at is a local stop transition, not permission to request
    the provider. Callers check stopped before making any unavailable retry.
    Unknown identities use the shared default, never an inferred Long type.
    """
    first, at = _reference_clock(first_confirmed_missing_at), _reference_clock(now)
    if first > at:
        raise PolicyError("video_unavailable_episode_clock_invalid")
    if video_type is None:
        policy = UnavailableSchedule()
    elif video_type in ("long", "short", "stream"):
        policy = getattr(snapshot.unavailable_by_type, video_type)
    else:
        raise PolicyError("video_unavailable_type_invalid")
    stop = _add(first, policy.stop_after_seconds)
    if at >= stop:
        return UnavailableRetryPolicy(None, stop, True)
    interval = (policy.daily_seconds if at < _add(first, policy.daily_window_seconds)
                else policy.weekly_seconds)
    return UnavailableRetryPolicy(min(_add(at, interval), stop), stop, False)


def typed_due_at(snapshot, video_type, video_id, age_anchor_at, checked_at, *, db_now):
    # SQL NULL NOT IN is unknown: NULL type reaches cadence_missing after the
    # source-clock branch. btrim(text) trims spaces, not all Unicode whitespace.
    if ((video_type is not None and video_type not in {"short", "stream"})
            or not isinstance(video_id, str) or video_id.strip(" ") == "" or checked_at is None):
        raise PolicyError("typed_video_collection_due_input_invalid")
    checked, now = clock(checked_at), _reference_clock(db_now)
    if isinstance(checked, Infinity) or _lt(now, checked):
        return now
    rule = select_policy(snapshot, video_type, "metadata", "all", age_anchor_at, checked, checked)
    if rule is None:
        raise PolicyError("typed_video_collection_cadence_missing")
    return _add(checked, typed_jittered_seconds(video_id, rule.cadence_seconds, rule.jitter_window_seconds))


def metadata_due_at(policy_due_at, availability_checked_at, as_of, *, db_now):
    due, available, at = map(clock, (policy_due_at, availability_checked_at, as_of))
    now = _reference_clock(db_now)
    if at is None or isinstance(at, Infinity) or _lt(now, at):
        raise PolicyError("hot_video_refresh_as_of_invalid")
    nominal = due if due is not None else at
    ceiling = at if available is None or isinstance(available, Infinity) or _lt(now, available) else _add(available, MAX_INTERVAL_SECONDS)
    return min((nominal, ceiling), key=_key)


@dataclass(frozen=True)
class InventorySourcePolicy:
    next_due_at: Clock
    no_new_streak: int
    retain_scan: bool


def _pg_int(value):
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise PolicyError("collection_policy_integer_out_of_range", sqlstate="22003")
    return value


def inventory_source_policy(video_type, scan_kind, outcome, new_count, no_new_streak,
                            attempt_count, error_count, error_code, now, *, schedule=None):
    if (video_type not in {"long", "short", "stream"}
            or scan_kind not in {"initial_inventory", "full_reconciliation", "discovery"}
            or outcome not in {"continuation", "complete", "empty", "boundary", "error", "playlist_not_found"}
            or now is None):
        raise PolicyError("inventory_policy_input_invalid")
    for value in (new_count, no_new_streak, attempt_count, error_count):
        if value is not None:
            _pg_int(value)
    # Pure callers retain the historical defaults. Transaction owners always
    # supply the validated immutable process snapshot; no file/env fallback.
    if schedule is None:
        schedule = getattr(InventorySchedules(), video_type)
    if type(schedule) is not InventorySchedule:
        raise PolicyError("inventory_schedule_invalid")
    at = clock(now)
    streak, retain = max(no_new_streak or 0, 0), False
    if outcome == "continuation":
        seconds = schedule.discovery_continuation_seconds if scan_kind == "discovery" else 0
        if video_type == "long" and scan_kind == "discovery" and (new_count or 0) > 0:
            streak = 0
    elif outcome in {"error", "playlist_not_found"}:
        retain = video_type == "long" and scan_kind == "full_reconciliation" and error_code not in _BAD_SCAN_ERRORS
        if outcome == "error" and error_code in {"provider_rate_limited", "youtuberatelimiterror"}:
            return InventorySourcePolicy(_add(at, 60), streak, True)
        if outcome == "playlist_not_found":
            seconds = schedule.missing_playlist_seconds
        else:
            attempt = attempt_count if attempt_count is not None else 1
            if video_type == "long":
                exponent = attempt if scan_kind == "full_reconciliation" else _pg_int((error_count or 0) + 1)
            else:
                exponent = attempt
            key = f"error_{max(exponent, 0)}_seconds" if exponent < 5 else "error_5_plus_seconds"
            seconds = getattr(schedule, key)
    elif video_type == "long" and scan_kind == "full_reconciliation":
        seconds = schedule.found_new_seconds
    elif (new_count or 0) > 0:
        streak, seconds = 0, schedule.found_new_seconds
    else:
        streak = _pg_int(streak + 1)
        key = f"quiet_{streak}_seconds" if streak < 6 else "quiet_6_plus_seconds"
        seconds = getattr(schedule, key)
    return InventorySourcePolicy(_add(at, seconds), streak, retain)


def quota_claim_allowed(last_error_code, next_due_at, source_next_after, checked_at):
    if last_error_code not in _QUOTA_ERRORS:
        return True
    due, source, checked = map(clock, (next_due_at, source_next_after, checked_at))
    return (_le(due if due is not None else Infinity.POSITIVE, checked)
            and _le(source if source is not None else Infinity.POSITIVE, checked))


def youtube_quota_reset_at(at):
    at = clock(at)
    if at is None or isinstance(at, Infinity):
        return at
    try:
        return next_pacific_midnight(at)
    except OverflowError:
        raise PolicyError("collection_policy_clock_out_of_range", sqlstate="22008") from None


def next_pacific_midnight(now):
    """The existing scenario helper's Pacific calendar arithmetic, moved down."""
    local = now.astimezone(YOUTUBE_QUOTA_TIMEZONE)
    midnight = datetime(local.year, local.month, local.day, tzinfo=YOUTUBE_QUOTA_TIMEZONE) + timedelta(days=1)
    return midnight.astimezone(timezone.utc)


@dataclass(frozen=True)
class FailurePolicy:
    retry_at: datetime
    consecutive_failures: int
    error_type: str | None


def failure_policy(*, now, consecutive_failures, error_type, retry_after_seconds, typed=False):
    """Inputs are native validated DB facts; trim uses PostgreSQL btrim semantics."""
    label = error_type.strip(" ") if error_type is not None else None
    quota = label == "youtube_quota_exhausted"
    if label == STATS_AVAILABILITY_CHECK_REQUIRED:
        # A successful Stats response cannot confirm availability. Hand off to
        # General at the next ordinary pass, without the accumulated backoff.
        return FailurePolicy(now + timedelta(seconds=30), consecutive_failures, label)
    if label == "provider_rate_limited":
        seconds = (min(60, max(1, retry_after_seconds))
                   if type(retry_after_seconds) is int else 60)
        return FailurePolicy(now + timedelta(seconds=seconds), consecutive_failures, label)
    if typed:
        seconds = 3600 if quota else min(86400, max(3600,
            retry_after_seconds if retry_after_seconds is not None else 3600*(1 << min(consecutive_failures,4))))
        label = label or "collection_error"
    else:
        requested = 30 if retry_after_seconds is None else retry_after_seconds
        seconds = requested if quota else max(requested,min(86400,30*(2**min(consecutive_failures,11))))
    retry_at = now + timedelta(seconds=seconds)
    if quota:
        retry_at = max(retry_at,youtube_quota_reset_at(now))
    return FailurePolicy(retry_at,consecutive_failures + (0 if quota else 1),
                         label[:120] if label is not None else None)
