"""Global playlist preparation with unchanged lock/write ordering.

The pause stage freezes every ineligible playlist under lock before touching
scans; channel preparation keeps Long/Short/Stream SELECT then INSERT-or-UPDATE
order. This intentionally retains per-slot round trips; this module does not
use the page-effect batch protocol. Shared control-policy projections and
native integrity guards remain authoritative. YouTube work and retries never
occur here.
"""
from ..channel.channel_read_policy import (CHANNEL_CONTROL_SQL)
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scopes
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation

_PAUSE_LOCK = ("""select
        array_agg(
            target.channel_id
            order by target.channel_id, target.video_type
        ) AS channel_ids,
        array_agg(
            target.video_type
            order by target.channel_id, target.video_type
        ) AS video_types,
        array_agg(
            target.active_scan_id
            order by target.channel_id, target.video_type
        ) AS scan_ids
    from (
        select
            discovery.channel_id,
            discovery.video_type,
            discovery.active_scan_id
        from public.channel_playlist_state as discovery
        join public.channels as channel
          on channel.channel_id = discovery.channel_id
        where discovery.video_type in ('long', 'short', 'stream')
          and discovery.runtime_enabled
          and not coalesce((SELECT control.typed_runtime_eligible FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + """ control WHERE control.channel_id=channel.channel_id),false)
          and not exists(select 1 from public.channel_collection_state control join public.operator_jobs job on job.id=control.initial_job_id
            where control.channel_id=channel.channel_id and not control.is_hidden and control.owner_permission='accepted'
              and job.channel_id=channel.channel_id and job.accepted_epoch=control.control_epoch
              and job.initial_scope_version=1 and job.phase='inventory' and job.status in('blocked','error','suspended'))
        order by discovery.channel_id, discovery.video_type
        for update of discovery
    ) as target""")

_PAUSE_PROJECTION = """SELECT 1 AS matched
    from public.channel_video_inventory_state as inventory_state
    join unnest(%(channel_ids)s::text[], %(video_types)s::text[])
        as target(channel_id, video_type)
      on inventory_state.channel_id = target.channel_id
     and inventory_state.video_type = target.video_type
    order by inventory_state.channel_id, inventory_state.video_type"""

_PAUSE_SCANS = """update public.channel_video_inventory_scans as scan
    set
        status = 'abandoned',
        coverage = 'partial',
        completed_at = null,
        error_code = 'channel_not_eligible',
        error_at = now(),
        updated_at = now()
    where scan.scan_id = any(%(scan_ids)s::uuid[])
      and scan.status = 'running'"""

_PAUSE_PLAYLISTS = """update public.channel_playlist_state as discovery
    set
