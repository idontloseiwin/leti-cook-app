"""Composed channel UPDATE boundary.

Retain each owner's transaction, native assignment, row lock and final fence.
Initial-error then estimate BEFORE effects precede the original final SET.
Snapshot then discovery AFTER effects complete before returning its receipt.
These Python-owned business effects require their former hooks to be absent.
"""
from collections.abc import Mapping
from psycopg import sql
from psycopg.types.json import Jsonb
from .channel_initial_error_updates import COLUMN_TYPES, _STAGES, _RETURN_ERROR
from .channel_initial_error_fields import ERROR_FIELDS, normalize_initial_error_fields
from .channel_workflow_estimate import normalize_inventory_estimate
from ..base.postgres import DatabaseError
from ..base.sql_values import identifier

_TYPES=dict(COLUMN_TYPES)
_STATUS='initial_long_inventory_status'
_ESTIMATE='initial_inventory_estimate'
_SNAPSHOT_TARGETS=frozenset(('channel_metadata_checked_at','channel_metadata_status','views','subscribers','video_count'))
_DISCOVERY_TARGETS=frozenset(('is_hidden',_STATUS))
_BEFORE_TARGETS=frozenset((_STATUS,_ESTIMATE))
_EFFECT_TARGETS=_SNAPSHOT_TARGETS|_DISCOVERY_TARGETS|_BEFORE_TARGETS
_SNAPSHOT_FACTS="""SELECT new.channel_metadata_status='ok' AND new.channel_metadata_checked_at IS NOT NULL
 AND (old.channel_metadata_checked_at IS DISTINCT FROM new.channel_metadata_checked_at
 OR old.channel_metadata_status IS DISTINCT FROM new.channel_metadata_status) AS capture
 FROM jsonb_to_record(%s::jsonb) old(channel_metadata_checked_at timestamptz,channel_metadata_status text)
 CROSS JOIN jsonb_to_record(%s::jsonb) new(channel_metadata_checked_at timestamptz,channel_metadata_status text)"""
_SNAPSHOT_INSERT="""INSERT INTO public.channel_snapshots(channel_id,recorded_at,views,subscribers,video_count,source)
 SELECT channel_id,channel_metadata_checked_at,views,subscribers,video_count,'youtube_channel_metadata_refresh'
 FROM jsonb_to_record(%s::jsonb) new(channel_id text,channel_metadata_checked_at timestamptz,views bigint,subscribers bigint,video_count bigint)
 ON CONFLICT(channel_id,recorded_at) DO NOTHING"""
_DISCOVERY_FACTS="""SELECT coalesce((SELECT NOT is_hidden AND owner_permission='accepted'
 AND collection_mode IN ('active','metadata_only','initial_inventory')
 FROM public.channel_collection_control WHERE channel_id=%s),false) AS eligible,control.control_epoch
 FROM public.channel_collection_state control WHERE control.channel_id=%s"""
_DISCOVERY_INSERT="""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at,state_origin,inventory_state_origin)
 VALUES(%s,'long','UULF'||substr(%s,3),%s,CASE WHEN %s THEN 'ready' ELSE 'paused' END,now(),'legacy_long_backfill','legacy_long_backfill')
 ON CONFLICT(channel_id,video_type) DO NOTHING"""


def _proposal(expressions,source,predicate,*,key_update=False):
    supplied=sql.SQL(',').join(sql.SQL('({})::{} AS {}').format(value,sql.SQL(_TYPES[name]),identifier(name)) for name,value in expressions.items())
    complete=sql.SQL('ROW({})::public.channels').format(sql.SQL(',').join(
        sql.SQL('locked.{}').format(identifier(name)) if name in expressions else
        sql.SQL('(locked.current).{}').format(identifier(name)) for name,_ in COLUMN_TYPES))
    return sql.SQL("""WITH locked AS MATERIALIZED (
 SELECT channel AS current,{} FROM public.channels channel{} WHERE {} FOR {} OF channel),
 proposed AS (SELECT (locked.current).channel_id AS identity,to_jsonb(locked.current)::text AS old_text,
 (locked.current).initial_inventory_estimate::text AS old_estimate_text,{} AS value FROM locked)
 SELECT identity,old_text,old_estimate_text,to_jsonb(value)::text AS proposed_text,
 (value).initial_inventory_estimate::text AS estimate_text FROM proposed""").format(
        supplied,sql.SQL(source),sql.SQL(predicate),sql.SQL('UPDATE' if key_update else 'NO KEY UPDATE'),complete)


