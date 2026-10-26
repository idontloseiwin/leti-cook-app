"""Three bounded analytics queries used by the named read repositories.

SQL owns windows, aggregation, type evidence and source freshness. Each
nonempty operation issues one data query through the ordinary read gate. Growth
first disables JIT inside its own read-only transaction; no pool setting changes.
The caller still owns freshness validation after transport and cache admission.
"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from hot_storage import iso_utc, utc_now, utc_instant, register_read_deadline, HotSourceReadExpired
from ..base.postgres import DatabaseError, Operation
from ..base.sql_values import application_value

TIMELINE_COLUMNS = frozenset(("bucket_at", "recorded_at", "views", "subscribers",
                             "correction_within_bucket", "scan_truncated", "snapshot_count"))
GROWTH_COLUMNS = frozenset(("video_id", "growth_status", "delta_views", "tracked_days",
                          "observed_start_at", "observed_end_at", "observed_current_views",
                          "actual_interval_seconds", "boundary_distance_seconds", "reason"))
SOURCE_COLUMNS = frozenset(("video_id", "channel_id", "source_status", "source_reason", "video_row"))
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


class AnalyticsReadResultError(DatabaseError):
    """A query result failed the local shape/identity contract."""
    def __init__(self):
        super().__init__("database_result_shape_invalid")


def _invalid(state="P0001"):
    # Local domain validation has the retained RPC's public SQLSTATE. No server
    # message, invalid input, SQL or provider payload is attached to the error.
    raise DatabaseError("database_operation_failed", code=state)


def _instant(value, *, nullable=False, state="P0001"):
    if value is None:
        if nullable:
            return None
        _invalid(state)
    # Repository callers supply aware datetime / ISO-8601 strings, as before.
    # Do not implement PostgreSQL's open-ended timestamp text grammar in Python.
    if not isinstance(value, (datetime, str)):
        raise TypeError("analytics_timestamp_type_invalid")
    try:
        return iso_utc(value)
    except (ValueError, TypeError, OverflowError):
        _invalid(state)


def _integer_bound(value, *, null_default, lower, upper):
    if value is None:
        value = null_default
    if type(value) is not int:
        raise TypeError("analytics_integer_type_invalid")
    if not -(2 ** 31) <= value < 2 ** 31:
        _invalid("22003")
    return min(max(value, lower), upper)


def _text(value, *, nullable=False):
    if value is None and nullable:
        return value
    if not isinstance(value, str):
        raise TypeError("analytics_text_type_invalid")
    return value


def _rows(data, columns, maximum, *, identities=None):
    if not isinstance(data, list):
        raise AnalyticsReadResultError()
    if len(data) > maximum:
        raise DatabaseError("database_result_limit_exceeded")
    if any(not isinstance(row, dict) or set(row) != columns for row in data):
        raise AnalyticsReadResultError()
    if identities is not None:
        found = [row["video_id"] for row in data]
        if (any(not isinstance(value, str) for value in found)
                or len(found) != len(identities) or len(set(found)) != len(found)
                or set(found) != set(identities)):
            raise AnalyticsReadResultError()
        indexed = {row["video_id"]: row for row in data}
        return [indexed[value] for value in identities]
    return data


def _admit_deadlines(rows):
    """Preserve item witnesses; admit and discard only the aggregate scan clock."""
    now = utc_now()
    for row in rows:
        for value in (row["source_expires_at"], row.pop("response_source_expires_at", None)):
            if value is not None:
                deadline = utc_instant(value)
                if deadline <= now:
                    raise HotSourceReadExpired("Analytical source expired during read")
                register_read_deadline(deadline)
        if row["source_expires_at"] is not None:
            row["source_expires_at"] = iso_utc(row["source_expires_at"])
    return rows


def read_channel_timeline_series(database, *, channel_id, metric,
                                 recorded_from, recorded_through, bucket_seconds,
                                 scan_limit=20_000, return_limit=500) -> list[dict[str, Any]]:
    """Keep SQL's bucket sentinel; omitted and explicit-null limits differ."""
    if metric not in ("views", "subscribers"):
        _invalid()
    upper = _instant(recorded_through)
    lower = _instant(recorded_from, nullable=True)
    scan = _integer_bound(scan_limit, null_default=1, lower=1, upper=20_000)
    result_limit = _integer_bound(return_limit, null_default=1, lower=1, upper=500)
    bucket = _integer_bound(bucket_seconds, null_default=3600, lower=300, upper=31_536_000)
    params = {"channel_id": _text(channel_id, nullable=True), "metric": metric,
              "recorded_from": lower, "recorded_through": upper,
              "scan_limit": scan, "return_limit": result_limit, "bucket_seconds": bucket}
    data = database.read_rows("analytics_channel_timeline", TIMELINE_SQL, params,
                              max_rows=result_limit + 1)
    rows = _admit_deadlines(_rows(data, TIMELINE_COLUMNS | {"source_expires_at", "response_source_expires_at"}, result_limit + 1))
    if any(type(row["snapshot_count"]) is not int or not 0 <= row["snapshot_count"] <= scan
           or type(row["scan_truncated"]) is not bool
           or type(row["correction_within_bucket"]) is not bool for row in rows):
        raise AnalyticsReadResultError()
    return rows


