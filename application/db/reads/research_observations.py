"""Fixed, indexed observation joins for the shared query executor.

All history is read through protected views in the same statement snapshot.
The consumer selects, filters and sorts the entire scope before taking a page.
Like the ordinary catalogue, a later observation does not invalidate paging.
"""
from research_query.operations import FIELD_OPERATIONS
from research_query.observation_limits import change_age_limit
from . import research_growth


def requested(plan):
    return bool(set(plan.referenced_fields) & FIELD_OPERATIONS.keys())


def proof_now(plan):
    """Lower bound for a proof's own source_expires_at: a frozen read takes only
    sources that outlive the frozen result (args.fresh_until)."""
    return '(SELECT fresh_until FROM args)' if plan.request.consistency == 'frozen' else 'statement_timestamp()'


# The age reference of a video: its publication; for a Stream its actual end
# The instant from which the age windows of a Stream are counted.
AGE_FROM = "CASE WHEN n.video_type='stream' THEN n.actual_end_at ELSE n.published_at END"


def prepared_sql(plan, params):
    if not requested(plan):
        return "prepared AS NOT MATERIALIZED (SELECT normalized.* FROM normalized)", "NULL::timestamptz"
    options=plan.request.observations
    params.update(observation_window=options.window_seconds, observation_tolerance=options.tolerance_seconds,
                  observation_age=options.age_seconds, observation_age_tolerance=options.age_tolerance_seconds)
    change=bool(set(plan.referenced_fields) & {'observation.views_delta','observation.views_per_hour'})
    age='observation.views_at_age' in plan.referenced_fields
    # A change window is offered only for videos refreshed at least once per
    # window under the collection schedule; older videos are left out entirely.
    params.update(observation_age_limit=change_age_limit(plan))
    where=(" WHERE %(observation_age_limit)s::double precision IS NULL OR coalesce(" + AGE_FROM +
           ",'-infinity'::timestamptz)>=(SELECT evaluated_at FROM args)"
           "-make_interval(secs=>%(observation_age_limit)s::double precision)") if change else ''
    joins=[]
    if change:
        joins.append("""LEFT JOIN LATERAL (
 SELECT s.recorded_at,s.views,s.views_definition_epoch,s.source_expires_at
 FROM readable.video_snapshots s WHERE s.video_id=n.video_id
 AND s.recorded_at=n.checked_at AND s.views=n.views AND s.views>=0
 AND s.source_expires_at>{proof_now}
 AND (n.video_type<>'stream' OR s.recorded_at>=n.actual_end_at)
 ORDER BY s.id DESC LIMIT 1) endpoint ON true
 LEFT JOIN LATERAL (
 SELECT s.recorded_at,s.views,s.views_definition_epoch,s.source_expires_at
 FROM readable.video_snapshots s WHERE s.video_id=n.video_id AND endpoint.recorded_at IS NOT NULL
 AND s.views>=0 AND s.recorded_at<=endpoint.recorded_at-make_interval(secs=>%(observation_window)s)
 AND s.recorded_at>=endpoint.recorded_at-make_interval(secs=>%(observation_window)s+%(observation_tolerance)s)
 AND s.source_expires_at>{proof_now}
 AND (n.video_type<>'stream' OR s.recorded_at>=n.actual_end_at)
 ORDER BY s.recorded_at DESC,s.id DESC LIMIT 1) boundary ON true""")
    if age:
        joins.append("""LEFT JOIN LATERAL (
 SELECT s.recorded_at,s.views,s.views_definition_epoch,s.source_expires_at
 FROM readable.video_snapshots s WHERE s.video_id=n.video_id AND s.views>=0
 AND s.recorded_at<={age_from}+make_interval(secs=>%(observation_age)s)
 AND s.recorded_at>={age_from}+make_interval(secs=>%(observation_age)s-%(observation_age_tolerance)s)
 AND s.source_expires_at>{proof_now}
 AND (n.video_type<>'stream' OR s.recorded_at>=n.actual_end_at)
 ORDER BY s.recorded_at DESC,s.id DESC LIMIT 1) age_point ON true""")
    joins=[join.replace('{proof_now}',proof_now(plan)).replace('{age_from}','('+AGE_FROM+')') for join in joins]
    reason=("CASE WHEN endpoint.recorded_at IS NULL THEN 'missing_endpoint'"
            " WHEN boundary.recorded_at IS NULL THEN 'missing_boundary'"
            " WHEN endpoint.views_definition_epoch IS NULL OR boundary.views_definition_epoch IS NULL THEN 'unknown_definition'"
            " WHEN endpoint.views_definition_epoch<>boundary.views_definition_epoch THEN 'incompatible_definition' ELSE 'known' END")
    fields=[]
    deadlines=[]
    if change:
        valid="endpoint.views_definition_epoch=boundary.views_definition_epoch AND endpoint.recorded_at>boundary.recorded_at"
        elapsed="extract(epoch FROM endpoint.recorded_at-boundary.recorded_at)"
        for name,formula in [('views_delta','endpoint.views::numeric-boundary.views::numeric'),
                             ('views_per_hour',f'round((endpoint.views::numeric-boundary.views::numeric)*3600/nullif({elapsed},0),6)')]:
            col='observation_'+name
            fields += [f"CASE WHEN {valid} THEN {formula} END AS {col}",
                       f"CASE WHEN ({reason})='known' THEN 'known' ELSE 'unavailable' END AS {col}_state",
                       f'endpoint.recorded_at AS {col}_observed_at',
                       f'least(endpoint.source_expires_at,boundary.source_expires_at) AS {col}_expires_at',
                       f"jsonb_build_object('method','video.observed_change.v1','version',1,'from_at',boundary.recorded_at,"
                       f"'to_at',endpoint.recorded_at,'actual_seconds',({elapsed})::text,'definition_epoch',endpoint.views_definition_epoch,'reason',({reason})) AS {col}_evidence"]
        deadlines += ['endpoint.source_expires_at','boundary.source_expires_at']
    if age:
        fields += ["age_point.views::numeric AS observation_views_at_age",
                   "CASE WHEN age_point.recorded_at IS NOT NULL THEN 'known' ELSE 'unavailable' END AS observation_views_at_age_state",
                   "age_point.recorded_at AS observation_views_at_age_observed_at",
                   "least(age_point.source_expires_at,n.metadata_checked_at+%(window)s::interval) AS observation_views_at_age_expires_at",
                   "jsonb_build_object('method','video.observed_age.v1','version',1,'from_at',"+AGE_FROM+",'to_at',age_point.recorded_at,"
                   "'actual_seconds',extract(epoch FROM age_point.recorded_at-("+AGE_FROM+"))::text,"
                   "'definition_epoch',age_point.views_definition_epoch,'reason',CASE WHEN age_point.recorded_at IS NULL THEN 'missing_age_observation' ELSE 'known' END) AS observation_views_at_age_evidence"]
        deadlines += ['age_point.source_expires_at','n.metadata_checked_at+%(window)s::interval']
    if research_growth.requested(plan):
        # 28-day growth never narrows the population: every admitted row gets a status.
        params.update(research_growth.parameters(plan))
        joins.append(research_growth.joins(plan))
        fields += list(research_growth.FIELDS)
        deadlines += list(research_growth.DEADLINES)
    fields.append('least('+','.join(deadlines)+') AS observation_deadline')
    # Observation joins feed both whole-scope expiry and the sorted page.
    # Compute them once; raw-only queries retain their inline path.
    return ('prepared AS MATERIALIZED (SELECT n.*,'+','.join(fields)+' FROM normalized n '+ '\n'.join(joins)+where+')',
            '(SELECT min(observation_deadline) FROM prepared WHERE observation_deadline>statement_timestamp())')
