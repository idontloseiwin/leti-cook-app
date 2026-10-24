"""Finite channel mode/count SQL over native facts, owned by pure policies.

No caller supplies identifiers, operators or SQL. Classification tables are
shared with Python; PostgreSQL retains snapshots, joins, type casts and set math.
Other consumers of the retained policy views remain separate migration work.
"""
from channel_sync_policy import (CHANNEL_MODE_RULES, CHANNEL_MODE_DEFAULT,
    CHANNEL_RUNTIME_TYPED_RULES, CHANNEL_PAUSE_RULES)
from channel_inventory_estimate import (COUNT_KINDS, COUNT_DIGIT_PATTERN,
    COUNT_MAXIMUM, COUNT_FRESH_HOURS, COUNT_SOFT_CLOCK_ERRORS,
    COUNT_PRENUMBER_FACTS, COUNT_PRECLOCK_FACTS)

FACT_SQL = {
    "control_present": "control.channel_id IS NOT NULL AND channel.channel_id IS NOT NULL",
    "hidden": "control.is_hidden",
    "pause_hidden": "coalesce(control.is_hidden,true)",
    "permission_accepted": "control.owner_permission='accepted'",
    "permission_not_accepted": "control.owner_permission<>'accepted'",
    "permission_manual": "control.owner_permission='manual_required'",
    "linked": "control.initial_job_id IS NOT NULL",
    "job_missing": "job.id IS NULL",
    "job_channel_distinct": "job.channel_id IS DISTINCT FROM control.channel_id",
    "job_type_distinct": "job.job_type IS DISTINCT FROM 'channel_initial_long_inventory'",
    "epoch_null": "job.accepted_epoch IS NULL",
    "errors_null": "job.error_count IS NULL",
    "status_null": "job.status IS NULL",
    "phase_null": "job.phase IS NULL",
    "types_long": "job.initial_video_types IS NOT DISTINCT FROM ARRAY['long']::text[]",
    "types_all": "job.initial_video_types IS NOT DISTINCT FROM ARRAY['long','short','stream']::text[]",
    "type_count_three": "cardinality(job.initial_video_types)=3",
    "scope_full": "job.initial_scope_version=1",
    "status_blocked": "job.status='blocked'",
    "status_error": "job.status='error'",
    "status_suspended": "job.status='suspended'",
    "status_completed": "job.status='completed'",
    "frozen_null": "job.targets_frozen_at IS NULL",
    "errors_zero": "job.error_count=0",
    "phase_verify": "job.phase='verify'",
    "phase_initial_stats": "job.phase='initial_stats'",
    "initial_completed": "channel.initial_long_inventory_status='completed'",
    "initial_skipped": "channel.initial_long_inventory_status='skipped_no_long_playlist'",
}
OUTPUT_SQL = {None: "NULL::text", False: "false", True: "true", **{
    name: "'" + name + "'::text" for name in (
        "operator_hold", "hidden", "metadata_only", "active", "initial_inventory",
        "channel_hidden", "channel_no_long_playlist", "channel_inventory_not_ready",
        "channel_inventory_manual_required")}}
FACT_NAMES = frozenset(FACT_SQL) | {"mode_active", "mode_metadata", "mode_initial"}

def _case_sql(rules, default):
    for rule in rules:
        assert all(name in FACT_NAMES and type(required) is bool for name, required in rule.terms)
    return "CASE\n" + "".join(" WHEN " + " AND ".join(
        "facts." + name + (" IS TRUE" if required else " IS FALSE")
        for name, required in rule.terms) + " THEN " + OUTPUT_SQL[rule.result] + "\n"
        for rule in rules) + " ELSE " + OUTPUT_SQL[default] + " END"


CHANNEL_FACTS_SQL = "SELECT channel.channel_id," + ",\n".join(
    "(" + expression + ") AS " + name for name, expression in FACT_SQL.items()) + """
 FROM public.channels channel
 LEFT JOIN public.channel_collection_state control USING(channel_id)
 LEFT JOIN public.operator_jobs job ON job.id=control.initial_job_id"""
