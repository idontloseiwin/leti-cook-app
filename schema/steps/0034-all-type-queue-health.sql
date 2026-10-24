-- Add an all-format read-only observation to the existing typed-health view.
-- No queue, deadline, runtime switch, claim predicate or grant is changed.
-- Keep schema33 video_types as legacy typed-runner observations. queue_health
-- describes the shared scheduled collector; their switch semantics differ.
CREATE OR REPLACE VIEW readable.typed_video_collection_health WITH (security_barrier=true, security_invoker=false) AS
WITH controls AS MATERIALIZED (
 SELECT channel_id,owner_permission,is_hidden,control_epoch,collection_mode,typed_runtime_eligible,
 EXISTS(SELECT 1 FROM public.operator_jobs initial_job WHERE initial_job.id=control.initial_job_id
  AND initial_job.job_type='channel_initial_long_inventory' AND initial_job.initial_scope_version=1
  AND initial_job.status<>'completed') AS full_initial_pending
 FROM readable.channel_control control
), facts AS (
 SELECT state.video_id,state.channel_id,coalesce(state.video_type,'unknown') AS video_type,
 state.paused_reason,state.next_due_at,state.lease_token,state.lease_expires_at,
 state.hydration_needed,state.metadata_due_at,state.consecutive_failures,
 state.typed_collection_status,
 CASE WHEN state.video_type IS NULL THEN 'identity_refresh'
  WHEN state.video_type='stream' AND state.typed_stream_actual_end_at IS NULL THEN 'stream_lifecycle'
  WHEN state.hydration_needed THEN 'hydrate'
  WHEN state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL
   OR state.metadata_due_at IS NULL OR state.metadata_due_at<=now() THEN 'metadata'
  ELSE 'counters' END AS stored_collection_mode,
 coalesce(control.owner_permission='accepted' AND NOT control.is_hidden
  AND control.control_epoch IS NOT NULL,false) AS control_allowed,
 coalesce(control.collection_mode='active',false) AS channel_active,
 coalesce(control.collection_mode IN('active','metadata_only'),false) AS recovery_channel_allowed,
 coalesce(control.typed_runtime_eligible,false) AS typed_channel_allowed,
 coalesce(control.full_initial_pending,false) AS full_initial_pending,
 CASE WHEN control.collection_mode='active'
  OR control.owner_permission IS DISTINCT FROM 'accepted'
  OR control.is_hidden IS DISTINCT FROM false OR control.control_epoch IS NULL THEN false ELSE
 EXISTS(SELECT 1 FROM public.channel_collection_state handoff_control
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
 AND handoff_target.status IN('updated','skipped','unavailable','checked')) END AS initial_handoff,
 CASE WHEN state.video_type IN('short','stream') THEN coalesce(runtime.runtime_enabled,false)
  ELSE true END AS runtime_enabled,
 CASE WHEN state.video_type IN('short','stream') THEN coalesce(playlist.runtime_enabled,false)
  ELSE true END AS playlist_enabled
 FROM public.video_collection_state state
 LEFT JOIN controls control USING(channel_id)
 LEFT JOIN public.typed_video_collection_runtime runtime ON runtime.video_type=state.video_type
 LEFT JOIN public.channel_playlist_state playlist
  ON playlist.channel_id=state.channel_id AND playlist.video_type=state.video_type
), eligible AS (
 SELECT facts.*,control_allowed AND (NOT full_initial_pending OR initial_handoff)
  AND CASE WHEN video_type IN('short','stream') THEN
   recovery_channel_allowed OR (typed_channel_allowed AND playlist_enabled) OR initial_handoff
   WHEN video_type='unknown' THEN recovery_channel_allowed
   ELSE channel_active OR initial_handoff END AS channel_eligible,
 control_allowed AND (channel_active OR initial_handoff) AS legacy_channel_eligible,
 playlist_enabled OR initial_handoff AS effective_playlist_enabled
 FROM facts
), candidates AS (
 SELECT eligible.*,channel_eligible AND paused_reason IS NULL AS candidate,
 CASE WHEN video_type IN('short','stream') AND NOT effective_playlist_enabled
  THEN 'identity_refresh' ELSE stored_collection_mode END AS collection_mode
 FROM eligible
), observed AS (
 SELECT candidates.*,candidate AND next_due_at<=now()
  AND (lease_token IS NULL OR lease_expires_at<=now()) AS is_due,
 runtime_enabled AND legacy_channel_eligible AND paused_reason IS NULL AND next_due_at<=now()
  AND (lease_token IS NULL OR lease_expires_at<=now()) AS legacy_is_due
 FROM candidates
), groups AS (
 SELECT video_type,grouping(video_type) AS overall,count(*)::bigint AS total,
 count(*) FILTER(WHERE is_due)::bigint AS due,
 count(*) FILTER(WHERE candidate AND consecutive_failures>0)::bigint AS failed,
 count(*) FILTER(WHERE NOT candidate AND consecutive_failures>0)::bigint AS excluded_failed,
 count(*) FILTER(WHERE NOT candidate)::bigint AS paused,
 count(*) FILTER(WHERE NOT channel_eligible)::bigint AS policy_paused,
 count(*) FILTER(WHERE NOT runtime_enabled)::bigint AS runtime_disabled,
 count(*) FILTER(WHERE NOT playlist_enabled)::bigint AS playlist_disabled,
 count(*) FILTER(WHERE lease_token IS NOT NULL AND lease_expires_at>now())::bigint AS leased,
 count(*) FILTER(WHERE lease_token IS NOT NULL AND lease_expires_at<=now())::bigint AS expired_leases,
 count(*) FILTER(WHERE is_due AND collection_mode='hydrate')::bigint AS hydration_due,
 count(*) FILTER(WHERE is_due AND collection_mode IN('metadata','identity_refresh','stream_lifecycle'))::bigint AS metadata_due,
 count(*) FILTER(WHERE is_due AND collection_mode='counters')::bigint AS counter_due,
 count(*) FILTER(WHERE typed_collection_status='pending')::bigint AS pending,
 count(*) FILTER(WHERE typed_collection_status='hydrated')::bigint AS hydrated,
 count(*) FILTER(WHERE typed_collection_status='awaiting_completion')::bigint AS awaiting_completion,
 count(*) FILTER(WHERE typed_collection_status='unavailable')::bigint AS unavailable,
 count(*) FILTER(WHERE video_type='unknown')::bigint AS identity_pending,
 min(next_due_at) FILTER(WHERE is_due) AS oldest_overdue_at,
 count(*) FILTER(WHERE NOT legacy_channel_eligible)::bigint AS legacy_policy_paused,
 count(*) FILTER(WHERE legacy_is_due)::bigint AS legacy_due,
 count(*) FILTER(WHERE legacy_channel_eligible AND consecutive_failures>0)::bigint AS legacy_failed,
 min(next_due_at) FILTER(WHERE legacy_is_due) AS legacy_oldest_overdue_at
 FROM observed GROUP BY GROUPING SETS ((video_type),())
), empty AS (
 SELECT jsonb_build_object('total',0,'due',0,'failed',0,'excluded_failed',0,'paused',0,
 'policy_paused',0,'runtime_disabled',0,'playlist_disabled',0,'leased',0,'expired_leases',0,
 'hydration_due',0,'metadata_due',0,'counter_due',0,'pending',0,'hydrated',0,
 'awaiting_completion',0,'unavailable',0,'identity_pending',0,'oldest_overdue_at',NULL) AS value
), typed AS (
 SELECT jsonb_object_agg(kind.video_type,
  coalesce(to_jsonb(summary)-'video_type'-'overall'-'legacy_policy_paused'-'legacy_due'
   -'legacy_failed'-'legacy_oldest_overdue_at',empty.value)) AS value
 FROM (VALUES('long'),('short'),('stream'),('unknown')) kind(video_type)
 CROSS JOIN empty LEFT JOIN groups summary ON summary.video_type=kind.video_type AND summary.overall=0
), legacy AS (
 SELECT coalesce(jsonb_object_agg(runtime.video_type,jsonb_build_object(
  'runtime_enabled',runtime.runtime_enabled,'total',coalesce(summary.total,0),
  'policy_paused',coalesce(summary.legacy_policy_paused,0),'due',coalesce(summary.legacy_due,0),
  'leased',coalesce(summary.leased,0),'pending',coalesce(summary.pending,0),
  'hydrated',coalesce(summary.hydrated,0),'awaiting_completion',coalesce(summary.awaiting_completion,0),
  'unavailable',coalesce(summary.unavailable,0),'failed',coalesce(summary.legacy_failed,0),
  'oldest_overdue_at',summary.legacy_oldest_overdue_at)),
  '{}'::jsonb) AS value
 FROM public.typed_video_collection_runtime runtime
 LEFT JOIN groups summary ON summary.video_type=runtime.video_type AND summary.overall=0
)
SELECT jsonb_build_object(
 'status','ok','schema_version',1,'repair_version',2,'bounded_claim_batch',true,
 'operational_eligibility_checked',false,'channel_eligibility_checked',true,
 'video_types',legacy.value,
 'statistics_collection',EXISTS(SELECT 1 FROM public.typed_video_collection_runtime WHERE runtime_enabled),
 'product_exposure_changed',false,
 'queue_health',(to_jsonb(summary)-'video_type'-'overall'-'legacy_policy_paused'-'legacy_due'
  -'legacy_failed'-'legacy_oldest_overdue_at') || jsonb_build_object(
  'schema_version',2,'scope','all_types','unit','videos','channel_eligibility_checked',true,
  'operational_eligibility_checked',false,'by_type',typed.value)) AS value
FROM groups summary CROSS JOIN typed CROSS JOIN legacy WHERE summary.overall=1;
