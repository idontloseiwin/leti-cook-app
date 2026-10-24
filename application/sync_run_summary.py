"""Compact persistence projection for operational sync-run summaries.

The worker may keep detailed per-item data in memory while a run is active.
This module defines the smaller, aggregate-only representation that is safe to
persist in public.sync_runs.
"""

from collections import Counter

from operational_copy_policy import safe_summary_copy


SYNC_RUN_SUMMARY_SCHEMA_VERSION = 2
_SAFE_NESTED_OBJECT_KEYS = frozenset(
    {
        "routing",
        "timings",
        "selector_timings",
        "result_status_counts",
        "schedule_reason_counts",
        "streak_buckets",
    }
)


def _is_scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def _safe_scalar_dict(value):
    if not isinstance(value, dict):
        return {}
    return {
        str(key): nested
        for key, nested in value.items()
        if _is_scalar(nested)
    }


def _compact_section(section):
    if not isinstance(section, dict):
        return {}

    compact = {}
    for key, value in section.items():
        if _is_scalar(value):
            compact[key] = value
        elif isinstance(value, list):
            compact[f"{key}_count"] = len(value)
        elif isinstance(value, dict) and key in _SAFE_NESTED_OBJECT_KEYS:
            compact[key] = _safe_scalar_dict(value)
    return safe_summary_copy(compact)


def _sorted_counts(rows, field, transform=None):
    counts = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(field)
        if transform is not None:
            value = transform(value)
        if value is not None:
            counts[str(value)] += 1
    return dict(sorted(counts.items()))


def _streak_bucket(value):
    try:
        streak = max(int(value or 0), 0)
    except (TypeError, ValueError):
        streak = 0
    if streak == 0:
        return "0_hot"
    if streak == 1:
        return "1"
    if streak == 2:
        return "2"
    if streak <= 5:
        return "3_5"
    return "6_plus"


def compact_sync_run_summary(summary):
    """Return a new aggregate-only summary without mutating the input.

    Detailed arrays remain available to the active worker through the original
    object. Persisted arrays become counts. Discovery per-channel rows are also
    reduced to the aggregate distributions consumed by Pipeline Health.
    """

    if not isinstance(summary, dict):
        return {}

    compact = {}
    for key, value in summary.items():
        if _is_scalar(value):
            compact[key] = value
        elif isinstance(value, list):
            compact[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            compact[key] = _compact_section(value)

    discovery = summary.get("channel_discovery")
    if isinstance(discovery, dict):
        rows = discovery.get("results")
        if isinstance(rows, list):
            section = compact.setdefault("channel_discovery", {})
            section["result_status_counts"] = _sorted_counts(rows, "status")
            section["schedule_reason_counts"] = _sorted_counts(
                rows, "schedule_reason"
            )
            section["streak_buckets"] = _sorted_counts(
                rows, "no_new_streak", transform=_streak_bucket
            )

    availability_loss = summary.get("long_availability_loss")
    if isinstance(availability_loss, dict):
        rows = availability_loss.get("results")
        if isinstance(rows, list):
            section = compact.setdefault("long_availability_loss", {})
            section["result_status_counts"] = _sorted_counts(rows, "status")

    compact["summary_schema_version"] = SYNC_RUN_SUMMARY_SCHEMA_VERSION
    return safe_summary_copy(compact)
