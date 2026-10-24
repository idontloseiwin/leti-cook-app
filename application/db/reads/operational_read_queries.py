"""Bounded operational reads; PostgreSQL still owns set aggregation.

These six former entry-point function bodies now execute as ordinary read-only
queries. The first three readers use fixed native fact projections and the
shared Python policy tables; native aggregation, arithmetic and bounds remain.
Other callers still depend on retained policy views/functions; this is not
permission to remove those schema objects.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from ..base.postgres import DatabaseError
from ..base.repository import DatabaseRepository
from ..channel.channel_read_policy import LONG_ESTIMATE_COUNT_SQL

COVERAGE_MAX_ROWS = 10_000
QUOTA_MAX_ROWS = 100_000
# F10 removal gate: direct SQL-function dependencies of these migrated readers.
# Their other view/job callers must migrate before the retained helpers can drop.
RETAINED_SQL_DEPENDENCIES = {}


def _json(database, operation, statement, params=()):
    row = database.read_row(operation, statement, params)
    if row is None:
        return None
    if not isinstance(row, Mapping) or set(row) != {"value"}:
        raise DatabaseError("database_result_shape_invalid")
    return row["value"]


def read_youtube_quota_day(database, params):
    """Keep omitted SQL now() distinct from an invalid explicit NULL clock.

    The repository remains the owner of its public limits, finite float checks,
    labels and output validation. These checks preserve retained SQL argument
    rejection for direct calls; no user input is embedded in an error.
    """
    allowed = {"p_project_id", "p_quota_day", "p_now", "p_stale_attempt_minutes", "p_max_rows"}
    if (not isinstance(params, Mapping) or not {"p_project_id", "p_quota_day"} <= set(params)
            or not set(params) <= allowed):
        raise ValueError("database_function_parameters_invalid")
    values = {"p_stale_attempt_minutes": 10, "p_max_rows": QUOTA_MAX_ROWS, **params}
    maximum, stale, project = values["p_max_rows"], values["p_stale_attempt_minutes"], values["p_project_id"]
    invalid = (project is None or isinstance(project, str) and not project.strip(" ")
               or values["p_quota_day"] is None or "p_now" in values and values["p_now"] is None
               or maximum is None or stale is None)
    if type(maximum) is not int:
        if maximum is not None:
            raise TypeError("operational_quota_limit_type_invalid")
    else:
        invalid = invalid or not 1 <= maximum <= QUOTA_MAX_ROWS
    if stale is not None:
        try:
            number = Decimal(str(stale))
        except (InvalidOperation, ValueError):
            raise TypeError("operational_quota_threshold_type_invalid") from None
        invalid = invalid or number.is_nan() or number < 0
    if invalid:
        raise DatabaseError("youtube_quota_summary_invalid_input", code="P0001")
    values["use_default_now"] = "p_now" not in values
    values.setdefault("p_now", None)
    return _json(database, "operational_quota_day", QUOTA_DAY_SQL, values)


def get_channel_coverage_summary(database):
    rows = DatabaseRepository(database).admit_source_deadlines(database.read_rows(
        "operational_channel_coverage", CHANNEL_COVERAGE_SQL, max_rows=COVERAGE_MAX_ROWS))
    return rows


def get_channel_discovery_queue_health(database):
    return database.read_rows("operational_channel_discovery_health", CHANNEL_DISCOVERY_SQL, max_rows=1)


def get_channel_sync_control_health(database):
    return _json(database, "operational_channel_sync_health", CHANNEL_SYNC_SQL)


def get_video_collection_health(database):
    return database.read_rows("operational_video_collection_health", VIDEO_COLLECTION_SQL, max_rows=1)


def get_typed_video_collection_health(database):
    return _json(database, "operational_typed_video_collection_health", TYPED_VIDEO_COLLECTION_SQL)

QUOTA_DAY_SQL = """
with sampled as materialized (
        select quota_bucket,quota_units,phase,endpoint,status,http_status,error_type,
            duration_ms,started_at,request_attempt_id
        from readable.quota_attempts
        where project_id=%(p_project_id)s::text and quota_day=%(p_quota_day)s::date
        order by started_at,request_attempt_id limit %(p_max_rows)s::integer+1
    ), bounded as materialized (
        select * from sampled order by started_at,request_attempt_id limit %(p_max_rows)s::integer
    ), dimensions as (
        select d.name,d.key,count(*) as attempts,sum(b.quota_units) as units
        from bounded b cross join lateral (values
            ('bucket',b.quota_bucket),('phase',b.phase),('endpoint',b.endpoint),
            ('status',b.status),('http_status',b.http_status::text),('error_type',b.error_type)
        ) d(name,key) where d.key is not null group by d.name,d.key
    ), maps as (
        select name,jsonb_object_agg(key,attempts) as counts,
            jsonb_object_agg(key,units) as units from dimensions group by name
    ), totals as (
        select count(*) as attempts,coalesce(sum(quota_units),0) as units,
            count(duration_ms) as duration_count,min(duration_ms) as duration_min,
            avg(duration_ms) as duration_avg,
            percentile_cont(0.5) within group(order by duration_ms) as duration_median,
            max(duration_ms) as duration_max,
            count(*) filter(where status='started') as unfinished_count,
            count(*) filter(where status='started' and
                round(greatest(extract(epoch from ((CASE WHEN %(use_default_now)s::boolean THEN now() ELSE %(p_now)s::timestamptz END)-started_at)),0)/60,3)
                    >=%(p_stale_attempt_minutes)s::numeric) as stale_count,
            max(greatest(extract(epoch from ((CASE WHEN %(use_default_now)s::boolean THEN now() ELSE %(p_now)s::timestamptz END)-started_at)),0)/60)
                filter(where status='started') as oldest_age_minutes
        from bounded
    )
    select jsonb_build_object(
        'schema_version',1,'attempts',t.attempts,'quota_units',t.units,
        'truncated',(select count(*)>%(p_max_rows)s::integer from sampled),
        'bucket_attempt_counts',coalesce((select counts from maps where name='bucket'),'{}'::jsonb),
        'bucket_units',coalesce((select units from maps where name='bucket'),'{}'::jsonb),
        'phase_attempt_counts',coalesce((select counts from maps where name='phase'),'{}'::jsonb),
        'phase_units',coalesce((select units from maps where name='phase'),'{}'::jsonb),
        'endpoint_attempt_counts',coalesce((select counts from maps where name='endpoint'),'{}'::jsonb),
        'status_counts',coalesce((select counts from maps where name='status'),'{}'::jsonb),
        'http_status_counts',coalesce((select counts from maps where name='http_status'),'{}'::jsonb),
        'error_type_counts',coalesce((select counts from maps where name='error_type'),'{}'::jsonb),
        'duration_ms',jsonb_build_object('count',t.duration_count,'min',t.duration_min,
            'avg',t.duration_avg,'median',t.duration_median,'max',t.duration_max),
        'unfinished',jsonb_build_object('count',t.unfinished_count,'stale_count',t.stale_count,
            'oldest_age_minutes',t.oldest_age_minutes,'stale_after_minutes',%(p_stale_attempt_minutes)s::numeric)
    ) AS value from totals t
