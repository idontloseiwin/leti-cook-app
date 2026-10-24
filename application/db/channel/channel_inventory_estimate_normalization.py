"""Native-text channel estimate effect in the writer transaction.

The caller owns native assignment/default evaluation, locked OLD, initial-error
BEFORE ordering and final DML/constraints. This helper never decodes JSONB or
opens a transaction. Native SQL retains lexical/count, JSONB and clock
semantics.
"""
from collections.abc import Mapping
from ..base.postgres import DatabaseError, DatabaseServerError

_EQUAL = "SELECT %s::jsonb IS NOT DISTINCT FROM %s::jsonb AS same"
_SHAPE = """SELECT jsonb_typeof(value) IS DISTINCT FROM 'object'
 OR jsonb_typeof(value->'counts') IS DISTINCT FROM 'object'
 OR jsonb_typeof(value->'statuses') IS DISTINCT FROM 'object'
 OR jsonb_typeof(value->'video_types') IS DISTINCT FROM 'array'
 OR (value ? 'checked_at_by_type' AND jsonb_typeof(value->'checked_at_by_type') IS DISTINCT FROM 'object') AS invalid
 FROM (SELECT %s::jsonb AS value) input"""
_TYPES = """WITH input AS MATERIALIZED(SELECT %s::jsonb AS value),
 types AS MATERIALIZED(SELECT array_agg(item ORDER BY CASE item WHEN 'long' THEN 1 WHEN 'short' THEN 2 ELSE 3 END) AS kinds
 FROM input CROSS JOIN LATERAL jsonb_array_elements_text(value->'video_types') item),
 checked AS(SELECT kinds,(value->>'scope'='long' AND kinds IS DISTINCT FROM ARRAY['long'])
 OR (value->>'scope'='all' AND kinds IS DISTINCT FROM ARRAY['long','short','stream'])
 OR coalesce(value->>'scope','') NOT IN('long','all')
 OR (SELECT count(*) FROM jsonb_object_keys(value->'counts'))<>cardinality(kinds)
 OR (SELECT count(*) FROM jsonb_object_keys(value->'statuses'))<>cardinality(kinds)
 OR EXISTS(SELECT 1 FROM unnest(kinds) k WHERE NOT(value->'counts' ? k AND value->'statuses' ? k)) AS invalid
 FROM input CROSS JOIN types)
 SELECT invalid,CASE WHEN NOT invalid THEN kinds END AS kinds FROM checked"""
_INITIAL = """WITH input AS(SELECT %s::jsonb AS incoming,%s::jsonb AS previous),
 base AS(SELECT incoming,CASE WHEN %s::boolean AND jsonb_typeof(previous)='object' THEN previous ELSE '{}'::jsonb END AS value FROM input)
 SELECT CASE WHEN value->>'scope'='all' OR incoming->>'scope'='all' THEN ARRAY['long','short','stream'] ELSE ARRAY['long'] END AS kinds,
 coalesce(value->'counts','{}'::jsonb)::text AS counts,coalesce(value->'statuses','{}'::jsonb)::text AS statuses,
 coalesce(value->'checked_at_by_type','{}'::jsonb)::text AS clocks,(value->'checked_at')::text AS common_input FROM base"""
_SEED = """SELECT
 (CASE WHEN NOT(counts ? kind) THEN counts||jsonb_build_object(kind,NULL) ELSE counts END)::text AS counts,
 (CASE WHEN NOT(statuses ? kind) THEN statuses||jsonb_build_object(kind,'unknown') ELSE statuses END)::text AS statuses,
 (CASE WHEN NOT(clocks ? kind) THEN clocks||jsonb_build_object(kind,common_input) ELSE clocks END)::text AS clocks
 FROM (SELECT %s::jsonb AS counts,%s::jsonb AS statuses,%s::jsonb AS clocks,%s::text AS kind,%s::jsonb AS common_input) input"""
_COUNT = """WITH input AS(SELECT value->'counts'->kind AS count,value->'statuses'->>kind AS status
 FROM (SELECT %s::jsonb AS value,%s::text AS kind) source)
 SELECT status IS NULL OR status NOT IN('ok','playlist_not_found','error')
 OR (status='error' AND count IS DISTINCT FROM 'null'::jsonb)
 OR (status='playlist_not_found' AND count IS DISTINCT FROM 'null'::jsonb AND count IS DISTINCT FROM '0'::jsonb)
 OR (status='ok' AND (jsonb_typeof(count) IS DISTINCT FROM 'number' OR count::text !~ '^[0-9]{1,10}$')) AS invalid,
 CASE WHEN status='ok' AND jsonb_typeof(count)='number' AND count::text ~ '^[0-9]{1,10}$'
 THEN count::text::bigint>2147483647 ELSE false END AS overflow,
 (CASE WHEN status='playlist_not_found' THEN 'null'::jsonb ELSE count END)::text AS count,status FROM input"""
