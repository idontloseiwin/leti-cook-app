"""Measured launch landmarks from bounded recent real observations.

The caller supplies the complete canonical pre-filter catalogue from a still
admitted browsing generation. This module owns no query, collection or storage.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any

from source_expiry import source_expiry, minimum_expiry
from catalogue_scope import CatalogueReadBudget, CatalogueScopeError
from hot_storage import ANALYTICS_WINDOW, HOT_DATA_WINDOW, register_read_deadline, utc_now

from video_history import _instant, _iso, _views, _elapsed_microseconds, _seconds_text


SNAPSHOT_ROW_LIMIT = 20_000
SNAPSHOT_BATCH_SIZE = 50
WINDOW_DAYS = ANALYTICS_WINDOW.days
HORIZONS = (("24h", 86_400, 7_200), ("7d", 604_800, 86_400),
            ("28d", 2_419_200, 86_400))


class VideoLaunchError(RuntimeError):
    """A complete, identity-consistent measured launch read was not possible."""


def _quality(count: int) -> str:
    return "insufficient_data" if count < 4 else "preliminary" if count < 12 else "ready"


def _empty_comparison(reference_count: int = 0) -> dict[str, Any]:
    return {"status": "unavailable", "reference_count": reference_count,
            "median": None, "p25": None, "p75": None, "classification": None,
            "multiplier": None, "multiplier_reason": None}


def _target(status: str, target_at: datetime | None = None) -> dict[str, Any]:
    return {"status": status, "target_at": _iso(target_at) if target_at else None,
            "observed_at": None, "actual_age_seconds": None, "views": None,
            "comparison": _empty_comparison()}


def _decimal(value: Fraction) -> str:
    """Linear quartiles of integer counters have at most quarter fractions."""
    quarters = value * 4
    if quarters.denominator != 1:
        raise VideoLaunchError("Launch quantile has an invalid precision")
    whole, remainder = divmod(quarters.numerator, 4)
    return str(whole) + ("", ".25", ".5", ".75")[remainder]


def _ratio(value: int, median: Fraction) -> str:
    # Fraction.__round__ is exact half-even, including beyond JS safe integers.
    hundredths = round(Fraction(value) / median * 100)
    whole, remainder = divmod(hundredths, 100)
    return f"{whole}.{remainder:02d}"


def _without_quantile(ordered: list[tuple[int, str]], excluded: int, q: Fraction) -> Fraction:
    """O(1) quantile over a sorted cohort after omitting one index."""
    count = len(ordered) - 1
    position = (count - 1) * q
    lower = position.numerator // position.denominator
    upper = min(lower + 1, count - 1)
    weight = position - lower
    low = ordered[lower + (lower >= excluded)][0]
    high = ordered[upper + (upper >= excluded)][0]
    return Fraction(low) + (high - low) * weight


def _classification(value: int, low: Fraction, high: Fraction) -> str:
    return "below_typical" if value < low else "above_typical" if value > high else "typical"


def _catalogue_rows(context: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    catalogue = context.get("catalogue")
    if not isinstance(catalogue, dict):
        raise VideoLaunchError("Launch catalogue context is missing")
    coverage = catalogue.get("coverage", {})
    rows = catalogue.get("videos")
    pagination = catalogue.get("pagination", {})
    inventory_count = coverage.get("scanned_count")
    if (catalogue.get("video_type") != "long"
            or catalogue.get("channel", {}).get("channel_id") != context["channel_id"]
            or coverage.get("scan_mode") != "full_catalogue"
            or coverage.get("truncated") is not False
            or coverage.get("inventory_complete") is not True
            or not isinstance(rows, list)
            or type(inventory_count) is not int or inventory_count < len(rows)
            or pagination.get("offset") != 0
            or pagination.get("returned_count") != len(rows)
            or pagination.get("matched_count") != len(rows)
            or pagination.get("has_more_in_scan") is not False):
        raise VideoLaunchError("Launch catalogue context is not a complete long-form source")
    seen = set()
    for row in rows:
        video_id = row.get("video_id") if isinstance(row, dict) else None
        if not isinstance(video_id, str) or not video_id or video_id in seen:
            raise VideoLaunchError("Launch catalogue contains an invalid identity")
        seen.add(video_id)
    return rows, inventory_count


def _video_windows(row: dict[str, Any], as_of: datetime, start: datetime) -> tuple[datetime | None, datetime | None, list[dict[str, Any]]]:
    try:
        published = _instant(row.get("published_at"))
    except (TypeError, ValueError, OverflowError):
        return None, None, [_target("missing_publication") for _ in HORIZONS]
    try:
        checked = _instant(row.get("checked_at"))
    except (TypeError, ValueError, OverflowError):
        return published, None, [_target("ineligible_video") for _ in HORIZONS]
    if published > as_of:
        return published, checked, [_target("ineligible_video") for _ in HORIZONS]
    states = []
    for _, horizon, tolerance in HORIZONS:
        try:
            target_at = published + timedelta(seconds=horizon)
        except OverflowError:
            states.append(_target("ineligible_video"))
            continue
        if target_at > as_of:
            status = "not_reached"
        elif target_at < start or target_at - timedelta(seconds=tolerance) > as_of:
            status = "outside_observation_window"
        else:
            status = "missing_snapshot"
        states.append(_target(status, target_at))
    return published, checked, states


def build_video_launch(repository: Any, context: dict[str, Any], *, now: datetime | None = None,
                       read_budget: CatalogueReadBudget | None = None) -> dict[str, Any]:
    """Project three independent milestones; caller rechecks live gates afterwards."""
    budget = read_budget or CatalogueReadBudget()
    try:
        as_of = _instant(context["browse_created_at"])
        generated_at = _instant(now or utc_now())
        start = generated_at - ANALYTICS_WINDOW
        if not start <= as_of <= generated_at:
            raise ValueError("Launch generation is outside the hot window")
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise VideoLaunchError("Launch browsing clock is invalid") from error
    rows, inventory_count = _catalogue_rows(context)
    budget.account(rows)
    windows = {}
    video_expiries = {row["video_id"]: source_expiry(row, "checked_at") for row in rows}
    candidate_ids = []
    for row in rows:
        budget.check()
        details = _video_windows(row, as_of, start)
        for state in details[2]:
            state["source_expires_at"] = video_expiries[row["video_id"]]
        windows[row["video_id"]] = details
        if any(state["status"] == "missing_snapshot" for state in details[2]):
            candidate_ids.append(row["video_id"])

    observations: dict[str, dict[datetime, list[int | None]]] = {key: {} for key in candidate_ids}
    seen_row_ids: set[int] = set()
    snapshot_count = 0
    for offset in range(0, len(candidate_ids), SNAPSHOT_BATCH_SIZE):
        ids = candidate_ids[offset:offset + SNAPSHOT_BATCH_SIZE]
        allowed_ids = set(ids)
        try:
            batch = budget.read(repository.load_snapshots_by_video_ids, ids,
                                recorded_from=start, recorded_through=as_of,
                                limit=SNAPSHOT_ROW_LIMIT - snapshot_count + 1,
                                include_engagement=False)
        except CatalogueScopeError:
            raise
        except Exception as error:
            raise VideoLaunchError("Launch snapshot source read failed") from error
        if not isinstance(batch, list) or snapshot_count + len(batch) > SNAPSHOT_ROW_LIMIT:
            raise VideoLaunchError("Launch snapshots exceed the complete read bound")
        snapshot_count += len(batch)
        for record in batch:
            budget.check()
            if not isinstance(record, dict) or record.get("video_id") not in allowed_ids:
                raise VideoLaunchError("Launch snapshots contain an unexpected identity")
            row_id = _views(record.get("id"))
            try:
                stamp = _instant(record.get("recorded_at"))
            except (TypeError, ValueError, OverflowError) as error:
                raise VideoLaunchError("Launch snapshot observation time is invalid") from error
            if row_id is None or row_id in seen_row_ids or not start <= stamp <= as_of:
                raise VideoLaunchError("Launch snapshot identity or window is invalid")
            register_read_deadline(stamp + HOT_DATA_WINDOW)
            seen_row_ids.add(row_id)
            video_id = record["video_id"]
            # Reads share one generation clock; an individual card can be older.
            if stamp > windows[video_id][1]:
                continue
            video_expiries[video_id] = minimum_expiry(video_expiries[video_id], source_expiry(record, "recorded_at"))
            observations[video_id].setdefault(stamp, []).append(_views(record.get("views")))

    for video_id, (published, _checked, states) in windows.items():
        budget.check()
        evidence = observations.get(video_id, {})
        conflict = any(any(value != values[0] for value in values[1:]) for values in evidence.values())
        ordered = sorted(evidence)
        for index, (_key, _horizon, tolerance) in enumerate(HORIZONS):
            state = states[index]
            if state["status"] != "missing_snapshot":
                continue
            state["source_expires_at"] = video_expiries[video_id]
            if conflict:
                state["status"] = "conflicting_snapshots"
                continue
            target_at = _instant(state["target_at"])
            lower = max(start, target_at - timedelta(seconds=tolerance))
            candidates = [stamp for stamp in ordered if lower <= stamp <= target_at]
            if not candidates:
                continue
            stamp = candidates[-1]
            value = evidence[stamp][0]
            if value is None:
                state["status"] = "invalid_snapshot"
                continue
            state.update(status="ready", observed_at=_iso(stamp),
                         actual_age_seconds=_seconds_text(_elapsed_microseconds(published, stamp)),
                         views=str(value))

    # Eligibility/counts inspect the whole catalogue; each video conflict check
    # uses all its admitted observations, even those not chosen as a milestone.
    horizons = []
    for index, (key, horizon, tolerance) in enumerate(HORIZONS):
        cohort_expiry = minimum_expiry(*(details[2][index].get("source_expires_at") for details in windows.values()))
        ordered = sorted((int(details[2][index]["views"]), video_id)
                         for video_id, details in windows.items() if details[2][index]["status"] == "ready")
        count = len(ordered)
        quality = _quality(count)
        counts = {"below_typical": 0, "typical": 0, "above_typical": 0}
        target = windows.get(context["video_id"], (None, None, [_target("ineligible_video") for _ in HORIZONS]))[2][index]
        target["comparison"] = _empty_comparison(count - (target["status"] == "ready"))
        if target["status"] == "ready" and count < 4:
            target["comparison"]["status"] = "insufficient_data"
        if count >= 4:
            for position, (value, video_id) in enumerate(ordered):
                budget.check()
                low = _without_quantile(ordered, position, Fraction(1, 4))
                high = _without_quantile(ordered, position, Fraction(3, 4))
                classification = _classification(value, low, high)
                counts[classification] += 1
                if video_id == context["video_id"]:
                    median = _without_quantile(ordered, position, Fraction(1, 2))
                    target["comparison"] = {"status": quality, "reference_count": count - 1,
                                            "median": _decimal(median), "p25": _decimal(low),
                                            "p75": _decimal(high), "classification": classification,
                                            "multiplier": _ratio(value, median) if median else None,
                                            "multiplier_reason": None if median else "zero_reference_median"}
        target["comparison"]["source_expires_at"] = cohort_expiry
        target["source_expires_at"] = minimum_expiry(target.get("source_expires_at"), cohort_expiry)
        horizons.append({"source_expires_at": cohort_expiry, "key": key, "horizon_seconds": horizon, "tolerance_before_seconds": tolerance,
                         "eligible_count": count, "quality": quality, "counts": counts, "target": target})

    result = {"contract_version": 1, "channel_id": context["channel_id"], "video_id": context["video_id"],
              "browse_session": context["browse_session"], "generated_at": _iso(generated_at),
              "browse_created_at": context["browse_created_at"], "expires_at": context["expires_at"],
              "method": "measured_launch_before_horizon_v1",
              "window": {"days": WINDOW_DAYS, "from_at": _iso(start), "to_at": _iso(as_of)},
              "reference": {"video_type": "long", "publication_period": "all_time",
                            "browsing_filters_applied": False, "leave_one_out": True},
              "coverage": {"inventory_count": inventory_count, "eligible_video_count": len(rows),
                           "excluded_video_count": inventory_count - len(rows),
                           "snapshot_rows": snapshot_count, "snapshot_row_limit": SNAPSHOT_ROW_LIMIT,
                           "complete": True}, "horizons": horizons,
              "source": {"snapshots_used": True, "youtube_requests": False,
                         "database_writes": False, "attribution": "Leti Cook"}}
    budget.account(result)
    return result
