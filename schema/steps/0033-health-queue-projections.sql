-- Read-only health projections; no domain state or deadlines change.
CREATE OR REPLACE VIEW readable.channel_discovery_health WITH (security_barrier=true, security_invoker=false) AS
WITH channel_policy_facts AS (SELECT channel.channel_id,(control.channel_id IS NOT NULL AND channel.channel_id IS NOT NULL) AS control_present,
(control.is_hidden) AS hidden,
(coalesce(control.is_hidden,true)) AS pause_hidden,
(control.owner_permission='accepted') AS permission_accepted,
(control.owner_permission<>'accepted') AS permission_not_accepted,
(control.owner_permission='manual_required') AS permission_manual,
(control.initial_job_id IS NOT NULL) AS linked,
(job.id IS NULL) AS job_missing,
(job.channel_id IS DISTINCT FROM control.channel_id) AS job_channel_distinct,
(job.job_type IS DISTINCT FROM 'channel_initial_long_inventory') AS job_type_distinct,
(job.accepted_epoch IS NULL) AS epoch_null,
(job.error_count IS NULL) AS errors_null,
(job.status IS NULL) AS status_null,
(job.phase IS NULL) AS phase_null,
(job.initial_video_types IS NOT DISTINCT FROM ARRAY['long']::text[]) AS types_long,
(job.initial_video_types IS NOT DISTINCT FROM ARRAY['long','short','stream']::text[]) AS types_all,
(cardinality(job.initial_video_types)=3) AS type_count_three,
(job.initial_scope_version=1) AS scope_full,
(job.status='blocked') AS status_blocked,
(job.status='error') AS status_error,
(job.status='suspended') AS status_suspended,
(job.status='completed') AS status_completed,
(job.targets_frozen_at IS NULL) AS frozen_null,
(job.error_count=0) AS errors_zero,
(job.phase='verify') AS phase_verify,
(job.phase='initial_stats') AS phase_initial_stats,
(channel.initial_long_inventory_status='completed') AS initial_completed,
(channel.initial_long_inventory_status='skipped_no_long_playlist') AS initial_skipped
 FROM public.channels channel
 LEFT JOIN public.channel_collection_state control USING(channel_id)
 LEFT JOIN public.operator_jobs job ON job.id=control.initial_job_id),
 channel_policy_modes AS (SELECT facts.*,CASE
 WHEN facts.control_present IS FALSE THEN 'operator_hold'::text
 WHEN facts.hidden IS TRUE THEN 'hidden'::text
 WHEN facts.permission_not_accepted IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.job_missing IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.job_channel_distinct IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.job_type_distinct IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.epoch_null IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.errors_null IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.status_null IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.phase_null IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.types_long IS FALSE AND facts.types_all IS FALSE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.scope_full IS TRUE AND facts.status_blocked IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.scope_full IS TRUE AND facts.status_error IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.scope_full IS TRUE AND facts.status_suspended IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.scope_full IS TRUE AND facts.status_completed IS TRUE AND facts.frozen_null IS TRUE THEN 'operator_hold'::text
 WHEN facts.linked IS TRUE AND facts.types_long IS TRUE AND facts.status_completed IS TRUE AND facts.initial_skipped IS TRUE THEN 'active'::text
 WHEN facts.linked IS TRUE AND facts.status_completed IS TRUE AND facts.errors_zero IS TRUE AND facts.type_count_three IS TRUE AND facts.phase_verify IS TRUE THEN 'active'::text
 WHEN facts.linked IS TRUE AND facts.status_completed IS TRUE AND facts.errors_zero IS TRUE AND facts.types_long IS TRUE AND facts.phase_verify IS TRUE THEN 'active'::text
 WHEN facts.linked IS TRUE AND facts.status_completed IS TRUE AND facts.errors_zero IS TRUE AND facts.types_long IS TRUE AND facts.phase_initial_stats IS TRUE THEN 'active'::text
 WHEN facts.linked IS TRUE THEN 'initial_inventory'::text
 WHEN facts.initial_completed IS TRUE THEN 'active'::text
 WHEN facts.initial_skipped IS TRUE THEN 'active'::text
 ELSE 'initial_inventory'::text END AS mode
 FROM channel_policy_facts facts),
 channel_policy_flags AS (SELECT modes.*,mode='active' AS mode_active,
 mode='metadata_only' AS mode_metadata,mode='initial_inventory' AS mode_initial
 FROM channel_policy_modes modes),
 channel_policy AS (SELECT facts.channel_id,facts.mode,CASE
 WHEN facts.control_present IS TRUE AND facts.hidden IS FALSE AND facts.permission_accepted IS TRUE AND facts.mode_active IS TRUE THEN true
 WHEN facts.control_present IS TRUE AND facts.hidden IS FALSE AND facts.permission_accepted IS TRUE AND facts.mode_metadata IS TRUE THEN true
 WHEN facts.control_present IS TRUE AND facts.hidden IS FALSE AND facts.permission_accepted IS TRUE AND facts.mode_initial IS TRUE THEN true
 ELSE false END AS typed_runtime_eligible,
 CASE
 WHEN facts.pause_hidden IS TRUE THEN 'channel_hidden'::text
 WHEN facts.mode_active IS TRUE THEN NULL::text
 WHEN facts.mode_metadata IS TRUE THEN 'channel_no_long_playlist'::text
 WHEN facts.mode_initial IS TRUE THEN 'channel_inventory_not_ready'::text
 WHEN facts.permission_manual IS TRUE THEN 'channel_inventory_manual_required'::text
 ELSE 'channel_inventory_not_ready'::text END AS pause_reason FROM channel_policy_flags facts), effective_queue AS (
      SELECT q.channel_id,q.next_due_at,q.attempt_count,q.last_error_code AS last_error_type,
       q.resume_pending,q.no_new_streak,q.lease_expires_at,
       CASE WHEN coalesce(policy.mode,'operator_hold')<>'active'
          OR (q.status='leased' AND q.lease_channel_epoch IS DISTINCT FROM c.control_epoch)
        THEN 'paused' ELSE q.status END AS status
      FROM public.channel_playlist_state q
      LEFT JOIN public.channel_collection_state c USING(channel_id)
      LEFT JOIN channel_policy policy ON policy.channel_id=q.channel_id
      WHERE q.video_type='long'
    )
    SELECT
        count(*) AS total,
        count(*) FILTER (
            WHERE queue.status = 'ready'
              AND queue.next_due_at <= now()
        ) AS due,
        count(*) FILTER (WHERE queue.status = 'ready') AS ready,
        count(*) FILTER (
            WHERE queue.status = 'leased'
              AND queue.lease_expires_at > now()
        ) AS leased,
        count(*) FILTER (
            WHERE queue.status = 'leased'
              AND queue.lease_expires_at <= now()
        ) AS expired_leases,
        count(*) FILTER (WHERE queue.status = 'paused') AS paused,
        count(*) FILTER (
            WHERE queue.attempt_count > 0
              AND queue.last_error_type IS NOT NULL
              AND queue.status <> 'paused'
              AND queue.last_error_type NOT IN ('typed_playlist_not_found', 'channel_not_eligible')
        ) AS failed,
        count(*) FILTER (WHERE queue.resume_pending AND queue.status<>'paused') AS active_resume_states,
        count(*) FILTER (WHERE queue.no_new_streak = 0) AS no_new_streak_0,
        count(*) FILTER (WHERE queue.no_new_streak = 1) AS no_new_streak_1,
        count(*) FILTER (WHERE queue.no_new_streak = 2) AS no_new_streak_2,
        count(*) FILTER (
            WHERE queue.no_new_streak BETWEEN 3 AND 5
        ) AS no_new_streak_3_5,
        count(*) FILTER (WHERE queue.no_new_streak >= 6) AS no_new_streak_6_plus,
        min(queue.next_due_at) FILTER (
            WHERE queue.status = 'ready'
              AND queue.next_due_at <= now()
        ) AS oldest_overdue_at,
        min(queue.lease_expires_at) FILTER (
            WHERE queue.status = 'leased'
        ) AS oldest_lease_expires_at,
        COALESCE(
            (
                SELECT control.reconciliation_required
                FROM public.channel_discovery_queue_control AS control
                WHERE control.singleton
            ),
            true
        ) AS reconciliation_required,
        count(*) FILTER (WHERE queue.status <> 'paused'
          AND queue.last_error_type = 'typed_playlist_not_found') AS playlist_not_found
    FROM effective_queue AS queue;