def _final_statement(fields,*,returning=False):
    terms=sql.SQL(',').join(sql.SQL('{}={}').format(identifier(name),
        sql.SQL('%s::jsonb') if name==_ESTIMATE else sql.SQL('proposed.{}').format(identifier(name))) for name in fields)
    suffix=', '+_RETURN_ERROR if returning else ''
    return sql.SQL('UPDATE public.channels channel SET {} FROM jsonb_populate_record(NULL::public.channels,%s::jsonb) proposed WHERE channel.channel_id=%s RETURNING to_jsonb(channel)::text AS new_text'+suffix).format(terms)


def _after(transaction,old_text,new_text,identity,targets):
    if _SNAPSHOT_TARGETS.intersection(targets):
        facts=transaction.fetch_one(_SNAPSHOT_FACTS,(old_text,new_text))
        if not isinstance(facts,Mapping) or set(facts)!={'capture'} or (facts['capture'] is not None and type(facts['capture']) is not bool):
            raise DatabaseError('database_result_shape_invalid')
        if facts['capture'] is True:
            transaction.execute(_SNAPSHOT_INSERT,(new_text,))
    if _DISCOVERY_TARGETS.intersection(targets):
        facts=transaction.fetch_one(_DISCOVERY_FACTS,(identity,identity))
        if facts is not None and (not isinstance(facts,Mapping) or set(facts)!={'eligible','control_epoch'} or type(facts['eligible']) is not bool):
            raise DatabaseError('database_result_shape_invalid')
        eligible=facts is not None and facts['eligible']
        # Even an existing Long row evaluates the original INSERT defaults;
        # do not pre-check existence and silently remove its sequence gap.
        transaction.execute(_DISCOVERY_INSERT,(identity,identity,eligible,eligible))


def _write(transaction,row,targets,*,initial_effect=True,returning=False):
    if row is None:return None if returning else 0
    keys={'identity','old_text','old_estimate_text','proposed_text','estimate_text'}
    if (not isinstance(row,Mapping) or set(row)!=keys
            or any(type(row[key]) is not str for key in ('identity','old_text','proposed_text'))
            or any(row[key] is not None and type(row[key]) is not str for key in ('old_estimate_text','estimate_text'))):
        raise DatabaseError('database_result_shape_invalid')
    normalized=row['proposed_text']
    if _STATUS in targets and initial_effect:
        normalized=normalize_initial_error_fields(transaction,proposed_columns_text=normalized,
            old_columns_text=row['old_text'],status_targeted=True)
    estimate=row['estimate_text']
    if _ESTIMATE in targets:
        estimate=normalize_inventory_estimate(transaction,incoming_text=estimate,
            previous_text=row['old_estimate_text'],targeted=True)
    fields=tuple(dict.fromkeys((*targets,*(ERROR_FIELDS if _STATUS in targets and initial_effect else ()))))
    parameters=((estimate,) if _ESTIMATE in fields else ())+(normalized,row['identity'])
    result=transaction.returning_one(_final_statement(fields,returning=returning),parameters)
    if result is None:return None if returning else 0
    if (not isinstance(result,Mapping) or set(result)!=({'new_text','value'} if returning else {'new_text'})
            or type(result['new_text']) is not str):
        raise DatabaseError('database_result_shape_invalid')
    _after(transaction,row['old_text'],result['new_text'],row['identity'],targets)
    return {'value':result['value']} if returning else 1


def update_channel_fields(transaction,channel_id,values):
    values=dict(values)
    fields=tuple(sorted(values))
    terms=sql.SQL(',').join(sql.SQL('{}=proposed.{}').format(identifier(name),identifier(name)) for name in fields)
    original=sql.SQL('UPDATE public.channels channel SET {} FROM jsonb_populate_record(NULL::public.channels,%s::jsonb) proposed WHERE channel.channel_id=%s').format(terms)
    parameters=(Jsonb(values),channel_id)
    with transaction.borrow():
        if not _EFFECT_TARGETS.intersection(fields):return transaction.execute(original,parameters)
        expressions={name:sql.SQL('proposed.{}').format(identifier(name)) for name in fields}
        row=transaction.fetch_one(_proposal(expressions,' CROSS JOIN jsonb_populate_record(NULL::public.channels,%s::jsonb) proposed',
            'channel.channel_id=%s',key_update=bool({'channel_id','user_work_id'}&set(fields))),parameters)
        return _write(transaction,row,fields)


def apply_channel_stage(transaction,stage,parameters):
    spec=_STAGES[stage]
    with transaction.borrow():
        expressions={name:sql.SQL(value) for name,value in spec.expressions}
        row=transaction.fetch_one(_proposal(expressions,spec.source,spec.predicate),parameters)
        return _write(transaction,row,tuple(expressions),initial_effect=not spec.no_effect,returning=spec.returning)