def read_video_view_growth_28d(database, *, channel_id, video_ids,
                              as_of, recorded_from=None, video_type="long") -> list[dict[str, Any]]:
    """One row per distinct space-trimmed ID in its first requested order."""
    if not isinstance(video_type, str) or video_type not in {"long", "short", "stream"}:
        _invalid()
    if channel_id is None or (isinstance(channel_id, str) and not channel_id.strip(" ")):
        _invalid()
    channel_id = _text(channel_id)
    as_of = _instant(as_of)
    if video_ids is not None and not isinstance(video_ids, (list, tuple)):
        raise TypeError("analytics_video_ids_type_invalid")
    ids = [] if video_ids is None else list(video_ids)
    if len(ids) > 5000:
        _invalid()
    if any(value is None or isinstance(value, str) and not value.strip(" ") for value in ids):
        _invalid()
    if any(not isinstance(value, str) for value in ids):
        raise TypeError("analytics_video_id_type_invalid")
    lower = _instant(recorded_from, nullable=True)
    expected = list(dict.fromkeys(value.strip(" ") for value in ids))
    if not expected:
        return []
    # Avoid compiling the protected-view expressions for each bounded batch.
    # This operation owns the transaction: commit or rollback restores JIT.
    with database.transaction(Operation("analytics_video_growth", read_only=True)) as tx:
        tx.fetch_one("SELECT set_config('jit','off',true) AS value")
        data = application_value(tx.fetch_all(GROWTH_SQL,
            {"channel_id": channel_id, "video_ids": ids, "as_of": as_of,
             "recorded_from": lower, "video_type": video_type},
            max_rows=len(expected)))
    return _admit_deadlines(_rows(data, GROWTH_COLUMNS | {"source_expires_at"}, len(expected), identities=expected))


def read_saved_collection_sources(database, *, video_ids, as_of) -> list[dict[str, Any]]:
    """Exact requested IDs, bounded lateral evidence and one captured DB clock."""
    as_of = _instant(as_of, state="22023")
    if (not isinstance(video_ids, (list, tuple)) or not 1 <= len(video_ids) <= 500
            or any(not isinstance(value, str) or not _VIDEO_ID.fullmatch(value) for value in video_ids)
            or len(set(video_ids)) != len(video_ids)):
        _invalid("22023")
    ids = list(video_ids)
    data = database.read_rows("analytics_saved_sources", SAVED_SOURCES_SQL,
                              {"video_ids": ids, "as_of": as_of}, max_rows=len(ids))
    rows = _admit_deadlines(_rows(data, SOURCE_COLUMNS | {"source_expires_at"}, len(ids), identities=ids))
    if any(row["video_row"] is not None and not isinstance(row["video_row"], dict) for row in rows):
        raise AnalyticsReadResultError()
    return rows

