"""Pure bounded Baseline Analytics for current typed long-form video rows."""

from __future__ import annotations

from decimal import Decimal, localcontext
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Callable

from catalogue_scope import CatalogueReadBudget, CatalogueScopeRepository
from urllib.parse import unquote, urlparse

from catalogue_filters import bucket_definitions, catalogue_time_zone, matches_publication_window
from hot_storage import source_is_current
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
from source_expiry import source_expiry, minimum_expiry, rows_expiry, captured_source_call, stamp_aggregate
from youtube_categories import youtube_video_category_name
from youtube_source_fields import youtube_duration_seconds


LEGACY_VIDEO_COLUMNS = (
    "video_id,title,published_at,checked_at,metadata_checked_at,duration,views,"
    "like_count,comment_count,category_id,tags,availability_status,availability_checked_at"
)
PROFILE_VIDEO_FIELDS = (
    "youtube_topic_categories", "default_audio_language",
    "default_language", "caption_available",
)
VIDEO_COLUMNS = LEGACY_VIDEO_COLUMNS + "," + ",".join(PROFILE_VIDEO_FIELDS)
CHANNEL_COLUMNS = "channel_id,country_code,is_hidden"
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

BASELINE_PERIODS = {
    "7d": ("7D", 7),
    "28d": ("28D", 28),
    "3m": ("3M", 90),
    "1y": ("1Y", 365),
    "max": ("MAX", None),
}

# YouTube's familiar analysis horizons, expressed as mutually exclusive ranges
# so distribution counts remain additive and percentages still total 100%.
YOUTUBE_DAY_RANGE_BUCKETS = (
    ("up_to_1d", "≤1 day", 1),
    ("2_7d", "2–7 days", 7),
    ("8_28d", "8–28 days", 28),
    ("29_90d", "29–90 days", 90),
    ("91_365d", "91–365 days", 365),
    ("over_365d", "Over 365 days", None),
)


SAFE_JSON_INTEGER = 2**53 - 1


def _profile_video_columns_unavailable(error: Exception) -> bool:
    # Undefined-column is the direct equivalent of the old projection failure.
    # This fallback is attempted only for the optional-profile projection.
    if getattr(error, "code", None) == "42703":
        return True
    message = str(error).lower()
    return (
        any(field in message for field in PROFILE_VIDEO_FIELDS)
        and any(
            marker in message
            for marker in ("schema cache", "column", "could not find", "pgrst")
        )
    )


def _rounded(value: float | int | Fraction | None) -> float | int | str | None:
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
                    Decimal(rounded_fraction.numerator) / Decimal(rounded_fraction.denominator),
                    "f",
                )
            return rendered.rstrip("0").rstrip(".")
        return float(rounded_fraction)
    rounded = round(float(value), 2)
    return int(rounded) if rounded.is_integer() else rounded


def _quantile(values: list[float | int], fraction: float) -> float | int | str | None:
    return _ordered_quantile(sorted(values), fraction)


def _ordered_quantile(ordered: list[float | int | Fraction], fraction: float) -> float | int | str | None:
    if not ordered:
        return None
    position = Fraction(len(ordered) - 1) * Fraction(str(fraction))
    lower = position.numerator // position.denominator
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    lower_value = Fraction(str(ordered[lower]))
    upper_value = Fraction(str(ordered[upper]))
    return _rounded(lower_value + (upper_value - lower_value) * weight)


def _source_metric(value: Any) -> int | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _metric_summary(
    rows: list[dict[str, Any]],
    getter: Callable[[dict[str, Any]], int | float | None],
) -> dict[str, Any]:
    values = [value for row in rows if (value := getter(row)) is not None]
    return _value_summary(values, sample_count=len(rows))


def _value_summary(
    values: list[int | float | Fraction],
    *,
    sample_count: int,
) -> dict[str, Any]:
    """Summarize an already validated derived sample without losing its coverage."""

    if not values:
        return {
            "known_count": 0,
            "missing_count": sample_count,
            "total": None,
            "mean": None,
            "median": None,
            "q1": None,
            "q3": None,
            "min": None,
            "max": None,
        }
    total = sum(values)
    ordered = sorted(values)
    return {
        "known_count": len(values),
        "missing_count": max(0, sample_count - len(values)),
        "total": _rounded(total),
        "mean": _rounded(Fraction(total, len(values))),
        "median": _ordered_quantile(ordered, 0.5),
        "q1": _ordered_quantile(ordered, 0.25),
        "q3": _ordered_quantile(ordered, 0.75),
        "min": _rounded(min(values)),
        "max": _rounded(max(values)),
    }


