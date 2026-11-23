"""SQL of research_query.operation_kinds.publication_years: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .common import data_final_select
from research_query.operation_kinds import overview


def compile_sql(plan, sql, params):
    overview.time_zone_parameter(plan, params)
    params['year'] = plan.parameters.get('year')
    return sql + SQL, params


SQL = """,
year_times AS MATERIALIZED (
 SELECT published_date AS date,published_year::integer AS year FROM population WHERE published_year IS NOT NULL),
year_now AS (SELECT extract(year FROM a.evaluated_at AT TIME ZONE %(time_zone)s::text)::integer AS year FROM args a),
year_selected AS (SELECT coalesce(%(year)s::integer,(SELECT year FROM year_now)) AS year),
year_list AS (SELECT DISTINCT year FROM year_times UNION SELECT year FROM year_now),
year_days AS (SELECT date,count(*) AS count FROM year_times
 WHERE year=(SELECT year FROM year_selected) GROUP BY date)
""" + data_final_select("""jsonb_build_object(
 'time_zone',%(time_zone)s::text,'population',(SELECT count(*) FROM population),'known_count',(SELECT count(*) FROM year_times),
 'default_year',(SELECT year FROM year_now),'year',(SELECT year FROM year_selected),
 'available_years',(SELECT jsonb_agg(year ORDER BY year DESC) FROM year_list),
 'days',coalesce((SELECT jsonb_agg(jsonb_build_object('date',to_char(date,'YYYY-MM-DD'),'count',count) ORDER BY date) FROM year_days),'[]'::jsonb))""",
    # The default year changes at the next local new year.
    "(SELECT (date_trunc('year',a.evaluated_at AT TIME ZONE %(time_zone)s::text)+interval '1 year') AT TIME ZONE %(time_zone)s::text FROM args a)")
