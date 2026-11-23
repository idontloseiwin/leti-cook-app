"""SQL of research_query.operation_kinds.calendar: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .common import FINAL_SELECT


def compile_sql(plan, sql, params):
    from db.reads.research_sql import local_publication
    params['year'] = plan.parameters['year']
    params['time_zone'] = plan.parameters.get('time_zone', plan.query.request.time.timezone)
    local, instant, in_range = local_publication('published_at', '%(time_zone)s::text')
    return sql + SQL.replace('{local}', local).replace('{known}', f'{instant} AND {in_range}'), params


SQL = """,
publication_times AS MATERIALIZED (
 SELECT {local} AS local_time FROM population WHERE published_at IS NOT NULL AND {known}),
daily AS (SELECT local_time::date AS date,count(*) AS count FROM publication_times
 WHERE extract(year FROM local_time)=%(year)s GROUP BY local_time::date),
hours AS (SELECT extract(hour FROM local_time)::integer AS hour,count(*) AS count FROM publication_times GROUP BY 1),
weekdays AS (SELECT extract(isodow FROM local_time)::integer AS weekday,count(*) AS count FROM publication_times GROUP BY 1)
""" + FINAL_SELECT.replace('{result}', """jsonb_build_object(
 'population',(SELECT count(*) FROM population),'known_count',(SELECT count(*) FROM publication_times),
 'year',%(year)s::integer,'time_zone',%(time_zone)s::text,
 'days',coalesce((SELECT jsonb_agg(jsonb_build_object('date',to_char(date,'YYYY-MM-DD'),'count',count) ORDER BY date) FROM daily),'[]'::jsonb),
 'hours',coalesce((SELECT jsonb_agg(to_jsonb(hours) ORDER BY hour) FROM hours),'[]'::jsonb),
 'weekdays',coalesce((SELECT jsonb_agg(to_jsonb(weekdays) ORDER BY weekday) FROM weekdays),'[]'::jsonb))""")
