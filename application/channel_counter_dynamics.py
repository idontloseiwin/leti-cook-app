"""Read-only assembly of fixed 28-day channel-counter dynamics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from source_expiry import source_expiry, minimum_expiry
from typing import Any, Literal

from hot_storage import ANALYTICS_WINDOW, utc_now, utc_instant, source_is_current


CounterMetric = Literal["subscribers", "views"]
CounterPeriod = Literal["7d", "28d"]

COUNTER_METRICS: tuple[CounterMetric, ...] = ("subscribers", "views")
COUNTER_DYNAMICS_WINDOW = ANALYTICS_WINDOW
COUNTER_DYNAMICS_WINDOWS: tuple[tuple[CounterPeriod, timedelta], ...] = (
    ("7d", timedelta(days=7)),
    ("28d", COUNTER_DYNAMICS_WINDOW),
)
COUNTER_DYNAMICS_CURRENT_MAX_AGE = timedelta(hours=2)
# Updated header comparisons accept one real observation per day. The fixed
# Previous endpoint retains its original limits for compatibility.
COUNTER_DYNAMICS_DAILY_TOLERANCE = timedelta(days=1)
SAFE_JSON_INTEGER = 2**53 - 1


class ChannelCounterDynamicsNotFoundError(LookupError):
    pass


def _utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return utc_instant(value)
    except (TypeError, ValueError):
        return None



def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _counter(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdigit():
            return int(normalized)
    return None


def _json_integer(value: int | None) -> int | str | None:
    if value is None:
        return None
    return str(value) if abs(value) > SAFE_JSON_INTEGER else value


def _metric_result(
    metric: CounterMetric,
    *,
    latest: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    generated_at: datetime,
    boundary_at: datetime,
    period_label: str = "28-day",
    current_max_age: timedelta = COUNTER_DYNAMICS_CURRENT_MAX_AGE,
    boundary_tolerance: timedelta = timedelta(0),
) -> dict[str, Any]:
    empty = {
        "source_expires_at": None,
        "status": "not_reported",
        "coverage": "unavailable",
        "start_value": None,
        "end_value": None,
        "absolute_change": None,
        "percent_change": None,
        "observed_start_at": None,
        "observed_end_at": None,
        "actual_interval_seconds": None,
        "reason": "No stored channel observation reports this counter.",
    }
    # Defense in depth: an expired observation may not escape in an error payload.
    if latest is not None:
        latest_at = _utc(latest.get("recorded_at"))
        if latest_at is None or not source_is_current(latest_at, now=generated_at):
            latest = None
    if latest is None:
        return empty

    empty["source_expires_at"] = source_expiry(latest, "recorded_at")
    end_at = _utc(latest.get("recorded_at"))
    raw_end = latest.get(metric)
    end_value = _counter(raw_end)
    end_at_text = _iso(end_at)
    if raw_end is None:
        return {
            **empty,
            "observed_end_at": end_at_text,
            "reason": (
                "The latest stored channel observation does not report a public "
                "subscriber count."
                if metric == "subscribers"
                else "The latest stored channel observation does not report this counter."
            ),
        }
    if end_value is None or end_at is None:
        return {
            **empty,
            "status": "unavailable",
            "end_value": _json_integer(end_value),
            "observed_end_at": end_at_text,
            "reason": "The latest stored counter observation is invalid.",
        }
    if end_at > generated_at:
        return {
            **empty,
            "status": "unavailable",
            "end_value": _json_integer(end_value),
            "observed_end_at": end_at_text,
            "reason": "The latest stored counter observation is in the future.",
        }
    if generated_at - end_at > current_max_age:
        return {
            **empty,
            "status": "stale_current",
            "end_value": _json_integer(end_value),
            "observed_end_at": end_at_text,
            "reason": (
                "The latest stored counter observation is more than "
                f"{int(current_max_age.total_seconds() // 3600)} hours old."
            ),
        }

    if baseline is not None:
        baseline_at = _utc(baseline.get("recorded_at"))
        if baseline_at is None or not source_is_current(baseline_at, now=generated_at):
            baseline = None
    if baseline is None:
        return {
            **empty,
            "status": "not_enough_history",
            "end_value": _json_integer(end_value),
            "observed_end_at": end_at_text,
            "reason": (
                f"No known observation is available inside the retained {period_label} window "
                "for this counter."
            ),
        }

    empty["source_expires_at"] = minimum_expiry(empty["source_expires_at"], source_expiry(baseline, "recorded_at"))
    start_at = _utc(baseline.get("recorded_at"))
    raw_start = baseline.get(metric)
    start_value = _counter(raw_start)
    start_at_text = _iso(start_at)
    if raw_start is None or start_value is None or start_at is None:
        return {
            **empty,
            "status": "unavailable",
            "start_value": _json_integer(start_value),
            "end_value": _json_integer(end_value),
            "observed_start_at": start_at_text,
            "observed_end_at": end_at_text,
            "reason": f"The stored {period_label} baseline observation is invalid.",
        }
    if start_at < boundary_at:
        return {
            **empty,
            "status": "unavailable",
            "start_value": _json_integer(start_value),
            "end_value": _json_integer(end_value),
            "observed_start_at": start_at_text,
            "observed_end_at": end_at_text,
            "reason": f"The stored baseline is before the {period_label} boundary.",
        }

    interval_seconds = (
        max(0, int((end_at - start_at).total_seconds()))
        if end_at >= start_at
        else None
    )
    common = {
        "start_value": _json_integer(start_value),
        "end_value": _json_integer(end_value),
        "observed_start_at": start_at_text,
        "observed_end_at": end_at_text,
        "actual_interval_seconds": interval_seconds,
    }
    if start_at >= end_at:
        return {
            **empty,
            **common,
            "status": "not_enough_history",
            "reason": "Two distinct stored counter observations are required.",
        }

    # Updated accepts daily sampling near both window boundaries. The requested
    # 7/28-day window and actual counter arithmetic remain unchanged.
    covered = (start_at - boundary_at <= boundary_tolerance
               and generated_at - end_at <= boundary_tolerance)
    coverage = "full" if covered else "partial"
    common["coverage"] = coverage
    absolute_change = end_value - start_value
    if start_value == 0:
        return {
            **empty,
            **common,
            "status": "zero_baseline",
            "absolute_change": _json_integer(absolute_change),
            "reason": (
                "Percentage change is unavailable because the "
                f"{period_label} baseline is zero."
            ),
        }

    percent_change = round(absolute_change * 100 / start_value, 6)
    return {
        **empty,
        **common,
        "status": "ready" if coverage == "full" else "partial",
        "absolute_change": _json_integer(absolute_change),
        "percent_change": percent_change,
        "reason": None if coverage == "full" else "Change covers only the actual retained observation interval.",
    }


def build_channel_counter_dynamics(
    repository: Any,
    channel_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one fixed 28-day snapshot comparison without external I/O or writes."""

    generated_at = now or utc_now()
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)
    boundary_at = generated_at - COUNTER_DYNAMICS_WINDOW

    channel = repository.load_channel(channel_id)
    if not channel or channel.get("is_hidden"):
        raise ChannelCounterDynamicsNotFoundError("Channel not found")

    latest = repository.load_latest_snapshot(
        channel_id,
        recorded_through=_iso(generated_at),
    )
    baselines = {
        metric: repository.load_metric_baseline(
            channel_id,
            metric=metric,
            recorded_from=_iso(boundary_at),
            recorded_through=_iso(generated_at),
        )
        for metric in COUNTER_METRICS
    }

    return {
        "contract_version": 1,
        "channel_id": channel_id,
        "period": "28d",
        "generated_at": _iso(generated_at),
        "metrics": {
            metric: _metric_result(
                metric,
                latest=latest,
                baseline=baselines[metric],
                generated_at=generated_at,
                boundary_at=boundary_at,
            )
            for metric in COUNTER_METRICS
        },
        "methods": {
            "period_semantics": "actual_interval_within_rolling_window",
            "window_seconds": int(COUNTER_DYNAMICS_WINDOW.total_seconds()),
            "boundary": "earliest_known_metric_at_or_after_window_start",
            "boundary_tolerance_seconds": 0,
            "current": "latest_stored_channel_snapshot_at_or_before_generated_at",
            "current_max_age_seconds": int(
                COUNTER_DYNAMICS_CURRENT_MAX_AGE.total_seconds()
            ),
            "percent_change": "(end_value-start_value)/start_value*100",
            "counter_corrections": "preserved_as_signed_changes",
            "interpolation": "none",
            "timezone": "UTC",
        },
        "source": {
            "kind": "derived_from_stored_channel_snapshots",
            "youtube_requests": False,
            "database_writes": False,
            "snapshots_used": True,
        },
    }


