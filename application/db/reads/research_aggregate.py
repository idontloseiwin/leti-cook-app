"""Fixed PostgreSQL operations over the common authorized query population.

Each registered operation kind (research_query.operation_kinds) appends its own
fixed CTEs and final SELECT, written in db/reads/research_operations, to the
shared population statement.
"""
from research_query.validation import QueryRejected
from .research_operations import COMPILERS
from .research_sql import compile_query


def compile_operation(plan):
    sql, params = compile_query(plan.query, population=True)
    compile_sql = COMPILERS.get(plan.operation)
    if compile_sql is None:
        raise QueryRejected('invalid_query')
    return compile_sql(plan, sql, params)