TIMELINE_SQL = """
WITH read_clock AS MATERIALIZED (SELECT statement_timestamp() AS read_at),
bounds AS MATERIALIZED (SELECT read_at, read_at - interval '696 hours' AS hot_floor,
    greatest(%(recorded_from)s::timestamptz, read_at - interval '696 hours') AS read_from,
    least(%(recorded_through)s::timestamptz, read_at) AS read_through FROM read_clock),
selected as materialized (
        select
            candidate.recorded_at,
            candidate.views,
            candidate.subscribers,
            candidate.metric_value
        from (
            select
                snapshot.recorded_at,
                snapshot.views,
                snapshot.subscribers,
                snapshot.views as metric_value
            from readable.channel_snapshots as snapshot
            where %(metric)s::text = 'views'
                and snapshot.channel_id = %(channel_id)s::text
                and snapshot.recorded_at <= (SELECT read_through FROM bounds)
                and snapshot.recorded_at >= (SELECT read_from FROM bounds)
                and snapshot.views is not null
            union all
            select
                snapshot.recorded_at,
                snapshot.views,
                snapshot.subscribers,
                snapshot.subscribers as metric_value
            from readable.channel_snapshots as snapshot
            where %(metric)s::text = 'subscribers'
                and snapshot.channel_id = %(channel_id)s::text
                and snapshot.recorded_at <= (SELECT read_through FROM bounds)
                and snapshot.recorded_at >= (SELECT read_from FROM bounds)
                and snapshot.subscribers is not null
        ) as candidate
        order by candidate.recorded_at desc
        limit %(scan_limit)s::integer + 1
    ),
    scan_meta as materialized (
        select
            count(*) > %(scan_limit)s::integer as truncated,
            least(count(*), %(scan_limit)s::integer::bigint) as snapshot_count
        from selected
    ),
    bounded as (
        select
            selected.recorded_at,
            selected.views,
            selected.subscribers,
            selected.metric_value
        from selected
        order by selected.recorded_at desc
        limit %(scan_limit)s::integer
    ),
    predecessor as materialized (
        select
            candidate.recorded_at,
            candidate.views,
            candidate.subscribers,
            candidate.metric_value
        from (
            select
                snapshot.recorded_at,
                snapshot.views,
                snapshot.subscribers,
                snapshot.views as metric_value
            from readable.channel_snapshots as snapshot
            where %(metric)s::text = 'views'
                and (SELECT read_from FROM bounds) > (SELECT hot_floor FROM bounds)
                and snapshot.channel_id = %(channel_id)s::text
                and snapshot.recorded_at < (SELECT read_from FROM bounds)
                and snapshot.recorded_at >= (SELECT hot_floor FROM bounds)
                and snapshot.views is not null
            union all
            select
                snapshot.recorded_at,
                snapshot.views,
                snapshot.subscribers,
                snapshot.subscribers as metric_value
            from readable.channel_snapshots as snapshot
            where %(metric)s::text = 'subscribers'
                and (SELECT read_from FROM bounds) > (SELECT hot_floor FROM bounds)
                and snapshot.channel_id = %(channel_id)s::text
                and snapshot.recorded_at < (SELECT read_from FROM bounds)
                and snapshot.recorded_at >= (SELECT hot_floor FROM bounds)
                and snapshot.subscribers is not null
        ) as candidate
        order by candidate.recorded_at desc
        limit 1
    ),
    lag_input as (
        select
            bounded.recorded_at,
            bounded.views,
            bounded.subscribers,
            bounded.metric_value,
            true as in_window
        from bounded
        union all
        select
            predecessor.recorded_at,
            predecessor.views,
            predecessor.subscribers,
            predecessor.metric_value,
            false as in_window
        from predecessor
    ),
    bucket_assigned as (
        select
            date_bin(
                make_interval(secs => %(bucket_seconds)s::integer),
                lag_input.recorded_at,
                (SELECT read_from FROM bounds)
            ) as bucket_at,
            lag_input.recorded_at,
            lag_input.views,
            lag_input.subscribers,
            lag_input.metric_value,
            lag_input.in_window
        from lag_input
    ),
    annotated as (
        select
            bucket_assigned.bucket_at,
            bucket_assigned.recorded_at,
            bucket_assigned.views,
            bucket_assigned.subscribers,
            bucket_assigned.metric_value,
            bucket_assigned.in_window,
            lag(bucket_assigned.metric_value) over (
                order by bucket_assigned.recorded_at asc
            ) as previous_metric_value,
            lag(bucket_assigned.recorded_at) over (
                order by bucket_assigned.recorded_at asc
            ) as previous_recorded_at
        from bucket_assigned
    ),
    bucketed as (
        select
            annotated.bucket_at,
            max(annotated.recorded_at) as recorded_at,
            (array_agg(
                annotated.views order by annotated.recorded_at desc
            ))[1] as views,
            (array_agg(
                annotated.subscribers order by annotated.recorded_at desc
            ))[1] as subscribers,
            bool_or(
                annotated.previous_metric_value is not null
                and annotated.metric_value < annotated.previous_metric_value
            ) as correction_within_bucket,
            min(least(annotated.recorded_at, annotated.previous_recorded_at))
                + interval '696 hours' AS source_expires_at
        from annotated
        where annotated.in_window
        group by annotated.bucket_at
    ),
    limited as (
        select
            bucketed.bucket_at,
            bucketed.recorded_at,
            bucketed.views,
            bucketed.subscribers,
            bucketed.correction_within_bucket,
            bucketed.source_expires_at
        from bucketed
        order by bucketed.recorded_at desc
        limit %(return_limit)s::integer + 1
    )
    select
        limited.bucket_at,
        limited.recorded_at,
        limited.views,
        limited.subscribers,
        limited.correction_within_bucket,
        scan_meta.truncated AS scan_truncated,
        scan_meta.snapshot_count,
        limited.source_expires_at,
        least((SELECT min(recorded_at) FROM selected),
              (SELECT min(recorded_at) FROM predecessor))+interval '696 hours' AS response_source_expires_at
    from limited
    cross join scan_meta
    order by limited.recorded_at asc
"""

