"""Channel initial-error BEFORE effect in the caller's transaction.

The writer owns native assignment/default evaluation and a lock on OLD through
its final DML. This finite helper borrows that same transaction, receives typed
full-row JSONB text, and returns the proposed native row with only the three
initial-error fields normalized. It does not construct an INSERT/UPDATE,
acquire a pool, choose a target, retry, or transfer snapshot/control/discovery
effects.
"""
from collections.abc import Mapping
from ..base.postgres import DatabaseError

ERROR_FIELDS = (
    'initial_long_inventory_error_code',
    'initial_long_inventory_error_at',
    'initial_long_inventory_error_count',
)

# Inputs must already represent complete native OLD/NEW composites. Casting the
# transport back to channels keeps integer/time types native; it is not incoming
# wire assignment conversion and does not supply omitted INSERT defaults.
_FACTS = """WITH native_rows AS (
 SELECT jsonb_populate_record(NULL::public.channels,%s::jsonb) AS proposed,
        jsonb_populate_record(NULL::public.channels,%s::jsonb) AS previous)
 SELECT to_jsonb(proposed)::text AS proposed_text,
 coalesce((proposed).initial_long_inventory_status='completed',false) AS completed,
 coalesce((proposed).initial_long_inventory_status='error',false) AS errored,
 (previous).initial_long_inventory_status IS NOT DISTINCT FROM 'error' AS was_error,
 coalesce((previous).initial_long_inventory_error_count,0) AS previous_count
 FROM native_rows"""
_COMPLETED = """SELECT (%s::jsonb || jsonb_build_object(
 'initial_long_inventory_error_code',NULL,
 'initial_long_inventory_error_at',NULL,
 'initial_long_inventory_error_count',0))::text AS proposed_text"""
_ERROR = """WITH native_row AS (
 SELECT jsonb_populate_record(NULL::public.channels,%s::jsonb) AS proposed)
 SELECT (to_jsonb(proposed) || jsonb_build_object(
 'initial_long_inventory_error_code',coalesce((proposed).initial_long_inventory_error_code,'initial_inventory_failed'),
 'initial_long_inventory_error_at',coalesce((proposed).initial_long_inventory_error_at,now()),
 'initial_long_inventory_error_count',greatest(coalesce((proposed).initial_long_inventory_error_count,0),%s::integer+1)
 ))::text AS proposed_text FROM native_row"""


def _text(row):
    if not isinstance(row, Mapping) or set(row) != {'proposed_text'} or type(row['proposed_text']) is not str:
        raise DatabaseError('database_result_shape_invalid')
    return row['proposed_text']


def normalize_initial_error_fields(transaction, *, proposed_columns_text, old_columns_text=None,
                                   inserted=False, status_targeted=False):
    """Return native full NEW text after the retained initial-error effect.

    INSERT always runs this effect. UPDATE runs it only when its original SET
    list names initial_long_inventory_status, including same-value assignments.
    Callers must keep their assignment cast/error ordering, INSERT omission and
    defaults, lock/recheck, savepoint, final constraints/RETURNING and scope fence.

    Completed clears all three fields; error INSERT/transition fills NULL code
    and time and applies PostgreSQL int32 greatest(new_count,old_count+1).
    Error-to-error and other statuses preserve supplied error fields exactly.
    now() is the caller transaction clock, never a new Python/wall-clock sample.
    """
    if (type(inserted) is not bool or type(status_targeted) is not bool
            or type(proposed_columns_text) is not str
            or (inserted and old_columns_text is not None)
            or (not inserted and type(old_columns_text) is not str)):
        raise ValueError('channel_initial_error_native_row_invalid')
    if not inserted and not status_targeted:
        return proposed_columns_text
    with transaction.borrow():
        facts = transaction.fetch_one(_FACTS, (proposed_columns_text, old_columns_text))
        if (not isinstance(facts, Mapping)
                or set(facts) != {'proposed_text','completed','errored','was_error','previous_count'}
                or type(facts['proposed_text']) is not str
                or any(type(facts[key]) is not bool for key in ('completed','errored','was_error'))
                or type(facts['previous_count']) is not int
                or not -(1 << 31) <= facts['previous_count'] < (1 << 31)):
            raise DatabaseError('database_result_shape_invalid')
        if facts['completed']:
            return _text(transaction.fetch_one(_COMPLETED, (facts['proposed_text'],)))
        if facts['errored'] and (inserted or not facts['was_error']):
            previous_count = 0 if inserted else facts['previous_count']
            return _text(transaction.fetch_one(_ERROR, (facts['proposed_text'], previous_count)))
        return facts['proposed_text']