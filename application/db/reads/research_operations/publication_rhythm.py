"""SQL of research_query.operation_kinds.publication_rhythm: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from . import overview as overview_sql
from .common import data_final_select
from research_query.operation_kinds import overview


def compile_sql(plan, sql, params):
    overview.period_parameters(plan, params)
    overview.time_zone_parameter(plan, params)
    return sql + overview_sql.PERIOD_CTES + SQL, params


SQL = """,
rhythm_times AS MATERIALIZED (
 SELECT published_at,published_at AT TIME ZONE %(time_zone)s::text AS local_time FROM overview_rows WHERE published_at IS NOT NULL),
rhythm_cells AS MATERIALIZED (
 SELECT extract(isodow FROM local_time)::integer AS weekday,extract(hour FROM local_time)::integer AS hour,count(*) AS count
 FROM rhythm_times GROUP BY 1,2)
""" + data_final_select("""jsonb_build_object(
 'period',""" + overview_sql.PERIOD_JSON + """,'population',""" + overview_sql.population_json() + """,
 'time_zone',%(time_zone)s::text,'known_count',(SELECT count(*) FROM rhythm_times),
 'first_published_at',(SELECT min(published_at) FROM rhythm_times),'last_published_at',(SELECT max(published_at) FROM rhythm_times),
 'cells',coalesce((SELECT jsonb_agg(jsonb_build_object('weekday',weekday,'hour',hour,'count',count) ORDER BY weekday,hour) FROM rhythm_cells),'[]'::jsonb),
 'hours',coalesce((SELECT jsonb_agg(jsonb_build_object('hour',hour,'count',n) ORDER BY hour)
 FROM (SELECT hour,sum(count)::bigint AS n FROM rhythm_cells GROUP BY hour) h),'[]'::jsonb),
 'weekdays',coalesce((SELECT jsonb_agg(jsonb_build_object('weekday',weekday,'count',n) ORDER BY weekday)
 FROM (SELECT weekday,sum(count)::bigint AS n FROM rhythm_cells GROUP BY weekday) d),'[]'::jsonb))""", overview_sql.PERIOD_UNTIL)