# The growth comparison spans 28 days (ANALYTICS_WINDOW) before each video's own
# last snapshot, inside the 29-day retention window; its two '672 hours' are
# that analytics span (the hot read floor and the per-video window start).
GROWTH_SQL = """
WITH read_clock AS MATERIALIZED (SELECT statement_timestamp() AS read_at),
bounds AS MATERIALIZED (SELECT read_at,
    greatest(read_at - interval '672 hours', %(recorded_from)s::timestamptz) AS hot_floor,
    least(%(as_of)s::timestamptz, read_at) AS read_through FROM read_clock),
requested as materialized (
        select btrim(input.raw_video_id) as requested_video_id,
            min(input.ordinality)::bigint as requested_ordinality
        from unnest(coalesce(%(video_ids)s::text[], array[]::text[]))
            with ordinality as input(raw_video_id, ordinality)
        group by btrim(input.raw_video_id)
    ), scoped as materialized (
        select requested.*, membership.video_id as scoped_video_id,
            video.views as current_views, video.checked_at as current_checked_at,
            video.metadata_checked_at, video.stream_actual_end_at,
            membership.source_expires_at AS membership_source_expires_at,
            video.source_expires_at AS current_source_expires_at,
            coalesce(video.checked_at between (SELECT hot_floor FROM bounds) and (SELECT read_through FROM bounds), false)
                and coalesce(video.metadata_checked_at between (SELECT hot_floor FROM bounds) and (SELECT read_through FROM bounds), false)
                and (%(video_type)s::text <> 'stream' or coalesce(
                    isfinite(video.stream_actual_end_at)
                    and video.stream_actual_end_at <= video.metadata_checked_at
                    and video.stream_actual_end_at <= video.checked_at, false))
                as current_fresh
        from requested
        left join lateral (
            select inventory.video_id,inventory.source_expires_at
            from readable.hot_video_inventory as inventory
            join readable.channels_with_control as channel
                on channel.channel_id = inventory.channel_id and channel.is_hidden is not true
            where inventory.channel_id = %(channel_id)s::text
                and inventory.video_type = %(video_type)s::text
                and inventory.video_id = requested.requested_video_id
                and inventory.source_checked_at between (SELECT hot_floor FROM bounds) and (SELECT read_through FROM bounds)
            limit 1
        ) as membership on true
        left join readable.videos as video
            on video.video_id = membership.video_id and video.channel_id = %(channel_id)s::text
    ), observations as materialized (
        select scoped.*, endpoint.recorded_at as endpoint_at, window_start.at as window_start_at,
            coalesce(full_point.recorded_at, first_point.recorded_at) as start_at,
            coalesce(full_point.views, first_point.views) as start_views
        from scoped
        left join lateral (
            select snapshot.recorded_at
            from readable.video_snapshots as snapshot
            where snapshot.video_id = scoped.scoped_video_id and scoped.current_fresh
                and snapshot.recorded_at = scoped.current_checked_at
                and snapshot.recorded_at between (SELECT hot_floor FROM bounds) and (SELECT read_through FROM bounds)
                and snapshot.views = scoped.current_views and snapshot.views >= 0
                and (%(video_type)s::text <> 'stream' or snapshot.recorded_at >= scoped.stream_actual_end_at)
            order by snapshot.id desc limit 1
        ) as endpoint on true
        -- The window is the 28 days before the video's last snapshot, not before
        -- this read. A full window starts at the latest snapshot at or before
        -- that start (retention keeps at most one more day), otherwise at the
        -- earliest snapshot after it.
        cross join lateral (select endpoint.recorded_at - interval '672 hours' as at) as window_start
        left join lateral (
            select snapshot.recorded_at, snapshot.views
            from readable.video_snapshots as snapshot
            where snapshot.video_id = scoped.scoped_video_id
                and snapshot.recorded_at <= window_start.at
                and snapshot.recorded_at >= coalesce(%(recorded_from)s::timestamptz, '-infinity'::timestamptz)
                and snapshot.views is not null and snapshot.views >= 0
                and (%(video_type)s::text <> 'stream' or snapshot.recorded_at >= scoped.stream_actual_end_at)
            order by snapshot.recorded_at desc, snapshot.id desc limit 1
        ) as full_point on true
        left join lateral (
            select snapshot.recorded_at, snapshot.views
            from readable.video_snapshots as snapshot
            where snapshot.video_id = scoped.scoped_video_id and full_point.recorded_at is null
                and snapshot.recorded_at > window_start.at
                and snapshot.recorded_at >= coalesce(%(recorded_from)s::timestamptz, '-infinity'::timestamptz)
                and snapshot.recorded_at <= endpoint.recorded_at
                and snapshot.views is not null and snapshot.views >= 0
                and (%(video_type)s::text <> 'stream' or snapshot.recorded_at >= scoped.stream_actual_end_at)
            order by snapshot.recorded_at asc, snapshot.id desc limit 1
        ) as first_point on true
    ), measured as (
        select observations.*, endpoint_at - start_at as measured_interval
        from observations
    ), classified as (
        select measured.*,
            case
                when scoped_video_id is null or not current_fresh
                    or current_views is null or current_views < 0 or endpoint_at is null
                    then 'unavailable'
                when start_at <= window_start_at then 'ready'
                when measured_interval >= interval '24 hours' then 'partial'
                when timezone('UTC', start_at)::date = timezone('UTC', (SELECT read_at FROM bounds))::date
                    then 'started_today'
                else 'waiting_next_snapshot'
            end as resolved_status
        from measured
    )
    select classified.requested_video_id AS video_id, classified.resolved_status AS growth_status,
        case when resolved_status in ('ready', 'partial')
            then current_views - start_views else null end AS delta_views,
        case when resolved_status = 'partial'
            then floor(extract(epoch from measured_interval) / 86400.0)::integer else null end AS tracked_days,
        case when resolved_status <> 'unavailable' then start_at else null end AS observed_start_at,
        case when current_fresh then current_checked_at else null end AS observed_end_at,
        case when current_fresh then current_views else null end AS observed_current_views,
        case when resolved_status <> 'unavailable'
            then floor(extract(epoch from measured_interval))::bigint else null end AS actual_interval_seconds,
        case when resolved_status = 'ready'
            then floor(extract(epoch from window_start_at - start_at))::bigint else null end AS boundary_distance_seconds,
        case
            when resolved_status = 'ready' then null
            when resolved_status = 'partial' then 'full_window_history_unavailable'
            when resolved_status = 'started_today' then 'tracking_started_today'
            when resolved_status = 'waiting_next_snapshot' then 'waiting_for_next_snapshot'
            when scoped_video_id is null then 'video_not_in_scope'
            when not current_fresh then 'current_data_stale'
            when current_views is null or current_views < 0 then 'current_counter_unavailable'
            when endpoint_at is null then 'current_snapshot_missing'
            else 'growth_unavailable'
        end AS reason,
        least(membership_source_expires_at,current_source_expires_at,
              start_at+interval '696 hours') AS source_expires_at
    from classified order by classified.requested_ordinality
"""

