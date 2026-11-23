"""SQL of research_query.operation_kinds.fingerprint: the database text this operation appends to a population.

Builds the fingerprint from the same rows and period used by the Overview.
"""
from . import overview as overview_sql
from . import summary as summary_sql
from .common import data_final_select
from research_query.operation_kinds import overview


def compile_sql(plan, sql, params):
    params['buckets'] = summary_sql.bucket_parameters(plan)
    overview.period_parameters(plan, params)
    # Coverage: scanned rows, why rows were not admitted, and how many
    # admitted videos carry stale values (kept as unknown).
    population = overview_sql.population_json(""",
 'scanned',(SELECT count(*) FROM raw),
 'stale_counter_count',(SELECT count(*) FROM population WHERE views_state='expired'),
 'stale_metadata_count',(SELECT count(*) FROM population WHERE title_state='expired'),
 'stale_count',(SELECT count(*) FROM population WHERE views_state='expired' OR title_state='expired'),
 'rejected',coalesce((SELECT jsonb_object_agg(reason,n) FROM (SELECT rejection AS reason,count(*) AS n
 FROM raw WHERE NOT admitted GROUP BY 1) rejected),'{}'::jsonb)""")
    statement = overview_sql.PERIOD_CTES + ',' + summary_sql.summary_ctes('overview_rows') + data_final_select("""jsonb_build_object(
 'period',""" + overview_sql.PERIOD_JSON + """,
 'population',""" + population + """,
 'uploads',jsonb_build_object('known_count',(SELECT count(*) FROM overview_rows WHERE published_at IS NOT NULL),
 'unknown_count',(SELECT count(*) FROM overview_rows WHERE published_at IS NULL)),
 'metrics',""" + summary_sql.metrics_json('overview_rows', separate_excluded=True) + ')', overview_sql.PERIOD_UNTIL)
    return sql + statement, params
