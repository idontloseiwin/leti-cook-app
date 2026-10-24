"""Addressed inventory priority used by the public repository.

Native bounded array/scope validation precedes one child UPDATE. Each live
lease stays owned; all other claim fields clear as in the retained function.
Only then does the captured/expected channel fence lock control rows. No retry.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_SQL)
from contextlib import contextmanager
from psycopg.types.json import Jsonb
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation

_INPUT="""WITH args AS MATERIALIZED(SELECT %s::text[] AS ids,%s::jsonb AS scopes)
 SELECT coalesce(cardinality(ids),0)<1 OR cardinality(ids)>500 AS count_invalid,
 EXISTS(SELECT 1 FROM unnest(ids) requested(id) WHERE nullif(btrim(id),'') IS NULL) AS id_invalid,
 scopes IS NOT NULL AS has_scopes FROM args"""
_SCOPES="""WITH input AS(SELECT %(scopes)s::jsonb AS scopes,%(channel_ids)s::text[] AS ids)
 SELECT CASE WHEN jsonb_typeof(scopes)<>'array' THEN true WHEN jsonb_array_length(scopes)>1000 THEN true ELSE
 EXISTS(SELECT 1 FROM jsonb_array_elements(scopes) e WHERE CASE WHEN jsonb_typeof(e)<>'object' THEN true ELSE
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(e) key) IS DISTINCT FROM ARRAY['channel_id','control_epoch']::text[]
 OR jsonb_typeof(e->'channel_id') IS DISTINCT FROM 'string' OR nullif(btrim(e->>'channel_id'),'') IS NULL
 OR e->>'channel_id'<>btrim(e->>'channel_id') OR length(e->>'channel_id')>128
 OR jsonb_typeof(e->'control_epoch') IS DISTINCT FROM 'number' OR (e->>'control_epoch')!~'^[1-9][0-9]*$'
 OR NOT pg_input_is_valid(e->>'control_epoch','bigint') END)
 OR (SELECT count(DISTINCT e->>'channel_id') FROM jsonb_array_elements(scopes) e)<>jsonb_array_length(scopes)
 OR (SELECT array_agg(e->>'channel_id' ORDER BY e->>'channel_id') FROM jsonb_array_elements(scopes) e)
 IS DISTINCT FROM (SELECT array_agg(channel_id ORDER BY channel_id) FROM
  (SELECT DISTINCT btrim(id) AS channel_id FROM unnest(ids) id WHERE nullif(btrim(id),'') IS NOT NULL) requested)
 END AS invalid FROM input"""
_UPDATE=("""update public.channel_playlist_state as discovery
    set
        status = case
            when discovery.status = 'leased'
                 and discovery.lease_expires_at > now()
                then 'leased'
            else 'ready'
        end,
        next_due_at = now(),
        priority = greatest(discovery.priority, 100),
        source_next_after = now(),
        lease_channel_epoch = case
            when discovery.status = 'leased' and discovery.lease_expires_at > now()
                then discovery.lease_channel_epoch else null end,
        lease_token = case
            when discovery.status = 'leased'
                 and discovery.lease_expires_at > now()
                then discovery.lease_token
            else null
        end,
        lease_owner = case
            when discovery.status = 'leased'
                 and discovery.lease_expires_at > now()
                then discovery.lease_owner
            else null
        end,
        lease_run_id = case
            when discovery.status = 'leased'
                 and discovery.lease_expires_at > now()
                then discovery.lease_run_id
            else null
        end,
        lease_expires_at = case
            when discovery.status = 'leased'
                 and discovery.lease_expires_at > now()
                then discovery.lease_expires_at
            else null
        end,
        updated_at = now()
    from public.channels as channel
    JOIN public.channel_collection_state control ON control.channel_id=channel.channel_id
    join public.channel_video_inventory_state as inventory_state
      on inventory_state.channel_id = channel.channel_id
     and inventory_state.video_type in ('long', 'short', 'stream')
    where discovery.channel_id = channel.channel_id
      and discovery.video_type = inventory_state.video_type
      and discovery.channel_id in (
          select distinct btrim(requested.channel_id)
          from unnest(%(channel_ids)s::text[]) as requested(channel_id)
      )
      and (%(scopes)s::jsonb IS NULL OR EXISTS (
          SELECT 1 FROM jsonb_array_elements(%(scopes)s::jsonb) scope
          WHERE scope->>'channel_id'=discovery.channel_id
            AND (scope->>'control_epoch')::bigint=control.control_epoch))
      and discovery.runtime_enabled
      and CASE WHEN discovery.last_error_code IN ('youtube_daily_quota_exceeded_error','youtube_quota_exhausted') THEN
 now() IS NOT NULL AND coalesce(discovery.next_due_at,'infinity'::timestamptz)<=now()
 AND coalesce(discovery.source_next_after,'infinity'::timestamptz)<=now() ELSE true END
      and coalesce((SELECT projection.typed_runtime_eligible
 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ projection WHERE projection.channel_id=channel.channel_id),false)
    RETURNING discovery.channel_id,control.control_epoch""")
_SCOPES_VALUE="SELECT %s::jsonb AS value"

@contextmanager
def _transaction(database,transaction):
 if transaction is not None:
  with transaction.borrow():yield transaction
 else:
  spec=get_operation('prioritize_typed_inventory_channels')
  with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
   lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
   error_codes=spec.error_codes)) as tx:yield tx

def prioritize_typed_inventory_channels(database,channel_ids,expected_channel_scopes=None,*,transaction=None):
 with _transaction(database,transaction) as tx:
  scopes=expected_channel_scopes
  encoded=scopes if scopes is None or isinstance(scopes,Jsonb) else Jsonb(scopes)
  args=dict(channel_ids=channel_ids,scopes=encoded)
  facts=tx.fetch_one(_INPUT,(channel_ids,encoded))
  if facts['count_invalid'] is True:raise DatabaseServerError('typed_inventory_priority_channel_ids_invalid',code='P0001')
  if facts['id_invalid'] is True:raise DatabaseServerError('typed_inventory_priority_channel_id_invalid',code='P0001')
  if facts['has_scopes']:
   if tx.fetch_one(_SCOPES,args)['invalid'] is True:raise DatabaseServerError('channel_collection_scope_invalid',code='P0001')
   # Native JSONB numeric spelling and bigint validation above own the envelope.
   scopes=tx.fetch_one(_SCOPES_VALUE,(encoded,))['value']
  changed=tx.returning_all(_UPDATE,args,max_rows=1500)
  if not facts['has_scopes']:
   scopes=list({(row['channel_id'],row['control_epoch']):row for row in changed}.values())
  assert_channel_scopes(tx,scopes)
  return len(changed)