SAVED_SOURCES_SQL = """
WITH read_clock AS MATERIALIZED (SELECT clock_timestamp() AS read_at),
bounds AS MATERIALIZED (SELECT read_at, least(%(as_of)s::timestamptz, read_at) AS read_through FROM read_clock),
source as materialized (
        select requested.item, requested.position, inventory.channel_id as source_channel,
            current_row.title AS title, current_row.published_at AS published_at, current_row.checked_at AS checked_at, current_row.duration AS duration, current_row.metadata_checked_at AS metadata_checked_at, current_row.availability_checked_at AS availability_checked_at, current_row.views AS views, current_row.like_count AS like_count, current_row.comment_count AS comment_count, current_row.category_id AS category_id, proof.checked_at as membership_checked_at,
            least(inventory.source_expires_at,resolution.source_expires_at,
                  proof.source_expires_at,current_row.source_expires_at) AS source_expires_at,
            case
                when inventory.video_id is null then 'inventory_missing'
                when channel.channel_id is null or channel.is_hidden is true then 'channel_unavailable'
                when inventory.source_expires_at is null or resolution.source_expires_at is null then 'source_stale'
                when inventory.video_type is not null and inventory.video_type <> 'long' then 'type_unsupported'
                when resolution.resolution_status = 'conflict' then 'type_conflict'
                when resolution.resolution_status is distinct from 'resolved' then 'type_unverified'
                when not coalesce(inventory.video_type = 'long'
                    and resolution.canonical_channel_id = inventory.channel_id
                    and resolution.canonical_video_type = 'long'
                    and (resolution.manual_valid or resolution.automatic_valid or (
                        resolution.present_membership_types = array['long']::text[]
                        and resolution.present_membership_channels = array[inventory.channel_id]::text[])), false)
                    then 'type_unverified'
                when current_row.video_id is null then 'source_missing'
                when current_row.channel_id is distinct from inventory.channel_id then 'source_mismatch'
                when not coalesce(pg_catalog.isfinite(current_row.checked_at) and pg_catalog.isfinite(current_row.metadata_checked_at)
                    and pg_catalog.isfinite(current_row.availability_checked_at)
                    and current_row.checked_at between (SELECT read_at FROM bounds) - interval '696 hours' and (SELECT read_through FROM bounds)
                    and current_row.metadata_checked_at between (SELECT read_at FROM bounds) - interval '696 hours' and (SELECT read_through FROM bounds)
                    and current_row.availability_checked_at between (SELECT read_at FROM bounds) - interval '696 hours' and (SELECT read_through FROM bounds)
                    and proof.checked_at is not null, false)
                    then 'source_stale'
                when lower(btrim(coalesce(current_row.availability_status, ''))) <> 'public' then 'source_unavailable'
                when current_row.published_at > (SELECT read_through FROM bounds) then 'source_unpublished'
                else null
            end as reason
        from unnest(%(video_ids)s::text[]) with ordinality as requested(item, position)
        left join readable.video_inventory inventory on inventory.video_id = requested.item
        left join lateral (select r.* from readable.video_inventory_type_resolution r
            where r.video_id = requested.item offset 0) resolution on true
        left join lateral (
            select max(membership.last_present_at) as checked_at,min(membership.source_expires_at) AS source_expires_at
            from readable.video_inventory_memberships membership
            where membership.video_id = requested.item
              and membership.channel_id = inventory.channel_id
              and membership.membership_status = 'present'
              and (resolution.manual_valid or membership.video_type = 'long')
              and pg_catalog.isfinite(membership.last_present_at)
              and membership.last_present_at between (SELECT read_at FROM bounds) - interval '696 hours' and (SELECT read_through FROM bounds)
        ) proof on true
        left join readable.channels_with_control channel on channel.channel_id = inventory.channel_id
        left join readable.videos current_row on current_row.video_id = requested.item
    )
    select source.item AS video_id,
        case when reason is null then source_channel else null end AS channel_id,
        case when reason is null then 'available' else 'unavailable' end AS source_status,
        reason AS source_reason,
        case when reason is null then jsonb_build_object(
            'video_id', source.item, 'title', source.title,
            'thumbnail_url', 'https://img.youtube.com/vi/' || source.item || '/mqdefault.jpg', 'published_at', source.published_at,
            'checked_at', source.checked_at, 'duration', source.duration,
            'metadata_checked_at', source.metadata_checked_at,
            'availability_checked_at', source.availability_checked_at,
            'membership_checked_at', membership_checked_at,
            'source_expires_at', source.source_expires_at,
            'views', source.views, 'like_count', source.like_count,
            'comment_count', source.comment_count, 'category_id', source.category_id
        ) else null end AS video_row,source.source_expires_at
    from source order by source.position
"""

