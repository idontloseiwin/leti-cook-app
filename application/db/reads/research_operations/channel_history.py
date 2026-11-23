"""SQL of research_query.operation_kinds.channel_history: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""



def compile_sql(plan, sql, params):
    params.update(plan.parameters)
    # The metric comes from the closed validator, never from user SQL.
    return sql + SQL.replace('{metric}', {'views': 'views', 'subscribers': 'subscribers'}[params['metric']]), params


# Its own final SELECT: the earliest point expiry also bounds the result's lifetime.
SQL = """,
history_points AS MATERIALIZED (
 SELECT s.recorded_at,s.{metric}::text AS value,s.source_expires_at,
 floor(extract(epoch FROM s.recorded_at)/(%(bucket_hours)s::integer*3600)) AS bucket
 FROM readable.channel_snapshots s JOIN population p USING(channel_id) CROSS JOIN args a
 WHERE isfinite(s.recorded_at) AND s.recorded_at<=a.evaluated_at
 AND s.recorded_at>=a.evaluated_at-make_interval(hours=>%(hours)s::integer)
 AND s.source_expires_at>a.evaluated_at AND s.{metric}>=0),
history_last AS (SELECT DISTINCT ON(bucket) * FROM history_points ORDER BY bucket,recorded_at DESC)
SELECT scope_info.*,coverage.*,a.evaluated_at,
 0::bigint AS rejected_rows,0::bigint AS unresolved_collection_members,0::bigint AS hidden_collection_members,
 least(coverage.valid_before,(SELECT min(source_expires_at) FROM history_points)) AS operation_valid_before,
 jsonb_build_object('metric',%(metric)s::text,'definition_epoch',NULL,
 'method','last_recorded_channel_observation_per_bucket_v1',
 'hours',%(hours)s::integer,'bucket_hours',%(bucket_hours)s::integer,
 'observations',(SELECT count(*) FROM history_points),
 'points',coalesce((SELECT jsonb_agg(jsonb_build_object('recorded_at',recorded_at,'value',value,
 'expires_at',source_expires_at) ORDER BY recorded_at) FROM history_last),'[]'::jsonb)) AS result
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""