CHANNEL_MODE_CASE = _case_sql(CHANNEL_MODE_RULES, CHANNEL_MODE_DEFAULT)
CHANNEL_TYPED_CASE = _case_sql(CHANNEL_RUNTIME_TYPED_RULES, False)
CHANNEL_PAUSE_CASE = _case_sql(CHANNEL_PAUSE_RULES, "channel_inventory_not_ready")
CHANNEL_POLICY_CTES = "channel_policy_facts AS (" + CHANNEL_FACTS_SQL + """),
 channel_policy_modes AS (SELECT facts.*,""" + CHANNEL_MODE_CASE + """ AS mode
 FROM channel_policy_facts facts),
 channel_policy_flags AS (SELECT modes.*,mode='active' AS mode_active,
 mode='metadata_only' AS mode_metadata,mode='initial_inventory' AS mode_initial
 FROM channel_policy_modes modes),
 channel_policy AS (SELECT facts.channel_id,facts.mode,""" + CHANNEL_TYPED_CASE + """ AS typed_runtime_eligible,
 """ + CHANNEL_PAUSE_CASE + " AS pause_reason FROM channel_policy_flags facts)"

# One scalar expression substitutes each old function call at its original
# expression position; do not eagerly parse every channel before the old CASE.
# All casts capable of failing stay below the same lexical/range/map admissions.
def _all_facts(names):
    return " AND ".join(name + " IS TRUE" for name in names)

_LONG_COUNT = """(WITH count_input AS (SELECT c.initial_inventory_estimate AS estimate,
 'long'::text AS kind,statement_timestamp() AS at), count_head AS (
 SELECT *,estimate->'counts'->kind AS value,
 (estimate->'statuses'->>kind)='ok' AS status_ok,
 kind IN (""" + ",".join("'"+kind+"'" for kind in COUNT_KINDS) + """ ) AS kind_allowed,
 jsonb_typeof(estimate)='object' AS estimate_object,
 jsonb_typeof(estimate->'counts'->kind)='number'
 AND (estimate->'counts'->kind)::text ~ '""" + COUNT_DIGIT_PATTERN + """' AS number_lexical,
 NOT (estimate ? 'checked_at_by_type') OR jsonb_typeof(estimate->'checked_at_by_type')='object' AS clock_map_allowed
 FROM count_input), count_numbers AS (SELECT *,CASE WHEN """ + _all_facts(COUNT_PRENUMBER_FACTS) + """
 THEN value::text::bigint END AS count FROM count_head), count_range AS (
 SELECT *,count<=""" + str(COUNT_MAXIMUM) + """ AS count_in_range FROM count_numbers), count_clocks AS (
 SELECT *,CASE WHEN """ + _all_facts(COUNT_PRECLOCK_FACTS) + """ THEN
 CASE WHEN estimate ? 'checked_at_by_type' THEN estimate->'checked_at_by_type'->>kind
 ELSE estimate->>'checked_at' END END AS clock_text FROM count_range), count_diagnosed AS (
 SELECT *,(pg_input_error_info(clock_text,'timestamptz')).sql_error_code AS clock_error FROM count_clocks),
 count_checked AS (SELECT *,CASE WHEN clock_error IN (""" + ",".join("'"+code+"'" for code in COUNT_SOFT_CLOCK_ERRORS) + """ )
 THEN NULL::timestamptz ELSE clock_text::timestamptz END AS clock FROM count_diagnosed)
 SELECT CASE WHEN """ + _all_facts(COUNT_PRECLOCK_FACTS) + """ AND clock IS NOT NULL AND isfinite(clock)
 AND clock<=at AND clock>=at-interval '""" + str(COUNT_FRESH_HOURS) + """ hours' THEN count::integer END
 FROM count_checked)"""
LONG_ESTIMATE_COUNT_SQL = _LONG_COUNT