_INPUT_CLOCK = """SELECT to_jsonb(CASE WHEN value ? 'checked_at_by_type'
 THEN (value->'checked_at_by_type'->>kind)::timestamptz ELSE (value->>'checked_at')::timestamptz END)#>>'{}' AS clock
 FROM (SELECT %s::jsonb AS value,%s::text AS kind) input"""
_SOURCE_CLOCK = """WITH sampled AS MATERIALIZED(SELECT clock_timestamp() AS at)
 SELECT value IS NULL OR NOT isfinite(value) OR value<at-interval '696 hours' OR value>at AS invalid
 FROM sampled CROSS JOIN (SELECT %s::timestamptz AS value) input"""
_OLD_CLOCK = """WITH input AS MATERIALIZED(SELECT %s::jsonb->>%s::text AS raw),
 diagnosed AS MATERIALIZED(SELECT raw,(pg_input_error_info(raw,'timestamptz')).sql_error_code AS error FROM input)
 SELECT raw,error,to_jsonb(CASE WHEN error IS NULL THEN raw::timestamptz END)#>>'{}' AS clock FROM diagnosed"""
_CAST_CLOCK = "SELECT to_jsonb(%s::timestamptz)#>>'{}' AS clock"
_OLD_FINITE = """SELECT to_jsonb(CASE WHEN NOT isfinite(value) THEN NULL
 WHEN value>clock_timestamp() THEN NULL ELSE value END)#>>'{}' AS clock FROM (SELECT %s::timestamptz AS value) input"""
_DECIDE = """SELECT old_clock IS NOT NULL AND (clock IS NULL OR clock<old_clock) AS keep,
 clock=old_clock AND (count IS DISTINCT FROM counts->kind OR status IS DISTINCT FROM statuses->>kind)
 AND NOT(status='playlist_not_found' AND statuses->>kind='playlist_not_found' AND counts->kind='0'::jsonb) AS conflict
 FROM (SELECT %s::timestamptz AS clock,%s::timestamptz AS old_clock,%s::jsonb AS count,%s::text AS status,
 %s::jsonb AS counts,%s::jsonb AS statuses,%s::text AS kind) input"""
_APPLY = """SELECT (counts||jsonb_build_object(kind,count))::text AS counts,
 (statuses||jsonb_build_object(kind,status))::text AS statuses,(clocks||jsonb_build_object(kind,clock))::text AS clocks
 FROM (SELECT %s::jsonb AS counts,%s::jsonb AS statuses,%s::jsonb AS clocks,
 %s::text AS kind,%s::jsonb AS count,%s::text AS status,%s::timestamptz AS clock) input"""
_COMMON = """SELECT NOT isfinite(clock) AS unknown,
 to_jsonb(CASE WHEN isfinite(clock) THEN least(common,clock) ELSE common END)#>>'{}' AS common
 FROM (SELECT %s::timestamptz AS common,%s::timestamptz AS clock) input"""
_OUTPUT = """SELECT jsonb_build_object('scope',CASE cardinality(kinds) WHEN 3 THEN 'all' ELSE 'long' END,
 'video_types',to_jsonb(kinds),'counts',counts,'statuses',statuses,'checked_at_by_type',clocks,'checked_at',common)::text AS value
 FROM (SELECT %s::text[] AS kinds,%s::jsonb AS counts,%s::jsonb AS statuses,%s::jsonb AS clocks,%s::timestamptz AS common) input"""


def _row(value, keys):
    if not isinstance(value, Mapping) or set(value)!=set(keys):
        raise DatabaseError('database_result_shape_invalid')
    return value


def _flag(row,key,*,nullable=False):
    value=row[key]
    if type(value) is not bool and not(nullable and value is None):
        raise DatabaseError('database_result_shape_invalid')
    return value is True


def _text(row,key,*,nullable=False):
    value=row[key]
    if type(value) is not str and not(nullable and value is None):
        raise DatabaseError('database_result_shape_invalid')
    return value


def _state(row):
    _row(row,('counts','statuses','clocks'))
    return tuple(_text(row,key) for key in ('counts','statuses','clocks'))


def _kinds(value):
    if type(value) is not list or value not in (['long'],['long','short','stream']):
        raise DatabaseError('database_result_shape_invalid')
    return value


