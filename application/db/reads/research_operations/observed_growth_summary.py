"""SQL of research_query.operation_kinds.observed_growth_summary: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .common import FINAL_SELECT


def compile_sql(plan, sql, params):
    return sql + SQL, params


SQL = """,
growth_counts AS MATERIALIZED (
 SELECT observation_growth_28d_status AS status,observation_growth_28d_reason AS reason,count(*) AS n
 FROM population GROUP BY observation_growth_28d_status,observation_growth_28d_reason)
""" + FINAL_SELECT.replace('{result}', """jsonb_build_object(
 'population',(SELECT count(*) FROM population),
 'statuses',coalesce((SELECT jsonb_object_agg(status,total) FROM
 (SELECT status,sum(n)::bigint AS total FROM growth_counts GROUP BY status) s),'{}'::jsonb),
 'reasons',coalesce((SELECT jsonb_object_agg(reason,total) FROM
 (SELECT reason,sum(n)::bigint AS total FROM growth_counts WHERE reason IS NOT NULL GROUP BY reason) r),'{}'::jsonb),
 'time_zone',%(growth_time_zone)s::text)""")