def _bucket_distribution(
    values: list[int | float | Fraction],
    definitions: tuple[
        tuple[
            str,
            str,
            int | float | Fraction | None,
            int | float | Fraction | None,
        ],
        ...,
    ],
) -> list[dict[str, Any]]:
    result = []
    for key, label, lower, upper in definitions:
        count = sum(
            1
            for value in values
            if (lower is None or value >= lower) and (upper is None or value < upper)
        )
        result.append({"key": key, "label": label, "count": count})
    return result


def _youtube_day_range_distribution(
    values: list[int | float | Fraction],
) -> list[dict[str, Any]]:
    """Group elapsed days by YouTube-style upper-inclusive horizons."""

    result = []
    previous_upper: int | None = None
    for key, label, upper in YOUTUBE_DAY_RANGE_BUCKETS:
        count = sum(
            1
            for value in values
            if value >= 0
            and (previous_upper is None or value > previous_upper)
            and (upper is None or value <= upper)
        )
        result.append({"key": key, "label": label, "count": count})
        previous_upper = upper
    return result


def _distribution_coverage(
    *,
    known_count: int,
    missing_count: int,
    excluded_count: int = 0,
) -> dict[str, int]:
    return {
        "known_count": known_count,
        "missing_count": missing_count,
        "excluded_count": excluded_count,
    }


def _timedelta_seconds(value: timedelta) -> Fraction:
    return (
        Fraction(value.days * 86_400 + value.seconds)
        + Fraction(value.microseconds, 1_000_000)
    )


def _publication_values(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[list[datetime], int, int]:
    values: list[datetime] = []
    missing_count = 0
    future_count = 0
    for row in rows:
        published = _timestamp(row.get("published_at"))
        if published is None:
            missing_count += 1
        elif published > now:
            future_count += 1
        else:
            values.append(published)
    return values, missing_count, future_count


def _publication_points(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, str | None]]:
    """Return exact, stable UTC publication points from the current in-memory sample."""

    ordered: list[tuple[datetime, str, str | None]] = []
    expiries = {str(row.get("video_id") or ""): source_expiry(row) for row in rows}
    for row in rows:
        published = _timestamp(row.get("published_at"))
        if published is None or published > now:
            continue
        video_id = str(row.get("video_id") or "").strip()
        title = row.get("title")
        ordered.append(
            (published, video_id, title if isinstance(title, str) else None)
        )
    ordered.sort(key=lambda item: (item[0], item[1]))
    return [
        {
            "video_id": video_id,
            "title": title,
            "published_at": published.isoformat().replace("+00:00", "Z"),
            "source_expires_at": expiries[video_id],
        }
        for published, video_id, title in ordered
    ]


