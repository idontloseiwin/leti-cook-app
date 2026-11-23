"""Native assignment validation for five finite operational writer shapes.

EXPLAIN without ANALYZE parses and plans the original parameterized statement.
It preserves PostgreSQL assignment coercion (which explicit SELECT casts can
widen) before capturing a proposed row. It executes no DML, row trigger, sequence
or UUID default. Plans are discarded by the driver and never returned/logged.
"""
from collections.abc import Mapping
from psycopg import sql
from .sql_values import assignments, insert_values

FAMILIES = frozenset({'sync_start', 'sync_finish_insert', 'sync_update',
                      'quota_start', 'quota_finish'})


def assignment_plan(family, values, identity):
    """Build only original INSERT/UPDATE templates; accept no SQL or table input."""
    if type(family) is not str or family not in FAMILIES or not isinstance(values, Mapping):
        raise ValueError('database_assignment_capability_invalid')
    values = dict(values)
    sync = family.startswith('sync_')
    json_columns = {'summary_json'} if sync else ()
    if family in {'sync_start', 'sync_finish_insert', 'quota_start'}:
        if identity is not None:
            raise ValueError('database_assignment_capability_invalid')
        columns, rows, parameters = insert_values([values], json_columns=json_columns)
        template = ('INSERT INTO public.sync_runs ({}) VALUES {}' if sync else
                    'INSERT INTO public.youtube_api_request_attempts ({}) VALUES {}')
        statement = sql.SQL(template).format(columns, rows)
        if family == 'sync_start':
            statement += sql.SQL(' RETURNING id')
    else:
        terms, parameters = assignments(values, json_columns=json_columns)
        template = ('UPDATE public.sync_runs SET {} WHERE id=%s' if sync else
                    "UPDATE public.youtube_api_request_attempts SET {} WHERE request_attempt_id=%s::uuid AND status='started'")
        statement = sql.SQL(template).format(terms)
        parameters += (identity,)
    return (sql.SQL('EXPLAIN (FORMAT JSON, ANALYZE FALSE, VERBOSE FALSE, COSTS FALSE) ') + statement,
            parameters)
