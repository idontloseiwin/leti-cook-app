"""Fixed-window video view growth for the card-first Explorer catalogue."""

from __future__ import annotations

from video_thumbnails import video_thumbnail_url

from catalogue_static import stored_tags

from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any

from catalogue_scope import CatalogueReadBudget, CatalogueScopeRepository, CatalogueScopeError

from catalogue_filters import is_catalogue_eligible
from channel_explorer import (
    CHANNEL_COLUMNS,
    DB_BATCH_SIZE,
    DEFAULT_PAGE_SIZE,
    PUBLIC_CHANNEL_FIELDS,
    PUBLIC_VIDEO_FIELDS,
    RAW_DATA_MAX_AGE,
    SCAN_LIMIT,
    SORT_FIELDS,
    VIDEO_COLUMNS,
    STREAM_VIDEO_COLUMNS,
    catalogue_video_type,
    catalogue_inventory_state,
    load_catalogue_inventory,
    catalogue_type_resolutions,
    catalogue_lifecycle_ready,
    ChannelExplorerNotFoundError,
    ChannelExplorerQueryError,
    _json_integer,
    _matches,
    _normalized_query,
    _number,
    _sort_rows,
    _timestamp,
)
from source_expiry import source_expiry, minimum_expiry, captured_source_call
from youtube_categories import youtube_video_category_name
from hot_storage import ANALYTICS_WINDOW, HOT_DATA_WINDOW, utc_instant, window_bounds, source_is_current


GROWTH_SORT_FIELD = "views_gained"
GROWTH_SORT_FIELDS = frozenset((*SORT_FIELDS, GROWTH_SORT_FIELD))
GROWTH_STATUSES = (
    "ready",
    "partial",
    "started_today",
    "waiting_next_snapshot",
    "unavailable",
)
GROWTH_WINDOW = ANALYTICS_WINDOW
GROWTH_BOUNDARY_TOLERANCE = timedelta(0)
PARTIAL_MINIMUM_SECONDS = 86_400


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nonnegative_integer(value: Any) -> int | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _canonical_nullable_integer(value: Any) -> tuple[bool, int | None]:
    if value is None:
        return True, None
    if isinstance(value, bool):
        return False, None
    if isinstance(value, int):
        return True, value
    if not isinstance(value, str):
        return False, None
    normalized = value.strip()
    if not normalized:
        return False, None
    digits = normalized[1:] if normalized[:1] in {"+", "-"} else normalized
    if not digits or not digits.isdigit():
        return False, None
    try:
        return True, int(normalized)
    except ValueError:
        return False, None


def _elapsed_seconds(start: datetime, end: datetime) -> int | None:
    elapsed = end - start
    if elapsed < timedelta(0):
        return None
    return (
        elapsed.days * 86_400 + elapsed.seconds
    )


def _average_views_per_day(delta: int, interval: int) -> str:
    """Round an exact signed interval mean to 2dp, ties-to-even, without floats."""
    hundredths = round(Fraction(delta * 86_400, interval) * 100)
    whole, fraction = divmod(abs(hundredths), 100)
    sign = "-" if hundredths < 0 else ""
    suffix = f".{fraction:02d}".rstrip("0") if fraction else ""
    return f"{sign}{whole}{suffix}"


def _unavailable_growth(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "delta_views": None,
        "average_views_per_day": None,
        "tracked_days": None,
        "observed_start_at": None,
        "observed_end_at": None,
        "actual_interval_seconds": None,
        "boundary_distance_seconds": None,
        "reason": reason,
    }


