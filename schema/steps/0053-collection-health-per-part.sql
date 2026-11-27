-- The collection health summary joined every video state to its format
-- runtime and playlist switch row by row and then sorted all states. It now
-- sums, per channel, stored format and handoff answer, how many videos meet
-- each row condition, then applies the channel, format and runtime flags once
-- per part. The handoff stays the shared rule, asked only where it decides.
CREATE OR REPLACE VIEW readable.typed_video_collection_health WITH (security_barrier=true, security_invoker=false) AS
WITH controls AS MATERIALIZED (
 SELECT channel_id,owner_permission,is_hidden,control_epoch,collection_mode,typed_runtime_eligible,
 EXISTS(SELECT 1 FROM public.operator_jobs initial_job WHERE initial_job.id=control.initial_job_id
  AND initial_job.job_type='channel_initial_long_inventory' AND initial_job.initial_scope_version=1
  AND initial_job.status<>'completed') AS full_initial_pending
 FROM readable.channel_control control
), rows AS (
 SELECT state.channel_id,state.video_type AS stored_type,
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
 AND handoff_target.status IN('updated','skipped','unavailable','checked')) END AS handoff_evidence,
 state.next_due_at,state.typed_collection_status,
 state.paused_reason IS NULL AS unpaused,
 state.next_due_at<=now() AND (state.lease_token IS NULL OR state.lease_expires_at<=now()) AS ready,
 state.consecutive_failures>0 AS failing,
 state.lease_token IS NOT NULL AND state.lease_expires_at>now() AS leased,
 state.lease_token IS NOT NULL AND state.lease_expires_at<=now() AS lease_expired,
 CASE WHEN state.video_type IS NULL THEN 'identity_refresh'
  WHEN state.video_type='stream' AND state.typed_stream_actual_end_at IS NULL THEN 'stream_lifecycle'
  WHEN state.hydration_needed THEN 'hydrate'
  WHEN state.availability_first_missing_at IS NOT NULL OR state.availability_resume_observed_at IS NOT NULL
   OR state.metadata_due_at IS NULL OR state.metadata_due_at<=now() THEN 'metadata'
  ELSE 'counters' END AS stored_collection_mode
 FROM public.video_collection_state state
 LEFT JOIN controls control USING(channel_id)
), parts AS (
 -- Videos of one channel, stored format and handoff answer share every flag
 -- below; their row conditions are summed once.
 SELECT channel_id,stored_type,coalesce(stored_type,'unknown') AS video_type,handoff_evidence,
 count(*)::bigint AS videos,
 count(*) FILTER(WHERE unpaused)::bigint AS unpaused_videos,
 count(*) FILTER(WHERE unpaused AND ready)::bigint AS ready_videos,
 count(*) FILTER(WHERE unpaused AND ready AND stored_collection_mode='hydrate')::bigint AS ready_hydrate,
 count(*) FILTER(WHERE unpaused AND ready AND stored_collection_mode IN('metadata','identity_refresh','stream_lifecycle'))::bigint AS ready_metadata,
 count(*) FILTER(WHERE unpaused AND ready AND stored_collection_mode='counters')::bigint AS ready_counters,
 count(*) FILTER(WHERE failing)::bigint AS failing_videos,
 count(*) FILTER(WHERE unpaused AND failing)::bigint AS unpaused_failing,
 count(*) FILTER(WHERE leased)::bigint AS leased_videos,
 count(*) FILTER(WHERE lease_expired)::bigint AS expired_videos,
 count(*) FILTER(WHERE typed_collection_status='pending')::bigint AS pending_videos,
 count(*) FILTER(WHERE typed_collection_status='hydrated')::bigint AS hydrated_videos,
 count(*) FILTER(WHERE typed_collection_status='awaiting_completion')::bigint AS awaiting_videos,
 count(*) FILTER(WHERE typed_collection_status='unavailable')::bigint AS unavailable_videos,
 min(next_due_at) FILTER(WHERE unpaused AND ready) AS ready_oldest_at
 FROM rows GROUP BY channel_id,stored_type,handoff_evidence
), facts AS (
 SELECT parts.*,
 coalesce(control.owner_permission='accepted' AND NOT control.is_hidden
  AND control.control_epoch IS NOT NULL,false) AS control_allowed,
 coalesce(control.collection_mode='active',false) AS channel_active,
 coalesce(control.collection_mode IN('active','metadata_only'),false) AS recovery_channel_allowed,
 coalesce(control.typed_runtime_eligible,false) AS typed_channel_allowed,
 coalesce(control.full_initial_pending,false) AS full_initial_pending,
 parts.handoff_evidence AS initial_handoff,
 CASE WHEN parts.stored_type IN('short','stream') THEN coalesce(runtime.runtime_enabled,false)
  ELSE true END AS runtime_enabled,
 CASE WHEN parts.stored_type IN('short','stream') THEN coalesce(playlist.runtime_enabled,false)
  ELSE true END AS playlist_enabled
 FROM parts
 LEFT JOIN controls control USING(channel_id)
 LEFT JOIN public.typed_video_collection_runtime runtime ON runtime.video_type=parts.stored_type
 LEFT JOIN public.channel_playlist_state playlist
  ON playlist.channel_id=parts.channel_id AND playlist.video_type=parts.stored_type
), eligible AS (
 SELECT facts.*,control_allowed AND (NOT full_initial_pending OR initial_handoff)
  AND CASE WHEN video_type IN('short','stream') THEN
   recovery_channel_allowed OR (typed_channel_allowed AND playlist_enabled) OR initial_handoff
   WHEN video_type='unknown' THEN recovery_channel_allowed
   ELSE channel_active OR initial_handoff END AS channel_eligible,
 control_allowed AND (channel_active OR initial_handoff) AS legacy_channel_eligible,
 video_type IN('short','stream') AND NOT (playlist_enabled OR initial_handoff) AS identity_only
 FROM facts
), groups AS (
 SELECT video_type,grouping(video_type) AS overall,coalesce(sum(videos),0)::bigint AS total,
 coalesce(sum(ready_videos) FILTER(WHERE channel_eligible),0)::bigint AS due,
 coalesce(sum(unpaused_failing) FILTER(WHERE channel_eligible),0)::bigint AS failed,
 coalesce(sum(failing_videos-CASE WHEN channel_eligible THEN unpaused_failing ELSE 0 END),0)::bigint AS excluded_failed,
 coalesce(sum(videos-CASE WHEN channel_eligible THEN unpaused_videos ELSE 0 END),0)::bigint AS paused,
 coalesce(sum(videos) FILTER(WHERE NOT channel_eligible),0)::bigint AS policy_paused,
 coalesce(sum(videos) FILTER(WHERE NOT runtime_enabled),0)::bigint AS runtime_disabled,
 coalesce(sum(videos) FILTER(WHERE NOT playlist_enabled),0)::bigint AS playlist_disabled,
 coalesce(sum(leased_videos),0)::bigint AS leased,
 coalesce(sum(expired_videos),0)::bigint AS expired_leases,
 coalesce(sum(ready_hydrate) FILTER(WHERE channel_eligible AND NOT identity_only),0)::bigint AS hydration_due,
 coalesce(sum(CASE WHEN identity_only THEN ready_videos ELSE ready_metadata END) FILTER(WHERE channel_eligible),0)::bigint AS metadata_due,
 coalesce(sum(ready_counters) FILTER(WHERE channel_eligible AND NOT identity_only),0)::bigint AS counter_due,
 coalesce(sum(pending_videos),0)::bigint AS pending,
 coalesce(sum(hydrated_videos),0)::bigint AS hydrated,
 coalesce(sum(awaiting_videos),0)::bigint AS awaiting_completion,
 coalesce(sum(unavailable_videos),0)::bigint AS unavailable,
 coalesce(sum(videos) FILTER(WHERE video_type='unknown'),0)::bigint AS identity_pending,
 min(ready_oldest_at) FILTER(WHERE channel_eligible) AS oldest_overdue_at,
 coalesce(sum(videos) FILTER(WHERE NOT legacy_channel_eligible),0)::bigint AS legacy_policy_paused,
 coalesce(sum(ready_videos) FILTER(WHERE runtime_enabled AND legacy_channel_eligible),0)::bigint AS legacy_due,
 coalesce(sum(failing_videos) FILTER(WHERE legacy_channel_eligible),0)::bigint AS legacy_failed,
 min(ready_oldest_at) FILTER(WHERE runtime_enabled AND legacy_channel_eligible) AS legacy_oldest_overdue_at
 FROM eligible GROUP BY GROUPING SETS ((video_type),())
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
