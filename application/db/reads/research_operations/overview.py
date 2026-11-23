"""SQL of research_query.operation_kinds.overview: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""



# Appended after the shared population statement; text starts with ','.
PERIOD_CTES = """,
overview_args AS MATERIALIZED (
 SELECT a.evaluated_at,CASE WHEN %(period_hours)s::integer IS NULL THEN NULL
 ELSE a.evaluated_at-make_interval(hours=>%(period_hours)s::integer) END AS period_start
 FROM args a),
overview_rows AS MATERIALIZED (
 SELECT p.* FROM population p CROSS JOIN overview_args o
 WHERE o.period_start IS NULL OR (p.published_at>=o.period_start AND p.published_at<=o.evaluated_at))"""


# When the oldest video of a rolling period leaves it: the result changes then.
PERIOD_UNTIL = """CASE WHEN %(period_hours)s::integer IS NULL THEN NULL
 ELSE (SELECT min(published_at) FROM overview_rows)+make_interval(hours=>%(period_hours)s::integer)+interval '1 microsecond' END"""


PERIOD_JSON = """jsonb_build_object('key',%(period_key)s::text,'label',%(period_label)s::text,
 'kind',CASE WHEN %(period_hours)s::integer IS NULL THEN 'max' ELSE 'rolling' END,'days',%(period_days)s::integer,
 'start_at',(SELECT period_start FROM overview_args),'end_at',(SELECT evaluated_at FROM overview_args))"""


def population_json(extra=''):
    """Population counts; `extra` adds fixed ",'key',expression" pairs."""
    return """jsonb_build_object('admitted',(SELECT count(*) FROM population),
 'matched',(SELECT count(*) FROM overview_rows),
 'publication_unknown_excluded',CASE WHEN %(period_hours)s::integer IS NULL THEN 0
 ELSE (SELECT count(*) FROM population WHERE published_at IS NULL) END""" + extra + ')'
