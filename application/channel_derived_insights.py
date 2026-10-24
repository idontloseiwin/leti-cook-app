"""Pure bounded change analytics from stored video snapshot observations."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from source_expiry import source_expiry, minimum_expiry, rows_expiry, stamp_aggregate
from typing import Any

from hot_storage import ANALYTICS_WINDOW, source_is_current, utc_instant, utc_now

from channel_explorer import (
    DB_BATCH_SIZE,
    RAW_DATA_MAX_AGE,
    SCAN_LIMIT,
    ChannelExplorerNotFoundError,
    ChannelExplorerQueryError,
    _is_current,
    _matches,
    _normalized_query,
    _number,
    _timestamp,
)


CHANNEL_COLUMNS = "channel_id,is_hidden"
VIDEO_COLUMNS = (
    "video_id,title,published_at,checked_at,metadata_checked_at,duration,views,"
    "like_count,comment_count,category_id,availability_status,availability_checked_at"
)
FILTER_KEYS = (
    "q",
    "category_id",
    "published_from",
    "published_before",
    "min_views",
    "max_views",
    "min_duration_seconds",
    "max_duration_seconds",
)

SELECTED_VIDEO_LIMIT = 500
SNAPSHOT_BATCH_SIZE = 50
SNAPSHOT_LOOKBACK = ANALYTICS_WINDOW
SNAPSHOT_ROW_LIMIT = 20_000
SAFE_JSON_INTEGER = 2**53 - 1


def _rounded(value: int | float | Fraction | None) -> int | float | str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value) if abs(value) > SAFE_JSON_INTEGER else value
    if isinstance(value, Fraction):
        rounded_fraction = round(value, 2)
        if rounded_fraction.denominator == 1:
            integer = rounded_fraction.numerator
            return str(integer) if abs(integer) > SAFE_JSON_INTEGER else integer
        if abs(rounded_fraction) > SAFE_JSON_INTEGER:
            with localcontext() as context:
                context.prec = max(28, len(str(abs(rounded_fraction.numerator))) + 4)
                rendered = format(
                    Decimal(rounded_fraction.numerator)
                    / Decimal(rounded_fraction.denominator),
                    "f",
                )
            return rendered.rstrip("0").rstrip(".")
        return float(rounded_fraction)
    rounded = round(float(value), 2)
    return int(rounded) if rounded.is_integer() else rounded


def _quantile(values: list[int | Fraction], fraction: Fraction) -> int | float | str | None:
    if not values:
        return None
    ordered = sorted(Fraction(value) for value in values)
    position = Fraction(len(ordered) - 1) * fraction
    lower = position.numerator // position.denominator
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return _rounded(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _public_filters(query: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _iso(query[key]) if isinstance(query[key], datetime) else query[key]
        for key in FILTER_KEYS
    }


def _elapsed_microseconds(earlier: datetime, latest: datetime) -> int:
    elapsed = latest - earlier
    return (
        elapsed.days * 86_400_000_000
        + elapsed.seconds * 1_000_000
        + elapsed.microseconds
    )


def _snapshot_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _source_counter(value: Any) -> int | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _latest_distinct_pair(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    valid: list[tuple[datetime, int, dict[str, Any]]] = []
    for row in rows:
        try:
            recorded_at = utc_instant(row.get("recorded_at"))
        except (TypeError, ValueError, OverflowError):
            continue
        row_id = _snapshot_id(row.get("id"))
        if (
            recorded_at is None
            or row_id is None
            or not generated_at - SNAPSHOT_LOOKBACK <= recorded_at <= generated_at
        ):
            continue
        valid.append((recorded_at, row_id, row))
    valid.sort(key=lambda item: (item[0], item[1]), reverse=True)

    distinct: list[tuple[datetime, int, dict[str, Any]]] = []
    for item in valid:
        if distinct and item[0] == distinct[-1][0]:
            continue
        distinct.append(item)
        if len(distinct) == 2:
            latest, earlier = distinct
            return earlier[2], latest[2]
    return None


def _metric_change(
    earlier: dict[str, Any],
    latest: dict[str, Any],
    *,
    field: str,
    elapsed_microseconds: int,
) -> dict[str, Any] | None:
    earlier_value = _source_counter(earlier.get(field))
    latest_value = _source_counter(latest.get(field))
    if earlier_value is None or latest_value is None:
        return None
    delta = latest_value - earlier_value
    if delta < 0:
        direction = "correction"
    elif delta > 0:
        direction = "increase"
    else:
        direction = "unchanged"
    return {
        "earlier": _rounded(earlier_value),
        "latest": _rounded(latest_value),
        "delta": _rounded(delta),
        "per_day": _rounded(Fraction(delta * 86_400_000_000, elapsed_microseconds)),
        "direction": direction,
    }


def _summary(values: list[int | Fraction]) -> dict[str, Any]:
    if not values:
        return {
            "min": None,
            "mean": None,
            "median": None,
            "max": None,
        }
    total = sum(values, Fraction())
    return {
        "min": _rounded(min(values)),
        "mean": _rounded(total / len(values)),
        "median": _quantile(values, Fraction(1, 2)),
        "max": _rounded(max(values)),
    }


def _metric_summary(changes: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    values = [change[metric] for change in changes if change[metric] is not None]
    deltas = [int(value["latest"]) - int(value["earlier"]) for value in values]
    velocities = [
        Fraction(
            delta * 86_400_000_000,
            int(change["_elapsed_microseconds"]),
        )
        for change in changes
        if (value := change[metric]) is not None
        for delta in [int(value["latest"]) - int(value["earlier"])]
    ]
    delta_total = sum(deltas)
    return {
        "known_count": len(values),
        "missing_count": len(changes) - len(values),
        "increase_count": sum(delta > 0 for delta in deltas),
        "unchanged_count": sum(delta == 0 for delta in deltas),
        "correction_count": sum(delta < 0 for delta in deltas),
        "total_delta": _rounded(delta_total) if deltas else None,
        "mean_delta": _rounded(Fraction(delta_total, len(deltas))) if deltas else None,
        "median_delta": _quantile(deltas, Fraction(1, 2)),
        "mean_per_day": _rounded(sum(velocities, Fraction()) / len(velocities))
        if velocities
        else None,
        "median_per_day": _quantile(velocities, Fraction(1, 2)),
    }


def _freshness(changes: list[dict[str, Any]]) -> dict[str, str | None]:
    values = sorted(
        value
        for change in changes
        if (value := _timestamp(change.get("latest_at"))) is not None
    )
    return {
        "oldest": _iso(values[0]) if values else None,
        "newest": _iso(values[-1]) if values else None,
    }


def build_channel_derived_insights(
    repository: Any,
    channel_id: str,
    *,
    q: Any = "",
    category_id: Any = "",
    published_from: Any = "",
    published_before: Any = "",
    min_views: Any = None,
    max_views: Any = None,
    min_duration_seconds: Any = None,
    max_duration_seconds: Any = None,
    catalogue_filters: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build filter-aware observed changes without writes or provider requests."""

    if catalogue_filters is not None:
        raise ChannelExplorerQueryError("Catalogue conditions are not supported by latest-pair insights")

    query = _normalized_query(
        q=q,
        category_id=category_id,
        published_from=published_from,
        published_before=published_before,
        min_views=min_views,
        max_views=max_views,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        sort_by="published_at",
        sort_order="desc",
        limit=1,
        offset=0,
    )
    generated_at = now or utc_now()
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)
    lookback_started_at = generated_at - SNAPSHOT_LOOKBACK

    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")

    inventory = repository.load_long_inventory(channel_id, limit=SCAN_LIMIT + 1)
    inventory_truncated = len(inventory) > SCAN_LIMIT
    inventory = inventory[:SCAN_LIMIT]
    inventory = [
        row for row in inventory
        if source_is_current(row.get("source_checked_at"), now=generated_at)
    ]
    video_ids = [str(row.get("video_id")) for row in inventory if row.get("video_id")]
    inventory_ids = set(video_ids)
    inventory_expiries = {str(item.get("video_id")): source_expiry(item, "source_checked_at") for item in inventory}

    loaded_rows: list[dict[str, Any]] = []
    for start in range(0, len(video_ids), DB_BATCH_SIZE):
        loaded_rows.extend(
            repository.load_videos_by_ids(
                video_ids[start:start + DB_BATCH_SIZE],
                columns=VIDEO_COLUMNS,
            )
        )
    rows_by_id = {
        str(row.get("video_id")): dict(row)
        for row in loaded_rows
        if str(row.get("video_id") or "") in inventory_ids
    }
    loaded = [rows_by_id[video_id] for video_id in video_ids if video_id in rows_by_id]
    current_rows = [
        row
        for row in loaded
        if _is_current(row.get("checked_at"), now=generated_at, maximum_age=RAW_DATA_MAX_AGE)
        and _is_current(
            row.get("metadata_checked_at"),
            now=generated_at,
            maximum_age=RAW_DATA_MAX_AGE,
        )
    ]
    eligible_rows = [
        row
        for row in current_rows
        if str(row.get("availability_status") or "").strip().lower() == "public"
        and source_is_current(row.get("availability_checked_at"), now=generated_at)
    ]
    matched_rows = [row for row in eligible_rows if _matches(row, query)]
    selected_rows = matched_rows[:SELECTED_VIDEO_LIMIT]
    selected_ids = [str(row["video_id"]) for row in selected_rows]

    snapshots: list[dict[str, Any]] = []
    cap_reached = False
    processed_batches = 0
    for start in range(0, len(selected_ids), SNAPSHOT_BATCH_SIZE):
        remaining = SNAPSHOT_ROW_LIMIT - len(snapshots)
        if remaining <= 0:
            cap_reached = True
            break
        batch_ids = selected_ids[start:start + SNAPSHOT_BATCH_SIZE]
        batch = repository.load_snapshots_by_video_ids(
            batch_ids,
            recorded_from=lookback_started_at,
            recorded_through=generated_at,
            limit=remaining,
        )
        processed_batches += 1
        snapshots.extend(dict(row) for row in batch[:remaining])
        if len(batch) >= remaining:
            cap_reached = True
            break

    snapshots_by_video: dict[str, list[dict[str, Any]]] = {
        video_id: [] for video_id in selected_ids
    }
    for row in snapshots:
        video_id = str(row.get("video_id") or "")
        if video_id in snapshots_by_video:
            snapshots_by_video[video_id].append(row)

    changes: list[dict[str, Any]] = []
    for row in selected_rows:
        video_id = str(row["video_id"])
        pair = _latest_distinct_pair(
            snapshots_by_video.get(video_id, []),
            generated_at=generated_at,
        )
        if pair is None:
            continue
        earlier, latest = pair
        if not all(
            source_is_current(observation.get("recorded_at"), now=generated_at)
            for observation in pair
        ):
            continue
        earlier_at = _timestamp(earlier.get("recorded_at"))
        latest_at = _timestamp(latest.get("recorded_at"))
        if earlier_at is None or latest_at is None:
            continue
        elapsed_microseconds = _elapsed_microseconds(earlier_at, latest_at)
        if elapsed_microseconds <= 0:
            continue
        change = {
            "source_expires_at": minimum_expiry(
                source_expiry(row, "checked_at", "metadata_checked_at", "availability_checked_at"),
                inventory_expiries.get(video_id), source_expiry(earlier, "recorded_at"), source_expiry(latest, "recorded_at")),
            "video_id": video_id,
            "title": str(row.get("title") or ""),
            "published_at": _iso(published)
            if (published := _timestamp(row.get("published_at"))) is not None
            else None,
            "earlier_at": _iso(earlier_at),
            "latest_at": _iso(latest_at),
            "elapsed_hours": _rounded(Fraction(elapsed_microseconds, 3_600_000_000)),
            "views": _metric_change(
                earlier,
                latest,
                field="views",
                elapsed_microseconds=elapsed_microseconds,
            ),
            "likes": _metric_change(
                earlier,
                latest,
                field="like_count",
                elapsed_microseconds=elapsed_microseconds,
            ),
            "comments": _metric_change(
                earlier,
                latest,
                field="comment_count",
                elapsed_microseconds=elapsed_microseconds,
            ),
            "_elapsed_microseconds": elapsed_microseconds,
        }
        changes.append(change)

    changes.sort(key=lambda change: str(change["video_id"]))
    changes.sort(
        key=lambda change: _timestamp(change.get("latest_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    intervals = [
        Fraction(int(change["_elapsed_microseconds"]), 3_600_000_000)
        for change in changes
    ]
    interval_stats = _summary(intervals)
    metric_summaries = {
        metric: _metric_summary(changes, metric)
        for metric in ("views", "likes", "comments")
    }
    for change in changes:
        change.pop("_elapsed_microseconds", None)

    selected_truncated = len(matched_rows) > SELECTED_VIDEO_LIMIT
    unprocessed_batches = processed_batches < (
        (len(selected_ids) + SNAPSHOT_BATCH_SIZE - 1) // SNAPSHOT_BATCH_SIZE
    )
    cap_reached = cap_reached or unprocessed_batches
    complete = not inventory_truncated and not selected_truncated and not cap_reached

    result = {
        "contract_version": 1,
        "video_type": "long",
        "format": "long",
        "generated_at": _iso(generated_at),
        "filters": _public_filters(query),
        "sample": {
            "scope": "filtered_current_typed_long_with_snapshot_history",
            "inventory_scan_limit": SCAN_LIMIT,
            "scanned_inventory_count": len(inventory),
            "inventory_pagination": "published_at_video_id_keyset",
            "transactional_snapshot": False,
            "inventory_complete": not inventory_truncated,
            "inventory_truncated": inventory_truncated,
            "loaded_video_count": len(loaded),
            "missing_video_row_count": max(0, len(inventory_ids) - len(rows_by_id)),
            "withheld_stale_count": len(loaded) - len(current_rows),
            "excluded_by_availability_count": len(current_rows) - len(eligible_rows),
            "eligible_current_count": len(eligible_rows),
            "matched_video_count": len(matched_rows),
            "selected_video_limit": SELECTED_VIDEO_LIMIT,
            "selected_video_count": len(selected_rows),
            "selection_complete": not selected_truncated,
            "selection_truncated": selected_truncated,
            "snapshot_batch_size": SNAPSHOT_BATCH_SIZE,
            "lookback_days": SNAPSHOT_LOOKBACK.days,
            "snapshot_row_limit": SNAPSHOT_ROW_LIMIT,
            "snapshot_pagination": "recorded_at_id_keyset",
            "scanned_snapshot_count": len(snapshots),
            "complete": complete,
            "cap_reached": cap_reached,
            "videos_with_two_points": len(changes),
            "videos_with_insufficient_points": len(selected_rows) - len(changes),
        },
        "interval_summary": {
            "count": len(intervals),
            "known_count": len(intervals),
            "missing_count": len(selected_rows) - len(intervals),
            "min_hours": interval_stats["min"],
            "mean_hours": interval_stats["mean"],
            "median_hours": interval_stats["median"],
            "max_hours": interval_stats["max"],
        },
        "metrics": metric_summaries,
        "changes": changes,
        "freshness": {
            "lookback_started_at": _iso(lookback_started_at),
            "latest_snapshot": _freshness(changes),
        },
        "methods": {
            "observation_pair": "latest_two_distinct_recorded_at_per_video",
            "elapsed_time": "actual_recorded_at_difference",
            "velocity": "counter_delta_divided_by_actual_elapsed_days",
            "counter_decrease": "reported_as_correction_not_clamped",
            "nulls": "metric_missing_unless_both_observations_known",
            "ordering": "latest_at_desc_then_video_id_asc",
            "selection": "newest_typed_long_inventory_order",
            "filters_applied": True,
            "exact_snapshot_row_limit": "reported_as_coverage_cap",
        },
        "source": {
            "kind": "derived_from_stored_video_snapshots",
            "format_source": "video_inventory.video_type",
            "youtube_requests": False,
            "database_writes": False,
            "snapshots_used": True,
            "availability_history_used": False,
            "forecasts_used": False,
            "performance_ranks_used": False,
            "freshness_enforced": True,
        },
    }
    expiry = rows_expiry(changes)
    for section in ("interval_summary", "metrics"):
        stamp_aggregate(result[section], expiry)
    return result
