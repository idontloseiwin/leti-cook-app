"""Initial typed hydration read used by the public repository.

One aggregate statement preserves the retained STABLE function snapshot and
native transaction clock. Counts, evidence joins and wire timestamp formatting
stay in PostgreSQL; eligibility uses the existing exact control projection.
No per-video transfer or query. Current read views remain dependencies.
"""
from contextlib import contextmanager
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseServerError,Operation
from ..base.sql_values import application_value
from hot_storage import register_read_deadline, utc_instant, utc_now, HotSourceReadExpired

_INPUT="SELECT nullif(btrim(%s::text),'') IS NULL OR length(%s::text)>128 AS invalid"
_READ=("""WITH args AS MATERIALIZED(SELECT btrim(%s::text) AS channel_id),
 scan_sources AS MATERIALIZED(SELECT * FROM readable.channel_scan_sources
 WHERE channel_id=(SELECT channel_id FROM args)),
 automatic_decisions AS MATERIALIZED(
  SELECT source.video_id,resolution.source_expires_at
  FROM readable.video_inventory AS source
  CROSS JOIN LATERAL (
   SELECT proof.canonical_channel_id,proof.canonical_video_type,proof.resolution_status,
    proof.automatic_valid,proof.source_expires_at
   FROM readable.video_inventory_type_resolution AS proof
   WHERE proof.video_id=source.video_id OFFSET 0
  ) AS resolution
  WHERE source.channel_id=(SELECT channel_id FROM args)
   AND source.video_type IN ('long','short')
   AND resolution.automatic_valid AND resolution.resolution_status='resolved'
   AND resolution.canonical_channel_id=source.channel_id
   AND resolution.canonical_video_type=source.video_type
 ),
 source_deadline AS (SELECT min(deadline) AS value FROM (
 SELECT min(source_expires_at) AS deadline FROM readable.video_inventory WHERE channel_id=(SELECT channel_id FROM args) AND video_type IN('short','stream')
 UNION ALL SELECT min(source_expires_at) FROM readable.video_inventory_memberships
 WHERE channel_id=(SELECT channel_id FROM args) OR video_id IN(
  SELECT video_id FROM readable.retained_video_identities WHERE channel_id=(SELECT channel_id FROM args))
 UNION ALL SELECT min(source_expires_at) FROM readable.videos WHERE video_id IN(
  SELECT video_id FROM readable.video_inventory WHERE channel_id=(SELECT channel_id FROM args) AND video_type IN('short','stream'))
 UNION ALL SELECT min(source_expires_at) FROM readable.channel_playlist_state WHERE channel_id=(SELECT channel_id FROM args)
 UNION ALL SELECT min(source_expires_at) FROM scan_sources
 UNION ALL SELECT min(source_expires_at) FROM automatic_decisions
 UNION ALL SELECT min(source_expires_at) FROM readable.open_video_availability
 WHERE channel_id=(SELECT channel_id FROM args) AND isfinite(last_seen_at)
 AND last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 UNION ALL SELECT min(source_expires_at) FROM readable.video_hydration_proof
 WHERE channel_id=(SELECT channel_id FROM args)
 ) sources),
 header AS MATERIALIZED(SELECT
 EXISTS(SELECT 1 FROM readable.channel_user_work channel WHERE channel.channel_id=args.channel_id) AS present,
 coalesce((SELECT control.typed_runtime_eligible
 FROM readable.channel_control control WHERE control.channel_id=args.channel_id),false) AS eligible
 FROM args)
 SELECT jsonb_build_object('source_expires_at',(SELECT value FROM source_deadline),'schema_version',1,'status',CASE WHEN header.present THEN 'ok' ELSE 'not_found' END,
 'channel_id',args.channel_id,'channel_eligible',header.eligible,
 'formats',(with formats as materialized (
        select format.video_type,
            coalesce(inventory_state.inventory_status, 'not_started') as inventory_status,
            coalesce(discovery.runtime_enabled, false) as runtime_enabled,
            coalesce((SELECT eligible FROM header)
                and inventory_state.inventory_status in ('completed', 'running')
                and completed.scan_id is not null and full_generation.present,
                false) as inventory_ready
        from (values ('short'::text), ('stream'::text)) as format(video_type)
        left join readable.channel_video_inventory_state as inventory_state
            on inventory_state.channel_id = (SELECT channel_id FROM args)
            and inventory_state.video_type = format.video_type
        left join readable.channel_playlist_state as discovery
            on discovery.channel_id = inventory_state.channel_id
            and discovery.video_type = inventory_state.video_type
        left join scan_sources as completed
            on completed.scan_id = inventory_state.last_completed_scan_id
            and completed.channel_id = inventory_state.channel_id
            and completed.video_type = inventory_state.video_type
            and completed.playlist_id = inventory_state.playlist_id
            and completed.status = 'completed' and completed.coverage = 'complete'
            and completed.completed_at <= now()
        left join lateral (
            -- A head discovery alone is not full initial inventory. Preserve
            -- completed full-generation coverage after later head scans replace
            -- last_completed_scan_id. That pointer is not a full-scan history.
            select true as present
            from scan_sources as scan
            where scan.channel_id = (SELECT channel_id FROM args)
                and scan.video_type = format.video_type
                and scan.playlist_id = inventory_state.playlist_id
                and scan.status = 'completed' and scan.coverage = 'complete'
                and scan.completed_at <= now()
                and (scan.scan_kind in ('initial_inventory', 'full_reconciliation')
                    )
            limit 1
        ) as full_generation on true
    ), inventory as materialized (
        select source.video_id, source.channel_id, source.video_type
        from readable.video_inventory as source
        where source.channel_id = (SELECT channel_id FROM args)
            and source.skeleton_since is null
            and source.video_type in ('short', 'stream')
    ), manual_decisions as materialized (
        -- Match installed manual_valid provenance without expanding the global
        -- resolution view. Include Long decisions so rejected typed evidence
        -- cannot reappear below as unresolved initial work for another format.
        select source.video_id
        from readable.video_inventory as source
        join readable.video_type_user_work as review on review.video_id = source.video_id
        where source.channel_id = (SELECT channel_id FROM args)
            and review.decision_type = source.video_type
            and review.channel_id = source.channel_id
            and review.decision_unblocked
            and not exists (
                select 1 from readable.video_inventory_memberships as foreign_evidence
                where foreign_evidence.video_id = source.video_id
                    and foreign_evidence.membership_status = 'present'
                    and foreign_evidence.channel_id <> source.channel_id
            )
    ), unresolved_membership as materialized (
        -- Insert-only inventory can retain NULL/Long/another canonical identity
        -- for a present typed member. That member is pending in its observed
        -- format, not evidence that this format is empty. The membership PK
        -- gives one row per video/channel/format. This is disjoint from the
        -- canonical identities counted above for that same channel/format.
        select observed.video_type, count(*) as unresolved_count
        from readable.video_inventory_memberships as observed
        join lateral (
            select source.video_id, source.channel_id, source.video_type, source.skeleton_since
            from readable.video_inventory as source
            where source.video_id = observed.video_id offset 0
        ) as canonical on true
        where observed.channel_id = (SELECT channel_id FROM args)
            and canonical.skeleton_since is null
            and observed.video_type in ('short', 'stream')
            and observed.membership_status = 'present'
            and (canonical.channel_id is distinct from observed.channel_id
                or canonical.video_type is distinct from observed.video_type)
            and not exists (
                select 1 from manual_decisions as decision
                where decision.video_id = canonical.video_id
            )
            and not exists (
                select 1 from automatic_decisions as decision
                where decision.video_id = canonical.video_id
            )
        group by observed.video_type
    ), membership as materialized (
        select inventory.video_id,
            bool_or(evidence.channel_id = inventory.channel_id
                and evidence.video_type = inventory.video_type) as exact_present,
            bool_or(evidence.channel_id <> inventory.channel_id
                or evidence.video_type <> inventory.video_type) as conflicting_present
        from inventory
        join readable.video_inventory_memberships as evidence
            on evidence.video_id = inventory.video_id
            and evidence.membership_status = 'present'
        group by inventory.video_id
    ), evidence as (
        select inventory.video_type, state.video_id as queue_video_id,
            coalesce(formats.inventory_ready and formats.runtime_enabled
                and (manual_decisions.video_id is not null
                    or automatic_decisions.video_id is not null
                    or (membership.exact_present and not membership.conflicting_present))
                and (state.video_id is null or state.identity_matches), false) as identity_valid,
            coalesce(state.profile_observed, false) as profile_observed,
            coalesce(state.unavailable_observed, false) as unavailable_observed,
            coalesce(state.completion_checked, false) as completion_checked,
            coalesce(state.has_failure, false) as has_failure
        from inventory
        join formats on formats.video_type = inventory.video_type
        left join membership on membership.video_id = inventory.video_id
        left join manual_decisions on manual_decisions.video_id = inventory.video_id
        left join automatic_decisions on automatic_decisions.video_id = inventory.video_id
        left join lateral (
            -- Keep the protected proof parameterized by this admitted ID. Do
            -- not add a channel/type filter: a mismatched queue is evidence,
            -- not a missing queue. OFFSET 0 retains all rows and NULL behavior.
            select proof.video_id, proof.identity_matches, proof.profile_observed,
                proof.unavailable_observed, proof.completion_checked, proof.has_failure
            from readable.video_hydration_proof as proof
            where proof.video_id = inventory.video_id offset 0
        ) as state on true
    ), outcomes as (
        select *, identity_valid and profile_observed as hydrated,
            identity_valid and unavailable_observed as unavailable,
            identity_valid and completion_checked as awaiting_completion
        from evidence
    ), counts as (
        select video_type, count(*) as known_count,
            count(*) filter (where hydrated) as hydrated_count,
            count(*) filter (where unavailable) as unavailable_count,
            count(*) filter (where awaiting_completion) as awaiting_completion_count,
            count(*) filter (where not (hydrated or unavailable or awaiting_completion)) as pending_count,
            count(*) filter (where queue_video_id is null) as missing_queue_count,
            count(*) filter (where not identity_valid) as identity_unresolved_count,
            count(*) filter (where has_failure
                and not (hydrated or unavailable or awaiting_completion)) as failed_count
        from outcomes group by video_type
    )
    select jsonb_object_agg(formats.video_type, jsonb_build_object(
        'inventory_status', formats.inventory_status,
        'inventory_ready', formats.inventory_ready,
        'runtime_enabled', formats.runtime_enabled,
        'known_count', coalesce(counts.known_count, 0)
            + coalesce(unresolved_membership.unresolved_count, 0),
        'hydrated_count', coalesce(counts.hydrated_count, 0),
        'unavailable_count', coalesce(counts.unavailable_count, 0),
        'awaiting_completion_count', coalesce(counts.awaiting_completion_count, 0),
        'pending_count', coalesce(counts.pending_count, 0)
            + coalesce(unresolved_membership.unresolved_count, 0),
        'missing_queue_count', coalesce(counts.missing_queue_count, 0),
        'identity_unresolved_count', coalesce(counts.identity_unresolved_count, 0)
            + coalesce(unresolved_membership.unresolved_count, 0),
        'failed_count', coalesce(counts.failed_count, 0)
    ))
    from formats left join counts on counts.video_type = formats.video_type
    left join unresolved_membership
        on unresolved_membership.video_type = formats.video_type),
 'inventory_formats',(SELECT jsonb_object_agg(types.video_type,jsonb_build_object(
      'inventory_status',coalesce(q.inventory_status,'not_started'),
      'runtime_enabled',coalesce(q.runtime_enabled,false),
      'product_exposure_ready',coalesce(q.product_exposure_ready,false),
      'active_scan_id',q.active_scan_id,'scan_status',active.status,'scan_kind',active.scan_kind,
      'resume_page_token',active.resume_page_token,'scan_boundary_at',active.scan_boundary_at,
      'token_history_verified',coalesce(active.token_history_verified,false),
      'last_completed_scan_id',q.last_completed_scan_id,
      'inventory_ready',coalesce((SELECT eligible FROM header) AND completed.scan_id IS NOT NULL
        AND completed.status='completed' AND completed.coverage='complete'
        AND completed.scan_kind IN('initial_inventory','full_reconciliation')
        AND isfinite(completed.source_checked_at) AND isfinite(completed.started_at)
        AND least(completed.source_checked_at,completed.started_at)>=now()-interval '696 hours'
        AND completed.completed_at<=now(),false)))
    FROM (VALUES('long'),('short'),('stream')) types(video_type)
    LEFT JOIN readable.channel_playlist_state q ON q.channel_id=(SELECT channel_id FROM args) AND q.video_type=types.video_type
    LEFT JOIN scan_sources active ON active.scan_id=q.active_scan_id
      AND active.channel_id=q.channel_id AND active.video_type=q.video_type
    LEFT JOIN scan_sources completed ON completed.scan_id=q.last_completed_scan_id
      AND completed.channel_id=q.channel_id AND completed.video_type=q.video_type AND completed.playlist_id=q.playlist_id)) AS value FROM args CROSS JOIN header""")

