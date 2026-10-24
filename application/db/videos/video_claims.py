"""Runtime addressed video claims: one transaction, no replay or provider work.

Queue UPDATE(attempt_count,last_claim_run_id,last_attempt_at) is reviewed and
tested. Existing queue/inventory/control grants supply the ordered child locks.
No new inventory grant is required. The final captured-scope fence runs after
all child DML, including zero-claim results.

Shared channel and typed-policy projections retain native predicate placement;
PostgreSQL supplies UUIDs and intrinsic integrity guards. Automatic due-window
claim cursors and ensure/reconcile remain separate protocols.
"""
from __future__ import annotations

from ..channel.channel_read_policy import (CHANNEL_STATE_MODE_SQL, CHANNEL_STATE_TYPED_SQL)
from contextlib import contextmanager
from .video_initial_policy import VIDEO_HANDOFF_STATE_SQL, VIDEO_INITIAL_PENDING_STATE_SQL
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError, Operation

_PREPARE = """
WITH args AS MATERIALIZED (
 SELECT %s::text[] AS video_ids, %s::text AS worker_id, %s::uuid AS run_id,
        %s::jsonb AS scopes, %s::integer AS lease_seconds
)
SELECT args.*, cardinality(video_ids) AS id_count, btrim(worker_id) AS worker FROM args
"""
_LOCK_QUEUE = """SELECT state.video_id FROM public.video_collection_state state
WHERE state.video_id=ANY(%s::text[]) ORDER BY state.video_id FOR UPDATE OF state"""
_LOCK_INVENTORY = """SELECT inventory.video_id FROM public.video_inventory inventory
WHERE inventory.video_id=ANY(%s::text[]) ORDER BY inventory.video_id FOR SHARE OF inventory"""
_IDENTITY_MATCH = """
SELECT NOT EXISTS (
 (SELECT DISTINCT inventory.channel_id FROM public.video_inventory inventory
  WHERE inventory.video_id=ANY(%(video_ids)s::text[])
  EXCEPT SELECT scope->>'channel_id' FROM jsonb_array_elements(%(scopes)s::jsonb) scopes(scope))
 UNION ALL
 (SELECT scope->>'channel_id' FROM jsonb_array_elements(%(scopes)s::jsonb) scopes(scope)
  EXCEPT SELECT DISTINCT inventory.channel_id FROM public.video_inventory inventory
  WHERE inventory.video_id=ANY(%(video_ids)s::text[]))
) AS matches
"""

# A finite two-entry statement family. No caller text is interpolated into SQL.
_UPDATE = """
UPDATE public.video_collection_state AS state
SET lease_token=pg_catalog.gen_random_uuid(),lease_channel_epoch=candidates.channel_epoch,
    lease_owner=btrim(%(worker_id)s::text),lease_started_at=now(),
    lease_expires_at=now()+make_interval(secs=>%(lease_seconds)s::integer),
    attempt_count=state.attempt_count+1,last_claim_run_id=%(run_id)s::uuid,
    last_attempt_at=now(),updated_at=now()
FROM (
 SELECT state.video_id,control.control_epoch AS channel_epoch
 FROM public.video_collection_state state
 JOIN public.channel_collection_state control ON control.channel_id=state.channel_id
 WHERE control.owner_permission='accepted' AND NOT control.is_hidden
   AND EXISTS (SELECT 1 FROM jsonb_array_elements(%(scopes)s::jsonb) wanted(scope)
               WHERE wanted.scope->>'channel_id'=state.channel_id
                 AND wanted.scope->'control_epoch'=to_jsonb(control.control_epoch))
   AND {eligible} AND state.video_id=ANY(%(video_ids)s::text[])
   AND state.video_type='long' AND {due}
   AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
   AND state.last_claim_run_id IS DISTINCT FROM %(run_id)s::uuid
 ORDER BY array_position(%(video_ids)s::text[],state.video_id)
 FOR UPDATE OF state SKIP LOCKED
) candidates
WHERE state.video_id=candidates.video_id
RETURNING array_position(%(video_ids)s::text[],state.video_id) AS position,
 jsonb_build_object('video_id',state.video_id,'channel_id',state.channel_id,
  'collection_mode',{mode},'lease_token',state.lease_token,
  'lease_expires_at',state.lease_expires_at,'priority',0::smallint,
  'next_due_at',{next_due}) AS value
"""
_CLAIM_DUE = _UPDATE.format(
    eligible="(" + CHANNEL_STATE_MODE_SQL + "='active' OR " + VIDEO_HANDOFF_STATE_SQL + ")",
    due="state.paused_reason IS NULL AND state.next_due_at<=now()",
    mode="CASE WHEN state.hydration_needed THEN 'hydrate' WHEN state.metadata_due_at IS NOT NULL AND state.metadata_due_at<=now() THEN 'metadata' ELSE 'counters' END",
    next_due='state.next_due_at')