def _fail(message,code='P0001'):
    raise DatabaseServerError(message,code=code)


def _old_clock(tx,clocks,kind):
    row=_row(tx.fetch_one(_OLD_CLOCK,(clocks,kind)),('raw','error','clock'))
    error=_text(row,'error',nullable=True)
    clock=_text(row,'clock',nullable=True)
    if error in ('22007','22008'):
        return None
    if error is not None:
        # Only these two SQLSTATEs were swallowed by the trigger. Redispatch
        # the native cast for any other error rather than inventing its detail.
        raw=_text(row,'raw',nullable=True)
        return _text(_row(tx.fetch_one(_CAST_CLOCK,(raw,)),('clock',)),'clock',nullable=True)
    return clock


def normalize_inventory_estimate(transaction,*,incoming_text,previous_text=None,inserted=False,targeted=False):
    """Return typed native JSONB text after exactly the retained estimate effect.

    SQL NULL is None; JSON null is the text 'null'. Writers evaluate original
    casts and lock OLD before entry, and keep this transaction through final
    DML. This pure effect does not supply defaults, write a row or call an RPC.
    """
    if (type(inserted) is not bool or type(targeted) is not bool
            or (incoming_text is not None and type(incoming_text) is not str)
            or (previous_text is not None and type(previous_text) is not str)
            or (inserted and previous_text is not None)):
        raise ValueError('channel_inventory_estimate_native_input_invalid')
    if incoming_text is None or (not inserted and not targeted):
        return incoming_text
    tx=transaction
    with tx.borrow():
        if not inserted and _flag(_row(tx.fetch_one(_EQUAL,(incoming_text,previous_text)),('same',)),'same'):
            return incoming_text
        if _flag(_row(tx.fetch_one(_SHAPE,(incoming_text,)),('invalid',)),'invalid'):
            _fail('channel_inventory_estimate_invalid')
        typed=_row(tx.fetch_one(_TYPES,(incoming_text,)),('invalid','kinds'))
        if _flag(typed,'invalid'):
            _fail('channel_inventory_estimate_invalid')
        input_types=_kinds(typed['kinds'])
        initial=_row(tx.fetch_one(_INITIAL,(incoming_text,previous_text,not inserted)),('kinds','counts','statuses','clocks','common_input'))
        types=_kinds(initial['kinds'])
        counts,statuses,clocks=(_text(initial,key) for key in ('counts','statuses','clocks'))
        common_input=_text(initial,'common_input',nullable=True)
        for kind in types:
            counts,statuses,clocks=_state(tx.fetch_one(_SEED,(counts,statuses,clocks,kind,common_input)))
        for kind in input_types:
            count=_row(tx.fetch_one(_COUNT,(incoming_text,kind)),('invalid','overflow','count','status'))
            if _flag(count,'invalid') or _flag(count,'overflow'):
                _fail('channel_inventory_estimate_invalid')
            value=_text(count,'count')
            status=_text(count,'status')
            clock=_text(_row(tx.fetch_one(_INPUT_CLOCK,(incoming_text,kind)),('clock',)),'clock',nullable=True)
            if clock is not None or status!='error':
                if _flag(_row(tx.fetch_one(_SOURCE_CLOCK,(clock,)),('invalid',)),'invalid'):
                    _fail('hot_source_clock_out_of_window','22023')
            old_clock=_old_clock(tx,clocks,kind)
            if old_clock is not None:
                old_clock=_text(_row(tx.fetch_one(_OLD_FINITE,(old_clock,)),('clock',)),'clock',nullable=True)
            decision=_row(tx.fetch_one(_DECIDE,(clock,old_clock,value,status,counts,statuses,kind)),('keep','conflict'))
            if _flag(decision,'keep'):
                continue
            if _flag(decision,'conflict',nullable=True):
                _fail('channel_inventory_estimate_clock_conflict')
            counts,statuses,clocks=_state(tx.fetch_one(_APPLY,(counts,statuses,clocks,kind,value,status,clock)))
        common=None
        unknown=False
        for kind in types:
            clock=_old_clock(tx,clocks,kind)
            if clock is None:
                unknown=True
            else:
                facts=_row(tx.fetch_one(_COMMON,(common,clock)),('unknown','common'))
                unknown=unknown or _flag(facts,'unknown')
                common=_text(facts,'common',nullable=True)
        result=_row(tx.fetch_one(_OUTPUT,(types,counts,statuses,clocks,None if unknown else common)),('value',))
        return _text(result,'value')