def build_channel_counter_dynamics_periods(
    repository: Any,
    channel_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build rolling 7/28-day comparisons accepting daily real observations."""

    generated_at = now or utc_now()
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    channel = repository.load_channel(channel_id)
    if not channel or channel.get("is_hidden"):
        raise ChannelCounterDynamicsNotFoundError("Channel not found")

    latest = repository.load_latest_snapshot(
        channel_id,
        recorded_through=_iso(generated_at),
    )
    periods: dict[CounterPeriod, dict[str, Any]] = {}
    for period, window in COUNTER_DYNAMICS_WINDOWS:
        boundary_at = generated_at - window
        baselines = {
            metric: repository.load_metric_baseline(
                channel_id,
                metric=metric,
                recorded_from=_iso(boundary_at),
                recorded_through=_iso(generated_at),
            )
            for metric in COUNTER_METRICS
        }
        period_label = f"{int(window.total_seconds() // 86400)}-day"
        periods[period] = {
            "period": period,
            "window_seconds": int(window.total_seconds()),
            "metrics": {
                metric: _metric_result(
                    metric,
                    latest=latest,
                    baseline=baselines[metric],
                    generated_at=generated_at,
                    boundary_at=boundary_at,
                    period_label=period_label,
                    current_max_age=COUNTER_DYNAMICS_DAILY_TOLERANCE,
                    boundary_tolerance=COUNTER_DYNAMICS_DAILY_TOLERANCE,
                )
                for metric in COUNTER_METRICS
            },
        }

    return {
        "contract_version": 1,
        "channel_id": channel_id,
        "generated_at": _iso(generated_at),
        "periods": periods,
        "methods": {
            "period_semantics": "actual_intervals_within_rolling_windows",
            "boundary": "earliest_known_metric_at_or_after_window_start",
            "boundary_tolerance_seconds": int(COUNTER_DYNAMICS_DAILY_TOLERANCE.total_seconds()),
            "current": "latest_stored_channel_snapshot_at_or_before_generated_at",
            "current_max_age_seconds": int(
                COUNTER_DYNAMICS_DAILY_TOLERANCE.total_seconds()
            ),
            "percent_change": "(end_value-start_value)/start_value*100",
            "counter_corrections": "preserved_as_signed_changes",
            "interpolation": "none",
            "timezone": "UTC",
        },
        "source": {
            "kind": "derived_from_stored_channel_snapshots",
            "youtube_requests": False,
            "database_writes": False,
            "snapshots_used": True,
        },
    }
