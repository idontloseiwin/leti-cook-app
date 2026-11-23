"""SQL of research_query.operation_kinds.inspection: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""



# The inspection clock and the inspected video's population row. as_of is the
# frozen evaluation time when bound, else the statement's; window_from is always
# the statement's evaluation minus 28 days.
TARGET_SQL = """inspector_args AS MATERIALIZED (
 SELECT coalesce(%(inspect_as_of)s::timestamptz,a.evaluated_at) AS as_of,
 a.evaluated_at-make_interval(hours=>672) AS window_from,
 %(inspect_video_id)s::text AS video_id,%(inspect_frozen)s::jsonb AS frozen FROM args a),
inspector_target AS MATERIALIZED (
 SELECT p.* FROM population p JOIN inspector_args i USING(video_id))"""


# The inspected video's identity, membership and bound reference: first keys of every result.
TARGET_JSON = """'video_id',(SELECT video_id FROM inspector_args),'video_type',(SELECT video_type FROM args),
 'member',EXISTS(SELECT 1 FROM inspector_target),'frozen',(SELECT frozen FROM inspector_args)"""
