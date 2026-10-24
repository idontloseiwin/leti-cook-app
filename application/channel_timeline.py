"""Typed assembly of honest stored channel-counter timelines."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from hot_storage import ANALYTICS_WINDOW, HOT_DATA_WINDOW, utc_now
from source_expiry import minimum_expiry, source_expiry


ChannelTimelineMetric = Literal["views", "subscribers"]
ChannelTimelinePeriod = Literal["1d", "7d", "28d", "3m", "1y", "max"]

PERIOD_WINDOWS: dict[ChannelTimelinePeriod, timedelta | None] = {
    "1d": timedelta(hours=24),
    "7d": timedelta(days=7),
    "28d": timedelta(days=28),
    "3m": timedelta(days=90),
    "1y": timedelta(days=365),
    "max": None,
}
SNAPSHOT_SCAN_LIMIT = 20_000
TIMELINE_POINT_LIMIT = 500
KEY_EVENT_LIMIT = 500
BOUNDARY_TOLERANCE = timedelta(hours=2)
LATEST_OBSERVATION_MAX_AGE = timedelta(hours=2)
SAFE_JSON_INTEGER = 2**53 - 1
SERIES_BUCKET_WINDOWS: dict[ChannelTimelinePeriod, timedelta] = {
    "1d": timedelta(hours=1),
    "7d": timedelta(days=1),
    "28d": timedelta(days=1),
    "3m": timedelta(days=1),
    "1y": timedelta(days=7),
    "max": timedelta(days=30),
}


class ChannelTimelineNotFoundError(LookupError):
    pass


class ChannelTimelineQueryError(ValueError):
    pass


def _utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _metric_rows(
    rows: list[dict[str, Any]],
    metric: ChannelTimelineMetric,
    *,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> list[tuple[datetime, int]]:
    normalized: dict[datetime, int] = {}
    for row in rows:
        observed_at = _utc(row.get("recorded_at"))
        value = _counter(row.get(metric))
        if observed_at is None or value is None:
            continue
        if not_before is not None and observed_at < not_before:
            continue
        if not_after is not None and observed_at > not_after:
            continue
        normalized[observed_at] = value
    return sorted(normalized.items())


def _series_metric_rows(
    rows: list[dict[str, Any]],
    metric: ChannelTimelineMetric,
    *,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> list[tuple[datetime, int, datetime]]:
    normalized: dict[datetime, tuple[int, datetime]] = {}
    for row in rows:
        observed_at = _utc(row.get("recorded_at"))
        bucket_at = _utc(row.get("bucket_at"))
        value = _counter(row.get(metric))
        if observed_at is None or bucket_at is None or value is None:
            continue
        if not_before is not None and observed_at < not_before:
            continue
        if not_after is not None and observed_at > not_after:
            continue
        normalized[observed_at] = (value, bucket_at)
    return [
        (observed_at, value, bucket_at)
        for observed_at, (value, bucket_at) in sorted(normalized.items())
    ]


def _bucket_start(
    observed_at: datetime,
    *,
    origin: datetime,
    bucket_seconds: int,
) -> datetime:
    bucket_index = int((observed_at - origin).total_seconds()) // bucket_seconds
    return origin + timedelta(seconds=bucket_index * bucket_seconds)


def _series_bucket_seconds(
    period: ChannelTimelinePeriod,
    *,
    generated_at: datetime,
    baseline: tuple[datetime, int] | None,
) -> int:
    base_seconds = int(SERIES_BUCKET_WINDOWS[period].total_seconds())
    if period != "max" or baseline is None:
        return base_seconds
    span_seconds = max(0, int((generated_at - baseline[0]).total_seconds()))
    required_seconds = max(1, (span_seconds + TIMELINE_POINT_LIMIT - 2) // (
        TIMELINE_POINT_LIMIT - 1
    ))
    multiples = max(1, (required_seconds + base_seconds - 1) // base_seconds)
    return min(365 * 24 * 60 * 60, multiples * base_seconds)


def _timeline_points(
    observations: list[tuple[datetime, int, datetime]],
    *,
    expected_interval_seconds: int,
    correction_times: set[datetime],
    source_expiries: dict[datetime, str | None],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    previous: tuple[datetime, int, datetime] | None = None
    for observed_at, value, bucket_at in observations:
        delta = None if previous is None else value - previous[1]
        interval_seconds = (
            None
            if previous is None
            else max(0, int((observed_at - previous[0]).total_seconds()))
        )
        bucket_interval_seconds = (
            None
            if previous is None
            else max(0, int((bucket_at - previous[2]).total_seconds()))
        )
        gap_from_previous = (
            bucket_interval_seconds is not None
            and bucket_interval_seconds > expected_interval_seconds
        )
        points.append({
            "source_expires_at": minimum_expiry(source_expiries.get(observed_at),
                                                 previous[0] + HOT_DATA_WINDOW if previous else None),
            "bucket_at": _iso(bucket_at),
            "observed_at": _iso(observed_at),
            "value": _json_integer(value),
            "delta": _json_integer(delta),
            "interval_seconds": interval_seconds,
            "gap_from_previous": gap_from_previous,
            "correction_within_bucket": observed_at in correction_times,
        })
        previous = (observed_at, value, bucket_at)
    return points


def _publication_events(
    repository: Any,
    channel_id: str,
    *,
    requested_start: datetime | None,
    requested_end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    rows = repository.load_publication_events(
        channel_id,
        published_from=_iso(requested_start),
        published_through=_iso(requested_end),
        limit=KEY_EVENT_LIMIT + 1,
    )
    truncated = len(rows) > KEY_EVENT_LIMIT
    if truncated:
        rows = rows[-KEY_EVENT_LIMIT:]
    valid: list[tuple[str, datetime, str | None]] = []
    for row in rows:
        video_id = str(row.get("video_id") or "").strip()
        published_at = _utc(row.get("published_at"))
        if not video_id or published_at is None:
            continue
        if requested_start is not None and published_at < requested_start:
            continue
        if published_at > requested_end:
            continue
        valid.append((video_id, published_at, source_expiry(row)))
    titles = repository.load_video_titles([video_id for video_id, _, _ in valid])
    return [
        {
            "video_id": video_id,
            "title": titles.get(video_id, {}).get("title"),
            "source_expires_at": minimum_expiry(expiry, source_expiry(titles.get(video_id))),
            "published_at": _iso(published_at),
        }
        for video_id, published_at, expiry in valid
    ], truncated


def build_channel_timeline(
    repository: Any,
    channel_id: str,
    *,
    metric: str,
    period: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one rolling timeline from stored observations without interpolation."""

    normalized_metric = str(metric or "").strip().lower()
    if normalized_metric not in {"views", "subscribers"}:
        raise ChannelTimelineQueryError("metric must be views or subscribers")
    normalized_period = str(period or "").strip().lower()
    if normalized_period not in PERIOD_WINDOWS:
        raise ChannelTimelineQueryError(
            "period must be one of 1d, 7d, 28d, 3m, 1y, max"
        )
    selected_metric: ChannelTimelineMetric = normalized_metric  # type: ignore[assignment]
    selected_period: ChannelTimelinePeriod = normalized_period  # type: ignore[assignment]

    generated_at = now or utc_now()
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    channel = repository.load_channel(channel_id)
    if not channel or channel.get("is_hidden"):
        raise ChannelTimelineNotFoundError("Channel not found")

    window = PERIOD_WINDOWS[selected_period]
    requested_start = generated_at - window if window is not None else None
    hot_start = generated_at - ANALYTICS_WINDOW
    available_start = max(requested_start, hot_start) if requested_start else hot_start
    retention_limited = requested_start is None or requested_start < hot_start
    baseline_candidates = repository.load_snapshot_edge_candidates(
        channel_id,
        metric=selected_metric,
        at_or_before=_iso(requested_start or generated_at),
        descending=requested_start is not None,
        limit=1,
    )
    baseline_rows = _metric_rows(
        baseline_candidates,
        selected_metric,
        not_before=hot_start,
        not_after=requested_start or generated_at,
    )
    baseline = (
        baseline_rows[-1]
        if requested_start is not None and baseline_rows
        else baseline_rows[0] if baseline_rows else None
    )

    series_bucket_seconds = _series_bucket_seconds(
        "28d" if retention_limited else selected_period,
        generated_at=generated_at,
        baseline=baseline,
    )
    series_limit = TIMELINE_POINT_LIMIT - (1 if baseline is not None else 0)

    raw_window_rows, scan_truncated, scanned_snapshot_count = (
        repository.load_snapshot_window(
            channel_id,
            metric=selected_metric,
            recorded_from=_iso(available_start),
            recorded_through=_iso(generated_at),
            bucket_seconds=series_bucket_seconds,
            scan_limit=SNAPSHOT_SCAN_LIMIT,
            limit=series_limit,
        )
    )
    series_truncated = len(raw_window_rows) > series_limit
    if series_truncated:
        raw_window_rows = raw_window_rows[-series_limit:]
    in_window = _series_metric_rows(
        raw_window_rows,
        selected_metric,
        not_before=available_start,
        not_after=generated_at,
    )
    correction_times = {
        observed_at
        for row in raw_window_rows
        if row.get("correction_within_bucket") is True
        for observed_at in [_utc(row.get("recorded_at"))]
        if observed_at is not None
    }

    observations_by_time = {
        observed_at: (observed_at, value, bucket_at)
        for observed_at, value, bucket_at in in_window
    }
    if baseline is not None:
        bucket_origin = requested_start or datetime(1970, 1, 1, tzinfo=timezone.utc)
        baseline_bucket = (
            requested_start - timedelta(seconds=series_bucket_seconds)
            if requested_start is not None and baseline[0] < requested_start
            else _bucket_start(
                baseline[0],
                origin=bucket_origin,
                bucket_seconds=series_bucket_seconds,
            )
        )
        observations_by_time.setdefault(
            baseline[0],
            (baseline[0], baseline[1], baseline_bucket),
        )
    observations = sorted(observations_by_time.values())
    start_observation = baseline
    end_observation = observations[-1] if observations else None

    reason: str | None = None
    if retention_limited:
        reason = "Only the most recent 28 days of observations are available in hot storage."
    elif baseline is None:
        reason = (
            "Stored history has not reached the start of this rolling window."
            if requested_start is not None
            else "At least two stored observations are required."
        )
    elif requested_start is not None and requested_start - baseline[0] > BOUNDARY_TOLERANCE:
        reason = "The nearest stored baseline is too far from the rolling-window boundary."
    elif end_observation is None or end_observation[0] <= baseline[0]:
        reason = "At least two distinct metric observations are required."
    elif generated_at - end_observation[0] > LATEST_OBSERVATION_MAX_AGE:
        reason = "The latest stored metric observation is stale."

    status = "not_enough_data" if reason else "ready"
    if status == "ready":
        assert start_observation is not None and end_observation is not None
        actual_interval_seconds = max(
            0,
            int((end_observation[0] - start_observation[0]).total_seconds()),
        )
        total_delta = end_observation[1] - start_observation[1]
        start_value = start_observation[1]
        end_value = end_observation[1]
    else:
        actual_interval_seconds = (
            max(0, int((end_observation[0] - baseline[0]).total_seconds()))
            if baseline is not None and end_observation is not None
            else None
        )
        total_delta = None
        start_value = baseline[1] if baseline is not None else None
        end_value = end_observation[1] if end_observation is not None else None

    points = _timeline_points(
        observations,
        expected_interval_seconds=series_bucket_seconds,
        correction_times=correction_times,
        source_expiries={_utc(row.get("recorded_at")): source_expiry(row, "recorded_at")
                         for row in [*baseline_candidates, *raw_window_rows]},
    )
    series_gap_count = sum(
        1 for point in points if point["gap_from_previous"] is True
    )
    series_complete = (
        status == "ready"
        and not scan_truncated
        and not series_truncated
        and series_gap_count == 0
        and len(points) >= 2
    )
    series_reason = None
    if scan_truncated:
        series_reason = "The bounded snapshot scan did not cover the full period."
    elif series_truncated:
        series_reason = "The bounded bucket series exceeded the returned point limit."
    elif series_gap_count:
        series_reason = (
            f"{series_gap_count} stored-history gap"
            f"{'s' if series_gap_count != 1 else ''} break the plotted series."
        )
    elif status != "ready":
        series_reason = reason

    key_events, events_truncated = _publication_events(
        repository,
        channel_id,
        requested_start=available_start,
        requested_end=generated_at,
    )
    first_snapshot_at = baseline[0] if baseline is not None else (
        observations[0][0] if observations else None
    )
    last_snapshot_at = observations[-1][0] if observations else None
    latest_age_seconds = (
        max(0, int((generated_at - last_snapshot_at).total_seconds()))
        if last_snapshot_at is not None
        else None
    )

    baseline_extra = 0
    if baseline is not None and (
        (requested_start is not None and baseline[0] < requested_start)
        or (requested_start is None and scan_truncated)
    ):
        baseline_extra = 1
    observation_count = max(len(observations), scanned_snapshot_count + baseline_extra)
    expected_start = available_start
    expected_point_count = (
        max(
            2,
            int((generated_at - expected_start).total_seconds())
            // max(1, series_bucket_seconds)
            + 1,
        )
        if expected_start is not None
        else 0
    )

    return {
        "contract_version": 1,
        "channel_id": channel_id,
        "metric": selected_metric,
        "period": selected_period,
        "generated_at": _iso(generated_at),
        "range": {
            "requested_start_at": _iso(requested_start),
            "requested_end_at": _iso(generated_at),
            "available_start_at": _iso(available_start),
            "observed_start_at": _iso(baseline[0]) if baseline else None,
            "observed_end_at": _iso(end_observation[0]) if end_observation else None,
            "actual_interval_seconds": actual_interval_seconds,
        },
        "summary": {
            "status": status,
            "start_value": _json_integer(start_value),
            "end_value": _json_integer(end_value),
            "total_delta": _json_integer(total_delta),
            "observation_count": observation_count,
        },
        "points": points,
        "key_events": key_events,
        "coverage": {
            "snapshot_count": observation_count,
            "required_snapshot_count": 2,
            "first_snapshot_at": _iso(first_snapshot_at),
            "last_snapshot_at": _iso(last_snapshot_at),
            "complete": series_complete,
            "retention_limited": retention_limited,
            "retained_days": ANALYTICS_WINDOW.days,
            "reason": reason,
            "series_complete": series_complete,
            "series_reason": series_reason,
            "series_gap_count": series_gap_count,
            "series_truncated": series_truncated,
            "expected_point_count": expected_point_count,
            "scan_limit": SNAPSHOT_SCAN_LIMIT,
            "scan_truncated": scan_truncated,
            "returned_point_count": len(points),
            "key_events_truncated": events_truncated,
        },
        "freshness": {
            "latest_observation_at": _iso(last_snapshot_at),
            "latest_observation_age_seconds": latest_age_seconds,
            "maximum_age_seconds": int(LATEST_OBSERVATION_MAX_AGE.total_seconds()),
            "current": (
                latest_age_seconds is not None
                and latest_age_seconds <= LATEST_OBSERVATION_MAX_AGE.total_seconds()
            ),
        },
        "methods": {
            "period_semantics": "rolling_window",
            "timezone": "UTC",
            "interpolation": "none",
            "series_value": "interval_delta",
            "series_bucket": "latest_real_observation_per_rolling_bucket",
            "series_bucket_seconds": series_bucket_seconds,
            "gap_policy": "break_when_bucket_index_skips_expected_bucket",
            "negative_deltas": "preserved_as_counter_corrections",
            "within_bucket_corrections": "disclosed_when_observed",
            "baseline": (
                "latest_metric_observation_at_or_before_window_start"
                if requested_start is not None
                else "earliest_stored_metric_observation"
            ),
            "boundary_tolerance_seconds": int(BOUNDARY_TOLERANCE.total_seconds()),
        },
        "source": {
            "kind": "derived_from_stored_channel_snapshots",
            "youtube_requests": False,
            "database_writes": False,
            "snapshots_used": True,
            "publication_events": "stored_typed_long_inventory",
        },
    }
