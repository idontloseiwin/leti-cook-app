"""Pure current-row static predicates/details, with no storage or source calls.

HTTP owners enforce the Baseline gate for calculated predicates and details.
Source counters/tags remain separate from Leti Cook calculations.
"""
from __future__ import annotations

import re
from decimal import Decimal
from fractions import Fraction
from typing import Any

from youtube_source_fields import youtube_counter_integer


STATIC_FIELDS = ("likes", "comments", "title_length", "tag_count", "like_rate", "comment_rate")
STATIC_BOUND_KEYS = tuple(f"{side}_{field}" for field in STATIC_FIELDS for side in ("min", "max"))
DERIVED_FIELDS = frozenset({"title_length", "tag_count", "like_rate", "comment_rate"})
STATIC_PRESETS = {
    "like_buckets": "likes", "comment_buckets": "comments",
    "title_length_buckets": "title_length", "tag_count_buckets": "tag_count",
}
MAX_STATIC_BOUND = 10**15
JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def normalize_static_bounds(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - set(STATIC_BOUND_KEYS):
        raise ValueError("Unsupported static catalogue bounds")
    result = {}
    for key in STATIC_BOUND_KEYS:
        if key not in value:
            continue
        raw = value[key]
        if not isinstance(raw, str):
            raise ValueError(f"{key} must be a string")
        raw = raw.strip(JS_WHITESPACE)
        if not raw:
            continue
        decimal = key.endswith("_rate")
        pattern = r"[0-9]+(?:\.[0-9]{1,6})?" if decimal else r"[0-9]+"
        if len(raw) > 32 or not re.fullmatch(pattern, raw):
            raise ValueError(f"{key} must be a nonnegative {'decimal percentage' if decimal else 'integer'}")
        number = Decimal(raw)
        if number > MAX_STATIC_BOUND:
            raise ValueError(f"{key} exceeds the supported range")
        normalized = format(number, "f")
        result[key] = normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
    for field in STATIC_FIELDS:
        lower, upper = result.get(f"min_{field}"), result.get(f"max_{field}")
        if lower is not None and upper is not None and Decimal(lower) > Decimal(upper):
            raise ValueError(f"max_{field} must be greater than or equal to min_{field}")
    return result


def stored_tags(row: dict[str, Any]) -> list[str] | None:
    value = row.get("tags")
    return list(value) if isinstance(value, list) and all(isinstance(tag, str) for tag in value) else None


def static_value(row: dict[str, Any], field: str) -> int | Fraction | None:
    if field in {"likes", "comments"}:
        return youtube_counter_integer(row.get("like_count" if field == "likes" else "comment_count"))
    if field == "title_length":
        title = row.get("title")
        return len(title) if isinstance(title, str) else None
    if field == "tag_count":
        tags = stored_tags(row)
        return len(tags) if tags is not None else None
    views = youtube_counter_integer(row.get("views"))
    numerator = static_value(row, "likes" if field == "like_rate" else "comments")
    return Fraction(numerator * 100, views) if numerator is not None and views is not None and views > 0 else None


def matches_static_bounds(row: dict[str, Any], bounds: dict[str, str]) -> bool:
    for field in STATIC_FIELDS:
        lower, upper = bounds.get(f"min_{field}"), bounds.get(f"max_{field}")
        if lower is None and upper is None:
            continue
        value = static_value(row, field)
        if value is None or (lower is not None and value < Fraction(lower)) or (upper is not None and value > Fraction(upper)):
            return False
    return True


def requires_static_analysis(filters: dict[str, Any] | None) -> bool:
    if not filters:
        return False
    return any(filters.get(key) for key, field in STATIC_PRESETS.items() if field in DERIVED_FIELDS) or any(
        key[4:] in DERIVED_FIELDS for key in filters.get("bounds", {})
    )


def static_details(row: dict[str, Any]) -> dict[str, Any]:
    def percentage(field):
        value = static_value(row, field)
        return float(round(value, 6)) if value is not None else None

    return {
        "metrics": {
            "like_rate_percent": percentage("like_rate"),
            "comment_rate_percent": percentage("comment_rate"),
            "title_length_characters": static_value(row, "title_length"),
            "tag_count": static_value(row, "tag_count"),
        },
        "raw": {"tags": stored_tags(row), "metadata_checked_at": row.get("metadata_checked_at")},
        "method": {"rates": "counter / views * 100", "zero_views": "unavailable",
                   "title_length": "unicode_code_points", "tag_count": "stored_array_length"},
        "source": {"kind": "derived_video_static_details", "attribution": "Leti Cook",
                   "snapshots_used": False, "youtube_requests": False, "database_writes": False},
    }
