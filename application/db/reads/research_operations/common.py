"""Shared final SELECT texts of the research operation statements (research_query.operation_kinds.kind).

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""



# The last part of a population statement: scope/coverage evidence and one JSON result.
FINAL_SELECT = """
SELECT scope_info.*,coverage.*,a.evaluated_at,{result} AS result
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""


# The same, for a video population summary that lives as long as the data clocks
# of its inputs instead of the 120 seconds of a current page; an empty
# scope without any data clock keeps the page bound. {until} is the first
# instant at which the result would change by time alone (a rolling period, an
# age bucket or the current year); NULL when none.
DATA_FINAL_SELECT = """
SELECT scope_info.*,coverage.channels_complete,coverage.rejected_rows,coverage.required_missing,
 coverage.unresolved_collection_members,coverage.hidden_collection_members,
 least(coalesce(coverage.data_valid_before,coverage.valid_before),{until}) AS valid_before,a.evaluated_at,{result} AS result
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""


def data_final_select(result, until='NULL::timestamptz'):
    return DATA_FINAL_SELECT.replace('{until}', until).replace('{result}', result)
