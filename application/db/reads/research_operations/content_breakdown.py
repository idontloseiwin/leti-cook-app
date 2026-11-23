"""SQL of research_query.operation_kinds.content_breakdown: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from . import overview as overview_sql
from .common import data_final_select
from research_query.operation_kinds import overview
from research_query.operation_kinds.content_breakdown import GROUP_LIMIT


def compile_sql(plan, sql, params):
    overview.period_parameters(plan, params)
    params.update(strip_chars=overview.STRIP_CHARS, group_limit=GROUP_LIMIT)
    return sql + overview_sql.PERIOD_CTES + SQL, params


SQL = """,
breakdown_rows AS MATERIALIZED (
 SELECT p.video_id,p.published_at,
 nullif(btrim(p.category_id,%(strip_chars)s::text),'') AS category,
 nullif(btrim(p.audio_language,%(strip_chars)s::text),'') AS audio_language,
 nullif(btrim(p.metadata_language,%(strip_chars)s::text),'') AS metadata_language,
 p.caption_available,
 CASE WHEN p.topic_urls IS NULL OR EXISTS(SELECT 1 FROM unnest(p.topic_urls) t(u) WHERE btrim(u,%(strip_chars)s::text)='')
 THEN NULL ELSE p.topic_urls END AS topics,
 CASE WHEN p.published_at IS NULL OR p.published_at>a.evaluated_at THEN NULL
 WHEN p.published_at>a.evaluated_at-make_interval(hours=>8760) THEN 'under_1y'
 WHEN p.published_at>a.evaluated_at-make_interval(hours=>26280) THEN '1_3y'
 WHEN p.published_at>a.evaluated_at-make_interval(hours=>43800) THEN '3_5y' ELSE '5y_plus' END AS age
 FROM overview_rows p CROSS JOIN args a),
breakdown_groups AS MATERIALIZED (
 SELECT section,key,count,row_number() OVER(PARTITION BY section ORDER BY count DESC,key COLLATE "C") AS rank,
 count(*) OVER(PARTITION BY section) AS group_count FROM (
 SELECT 'official_categories' AS section,category AS key,count(*) AS count FROM breakdown_rows WHERE category IS NOT NULL GROUP BY category
 UNION ALL SELECT 'audio_languages',audio_language,count(*) FROM breakdown_rows WHERE audio_language IS NOT NULL GROUP BY audio_language
 UNION ALL SELECT 'metadata_languages',metadata_language,count(*) FROM breakdown_rows WHERE metadata_language IS NOT NULL GROUP BY metadata_language
 UNION ALL SELECT 'captions',CASE WHEN caption_available THEN 'available' ELSE 'unavailable' END,count(*)
 FROM breakdown_rows WHERE caption_available IS NOT NULL GROUP BY 2
 UNION ALL SELECT 'catalogue_age',age,count(*) FROM breakdown_rows WHERE age IS NOT NULL GROUP BY age
 UNION ALL SELECT 'youtube_topics',t.key,count(DISTINCT b.video_id) FROM breakdown_rows b
 CROSS JOIN LATERAL (SELECT btrim(u,%(strip_chars)s::text) AS key FROM unnest(b.topics) x(u)) t
 WHERE b.topics IS NOT NULL GROUP BY t.key) g)
""" + data_final_select("""jsonb_build_object(
 'period',""" + overview_sql.PERIOD_JSON + """,'population',""" + overview_sql.population_json() + """,
 'sample_count',(SELECT count(*) FROM breakdown_rows),
 'known',(SELECT jsonb_build_object('official_categories',count(category),'youtube_topics',count(topics),
 'audio_languages',count(audio_language),'metadata_languages',count(metadata_language),
 'captions',count(caption_available),'catalogue_age',count(age)) FROM breakdown_rows),
 'topics_empty',(SELECT count(*) FROM breakdown_rows WHERE cardinality(topics)=0),
 'groups',(SELECT jsonb_object_agg(s.section,jsonb_build_object(
 'items',coalesce((SELECT jsonb_agg(jsonb_build_object('key',g.key,'count',g.count) ORDER BY g.rank)
 FROM breakdown_groups g WHERE g.section=s.section AND g.rank<=%(group_limit)s::integer),'[]'::jsonb),
 'group_count',coalesce((SELECT max(g.group_count) FROM breakdown_groups g WHERE g.section=s.section),0)))
 FROM (VALUES('official_categories'),('youtube_topics'),('audio_languages'),('metadata_languages'),
 ('captions'),('catalogue_age')) s(section)))""",
    # A video moves to the next catalogue age bucket on its 1st, 3rd or 5th 365-day anniversary.
    "least(" + overview_sql.PERIOD_UNTIL + ",(SELECT min(n) FROM breakdown_rows b CROSS JOIN args a CROSS JOIN LATERAL"
    " (VALUES(b.published_at+make_interval(hours=>8760)),(b.published_at+make_interval(hours=>26280)),"
    "(b.published_at+make_interval(hours=>43800))) t(n)"
    " WHERE n>a.evaluated_at))")
