"""Channel INSERT normalization followed by explicit INSERT effects.

The final schema delegates these business effects to this composed writer. It
never toggles hooks or owns permission/adoption/final fences.
"""
from collections.abc import Mapping
from psycopg import sql
from psycopg.types.json import Jsonb
from .channel_initial_error_inserts import (COLUMN_TYPES,_DEFAULTS,_SCHEMA_PIN,
    _SCHEMA_PARAMETERS,_proposal_statement,_RESTORE_INSERT)
from .channel_initial_error_fields import normalize_initial_error_fields
from .channel_workflow_estimate import normalize_inventory_estimate
from ..base.postgres import DatabaseError
from ..base.sql_values import identifier


def _schema(transaction):
    facts=transaction.fetch_one(_SCHEMA_PIN,_SCHEMA_PARAMETERS)
    if not isinstance(facts,Mapping) or set(facts)!={'matches'} or type(facts['matches']) is not bool:
        raise DatabaseError('database_result_shape_invalid')
    if not facts['matches']:raise DatabaseError('database_operation_failed',code='P0001')


def _insert_captured(transaction,row):
    if (not isinstance(row,Mapping) or set(row)!={'columns_text','estimate_text'}
            or type(row['columns_text']) is not str
            or (row['estimate_text'] is not None and type(row['estimate_text']) is not str)):
        raise DatabaseError('database_result_shape_invalid')
    columns=normalize_initial_error_fields(transaction,proposed_columns_text=row['columns_text'],inserted=True)
    estimate=normalize_inventory_estimate(transaction,incoming_text=row['estimate_text'],inserted=True)
    # The accepted69 create family writes all39 captured columns and owns its
    # existing compound AFTER graph; restore supplies its native proposal here.
    return transaction.insert_channel_with_effects('create',(estimate,columns))


def insert_collection_channel(transaction,values):
    values=dict(values)
    fields=tuple(sorted(values))
    for name in fields:identifier(name)
    with transaction.borrow():
        _schema(transaction)
        return _insert_captured(transaction,transaction.fetch_one(_proposal_statement(fields),(Jsonb(values),)))


def _restore_proposal():
    prefix='INSERT INTO public.channels('
    assert _RESTORE_INSERT.startswith(prefix)
    target,select=_RESTORE_INSERT[len(prefix):].split(')\n SELECT ',1)
    fields=tuple(name.strip() for name in target.split(','))
    assert len(fields)==28 and len(set(fields))==28 and set(fields)<=dict(COLUMN_TYPES).keys()
    # Materialize the original28 expressions in their original SELECT order,
    # including scalar casts and the first conditional hidden fallback clock.
    aliases=sql.SQL(',').join(identifier(name) for name in fields)
    terms=sql.SQL(',').join(sql.SQL('supplied.{}').format(identifier(name)) if name in fields else
        sql.SQL(_DEFAULTS.get(name,'NULL::'+kind)) for name,kind in COLUMN_TYPES)
    return sql.SQL('WITH supplied({}) AS MATERIALIZED (SELECT '+select+'''), proposed AS MATERIALIZED (
 SELECT ROW({})::public.channels AS value FROM supplied)
 SELECT to_jsonb(value)::text AS columns_text,(value).initial_inventory_estimate::text AS estimate_text FROM proposed''').format(aliases,terms)


_RESTORE_PROPOSAL=_restore_proposal()


def insert_restored_channel(transaction,parameters):
    with transaction.borrow():
        _schema(transaction)
        row=_insert_captured(transaction,transaction.fetch_one(_RESTORE_PROPOSAL,parameters))
        return 0 if row is None else 1