CREATE OR REPLACE VIEW readable.video_collection_health WITH (security_barrier=true, security_invoker=false) AS
WITH controls AS MATERIALIZED (
 SELECT channel_id,owner_permission,is_hidden,control_epoch,collection_mode
 FROM readable.channel_control
), effective_state AS (
 SELECT state.video_id,state.channel_id,state.video_type,state.paused_reason,
 state.next_due_at,state.lease_token,state.lease_expires_at,state.hydration_needed,
 state.metadata_due_at,state.consecutive_failures,state.typed_collection_status, coalesce(control.owner_permission='accepted'
  AND NOT control.is_hidden AND control.control_epoch IS NOT NULL
  AND (control.collection_mode='active' OR (EXISTS(SELECT 1 FROM public.channel_collection_state handoff_control
 JOIN public.operator_jobs handoff_job ON handoff_job.id=handoff_control.initial_job_id
 JOIN public.operator_job_targets handoff_target ON handoff_target.job_id=handoff_job.id
 WHERE handoff_control.channel_id=state.channel_id
 AND handoff_control.owner_permission='accepted' AND NOT handoff_control.is_hidden
 AND handoff_job.channel_id=handoff_control.channel_id
 AND handoff_job.job_type='channel_initial_long_inventory'
 AND handoff_job.initial_scope_version=1 AND handoff_job.targets_frozen_at IS NOT NULL
 AND handoff_job.accepted_epoch=handoff_control.control_epoch
 AND handoff_job.initial_video_types=ARRAY['long','short','stream']::text[]
 AND handoff_job.phase IN('hydrate_videos','verify')
 AND handoff_job.status IN('queued','running','completed','error')
 AND handoff_target.video_id=state.video_id AND handoff_target.video_type=state.video_type
 AND handoff_target.status IN('updated','skipped','unavailable','checked')))),false) AS channel_eligible
 FROM public.video_collection_state state
 LEFT JOIN controls control USING(channel_id)
)
select
        count(*)::bigint AS total,
        count(*) filter (
            where state.channel_eligible AND state.paused_reason is null
              and state.next_due_at <= now()
              and (
                  state.lease_token is null
                  or state.lease_expires_at <= now()
              )
        )::bigint AS due,
        count(*) filter (
            where state.lease_token is not null
              and state.lease_expires_at > now()
        )::bigint AS leased,
        count(*) filter (
            where state.lease_token is not null
              and state.lease_expires_at <= now()
        )::bigint AS expired_leases,
        count(*) filter (where NOT state.channel_eligible OR state.paused_reason is not null)::bigint AS paused,
        count(*) filter (
            where state.channel_eligible AND state.paused_reason is null
              and state.next_due_at <= now()
              and (
                  state.lease_token is null
                  or state.lease_expires_at <= now()
              )
              and state.hydration_needed
        )::bigint AS hydration_due,
        count(*) filter (
            where state.channel_eligible AND state.paused_reason is null
              and state.next_due_at <= now()
              and (
                  state.lease_token is null
                  or state.lease_expires_at <= now()
              )
              and not state.hydration_needed
              and state.metadata_due_at <= now()
        )::bigint AS metadata_due,
        count(*) filter (
            where state.channel_eligible AND state.paused_reason is null
              and state.next_due_at <= now()
              and (
                  state.lease_token is null
                  or state.lease_expires_at <= now()
              )
              and not state.hydration_needed
              and (
                  state.metadata_due_at is null
                  or state.metadata_due_at > now()
              )
        )::bigint AS counter_due,
        count(*) filter (where state.channel_eligible AND state.consecutive_failures > 0)::bigint AS failed,
        min(state.next_due_at) filter (
            where state.channel_eligible AND state.paused_reason is null
              and state.next_due_at <= now()
              and (
                  state.lease_token is null
                  or state.lease_expires_at <= now()
              )
        ) AS oldest_overdue_at,
        count(*) filter (where NOT state.channel_eligible)::bigint AS policy_paused
    from effective_state as state
    where state.video_type = 'long';