inventory_status = case
            when discovery.product_exposure_ready OR (discovery.inventory_state_origin='legacy_long_backfill' AND discovery.inventory_status='completed') then 'completed'
            when discovery.last_completed_scan_id is not null
                then 'completed'
            else 'pending'
        end,
        inventory_last_error_code = 'channel_not_eligible',
        inventory_last_error_at = now() ,
        inventory_updated_at = now(),
        runtime_enabled = false,
        status = 'paused',
        next_due_at = null,
        priority = 0,
        active_scan_id = null,
        resume_page_token = null,
        resume_started_at = null,
        scan_boundary_at = null,
        source_next_after = null,
        lease_channel_epoch = null,lease_token = null,
        lease_owner = null,
        lease_run_id = null,
        lease_expires_at = null,
        last_error_code = 'channel_not_eligible',
        updated_at = now()
    from unnest(%(channel_ids)s::text[], %(video_types)s::text[])
        as target(channel_id, video_type)
    where discovery.channel_id = target.channel_id
      and discovery.video_type = target.video_type
      and discovery.runtime_enabled"""

_INPUT = "SELECT %s::integer AS limit,%s::boolean AS enable"
_ELIGIBLE = ("""coalesce((SELECT projection.typed_runtime_eligible
 FROM """ + "(" + CHANNEL_CONTROL_SQL + ")" + ' projection WHERE projection.channel_id=c.channel_id),false)')
_CANDIDATES = """SELECT c.channel_id,control.control_epoch FROM public.channels c
 JOIN public.channel_collection_state control USING(channel_id)
 WHERE """ + _ELIGIBLE + """ AND
 (EXISTS(SELECT 1 FROM (VALUES('long'),('short'),('stream')) f(video_type)
   WHERE NOT EXISTS(SELECT 1 FROM public.channel_playlist_state s
    WHERE s.channel_id=c.channel_id AND s.video_type=f.video_type))
  OR (%s::boolean AND EXISTS(SELECT 1 FROM public.channel_playlist_state s
   WHERE s.channel_id=c.channel_id AND s.video_type IN('long','short','stream')
    AND (NOT s.runtime_enabled OR s.status='paused'))))
 ORDER BY c.channel_id LIMIT %s::integer"""
_SLOT = """SELECT active_scan_id,runtime_enabled,status FROM public.channel_playlist_state
 WHERE channel_id=%s::text AND video_type=%s::text FOR UPDATE"""
_INSERT = """INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,inventory_status,
 product_exposure_ready,runtime_enabled,status,next_due_at)
 VALUES(%s::text,%s::text,%s::text||substr(%s::text,3),'pending',false,%s::boolean,
 CASE WHEN %s::boolean THEN 'ready' ELSE 'paused' END,CASE WHEN %s::boolean THEN now() END)
 ON CONFLICT(channel_id,video_type) DO NOTHING"""
_ENABLE = """UPDATE public.channel_playlist_state d SET runtime_enabled=true,status='ready',
 next_due_at=least(coalesce(d.next_due_at,now()),now()),lease_channel_epoch=NULL,
 lease_token=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,updated_at=now()
 WHERE d.channel_id=%s::text AND d.video_type=%s::text"""
_REMAINING_PREPARE = """SELECT count(*) AS value FROM public.channels c
 CROSS JOIN(VALUES('long'),('short'),('stream')) f(video_type)
 WHERE """ + _ELIGIBLE + """ AND NOT EXISTS(SELECT 1 FROM public.channel_playlist_state s
 WHERE s.channel_id=c.channel_id AND s.video_type=f.video_type)"""
_REMAINING_ENABLE = """SELECT count(*) AS value FROM public.channels c
 JOIN public.channel_playlist_state s USING(channel_id)
 WHERE s.video_type IN('long','short','stream') AND """ + _ELIGIBLE + """
 AND (NOT s.runtime_enabled OR s.status='paused')"""
_TYPES=(('long','UULF'),('short','UUSH'),('stream','UULV'))

@contextmanager
def _transaction(database,transaction,name):
 if transaction is not None:
  with transaction.borrow():yield transaction
 else:
  spec=get_operation(name)
  with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
   lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
   error_codes=spec.error_codes)) as owned:yield owned

def pause_ineligible_typed_inventory_runtime(database,*,transaction=None):
 with _transaction(database,transaction,'pause_ineligible_typed_inventory_runtime') as tx:
  selected=tx.fetch_one(_PAUSE_LOCK)
  if not selected['channel_ids']:return 0
  # The retained physical channel cap is 10,000; unique types cap this complete
  # projection at 30,000 rows. Never truncate it to the preparation page size.
  tx.fetch_all(_PAUSE_PROJECTION,selected,max_rows=30000)
  tx.execute(_PAUSE_SCANS,selected)
  return tx.execute(_PAUSE_PLAYLISTS,selected)

def prepare_typed_inventory_runtime(database,limit=500,enable=False,*,transaction=None):
 with _transaction(database,transaction,'prepare_typed_inventory_runtime') as tx:
  args=tx.fetch_one(_INPUT,(limit,enable))
  limit=args['limit']
  enable=args['enable']
  if limit is None or limit<1 or limit>1000:
   raise DatabaseServerError('typed_inventory_prepare_limit_invalid',code='P0001')
  if enable is None:raise DatabaseServerError('typed_inventory_prepare_enable_required',code='P0001')
  pause_ineligible_typed_inventory_runtime(None,transaction=tx)
  channels=tx.fetch_all(_CANDIDATES,(enable,limit),max_rows=limit)
  inserted=enabled=0
  for channel in channels:
   channel_id=channel['channel_id']
   for kind,prefix in _TYPES:
    row=tx.fetch_one(_SLOT,(channel_id,kind))
    if row is None:
     inserted+=tx.execute(_INSERT,(channel_id,kind,prefix,channel_id,enable,enable,enable))
    elif enable and row['active_scan_id'] is None and (row['runtime_enabled'] is False or row['status']=='paused'):
     enabled+=tx.execute(_ENABLE,(channel_id,kind))
  remaining_prepare=tx.fetch_one(_REMAINING_PREPARE)['value']
  remaining_enable=tx.fetch_one(_REMAINING_ENABLE)['value']
  assert_channel_scopes(tx,channels)
  return dict(selected_channels=len(channels),inventory_states_inserted=inserted,
   discovery_states_inserted=inserted,discovery_states_enabled=enabled,
   remaining_to_prepare=remaining_prepare,remaining_to_enable=remaining_enable,enable_requested=enable)