_CLAIM_METADATA = _UPDATE.format(
    eligible=CHANNEL_STATE_TYPED_SQL,
    due="state.paused_reason IS DISTINCT FROM 'channel_hidden' AND NOT " + VIDEO_INITIAL_PENDING_STATE_SQL,
    mode="'metadata'::text",next_due='coalesce(state.next_due_at,now())')


def _fail(message='database_operation_failed'):
    raise DatabaseServerError(message,code='P0001')


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation(name)
        with database.transaction(Operation(name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
                error_codes=spec.error_codes)) as owned:
            yield owned


def _claim(database,video_ids,worker_id,run_id,channel_scopes,lease_seconds,*,metadata,transaction):
    name='claim_video_metadata_collection_ids' if metadata else 'claim_video_collection_ids'
    with _transaction(database,transaction,name) as tx:
        scopes=channel_scopes if channel_scopes is None or isinstance(channel_scopes,Jsonb) else Jsonb(channel_scopes)
        args=tx.fetch_one(_PREPARE,(video_ids,worker_id,run_id,scopes,lease_seconds))
        if args['id_count'] is None or args['id_count']>1000:_fail()
        if not isinstance(args['scopes'],list) or len(args['scopes'])>1000:
            _fail('channel_collection_scope_invalid')
        values={key:args[key] for key in ('video_ids','worker_id','run_id','lease_seconds')}
        values['scopes']=Jsonb(args['scopes'])
        # These initial locks intentionally WAIT, exactly as the retained SQL.
        # The later candidate SKIP LOCKED does not weaken the identity prelock.
        tx.fetch_all(_LOCK_QUEUE,(args['video_ids'],),max_rows=1000)
        tx.fetch_all(_LOCK_INVENTORY,(args['video_ids'],),max_rows=1000)
        if tx.fetch_one(_IDENTITY_MATCH,values)['matches'] is not True:
            _fail('channel_collection_scope_invalid')
        if args['id_count']==0:
            assert_channel_scopes(tx,args['scopes'])
            return []
        if not args['worker'] or args['run_id'] is None or args['lease_seconds'] is None or not 30<=args['lease_seconds']<=3600:
            _fail()
        rows=tx.returning_all(_CLAIM_METADATA if metadata else _CLAIM_DUE,values,max_rows=1000)
        # Native array_position ranks preserve caller order, including duplicate
        # input IDs. Only the queue's one row per identity can be returned.
        rows.sort(key=lambda row:row['position'])
        result=[row['value'] for row in rows]
        assert_channel_scopes(tx,args['scopes'])
        return result


def claim_video_collection_ids(database,video_ids,worker_id,run_id,channel_scopes,lease_seconds=300,*,transaction=None):
    return _claim(database,video_ids,worker_id,run_id,channel_scopes,lease_seconds,metadata=False,transaction=transaction)


def claim_video_metadata_collection_ids(database,video_ids,worker_id,run_id,channel_scopes,lease_seconds=300,*,transaction=None):
    return _claim(database,video_ids,worker_id,run_id,channel_scopes,lease_seconds,metadata=True,transaction=transaction)