CREATE OR REPLACE VIEW readable.typed_video_collection_health WITH (security_barrier=true, security_invoker=false) AS
WITH controls AS MATERIALIZED (
 SELECT channel_id,owner_permission,is_hidden,control_epoch,collection_mode
 FROM readable.channel_control
), effective_state AS (
 SELECT state.video_id,state.channel_id,state.video_type,state.paused_reason,
 state.next_due_at,state.lease_token,state.lease_expires_at,state.hydration_needed,
 state.metadata_due_at,state.consecutive_failures,state.typed_collection_status, coalesce(control.owner_permission='accepted'
  AND NOT control.is_hidden AND control.control_epoch IS NOT NULL
  AND (control.collection_mode='active' OR (EXISTS(SELECT 1 FROM public.channel_collection_state handoff_control
 JOIN public.operator_jobs handoff_job ON handoff_job.id=handoff_control.initial_job_id
 JOIN public.operator_job_targets handoff_target ON handoff_target.job_id=handoff_job.id
 WHERE handoff_control.channel_id=state.channel_id
 AND handoff_control.owner_permission='accepted' AND NOT handoff_control.is_hidden
 AND handoff_job.channel_id=handoff_control.channel_id
 AND handoff_job.job_type='channel_initial_long_inventory'
 AND handoff_job.initial_scope_version=1 AND handoff_job.targets_frozen_at IS NOT NULL
 AND handoff_job.accepted_epoch=handoff_control.control_epoch
 AND handoff_job.initial_video_types=ARRAY['long','short','stream']::text[]
 AND handoff_job.phase IN('hydrate_videos','verify')
 AND handoff_job.status IN('queued','running','completed','error')
 AND handoff_target.video_id=state.video_id AND handoff_target.video_type=state.video_type
 AND handoff_target.status IN('updated','skipped','unavailable','checked')))),false) AS channel_eligible
 FROM public.video_collection_state state
 LEFT JOIN controls control USING(channel_id)
), types AS (
select coalesce(jsonb_object_agg(summary.video_type, summary.payload), '{}'::jsonb) AS value
    from (
        select
            runtime.video_type,
            jsonb_build_object(
                'runtime_enabled', runtime.runtime_enabled,
                'total', count(state.video_id),
                'policy_paused', count(state.video_id) filter (where NOT state.channel_eligible),
                'due', count(state.video_id) filter (
                    where runtime.runtime_enabled AND state.channel_eligible AND state.paused_reason is null
                      and state.next_due_at <= now()
                      and (
                          state.lease_token is null
                          or state.lease_expires_at <= now()
                      )
                ),
                'leased', count(state.video_id) filter (
                    where state.lease_token is not null
                      and state.lease_expires_at > now()
                ),
                'pending', count(state.video_id) filter (
                    where state.typed_collection_status = 'pending'
                ),
                'hydrated', count(state.video_id) filter (
                    where state.typed_collection_status = 'hydrated'
                ),
                'awaiting_completion', count(state.video_id) filter (
                    where state.typed_collection_status = 'awaiting_completion'
                ),
                'unavailable', count(state.video_id) filter (
                    where state.typed_collection_status = 'unavailable'
                ),
                'failed', count(state.video_id) filter (
                    where state.channel_eligible AND state.consecutive_failures > 0
                ),
                'oldest_overdue_at', min(state.next_due_at) filter (
                    where runtime.runtime_enabled AND state.channel_eligible AND state.paused_reason is null
                      and state.next_due_at <= now()
                      and (
                          state.lease_token is null
                          or state.lease_expires_at <= now()
                      )
                )
            ) as payload
        from public.typed_video_collection_runtime as runtime
        left join effective_state as state
          on state.video_type = runtime.video_type
        group by
            runtime.video_type,
            runtime.runtime_enabled
    ) as summary
)
SELECT jsonb_build_object(
        'status', 'ok',
        'schema_version', 1,
        'repair_version', 2,
        'bounded_claim_batch', true,
        'operational_eligibility_checked', false,
        'channel_eligibility_checked', true,
        'video_types', (SELECT value FROM types),
        'statistics_collection', exists (
            select 1
            from public.typed_video_collection_runtime as runtime
            where runtime.runtime_enabled
        ),
        'product_exposure_changed', false
    ) AS value;
