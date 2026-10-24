-- Operational retry episodes share the existing per-video lease owner.
-- These fields never renew the acquisition clocks of stored YouTube data.
ALTER TABLE public.video_collection_state
 ADD COLUMN availability_first_missing_at timestamptz,
 ADD COLUMN availability_schedule_type text,
 ADD COLUMN availability_resume_observed_at timestamptz;
ALTER TABLE public.video_collection_state ADD CONSTRAINT video_collection_availability_episode CHECK
 (((availability_first_missing_at IS NULL AND availability_schedule_type IS NULL)
 OR (availability_first_missing_at IS NOT NULL AND isfinite(availability_first_missing_at)
     AND (availability_schedule_type IS NULL OR availability_schedule_type IN ('long','short','stream'))))
 AND (availability_resume_observed_at IS NULL OR isfinite(availability_resume_observed_at)));
GRANT INSERT (availability_first_missing_at,availability_schedule_type,availability_resume_observed_at)
 ON public.video_collection_state TO service_role;
GRANT UPDATE (availability_first_missing_at,availability_schedule_type,availability_resume_observed_at)
 ON public.video_collection_state TO service_role;

-- Preserve an already observed absence across the upgrade. No invented source
-- date: the earliest retained incident wins, otherwise its existing check date.
WITH episodes AS (
 SELECT state.video_id,state.video_type,
 coalesce(incident.first_seen_at,
   CASE WHEN state.typed_collection_status='unavailable' THEN state.typed_collection_checked_at END,
   CASE WHEN video.availability_status='unknown_unavailable' THEN video.availability_checked_at END) AS first_missing
 FROM public.video_collection_state state
 LEFT JOIN public.video_availability_incidents incident ON incident.video_id=state.video_id AND incident.restored_at IS NULL
 LEFT JOIN public.videos video ON video.video_id=state.video_id)
UPDATE public.video_collection_state state
 SET availability_first_missing_at=episodes.first_missing,availability_schedule_type=episodes.video_type,
 stats_due_at=NULL,hydration_needed=true,
 paused_reason=CASE WHEN state.paused_reason IN ('unavailable_inventory','typed_identity_not_eligible',
  'channel_hidden','channel_no_long_playlist','channel_inventory_not_ready',
  'channel_inventory_manual_required','channel_inventory_error') THEN NULL ELSE state.paused_reason END,
 next_due_at=coalesce(state.next_due_at,clock_timestamp()),
 metadata_due_at=coalesce(state.next_due_at,state.metadata_due_at,clock_timestamp())
 FROM episodes WHERE state.video_id=episodes.video_id AND episodes.first_missing IS NOT NULL
 AND isfinite(episodes.first_missing) AND episodes.first_missing<=clock_timestamp();