def _counted_distribution(
    values: list[Any],
    key_and_label: Callable[[Any], tuple[str, str]],
    *,
    sort_key: Callable[[tuple[str, str]], Any] | None = None,
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for value in values:
        identity = key_and_label(value)
        counts[identity] = counts.get(identity, 0) + 1
    ordered = sorted(counts, key=sort_key)
    return [
        {"key": key, "label": label, "count": counts[(key, label)]}
        for key, label in ordered
    ]


def _group_row(
    key: str,
    label: str,
    rows: list[dict[str, Any]],
    *,
    share_denominator: int,
) -> dict[str, Any]:
    views = _metric_summary(rows, lambda row: _source_metric(row.get("views")))
    likes = _metric_summary(rows, lambda row: _source_metric(row.get("like_count")))
    comments = _metric_summary(rows, lambda row: _source_metric(row.get("comment_count")))
    return {
        "key": key,
        "label": label,
        "count": len(rows),
        "share_percent": _rounded(len(rows) * 100 / share_denominator),
        "median_views": views["median"],
        "median_likes": likes["median"],
        "median_comments": comments["median"],
        "views_known_count": views["known_count"],
        "likes_known_count": likes["known_count"],
        "comments_known_count": comments["known_count"],
    }


def _single_label_section(
    rows: list[dict[str, Any]],
    *,
    source: str,
    identity: Callable[[dict[str, Any]], tuple[str, str] | None],
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    missing_count = 0
    for row in rows:
        resolved = identity(row)
        if resolved is None:
            missing_count += 1
            continue
        key, label = resolved
        grouped.setdefault((key, label), []).append(row)
    known_count = len(rows) - missing_count
    groups = [
        _group_row(key, label, grouped[(key, label)], share_denominator=known_count)
        for key, label in sorted(
            grouped,
            key=lambda item: (-len(grouped[item]), item[0]),
        )
    ]
    return {
        "kind": "single_label",
        "source": source,
        "share_basis": "known_rows",
        "sample_count": len(rows),
        "known_count": known_count,
        "missing_count": missing_count,
        "groups": groups,
    }


def _topic_label(value: str) -> str:
    parsed = urlparse(value)
    slug = unquote(parsed.path.rsplit("/", 1)[-1]).replace("_", " ").strip()
    return slug or value


def _youtube_topic_section(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    known_count = 0
    assigned_count = 0
    empty_count = 0
    for row in rows:
        raw_topics = row.get("youtube_topic_categories")
        if not isinstance(raw_topics, list) or any(
            not isinstance(value, str) or not value.strip() for value in raw_topics
        ):
            continue
        known_count += 1
        topics = list(dict.fromkeys(value.strip() for value in raw_topics))
        if not topics:
            empty_count += 1
            continue
        assigned_count += 1
        for topic in topics:
            identity = (topic, _topic_label(topic))
            grouped.setdefault(identity, []).append(row)
    groups = [
        _group_row(key, label, grouped[(key, label)], share_denominator=len(rows))
        for key, label in sorted(
            grouped,
            key=lambda item: (-len(grouped[item]), item[0]),
        )
    ]
    return {
        "kind": "multi_label",
        "source": "youtube.video.topicDetails.topicCategories",
        "share_basis": "sample_rows",
        "sample_count": len(rows),
        "known_count": known_count,
        "missing_count": len(rows) - known_count,
        "assigned_count": assigned_count,
        "empty_count": empty_count,
        "groups": groups,
    }


def _publication_calendar(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    zone: Any = timezone.utc,
) -> dict[str, Any]:
    daily_counts: dict[str, int] = {}
    for row in rows:
        published = _timestamp(row.get("published_at"))
        if published is None or published > now:
            continue
        date = published.astimezone(zone).date().isoformat()
        daily_counts[date] = daily_counts.get(date, 0) + 1
    default_year = now.astimezone(zone).year
    available_years = sorted(
        {default_year, *(int(date[:4]) for date in daily_counts)},
        reverse=True,
    )
    return {
        "timezone": getattr(zone, "key", "UTC"),
        "default_year": default_year,
        "available_years": available_years,
        "years": [
            {
                "year": year,
                "days": [
                    {"date": date, "count": daily_counts[date]}
                    for date in sorted(daily_counts)
                    if date.startswith(f"{year:04d}-")
                ],
            }
            for year in available_years
        ],
    }


def _ratio_per_thousand_values(
    rows: list[dict[str, Any]],
    metric_field: str,
) -> tuple[list[Fraction], dict[str, int]]:
    values: list[Fraction] = []
    missing_count = 0
    excluded_count = 0
    for row in rows:
        views = _number(row.get("views"))
        metric = _number(row.get(metric_field))
        if views is None or views < 0 or metric is None or metric < 0:
            missing_count += 1
        elif views == 0:
            excluded_count += 1
        else:
            values.append(Fraction(metric * 1_000, views))
    return values, _distribution_coverage(
        known_count=len(values),
        missing_count=missing_count,
        excluded_count=excluded_count,
    )


def _lifetime_views_per_day_values(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
) -> tuple[list[Fraction], dict[str, int]]:
    values: list[Fraction] = []
    missing_count = 0
    excluded_count = 0
    minimum_age = Fraction(86_400)
    for row in rows:
        views = _source_metric(row.get("views"))
        published = _timestamp(row.get("published_at"))
        checked = _timestamp(row.get("checked_at"))
        if views is None or published is None or checked is None:
            missing_count += 1
            continue
        elapsed_seconds = _timedelta_seconds(checked - published)
        if published > now or elapsed_seconds < minimum_age:
            excluded_count += 1
            continue
        values.append(Fraction(views) * 86_400 / elapsed_seconds)
    return values, _distribution_coverage(
        known_count=len(values),
        missing_count=missing_count,
        excluded_count=excluded_count,
    )


def _publication_cadence(rows: list[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    valid: list[datetime] = []
    future_count = 0
    invalid_count = 0
    for row in rows:
        published = _timestamp(row.get("published_at"))
        if published is None:
            invalid_count += 1
        elif published > now:
            future_count += 1
        else:
            valid.append(published)
    valid.sort()
    gaps = [
        (current - previous).total_seconds() / 86400
        for previous, current in zip(valid, valid[1:])
    ]
    span_days = (valid[-1] - valid[0]).total_seconds() / 86400 if len(valid) > 1 else None
    trailing_28_day_start = now - timedelta(days=28)
    publications_last_28_days = sum(
        published >= trailing_28_day_start
        for published in valid
    )
    publications_per_30_days = (
        _rounded((len(valid) - 1) * 30 / span_days)
        if span_days is not None and span_days > 0
        else None
    )
    return {
        "known_publication_count": len(valid),
        "missing_or_invalid_count": invalid_count,
        "future_count": future_count,
        "first_published_at": valid[0].isoformat().replace("+00:00", "Z") if valid else None,
        "last_published_at": valid[-1].isoformat().replace("+00:00", "Z") if valid else None,
        "interval_count": len(gaps),
        "mean_gap_days": _rounded(sum(gaps) / len(gaps)) if gaps else None,
        "median_gap_days": _quantile(gaps, 0.5),
        "q1_gap_days": _quantile(gaps, 0.25),
        "q3_gap_days": _quantile(gaps, 0.75),
        "min_gap_days": _rounded(min(gaps)) if gaps else None,
        "max_gap_days": _rounded(max(gaps)) if gaps else None,
        "publications_last_28_days": publications_last_28_days,
        "publications_per_30_days": publications_per_30_days,
    }


def _freshness(rows: list[dict[str, Any]], field: str) -> dict[str, str | None]:
    values = sorted(value for row in rows if (value := _timestamp(row.get(field))) is not None)
    return {
        "oldest": values[0].isoformat().replace("+00:00", "Z") if values else None,
        "newest": values[-1].isoformat().replace("+00:00", "Z") if values else None,
    }


def _public_filters(query: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            query[key].isoformat().replace("+00:00", "Z")
            if isinstance(query[key], datetime)
            else query[key]
        )
        for key in (*FILTER_KEYS, *(("catalogue_filters",) if "catalogue_filters" in query else ()))
    }


def _period_details(
    period: Any,
    *,
    generated_at: datetime,
) -> tuple[dict[str, Any], datetime | None]:
    normalized = "max" if period is None else str(period).strip().lower()
    definition = BASELINE_PERIODS.get(normalized)
    if definition is None:
        raise ChannelExplorerQueryError(
            "period must be one of 7d, 28d, 3m, 1y, max"
        )
    label, days = definition
    requested_start = (
        generated_at - timedelta(days=days)
        if days is not None
        else None
    )
    return {
        "key": normalized,
        "label": label,
        "kind": "rolling" if days is not None else "max",
        "days": days,
        "start_at": (
            requested_start.isoformat().replace("+00:00", "Z")
            if requested_start is not None
            else None
        ),
        "end_at": generated_at.isoformat().replace("+00:00", "Z"),
    }, requested_start


def _matches_period(
    row: dict[str, Any],
    *,
    requested_start: datetime | None,
    requested_end: datetime,
) -> bool:
    return matches_publication_window(row, requested_start, requested_end)


def build_channel_baseline(
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
    period: Any = "max",
    catalogue_filters: Any = None,
    full_scope: bool = False,
    read_budget: CatalogueReadBudget | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build filter-aware descriptive statistics without history or side effects."""

    if full_scope:
        repository = CatalogueScopeRepository(repository, read_budget)

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
        catalogue_filters=catalogue_filters,
    )
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)
    # Years, quarters, weekdays, hours and calendar days use the requested zone;
    # the *_utc keys keep their names but follow it too.
    zone = catalogue_time_zone(query.get("catalogue_filters"))
    period_details, requested_start = _period_details(
        period,
        generated_at=generated_at,
    )

    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")

    inventory = (
        repository.load_full_long_inventory(channel_id) if full_scope
        else repository.load_long_inventory(channel_id, limit=SCAN_LIMIT + 1)
    )
    truncated = not full_scope and len(inventory) > SCAN_LIMIT
    if not full_scope:
        inventory = inventory[:SCAN_LIMIT]
    inventory = [
        row for row in inventory
        if source_is_current(row.get("source_checked_at"), now=generated_at)
    ]
    video_ids = [str(row.get("video_id")) for row in inventory if row.get("video_id")]
    inventory_ids = set(video_ids)

    loaded_rows: list[dict[str, Any]] = []
    profile_columns_available = True
    for start in range(0, len(video_ids), DB_BATCH_SIZE):
        columns = VIDEO_COLUMNS if profile_columns_available else LEGACY_VIDEO_COLUMNS
        try:
            batch = repository.load_videos_by_ids(
                video_ids[start:start + DB_BATCH_SIZE],
                columns=columns,
            )
        except Exception as error:
            if not profile_columns_available or not _profile_video_columns_unavailable(error):
                raise
            profile_columns_available = False
            batch = repository.load_videos_by_ids(
                video_ids[start:start + DB_BATCH_SIZE],
                columns=LEGACY_VIDEO_COLUMNS,
            )
        for row in batch:
            normalized = dict(row)
            for field in PROFILE_VIDEO_FIELDS:
                normalized.setdefault(field, None)
            loaded_rows.append(normalized)
    rows_by_id = {
        str(row.get("video_id")): dict(row)
        for row in loaded_rows
        if str(row.get("video_id") or "") in inventory_ids
    }
    loaded = list(rows_by_id.values())

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
    inventory_expiries = {str(item.get("video_id")): source_expiry(item, "source_checked_at") for item in inventory}
    for row in eligible_rows:
        row["source_expires_at"] = minimum_expiry(source_expiry(row, "checked_at", "metadata_checked_at", "availability_checked_at"), inventory_expiries.get(str(row.get("video_id"))))
    current_title_expiries = {str(row.get("video_id")): source_expiry(row) for row in eligible_rows if isinstance(row.get("title"), str)}
    current_titles_by_id = {
        str(row.get("video_id")): row.get("title")
        for row in eligible_rows
        if row.get("video_id") and isinstance(row.get("title"), str)
    }
    inventory_publication_rows = [
        {
            "video_id": str(row.get("video_id") or ""),
            "published_at": row.get("published_at"),
            "title": current_titles_by_id.get(str(row.get("video_id") or "")),
            "source_expires_at": minimum_expiry(source_expiry(row, "source_checked_at"), current_title_expiries.get(str(row.get("video_id")))),
        }
        for row in inventory
    ]
    inventory_period_publication_rows = [
        row
        for row in inventory_publication_rows
        if _matches_period(
            row,
            requested_start=requested_start,
            requested_end=generated_at,
        )
    ]
    query_matched_rows = [
        row
        for row in eligible_rows
        if _matches(row, query)
    ]
    matched_rows = [
        row
        for row in query_matched_rows
        if _matches_period(
            row,
            requested_start=requested_start,
            requested_end=generated_at,
        )
    ]
    period_future_rows = [
        row
        for row in query_matched_rows
        if (
            (published := _timestamp(row.get("published_at"))) is not None
            and published > generated_at
        )
    ]

    category_labels = {}
    category_expiries = []
    def category_label(category):
        if category not in category_labels:
            label, expiry = captured_source_call(youtube_video_category_name, category)
            category_labels[category] = label
            category_expiries.append(expiry)
        return category_labels[category]

    category_counts: dict[str, int] = {}
    unknown_category_count = 0
    for row in matched_rows:
        category = str(row.get("category_id") or "").strip()
        if not category:
            unknown_category_count += 1
            continue
        category_counts[category] = category_counts.get(category, 0) + 1
    known_category_count = sum(category_counts.values())
    categories = [
        {
            "category_id": category_id,
            "category_name": category_label(category_id),
            "count": count,
            "share_percent": _rounded(count * 100 / known_category_count)
            if known_category_count
            else None,
        }
        for category_id, count in sorted(
            category_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    view_values = [
        value for row in matched_rows if (value := _source_metric(row.get("views"))) is not None
    ]
    like_values = [
        value for row in matched_rows if (value := _source_metric(row.get("like_count"))) is not None
    ]
    comment_values = [
        value for row in matched_rows if (value := _source_metric(row.get("comment_count"))) is not None
    ]
    duration_values = [
        value
        for row in matched_rows
        if (value := youtube_duration_seconds(row.get("duration"))) is not None
    ]
    publication_values, missing_publication_count, future_publication_count = (
        _publication_values(matched_rows, now=generated_at)
    )
    publication_values = [published.astimezone(zone) for published in publication_values]
    publication_points = _publication_points(
        inventory_period_publication_rows, now=generated_at
    )
    publication_coverage = _distribution_coverage(
        known_count=len(publication_values),
        missing_count=missing_publication_count,
        excluded_count=future_publication_count,
    )
    lifetime_views_per_day, lifetime_views_per_day_coverage = (
        _lifetime_views_per_day_values(matched_rows, now=generated_at)
    )
    likes_per_1000_views, likes_per_1000_views_coverage = (
        _ratio_per_thousand_values(matched_rows, "like_count")
    )
    comments_per_1000_views, comments_per_1000_views_coverage = (
        _ratio_per_thousand_values(matched_rows, "comment_count")
    )
    ordered_publication_values = sorted(publication_values)
    publication_gap_days = [
        _timedelta_seconds(current - previous) / 86_400
        for previous, current in zip(
            ordered_publication_values,
            ordered_publication_values[1:],
        )
    ]
    publication_gap_coverage = _distribution_coverage(
        known_count=len(publication_gap_days),
        missing_count=missing_publication_count,
        excluded_count=(
            future_publication_count + (1 if publication_values else 0)
        ),
    )
    video_age_days = [
        _timedelta_seconds(generated_at - published) / 86_400
        for published in publication_values
    ]
    title_length_characters = [
        len(title)
        for row in matched_rows
        if isinstance((title := row.get("title")), str)
    ]
    tag_counts = [
        len(tags)
        for row in matched_rows
        if (
            isinstance((tags := row.get("tags")), list)
            and all(isinstance(tag, str) for tag in tags)
        )
    ]

    def text_identity(row, field):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        normalized = value.strip()
        return normalized, normalized

    def category_identity(row):
        value = row.get("category_id")
        if not isinstance(value, str) or not value.strip():
            return None
        category = value.strip()
        return category, category_label(category)

    def caption_identity(row):
        value = row.get("caption_available")
        if not isinstance(value, bool):
            return None
        return (
            ("available", "Captions available")
            if value
            else ("unavailable", "No captions")
        )

    def catalogue_age_identity(row):
        published = _timestamp(row.get("published_at"))
        if published is None or published > generated_at:
            return None
        age_days = _timedelta_seconds(generated_at - published) / 86_400
        if age_days < 365:
            return "under_1y", "Under 1 year"
        if age_days < 365 * 3:
            return "1_3y", "1-3 years"
        if age_days < 365 * 5:
            return "3_5y", "3-5 years"
        return "5y_plus", "5+ years"

    content_breakdown = {
        "official_categories": _single_label_section(
            matched_rows,
            source="youtube.video.snippet.categoryId",
            identity=category_identity,
        ),
        "youtube_topics": _youtube_topic_section(matched_rows),
        "audio_languages": _single_label_section(
            matched_rows,
            source="youtube.video.snippet.defaultAudioLanguage",
            identity=lambda row: text_identity(row, "default_audio_language"),
        ),
        "metadata_languages": _single_label_section(
            matched_rows,
            source="youtube.video.snippet.defaultLanguage",
            identity=lambda row: text_identity(row, "default_language"),
        ),
        "captions": _single_label_section(
            matched_rows,
            source="youtube.video.contentDetails.caption",
            identity=caption_identity,
        ),
        "catalogue_age": _single_label_section(
            matched_rows,
            source="leti_cook.video_age_from_published_at",
            identity=catalogue_age_identity,
        ),
    }
    publication_calendar = _publication_calendar(
        inventory_publication_rows,
        now=generated_at,
        zone=zone,
    )
    result = {
        "contract_version": 1,
        "video_type": "long",
        "format": "long",
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "period": period_details,
        "filters": _public_filters(query),
        "sample": {
            "scope": "filtered_current_typed_long_scan",
            "scan_limit": None if full_scope else SCAN_LIMIT,
            **({"scan_mode": "full_catalogue"} if full_scope else {}),
            "scanned_inventory_count": len(inventory),
            "inventory_pagination": "published_at_video_id_keyset",
            "transactional_snapshot": False,
            "inventory_complete": not truncated,
            "truncated": truncated,
            "loaded_video_count": len(loaded),
            "missing_video_row_count": max(0, len(inventory_ids) - len(rows_by_id)),
            "withheld_stale_count": len(loaded) - len(current_rows),
            "excluded_by_availability_count": len(current_rows) - len(eligible_rows),
            "eligible_current_count": len(eligible_rows),
            "matched_video_count": len(matched_rows),
            "period_future_excluded_count": len(period_future_rows),
            "filtered_out_count": len(eligible_rows) - len(matched_rows),
        },
        "statistics": {
            "views": _metric_summary(matched_rows, lambda row: _source_metric(row.get("views"))),
            "likes": _metric_summary(matched_rows, lambda row: _source_metric(row.get("like_count"))),
            "comments": _metric_summary(matched_rows, lambda row: _source_metric(row.get("comment_count"))),
            "duration_seconds": _metric_summary(
                matched_rows,
                lambda row: youtube_duration_seconds(row.get("duration")),
            ),
            "lifetime_views_per_day": _value_summary(
                lifetime_views_per_day,
                sample_count=len(matched_rows),
            ),
            "likes_per_1000_views": _value_summary(
                likes_per_1000_views,
                sample_count=len(matched_rows),
            ),
            "comments_per_1000_views": _value_summary(
                comments_per_1000_views,
                sample_count=len(matched_rows),
            ),
            "title_length_characters": _value_summary(
                title_length_characters,
                sample_count=len(matched_rows),
            ),
            "tag_count": _value_summary(
                tag_counts,
                sample_count=len(matched_rows),
            ),
        },
        "publication_cadence": _publication_cadence(matched_rows, now=generated_at),
        "publication_points": publication_points,
        "publication_calendar": publication_calendar,
        "content_breakdown": content_breakdown,
        "distributions": {
            "views": _bucket_distribution(
                view_values,
                bucket_definitions("views"),
            ),
            "likes": _bucket_distribution(
                like_values,
                bucket_definitions("likes"),
            ),
            "comments": _bucket_distribution(
                comment_values,
                bucket_definitions("comments"),
            ),
            "lifetime_views_per_day": _bucket_distribution(
                lifetime_views_per_day,
                (
                    ("under_10", "Under 10", None, 10),
                    ("10_100", "10–100", 10, 100),
                    ("100_1k", "100–1K", 100, 1_000),
                    ("1k_10k", "1K–10K", 1_000, 10_000),
                    ("10k_100k", "10K–100K", 10_000, 100_000),
                    ("100k_plus", "100K+", 100_000, None),
                ),
            ),
            "likes_per_1000_views": _bucket_distribution(
                likes_per_1000_views,
                (
                    ("0_5", "0–5", 0, 5),
                    ("5_15", "5–15", 5, 15),
                    ("15_30", "15–30", 15, 30),
                    ("30_plus", "30+", 30, None),
                ),
            ),
            "comments_per_1000_views": _bucket_distribution(
                comments_per_1000_views,
                (
                    ("0_0_5", "0–0.5", 0, Fraction(1, 2)),
                    ("0_5_2", "0.5–2", Fraction(1, 2), 2),
                    ("2_5", "2–5", 2, 5),
                    ("5_plus", "5+", 5, None),
                ),
            ),
            "duration_seconds": _bucket_distribution(
                duration_values,
                bucket_definitions("duration"),
            ),
            "publication_years": _counted_distribution(
                publication_values,
                lambda published: (str(published.year), str(published.year)),
                sort_key=lambda identity: identity[0],
            ),
            "publication_quarters": _counted_distribution(
                publication_values,
                lambda published: (
                    f"{published.year}-Q{(published.month - 1) // 3 + 1}",
                    f"Q{(published.month - 1) // 3 + 1} {published.year}",
                ),
                sort_key=lambda identity: identity[0],
            ),
            "video_age_days": _youtube_day_range_distribution(video_age_days),
            "categories": categories,
            "known_category_count": known_category_count,
            "unknown_category_count": unknown_category_count,
            "publication_gap_days": _youtube_day_range_distribution(
                publication_gap_days
            ),
            "publication_weekdays_utc": _bucket_distribution(
                [published.weekday() for published in publication_values],
                (
                    ("monday", "Monday", 0, 1),
                    ("tuesday", "Tuesday", 1, 2),
                    ("wednesday", "Wednesday", 2, 3),
                    ("thursday", "Thursday", 3, 4),
                    ("friday", "Friday", 4, 5),
                    ("saturday", "Saturday", 5, 6),
                    ("sunday", "Sunday", 6, 7),
                ),
            ),
            "publication_hours_utc": _bucket_distribution(
                [published.hour for published in publication_values],
                (
                    ("00_06", "00:00–06:00", 0, 6),
                    ("06_12", "06:00–12:00", 6, 12),
                    ("12_18", "12:00–18:00", 12, 18),
                    ("18_24", "18:00–24:00", 18, 24),
                ),
            ),
            "title_length_characters": _bucket_distribution(
                title_length_characters,
                bucket_definitions("title_length"),
            ),
            "tag_count": _bucket_distribution(
                tag_counts,
                bucket_definitions("tag_count"),
            ),
        },
        "distribution_coverage": {
            "views": _distribution_coverage(
                known_count=len(view_values),
                missing_count=len(matched_rows) - len(view_values),
            ),
            "likes": _distribution_coverage(
                known_count=len(like_values),
                missing_count=len(matched_rows) - len(like_values),
            ),
            "comments": _distribution_coverage(
                known_count=len(comment_values),
                missing_count=len(matched_rows) - len(comment_values),
            ),
            "lifetime_views_per_day": lifetime_views_per_day_coverage,
            "likes_per_1000_views": likes_per_1000_views_coverage,
            "comments_per_1000_views": comments_per_1000_views_coverage,
            "duration_seconds": _distribution_coverage(
                known_count=len(duration_values),
                missing_count=len(matched_rows) - len(duration_values),
            ),
            "publication_years": dict(publication_coverage),
            "publication_quarters": dict(publication_coverage),
            "video_age_days": dict(publication_coverage),
            "categories": _distribution_coverage(
                known_count=known_category_count,
                missing_count=unknown_category_count,
            ),
            "publication_gap_days": publication_gap_coverage,
            "publication_weekdays_utc": dict(publication_coverage),
            "publication_hours_utc": dict(publication_coverage),
            "title_length_characters": _distribution_coverage(
                known_count=len(title_length_characters),
                missing_count=len(matched_rows) - len(title_length_characters),
            ),
            "tag_count": _distribution_coverage(
                known_count=len(tag_counts),
                missing_count=len(matched_rows) - len(tag_counts),
            ),
        },
        "freshness": {
            "maximum_age_days": RAW_DATA_MAX_AGE.days,
            "counters": _freshness(matched_rows, "checked_at"),
            "metadata": _freshness(matched_rows, "metadata_checked_at"),
            "category_cache_available": False,  # Retained wire fields; labels use the static registry.
            "category_cache_fresh": False,
        },
        "methods": {
            "period_semantics": "published_at_rolling_window_current_counters",
            "nulls": "excluded_per_metric_not_zero",
            "quartiles": "linear_interpolation_n_minus_1",
            "date_filter": "video_publication_timestamp_half_open",
            "counter_totals": "sum_of_current_lifetime_video_counters",
            "catalogue_filters_applied": True,
            "publication_timezone": "UTC",
            "video_age": "generated_at_minus_published_at",
            "lifetime_views_per_day": (
                "views_divided_by_exact_checked_at_minus_published_at_minimum_24_hours"
            ),
            "per_1000_views": "stored_counter_divided_by_positive_views_times_1000",
            "publication_gaps": (
                "between_consecutive_matched_publications_chronological"
            ),
            "title_length": "unicode_code_points",
            "tag_count": "stored_string_list_length_empty_list_is_zero",
            "content_breakdown_single_label_share": "known_rows_denominator",
            "youtube_topics_share": "active_matched_sample_denominator_multi_label",
            "youtube_topics_coverage": (
                "null_is_not_collected_empty_array_is_collected_without_assigned_topics"
            ),
            "publication_points": (
                "selected_period_typed_long_inventory_valid_non_future_publications"
            ),
            "publication_calendar": (
                "typed_long_inventory_lifetime_valid_non_future_publications_independent_of_period"
            ),
        },
        "source": {
            "kind": "derived_from_stored_current_youtube_fields",
            "format_source": "video_inventory.video_type",
            "youtube_requests": False,
            "publication_source": "video_inventory.video_id,published_at",
            "database_writes": False,
            "snapshots_used": False,
            "availability_history_used": False,
            "trends_used": False,
            "performance_scores_used": False,
            "freshness_enforced": True,
        },
    }
    sample_expiry = rows_expiry(matched_rows)
    for section in ("statistics", "publication_cadence", "content_breakdown", "distributions"):
        stamp_aggregate(result[section], sample_expiry)
    category_expiry = minimum_expiry(sample_expiry, *category_expiries)
    stamp_aggregate(result["content_breakdown"]["official_categories"], category_expiry)
    stamp_aggregate(result["distributions"]["categories"], category_expiry)
    stamp_aggregate(result["publication_calendar"], rows_expiry(inventory, "source_checked_at"))
    result["source_expires_at"] = minimum_expiry(sample_expiry, rows_expiry(inventory_publication_rows), category_expiry)
    return repository.finish(result) if full_scope else result