@contextmanager
def _transaction(database,transaction):
 if transaction is not None:
  with transaction.borrow():yield transaction
 else:
  spec=get_operation('get_channel_initial_typed_hydration')
  with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
   lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,
   error_codes=spec.error_codes,read_only=True)) as tx:yield tx

def get_channel_initial_typed_hydration(database,channel_id,*,transaction=None):
 with _transaction(database,transaction) as tx:
  if tx.fetch_one(_INPUT,(channel_id,channel_id))['invalid'] is True:
   raise DatabaseServerError('initial_typed_channel_id_invalid',code='P0001')
  # PostgreSQL otherwise compiles hundreds of expressions for this small
  # aggregate through security-barrier views. Native plans show compilation
  # dwarfing execution. Keep the setting local to this read, never the pool.
  previous_jit=tx.fetch_one("SELECT current_setting('jit') AS value")['value'] if transaction is not None else None
  tx.fetch_one("SELECT set_config('jit','off',true) AS value")
  result=application_value(tx.fetch_one(_READ,(channel_id,))['value'])
  if previous_jit is not None:
   tx.fetch_one("SELECT set_config('jit',%s,true) AS value",(previous_jit,))
  deadline=result.get('source_expires_at')
  if deadline is not None:
   expires_at=utc_instant(deadline)
   if expires_at<=utc_now():
    raise HotSourceReadExpired('Hydration source expired during read')
   register_read_deadline(expires_at)
  return result