DO $direct_completion_queue_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('video_collection_state',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_collection_state','video_id','INSERT'),
 ('video_collection_state','channel_id','INSERT'),
 ('video_collection_state','stats_due_at','INSERT'),
 ('video_collection_state','metadata_due_at','INSERT'),
 ('video_collection_state','stats_source_checked_at','INSERT'),
 ('video_collection_state','metadata_source_checked_at','INSERT'),
 ('video_collection_state','next_due_at','INSERT'),
 ('video_collection_state','policy_version','INSERT'),
 ('video_collection_state','hydration_needed','INSERT'),
 ('video_collection_state','paused_reason','INSERT'),
 ('video_collection_state','updated_at','INSERT'),
 ('video_collection_state','video_type','INSERT'),
 ('video_collection_state','typed_source_published_at','INSERT'),
 ('video_collection_state','typed_source_published_acquired_at','INSERT'),
 ('video_collection_state','typed_collection_status','INSERT'),
 ('video_collection_state','availability_first_missing_at','INSERT'),
 ('video_collection_state','availability_schedule_type','INSERT'),
 ('video_collection_state','availability_resume_observed_at','INSERT'),
 ('video_collection_state','next_due_at','UPDATE'),
 ('video_collection_state','lease_channel_epoch','UPDATE'),
 ('video_collection_state','lease_token','UPDATE'),
 ('video_collection_state','lease_owner','UPDATE'),
 ('video_collection_state','lease_started_at','UPDATE'),
 ('video_collection_state','lease_expires_at','UPDATE'),
 ('video_collection_state','consecutive_failures','UPDATE'),
 ('video_collection_state','last_error_type','UPDATE'),
 ('video_collection_state','updated_at','UPDATE'),
 ('video_collection_state','metadata_due_at','UPDATE'),
 ('video_collection_state','stats_due_at','UPDATE'),
 ('video_collection_state','attempt_count','UPDATE'),
 ('video_collection_state','last_claim_run_id','UPDATE'),
 ('video_collection_state','last_attempt_at','UPDATE'),
 ('video_collection_state','channel_id','UPDATE'),
 ('video_collection_state','stats_source_checked_at','UPDATE'),
 ('video_collection_state','metadata_source_checked_at','UPDATE'),
 ('video_collection_state','policy_version','UPDATE'),
 ('video_collection_state','hydration_needed','UPDATE'),
 ('video_collection_state','paused_reason','UPDATE'),
 ('video_collection_state','video_type','UPDATE'),
 ('video_collection_state','typed_source_published_at','UPDATE'),
 ('video_collection_state','typed_source_published_acquired_at','UPDATE'),
 ('video_collection_state','typed_stream_actual_end_at','UPDATE'),
 ('video_collection_state','typed_source_stream_end_acquired_at','UPDATE'),
 ('video_collection_state','last_success_at','UPDATE'),
 ('video_collection_state','profile_metadata_version','UPDATE'),
 ('video_collection_state','typed_collection_status','UPDATE'),
 ('video_collection_state','typed_collection_checked_at','UPDATE'),
 ('video_collection_state','availability_first_missing_at','UPDATE'),
 ('video_collection_state','availability_schedule_type','UPDATE'),
 ('video_collection_state','availability_resume_observed_at','UPDATE')
), roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid
), relations AS MATERIALIZED (
 SELECT expected.*,relation.oid,relation.relowner,relation.relacl,relation.relkind,relation.relrowsecurity
 FROM required_tables expected LEFT JOIN pg_catalog.pg_class relation
 ON relation.oid=pg_catalog.to_regclass('public.'||expected.table_name)
), table_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')),
 column_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))
SELECT roles.valid AS roles_valid,
 NOT EXISTS(SELECT 1 FROM relations r WHERE r.oid IS NULL OR r.relkind<>'r' OR NOT r.relrowsecurity
  OR pg_catalog.pg_get_userbyid(r.relowner)<>'postgres')
 AND NOT EXISTS(SELECT 1 FROM required_columns required LEFT JOIN relations r USING(table_name)
  WHERE NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=r.oid
   AND a.attnum>0 AND NOT a.attisdropped AND a.attname=required.column_name)) AS relations_valid,
 NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
  WHERE pg_catalog.has_table_privilege(roles.service_oid,r.oid,p.privilege)
    IS DISTINCT FROM (p.privilege=ANY(r.privileges)))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN column_privileges p WHERE a.attnum>0 AND NOT a.attisdropped
  AND pg_catalog.has_column_privilege(roles.service_oid,r.oid,a.attnum,p.privilege)
   IS DISTINCT FROM (p.privilege=ANY(r.privileges) OR EXISTS(SELECT 1 FROM required_columns required
    WHERE required.table_name=r.table_name AND required.column_name=a.attname AND required.privilege=p.privilege)))
 AND NOT EXISTS(SELECT 1 FROM relations r
  CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce(r.relacl,pg_catalog.acldefault('r',r.relowner))) acl
  WHERE acl.grantee<>r.relowner AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable
   OR NOT(acl.privilege_type=ANY(r.privileges))))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
  WHERE a.attnum>0 AND NOT a.attisdropped AND acl.grantee<>r.relowner
  AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable OR NOT EXISTS(
   SELECT 1 FROM required_columns required WHERE required.table_name=r.table_name
    AND required.column_name=a.attname AND required.privilege=acl.privilege_type))) AS privileges_valid
