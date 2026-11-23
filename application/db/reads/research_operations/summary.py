"""SQL of research_query.operation_kinds.summary: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .common import FINAL_SELECT
from .population_stats import SUMMARIES
from .population_stats import metric_values
from .population_stats import view_definition
from research_query.operation_kinds.summary import METRICS


def bucket_parameters(plan):
    """Fixed bucket edges of every metric; the one place that reads the preset registry."""
    # SQL-only imports load at compile time; validation stays import-light.
    from psycopg.types.json import Jsonb
    from research_query.registry import preset_buckets
    # The shared bucket registry: Shorts and Streams have their own duration sets.
    video_type = plan.query.request.video_types[0]
    return Jsonb([
        dict(metric=metric, key=bucket.key, label=bucket.label, lower=bucket.min, upper=bucket.max, ordinal=i)
        for metric in METRICS
        for i, bucket in enumerate(preset_buckets('duration' if metric == 'duration_seconds' else metric, video_type))])


def compile_sql(plan, sql, params):
    params['buckets'] = bucket_parameters(plan)
    return sql + SQL, params


def summary_ctes(rows):
    """Statistics CTEs over the relation `rows` (one row per video, population columns)."""
    return view_definition(rows) + ',\n' + metric_values("""('duration_seconds',p.duration_seconds),('likes',p.likes),
 ('comments',p.comments),('title_length',char_length(p.title)::numeric),
 ('tag_count',cardinality(p.tags)::numeric)""", rows) + ',\n' + SUMMARIES + """,
buckets AS (
 SELECT b.metric,b.key,b.label,b.ordinal,
 (SELECT count(*) FROM metric_values v WHERE v.metric=b.metric AND
 (b.lower IS NULL OR v.value>=b.lower) AND (b.upper IS NULL OR v.value<b.upper)) AS count
 FROM jsonb_to_recordset(%(buckets)s::jsonb) b(metric text,key text,label text,lower numeric,upper numeric,ordinal integer))"""


# Known values of views left out by the definition rule (zero for other metrics).
_EXCLUDED = ("CASE WHEN m.metric<>'views' THEN 0 WHEN (SELECT state FROM view_definition)='known' "
             "THEN (SELECT unknown_epoch FROM view_definition) ELSE (SELECT reported FROM view_definition) END")


def metrics_json(rows, *, separate_excluded):
    """The metrics object over `rows`. With separate_excluded a views value left
    out by the definition rule is counted as excluded_count, otherwise inside
    missing_count (video.summary.v1 keeps known+missing equal to its population)."""
    missing = "(SELECT count(*) FROM {rows})-coalesce(s.known_count,0)" + ("-" + _EXCLUDED if separate_excluded else "")
    excluded = ",'excluded_count'," + _EXCLUDED if separate_excluded else ""
    return """(SELECT jsonb_object_agg(m.metric,jsonb_build_object(
 'known_count',coalesce(s.known_count,0),'missing_count',{missing}{excluded},
 'definition_epoch',CASE WHEN m.metric='views' THEN (SELECT epoch FROM view_definition) END,
 'state',CASE WHEN m.metric='views' THEN (SELECT state FROM view_definition) WHEN coalesce(s.known_count,0)=0 THEN 'no_known_values' ELSE 'known' END,
 'total',s.total,'mean',s.mean,'median',s.median,'q1',s.q1,'q3',s.q3,'min',s.min,'max',s.max,
 'buckets',(SELECT jsonb_agg(jsonb_build_object('key',b.key,'label',b.label,'count',b.count) ORDER BY b.ordinal)
 FROM buckets b WHERE b.metric=m.metric))
 ||CASE WHEN m.metric='views' THEN jsonb_build_object('unknown_definition_epoch_count',(SELECT unknown_epoch FROM view_definition))
 ELSE '{}'::jsonb END)
 FROM (VALUES('views'),('duration_seconds'),('likes'),('comments'),('title_length'),('tag_count'))m(metric)
 LEFT JOIN summaries s USING(metric))""".replace('{missing}', missing).replace('{excluded}', excluded).replace('{rows}', rows)


SQL = ',\n' + summary_ctes('population') + FINAL_SELECT.replace('{result}', """jsonb_build_object(
 'population',(SELECT count(*) FROM population),
 'metrics',""" + metrics_json('population', separate_excluded=False) + ')')
