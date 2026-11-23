"""SQL of research_query.operation_kinds.view_pace_median: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .population_stats import view_definition
from research_query.operation_kinds.view_pace_median import METHOD


# The population statistics query: the definition of each row's views, one state for all.
SQL = """,
""" + view_definition() + """,
pace_known AS MATERIALIZED (
 SELECT p.lifetime_views_per_day AS value,d.epoch FROM population p LEFT JOIN view_definitions d USING(video_id)
 WHERE p.lifetime_views_per_day_state='known'),
pace AS MATERIALIZED (
 SELECT value FROM pace_known WHERE epoch IS NOT NULL AND (SELECT state FROM view_definition)='known'),
ranked AS MATERIALIZED (
 SELECT value,row_number() OVER (ORDER BY value) AS position,count(*) OVER () AS total FROM pace)
SELECT scope_info.*,coverage.channels_complete,coverage.rejected_rows,coverage.required_missing,
 coverage.unresolved_collection_members,coverage.hidden_collection_members,
 coalesce(coverage.data_valid_before,coverage.valid_before) AS valid_before,a.evaluated_at,
 jsonb_build_object('field','video.lifetime_views_per_day','method',%(pace_method)s::text,
 'population_count',(SELECT count(*) FROM population),'known_count',(SELECT count(*) FROM pace),
 'missing',jsonb_build_object(
  'unknown',(SELECT count(*) FROM population WHERE lifetime_views_per_day_state='unknown'),
  'expired',(SELECT count(*) FROM population WHERE lifetime_views_per_day_state='expired'),
  'unavailable',(SELECT count(*) FROM population WHERE lifetime_views_per_day_state='unavailable')),
 'definition',(SELECT jsonb_build_object('state',state,'epoch',epoch,
  'excluded_count',(SELECT count(*) FROM pace_known)-(SELECT count(*) FROM pace)) FROM view_definition),
 'median',(SELECT (sum(value)/count(*))::text FROM ranked WHERE position IN ((total+1)/2,(total+2)/2))) AS result
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""


def compile_sql(plan, sql, params):
    params['pace_method'] = METHOD
    return sql + SQL, params
