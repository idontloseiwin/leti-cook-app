"""Inspector projection of the existing descriptive, current-row Baseline.

This is not an individual performance score or an age/launch model. The caller
provides the admitted browsing generation's frozen *full* source repository,
not its displayed or filtered result rows. FastAPI and the browse cache retain
ownership of visibility, feature gates, generation identity and expiry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from channel_baseline import build_channel_baseline


LIFETIME_BASELINE_METHOD = "current_lifetime_views_median_iqr_v1"


def build_video_lifetime_baseline(
    repository: Any,
    channel_id: str,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Project canonical all-time median/IQR without adding statistics math.

    ``full_scope=True`` delegates resource bounds and complete traversal to the
    canonical builder's CatalogueScopeRepository. No browsing conditions or
    inspected-video exclusion are accepted: this is a descriptive channel
    sample, including the inspected video when it is itself eligible.

    Sample-size labels are display policy, not statistical confidence claims.
    Missing views count only otherwise eligible rows; excluded coverage also
    discloses missing source rows, stale clocks and other canonical exclusions.
    """

    baseline = build_channel_baseline(
        repository,
        channel_id,
        period="max",
        full_scope=True,
        now=now,
    )
    views = baseline["statistics"]["views"]
    sample = baseline["sample"]
    sample_size = views["known_count"]
    status = (
        "insufficient_data" if sample_size < 4
        else "preliminary" if sample_size < 12
        else "ready"
    )
    inventory_count = sample["scanned_inventory_count"]
    eligible_count = sample["matched_video_count"]
    freshness = baseline["freshness"]

    return {
        "source_expires_at": baseline.get("source_expires_at"),
        "method": LIFETIME_BASELINE_METHOD,
        "status": status,
        "sample_size": sample_size,
        "missing_views": views["missing_count"],
        "median": views["median"] if sample_size >= 4 else None,
        "p25": views["q1"] if sample_size >= 4 else None,
        "p75": views["q3"] if sample_size >= 4 else None,
        "coverage": {
            "inventory_count": inventory_count,
            "eligible_count": eligible_count,
            "excluded_count": inventory_count - eligible_count,
        },
        "reference": {
            "video_type": "long",
            "publication_period": "all_time",
            "browsing_filters_applied": False,
        },
        "freshness": {
            "maximum_age_days": freshness["maximum_age_days"],
            "counters": dict(freshness["counters"]),
            "metadata": dict(freshness["metadata"]),
        },
    }