FROM roles
 ) facts;
 IF checked.roles_valid IS DISTINCT FROM true OR checked.relations_valid IS DISTINCT FROM true
  OR checked.privileges_valid IS DISTINCT FROM true
 THEN RAISE EXCEPTION 'video_failure_acl_invalid'; END IF;
END $direct_completion_queue_acl$;

-- A missing General response observes availability, not metadata. Keep the
-- protected read proof tied to that observation and its live incident.
CREATE OR REPLACE VIEW readable.video_hydration_proof WITH (security_barrier=true, security_invoker=false) AS
SELECT state.video_id,state.channel_id,
 coalesce(state.channel_id=inventory.channel_id AND state.video_type=inventory.video_type
  AND state.paused_reason IS NULL,false) AS identity_matches,
 coalesce(state.typed_collection_status = 'hydrated'
                and not state.hydration_needed
                and video.channel_id = inventory.channel_id
                and video.published_at is not null and video.views is not null
                and state.stats_source_checked_at is not null
                and state.metadata_source_checked_at is not null
                and state.typed_collection_checked_at is not null
                and state.stats_source_checked_at = video.checked_at
                and state.metadata_source_checked_at = video.metadata_checked_at
                and state.typed_collection_checked_at = state.metadata_source_checked_at
                and state.stats_source_checked_at <= now()
                and state.metadata_source_checked_at <= now()
                and (inventory.video_type = 'short'
                    or (video.stream_actual_end_at is not null
                        and video.stream_actual_end_at = state.typed_stream_actual_end_at
                        and video.stream_actual_end_at <= state.typed_collection_checked_at)),
                false) as profile_observed,
            coalesce(state.typed_collection_status = 'unavailable'
                and state.typed_collection_checked_at is not null
                and isfinite(state.typed_collection_checked_at)
                and state.typed_collection_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
                and unavailable.channel_id = inventory.channel_id
                and unavailable.last_seen_at = state.typed_collection_checked_at,
                false) as unavailable_observed,
            coalesce(inventory.video_type = 'stream'
                and state.typed_collection_status = 'awaiting_completion'
                and state.hydration_needed
                and state.typed_stream_actual_end_at is null
                and video.stream_actual_end_at is null
                and state.typed_collection_checked_at is not null
                and isfinite(state.typed_collection_checked_at)
                and state.typed_collection_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
                and state.metadata_source_checked_at = state.typed_collection_checked_at,
                false) as completion_checked,
            coalesce(state.consecutive_failures > 0 or state.last_error_type is not null,
                false) as has_failure,
 least(video.source_expires_at,unavailable.source_expires_at,
   CASE WHEN isfinite(state.typed_collection_checked_at)
    AND state.typed_collection_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
    THEN state.typed_collection_checked_at+interval '696 hours' END) AS source_expires_at
 FROM public.video_collection_state state
 LEFT JOIN LATERAL (
  SELECT source.channel_id,source.video_type
  FROM readable.video_inventory source
  WHERE source.video_id=state.video_id OFFSET 0
 ) inventory ON true
 LEFT JOIN readable.videos video ON video.video_id=state.video_id
 LEFT JOIN readable.open_video_availability unavailable ON unavailable.video_id=state.video_id;


