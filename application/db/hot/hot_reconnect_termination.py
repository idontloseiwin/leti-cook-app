"""Closed, source-free cancellation of one acknowledged pending-channel lease."""
from uuid import UUID

from ..base.operation_contracts import get_operation

ERRORS = get_operation('abort_hot_reconnect_claim').error_codes

# No SKIP LOCKED: a busy row must not be mistaken for a lost lease. The closed
# driver imposes 250ms lock / 5s total budgets. Only private ownership fields are
# read, never the locator, labels, saved work or API payloads.
_OWNED = '''SELECT revision,reconnect_revision,reconnect_state,
 reconnect_lease_token::text AS lease_token
 FROM public.user_channel_restore_items WHERE id=%s::uuid FOR UPDATE NOWAIT'''
_RELEASE = '''UPDATE public.user_channel_restore_items
 SET reconnect_state='waiting',reconnect_not_before=clock_timestamp()+interval '30 seconds',
 reconnect_error_code='hot_runtime_interrupted',
 reconnect_lease_token=NULL,reconnect_lease_expires_at=NULL
 WHERE id=%s::uuid AND revision=%s AND reconnect_revision=%s
 AND reconnect_state='running' AND reconnect_lease_token=%s::uuid
 RETURNING id::text AS id'''


def validate(identity, expected_revision, lease_token):
    if (type(identity) is not str or type(lease_token) is not str
            or type(expected_revision) is not int or not 1 <= expected_revision <= 9007199254740991):
        raise ValueError('hot_reconnect_claim_invalid')
    try:
        canonical_id, canonical_token = str(UUID(identity)), str(UUID(lease_token))
    except ValueError:
        raise ValueError('hot_reconnect_claim_invalid') from None
    if canonical_id != identity or canonical_token != lease_token:
        raise ValueError('hot_reconnect_claim_invalid')
    return identity, expected_revision, lease_token


def abort_hot_reconnect_claim(transaction, identity, expected_revision, lease_token):
    identity, expected, token = validate(identity, expected_revision, lease_token)
    with transaction.borrow():
        row = transaction.fetch_one(_OWNED, (identity,))
        if (row is None or row['revision'] != expected or row['reconnect_revision'] != expected
                or row['reconnect_state'] != 'running' or row['lease_token'] != token):
            return {'outcome': 'lease_lost'}
        written = transaction.returning_one(_RELEASE, (identity, expected, expected, token))
        if written is None or written.get('id') != identity:
            raise RuntimeError('hot_reconnect_abort_unconfirmed')
        return {'outcome': 'ready'}
