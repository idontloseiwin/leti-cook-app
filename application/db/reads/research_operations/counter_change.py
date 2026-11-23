"""SQL of research_query.operation_kinds.counter_change: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from research_query.operation_kinds.counter_change import METRICS
from research_query.operation_kinds.counter_change import TOLERANCE_HOURS
from research_query.operation_kinds.counter_change import WINDOWS


def compile_sql(plan, sql, params):
    params.update(counter_tolerance_hours=TOLERANCE_HOURS)
    return sql + SQL, params


def _start_sql(metric, hours):
    # The metric name comes from the closed METRICS tuple, never from a request.
    point = f"jsonb_build_object('recorded_at',c.recorded_at,'value',c.{metric}::text,'expires_at',c.source_expires_at)"
    return f"""jsonb_build_object('boundary',(SELECT {point} FROM counter_points c CROSS JOIN counter_latest l
 WHERE c.{metric} IS NOT NULL AND c.recorded_at<=l.recorded_at-make_interval(hours=>{hours})
 AND c.recorded_at>=l.recorded_at-make_interval(hours=>{hours}+%(counter_tolerance_hours)s::integer)
 ORDER BY c.recorded_at DESC LIMIT 1),
 'earliest',(SELECT {point} FROM counter_points c CROSS JOIN counter_latest l
 WHERE c.{metric} IS NOT NULL AND c.recorded_at>l.recorded_at-make_interval(hours=>{hours}) AND c.recorded_at<l.recorded_at
 ORDER BY c.recorded_at ASC LIMIT 1))"""


# The latest snapshot bounds the result from above; lifetime() then narrows it
# to the snapshots actually shown. Without any snapshot: the page bound.
SQL = """,
counter_points AS MATERIALIZED (
 SELECT s.recorded_at,s.subscribers,s.views,s.source_expires_at
 FROM readable.channel_snapshots s JOIN population p USING(channel_id) CROSS JOIN args a
 WHERE isfinite(s.recorded_at) AND s.recorded_at<=a.evaluated_at AND s.source_expires_at>a.evaluated_at),
counter_latest AS (SELECT * FROM counter_points ORDER BY recorded_at DESC LIMIT 1)
SELECT scope_info.*,coverage.channels_complete,coverage.required_missing,0::bigint AS rejected_rows,
 0::bigint AS unresolved_collection_members,0::bigint AS hidden_collection_members,a.evaluated_at,
 coalesce((SELECT source_expires_at FROM counter_latest),coverage.valid_before) AS valid_before,
 jsonb_build_object('evaluated_at',a.evaluated_at,
 'latest',(SELECT jsonb_build_object('recorded_at',recorded_at,'subscribers',subscribers::text,'views',views::text,
 'expires_at',source_expires_at) FROM counter_latest),
 'windows',jsonb_build_object(""" + ','.join(
    f"'{period}',jsonb_build_object(" + ','.join(f"'{metric}',{_start_sql(metric, hours)}" for metric in METRICS) + ')'
    for period, hours in WINDOWS) + """)) AS result
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""