-- Completed, accepted legacy empty-Long channels use the common typed lifecycle.
CREATE OR REPLACE VIEW public.channel_collection_control WITH (security_invoker='true') AS
 SELECT control.channel_id,
    control.user_work_id,
    control.owner_permission,
    control.is_hidden,
    control.hidden_at,
    control.control_epoch,
    control.initial_job_id,
        CASE
            WHEN control.is_hidden THEN 'hidden'::text
            WHEN (control.owner_permission <> 'accepted'::text) THEN 'operator_hold'::text
            WHEN (control.initial_job_id IS NOT NULL) THEN
            CASE
                WHEN ((job.id IS NULL) OR (job.channel_id IS DISTINCT FROM control.channel_id)) THEN 'operator_hold'::text
                WHEN (job.job_type IS DISTINCT FROM 'channel_initial_long_inventory'::text) THEN 'operator_hold'::text
                WHEN ((job.accepted_epoch IS NULL) OR (job.error_count IS NULL) OR (job.status IS NULL) OR (job.phase IS NULL) OR ((job.initial_video_types IS DISTINCT FROM ARRAY['long'::text]) AND (job.initial_video_types IS DISTINCT FROM ARRAY['long'::text, 'short'::text, 'stream'::text]))) THEN 'operator_hold'::text
                WHEN ((job.initial_scope_version = 1) AND (job.status = ANY (ARRAY['blocked'::text, 'error'::text, 'suspended'::text]))) THEN 'operator_hold'::text
                WHEN ((job.initial_scope_version = 1) AND (job.status = 'completed'::text) AND (job.targets_frozen_at IS NULL)) THEN 'operator_hold'::text
                WHEN ((job.initial_video_types = ARRAY['long'::text]) AND (job.status = 'completed'::text) AND (channel.initial_long_inventory_status = 'skipped_no_long_playlist'::text)) THEN 'active'::text
                WHEN ((job.status = 'completed'::text) AND (job.error_count = 0) AND (((cardinality(job.initial_video_types) = 3) AND (job.phase = 'verify'::text)) OR ((job.initial_video_types = ARRAY['long'::text]) AND (job.phase = ANY (ARRAY['verify'::text, 'initial_stats'::text]))))) THEN 'active'::text
                ELSE 'initial_inventory'::text
            END
            WHEN (channel.initial_long_inventory_status = 'completed'::text) THEN 'active'::text
            WHEN (channel.initial_long_inventory_status = 'skipped_no_long_playlist'::text) THEN 'active'::text
            ELSE 'initial_inventory'::text
        END AS collection_mode
   FROM ((public.channel_collection_state control
     JOIN public.channels channel ON ((channel.channel_id = control.channel_id)))
     LEFT JOIN public.operator_jobs job ON ((job.id = control.initial_job_id)));


-- Completed, accepted legacy empty-Long channels use the common typed lifecycle.
CREATE OR REPLACE VIEW readable.channel_control WITH (security_barrier=true, security_invoker=false) AS
SELECT control.*, coalesce((SELECT job.status='error' AND job.last_error_code='source_scope_expired'
 AND job.lease_token IS NULL AND job.lease_expires_at IS NULL AND isfinite(job.requested_at)
 AND job.requested_at<clock_timestamp()-interval '696 hours'
 FROM public.operator_jobs job WHERE job.channel_id=channel.channel_id AND job.job_type='channel_initial_long_inventory'
 ORDER BY job.requested_at DESC,job.id DESC LIMIT 1),false) AS restart_initial_scope FROM (SELECT facts.channel_id,facts.user_work_id,facts.owner_permission,facts.is_hidden,facts.hidden_at,facts.control_epoch,facts.initial_job_id,facts.collection_mode,CASE
 WHEN facts.control_present IS TRUE AND facts.hidden IS FALSE AND facts.permission_accepted IS TRUE AND facts.mode_active IS TRUE THEN true
 WHEN facts.control_present IS TRUE AND facts.hidden IS FALSE AND facts.permission_accepted IS TRUE AND facts.mode_metadata IS TRUE THEN true
 WHEN facts.control_present IS TRUE AND facts.hidden IS FALSE AND facts.permission_accepted IS TRUE AND facts.mode_initial IS TRUE THEN true
 ELSE false END AS typed_runtime_eligible FROM (SELECT modes.*,collection_mode='active' AS mode_active,
 collection_mode='metadata_only' AS mode_metadata,collection_mode='initial_inventory' AS mode_initial
 FROM (SELECT facts.*,CASE
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
 ELSE 'initial_inventory'::text END AS collection_mode FROM (SELECT control.channel_id,control.user_work_id,control.owner_permission,control.is_hidden,control.hidden_at,control.control_epoch,control.initial_job_id,(control.channel_id IS NOT NULL AND channel.channel_id IS NOT NULL) AS control_present,(control.is_hidden) AS hidden,(coalesce(control.is_hidden,true)) AS pause_hidden,(control.owner_permission='accepted') AS permission_accepted,(control.owner_permission<>'accepted') AS permission_not_accepted,(control.owner_permission='manual_required') AS permission_manual,(control.initial_job_id IS NOT NULL) AS linked,(job.id IS NULL) AS job_missing,(job.channel_id IS DISTINCT FROM control.channel_id) AS job_channel_distinct,(job.job_type IS DISTINCT FROM 'channel_initial_long_inventory') AS job_type_distinct,(job.accepted_epoch IS NULL) AS epoch_null,(job.error_count IS NULL) AS errors_null,(job.status IS NULL) AS status_null,(job.phase IS NULL) AS phase_null,(job.initial_video_types IS NOT DISTINCT FROM ARRAY['long']::text[]) AS types_long,(job.initial_video_types IS NOT DISTINCT FROM ARRAY['long','short','stream']::text[]) AS types_all,(cardinality(job.initial_video_types)=3) AS type_count_three,(job.initial_scope_version=1) AS scope_full,(job.status='blocked') AS status_blocked,(job.status='error') AS status_error,(job.status='suspended') AS status_suspended,(job.status='completed') AS status_completed,(job.targets_frozen_at IS NULL) AS frozen_null,(job.error_count=0) AS errors_zero,(job.phase='verify') AS phase_verify,(job.phase='initial_stats') AS phase_initial_stats,(channel.initial_long_inventory_status='completed') AS initial_completed,(channel.initial_long_inventory_status='skipped_no_long_playlist') AS initial_skipped FROM public.channel_collection_state control
 JOIN public.channels channel ON channel.channel_id=control.channel_id
 LEFT JOIN public.operator_jobs job ON job.id=control.initial_job_id) facts) modes) facts) control JOIN public.channels channel USING(channel_id);


