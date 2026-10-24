"""Canonical typed-playlist count observations for channel initial estimates.

Only call the observation builder after a provider response. Reusing a stored
count or counting local inventory is not a new source observation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from hot_storage import RETENTION_HOURS


def long_inventory_estimate_values(count: int | None, *, status: str | None = None,
                                   observed_at: datetime | None = None) -> dict:
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 2147483647):
        raise ValueError("invalid_long_inventory_count")
    outcome = status or ("error" if count is None else "ok")
    if outcome not in {"ok", "playlist_not_found", "error"}:
        raise ValueError("invalid_long_inventory_count_status")
    if ((outcome == "ok" and count is None)
            or (outcome == "playlist_not_found" and count not in (None, 0))
            or (outcome == "error" and count is not None)):
        raise ValueError("invalid_long_inventory_count_status")
    # Retained callers may still pass 0 for an absent source. It is unknown,
    # not a measured empty playlist, and must be stored that way.
    if outcome == "playlist_not_found":
        count = None
    now = datetime.now(timezone.utc) if observed_at is None else observed_at
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("inventory_observation_requires_utc_clock")
    stamp = now.astimezone(timezone.utc).isoformat()
    # The DB trigger merges this Long observation into the canonical JSON under
    # the existing channel row lock. Short/Stream values and clocks are retained.
    return {"initial_inventory_estimate": {
        "scope": "long", "video_types": ["long"],
        "counts": {"long": count}, "statuses": {"long": outcome},
        "checked_at": stamp, "checked_at_by_type": {"long": stamp},
    }}


def initial_inventory_count_bounds(estimate: dict) -> dict:
    """Validate three count observations without inventing a distinct total.

    Playlist overlap makes sum an estimated upper bound and max a lower bound.
    A list YouTube says does not exist (a channel without Shorts or streams)
    holds no work, so it counts as empty; only a failed probe makes the upper
    bound unknown. Old absent-source zeros remain an accepted representation.
    Source freshness and final admission remain the database's responsibility.
    """
    kinds = ("long", "short", "stream")
    if (not isinstance(estimate, dict) or estimate.get("scope") != "all"
            or estimate.get("video_types") != list(kinds)
            or not isinstance(estimate.get("counts"), dict)
            or not isinstance(estimate.get("statuses"), dict)):
        raise ValueError("initial_inventory_count_invalid")
    counts, statuses = estimate["counts"], estimate["statuses"]
    known = []
    for kind in kinds:
        value, status = counts.get(kind), statuses.get(kind)
        if status == "ok":
            if type(value) is not int or not 0 <= value <= 2147483647:
                raise ValueError("initial_inventory_count_invalid")
            known.append(value)
        elif status == "playlist_not_found":
            if value is not None and (type(value) is not int or value != 0):
                raise ValueError("initial_inventory_count_invalid")
            known.append(0)
        elif status == "error":
            if value is not None:
                raise ValueError("initial_inventory_count_invalid")
        else:
            raise ValueError("initial_inventory_count_invalid")
    upper = sum(known) if len(known) == len(kinds) else None
    # The public estimate uses PostgreSQL integer fields. An unrepresentable
    # sum is unknown, never a wrapped/saturated promise about the workset.
    if upper is not None and upper > 2147483647:
        upper = None
    return {"estimate_kind": "upper_bound" if upper is not None else "unknown",
            "items_lower_bound": max(known, default=0), "items_upper_bound": upper}


# Stored count selection uses native JSONB text/timestamp facts. These constants
# and fact admission are shared by the fixed SQL adapter; observation APIs above
# retain their separate provider-result validation and do not reuse stored data.
COUNT_KINDS = ("long", "short", "stream")
COUNT_DIGIT_PATTERN = "^[0-9]{1,10}$"
COUNT_MAXIMUM = 2147483647
# A stored count is fresh for the source window (hot_storage.RETENTION_HOURS).
COUNT_FRESH_HOURS = RETENTION_HOURS
COUNT_SOFT_CLOCK_ERRORS = ("22007", "22008", "22003")
COUNT_PRENUMBER_FACTS = ("status_ok", "kind_allowed", "estimate_object", "number_lexical")
COUNT_PRECLOCK_FACTS = (*COUNT_PRENUMBER_FACTS, "count_in_range", "clock_map_allowed")


class InventoryEstimateCountError(ValueError):
    """An original native cast error not covered by the stored reader's handler."""
    def __init__(self, sqlstate: str):
        super().__init__("inventory_estimate_native_cast_failed")
        self.sqlstate = sqlstate


def inventory_count_from_native_facts(facts: dict) -> int | None:
    """Choose a stored count from native lexical, cast and clock observations.

    This does not decode a JSON number or timestamp in Python. Native primitive
    facts preserve PostgreSQL numeric text, clock bounds and three-state NULLs.
    """
    if any(facts[name] is not True for name in COUNT_PRECLOCK_FACTS):
        return None
    error = facts["clock_error"]
    if error in COUNT_SOFT_CLOCK_ERRORS:
        return None
    if error is not None:
        raise InventoryEstimateCountError(error)
    return facts["count"] if facts["clock_valid"] is True else None
