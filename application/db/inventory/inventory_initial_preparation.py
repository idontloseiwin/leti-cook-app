"""Prepare three initial playlists under one captured channel generation.

The public repository uses this protocol. The channel row is locked before
eligibility is observed; three ordered INSERTs keep existing playlist rows and
their active leases intact. Final control fence belongs to this transaction.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_FACTS_SQL, channel_eligible_row)
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation

_INPUT="""SELECT btrim(%s::text) AS channel_id,
 nullif(btrim(%s::text),'') IS NULL OR length(%s::text)>128 AS invalid"""
_LOCK="SELECT channel_id FROM public.channels WHERE channel_id=%s::text FOR UPDATE"
_SCOPE=('SELECT control.control_epoch,projection.policy_facts FROM public.channel_collection_state control LEFT JOIN (' + CHANNEL_CONTROL_FACTS_SQL + ') projection USING(channel_id) WHERE control.channel_id=%s::text')
_INSERT="""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,inventory_status,
 product_exposure_ready,runtime_enabled,status,next_due_at)
 VALUES(%s::text,%s::text,%s::text||substr(%s::text,3),'pending',false,true,'ready',now())
 ON CONFLICT(channel_id,video_type) DO NOTHING"""
_TYPES=(('long','UULF'),('short','UUSH'),('stream','UULV'))

@contextmanager
def _transaction(database,transaction):
 if transaction is not None:
  with transaction.borrow():yield transaction
 else:
  spec=get_operation('prepare_channel_initial_typed_inventory')
  with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
    lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:yield tx

def _result(channel,status,inserted=0):
 return dict(schema_version=1,status=status,channel_id=channel,prepared=inserted*2,
   inventory_states_inserted=inserted,discovery_states_inserted=inserted)

def prepare_channel_initial_typed_inventory(database,channel_id,*,transaction=None):
 with _transaction(database,transaction) as tx:
  args=tx.fetch_one(_INPUT,(channel_id,channel_id,channel_id))
  if args['invalid'] is True:raise DatabaseServerError('initial_typed_channel_id_invalid',code='P0001')
  channel=args['channel_id']
  held=tx.fetch_one(_LOCK,(channel,))
  if held is None:return _result(channel,'not_found')
  scope=channel_eligible_row(tx.fetch_one(_SCOPE,(channel,)))
  if scope is None or scope['eligible'] is not True:return _result(channel,'ineligible')
  inserted=0
  for kind,prefix in _TYPES:inserted+=tx.execute(_INSERT,(channel,kind,prefix,channel))
  assert_channel_scope(tx,channel,scope['control_epoch'])
  return _result(channel,'ok',inserted)
