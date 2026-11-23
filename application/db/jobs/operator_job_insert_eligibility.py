"""Finite eligibility boundary for the two native public job INSERT shapes.

Caller retains channel ownership, original INSERT/defaults/constraints and final
control fence. This reads fresh native facts, never opens a transaction or writes.
The intrinsic source-scope trigger remains authoritative at the actual INSERT.
"""
from collections.abc import Mapping
from ..inventory.inventory_scan_steps import _HOT
from ..channel.channel_read_policy import CHANNEL_CONTROL_FACTS_SQL,channel_mode_row
from ..base.postgres import DatabaseError,DatabaseServerError

_NOW="SELECT to_jsonb(now())#>>'{}' AS requested_at"
_HIDDEN="SELECT coalesce((SELECT is_hidden FROM public.channel_collection_state WHERE channel_id=%s::text),true) AS hidden"
_MODE='SELECT (SELECT policy_facts FROM ('+CHANNEL_CONTROL_FACTS_SQL+') policy WHERE channel_id=%s::text) AS policy_facts'
_PRESENT='SELECT EXISTS(SELECT 1 FROM public.channel_collection_state WHERE channel_id=%s::text) AS present'
_KINDS=frozenset(('channel_current_stats_backfill','channel_initial_long_inventory'))


def _flag(row,key):
    if not isinstance(row,Mapping) or set(row)!={key} or type(row[key]) is not bool:
        raise DatabaseError('database_result_shape_invalid')
    return row[key]


def _fail(message):
    raise DatabaseServerError(message,code='P0001')


def assert_operator_job_insert_eligible(transaction,channel_id,job_type):
    """Fresh source, hidden, mode/control order immediately before original INSERT.

    Public owners have already cast arguments and fix job_type. requested_at is
    omitted by both and has native now() default; use the existing native source
    clock predicate, without supplying or changing that default. No row lock or
    retry is added. A concurrent hide after these reads is rejected by the
    caller final fence as scope_lost with full rollback; that bounded code
    difference from the former insert hook is an accepted public conflict.
    A clock jump between this read and INSERT while eligibility
    also fails can change first-error precedence; no equivalence claim covers it.
    """
    if type(job_type) is not str or job_type not in _KINDS:
        raise ValueError('operator_job_insert_kind_invalid')
    tx=transaction
    with tx.borrow():
        row=tx.fetch_one(_NOW)
        if not isinstance(row,Mapping) or set(row)!={'requested_at'} or type(row['requested_at']) is not str:
            raise DatabaseError('database_result_shape_invalid')
        stamp=row['requested_at']
        if not _flag(tx.fetch_one(_HOT,(stamp,stamp)),'valid'):
            _fail('operator_job_source_scope_expired')
        if _flag(tx.fetch_one(_HIDDEN,(channel_id,)),'hidden'):
            _fail('operator_job_channel_not_visible')
        if job_type=='channel_current_stats_backfill':
            mode=tx.fetch_one(_MODE,(channel_id,))
            if not isinstance(mode,dict) or set(mode)!={'policy_facts'}:
                raise DatabaseError('database_result_shape_invalid')
            if channel_mode_row(mode,field='mode')['mode']!='active':
                _fail('operator_job_channel_sync_not_ready')
        elif not _flag(tx.fetch_one(_PRESENT,(channel_id,)),'present'):
            _fail('operator_job_initial_inventory_status_not_eligible')
