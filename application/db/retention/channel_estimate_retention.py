"""Exact two-write estimate retention boundary."""
from psycopg.types.json import Jsonb
from ..channel.channel_workflow_estimate import normalize_inventory_estimate
from ..base.postgres import DatabaseError

_CLEAR='UPDATE public.channels SET initial_inventory_estimate=NULL WHERE channel_id=%s'
_WRITE='UPDATE public.channels SET initial_inventory_estimate=%s::jsonb WHERE channel_id=%s'
_TYPED='SELECT %s::jsonb::text AS estimate_text'


def reset_inventory_estimate(transaction,channel_id,proposed):
    """Caller already owns channel SKIP LOCKED and its locked scrub decision."""
    with transaction.borrow():
        transaction.execute(_CLEAR,(channel_id,))
        if proposed is not None:
            # Preserve serialization after the first actual NULL UPDATE. OLD at
            # the second UPDATE is SQL NULL, never the expired pre-scrub input.
            row=transaction.fetch_one(_TYPED,(Jsonb(proposed),))
            if not isinstance(row,dict) or set(row)!={'estimate_text'} or type(row['estimate_text']) is not str:
                raise DatabaseError('database_result_shape_invalid')
            estimate=normalize_inventory_estimate(transaction,incoming_text=row['estimate_text'],previous_text=None,targeted=True)
            transaction.execute(_WRITE,(estimate,channel_id))