# Fixed relational projections for existing callers. These keep the original
# control -> channel inner join and linked-job left join; the finite CASEs above
# remain the only classification rules. No caller supplies SQL or identifiers.
_CONTROL_COLUMNS = ('channel_id','user_work_id','owner_permission','is_hidden',
                    'hidden_at','control_epoch','initial_job_id')
_CONTROL_FROM = """ FROM public.channel_collection_state control
 JOIN public.channels channel ON channel.channel_id=control.channel_id
 LEFT JOIN public.operator_jobs job ON job.id=control.initial_job_id"""
_CONTROL_NATIVE = "SELECT " + ",".join("control."+name for name in _CONTROL_COLUMNS) + "," + ",".join(
    "("+expression+") AS "+name for name,expression in FACT_SQL.items()) + _CONTROL_FROM
_CONTROL_MODE = "SELECT facts.*,"+CHANNEL_MODE_CASE+" AS collection_mode FROM ("+_CONTROL_NATIVE+") facts"
_CONTROL_FLAGS = """SELECT modes.*,collection_mode='active' AS mode_active,
 collection_mode='metadata_only' AS mode_metadata,collection_mode='initial_inventory' AS mode_initial
 FROM ("""+_CONTROL_MODE+") modes"
CHANNEL_CONTROL_SQL = "SELECT " + ",".join("facts."+name for name in _CONTROL_COLUMNS) + ",facts.collection_mode," + CHANNEL_TYPED_CASE + " AS typed_runtime_eligible FROM (" + _CONTROL_FLAGS + ") facts"
_FACTS_OBJECT = "jsonb_build_object(" + ",".join("'"+name+"',facts."+name for name in FACT_SQL) + ")"
CHANNEL_CONTROL_FACTS_SQL = "SELECT " + ",".join("facts."+name for name in _CONTROL_COLUMNS) + "," + _FACTS_OBJECT + " AS policy_facts FROM (" + _CONTROL_NATIVE + ") facts"
CHANNEL_FACTS_RELATION_SQL = "SELECT facts.channel_id," + _FACTS_OBJECT + " AS policy_facts FROM (" + CHANNEL_FACTS_SQL + ") facts"
CHANNEL_STATE_MODE_SQL = "coalesce((SELECT policy.collection_mode FROM ("+CHANNEL_CONTROL_SQL+") policy WHERE policy.channel_id=state.channel_id),'operator_hold')"
CHANNEL_STATE_TYPED_SQL = "coalesce((SELECT policy.typed_runtime_eligible FROM ("+CHANNEL_CONTROL_SQL+") policy WHERE policy.channel_id=state.channel_id),false)"


def _project_policy_row(row, field, choice):
    if row is None:
        return None
    from ..base.postgres import DatabaseError
    from channel_sync_policy import channel_policy_from_native_facts
    if not isinstance(row,dict) or 'policy_facts' not in row:
        raise DatabaseError('database_result_shape_invalid')
    result=dict(row)
    facts=result.pop('policy_facts')
    if facts is None:
        # A scalar subquery with no source row. Existing anchored pause SELECTs
        # preserve NULL, while mode/typed scalar fallbacks remain hold/false.
        policy={'mode':'operator_hold','typed_runtime_eligible':False,'pause_reason':None}
    else:
        if not isinstance(facts,dict) or set(facts)!=set(FACT_SQL):
            raise DatabaseError('database_result_shape_invalid')
        try:policy=channel_policy_from_native_facts(facts)
        except (KeyError,ValueError,TypeError):
            raise DatabaseError('database_result_shape_invalid') from None
    result[field]=policy[choice]
    return result


def channel_mode_row(row, *, field='collection_mode'):
    return _project_policy_row(row,field,'mode')


def channel_eligible_row(row, *, field='eligible'):
    return _project_policy_row(row,field,'typed_runtime_eligible')


def channel_pause_row(row, *, field='pause_reason'):
    return _project_policy_row(row,field,'pause_reason')
