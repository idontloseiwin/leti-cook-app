"""Quota retention delete effect in the maintenance transaction.

The existing maintenance owner supplies the transaction, per-parent savepoint,
cutoff and 250 ms lock budget. Hold the exact expired parent before opening one
fixed child snapshot. Native FK checks fence new references while Python
applies only the four existing label rules to every surviving child. The
enabled FK and original parent DELETE remain the final referential and receipt
authorities.
"""
from collections.abc import Mapping
from operational_copy_policy import quota_label
from ..base.postgres import DatabaseError

_LABELS = ('phase', 'endpoint', 'quota_bucket', 'error_type')
_PARENT = """SELECT request_attempt_id::text AS id
 FROM public.youtube_api_request_attempts
 WHERE request_attempt_id=%s::uuid AND started_at<%s::timestamptz
 FOR UPDATE"""
# The fixed driver cursor holds one statement snapshot. It must not acquire a
# new snapshot/page or skip locked children. The removed parent's self-reference
# has no surviving child effect in the retained native DELETE.
_CHILDREN = """SELECT request_attempt_id::text AS id,phase,endpoint,quota_bucket,error_type
 FROM public.youtube_api_request_attempts
 WHERE retry_of_request_attempt_id=%s::uuid AND request_attempt_id<>%s::uuid
 FOR NO KEY UPDATE"""
_DETACH = """UPDATE public.youtube_api_request_attempts
 SET retry_of_request_attempt_id=NULL,phase=%s::text,endpoint=%s::text,
 quota_bucket=%s::text,error_type=%s::text
 WHERE request_attempt_id=%s::uuid AND retry_of_request_attempt_id=%s::uuid"""
_DELETE = """DELETE FROM public.youtube_api_request_attempts
 WHERE request_attempt_id=%s::uuid AND started_at<%s::timestamptz"""


def _shape_error():
    raise DatabaseError('database_result_shape_invalid')


def delete_expired_quota_attempt(transaction, *, attempt_id, cutoff):
    """Detach/normalize all children, then return only the original DELETE count.

    There is no child cutoff, status/project filter or materialized Python page.
    Each child remains natively locked from cursor fetch through its UPDATE;
    fields other than its FK and four labels are never reconstructed or assigned.
    The caller's savepoint must roll back every child change when a later lock or
    deadline fails. No commit, new pool, retry, clock, default or FK DDL is owned
    here. Integration requires the fixed maintenance-only driver cursor.
    """
    tx = transaction
    with tx.borrow():
        parent = tx.fetch_one(_PARENT, (attempt_id, cutoff))
        if parent is None:
            return 0
        if not isinstance(parent, Mapping) or set(parent) != {'id'} or type(parent['id']) is not str:
            _shape_error()
        parent_id = parent['id']
        with tx.quota_retry_children(parent_id=parent_id) as children:
            for child in children:
                if (not isinstance(child, Mapping) or set(child) != {'id', *_LABELS}
                        or type(child['id']) is not str or child['id'] == parent_id
                        or any(child[name] is not None and type(child[name]) is not str for name in _LABELS)):
                    _shape_error()
                labels = tuple(quota_label(name, child[name]) for name in _LABELS)
                changed = tx.execute(_DETACH, (*labels, child['id'], parent_id))
                if type(changed) is not int or changed != 1:
                    _shape_error()
        changed = tx.execute(_DELETE, (parent_id, cutoff))
        if type(changed) is not int or changed != 1:
            _shape_error()
        return changed