def _normalized_growth(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return _unavailable_growth("growth_row_missing")

    status = str(raw.get("growth_status") or "").strip()
    if status not in GROWTH_STATUSES:
        return _unavailable_growth("invalid_growth_status")

    start = _timestamp(raw.get("observed_start_at"))
    end = _timestamp(raw.get("observed_end_at"))
    interval = _nonnegative_integer(raw.get("actual_interval_seconds"))
    boundary_distance = _nonnegative_integer(raw.get("boundary_distance_seconds"))
    tracked_days = _nonnegative_integer(raw.get("tracked_days"))
    delta = _number(raw.get("delta_views"))
    reason_value = raw.get("reason")
    reason = str(reason_value).strip() if reason_value is not None else None
    reason = reason or None

    measured_interval = (
        _elapsed_seconds(start, end) if start is not None and end is not None else None
    )
    if status == "ready":
        if (
            delta is None
            or start is None
            or end is None
            or interval is None
            or interval != int(GROWTH_WINDOW.total_seconds())
            or boundary_distance is None
            or boundary_distance > int(GROWTH_BOUNDARY_TOLERANCE.total_seconds())
            or measured_interval != interval
            or raw.get("tracked_days") is not None
            or reason is not None
        ):
            return _unavailable_growth("invalid_ready_growth_row")
        tracked_days = None
        reason = None
    elif status == "partial":
        if (
            delta is None
            or start is None
            or end is None
            or interval is None
            or interval < PARTIAL_MINIMUM_SECONDS
            or interval >= int(GROWTH_WINDOW.total_seconds())
            or tracked_days is None
            or tracked_days < 1
            or measured_interval != interval
            or tracked_days != interval // PARTIAL_MINIMUM_SECONDS
            or raw.get("boundary_distance_seconds") is not None
            or reason is None
        ):
            return _unavailable_growth("invalid_partial_growth_row")
        boundary_distance = None
        reason = reason or "full_window_history_unavailable"
    elif status in {"started_today", "waiting_next_snapshot"}:
        delta = None
        tracked_days = None
        boundary_distance = None
        if (
            raw.get("delta_views") is not None
            or raw.get("tracked_days") is not None
            or raw.get("boundary_distance_seconds") is not None
            or reason is None
            or
            start is None
            or end is None
            or interval is None
            or interval >= PARTIAL_MINIMUM_SECONDS
            or measured_interval != interval
        ):
            return _unavailable_growth("invalid_tracking_growth_row")
        reason = reason or (
            "tracking_started_today"
            if status == "started_today"
            else "waiting_for_next_snapshot"
        )
    else:
        delta = None
        tracked_days = None
        boundary_distance = None
        if (
            raw.get("delta_views") is not None
            or raw.get("tracked_days") is not None
            or raw.get("boundary_distance_seconds") is not None
            or start is not None
            or interval is not None
            or reason is None
        ):
            return _unavailable_growth("invalid_unavailable_growth_row")
        reason = reason or "growth_unavailable"

    return {
        "status": status,
        "delta_views": _json_integer(delta) if delta is not None else None,
        "average_views_per_day": (
            _average_views_per_day(delta, interval)
            if status in {"ready", "partial"} and delta is not None and interval is not None
            else None
        ),
        "tracked_days": tracked_days,
        "observed_start_at": _iso(start) if start is not None else None,
        "observed_end_at": _iso(end) if end is not None else None,
        "actual_interval_seconds": interval,
        "boundary_distance_seconds": boundary_distance,
        "reason": reason,
    }


def _growth_sort_value(row: dict[str, Any]) -> int | None:
    growth = row.get("view_growth")
    if not isinstance(growth, dict) or growth.get("status") not in {"ready", "partial"}:
        return None
    return _number(growth.get("delta_views"))


def normalize_growth_for_video(
    row: dict[str, Any], raw_growth: dict[str, Any] | None, *, now: datetime | None = None,
    source_valid_until: datetime | None = None,
) -> dict[str, Any]:
    """One integrity boundary shared by catalogue and frozen contextual facets."""
    normalized = _normalized_growth(raw_growth)
    if raw_growth is None:
        return normalized
    expected_end_at = _timestamp(row.get("checked_at"))
    expected_valid, expected_views = _canonical_nullable_integer(row.get("views"))
    returned_valid, returned_views = _canonical_nullable_integer(raw_growth.get("observed_current_views"))
    if (
        expected_end_at is None
        or _timestamp(raw_growth.get("observed_end_at")) != expected_end_at
        or not expected_valid or "observed_current_views" not in raw_growth
        or not returned_valid or returned_views != expected_views
    ):
        return _unavailable_growth("endpoint_changed_during_read")
    bounds = window_bounds(now=now)
    for field in ("observed_start_at", "observed_end_at"):
        value = raw_growth.get(field)
        if value is None:
            continue
        try:
            observed = utc_instant(value)
        except ValueError:
            return _unavailable_growth("invalid_growth_clock")
        if not bounds[0] <= observed <= bounds[1] or not source_is_current(
            observed, now=now, valid_until=source_valid_until,
        ):
            return _unavailable_growth("history_outside_hot_window")
    normalized["source_expires_at"] = minimum_expiry(
        source_expiry(row, "checked_at"), source_expiry(raw_growth, "observed_start_at", "observed_end_at"))
    return normalized


def _sort_growth_rows(rows: list[dict[str, Any]], *, order: str) -> list[dict[str, Any]]:
    present: list[tuple[int, str, dict[str, Any]]] = []
    missing: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        video_id = str(row.get("video_id") or "")
        value = _growth_sort_value(row)
        if value is None:
            missing.append((video_id, row))
        else:
            present.append((value, video_id, row))
    present.sort(key=lambda item: item[1])
    present.sort(key=lambda item: item[0], reverse=order == "desc")
    missing.sort(key=lambda item: item[0])
    return [item[2] for item in present] + [item[1] for item in missing]


def _growth_query(
    *,
    q: Any,
    category_id: Any,
    published_from: Any,
    published_before: Any,
    min_views: Any,
    max_views: Any,
    min_duration_seconds: Any,
    max_duration_seconds: Any,
    sort_by: Any,
    sort_order: Any,
    limit: Any,
    offset: Any,
    catalogue_filters: Any = None,
) -> dict[str, Any]:
    requested_sort = str(sort_by or "published_at").strip()
    if requested_sort not in GROWTH_SORT_FIELDS:
        raise ChannelExplorerQueryError("unsupported sort_by")
    query = _normalized_query(
        q=q,
        category_id=category_id,
        published_from=published_from,
        published_before=published_before,
        min_views=min_views,
        max_views=max_views,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        sort_by="published_at" if requested_sort == GROWTH_SORT_FIELD else requested_sort,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
        catalogue_filters=catalogue_filters,
        allow_growth_filters=True,
    )
    query["sort_by"] = requested_sort
    return query


def build_channel_video_growth(
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
    sort_by: Any = "published_at",
    sort_order: Any = "desc",
    limit: Any = DEFAULT_PAGE_SIZE,
    offset: Any = 0,
    video_type: str = "long",
    typed_catalogue_read: bool = False,
    catalogue_filters: Any = None,
    full_scope: bool = False,
    all_rows: bool = False,
    read_budget: CatalogueReadBudget | None = None,
    now: datetime | None = None,
    source_valid_until: datetime | None = None,
) -> dict[str, Any]:
    """Build one catalogue page with honest stored-snapshot view growth."""

    selected_video_type = catalogue_video_type(video_type)
    if all_rows and not full_scope:
        raise ChannelExplorerQueryError("all_rows requires full_scope")
    if full_scope:
        repository = CatalogueScopeRepository(repository, read_budget)

    query = _growth_query(
        q=q,
        category_id=category_id,
        published_from=published_from,
        published_before=published_before,
        min_views=min_views,
        max_views=max_views,
        min_duration_seconds=min_duration_seconds,
        max_duration_seconds=max_duration_seconds,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
        catalogue_filters=catalogue_filters,
    )
    if all_rows and query["offset"] != 0:
        raise ChannelExplorerQueryError("all_rows requires offset zero")
    dynamic_filters = bool(query.get("catalogue_filters", {}).get("growth"))
    if dynamic_filters and not full_scope:
        raise ChannelExplorerQueryError("Growth conditions require full-catalogue browse")
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    channel = repository.load_channel(channel_id=channel_id, columns=CHANNEL_COLUMNS)
    if not channel or channel.get("is_hidden"):
        raise ChannelExplorerNotFoundError("Channel not found")

    inventory_state, type_coverage = catalogue_inventory_state(repository, channel_id, selected_video_type,
        typed_catalogue_read=typed_catalogue_read, now=generated_at, source_valid_until=source_valid_until)
    inventory = load_catalogue_inventory(repository, channel_id, selected_video_type, full_scope=full_scope) if type_coverage["inventory_ready"] else []
    truncated = not full_scope and len(inventory) > SCAN_LIMIT
    if not full_scope:
        inventory = inventory[:SCAN_LIMIT]
    inventory_before_admission = len(inventory)
    inventory = [row for row in inventory if source_is_current(
        row.get("source_checked_at"), now=generated_at, valid_until=source_valid_until,
    )]
    inventory_withheld = inventory_before_admission - len(inventory)
    video_ids = [str(row.get("video_id")) for row in inventory if row.get("video_id")]
    video_ids, resolution_expiries, type_resolution_withheld = catalogue_type_resolutions(
        repository, channel_id, selected_video_type, video_ids, typed_catalogue_read=typed_catalogue_read,
        now=generated_at, source_valid_until=source_valid_until)
    inventory_ids = set(video_ids)

    loaded_rows: list[dict[str, Any]] = []
    for start in range(0, len(video_ids), DB_BATCH_SIZE):
        loaded_rows.extend(repository.load_videos_by_ids(
            video_ids[start:start + DB_BATCH_SIZE],
            columns=(STREAM_VIDEO_COLUMNS if selected_video_type == "stream" else VIDEO_COLUMNS)
                + (",tags" if catalogue_filters is not None else "")
                + (",channel_id,publication_status" if selected_video_type != "long" else ""),
        ))
    rows = [
        dict(row)
        for row in loaded_rows
        if str(row.get("video_id") or "") in inventory_ids
    ]
    loaded_ids = {str(row.get("video_id") or "") for row in rows}
    same_channel = rows if selected_video_type == "long" or not typed_catalogue_read else [
        row for row in rows if row.get("channel_id") == channel_id]
    identity_withheld = len(rows) - len(same_channel)
    rows = same_channel
    lifecycle_rows = rows if selected_video_type == "long" else [row for row in rows
        if catalogue_lifecycle_ready(row, selected_video_type, now=generated_at)]
    lifecycle_withheld = len(rows) - len(lifecycle_rows)
    rows = lifecycle_rows
    # Counting rejected source rows must not register their earlier expiry.
    # Only evidence admitted below can limit this response or browse generation.
    bounds = window_bounds(now=generated_at)
    def readable_clock(value):
        try:
            return bounds[0] <= utc_instant(value) <= bounds[1]
        except (TypeError, ValueError):
            return False
    stale_counter_rows = sum(not readable_clock(row.get("checked_at")) for row in rows)
    stale_metadata_rows = sum(not readable_clock(row.get("metadata_checked_at")) for row in rows)
    current_rows = [row for row in rows if readable_clock(row.get("checked_at"))
                    and readable_clock(row.get("metadata_checked_at"))]
    stale_video_rows = len(rows) - len(current_rows)
    before_admission = len(current_rows)
    current_rows = [row for row in current_rows if all(source_is_current(
        row.get(field), now=generated_at, valid_until=source_valid_until,
    ) for field in ("checked_at", "metadata_checked_at"))]
    source_expiry_excluded = before_admission - len(current_rows)

    public_rows = [row for row in current_rows if row.get("availability_status") == "public"
        and source_is_current(row.get("availability_checked_at"), now=generated_at,
                              valid_until=source_valid_until)]
    catalogue_exclusions = {"availability_excluded_count": len(current_rows) - len(public_rows)}
    current_rows = public_rows
    if catalogue_filters is not None or selected_video_type != "long":
        eligible_rows = [row for row in current_rows if is_catalogue_eligible(row, now=generated_at)]
        catalogue_exclusions.update({
            "future_excluded_count": len(current_rows) - len(eligible_rows),
            "missing_video_row_count": len(inventory_ids - loaded_ids),
        })
        current_rows = eligible_rows
    # Contextual facets remove one static field at a time. Freeze growth for all
    # eligible rows, not just this query's static matches, when dynamics apply.
    growth_scope = current_rows if dynamic_filters else [row for row in current_rows if _matches(row, query)]
    matched_ids = [str(row.get("video_id") or "") for row in growth_scope]
    matched_id_set = set(matched_ids)

    raw_growth_rows = repository.load_video_view_growth(
        channel_id,
        matched_ids,
        as_of=generated_at,
        **({"video_type": selected_video_type} if selected_video_type != "long" else {}),
        **({"recorded_from": utc_instant(source_valid_until) - HOT_DATA_WINDOW}
           if source_valid_until is not None else {}),
    )
    growth_rows_by_id: dict[str, dict[str, Any]] = {}
    for raw_growth in raw_growth_rows:
        video_id = str(raw_growth.get("video_id") or "")
        if video_id not in matched_id_set:
            continue
        if video_id in growth_rows_by_id:
            raise RuntimeError("video growth RPC returned a duplicate video ID")
        growth_rows_by_id[video_id] = dict(raw_growth)
    if dynamic_filters and matched_id_set != set(growth_rows_by_id):
        raise CatalogueScopeError("catalogue_growth_evidence_incomplete")
    for row in growth_scope:
        video_id = str(row.get("video_id") or "")
        raw_growth = growth_rows_by_id.get(video_id)
        row["view_growth"] = normalize_growth_for_video(
            row, raw_growth, now=generated_at, source_valid_until=source_valid_until,
        )

    matched_rows = [row for row in growth_scope if _matches(row, query)] if dynamic_filters else growth_scope

    if query["sort_by"] == GROWTH_SORT_FIELD:
        matched_rows = _sort_growth_rows(matched_rows, order=query["sort_order"])
    else:
        matched_rows = _sort_rows(
            matched_rows,
            field=query["sort_by"],
            order=query["sort_order"],
        )

    inventory_expiries = {str(item.get("video_id")): source_expiry(item, "source_checked_at") for item in inventory}
    for row in matched_rows:
        row["source_expires_at"] = minimum_expiry(source_expiry(row),
            resolution_expiries.get(str(row.get("video_id"))), source_expiry(inventory_state))
    present_category_names: dict[str, str] = {}
    category_expiries = {}
    for row in matched_rows:
        present_category = str(row.get("category_id") or "").strip()
        if present_category and present_category not in present_category_names:
            present_category_names[present_category], category_expiries[present_category] = captured_source_call(youtube_video_category_name, present_category)

    category_option_expiries = {key: minimum_expiry(category_expiries[key],
        *(minimum_expiry(source_expiry(row, "checked_at", "metadata_checked_at", "availability_checked_at"),
                        inventory_expiries.get(str(row.get("video_id"))), source_expiry(row.get("view_growth")))
          for row in matched_rows if str(row.get("category_id") or "").strip() == key))
        for key in present_category_names}
    matched_count = len(matched_rows)
    page = matched_rows if all_rows else matched_rows[query["offset"]:query["offset"] + query["limit"]]
    public_rows = []
    for row in page:
        public_row = {field: row.get(field) for field in PUBLIC_VIDEO_FIELDS}
        public_row["thumbnail_url"] = video_thumbnail_url(row.get("video_id"))
        if catalogue_filters is not None:
            public_row["tags"] = stored_tags(row)
        present_category = str(public_row.get("category_id") or "").strip()
        public_row["category_name"] = (
            present_category_names.get(present_category)
            if present_category
            else None
        )
        public_row["view_growth"] = row["view_growth"]
        public_row["source_expires_at"] = minimum_expiry(
            source_expiry(row, "checked_at", "metadata_checked_at", "availability_checked_at"),
            inventory_expiries.get(str(row.get("video_id"))), category_expiries.get(present_category),
            source_expiry(public_row["view_growth"]))
        public_rows.append(public_row)

    channel_data_fresh = channel.get("channel_metadata_status") == "ok" and source_is_current(
        channel.get("channel_metadata_checked_at"), now=generated_at, valid_until=source_valid_until,
    )
    public_channel = {
        field: channel.get(field)
        if channel_data_fresh or field in {"channel_id", "channel_metadata_checked_at"}
        else None
        for field in PUBLIC_CHANNEL_FIELDS
    }
    public_channel["source_expires_at"] = source_expiry(channel, "channel_metadata_checked_at") if channel_data_fresh else None
    status_counts = {
        status: sum(
            row["view_growth"]["status"] == status
            for row in growth_scope
        )
        for status in GROWTH_STATUSES
    }

    result = {
        "contract_version": 1,
        "video_type": selected_video_type,
        "generated_at": _iso(generated_at),
        "channel": public_channel,
        "filters": {
            key: _iso(value) if isinstance(value, datetime) else value
            for key, value in query.items()
        },
        "coverage": {
            "scope": f"typed_{selected_video_type}_inventory",
            "video_type": selected_video_type,
            **type_coverage,
            "type_resolution_withheld": type_resolution_withheld,
            "video_rows_withheld_lifecycle": lifecycle_withheld,
            "video_rows_withheld_identity": identity_withheld,
            "inventory_complete": type_coverage["inventory_ready"] and not truncated,
            "scan_limit": None if full_scope else SCAN_LIMIT,
            **({"scan_mode": "full_catalogue"} if full_scope else {}),
            "scanned_count": inventory_before_admission,
            "inventory_rows_withheld_source": inventory_withheld,
            "video_rows_withheld_expiring": source_expiry_excluded,
            "source_valid_until": _iso(utc_instant(source_valid_until)) if source_valid_until is not None else None,
            "inventory_pagination": "published_at_video_id_keyset",
            "transactional_snapshot": False,
            "complete": type_coverage["inventory_ready"] and type_resolution_withheld == 0 and identity_withheld == 0 and lifecycle_withheld == 0 and not truncated and inventory_withheld == 0 and source_expiry_excluded == 0 and catalogue_exclusions["availability_excluded_count"] == 0 and ((catalogue_filters is None and selected_video_type == "long") or (stale_video_rows == 0 and catalogue_exclusions["missing_video_row_count"] == 0)),
            "truncated": truncated,
            "category_cache_available": False,  # Retained wire fields; labels use the static registry.
            "category_cache_fresh": False,
            "category_cache_refreshed_at": None,
            "current_data_max_age_days": RAW_DATA_MAX_AGE.days,
            "channel_data_fresh": channel_data_fresh,
            "video_rows_withheld_stale": stale_video_rows,
            "video_rows_with_stale_counters": stale_counter_rows,
            "video_rows_with_stale_metadata": stale_metadata_rows,
            **catalogue_exclusions,
            "growth": {
                "requested_video_count": len(growth_scope),
                "returned_growth_row_count": len(growth_rows_by_id),
                "calculable_count": status_counts["ready"] + status_counts["partial"],
                "ready_count": status_counts["ready"],
                "partial_count": status_counts["partial"],
                "started_today_count": status_counts["started_today"],
                "waiting_next_snapshot_count": status_counts["waiting_next_snapshot"],
                "unavailable_count": status_counts["unavailable"],
            },
        },
        "pagination": {
            "limit": query["limit"],
            "offset": query["offset"],
            "returned_count": len(public_rows),
            "has_more_in_scan": query["offset"] + len(public_rows) < matched_count,
            **({"matched_count": matched_count} if catalogue_filters is not None else {}),
        },
        "options": {
            "categories": sorted(
                [
                    {"category_id": category, "category_name": name, "source_expires_at": category_option_expiries.get(category)}
                    for category, name in present_category_names.items()
                ],
                key=lambda row: (row["category_name"], row["category_id"]),
            ),
            "sort_fields": sorted(GROWTH_SORT_FIELDS),
        },
        "videos": public_rows,
        "window": {
            "metric": "views",
            "requested_days": GROWTH_WINDOW.days,
            "boundary_tolerance_days": GROWTH_BOUNDARY_TOLERANCE.days,
            "endpoint": "per_video_current_checked_at",
        },
        "methods": {
            "endpoint_observation": "current_video_row_with_matching_snapshot",
            "full_window_start": "exact_28d_interval_inside_hot_window",
            "partial_window_start": "earliest_snapshot_inside_hot_window",
            "partial_minimum_seconds": PARTIAL_MINIMUM_SECONDS,
            "counter_decrease": "reported_as_correction_not_clamped",
            "average_views_per_day": "signed_delta_per_actual_second_times_86400_rounded_2dp_half_even",
            "sorting": "signed_delta_ready_and_partial_nulls_last_then_video_id",
            "filters_applied_before_growth_read": not dynamic_filters,
            **({
                "growth_filters_applied_after_growth_read": True,
                "read_scope": "full_eligible_catalogue_for_contextual_facets",
            } if dynamic_filters else {}),
            "pagination_applied_after_sorting": True,
        },
        "source": {
            "kind": "derived_video_view_growth_from_stored_snapshots",
            "format_source": "video_inventory.video_type",
            "youtube_requests": False,
            "database_writes": False,
            "snapshots_used": True,
            "availability_history_used": False,
            "derived_performance_metrics": True,
            "forecasts_used": False,
            "performance_ranks_used": False,
            "freshness_enforced": True,
        },
    }
    return repository.finish(result) if full_scope else result