-- Completed, accepted legacy empty-Long channels use the common typed lifecycle.
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
        ) AS reconciliation_required
    FROM effective_queue AS queue;


-- Completed, accepted legacy empty-Long channels use the common typed lifecycle.
CREATE OR REPLACE VIEW readable.channel_sync_health WITH (security_barrier=true, security_invoker=false) AS
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
 ELSE 'channel_inventory_not_ready'::text END AS pause_reason FROM channel_policy_flags facts), channel_modes as (
        select channel.mode as mode
        from channel_policy as channel
    )
    select jsonb_build_object(
        'channel_mode_counts', coalesce((
            select jsonb_object_agg(mode, count_value)
            from (
                select mode, count(*)::bigint as count_value
                from channel_modes
                group by mode
                order by mode
            ) as counts
        ), '{}'::jsonb),
        -- Readiness measures broken canonical authority, not asynchronously
        -- retained materialized pauses: short remove intentionally has no fanout.
        'collection_rows_policy_drift', (
            select count(*)
            from public.channels as channel
            left join public.channel_collection_state as control
              on control.channel_id=channel.channel_id
            where control.channel_id is null
               or control.user_work_id is distinct from channel.user_work_id
               or control.control_epoch is null or control.control_epoch<=0
               or control.owner_permission is null
               or control.owner_permission not in ('accepted','manual_required','legacy_hold')
               or control.is_hidden is null
        ),
        'materialized_collection_pause_drift', (
            select count(*)
            from public.video_collection_state as state
            join channel_policy as channel
              on channel.channel_id = state.channel_id
            where (
                (case when state.video_type='long' then channel.mode
                 when channel.typed_runtime_eligible then 'active'
                 else 'operator_hold' end) = 'active'
                and state.paused_reason in (
                    'channel_hidden',
                    'channel_no_long_playlist',
                    'channel_inventory_not_ready',
                    'channel_inventory_manual_required',
                    'channel_inventory_error'
                )
            ) or (
                (case when state.video_type='long' then channel.mode
                 when channel.typed_runtime_eligible then 'active'
                 else 'operator_hold' end) <> 'active'
                and state.paused_reason is null
            )
        )
    ) AS value;
