"""Preserve native statement error exposure for the composed estimate effect.

The effective transaction already owns its immutable allowed-error vocabulary.
Only the three errors synthesized by the transferred native estimate effect are
mapped here; native driver errors and unrelated Python validation pass through.
No caller flag, extra grant, query, transaction, retry or driver change.
"""
from .channel_inventory_estimate_normalization import normalize_inventory_estimate as _normalize
from ..base.postgres import DatabaseError,DatabaseServerError

_ERRORS={
    'channel_inventory_estimate_invalid':'P0001',
    'channel_inventory_estimate_clock_conflict':'P0001',
    'hot_source_clock_out_of_window':'22023',
}


def normalize_inventory_estimate(transaction,*,incoming_text,previous_text=None,inserted=False,targeted=False):
    try:
        return _normalize(transaction,incoming_text=incoming_text,previous_text=previous_text,
                          inserted=inserted,targeted=targeted)
    except DatabaseServerError as error:
        if _ERRORS.get(error.message)!=error.code:
            raise
        # This is the same bounded driver's vocabulary used by _mapped for the
        # original trigger exception. Borrowed calls retain the outer operation.
        allowed=transaction._allowed
        if type(allowed) is not frozenset:
            raise DatabaseError('database_result_shape_invalid') from None
        if error.message in allowed:
            raise
        raise DatabaseServerError('database_operation_failed',code=error.code) from None
