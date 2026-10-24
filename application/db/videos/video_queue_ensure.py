"""Addressed queue initialization through the direct repository.

Existing queue rows lock first, then canonical inventory identities, and the
complete captured channel scope fences the provisional writes before COMMIT.
The initializer owns Python cadence policy. Shared channel and typed-policy
projections preserve native source facts; intrinsic integrity guards remain
authoritative. No retained ensure RPC, second transaction or automatic retry is
introduced.
"""
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from .video_queue_initialization import ensure_video_collection_state_in_transaction

_INPUT="""WITH args AS MATERIALIZED(SELECT %s::text[] AS ids,%s::jsonb AS scopes)
 SELECT args.*,cardinality(ids) AS count FROM args"""
_LOCK_QUEUE="""SELECT state.video_id FROM public.video_collection_state state
 WHERE state.video_id=ANY(%s::text[]) ORDER BY state.video_id FOR UPDATE OF state"""
_LOCK_INVENTORY="""SELECT inventory.video_id FROM public.video_inventory inventory
 WHERE inventory.video_id=ANY(%s::text[]) ORDER BY inventory.video_id FOR SHARE OF inventory"""
_IDENTITY="""SELECT NOT EXISTS(
 (SELECT DISTINCT inventory.channel_id FROM public.video_inventory inventory
  WHERE inventory.video_id=ANY(%(ids)s::text[])
  EXCEPT SELECT scope->>'channel_id' FROM jsonb_array_elements(%(scopes)s::jsonb) scopes(scope))
 UNION ALL
 (SELECT scope->>'channel_id' FROM jsonb_array_elements(%(scopes)s::jsonb) scopes(scope)
  EXCEPT SELECT DISTINCT inventory.channel_id FROM public.video_inventory inventory
  WHERE inventory.video_id=ANY(%(ids)s::text[]))
 ) AS matches"""


@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('ensure_video_collection_state')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:
            yield tx


def ensure_video_collection_state(database,video_ids,channel_scopes,*,transaction=None):
    """Return affected rows after native identity checks and the final scope fence."""
    with _transaction(database,transaction) as tx:
        scopes=channel_scopes if channel_scopes is None or isinstance(channel_scopes,Jsonb) else Jsonb(channel_scopes)
        args=tx.fetch_one(_INPUT,(video_ids,scopes))
        if args['count'] is None or args['count']>1000 or not isinstance(args['scopes'],list):
            raise DatabaseServerError('channel_collection_scope_invalid',code='P0001')
        # These are WAIT locks. Final control SHARE is deliberately later than
        # all child locks and writes; an early SHARE would invert owner order.
        tx.fetch_all(_LOCK_QUEUE,(args['ids'],),max_rows=1000)
        tx.fetch_all(_LOCK_INVENTORY,(args['ids'],),max_rows=1000)
        if tx.fetch_one(_IDENTITY,dict(ids=args['ids'],scopes=Jsonb(args['scopes'])))['matches'] is not True:
            raise DatabaseServerError('channel_collection_scope_invalid',code='P0001')
        changed=ensure_video_collection_state_in_transaction(tx,args['ids'])
        # Do not move the scope-item/cardinality validation above the initializer:
        # the retained function performs those checks only at this final fence.
        assert_channel_scopes(tx,args['scopes'])
        return changed
