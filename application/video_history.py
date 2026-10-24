"""Bounded Inspector history from real snapshots around one frozen catalogue end.

This composes an existing db/ loader; it owns no query, persistence or collection.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from source_expiry import minimum_expiry, source_expiry, rows_expiry
from catalogue_filters import _strict_instant
from catalogue_scope import CatalogueReadBudget, CatalogueScopeError
from channel_video_growth import _average_views_per_day
from hot_storage import ANALYTICS_WINDOW, HOT_DATA_WINDOW, register_read_deadline, utc_now


HISTORY_ROW_LIMIT = 500
HISTORY_WINDOW_DAYS = ANALYTICS_WINDOW.days


class VideoHistoryError(RuntimeError):
    """A bounded read could not prove a complete, coherent history."""


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("An observation timezone is required")
        value = value.isoformat()
    return datetime.fromisoformat(_strict_instant(value, "recorded_at").replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _views(value: Any) -> int | None:
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        try:
            return int(value.strip())
        except ValueError:
            pass
    return None


def _elapsed_microseconds(start: datetime, end: datetime) -> int:
    elapsed = end - start
    return (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds


def _seconds_text(microseconds: int) -> str:
    whole, fraction = divmod(microseconds, 1000000)
    return str(whole) + (f".{fraction:06d}".rstrip("0") if fraction else "")


def _empty_series(reason: str, *, duplicates: int = 0) -> dict[str, Any]:
    return {
        "status": "unavailable", "reason": reason,
        "points": [], "intervals": [], "latest_interval": None,
        "coverage": {"point_count": 0, "valid_count": 0, "missing_count": 0,
                     "duplicate_timestamp_count": duplicates},
    }


def _project_series(observations: dict[datetime, list[dict[str, Any]]], *, field: str,
                    expected_raw: Any, end: datetime, budget: CatalogueReadBudget,
                    reject_conflicts: bool = False, endpoint_expiry: str | None = None) -> dict[str, Any]:
    """One counter projection over the shared, identity-validated observations."""
    expected = _views(expected_raw)
    if expected_raw is not None and expected is None:
        return _empty_series("invalid_catalogue_endpoint")
    duplicates = sum(len(rows) - 1 for rows in observations.values())
    ordered: list[tuple[datetime, int | None]] = []
    expiry_by_time = {}
    endpoint_counter_invalid = False
    for stamp, rows in sorted(observations.items()):
        budget.check()
        values = [_views(row.get(field)) for row in rows]
        if any(value != values[0] for value in values[1:]):
            if reject_conflicts:
                raise VideoHistoryError("Video history contains conflicting simultaneous observations")
            return _empty_series("conflicting_simultaneous_observations", duplicates=duplicates)
        if stamp == end and any(row.get(field) is not None and value is None for row, value in zip(rows, values)):
            endpoint_counter_invalid = True
        ordered.append((stamp, values[0]))
        expiry_by_time[stamp] = minimum_expiry(rows_expiry(rows, "recorded_at"), endpoint_expiry)
    if end not in observations:
        return _empty_series("endpoint_snapshot_missing", duplicates=duplicates)
    if endpoint_counter_invalid or ordered[-1][1] != expected:
        return _empty_series("endpoint_counter_mismatch", duplicates=duplicates)
    points = [{"recorded_at": _iso(stamp), "value": str(value) if value is not None else None,
               "source_expires_at": expiry_by_time[stamp]}
              for stamp, value in ordered]
    intervals = []
    for (earlier_at, earlier_value), (later_at, later_value) in zip(ordered, ordered[1:]):
        budget.check()
        elapsed = _elapsed_microseconds(earlier_at, later_at)
        delta = None if earlier_value is None or later_value is None else later_value - earlier_value
        intervals.append({
            "from_at": _iso(earlier_at), "to_at": _iso(later_at),
            "source_expires_at": minimum_expiry(expiry_by_time[earlier_at], expiry_by_time[later_at]),
            "actual_interval_seconds": _seconds_text(elapsed),
            "delta": str(delta) if delta is not None else None,
            # Preserve exact rational rates for subsecond observations too.
            "per_day": _average_views_per_day(delta * 1000000, elapsed) if delta is not None else None,
        })
    valid_count = sum(point["value"] is not None for point in points)
    return {
        "status": "ready", "reason": None, "points": points, "intervals": intervals,
        "latest_interval": intervals[-1] if intervals and intervals[-1]["delta"] is not None else None,
        "coverage": {"point_count": len(points), "valid_count": valid_count,
                     "missing_count": len(points) - valid_count, "duplicate_timestamp_count": duplicates},
    }


def build_video_history(repository: Any, context: dict[str, Any], *, now: datetime | None = None,
                        read_budget: CatalogueReadBudget | None = None) -> dict[str, Any]:
    """Context must be admitted by CatalogueBrowseCache immediately before use."""
    generated_at = now or utc_now()
    budget = read_budget or CatalogueReadBudget(max_bytes=2 * 1024 * 1024, timeout_seconds=10)
    result = {
        "contract_version": 1, "channel_id": context["channel_id"], "video_id": context["video_id"],
        "browse_session": context["browse_session"], "generated_at": _iso(generated_at),
        "browse_created_at": context["browse_created_at"], "expires_at": context["expires_at"],
        "status": "ready", "reason": None,
        "window": {"requested_days": HISTORY_WINDOW_DAYS, "from_at": None, "to_at": None,
                   "endpoint": "frozen_catalogue_checked_at"},
        "points": [], "intervals": [], "latest_interval": None,
        "coverage": {"complete": True, "scanned_count": 0, "point_count": 0, "valid_view_count": 0,
                     "missing_view_count": 0, "duplicate_timestamp_count": 0, "row_limit": HISTORY_ROW_LIMIT},
        "methods": {"ordering": "recorded_at_ascending",
                    "same_timestamp": "identical_views_collapsed_conflicts_rejected",
                    "gaps": "no_interpolation_or_daily_allocation",
                    "rate": "signed_delta_per_exact_elapsed_second_times_86400_rounded_2dp_half_even",
                    "window": "strict_28d_not_card_comparison_interval",
                    "interaction_metrics": "independent_stored_counters_same_observation_window"},
        "source": {"kind": "derived_video_snapshot_history", "snapshots_used": True,
                   "youtube_requests": False, "database_writes": False, "attribution": "Leti Cook"},
    }
    result["interaction_metrics"] = {
        # Legacy Views keeps a null window for an invalid current Views counter.
        # The additive counters still share the valid frozen observation clock.
        "window": dict(result["window"]), "scanned_count": 0,
        "likes": _empty_series("invalid_catalogue_endpoint"),
        "comments": _empty_series("invalid_catalogue_endpoint"),
    }

    def unavailable(reason: str) -> dict[str, Any]:
        result.update(status="unavailable", reason=reason)
        budget.account(result)
        return result

    try:
        end = _instant(context["video"].get("checked_at"))
        start = generated_at - ANALYTICS_WINDOW
        if not start <= end <= generated_at:
            return unavailable("catalogue_endpoint_outside_hot_window")
    except (TypeError, ValueError, OverflowError):
        return unavailable("invalid_catalogue_endpoint")
    expected_raw = context["video"].get("views")
    expected = _views(expected_raw)
    invalid_views_endpoint = expected_raw is not None and expected is None
    if not invalid_views_endpoint:
        result["window"].update(from_at=_iso(start), to_at=_iso(end))
    result["interaction_metrics"]["window"].update(from_at=_iso(start), to_at=_iso(end))
    try:
        rows = budget.read(repository.load_snapshots_by_video_ids, [context["video_id"]],
                           recorded_from=start, recorded_through=end, limit=HISTORY_ROW_LIMIT + 1)
    except CatalogueScopeError:
        raise
    except Exception as error:
        raise VideoHistoryError("Video history source read failed") from error
    if not isinstance(rows, list) or len(rows) > HISTORY_ROW_LIMIT:
        raise VideoHistoryError("Video history exceeds the complete read bound")
    if not invalid_views_endpoint:
        result["coverage"]["scanned_count"] = len(rows)
    result["interaction_metrics"]["scanned_count"] = len(rows)
    observations: dict[datetime, list[dict[str, Any]]] = {}
    seen_ids: set[int] = set()
    for row in rows:
        budget.check()
        if not isinstance(row, dict) or row.get("video_id") != context["video_id"]:
            raise VideoHistoryError("Video history contains an unexpected row")
        try:
            stamp = _instant(row.get("recorded_at"))
            row_id = _views(row.get("id"))
        except (TypeError, ValueError, OverflowError) as error:
            raise VideoHistoryError("Video history contains an invalid observation time") from error
        if row_id is None or row_id in seen_ids or not start <= stamp <= end:
            raise VideoHistoryError("Video history contains invalid or duplicated identity")
        register_read_deadline(stamp + HOT_DATA_WINDOW)
        seen_ids.add(row_id)
        observations.setdefault(stamp, []).append(row)
    endpoint_expiry = source_expiry(context["video"], "checked_at")
    views = _project_series(observations, field="views", expected_raw=expected_raw,
                            end=end, budget=budget, reject_conflicts=True, endpoint_expiry=endpoint_expiry)
    result.update(status=views["status"], reason=views["reason"])
    result["points"] = [{"recorded_at": point["recorded_at"], "views": point["value"], "source_expires_at": point["source_expires_at"]}
                        for point in views["points"]]
    result["intervals"] = [{"from_at": item["from_at"], "to_at": item["to_at"],
                            "actual_interval_seconds": item["actual_interval_seconds"],
                            "delta_views": item["delta"], "views_per_day": item["per_day"],
                            "source_expires_at": item["source_expires_at"]}
                           for item in views["intervals"]]
    result["latest_interval"] = result["intervals"][-1] if views["latest_interval"] is not None else None
    result["coverage"].update(point_count=views["coverage"]["point_count"],
                              valid_view_count=views["coverage"]["valid_count"],
                              missing_view_count=views["coverage"]["missing_count"],
                              duplicate_timestamp_count=views["coverage"]["duplicate_timestamp_count"])
    for metric, field in (("likes", "like_count"), ("comments", "comment_count")):
        result["interaction_metrics"][metric] = _project_series(
            observations, field=field, expected_raw=context["video"].get(field), end=end, budget=budget, endpoint_expiry=endpoint_expiry,
        )
    budget.account(result)
    return result
