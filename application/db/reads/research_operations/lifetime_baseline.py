"""SQL of research_query.operation_kinds.lifetime_baseline: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .inspection import TARGET_JSON
from .inspection import TARGET_SQL
from .common import FINAL_SELECT
from .population_stats import SUMMARIES
from .population_stats import metric_values
from research_query.operation_kinds.inspection import bind_target
from .population_stats import VIEW_DEFINITION


def compile_sql(plan, sql, params):
    bind_target(plan, params)
    params['baseline_conditions'] = plan.query.request.where is not None or bool(plan.query.request.coverage.required_fields)
    return sql + SQL, params


SQL = """,
""" + VIEW_DEFINITION + """,
""" + metric_values('') + """,
""" + SUMMARIES + """,
""" + TARGET_SQL + FINAL_SELECT.replace('{result}', """jsonb_build_object(""" + TARGET_JSON + """,
 'conditions_applied',%(baseline_conditions)s::boolean,'population',(SELECT count(*) FROM population),
 'definition',(SELECT jsonb_build_object('state',state,'epoch',epoch,'unknown_epoch',unknown_epoch) FROM view_definition),
 'views',(SELECT jsonb_build_object('known_count',known_count,'median',median,'q1',q1,'q3',q3)
 FROM summaries WHERE metric='views'),
 'target',(SELECT jsonb_build_object('views',views::text,'views_state',views_state,'checked_at',checked_at)
 FROM inspector_target))""")
