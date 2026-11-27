"""Shared statistics SQL over a query population: one owner for every kind that reads it.

The views definition-epoch rule and the exact linear quantiles are written once
here. video.summary.v1, video.fingerprint.v1 (Overview, over its period rows) and
video.lifetime_baseline.v1 (the Inspector's channel baseline) assemble their
statements from these parts, so a change of either rule reaches all of them.
Fixed text only: `rows` names the population CTE or a CTE over it; values stay
parameters of the calling kind.

Views definition: a current counter is used only with the definition of
the real snapshot that recorded exactly that counter. A value without one (no
such snapshot, or one from before definitions were recorded) is left out on its
own and counted (unknown_epoch); two different recorded definitions withhold
the views statistics as a whole.
"""



def view_definition(rows='population'):
    """Every row's current views with its definition; one definition state for all."""
    return """view_definitions AS MATERIALIZED (
 SELECT p.video_id,p.views,endpoint.views_definition_epoch AS epoch
 FROM {rows} p LEFT JOIN LATERAL (
 SELECT s.views_definition_epoch FROM readable.video_snapshots s
 WHERE s.video_id=p.video_id AND s.recorded_at=p.checked_at AND s.views=p.views
 AND s.source_expires_at>statement_timestamp() ORDER BY s.id DESC LIMIT 1) endpoint ON true
 WHERE p.views IS NOT NULL),
view_definition AS (
 SELECT CASE WHEN count(DISTINCT epoch)>1 THEN 'mixed_definition_epochs'
 WHEN count(epoch)>0 THEN 'known' WHEN count(*)>0 THEN 'unknown_definition_epoch' ELSE 'no_known_values' END AS state,
 CASE WHEN count(DISTINCT epoch)=1 THEN min(epoch) END AS epoch,
 count(*) AS reported,count(*) FILTER(WHERE epoch IS NULL) AS unknown_epoch
 FROM view_definitions)""".replace('{rows}', rows)


def metric_values(values, rows='population'):
    """(metric, value) pairs: views from view_definitions under the definition rule,
    then the other metrics of `rows`.

    values: fixed SQL text of ('metric',expression) tuples over rows p, without
    views; empty for views only. Views come straight from the definition rows: a
    join of two whole-scope results let the planner pick a nested loop (20,000
    rows passed the 5 s statement limit).
    """
    views = ("SELECT 'views' AS metric,d.views AS value FROM view_definitions d"
             " WHERE d.epoch IS NOT NULL AND (SELECT state FROM view_definition)='known'")
    other = ("\n UNION ALL\n SELECT m.metric,m.value FROM " + rows + " p CROSS JOIN LATERAL\n (VALUES" + values
             + ") m(metric,value) WHERE m.value IS NOT NULL") if values else ""
    return "metric_values AS MATERIALIZED (\n " + views + other + ")"


# Exact R-7 quantiles at p = 1/4, 1/2 and 3/4. Their one-based ranks use
# integer division. With n constant per metric, the fractional weight needs
# to be computed only after aggregation. Numeric weights preserve precision.
_QUARTILES = (('q1', 1, 4, '.25'), ('median', 1, 2, '.5'), ('q3', 3, 4, '.25'))
_RANK_VALUES = ',\n '.join(
    f"max(value) FILTER (WHERE r=((n-1)*{numerator})/{denominator}+1) AS {name}_lo,\n "
    f"max(value) FILTER (WHERE r=(((n-1)*{numerator})+{denominator-1})/{denominator}+1) AS {name}_hi"
    for name, numerator, denominator, _ in _QUARTILES)


def _interpolation(name, numerator, denominator, step):
    weight = f"(mod((known_count-1)*{numerator},{denominator})*{step})"
    return f"({name}_lo*(1-{weight})+{name}_hi*{weight})::text AS {name}"


# Exact count, total, mean, extremes and quartiles per metric of metric_values.
SUMMARIES = """ranked_values AS (
 SELECT metric,value,row_number() OVER(PARTITION BY metric ORDER BY value) AS r,
 count(*) OVER(PARTITION BY metric) AS n FROM metric_values),
rank_summary AS (
 SELECT metric,count(*) AS known_count,sum(value)::text AS total,avg(value)::text AS mean,
 min(value)::text AS min,max(value)::text AS max,
 """ + _RANK_VALUES + """
 FROM ranked_values GROUP BY metric),
summaries AS (
 SELECT metric,known_count,total,mean,min,max,
 """ + ',\n '.join(_interpolation(*q) for q in _QUARTILES) + """
 FROM rank_summary)"""


VIEW_DEFINITION = view_definition()
