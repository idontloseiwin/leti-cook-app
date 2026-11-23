"""SQL of research_query.operation_kinds.format_totals: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from research_query.operation_kinds.format_totals import CLASSES


def compile_sql(plan, sql, params):
    from db.reads.research_sql import stream_end_order_sql
    return sql + SQL.replace('{stream_end_order}', stream_end_order_sql(
        'v.stream_actual_end_at', 'v.checked_at', 'v.metadata_checked_at', 'a.evaluated_at')), params


SQL = """,
format_kinds AS (SELECT k.video_type FROM (VALUES('long'),('short'),('stream')) k(video_type)),
format_state AS MATERIALIZED (
 SELECT k.video_type,CASE coalesce(s.status,'not_checked') WHEN 'complete' THEN 'completed' WHEN 'absent' THEN 'playlist_not_found'
 WHEN 'not_checked' THEN 'not_started' ELSE 'error' END AS inventory_status,
 coalesce(CASE WHEN s.status='absent' THEN s.absent_checked_at+%(window)s::interval
 ELSE s.full_expires_at END>a.evaluated_at,false) AS current,
 CASE WHEN s.status='absent' THEN s.absent_checked_at+%(window)s::interval ELSE s.full_expires_at END AS expires_at
 FROM format_kinds k CROSS JOIN args a LEFT JOIN readable.library_format_coverage s
 ON s.channel_id=%(scope_id)s::text AND s.video_type=k.video_type AND EXISTS(SELECT 1 FROM population)),
format_rows AS MATERIALIZED (
 SELECT l.video_type,l.video_id,CASE WHEN l.source_expires_at>a.evaluated_at THEN l.source_expires_at END AS list_expiry,
 CASE WHEN NOT coalesce(isfinite(l.source_checked_at)
 AND l.source_checked_at BETWEEN a.evaluated_at-%(window)s::interval AND a.evaluated_at,false) THEN 'stale_inventory'
 WHEN v.video_id IS NULL OR v.channel_id IS DISTINCT FROM l.channel_id THEN 'missing_video_row'
 WHEN NOT coalesce(isfinite(v.availability_checked_at)
 AND v.availability_checked_at BETWEEN a.evaluated_at-%(window)s::interval AND a.evaluated_at,false) THEN 'availability_withheld'
 WHEN v.availability_status IS DISTINCT FROM 'public' THEN 'unavailable_excluded'
 WHEN l.video_type='stream' AND NOT coalesce({stream_end_order},false) THEN 'stream_completion_withheld'
 WHEN NOT coalesce(isfinite(v.checked_at)
 AND v.checked_at BETWEEN a.evaluated_at-%(window)s::interval AND a.evaluated_at,false) THEN 'stale_counter'
 WHEN l.video_type='stream' AND NOT coalesce(isfinite(v.metadata_checked_at)
 AND v.metadata_checked_at BETWEEN a.evaluated_at-%(window)s::interval AND a.evaluated_at,false) THEN 'stale_metadata'
 WHEN v.views IS NULL OR v.views<0 THEN 'unknown_view' ELSE 'known' END AS class,
 v.views,least(v.source_expires_at,v.checked_at+%(window)s::interval,v.availability_checked_at+%(window)s::interval,
 CASE WHEN l.video_type='stream' THEN v.metadata_checked_at+%(window)s::interval END) AS row_expiry
 FROM readable.hot_video_inventory l CROSS JOIN args a
 LEFT JOIN LATERAL (SELECT x.video_id,x.channel_id,x.availability_status,x.availability_checked_at,x.checked_at,
 x.metadata_checked_at,x.views,x.stream_actual_end_at,x.source_expires_at
 FROM readable.videos x WHERE x.video_id=l.video_id LIMIT 1) v ON true
 WHERE l.channel_id=%(scope_id)s::text AND EXISTS(SELECT 1 FROM population)),
format_counts AS MATERIALIZED (
 SELECT k.video_type,count(r.video_id) AS scanned,
 coalesce((SELECT c.canonical_count FROM readable.published_inventory_counts c
 WHERE c.channel_id=%(scope_id)s::text AND c.video_type=k.video_type),0) AS canonical,
 jsonb_build_object(""" + ','.join(f"'{name}',count(r.video_id) FILTER(WHERE r.class='{name}')" for name in CLASSES) + """) AS classes,
 sum(r.views) FILTER(WHERE r.class='known')::text AS views,
 least(min(r.list_expiry) FILTER(WHERE r.class<>'stale_inventory'),min(r.row_expiry) FILTER(WHERE r.class='known'),
 (SELECT s.expires_at FROM format_state s WHERE s.video_type=k.video_type)) AS expires_at
 FROM format_kinds k LEFT JOIN format_rows r ON r.video_type=k.video_type GROUP BY k.video_type)
SELECT scope_info.*,coverage.channels_complete,coverage.required_missing,0::bigint AS rejected_rows,
 0::bigint AS unresolved_collection_members,0::bigint AS hidden_collection_members,a.evaluated_at,
 coalesce((SELECT min(c.expires_at) FROM format_counts c),coverage.valid_before) AS valid_before,
 (SELECT jsonb_object_agg(c.video_type,jsonb_build_object(
 'state',(SELECT jsonb_build_object('inventory_status',s.inventory_status,'current',s.current,'expires_at',s.expires_at)
 FROM format_state s WHERE s.video_type=c.video_type),
 'counts',jsonb_build_object('scanned',c.scanned,'canonical',c.canonical,'classes',c.classes,'views',c.views,'expires_at',c.expires_at)))
 FROM format_counts c) AS result
FROM scope_info CROSS JOIN coverage CROSS JOIN args a"""
