"""SQL of research_query.operation_kinds.observed_history: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .inspection import TARGET_JSON
from .inspection import TARGET_SQL
from .common import FINAL_SELECT
from research_query.operation_kinds.inspection import bind_target
from research_query.operation_kinds.observed_history import _POINT


def compile_sql(plan, sql, params):
    bind_target(plan, params)
    return sql + SQL, params


_AGGREGATES = ",\n ".join(
    f"min({column}) FILTER(WHERE {column}>=0) AS {metric},count(DISTINCT {column}) FILTER(WHERE {column}>=0) AS {metric}_values,"
    f"bool_or({column} IS NULL OR {column}<0) AS {metric}_missing,coalesce(bool_or({column}<0),false) AS {metric}_invalid"
    for metric, column in (('views', 'views'), ('likes', 'like_count'), ('comments', 'comment_count')))


SQL = """,
""" + TARGET_SQL + """,
history_bounds AS MATERIALIZED (
 SELECT t.video_id,
 CASE WHEN %(inspect_frozen_counters)s::boolean THEN %(inspect_end_at)s::timestamptz ELSE t.checked_at END AS end_at,
 CASE WHEN t.video_type='stream' THEN greatest(i.window_from,t.actual_end_at) ELSE i.window_from END AS from_at,i.as_of
 FROM inspector_target t CROSS JOIN inspector_args i),
history_rows AS MATERIALIZED (
 SELECT s.recorded_at,s.views,s.like_count,s.comment_count,s.views_definition_epoch AS epoch
 FROM history_bounds b CROSS JOIN LATERAL (
 SELECT x.recorded_at,x.views,x.like_count,x.comment_count,x.views_definition_epoch
 FROM readable.video_snapshots x WHERE x.video_id=b.video_id
 AND x.recorded_at>=b.from_at AND x.recorded_at<=b.end_at
 ORDER BY x.recorded_at,x.id LIMIT 501) s
 WHERE b.end_at>=b.from_at AND b.end_at<=b.as_of),
history_points AS MATERIALIZED (
 SELECT recorded_at,count(*) AS rows_at,
 """ + _AGGREGATES + """,
 CASE WHEN count(DISTINCT epoch)=1 AND bool_and(epoch IS NOT NULL) THEN min(epoch) END AS epoch
 FROM history_rows GROUP BY recorded_at),
history_series AS (
 SELECT h.*,views-lag(views) OVER w AS views_delta,likes-lag(likes) OVER w AS likes_delta,
 comments-lag(comments) OVER w AS comments_delta,
 (extract(epoch FROM recorded_at-lag(recorded_at) OVER w)*1000000)::bigint AS elapsed_us
 FROM history_points h WINDOW w AS (ORDER BY recorded_at))
""" + FINAL_SELECT.replace('{result}', """jsonb_build_object(""" + TARGET_JSON + """,
 'as_of',(SELECT as_of FROM inspector_args),
 'bounds',(SELECT jsonb_build_object('from_at',from_at,'end_at',end_at) FROM history_bounds),
 'expected',(SELECT jsonb_build_object('views',views::text,'views_state',views_state,'likes',likes::text,
 'likes_state',likes_state,'comments',comments::text,'comments_state',comments_state) FROM inspector_target),
 'scanned',(SELECT count(*) FROM history_rows),
 'points',coalesce((SELECT jsonb_agg(jsonb_build_object('recorded_at',recorded_at,'rows',rows_at,'epoch',epoch,
 'elapsed_us',elapsed_us::text,""" + _POINT + """) ORDER BY recorded_at) FROM history_series),'[]'::jsonb))""")
