"""SQL of research_query.operation_kinds.launch_comparison: the database text this operation appends to a population.

Moved from the operation module so that all query construction lives in db/ (CONTRIBUTING.md).
"""
from .inspection import TARGET_JSON
from .inspection import TARGET_SQL
from .common import FINAL_SELECT
from research_query.operation_kinds.inspection import bind_target


def compile_sql(plan, sql, params):
    bind_target(plan, params)
    params['launch_conditions'] = plan.query.request.where is not None or bool(plan.query.request.coverage.required_fields)
    return sql + SQL, params


SQL = """,
""" + TARGET_SQL + """,
launch_horizons AS (SELECT * FROM (VALUES(1,'24h',86400,7200),(2,'7d',604800,86400),(3,'28d',2419200,86400))
 h(ordinal,key,seconds,tolerance)),
launch_targets AS MATERIALIZED (
 SELECT p.video_id,h.ordinal,p.checked_at,r.reference_at,h.tolerance,
 r.reference_at+make_interval(secs=>h.seconds) AS target_at,
 CASE WHEN r.reference_at IS NULL THEN 'missing_publication'
 WHEN p.checked_at IS NULL OR r.reference_at>i.as_of THEN 'ineligible_video'
 WHEN r.reference_at+make_interval(secs=>h.seconds)>i.as_of THEN 'not_reached'
 WHEN r.reference_at+make_interval(secs=>h.seconds)<i.window_from THEN 'outside_observation_window'
 ELSE 'candidate' END AS state
 FROM population p CROSS JOIN launch_horizons h CROSS JOIN inspector_args i
 CROSS JOIN LATERAL (SELECT CASE WHEN p.video_type='stream' THEN p.actual_end_at ELSE p.published_at END AS reference_at) r),
launch_points AS MATERIALIZED (
 SELECT t.video_id,t.ordinal,pick.recorded_at AS observed_at,here.rows_at,here.value_count,here.missing,here.views,
 here.epochs,here.epoch_missing,here.epoch
 FROM launch_targets t CROSS JOIN inspector_args i
 LEFT JOIN LATERAL (
 SELECT s.recorded_at FROM readable.video_snapshots s WHERE s.video_id=t.video_id
 AND s.recorded_at<=t.target_at AND s.recorded_at<=t.checked_at AND s.recorded_at<=i.as_of
 AND s.recorded_at>=greatest(i.window_from,t.target_at-make_interval(secs=>t.tolerance))
 ORDER BY s.recorded_at DESC LIMIT 1) pick ON true
 LEFT JOIN LATERAL (
 SELECT count(*) AS rows_at,count(DISTINCT s.views) FILTER(WHERE s.views>=0) AS value_count,
 bool_or(s.views IS NULL OR s.views<0) AS missing,min(s.views) FILTER(WHERE s.views>=0) AS views,
 count(DISTINCT s.views_definition_epoch) AS epochs,bool_or(s.views_definition_epoch IS NULL) AS epoch_missing,
 min(s.views_definition_epoch) AS epoch
 FROM readable.video_snapshots s WHERE s.video_id=t.video_id AND s.recorded_at=pick.recorded_at) here
 ON pick.recorded_at IS NOT NULL
 WHERE t.state='candidate'),
launch_status AS MATERIALIZED (
 SELECT t.video_id,t.ordinal,t.reference_at,t.target_at,pt.observed_at,pt.views,
 CASE WHEN t.state<>'candidate' THEN t.state WHEN pt.observed_at IS NULL THEN 'missing_snapshot'
 WHEN pt.value_count>1 OR (pt.value_count=1 AND pt.missing) THEN 'conflicting_snapshots'
 WHEN pt.views IS NULL THEN 'invalid_snapshot' ELSE 'ready' END AS status,
 CASE WHEN pt.epochs=1 AND NOT pt.epoch_missing THEN pt.epoch END AS epoch
 FROM launch_targets t LEFT JOIN launch_points pt USING(video_id,ordinal)),
launch_ready AS MATERIALIZED (SELECT ordinal,video_id,views,epoch FROM launch_status WHERE status='ready'),
launch_definition AS MATERIALIZED (
 SELECT h.ordinal,count(r.video_id) AS ready_count,count(r.video_id) FILTER(WHERE r.epoch IS NULL) AS unknown_count,
 count(DISTINCT r.epoch) AS epochs,min(r.epoch) AS epoch
 FROM launch_horizons h LEFT JOIN launch_ready r USING(ordinal) GROUP BY h.ordinal),
launch_cohort AS MATERIALIZED (
 SELECT r.ordinal,r.video_id,r.views,
 row_number() OVER(PARTITION BY r.ordinal ORDER BY r.views,r.video_id COLLATE "C")-1 AS position,
 count(*) OVER(PARTITION BY r.ordinal) AS n
 FROM launch_ready r JOIN launch_definition d USING(ordinal) WHERE r.epoch IS NOT NULL AND d.epochs=1),
launch_sorted AS MATERIALIZED (
 SELECT ordinal,array_agg(views ORDER BY views,video_id COLLATE "C") AS sorted FROM launch_cohort GROUP BY ordinal),
launch_compared AS MATERIALIZED (
 SELECT c.ordinal,c.video_id,q.p25,q.median,q.p75,
 CASE WHEN c.views<q.p25 THEN 'below_typical' WHEN c.views>q.p75 THEN 'above_typical' ELSE 'typical' END AS classification
 FROM launch_cohort c JOIN launch_sorted s USING(ordinal) CROSS JOIN LATERAL (
 SELECT max(x.v) FILTER(WHERE x.name='p25') AS p25,max(x.v) FILTER(WHERE x.name='median') AS median,
 max(x.v) FILTER(WHERE x.name='p75') AS p75
 FROM (SELECT f.name,s.sorted[l.lo+(l.lo>=c.position)::integer+1]::numeric
 +(s.sorted[l.up+(l.up>=c.position)::integer+1]::numeric-s.sorted[l.lo+(l.lo>=c.position)::integer+1]::numeric)*l.w AS v
 FROM (VALUES('p25',0.25),('median',0.5),('p75',0.75)) f(name,fraction)
 CROSS JOIN LATERAL (SELECT floor((c.n-2)*f.fraction)::integer AS lo,
 least(floor((c.n-2)*f.fraction)::integer+1,(c.n-2)::integer) AS up,
 (c.n-2)*f.fraction-floor((c.n-2)*f.fraction) AS w) l) x) q
 WHERE c.n>=4),
launch_counts AS (
 SELECT ordinal,count(*) FILTER(WHERE classification='below_typical') AS below,
 count(*) FILTER(WHERE classification='typical') AS typical,
 count(*) FILTER(WHERE classification='above_typical') AS above
 FROM launch_compared GROUP BY ordinal)
""" + FINAL_SELECT.replace('{result}', """jsonb_build_object(""" + TARGET_JSON + """,
 'as_of',(SELECT as_of FROM inspector_args),'window_from',(SELECT window_from FROM inspector_args),
 'conditions_applied',%(launch_conditions)s::boolean,
 'population',(SELECT count(*) FROM population),
 'candidates',(SELECT count(*) FROM launch_targets WHERE state='candidate'),
 'snapshot_rows',(SELECT coalesce(sum(rows_at),0)::bigint FROM launch_points),
 'horizons',(SELECT jsonb_agg(jsonb_build_object('ordinal',h.ordinal,'key',h.key,'seconds',h.seconds,
 'tolerance',h.tolerance,'ready_count',d.ready_count,'unknown_definition_count',d.unknown_count,
 'definition_epochs',d.epochs,'definition_epoch',d.epoch,
 'cohort_count',(SELECT count(*) FROM launch_cohort c WHERE c.ordinal=h.ordinal),
 'counts',jsonb_build_object('below_typical',coalesce(k.below,0),'typical',coalesce(k.typical,0),
 'above_typical',coalesce(k.above,0)),
 'target',(SELECT jsonb_build_object('status',st.status,'target_at',st.target_at,'observed_at',st.observed_at,
 'views',st.views::text,'epoch',st.epoch,
 'age_us',(extract(epoch FROM st.observed_at-st.reference_at)*1000000)::bigint::text,
 'in_cohort',EXISTS(SELECT 1 FROM launch_cohort c WHERE c.ordinal=st.ordinal AND c.video_id=st.video_id),
 'p25',cm.p25::text,'median',cm.median::text,'p75',cm.p75::text,'classification',cm.classification)
 FROM launch_status st LEFT JOIN launch_compared cm USING(ordinal,video_id)
 WHERE st.ordinal=h.ordinal AND st.video_id=(SELECT video_id FROM inspector_args)))
 ORDER BY h.ordinal)
 FROM launch_horizons h JOIN launch_definition d USING(ordinal) LEFT JOIN launch_counts k USING(ordinal)))""")