"""

CHANNEL_COVERAGE_SQL = """
with inventory_counts as (
        select
            vi.channel_id,
            count(*)::bigint as inventory_found,min(vi.source_expires_at) AS source_expires_at
        from readable.video_inventory vi
        where vi.video_type = 'long'
        group by vi.channel_id
    ),
    video_counts as (
        select
            vi.channel_id,
            count(v.video_id)::bigint as videos_in_db,min(least(vi.source_expires_at,v.source_expires_at)) AS source_expires_at
        from readable.video_inventory vi
        join readable.videos v on v.video_id = vi.video_id
        where vi.video_type = 'long'
        group by vi.channel_id
    ),
    base as (
        select
            c.channel_id::text as channel_id,least(c.source_expires_at,ic.source_expires_at,vc.source_expires_at) AS source_expires_at,
            c.title::text as title,
            c.video_count::bigint as video_count,
            """ + LONG_ESTIMATE_COUNT_SQL + """::bigint as long_video_count,
            case
                when c.initial_long_inventory_status = 'skipped_no_long_playlist'
                then 0::bigint
                when c.initial_long_inventory_status = 'completed'
                    or c.deep_inventory_status = 'completed'
                then greatest(
                    coalesce(""" + LONG_ESTIMATE_COUNT_SQL + """, 0)::bigint,
                    coalesce(ic.inventory_found, 0)::bigint
                )
                else coalesce(""" + LONG_ESTIMATE_COUNT_SQL + """, c.video_count, 0)::bigint
            end as expected_long_count,
            coalesce(vc.videos_in_db, 0)::bigint as videos_in_db,
            coalesce(ic.inventory_found, 0)::bigint as inventory_found,
            coalesce(c.deep_bypass, false) as deep_bypass,
            c.deep_bypass_reason,
            coalesce(c.deep_inventory_status, 'idle') as deep_inventory_status,
            c.deep_inventory_started_at,
            c.deep_inventory_checked_at,
            c.deep_inventory_error,
            false as has_deep_inventory_page_token,
            c.initial_long_inventory_status,
            c.initial_28d_stats_status
        from readable.channels c
        left join readable.channel_control visibility USING(channel_id)
        left join inventory_counts ic on ic.channel_id = c.channel_id
        left join video_counts vc on vc.channel_id = c.channel_id
        where coalesce(visibility.is_hidden, true) = false
    )
    select
        base.channel_id,base.source_expires_at,
        base.title,
        base.video_count,
        base.long_video_count,
        base.expected_long_count,
        base.videos_in_db,
        greatest(base.expected_long_count - base.videos_in_db, 0)::bigint as missing_estimate,
        case
            when base.expected_long_count > 0
                then round((base.videos_in_db::numeric / base.expected_long_count::numeric) * 100, 2)
            else null
        end as coverage_percent,
        base.inventory_found,
        base.deep_bypass,
        base.deep_bypass_reason,
        base.deep_inventory_status,
        base.deep_inventory_started_at,
        base.deep_inventory_checked_at,
        base.deep_inventory_error,
        base.has_deep_inventory_page_token,
        base.initial_long_inventory_status,
        base.initial_28d_stats_status
    from base
    order by base.deep_bypass asc, greatest(base.expected_long_count - base.videos_in_db, 0) desc
"""

CHANNEL_DISCOVERY_SQL = "SELECT * FROM readable.channel_discovery_health"

CHANNEL_SYNC_SQL = "SELECT * FROM readable.channel_sync_health"

VIDEO_COLLECTION_SQL = "SELECT * FROM readable.video_collection_health"

TYPED_VIDEO_COLLECTION_SQL = "SELECT * FROM readable.typed_video_collection_health"
