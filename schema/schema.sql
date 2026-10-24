-- leti-cook self-hosted clean schema; format=1; application-data=none

BEGIN;

SET LOCAL lock_timeout='3s';

SET LOCAL statement_timeout='60s';

DO $fresh_install$
BEGIN
 IF current_user<>'postgres' OR current_setting('server_version_num')::integer<170000 THEN
  RAISE EXCEPTION 'self_hosted_requires_postgres_owner_and_pg17'; END IF;
 IF (SELECT count(*) FROM pg_roles WHERE rolname IN('anon','authenticated','service_role','lic_reader'))<>4
    OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='service_role' AND rolbypassrls)
    OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lic_reader' AND NOT rolcanlogin
      AND NOT rolbypassrls AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
      AND NOT rolreplication AND NOT rolinherit) THEN
  RAISE EXCEPTION 'self_hosted_platform_roles_required'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
               WHERE e.extname='pgcrypto' AND n.nspname='extensions') THEN
  RAISE EXCEPTION 'self_hosted_pgcrypto_in_extensions_required'; END IF;
 IF EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname IN('public','operator_private') AND NOT EXISTS(
             SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e'))
 OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname IN('public','operator_private') AND NOT EXISTS(
             SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e'))
 OR EXISTS(SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
           WHERE n.nspname IN('public','operator_private') AND t.typtype IN('e','d','r','m')
             AND NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e'))
 THEN RAISE EXCEPTION 'self_hosted_application_schema_not_empty'; END IF;
 IF EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='operator_private' AND nspowner<>'postgres'::regrole) THEN
  RAISE EXCEPTION 'self_hosted_operator_schema_owner_mismatch'; END IF;
END $fresh_install$;

CREATE SCHEMA IF NOT EXISTS public;

CREATE SCHEMA IF NOT EXISTS operator_private;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC,anon,authenticated,service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC,anon,authenticated,service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC,anon,authenticated,service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA operator_private REVOKE ALL ON TABLES FROM PUBLIC,anon,authenticated,service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA operator_private REVOKE ALL ON SEQUENCES FROM PUBLIC,anon,authenticated,service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA operator_private REVOKE ALL ON FUNCTIONS FROM PUBLIC,anon,authenticated,service_role;

SET client_encoding = 'UTF8';

SET standard_conforming_strings = on;

SELECT pg_catalog.set_config('search_path', '', false);

SET check_function_bodies = false;

SET xmloption = content;

SET client_min_messages = warning;

SET row_security = off;

COMMENT ON SCHEMA public IS 'standard public schema';

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE public.operator_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_type text NOT NULL,
    channel_id text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    lease_token uuid,
    lease_owner text,
    lease_expires_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    target_count integer DEFAULT 0 NOT NULL,
    processed_count integer DEFAULT 0 NOT NULL,
    updated_count integer DEFAULT 0 NOT NULL,
    skipped_count integer DEFAULT 0 NOT NULL,
    unavailable_count integer DEFAULT 0 NOT NULL,
    error_count integer DEFAULT 0 NOT NULL,
    max_items integer,
    max_quota_units integer,
    quota_units_estimated integer DEFAULT 0 NOT NULL,
    quota_units_reserved integer DEFAULT 0 NOT NULL,
    quota_units_observed_estimate integer DEFAULT 0 NOT NULL,
    max_target_attempts smallint DEFAULT 3 NOT NULL,
    summary_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_error_code text,
    last_error_summary text,
    phase text DEFAULT 'video_targets'::text NOT NULL,
    resume_page_token text,
    max_pages integer,
    pages_processed integer DEFAULT 0 NOT NULL,
    items_processed integer DEFAULT 0 NOT NULL,
    inventory_inserted_count integer DEFAULT 0 NOT NULL,
    inventory_updated_count integer DEFAULT 0 NOT NULL,
    initial_video_types text[] DEFAULT ARRAY['long'::text] NOT NULL,
    inventory_video_type text DEFAULT 'long'::text NOT NULL,
    retry_not_before timestamp with time zone,
    lease_channel_epoch bigint,
    accepted_epoch bigint,
    inventory_scan_id uuid,
    inventory_checkpoint jsonb DEFAULT '{}'::jsonb NOT NULL,
    initial_scope_version smallint DEFAULT 0 NOT NULL,
    targets_frozen_at timestamp with time zone,
    CONSTRAINT operator_jobs_accepted_epoch_check CHECK ((accepted_epoch > 0)),
    CONSTRAINT operator_jobs_counts_check CHECK (((target_count >= 0) AND (processed_count >= 0) AND (updated_count >= 0) AND (skipped_count >= 0) AND (unavailable_count >= 0) AND (error_count >= 0) AND (quota_units_estimated >= 0) AND (quota_units_reserved >= 0) AND (quota_units_observed_estimate >= 0) AND (max_target_attempts > 0))),
    CONSTRAINT operator_jobs_error_code_check CHECK (((last_error_code IS NULL) OR (last_error_code ~ '^[a-z0-9_]{1,120}$'::text))),
    CONSTRAINT operator_jobs_error_summary_check CHECK (((last_error_summary IS NULL) OR (char_length(last_error_summary) <= 500))),
    CONSTRAINT operator_jobs_initial_scope_check CHECK (((((initial_scope_version = 0) AND (max_items IS NOT NULL) AND (max_items > 0) AND (max_quota_units IS NOT NULL) AND (max_quota_units > 0) AND (targets_frozen_at IS NULL)) OR ((initial_scope_version = 1) AND (job_type = 'channel_initial_long_inventory'::text) AND (initial_video_types = ARRAY['long'::text, 'short'::text, 'stream'::text]) AND (max_items IS NULL) AND (max_pages IS NULL) AND (max_quota_units IS NULL) AND ((targets_frozen_at IS NULL) OR (isfinite(targets_frozen_at) AND (targets_frozen_at >= requested_at))))) IS TRUE)),
    CONSTRAINT operator_jobs_initial_types_check CHECK ((((initial_video_types = ARRAY['long'::text]) OR (initial_video_types = ARRAY['long'::text, 'short'::text, 'stream'::text])) AND (inventory_video_type = ANY (initial_video_types)) AND ((job_type = 'channel_initial_long_inventory'::text) OR (initial_video_types = ARRAY['long'::text])))),
    CONSTRAINT operator_jobs_inventory_ack_check CHECK ((((NOT (summary_json ? 'inventory_ack'::text)) OR ((jsonb_typeof((summary_json -> 'inventory_ack'::text)) = 'object'::text) AND (((summary_json -> 'inventory_ack'::text) - ARRAY['version'::text, 'kind'::text, 'channel_id'::text, 'video_type'::text, 'transition_id'::text, 'job_lease_token'::text, 'scan_id'::text, 'playlist_lease_token'::text, 'request_hash'::text, 'provider_attempts'::text, 'quota_estimated'::text, 'outcome'::text, 'error_code'::text]) = '{}'::jsonb) AND (((summary_json -> 'inventory_ack'::text) -> 'version'::text) = '1'::jsonb) AND (((summary_json -> 'inventory_ack'::text) ->> 'kind'::text) = ANY (ARRAY['settled'::text, 'failed'::text])) AND (((summary_json -> 'inventory_ack'::text) ->> 'channel_id'::text) = channel_id) AND (((summary_json -> 'inventory_ack'::text) ->> 'video_type'::text) = ANY (initial_video_types)) AND (((summary_json -> 'inventory_ack'::text) ->> 'transition_id'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text) AND (((summary_json -> 'inventory_ack'::text) ->> 'job_lease_token'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text) AND ((((summary_json -> 'inventory_ack'::text) -> 'scan_id'::text) = 'null'::jsonb) OR (((summary_json -> 'inventory_ack'::text) ->> 'scan_id'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text)) AND ((((summary_json -> 'inventory_ack'::text) -> 'playlist_lease_token'::text) = 'null'::jsonb) OR (((summary_json -> 'inventory_ack'::text) ->> 'playlist_lease_token'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text)) AND (((summary_json -> 'inventory_ack'::text) ->> 'request_hash'::text) ~ '^[0-9a-f]{32}$'::text) AND (((summary_json -> 'inventory_ack'::text) -> 'provider_attempts'::text) = ANY (ARRAY['0'::jsonb, '1'::jsonb])) AND (((summary_json -> 'inventory_ack'::text) -> 'quota_estimated'::text) = 'true'::jsonb) AND (((summary_json -> 'inventory_ack'::text) ->> 'outcome'::text) = ANY (ARRAY['none'::text, 'page'::text, 'playlist_not_found'::text, 'error'::text, 'quota'::text, 'deferred'::text, 'unknown'::text])) AND ((((summary_json -> 'inventory_ack'::text) -> 'error_code'::text) = 'null'::jsonb) OR (((summary_json -> 'inventory_ack'::text) ->> 'error_code'::text) ~ '^[a-z0-9_]{1,120}$'::text)))) IS TRUE)),
    CONSTRAINT operator_jobs_inventory_checkpoint_check CHECK ((((inventory_checkpoint = '{}'::jsonb) OR ((jsonb_typeof(inventory_checkpoint) = 'object'::text) AND ((inventory_checkpoint ->> 'phase'::text) = ANY (ARRAY['prepared'::text, 'bound'::text])) AND (jsonb_typeof((inventory_checkpoint -> 'job_lease_token'::text)) = 'string'::text) AND ((inventory_checkpoint ->> 'job_lease_token'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text) AND (jsonb_typeof((inventory_checkpoint -> 'transition_id'::text)) = 'string'::text) AND ((inventory_checkpoint ->> 'transition_id'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text) AND (jsonb_typeof((inventory_checkpoint -> 'reserved_units'::text)) = 'number'::text) AND ((inventory_checkpoint ->> 'reserved_units'::text) = ANY (ARRAY['0'::text, '1'::text])) AND ((((inventory_checkpoint ->> 'phase'::text) = 'prepared'::text) AND ((inventory_checkpoint - ARRAY['phase'::text, 'job_lease_token'::text, 'transition_id'::text, 'reserved_units'::text]) = '{}'::jsonb)) OR (((inventory_checkpoint ->> 'phase'::text) = 'bound'::text) AND (inventory_scan_id IS NOT NULL) AND (jsonb_typeof((inventory_checkpoint -> 'playlist_lease_token'::text)) = 'string'::text) AND ((inventory_checkpoint ->> 'playlist_lease_token'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text) AND ((inventory_checkpoint - ARRAY['phase'::text, 'job_lease_token'::text, 'transition_id'::text, 'reserved_units'::text, 'playlist_lease_token'::text]) = '{}'::jsonb))))) IS TRUE)),
    CONSTRAINT operator_jobs_inventory_progress_check CHECK (((pages_processed >= 0) AND (items_processed >= 0) AND (inventory_inserted_count >= 0) AND (inventory_updated_count >= 0) AND ((resume_page_token IS NULL) OR (length(resume_page_token) <= 2048)))),
    CONSTRAINT operator_jobs_lease_channel_epoch_check CHECK ((lease_channel_epoch > 0)),
    CONSTRAINT operator_jobs_phase_check CHECK (((((job_type = 'channel_current_stats_backfill'::text) AND (phase = 'video_targets'::text) AND (resume_page_token IS NULL)) OR ((job_type = 'channel_initial_long_inventory'::text) AND (phase = ANY (ARRAY['inventory'::text, 'hydrate_videos'::text, 'verify'::text, 'initial_stats'::text])) AND (((initial_scope_version = 0) AND (max_pages IS NOT NULL) AND (max_pages > 0)) OR ((initial_scope_version = 1) AND (max_pages IS NULL) AND (phase <> 'initial_stats'::text) AND (((phase = 'inventory'::text) AND (targets_frozen_at IS NULL)) OR ((phase = ANY (ARRAY['hydrate_videos'::text, 'verify'::text])) AND (targets_frozen_at IS NOT NULL))))))) IS TRUE)),
    CONSTRAINT operator_jobs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text, 'blocked'::text, 'error'::text, 'suspended'::text]))),
    CONSTRAINT operator_jobs_type_check CHECK ((job_type = ANY (ARRAY['channel_current_stats_backfill'::text, 'channel_initial_long_inventory'::text])))
);

CREATE TABLE public.channel_video_inventory_scans (
    scan_id uuid DEFAULT gen_random_uuid() NOT NULL,
    channel_id text NOT NULL,
    video_type text NOT NULL,
    playlist_id text NOT NULL,
    scan_kind text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    coverage text DEFAULT 'partial'::text NOT NULL,
    scan_boundary_at timestamp with time zone DEFAULT now() NOT NULL,
    resume_page_token text,
    pages_completed integer DEFAULT 0 NOT NULL,
    items_seen integer DEFAULT 0,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    source_checked_at timestamp with time zone,
    completed_at timestamp with time zone,
    error_code text,
    error_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_transition_id uuid,
    last_transition_kind text,
    new_memberships_found integer DEFAULT 0,
    token_history_verified boolean DEFAULT false NOT NULL,
    CONSTRAINT channel_video_inventory_scans_completion_check CHECK ((((status = 'completed'::text) AND (completed_at IS NOT NULL) AND (resume_page_token IS NULL)) OR ((status <> 'completed'::text) AND (completed_at IS NULL)))),
    CONSTRAINT channel_video_inventory_scans_coverage_check CHECK ((coverage = ANY (ARRAY['partial'::text, 'complete'::text]))),
    CONSTRAINT channel_video_inventory_scans_error_code_check CHECK (((error_code IS NULL) OR (error_code ~ '^[a-z0-9_]{1,64}$'::text))),
    CONSTRAINT channel_video_inventory_scans_kind_check CHECK ((scan_kind = ANY (ARRAY['initial_inventory'::text, 'full_reconciliation'::text, 'discovery'::text]))),
    CONSTRAINT channel_video_inventory_scans_new_memberships_check CHECK ((new_memberships_found >= 0)),
    CONSTRAINT channel_video_inventory_scans_playlist_check CHECK ((((video_type = 'long'::text) AND ("left"(playlist_id, 4) = 'UULF'::text)) OR ((video_type = 'short'::text) AND ("left"(playlist_id, 4) = 'UUSH'::text)) OR ((video_type = 'stream'::text) AND ("left"(playlist_id, 4) = 'UULV'::text)))),
    CONSTRAINT channel_video_inventory_scans_progress_check CHECK (((pages_completed >= 0) AND (items_seen >= 0))),
    CONSTRAINT channel_video_inventory_scans_resume_token_check CHECK (((resume_page_token IS NULL) OR ((length(resume_page_token) >= 1) AND (length(resume_page_token) <= 2048)))),
    CONSTRAINT channel_video_inventory_scans_status_check CHECK ((status = ANY (ARRAY['running'::text, 'completed'::text, 'failed'::text, 'abandoned'::text]))),
    CONSTRAINT channel_video_inventory_scans_transition_check CHECK ((((last_transition_id IS NULL) AND (last_transition_kind IS NULL)) OR ((last_transition_id IS NOT NULL) AND (last_transition_kind = ANY (ARRAY['page'::text, 'failure'::text]))))),
    CONSTRAINT channel_video_inventory_scans_video_type_check CHECK ((video_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text])))
);

CREATE FUNCTION public.bind_channel_user_work_owner() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE owner_id uuid;
BEGIN
 IF TG_TABLE_NAME='saved_collection_members' THEN
  SELECT c.user_work_id INTO owner_id FROM public.video_inventory v JOIN public.channel_collection_state c ON c.channel_id=v.channel_id WHERE v.video_id=NEW.video_id;
 ELSE
  SELECT c.user_work_id INTO owner_id FROM public.channel_collection_state c WHERE c.channel_id=NEW.channel_id;
 END IF;
 -- Detaching expired API evidence must preserve this local organization link.
 -- A restored channel's bind sets the owner explicitly: keep it when no inventory maps the row.
 NEW.user_channel_ref:=coalesce(owner_id,NEW.user_channel_ref,CASE WHEN TG_OP='UPDATE' THEN OLD.user_channel_ref ELSE NULL END);
 RETURN NEW;
END $$;

CREATE FUNCTION public.enforce_user_work_backup_limits() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
-- Ordinary writes stay within the limits that export_user_work_backup and
-- import_user_work_backup accept, so using the app can never make a backup
-- fail. A restore item bound in this transaction is counted once: its channel
-- already has the item's id as user_work_id before the item is removed.
DECLARE exceeded boolean;
BEGIN
 exceeded:=CASE TG_TABLE_NAME
  WHEN 'channels' THEN
   (SELECT count(*) FROM public.channels)
   +(SELECT count(*) FROM public.user_channel_restore_items p
     WHERE NOT EXISTS(SELECT 1 FROM public.channels c WHERE c.user_work_id=p.id))>10000
  WHEN 'saved_objects' THEN
   (SELECT count(*) FROM public.saved_objects WHERE deleted_at IS NULL)
   +coalesce((SELECT sum(jsonb_array_length(p.saved_views)) FROM public.user_channel_restore_items p
     WHERE NOT EXISTS(SELECT 1 FROM public.channels c WHERE c.user_work_id=p.id)),0)>1000
  WHEN 'folders' THEN (SELECT count(*) FROM(SELECT 1 FROM public.folders LIMIT 1001)x)>1000
  WHEN 'channel_labels' THEN (SELECT count(*) FROM(SELECT 1 FROM public.channel_labels LIMIT 1002)x)>1001
  WHEN 'channel_label_assignments' THEN
   (SELECT count(*) FROM public.channel_label_assignments)
   +coalesce((SELECT sum(cardinality(p.label_keys)) FROM public.user_channel_restore_items p
     WHERE NOT EXISTS(SELECT 1 FROM public.channels c WHERE c.user_work_id=p.id)),0)>100000
  WHEN 'saved_collection_members' THEN
   (SELECT count(*) FROM(SELECT 1 FROM public.saved_collection_members LIMIT 100001)x)>100000
  -- Every decision adds history, so both review limits are checked here.
  WHEN 'video_type_review_history' THEN
   (SELECT count(*) FROM(SELECT 1 FROM public.video_type_review_history LIMIT 10001)x)>10000
   OR (SELECT count(*) FROM(SELECT 1 FROM public.video_type_review_cases c WHERE c.decision_type IS NOT NULL
       OR EXISTS(SELECT 1 FROM public.video_type_review_history h WHERE h.case_id=c.case_id) LIMIT 1001)x)>1000
  ELSE false
 END;
 IF exceeded THEN
  RAISE EXCEPTION 'user_work_limit_reached' USING DETAIL=TG_TABLE_NAME;
 END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION public.enforce_video_inventory_membership_scan() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
begin
    if new.first_seen_scan_id is not null
       and not exists (
           select 1
           from public.channel_video_inventory_scans as scan
           where scan.scan_id = new.first_seen_scan_id
             and scan.channel_id = new.channel_id
             and scan.video_type = new.video_type
       )
    then
        raise exception 'membership_first_seen_scan_scope_mismatch';
    end if;

    if new.last_seen_scan_id is not null
       and not exists (
           select 1
           from public.channel_video_inventory_scans as scan
           where scan.scan_id = new.last_seen_scan_id
             and scan.channel_id = new.channel_id
             and scan.video_type = new.video_type
       )
    then
        raise exception 'membership_last_seen_scan_scope_mismatch';
    end if;

    if new.membership_status = 'absent'
       and not exists (
           select 1
           from public.channel_video_inventory_scans as scan
           where scan.scan_id = new.absence_confirmed_by_scan_id
             and scan.channel_id = new.channel_id
             and scan.video_type = new.video_type
             and scan.status = 'completed'
             and scan.coverage = 'complete'
       )
    then
        raise exception 'membership_absence_requires_completed_full_generation';
    end if;

    return new;
end;
$$;

CREATE FUNCTION public.guard_channel_user_work_identity() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
 IF NEW.user_work_id IS DISTINCT FROM OLD.user_work_id THEN
  RAISE EXCEPTION 'user_work_identity_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.guard_hot_operator_scope() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'operator_private', 'pg_temp'
    AS $$
DECLARE expires boolean; source_free_release boolean; at_time timestamptz:=clock_timestamp();
BEGIN
 IF TG_OP='UPDATE' AND NEW.requested_at IS DISTINCT FROM OLD.requested_at THEN RAISE EXCEPTION 'operator_job_source_clock_immutable'; END IF;
 source_free_release:=TG_OP='UPDATE' AND OLD.status IN ('queued','running')
  AND NEW.status IN ('suspended','error') AND NEW.lease_token IS NULL
  AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL AND NEW.lease_channel_epoch IS NULL
  AND (to_jsonb(NEW)-ARRAY['status','lease_token','lease_owner','lease_expires_at','lease_channel_epoch','updated_at','heartbeat_at','finished_at','last_error_code','last_error_summary'])
      IS NOT DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','lease_token','lease_owner','lease_expires_at','lease_channel_epoch','updated_at','heartbeat_at','finished_at','last_error_code','last_error_summary']);
 expires:=NEW.requested_at IS NULL OR NOT isfinite(NEW.requested_at) OR NEW.requested_at>at_time OR NEW.requested_at<at_time-interval '696 hours';
 IF expires AND NOT source_free_release AND NOT (TG_OP='UPDATE' AND NEW.last_error_code='source_scope_expired'
  AND NEW.status IN ('error','completed') AND NEW.resume_page_token IS NULL
  AND NEW.summary_json=CASE WHEN OLD.summary_json->'inventory_chain_version'='1'::jsonb
   THEN '{"source_scope_expired":true,"inventory_chain_version":1}'::jsonb
   ELSE '{"source_scope_expired":true}'::jsonb END AND NEW.last_error_summary IS NULL
  AND NEW.lease_token IS NULL AND NEW.lease_owner IS NULL AND NEW.lease_expires_at IS NULL) THEN
  RAISE EXCEPTION 'operator_job_source_scope_expired';
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.guard_hot_operator_target_scope() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'operator_private', 'pg_temp'
    AS $$
DECLARE parent_job public.operator_jobs%rowtype; source_at timestamptz;
BEGIN
 SELECT requested_at INTO source_at FROM public.operator_jobs WHERE id=NEW.job_id FOR SHARE;
 IF NOT FOUND OR source_at IS NULL OR NOT isfinite(source_at) OR source_at>clock_timestamp() OR source_at<clock_timestamp()-interval '696 hours' THEN
  RAISE EXCEPTION 'operator_job_source_scope_expired';
 END IF;
 SELECT * INTO parent_job FROM public.operator_jobs WHERE id=NEW.job_id FOR SHARE;
 IF NEW.video_type IS NULL AND (parent_job.initial_scope_version<>1 OR parent_job.phase<>'inventory' OR parent_job.targets_frozen_at IS NOT NULL)
 THEN RAISE EXCEPTION 'operator_job_target_type_required'; END IF;
 IF TG_OP='INSERT' AND parent_job.initial_scope_version=1 AND parent_job.targets_frozen_at IS NOT NULL THEN RAISE EXCEPTION 'full_initial_targets_frozen'; END IF;
 IF TG_OP='UPDATE' AND (NEW.job_id IS DISTINCT FROM OLD.job_id OR NEW.video_id IS DISTINCT FROM OLD.video_id) THEN RAISE EXCEPTION 'operator_job_target_identity_immutable'; END IF;
 return new;
END $$;

CREATE FUNCTION public.guard_hot_storage_collection_delete() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
begin
    if old.lease_token is not null and old.lease_expires_at>clock_timestamp() then
        raise exception using errcode='55P03',message='hot_storage_active_work';
    end if;
    return old;
end;
$$;

CREATE FUNCTION public.guard_manual_video_type_decision() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
declare v_case public.video_type_review_cases%rowtype;
begin
    select * into v_case from public.video_type_review_cases where video_id=old.video_id;
    if v_case.decision_type is not null and
       (new.video_id is distinct from old.video_id or new.channel_id is distinct from v_case.channel_id
        or (new.video_type is distinct from v_case.decision_type
            and not (new.video_type is null and new.skeleton_since is not null))) then
        raise exception using errcode='23514', message='manual_video_type_decision_protected';
    end if;
    return new;
end;
$$;

CREATE SEQUENCE operator_private.inventory_dispatch_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

CREATE TABLE operator_private.operator_job_retry_requests (
    job_id uuid NOT NULL,
    retry_request_id uuid NOT NULL,
    max_items integer,
    max_pages integer,
    max_quota_units integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    request_control_epoch bigint,
    initial_scope_version smallint DEFAULT 0 NOT NULL,
    CONSTRAINT operator_job_retry_requests_request_control_epoch_check CHECK ((request_control_epoch > 0)),
    CONSTRAINT operator_job_retry_requests_scope_check CHECK (((((initial_scope_version = 0) AND (max_items IS NOT NULL) AND (max_items > 0) AND (max_pages IS NOT NULL) AND ((max_pages >= 1) AND (max_pages <= 2000)) AND (max_quota_units IS NOT NULL) AND (max_quota_units > 0)) OR ((initial_scope_version = 1) AND (max_items IS NULL) AND (max_pages IS NULL) AND (max_quota_units IS NULL))) IS TRUE))
);

CREATE TABLE public.video_collection_state (
    video_id text NOT NULL,
    channel_id text NOT NULL,
    stats_due_at timestamp with time zone,
    metadata_due_at timestamp with time zone,
    stats_source_checked_at timestamp with time zone,
    metadata_source_checked_at timestamp with time zone,
    next_due_at timestamp with time zone,
    hydration_needed boolean DEFAULT false NOT NULL,
    policy_version integer DEFAULT 1 NOT NULL,
    lease_token uuid,
    lease_owner text,
    lease_started_at timestamp with time zone,
    lease_expires_at timestamp with time zone,
    attempt_count integer DEFAULT 0 NOT NULL,
    consecutive_failures integer DEFAULT 0 NOT NULL,
    last_claim_run_id uuid,
    last_attempt_at timestamp with time zone,
    last_success_at timestamp with time zone,
    last_error_type text,
    paused_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    profile_metadata_version smallint DEFAULT 0 NOT NULL,
    video_type text DEFAULT 'long'::text,
    typed_source_published_at timestamp with time zone,
    typed_stream_actual_end_at timestamp with time zone,
    typed_collection_status text,
    typed_collection_checked_at timestamp with time zone,
    lease_channel_epoch bigint,
    typed_source_published_acquired_at timestamptz,
    typed_source_stream_end_acquired_at timestamptz,
    CONSTRAINT video_collection_state_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT video_collection_state_error_type_check CHECK (((last_error_type IS NULL) OR (last_error_type ~ '^[a-z0-9_]{1,120}$'::text))),
    CONSTRAINT video_collection_state_failure_count_check CHECK ((consecutive_failures >= 0)),
    CONSTRAINT video_collection_state_lease_channel_epoch_check CHECK ((lease_channel_epoch > 0)),
    CONSTRAINT video_collection_state_lease_check CHECK ((((lease_token IS NULL) AND (lease_owner IS NULL) AND (lease_started_at IS NULL) AND (lease_expires_at IS NULL)) OR ((lease_token IS NOT NULL) AND (lease_owner IS NOT NULL) AND (btrim(lease_owner) <> ''::text) AND (lease_started_at IS NOT NULL) AND (lease_expires_at > lease_started_at)))),
    CONSTRAINT video_collection_state_paused_reason_check CHECK (((paused_reason IS NULL) OR (btrim(paused_reason) <> ''::text))),
    CONSTRAINT video_collection_state_policy_version_check CHECK ((policy_version > 0)),
    CONSTRAINT video_collection_state_profile_metadata_version_check CHECK (((profile_metadata_version >= 0) AND (profile_metadata_version <= 1))),
    CONSTRAINT video_collection_state_stream_end_type_check CHECK (((typed_stream_actual_end_at IS NULL) OR (video_type = 'stream'::text))),
    CONSTRAINT video_collection_state_video_type_check CHECK ((video_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text])))
);

CREATE VIEW operator_private.video_collection_state_canonical_repair_lock WITH (security_invoker='false') AS
 SELECT count(*) AS state_row_count
   FROM public.video_collection_state;

CREATE TABLE public.channel_collection_state (
    channel_id text NOT NULL,
    user_work_id uuid NOT NULL,
    owner_permission text DEFAULT 'legacy_hold'::text NOT NULL,
    is_hidden boolean DEFAULT false NOT NULL,
    hidden_at timestamp with time zone,
    control_epoch bigint NOT NULL,
    initial_job_id uuid,
    CONSTRAINT channel_collection_state_control_epoch_check CHECK ((control_epoch > 0)),
    CONSTRAINT channel_collection_state_owner_permission_check CHECK ((owner_permission = ANY (ARRAY['accepted'::text, 'manual_required'::text, 'legacy_hold'::text])))
);

CREATE TABLE public.channels (
    channel_id text NOT NULL,
    title text,
    subscribers bigint,
    views bigint,
    video_count bigint,
    added_at timestamp with time zone,
    avatar_url text,
    folder_id uuid,
    parent_channel_id text,
    uploads_playlist_id text,
    deep_bypass boolean DEFAULT false,
    deep_bypass_reason text,
    deep_inventory_status text DEFAULT 'idle'::text,
    deep_inventory_started_at timestamp with time zone,
    deep_inventory_checked_at timestamp with time zone,
    deep_inventory_error text,
    initial_long_inventory_status text,
    initial_28d_stats_status text,
    is_hidden boolean DEFAULT false NOT NULL,
    hidden_at timestamp with time zone,
    initial_long_inventory_started_at timestamp with time zone,
    initial_28d_stats_started_at timestamp with time zone,
    channel_metadata_checked_at timestamp with time zone,
    channel_metadata_next_after timestamp with time zone,
    channel_metadata_status text,
    channel_metadata_started_at timestamp with time zone,
    channel_metadata_error text,
    channel_metadata_error_count integer DEFAULT 0 NOT NULL,
    channel_video_count_changed_at timestamp with time zone,
    country_code text,
    country_collected_at timestamp with time zone,
    initial_long_inventory_error_code text,
    initial_long_inventory_error_at timestamp with time zone,
    initial_long_inventory_error_count integer DEFAULT 0 NOT NULL,
    channel_created_at timestamp with time zone,
    initial_inventory_estimate jsonb,
    user_input text,
    user_work_id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_work_restored boolean DEFAULT false NOT NULL,
    CONSTRAINT channels_avatar_url_bound_check CHECK (((avatar_url IS NULL) OR (char_length(avatar_url) <= 2048))),
    CONSTRAINT channels_channel_metadata_error_code_check CHECK (((channel_metadata_error IS NULL) OR (channel_metadata_error ~ '^[a-z0-9_]{1,64}$'::text))),
    CONSTRAINT channels_deep_bypass_reason_code_check CHECK (((deep_bypass_reason IS NULL) OR (deep_bypass_reason ~ '^[a-z0-9_]{1,64}$'::text))),
    CONSTRAINT channels_deep_inventory_error_code_check CHECK (((deep_inventory_error IS NULL) OR (deep_inventory_error ~ '^[a-z0-9_]{1,64}$'::text))),
    CONSTRAINT channels_initial_long_inventory_error_code_check CHECK (((initial_long_inventory_error_code IS NULL) OR (initial_long_inventory_error_code ~ '^[a-z0-9_]{1,64}$'::text))),
    CONSTRAINT channels_initial_long_inventory_error_count_check CHECK ((initial_long_inventory_error_count >= 0)),
    CONSTRAINT channels_title_bound_check CHECK (((title IS NULL) OR (char_length(title) <= 1000))),
    CONSTRAINT channels_user_input_safe CHECK (((user_input IS NULL) OR ((length(user_input) >= 1) AND (length(user_input) <= 2048) AND (user_input ~ '^https://www\.youtube\.com/(channel/UC[A-Za-z0-9_-]{22}|watch\?v=[A-Za-z0-9_-]{11}|@([A-Za-z0-9_.-]|%[89A-F][0-9A-F])+)$'::text))))
);

CREATE VIEW public.channel_collection_control WITH (security_invoker='true') AS
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

CREATE SEQUENCE public.channel_collection_epoch_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.channel_collection_epoch_seq OWNED BY public.channel_collection_state.control_epoch;

CREATE TABLE public.channel_playlist_state (
    channel_id text NOT NULL,
    video_type text NOT NULL,
    playlist_id text NOT NULL,
    runtime_enabled boolean DEFAULT false NOT NULL,
    status text DEFAULT 'paused'::text NOT NULL,
    next_due_at timestamp with time zone,
    priority integer DEFAULT 0 NOT NULL,
    no_new_streak integer DEFAULT 0 NOT NULL,
    active_scan_id uuid,
    resume_page_token text,
    resume_started_at timestamp with time zone,
    scan_boundary_at timestamp with time zone,
    source_next_after timestamp with time zone,
    source_checked_at timestamp with time zone,
    lease_token uuid,
    lease_owner text,
    lease_run_id text,
    lease_expires_at timestamp with time zone,
    last_claim_run_id text,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_error_code text,
    last_completion_attempt_id uuid,
    last_failure_attempt_id uuid,
    state_origin text DEFAULT 'typed_native'::text NOT NULL,
    legacy_source_status text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    inventory_status text DEFAULT 'not_started'::text NOT NULL,
    product_exposure_ready boolean DEFAULT false NOT NULL,
    expected_item_count bigint,
    last_completed_scan_id uuid,
    inventory_completed_at timestamp with time zone,
    inventory_last_error_code text,
    inventory_last_error_at timestamp with time zone,
    inventory_error_count integer DEFAULT 0 NOT NULL,
    inventory_state_origin text DEFAULT 'typed_native'::text NOT NULL,
    inventory_created_at timestamp with time zone DEFAULT now() NOT NULL,
    inventory_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_result text,
    discovery_started_at timestamp with time zone,
    discovery_error text,
    discovery_error_count integer,
    resume_pending boolean GENERATED ALWAYS AS ((resume_page_token IS NOT NULL)) STORED,
    lease_channel_epoch bigint,
    dispatch_ordinal bigint DEFAULT nextval('operator_private.inventory_dispatch_seq'::regclass) NOT NULL,
    CONSTRAINT channel_playlist_discovery_error_check CHECK (((discovery_error IS NULL) OR (discovery_error ~ '^[A-Za-z0-9_]{1,120}$'::text))),
    CONSTRAINT channel_playlist_dispatch_ordinal_check CHECK ((dispatch_ordinal > 0)),
    CONSTRAINT channel_playlist_inventory_active_scan_check CHECK (((active_scan_id IS NULL) OR (inventory_status = ANY (ARRAY['running'::text, 'completed'::text])))),
    CONSTRAINT channel_playlist_inventory_error_code_check CHECK (((inventory_last_error_code IS NULL) OR (inventory_last_error_code ~ '^[a-z0-9_]{1,64}$'::text))),
    CONSTRAINT channel_playlist_inventory_error_count_check CHECK ((inventory_error_count >= 0)),
    CONSTRAINT channel_playlist_inventory_expected_count_check CHECK (((expected_item_count IS NULL) OR (expected_item_count >= 0))),
    CONSTRAINT channel_playlist_inventory_exposure_check CHECK (((NOT product_exposure_ready) OR (inventory_status = 'completed'::text))),
    CONSTRAINT channel_playlist_inventory_native_completion_check CHECK (((inventory_state_origin = 'legacy_long_backfill'::text) OR (inventory_status <> 'completed'::text) OR (last_completed_scan_id IS NOT NULL))),
    CONSTRAINT channel_playlist_inventory_origin_check CHECK ((inventory_state_origin = ANY (ARRAY['typed_native'::text, 'legacy_long_backfill'::text]))),
    CONSTRAINT channel_playlist_inventory_status_check CHECK ((inventory_status = ANY (ARRAY['not_started'::text, 'pending'::text, 'running'::text, 'completed'::text, 'manual_required'::text, 'playlist_not_found'::text, 'error'::text]))),
    CONSTRAINT channel_playlist_state_lease_channel_epoch_check CHECK ((lease_channel_epoch > 0)),
    CONSTRAINT channel_playlist_state_runtime_scope_check CHECK (((NOT runtime_enabled) OR (status = 'paused'::text) OR (next_due_at IS NOT NULL))),
    CONSTRAINT channel_video_discovery_state_attempt_check CHECK ((attempt_count >= 0)),
    CONSTRAINT channel_video_discovery_state_error_code_check CHECK (((last_error_code IS NULL) OR (last_error_code ~ '^[A-Za-z0-9_]{1,120}$'::text))),
    CONSTRAINT channel_video_discovery_state_inactive_check CHECK ((runtime_enabled OR (status = 'paused'::text))),
    CONSTRAINT channel_video_discovery_state_lease_check CHECK ((((status = 'leased'::text) AND runtime_enabled AND (lease_token IS NOT NULL) AND (lease_owner IS NOT NULL) AND (lease_run_id IS NOT NULL) AND (lease_expires_at IS NOT NULL)) OR ((status <> 'leased'::text) AND (lease_token IS NULL) AND (lease_owner IS NULL) AND (lease_run_id IS NULL) AND (lease_expires_at IS NULL)))),
    CONSTRAINT channel_video_discovery_state_legacy_status_check CHECK (((legacy_source_status IS NULL) OR (legacy_source_status = ANY (ARRAY['ready'::text, 'leased'::text, 'paused'::text])))),
    CONSTRAINT channel_video_discovery_state_no_new_check CHECK ((no_new_streak >= 0)),
    CONSTRAINT channel_video_discovery_state_origin_check CHECK ((state_origin = ANY (ARRAY['typed_native'::text, 'legacy_long_backfill'::text]))),
    CONSTRAINT channel_video_discovery_state_playlist_check CHECK ((((video_type = 'long'::text) AND ("left"(playlist_id, 4) = 'UULF'::text)) OR ((video_type = 'short'::text) AND ("left"(playlist_id, 4) = 'UUSH'::text)) OR ((video_type = 'stream'::text) AND ("left"(playlist_id, 4) = 'UULV'::text)))),
    CONSTRAINT channel_video_discovery_state_resume_token_check CHECK (((resume_page_token IS NULL) OR ((length(resume_page_token) >= 1) AND (length(resume_page_token) <= 2048)))),
    CONSTRAINT channel_video_discovery_state_status_check CHECK ((status = ANY (ARRAY['ready'::text, 'leased'::text, 'paused'::text]))),
    CONSTRAINT channel_video_discovery_state_video_type_check CHECK ((video_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text])))
);

CREATE VIEW public.channel_discovery_queue WITH (security_invoker='true') AS
 SELECT channel_id,
    next_due_at,
    priority,
    status,
    resume_pending,
    no_new_streak,
    source_next_after,
    source_checked_at,
    lease_token,
    lease_owner,
    lease_run_id,
    lease_expires_at,
    last_claim_run_id,
    attempt_count,
    last_error_code AS last_error_type,
    last_completion_attempt_id,
    last_failure_attempt_id,
    updated_at
   FROM public.channel_playlist_state
  WHERE (video_type = 'long'::text);

CREATE TABLE public.channel_discovery_queue_control (
    singleton boolean DEFAULT true NOT NULL,
    reconciliation_required boolean DEFAULT true NOT NULL,
    last_reconciled_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT channel_discovery_queue_control_singleton_check CHECK (singleton)
);

CREATE TABLE public.channel_label_assignments (
    channel_id text NOT NULL,
    label_key text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.channel_labels (
    label_key text NOT NULL,
    name text NOT NULL,
    label_type text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT channel_labels_label_type_check CHECK ((label_type = ANY (ARRAY['preset'::text, 'custom'::text]))),
    CONSTRAINT channel_labels_name_check CHECK (((char_length(name) >= 1) AND (char_length(name) <= 40) AND (name !~ '[\u0001-\u001f\u007f]'::text)))
);

CREATE TABLE public.channel_snapshots (
    channel_id text NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    views bigint,
    subscribers bigint,
    video_count bigint,
    source text NOT NULL,
    CONSTRAINT channel_snapshots_source_known CHECK ((source = ANY (ARRAY['youtube_channel_metadata_refresh'::text, 'migration_seed_current_channel_row'::text]))),
    CONSTRAINT channel_snapshots_subscribers_nonnegative CHECK (((subscribers IS NULL) OR (subscribers >= 0))),
    CONSTRAINT channel_snapshots_video_count_nonnegative CHECK (((video_count IS NULL) OR (video_count >= 0))),
    CONSTRAINT channel_snapshots_views_nonnegative CHECK (((views IS NULL) OR (views >= 0)))
);

CREATE VIEW public.channel_video_inventory_state WITH (security_invoker='true') AS
 SELECT channel_id,
    video_type,
    playlist_id,
    inventory_status,
    product_exposure_ready,
    expected_item_count,
    active_scan_id,
    last_completed_scan_id,
    source_checked_at,
    inventory_completed_at,
    inventory_last_error_code AS last_error_code,
    inventory_last_error_at AS last_error_at,
    inventory_error_count AS error_count,
    inventory_state_origin AS state_origin,
    inventory_created_at AS created_at,
    inventory_updated_at AS updated_at
   FROM public.channel_playlist_state;

CREATE VIEW public.channels_with_control WITH (security_invoker='true') AS
 SELECT channel.channel_id,
    channel.title,
    channel.subscribers,
    channel.views,
    channel.video_count,
    channel.added_at,
    channel.avatar_url,
    channel.folder_id,
    channel.parent_channel_id,
    channel.uploads_playlist_id,
    channel.deep_bypass,
    channel.deep_bypass_reason,
    channel.deep_inventory_status,
    NULL::text AS deep_inventory_page_token,
    channel.deep_inventory_started_at,
    channel.deep_inventory_checked_at,
    channel.deep_inventory_error,
    channel.initial_long_inventory_status,
    channel.initial_28d_stats_status,
    COALESCE(control.is_hidden, true) AS is_hidden,
    control.hidden_at,
    channel.initial_long_inventory_started_at,
    channel.initial_28d_stats_started_at,
    channel.channel_metadata_checked_at,
    channel.channel_metadata_next_after,
    channel.channel_metadata_status,
    channel.channel_metadata_started_at,
    channel.channel_metadata_error,
    channel.channel_metadata_error_count,
    channel.channel_video_count_changed_at,
    channel.country_code,
    channel.country_collected_at,
    channel.initial_long_inventory_error_code,
    channel.initial_long_inventory_error_at,
    channel.initial_long_inventory_error_count,
    channel.channel_created_at,
    channel.initial_inventory_estimate,
    channel.user_input,
    channel.user_work_id,
    channel.user_work_restored,
    COALESCE(control.owner_permission, 'legacy_hold'::text) AS collection_permission,
    control.control_epoch AS collection_control_epoch,
    COALESCE(control.collection_mode, 'operator_hold'::text) AS collection_mode
   FROM (public.channels channel
     LEFT JOIN public.channel_collection_control control ON ((control.channel_id = channel.channel_id)));

CREATE TABLE public.folders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT folders_name_check CHECK (((char_length(name) >= 1) AND (char_length(name) <= 120) AND (name = btrim(name)) AND (name !~ '[\u0001-\u001f\u007f]'::text)))
);

CREATE TABLE public.video_inventory (
    video_id text NOT NULL,
    channel_id text NOT NULL,
    discovered_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    video_type text,
    published_at timestamp with time zone,
    skeleton_since timestamp with time zone,
    published_acquired_at timestamptz,
    CONSTRAINT video_inventory_skeleton_check CHECK (skeleton_since IS NULL OR
        (isfinite(skeleton_since) AND video_type IS NULL AND published_at IS NULL)),
    CONSTRAINT video_inventory_video_type_check CHECK ((video_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text])))
);

CREATE INDEX video_inventory_skeleton_channel_idx ON public.video_inventory (channel_id,video_id)
    WHERE skeleton_since IS NOT NULL;

CREATE TABLE public.video_inventory_memberships (
    video_id text NOT NULL,
    channel_id text NOT NULL,
    video_type text NOT NULL,
    playlist_id text NOT NULL,
    source_published_at timestamp with time zone,
    membership_status text DEFAULT 'present'::text NOT NULL,
    evidence_source text DEFAULT 'typed_playlist_scan'::text NOT NULL,
    first_seen_at timestamp with time zone,
    last_seen_at timestamp with time zone NOT NULL,
    last_present_at timestamp with time zone,
    absent_since timestamp with time zone,
    first_seen_scan_id uuid,
    last_seen_scan_id uuid,
    absence_confirmed_by_scan_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_inventory_memberships_absence_check CHECK ((((membership_status = 'present'::text) AND (absent_since IS NULL) AND (absence_confirmed_by_scan_id IS NULL)) OR ((membership_status = 'absent'::text) AND (absent_since IS NOT NULL) AND (absence_confirmed_by_scan_id IS NOT NULL)))),
    CONSTRAINT video_inventory_memberships_playlist_check CHECK ((((video_type = 'long'::text) AND ("left"(playlist_id, 4) = 'UULF'::text)) OR ((video_type = 'short'::text) AND ("left"(playlist_id, 4) = 'UUSH'::text)) OR ((video_type = 'stream'::text) AND ("left"(playlist_id, 4) = 'UULV'::text)))),
    CONSTRAINT video_inventory_memberships_seen_clock_check CHECK (((last_seen_at >= first_seen_at) AND (last_present_at >= first_seen_at))),
    CONSTRAINT video_inventory_memberships_source_check CHECK ((evidence_source = ANY (ARRAY['typed_playlist_scan'::text, 'legacy_inventory_backfill'::text, 'legacy_long_discovery'::text]))),
    CONSTRAINT video_inventory_memberships_status_check CHECK ((membership_status = ANY (ARRAY['present'::text, 'absent'::text]))),
    CONSTRAINT video_inventory_memberships_video_type_check CHECK ((video_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text])))
);

CREATE TABLE public.video_type_review_cases (
    video_id text,
    channel_id text,
    observed_canonical_type text,
    observed_types text[] DEFAULT ARRAY[]::text[] NOT NULL,
    observed_channels text[] DEFAULT ARRAY[]::text[] NOT NULL,
    state text NOT NULL,
    revision bigint DEFAULT 1 NOT NULL,
    first_seen_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    blocked_reason text,
    decision_type text,
    decision_reason text,
    decision_at timestamp with time zone,
    decision_revision bigint,
    case_id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_detached_at timestamp with time zone,
    user_channel_ref uuid,
    restore_owner_ref uuid,
    evidence_expires_at timestamptz,
    CONSTRAINT video_type_review_cases_blocked_reason_check CHECK ((blocked_reason = 'channel_conflict'::text)),
    CONSTRAINT video_type_review_cases_check CHECK ((((decision_type IS NULL) AND (decision_reason IS NULL) AND (decision_at IS NULL) AND (decision_revision IS NULL)) OR ((decision_type IS NOT NULL) AND (decision_reason IS NOT NULL) AND (decision_at IS NOT NULL) AND ((decision_revision >= 2) AND (decision_revision <= revision)) AND ((char_length(decision_reason) >= 1) AND (char_length(decision_reason) <= 1000))))),
    CONSTRAINT video_type_review_cases_check1 CHECK (((state <> 'resolved'::text) OR ((decision_type IS NOT NULL) AND (blocked_reason IS NULL)))),
    CONSTRAINT video_type_review_cases_decision_reason_check CHECK (((decision_reason IS NULL) OR ((char_length(decision_reason) >= 1) AND (char_length(decision_reason) <= 1000) AND (decision_reason !~ '[\u0001-\u001f\u007f]'::text)))),
    CONSTRAINT video_type_review_cases_decision_type_check CHECK ((decision_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text]))),
    CONSTRAINT video_type_review_cases_observed_canonical_type_check CHECK ((observed_canonical_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text]))),
    CONSTRAINT video_type_review_cases_one_owner_check CHECK (((user_channel_ref IS NULL) OR (restore_owner_ref IS NULL))),
    CONSTRAINT video_type_review_cases_revision_check CHECK (((revision >= 1) AND (revision <= '9007199254740991'::bigint))),
    CONSTRAINT video_type_review_cases_state_check CHECK ((state = ANY (ARRAY['open'::text, 'resolved'::text]))),
    CONSTRAINT video_type_review_source_lifecycle CHECK ((((video_id IS NOT NULL) AND (channel_id IS NOT NULL) AND (source_detached_at IS NULL)) OR ((video_id IS NULL) AND (channel_id IS NULL) AND (source_detached_at IS NOT NULL) AND (observed_canonical_type IS NULL) AND (cardinality(observed_types) = 0) AND (cardinality(observed_channels) = 0) AND (blocked_reason IS NULL))))
);

CREATE VIEW public.video_inventory_type_resolution WITH (security_invoker='true', security_barrier='true') AS
 WITH evidence AS (
         SELECT i.video_id,
            i.channel_id AS canonical_channel_id,
            i.video_type AS canonical_video_type,
            COALESCE(array_agg(DISTINCT m.video_type ORDER BY m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_types,
            COALESCE(array_agg(DISTINCT m.channel_id ORDER BY m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_channels,
            count(DISTINCT m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_type_count,
            count(DISTINCT m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_channel_count,
            COALESCE(bool_or(((m.membership_status = 'present'::text) AND (m.channel_id = i.channel_id) AND (m.video_type = i.video_type))), false) AS canonical_membership_supported
           FROM (public.video_inventory i
             LEFT JOIN public.video_inventory_memberships m ON ((m.video_id = i.video_id)))
          GROUP BY i.video_id, i.channel_id, i.video_type
        ), resolved AS (
         SELECT e.video_id,
            e.canonical_channel_id,
            e.canonical_video_type,
            e.present_membership_types,
            e.present_membership_channels,
            e.present_type_count,
            e.present_channel_count,
            e.canonical_membership_supported,
            COALESCE(((c.decision_type = e.canonical_video_type) AND (c.channel_id = e.canonical_channel_id) AND (c.blocked_reason IS NULL) AND (NOT (EXISTS ( SELECT 1
                   FROM unnest(e.present_membership_channels) x(id)
                  WHERE (x.id <> e.canonical_channel_id))))), false) AS manual_valid
           FROM (evidence e
             LEFT JOIN public.video_type_review_cases c ON ((c.video_id = e.video_id)))
        )
 SELECT video_id,
    canonical_channel_id,
    canonical_video_type,
    present_membership_types,
    present_membership_channels,
    present_type_count,
    present_channel_count,
    canonical_membership_supported,
        CASE
            WHEN manual_valid THEN 'resolved'::text
            WHEN (present_type_count = 0) THEN 'unverified'::text
            WHEN ((present_type_count > 1) OR (present_channel_count > 1)) THEN 'conflict'::text
            WHEN (NOT canonical_membership_supported) THEN 'canonical_unsupported'::text
            ELSE 'resolved'::text
        END AS resolution_status,
    manual_valid
   FROM resolved;

CREATE TABLE public.video_availability_incidents (
    id bigint NOT NULL,
    video_id text NOT NULL,
    channel_id text,
    first_status text,
    latest_status text,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    attempt_count integer DEFAULT 1 NOT NULL,
    source_first_seen text,
    source_latest_seen text,
    restored_at timestamp with time zone,
    restored_status text,
    source_restored text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE VIEW public.open_video_availability WITH (security_invoker='true') AS
 SELECT video_id,
    channel_id,
    COALESCE(latest_status, 'unknown_unavailable'::text) AS availability_status,
    first_seen_at,
    last_seen_at,
    attempt_count
   FROM public.video_availability_incidents
  WHERE (restored_at IS NULL);

CREATE TABLE public.operator_job_targets (
    job_id uuid NOT NULL,
    video_id text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    job_attempts smallint DEFAULT 0 NOT NULL,
    result_code text,
    completed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    video_type text DEFAULT 'long'::text,
    CONSTRAINT operator_job_targets_attempts_check CHECK ((job_attempts >= 0)),
    CONSTRAINT operator_job_targets_result_code_check CHECK (((result_code IS NULL) OR ((btrim(result_code) <> ''::text) AND (char_length(result_code) <= 120)))),
    CONSTRAINT operator_job_targets_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'updated'::text, 'skipped'::text, 'unavailable'::text, 'error'::text, 'checked'::text]))),
    CONSTRAINT operator_job_targets_type_check CHECK ((video_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text]))),
    CONSTRAINT operator_job_targets_unresolved_check CHECK ((((video_type IS NOT NULL) OR ((status = 'pending'::text) AND (job_attempts = 0) AND (result_code IS NULL) AND (completed_at IS NULL))) IS TRUE))
);

CREATE TABLE public.saved_collection_commands (
    collection_id uuid NOT NULL,
    request_id uuid NOT NULL,
    request_fingerprint text NOT NULL,
    action text NOT NULL,
    requested_count integer NOT NULL,
    changed_count integer NOT NULL,
    applied_revision bigint NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    collection_kind text DEFAULT 'collection'::text NOT NULL,
    CONSTRAINT saved_collection_commands_action_check CHECK ((action = ANY (ARRAY['add'::text, 'remove'::text]))),
    CONSTRAINT saved_collection_commands_applied_revision_check CHECK ((applied_revision > 1)),
    CONSTRAINT saved_collection_commands_changed_count_check CHECK (((changed_count >= 0) AND (changed_count <= 500))),
    CONSTRAINT saved_collection_commands_collection_kind_check CHECK ((collection_kind = 'collection'::text)),
    CONSTRAINT saved_collection_commands_request_fingerprint_check CHECK ((request_fingerprint ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT saved_collection_commands_requested_count_check CHECK (((requested_count >= 1) AND (requested_count <= 500)))
);

CREATE TABLE public.saved_collection_members (
    collection_id uuid NOT NULL,
    video_id text NOT NULL,
    added_at timestamp with time zone NOT NULL,
    collection_kind text DEFAULT 'collection'::text NOT NULL,
    user_channel_ref uuid,
    restore_owner_ref uuid,
    CONSTRAINT saved_collection_members_collection_kind_check CHECK ((collection_kind = 'collection'::text)),
    CONSTRAINT saved_collection_members_one_owner_check CHECK (((user_channel_ref IS NULL) OR (restore_owner_ref IS NULL))),
    CONSTRAINT saved_collection_members_video_id_check CHECK ((video_id ~ '^[A-Za-z0-9_-]{11}$'::text))
);

CREATE TABLE public.saved_objects (
    kind text NOT NULL,
    id uuid NOT NULL,
    name text,
    channel_id text,
    conditions jsonb,
    create_fingerprint text NOT NULL,
    revision bigint DEFAULT 1 NOT NULL,
    member_count bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    updated_at timestamp with time zone DEFAULT clock_timestamp() NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT saved_objects_create_fingerprint_check CHECK ((create_fingerprint ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT saved_objects_id_check CHECK (((id)::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)),
    CONSTRAINT saved_objects_kind_check CHECK ((kind = ANY (ARRAY['view'::text, 'collection'::text]))),
    CONSTRAINT saved_objects_lifecycle CHECK (((((deleted_at IS NULL) AND (name IS NOT NULL) AND (name = btrim(name)) AND ((char_length(name) >= 1) AND (char_length(name) <= 120)) AND (name !~ '[[:cntrl:]]'::text) AND (((kind = 'collection'::text) AND (channel_id IS NULL) AND (conditions IS NULL)) OR ((kind = 'view'::text) AND (member_count = 0) AND (channel_id IS NOT NULL) AND (conditions IS NOT NULL) AND (channel_id ~ '^UC[A-Za-z0-9_-]{22}$'::text) AND (jsonb_typeof(conditions) = 'object'::text) AND ((conditions -> 'version'::text) = '1'::jsonb) AND (jsonb_typeof((conditions -> 'filters'::text)) = 'object'::text) AND (jsonb_typeof((conditions -> 'facets'::text)) = 'object'::text) AND (octet_length((conditions)::text) <= 32768)))) OR ((deleted_at IS NOT NULL) AND (name IS NULL) AND (channel_id IS NULL) AND (conditions IS NULL) AND (member_count = 0))) IS TRUE)),
    CONSTRAINT saved_objects_member_count_check CHECK ((member_count >= 0)),
    CONSTRAINT saved_objects_revision_check CHECK (((revision >= 1) AND (revision <= '9007199254740991'::bigint)))
);

CREATE TABLE public.sync_runs (
    id bigint NOT NULL,
    run_type text DEFAULT 'auto_sync'::text NOT NULL,
    status text DEFAULT 'running'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    duration_seconds double precision,
    errors integer DEFAULT 0 NOT NULL,
    metadata_processed integer DEFAULT 0 NOT NULL,
    metadata_updated integer DEFAULT 0 NOT NULL,
    video_count_changed integer DEFAULT 0 NOT NULL,
    discovery_processed integer DEFAULT 0 NOT NULL,
    new_videos integer DEFAULT 0 NOT NULL,
    new_video_stats_updated integer DEFAULT 0 NOT NULL,
    regular_stats_requested integer DEFAULT 0 NOT NULL,
    regular_stats_updated integer DEFAULT 0 NOT NULL,
    stats_updated integer DEFAULT 0 NOT NULL,
    unavailable_marked integer DEFAULT 0 NOT NULL,
    missing integer DEFAULT 0 NOT NULL,
    summary_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.sync_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sync_runs_id_seq OWNED BY public.sync_runs.id;

CREATE TABLE public.typed_inventory_receipts (
    receipt_scope text NOT NULL,
    transition_id uuid NOT NULL,
    scan_id uuid,
    transition_kind text,
    claims jsonb,
    source_checked_at timestamp with time zone,
    result jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    next_page_token_hash bytea,
    CONSTRAINT typed_inventory_receipt_token_hash_check CHECK (((next_page_token_hash IS NULL) OR ((octet_length(next_page_token_hash) = 32) AND (receipt_scope = 'scan_transition'::text) AND (transition_kind = 'page'::text)))),
    CONSTRAINT typed_inventory_receipts_result_check CHECK ((jsonb_typeof(result) = 'object'::text)),
    CONSTRAINT typed_inventory_receipts_scope_check CHECK (((((receipt_scope = 'scan_transition'::text) AND (scan_id IS NOT NULL) AND (transition_kind = ANY (ARRAY['page'::text, 'failure'::text])) AND (claims IS NULL)) OR ((receipt_scope = 'quota_defer'::text) AND (scan_id IS NULL) AND (transition_kind IS NULL) AND (((jsonb_typeof(claims) = 'array'::text) AND ((jsonb_array_length(claims) >= 1) AND (jsonb_array_length(claims) <= 500)) AND (source_checked_at IS NOT NULL)) OR ((claims IS NULL) AND (result = '{"source_expired": true}'::jsonb))))) IS TRUE))
);

CREATE TABLE public.typed_video_collection_receipts (
    video_id text NOT NULL,
    collection_attempt_id uuid NOT NULL,
    channel_id text NOT NULL,
    video_type text,
    lease_token uuid NOT NULL,
    receipt_kind text NOT NULL,
    recorded_at timestamp with time zone NOT NULL,
    payload_fingerprint text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT typed_video_collection_receipts_fingerprint_check CHECK ((payload_fingerprint ~ '^[0-9a-f]{32}$'::text)),
    CONSTRAINT typed_video_collection_receipts_kind_check CHECK ((receipt_kind = ANY (ARRAY['metadata'::text, 'stream_probe'::text, 'missing'::text, 'identity_refresh'::text]))),
    CONSTRAINT typed_video_collection_receipts_type_check CHECK ((((receipt_kind = 'identity_refresh' AND video_type IS NULL) OR (receipt_kind <> 'identity_refresh' AND video_type IS NOT NULL AND ((video_type = ANY (ARRAY['short'::text, 'stream'::text])) OR ((video_type = 'long'::text) AND (receipt_kind = 'missing'::text)))))) IS TRUE)
);

CREATE TABLE public.typed_video_collection_runtime (
    video_type text NOT NULL,
    runtime_enabled boolean DEFAULT false NOT NULL,
    paused_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    reconcile_queue_cursor text,
    reconcile_inventory_cursor text,
    CONSTRAINT typed_video_collection_runtime_type_check CHECK ((video_type = ANY (ARRAY['short'::text, 'stream'::text])))
);

CREATE TABLE public.user_channel_restore_items (
    id uuid NOT NULL,
    user_input text,
    folder_id uuid,
    parent_ref uuid,
    is_hidden boolean NOT NULL,
    added_at timestamp with time zone,
    label_keys text[] DEFAULT ARRAY[]::text[] NOT NULL,
    saved_views jsonb DEFAULT '[]'::jsonb NOT NULL,
    revision bigint DEFAULT 1 NOT NULL,
    hidden_at timestamp with time zone,
    CONSTRAINT user_channel_restore_items_added_at_check CHECK (((added_at IS NULL) OR isfinite(added_at))),
    CONSTRAINT user_channel_restore_items_check CHECK (((parent_ref IS NULL) OR (parent_ref <> id))),
    CONSTRAINT user_channel_restore_items_hidden_at_check CHECK (((hidden_at IS NULL) OR (is_hidden AND isfinite(hidden_at)))),
    CONSTRAINT user_channel_restore_items_id_check CHECK (((id)::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)),
    CONSTRAINT user_channel_restore_items_label_keys_check CHECK (((cardinality(label_keys) <= 1001) AND (array_position(label_keys, NULL::text) IS NULL))),
    CONSTRAINT user_channel_restore_items_revision_check CHECK (((revision >= 1) AND (revision <= '9007199254740991'::bigint))),
    CONSTRAINT user_channel_restore_items_saved_views_check CHECK (((jsonb_typeof(saved_views) = 'array'::text) AND (jsonb_array_length(saved_views) <= 1000))),
    CONSTRAINT user_channel_restore_items_user_input_check CHECK (((user_input IS NULL) OR ((length(user_input) <= 2048) AND (user_input ~ '^https://www\.youtube\.com/(channel/UC[A-Za-z0-9_-]{22}|watch\?v=[A-Za-z0-9_-]{11}|@([A-Za-z0-9_.-]|%[89A-F][0-9A-F])+)$'::text)))),
    reconnect_state text NOT NULL DEFAULT 'idle',
    reconnect_revision bigint,
    reconnect_requested_at timestamptz,
    reconnect_not_before timestamptz,
    reconnect_last_attempt_at timestamptz,
    reconnect_attempt_count integer NOT NULL DEFAULT 0,
    reconnect_error_code text,
    reconnect_lease_token uuid,
    reconnect_lease_expires_at timestamptz,
    CONSTRAINT user_channel_restore_items_reconnect_state_check CHECK
  (reconnect_state IN ('idle','queued','running','waiting','blocked')),
    CONSTRAINT user_channel_restore_items_reconnect_intent_check CHECK
  (((reconnect_state='idle' AND reconnect_revision IS NULL AND reconnect_requested_at IS NULL
     AND reconnect_not_before IS NULL AND reconnect_last_attempt_at IS NULL
     AND reconnect_attempt_count=0 AND reconnect_error_code IS NULL)
    OR (reconnect_state<>'idle' AND reconnect_revision=revision AND NOT is_hidden
     AND reconnect_requested_at IS NOT NULL AND isfinite(reconnect_requested_at))) IS TRUE),
    CONSTRAINT user_channel_restore_items_reconnect_lease_check CHECK
  (((reconnect_state='running' AND reconnect_lease_token IS NOT NULL
     AND reconnect_lease_expires_at IS NOT NULL AND isfinite(reconnect_lease_expires_at))
    OR (reconnect_state<>'running' AND reconnect_lease_token IS NULL AND reconnect_lease_expires_at IS NULL)) IS TRUE),
    CONSTRAINT user_channel_restore_items_reconnect_wait_check CHECK
  (((reconnect_state='waiting' AND reconnect_not_before IS NOT NULL AND isfinite(reconnect_not_before))
    OR (reconnect_state<>'waiting' AND reconnect_not_before IS NULL)) IS TRUE),
    CONSTRAINT user_channel_restore_items_reconnect_attempt_check CHECK
  (reconnect_attempt_count>=0 AND (reconnect_last_attempt_at IS NULL OR isfinite(reconnect_last_attempt_at))),
    CONSTRAINT user_channel_restore_items_reconnect_error_check CHECK
  (reconnect_error_code IS NULL OR reconnect_error_code ~ '^[a-z0-9_]{1,120}$')
);

CREATE TABLE public.video_availability_events (
    id bigint NOT NULL,
    video_id text NOT NULL,
    channel_id text,
    event_type text NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL,
    source text,
    from_status text,
    to_status text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_availability_events_event_type_check CHECK ((event_type = ANY (ARRAY['unavailable_detected'::text, 'unavailable_status_changed'::text, 'restored'::text])))
);

ALTER TABLE public.video_availability_events ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.video_availability_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

ALTER TABLE public.video_availability_incidents ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.video_availability_incidents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.video_collection_cadence (
    collection_kind text NOT NULL,
    availability_class text NOT NULL,
    min_age_seconds bigint NOT NULL,
    max_age_seconds bigint,
    cadence_seconds bigint NOT NULL,
    policy_version integer DEFAULT 1 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    video_type text DEFAULT 'long'::text NOT NULL,
    age_bucket text,
    jitter_window_seconds integer NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT video_collection_cadence_hot_maximum CHECK ((cadence_seconds <= 2419200)),
    CONSTRAINT video_collection_cadence_kind_check CHECK ((collection_kind = ANY (ARRAY['counters'::text, 'metadata'::text]))),
    CONSTRAINT video_collection_cadence_seconds_check CHECK ((cadence_seconds > 0)),
    CONSTRAINT video_collection_cadence_type_policy_check CHECK ((((video_type = 'long'::text) AND (availability_class = ANY (ARRAY['public'::text, 'unlisted'::text])) AND (age_bucket IS NULL) AND (jitter_window_seconds = 0) AND (min_age_seconds >= 0) AND ((max_age_seconds IS NULL) OR (max_age_seconds > min_age_seconds))) OR ((video_type = ANY (ARRAY['short'::text, 'stream'::text])) AND (collection_kind = 'metadata'::text) AND (availability_class = 'all'::text) AND (age_bucket IS NOT NULL) AND (age_bucket = ANY (ARRAY['age_0_90d'::text, 'age_91d_plus'::text])) AND (min_age_seconds >= 0) AND ((min_age_seconds % (86400)::bigint) = 0) AND ((min_age_seconds / 86400) <= 2147483647) AND ((max_age_seconds IS NULL) OR ((max_age_seconds >= min_age_seconds) AND ((max_age_seconds % (86400)::bigint) = 0) AND ((max_age_seconds / 86400) <= 2147483647))) AND (cadence_seconds = ANY (ARRAY[(604800)::bigint, (2419200)::bigint])) AND ((jitter_window_seconds >= 0) AND (jitter_window_seconds <= cadence_seconds)) AND (created_at IS NOT NULL) AND (updated_at IS NOT NULL)))),
    CONSTRAINT video_collection_cadence_version_check CHECK ((policy_version > 0))
);

CREATE TABLE public.video_collection_control (
    singleton boolean DEFAULT true NOT NULL,
    reconciliation_required boolean DEFAULT true NOT NULL,
    invalidated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_reconciled_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT video_collection_control_singleton_check CHECK (singleton)
);

CREATE TABLE public.video_snapshots (
    id bigint NOT NULL,
    video_id text NOT NULL,
    views bigint,
    recorded_at timestamp with time zone,
    like_count bigint,
    comment_count bigint,
    collection_attempt_id uuid
);

ALTER TABLE public.video_snapshots ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.video_snapshots_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.video_type_review_history (
    revision bigint NOT NULL,
    video_type text NOT NULL,
    reason text NOT NULL,
    decided_at timestamp with time zone NOT NULL,
    request_id uuid NOT NULL,
    expected_revision bigint NOT NULL,
    case_id uuid NOT NULL,
    CONSTRAINT video_type_review_history_reason_check CHECK (((char_length(reason) >= 1) AND (char_length(reason) <= 1000) AND (reason !~ '[\u0001-\u001f\u007f]'::text))),
    CONSTRAINT video_type_review_history_video_type_check CHECK ((video_type = ANY (ARRAY['long'::text, 'short'::text, 'stream'::text])))
);

CREATE TABLE public.videos (
    video_id text NOT NULL,
    channel_id text NOT NULL,
    title text,
    views bigint,
    checked_at timestamp with time zone,
    published_at timestamp with time zone,
    duration text,
    comment_count bigint,
    tags text[],
    availability_status text,
    privacy_status text,
    upload_status text,
    availability_checked_at timestamp with time zone,
    like_count bigint,
    category_id text,
    metadata_checked_at timestamp with time zone,
    youtube_topic_categories text[],
    default_audio_language text,
    default_language text,
    caption_available boolean,
    stream_actual_end_at timestamp with time zone,
    CONSTRAINT videos_default_audio_language_bound_check CHECK (((default_audio_language IS NULL) OR (char_length(default_audio_language) <= 64))),
    CONSTRAINT videos_default_language_bound_check CHECK (((default_language IS NULL) OR (char_length(default_language) <= 64))),
    CONSTRAINT videos_duration_bound_check CHECK (((duration IS NULL) OR (char_length(duration) <= 64))),
    CONSTRAINT videos_stream_actual_end_requires_metadata_clock CHECK (((stream_actual_end_at IS NULL) OR (metadata_checked_at IS NOT NULL))),
    CONSTRAINT videos_tags_bound_check CHECK (((tags IS NULL) OR ((cardinality(tags) <= 500) AND (array_position(tags, NULL::text) IS NULL) AND (octet_length(array_to_string(tags, ''::text)) <= 25600)))),
    CONSTRAINT videos_title_bound_check CHECK (((title IS NULL) OR (char_length(title) <= 1000))),
    CONSTRAINT videos_youtube_topic_categories_bound_check CHECK (((youtube_topic_categories IS NULL) OR ((cardinality(youtube_topic_categories) <= 50) AND (array_position(youtube_topic_categories, NULL::text) IS NULL) AND (octet_length(array_to_string(youtube_topic_categories, ''::text)) <= 25600))))
);

CREATE VIEW public.hot_video_inventory WITH (security_invoker='true') AS
 SELECT inv.video_id,
    inv.channel_id,
    inv.video_type,
    proof.source_published_at AS published_at,
    proof.last_present_at AS source_checked_at
   FROM ((public.video_inventory inv
     JOIN public.video_inventory_type_resolution resolution ON (((resolution.video_id = inv.video_id) AND (resolution.resolution_status = 'resolved'::text) AND (resolution.canonical_video_type = inv.video_type) AND ((resolution.manual_valid IS TRUE) OR ((resolution.present_membership_types = ARRAY[inv.video_type]) AND (resolution.present_membership_channels = ARRAY[inv.channel_id]))))))
     LEFT JOIN LATERAL ( SELECT membership.source_published_at,
            membership.last_present_at
           FROM public.video_inventory_memberships membership
          WHERE ((membership.video_id = inv.video_id) AND (membership.channel_id = inv.channel_id) AND (membership.membership_status = 'present'::text) AND ((resolution.manual_valid IS TRUE) OR (membership.video_type = inv.video_type)) AND isfinite(membership.last_present_at) AND ((membership.last_present_at >= (statement_timestamp() - '696:00:00'::interval)) AND (membership.last_present_at <= statement_timestamp())))
          ORDER BY membership.last_present_at DESC, membership.video_type, membership.playlist_id
         LIMIT 1) membership_proof ON true)
     LEFT JOIN LATERAL (SELECT v.published_at AS source_published_at,v.metadata_checked_at AS last_present_at
       FROM public.videos v WHERE resolution.manual_valid IS TRUE AND v.video_id=inv.video_id
       AND v.channel_id=inv.channel_id AND isfinite(v.metadata_checked_at)
       AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
       LIMIT 1) manual_proof ON true
     CROSS JOIN LATERAL (SELECT coalesce(membership_proof.source_published_at,manual_proof.source_published_at) AS source_published_at,
       coalesce(membership_proof.last_present_at,manual_proof.last_present_at) AS last_present_at) proof
   WHERE inv.skeleton_since IS NULL AND proof.last_present_at IS NOT NULL;

CREATE TABLE public.youtube_api_request_attempts (
    request_attempt_id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id text NOT NULL,
    quota_bucket text NOT NULL,
    quota_day date DEFAULT ((now() AT TIME ZONE 'America/Los_Angeles'::text))::date NOT NULL,
    phase text NOT NULL,
    endpoint text NOT NULL,
    quota_units integer NOT NULL,
    status text DEFAULT 'started'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    duration_ms integer,
    http_status integer,
    error_type text,
    retry_of_request_attempt_id uuid,
    CONSTRAINT youtube_api_request_attempts_duration_ms_check CHECK (((duration_ms IS NULL) OR (duration_ms >= 0))),
    CONSTRAINT youtube_api_request_attempts_endpoint_check CHECK ((btrim(endpoint) <> ''::text)),
    CONSTRAINT youtube_api_request_attempts_error_type_check CHECK (((error_type IS NULL) OR (error_type ~ '^[A-Za-z][A-Za-z0-9_.:-]{0,119}$'::text))),
    CONSTRAINT youtube_api_request_attempts_http_status_check CHECK (((http_status IS NULL) OR ((http_status >= 100) AND (http_status <= 599)))),
    CONSTRAINT youtube_api_request_attempts_lifecycle_check CHECK ((((status = 'started'::text) AND (finished_at IS NULL)) OR ((status <> 'started'::text) AND (finished_at IS NOT NULL)))),
    CONSTRAINT youtube_api_request_attempts_phase_check CHECK ((btrim(phase) <> ''::text)),
    CONSTRAINT youtube_api_request_attempts_project_id_check CHECK ((btrim(project_id) <> ''::text)),
    CONSTRAINT youtube_api_request_attempts_quota_bucket_check CHECK ((btrim(quota_bucket) <> ''::text)),
    CONSTRAINT youtube_api_request_attempts_quota_units_check CHECK ((quota_units > 0)),
    CONSTRAINT youtube_api_request_attempts_status_check CHECK ((status = ANY (ARRAY['started'::text, 'success'::text, 'http_error'::text, 'transport_error'::text])))
);

ALTER TABLE ONLY public.channel_collection_state ALTER COLUMN control_epoch SET DEFAULT nextval('public.channel_collection_epoch_seq'::regclass);

ALTER TABLE ONLY public.sync_runs ALTER COLUMN id SET DEFAULT nextval('public.sync_runs_id_seq'::regclass);

ALTER TABLE ONLY operator_private.operator_job_retry_requests
    ADD CONSTRAINT operator_job_retry_requests_pkey PRIMARY KEY (job_id, retry_request_id);

ALTER TABLE ONLY public.channel_collection_state
    ADD CONSTRAINT channel_collection_state_pkey PRIMARY KEY (channel_id);

ALTER TABLE ONLY public.channel_collection_state
    ADD CONSTRAINT channel_collection_state_user_work_id_key UNIQUE (user_work_id);

ALTER TABLE ONLY public.channel_discovery_queue_control
    ADD CONSTRAINT channel_discovery_queue_control_pkey PRIMARY KEY (singleton);

ALTER TABLE ONLY public.channel_label_assignments
    ADD CONSTRAINT channel_label_assignments_pkey PRIMARY KEY (channel_id, label_key);

ALTER TABLE ONLY public.channel_labels
    ADD CONSTRAINT channel_labels_pkey PRIMARY KEY (label_key);

ALTER TABLE ONLY public.channel_snapshots
    ADD CONSTRAINT channel_snapshots_pkey PRIMARY KEY (channel_id, recorded_at);

ALTER TABLE ONLY public.channel_playlist_state
    ADD CONSTRAINT channel_video_discovery_state_pkey PRIMARY KEY (channel_id, video_type);

ALTER TABLE ONLY public.channel_video_inventory_scans
    ADD CONSTRAINT channel_video_inventory_scans_identity_uidx UNIQUE (scan_id, channel_id, video_type);

ALTER TABLE ONLY public.channel_video_inventory_scans
    ADD CONSTRAINT channel_video_inventory_scans_pkey PRIMARY KEY (scan_id);

ALTER TABLE ONLY public.channels
    ADD CONSTRAINT channels_pkey PRIMARY KEY (channel_id);

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.operator_job_targets
    ADD CONSTRAINT operator_job_targets_pkey PRIMARY KEY (job_id, video_id);

ALTER TABLE ONLY public.operator_jobs
    ADD CONSTRAINT operator_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.saved_collection_commands
    ADD CONSTRAINT saved_collection_commands_pkey PRIMARY KEY (collection_id, request_id);

ALTER TABLE ONLY public.saved_collection_members
    ADD CONSTRAINT saved_collection_members_pkey PRIMARY KEY (collection_id, video_id);

ALTER TABLE ONLY public.saved_objects
    ADD CONSTRAINT saved_objects_pkey PRIMARY KEY (kind, id);

ALTER TABLE ONLY public.sync_runs
    ADD CONSTRAINT sync_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.typed_inventory_receipts
    ADD CONSTRAINT typed_inventory_receipts_pkey PRIMARY KEY (receipt_scope, transition_id);

ALTER TABLE ONLY public.typed_video_collection_receipts
    ADD CONSTRAINT typed_video_collection_receipts_pkey PRIMARY KEY (video_id, collection_attempt_id);

ALTER TABLE ONLY public.typed_video_collection_runtime
    ADD CONSTRAINT typed_video_collection_runtime_pkey PRIMARY KEY (video_type);

ALTER TABLE ONLY public.user_channel_restore_items
    ADD CONSTRAINT user_channel_restore_items_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_availability_events
    ADD CONSTRAINT video_availability_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_availability_incidents
    ADD CONSTRAINT video_availability_incidents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_collection_cadence
    ADD CONSTRAINT video_collection_cadence_pkey PRIMARY KEY (video_type, collection_kind, availability_class, min_age_seconds, policy_version);

ALTER TABLE ONLY public.video_collection_control
    ADD CONSTRAINT video_collection_control_pkey PRIMARY KEY (singleton);

ALTER TABLE ONLY public.video_collection_state
    ADD CONSTRAINT video_collection_state_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.video_inventory_memberships
    ADD CONSTRAINT video_inventory_memberships_pkey PRIMARY KEY (video_id, channel_id, video_type);

ALTER TABLE ONLY public.video_inventory
    ADD CONSTRAINT video_inventory_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.video_snapshots
    ADD CONSTRAINT video_snapshots_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_type_review_cases
    ADD CONSTRAINT video_type_review_cases_pkey PRIMARY KEY (case_id);

ALTER TABLE ONLY public.video_type_review_cases
    ADD CONSTRAINT video_type_review_cases_video_id_key UNIQUE (video_id);

ALTER TABLE ONLY public.video_type_review_history
    ADD CONSTRAINT video_type_review_history_case_id_request_id_key UNIQUE (case_id, request_id);

ALTER TABLE ONLY public.video_type_review_history
    ADD CONSTRAINT video_type_review_history_pkey PRIMARY KEY (case_id, revision);

ALTER TABLE ONLY public.videos
    ADD CONSTRAINT videos_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.youtube_api_request_attempts
    ADD CONSTRAINT youtube_api_request_attempts_pkey PRIMARY KEY (request_attempt_id);

CREATE INDEX channel_collection_state_hidden_idx ON public.channel_collection_state USING btree (hidden_at, channel_id) WHERE is_hidden;

CREATE INDEX channel_collection_state_initial_job_idx ON public.channel_collection_state USING btree (initial_job_id) WHERE (initial_job_id IS NOT NULL);

CREATE INDEX channel_label_assignments_label_channel_idx ON public.channel_label_assignments USING btree (label_key, channel_id);

CREATE UNIQUE INDEX channel_labels_name_ci_key ON public.channel_labels USING btree (lower(name));

CREATE INDEX channel_playlist_dispatch_idx ON public.channel_playlist_state USING btree (dispatch_ordinal, channel_id, video_type) WHERE runtime_enabled;

CREATE INDEX channel_playlist_state_long_due_idx ON public.channel_playlist_state USING btree (resume_pending DESC, priority DESC, next_due_at, channel_id) WHERE ((video_type = 'long'::text) AND (status = ANY (ARRAY['ready'::text, 'leased'::text])));

CREATE INDEX channel_snapshots_known_subscribers_idx ON public.channel_snapshots USING btree (channel_id, recorded_at DESC) INCLUDE (subscribers, views) WHERE (subscribers IS NOT NULL);

CREATE INDEX channel_video_discovery_state_expired_lease_idx ON public.channel_playlist_state USING btree (lease_expires_at) WHERE (runtime_enabled AND (status = 'leased'::text));

CREATE INDEX channel_video_discovery_state_ready_idx ON public.channel_playlist_state USING btree (next_due_at, priority DESC, channel_id, video_type) WHERE (runtime_enabled AND (status = 'ready'::text));

CREATE INDEX channel_video_inventory_scans_channel_type_idx ON public.channel_video_inventory_scans USING btree (channel_id, video_type, started_at DESC);

CREATE INDEX channel_video_inventory_scans_earliest_clock_idx ON public.channel_video_inventory_scans USING btree ((least(COALESCE(started_at, '-infinity'::timestamp with time zone), COALESCE(scan_boundary_at, '-infinity'::timestamp with time zone), CASE WHEN (status = 'completed'::text) THEN COALESCE(completed_at, '-infinity'::timestamp with time zone) ELSE 'infinity'::timestamp with time zone END)), scan_id);

CREATE INDEX channel_video_inventory_scans_latest_clock_idx ON public.channel_video_inventory_scans USING btree ((greatest(COALESCE(started_at, '-infinity'::timestamp with time zone), COALESCE(scan_boundary_at, '-infinity'::timestamp with time zone), COALESCE(source_checked_at, '-infinity'::timestamp with time zone), CASE WHEN (status = 'completed'::text) THEN COALESCE(completed_at, '-infinity'::timestamp with time zone) ELSE '-infinity'::timestamp with time zone END)), scan_id);

CREATE UNIQUE INDEX channel_video_inventory_scans_one_running_uidx ON public.channel_video_inventory_scans USING btree (channel_id, video_type) WHERE (status = 'running'::text);

CREATE INDEX channel_video_inventory_scans_static_check_idx ON public.channel_video_inventory_scans USING btree (scan_id) WHERE (CASE WHEN (source_checked_at IS NULL) THEN (NOT COALESCE(((status = 'running'::text) AND (coverage = 'partial'::text) AND (pages_completed = 0) AND (items_seen = 0) AND (new_memberships_found = 0) AND (resume_page_token IS NULL) AND (last_transition_id IS NULL) AND (completed_at IS NULL)), false)) ELSE (source_checked_at < COALESCE(scan_boundary_at, 'infinity'::timestamp with time zone)) END);

CREATE INDEX channels_metadata_next_after_idx ON public.channels USING btree (channel_metadata_next_after) WHERE (is_hidden IS DISTINCT FROM true);

CREATE INDEX channels_trash_expiry_idx ON public.channels USING btree (hidden_at, channel_id) WHERE is_hidden;

CREATE UNIQUE INDEX channels_user_work_id_key ON public.channels USING btree (user_work_id);

CREATE INDEX channels_visible_idx ON public.channels USING btree (is_hidden, title);

CREATE INDEX hot_cleanup_channel_country_idx ON public.channels USING btree (COALESCE(country_collected_at, '-infinity'::timestamp with time zone), channel_id) WHERE (country_code IS NOT NULL);

CREATE INDEX hot_cleanup_channel_profile_idx ON public.channels USING btree ((
CASE
    WHEN (channel_metadata_status = 'ok'::text) THEN COALESCE(channel_metadata_checked_at, '-infinity'::timestamp with time zone)
    ELSE '-infinity'::timestamp with time zone
END), channel_id) WHERE ((title IS NOT NULL) OR (avatar_url IS NOT NULL) OR (subscribers IS NOT NULL) OR (views IS NOT NULL) OR (video_count IS NOT NULL) OR (channel_created_at IS NOT NULL) OR (uploads_playlist_id IS NOT NULL));

CREATE INDEX hot_cleanup_video_availability_idx ON public.videos USING btree (COALESCE(availability_checked_at, '-infinity'::timestamp with time zone), video_id) WHERE ((availability_status IS NOT NULL) OR (privacy_status IS NOT NULL) OR (upload_status IS NOT NULL));

CREATE INDEX hot_cleanup_video_counters_idx ON public.videos USING btree (COALESCE(checked_at, '-infinity'::timestamp with time zone), video_id) WHERE ((views IS NOT NULL) OR (like_count IS NOT NULL) OR (comment_count IS NOT NULL));

CREATE INDEX hot_cleanup_video_metadata_idx ON public.videos USING btree (COALESCE(metadata_checked_at, '-infinity'::timestamp with time zone), video_id) WHERE ((title IS NOT NULL) OR (published_at IS NOT NULL) OR (duration IS NOT NULL) OR (tags IS NOT NULL) OR (category_id IS NOT NULL) OR (youtube_topic_categories IS NOT NULL) OR (default_audio_language IS NOT NULL) OR (default_language IS NOT NULL) OR (caption_available IS NOT NULL) OR (stream_actual_end_at IS NOT NULL));

CREATE INDEX hot_evidence_availability_events_order_idx ON public.video_availability_events USING btree (COALESCE(observed_at, '-infinity'::timestamp with time zone), lpad((id)::text, 20, '0'::text));

CREATE INDEX hot_evidence_availability_incidents_order_idx ON public.video_availability_incidents USING btree (COALESCE(first_seen_at, '-infinity'::timestamp with time zone), lpad((id)::text, 20, '0'::text));

CREATE INDEX hot_evidence_channel_snapshots_order_idx ON public.channel_snapshots USING btree (COALESCE(recorded_at, '-infinity'::timestamp with time zone), channel_id);

CREATE INDEX hot_evidence_review_evidence_order_idx ON public.video_type_review_cases USING btree (COALESCE(first_seen_at, '-infinity'::timestamp with time zone), ((case_id)::text));

CREATE INDEX hot_evidence_typed_video_collection_receipts_order_idx ON public.typed_video_collection_receipts USING btree (COALESCE(recorded_at, '-infinity'::timestamp with time zone), (((video_id || ':'::text) || (collection_attempt_id)::text)));

CREATE INDEX hot_evidence_video_snapshots_order_idx ON public.video_snapshots USING btree (COALESCE(recorded_at, '-infinity'::timestamp with time zone), lpad((id)::text, 20, '0'::text));

CREATE INDEX idx_videos_published_at_desc ON public.videos USING btree (published_at DESC);

CREATE INDEX operator_job_targets_pending_idx ON public.operator_job_targets USING btree (job_id, status, video_id);

CREATE INDEX operator_job_targets_unresolved_idx ON public.operator_job_targets USING btree (job_id, video_id) WHERE (video_type IS NULL);

CREATE INDEX operator_jobs_channel_history_idx ON public.operator_jobs USING btree (channel_id, requested_at DESC);

CREATE INDEX operator_jobs_claim_idx ON public.operator_jobs USING btree (requested_at, id) WHERE (status = ANY (ARRAY['queued'::text, 'running'::text]));

CREATE UNIQUE INDEX operator_jobs_one_active_channel_job ON public.operator_jobs USING btree (channel_id) WHERE (status = ANY (ARRAY['queued'::text, 'running'::text]));

CREATE INDEX operator_jobs_terminal_cleanup_idx ON public.operator_jobs USING btree (finished_at, id) WHERE (status = ANY (ARRAY['completed'::text, 'blocked'::text, 'error'::text, 'suspended'::text]));

CREATE INDEX review_cases_user_channel_ref_idx ON public.video_type_review_cases USING btree (user_channel_ref) WHERE (user_channel_ref IS NOT NULL);

CREATE INDEX saved_collection_members_page_idx ON public.saved_collection_members USING btree (collection_id, added_at DESC, video_id DESC);

CREATE INDEX saved_members_user_channel_ref_idx ON public.saved_collection_members USING btree (user_channel_ref) WHERE (user_channel_ref IS NOT NULL);

CREATE INDEX saved_objects_active_created_idx ON public.saved_objects USING btree (kind, created_at DESC, id DESC) WHERE (deleted_at IS NULL);

CREATE INDEX sync_runs_retention_idx ON public.sync_runs USING btree (started_at, id);

CREATE INDEX sync_runs_started_at_idx ON public.sync_runs USING btree (started_at DESC);

CREATE INDEX sync_runs_status_idx ON public.sync_runs USING btree (status);

CREATE INDEX typed_inventory_receipt_token_hash_idx ON public.typed_inventory_receipts USING btree (scan_id, next_page_token_hash) WHERE ((receipt_scope = 'scan_transition'::text) AND (transition_kind = 'page'::text) AND (next_page_token_hash IS NOT NULL));

CREATE INDEX typed_inventory_receipts_expiry_idx ON public.typed_inventory_receipts USING btree ((CASE WHEN (result ? 'source_expired'::text) THEN CASE WHEN (next_page_token_hash IS NOT NULL) THEN '-infinity'::timestamp with time zone ELSE COALESCE(created_at, '-infinity'::timestamp with time zone) END ELSE COALESCE(source_checked_at, '-infinity'::timestamp with time zone) END), receipt_scope, transition_id);

CREATE INDEX typed_inventory_receipts_scan_idx ON public.typed_inventory_receipts USING btree (scan_id, created_at) WHERE (receipt_scope = 'scan_transition'::text);

CREATE INDEX user_channel_restore_parent_idx ON public.user_channel_restore_items USING btree (parent_ref) WHERE (parent_ref IS NOT NULL);

CREATE INDEX video_availability_events_channel_observed_idx ON public.video_availability_events USING btree (channel_id, observed_at DESC);

CREATE INDEX video_availability_events_type_observed_idx ON public.video_availability_events USING btree (event_type, observed_at DESC);

CREATE INDEX video_availability_events_video_observed_idx ON public.video_availability_events USING btree (video_id, observed_at DESC);

CREATE INDEX video_availability_incidents_channel_idx ON public.video_availability_incidents USING btree (channel_id, first_seen_at DESC);

CREATE UNIQUE INDEX video_availability_incidents_open_video_uidx ON public.video_availability_incidents USING btree (video_id) WHERE (restored_at IS NULL);

CREATE INDEX video_availability_incidents_restored_idx ON public.video_availability_incidents USING btree (restored_at DESC);

CREATE UNIQUE INDEX video_collection_cadence_typed_bucket_key ON public.video_collection_cadence USING btree (video_type, age_bucket) WHERE (video_type = ANY (ARRAY['short'::text, 'stream'::text]));

CREATE INDEX video_collection_state_retention_evidence_idx ON public.video_collection_state (video_id)
    WHERE typed_collection_checked_at IS NOT NULL OR typed_source_published_at IS NOT NULL OR typed_stream_actual_end_at IS NOT NULL;

CREATE INDEX video_collection_state_channel_idx ON public.video_collection_state USING btree (channel_id, next_due_at);

CREATE INDEX video_collection_state_common_due_idx ON public.video_collection_state USING btree (next_due_at, video_id) WHERE ((paused_reason IS NULL) AND (next_due_at IS NOT NULL));

CREATE INDEX video_collection_state_failures_idx ON public.video_collection_state USING btree (consecutive_failures DESC, next_due_at) WHERE (consecutive_failures > 0);

CREATE INDEX video_collection_state_format_due_idx ON public.video_collection_state USING btree (video_type, next_due_at, video_id) WHERE ((paused_reason IS NULL) AND (next_due_at IS NOT NULL));

CREATE INDEX video_collection_state_lease_expiry_idx ON public.video_collection_state USING btree (lease_expires_at) WHERE (lease_token IS NOT NULL);

CREATE INDEX video_collection_state_typed_reconcile_idx ON public.video_collection_state USING btree (video_type, video_id) WHERE (video_type = ANY (ARRAY['short'::text, 'stream'::text]));

CREATE INDEX video_inventory_channel_long_published_idx ON public.video_inventory USING btree (channel_id, published_at DESC NULLS LAST) WHERE (video_type = 'long'::text);

CREATE INDEX video_inventory_channel_type_idx ON public.video_inventory USING btree (channel_id, video_type);

CREATE INDEX video_inventory_long_order_idx ON public.video_inventory USING btree (published_at DESC NULLS LAST, discovered_at DESC NULLS LAST, video_id) WHERE (video_type = 'long'::text);

CREATE INDEX video_inventory_memberships_absence_scan_idx ON public.video_inventory_memberships USING btree (absence_confirmed_by_scan_id, video_id) WHERE (absence_confirmed_by_scan_id IS NOT NULL);

CREATE INDEX video_inventory_memberships_earliest_clock_idx ON public.video_inventory_memberships USING btree ((least(COALESCE(CASE WHEN (membership_status = 'present'::text) THEN last_present_at ELSE absent_since END, '-infinity'::timestamp with time zone), last_seen_at, CASE WHEN ((first_seen_at IS NULL) AND (first_seen_scan_id IS NULL)) THEN 'infinity'::timestamp with time zone ELSE COALESCE(first_seen_at, '-infinity'::timestamp with time zone) END, CASE WHEN ((last_present_at IS NULL) AND (source_published_at IS NULL)) THEN 'infinity'::timestamp with time zone ELSE COALESCE(last_present_at, '-infinity'::timestamp with time zone) END)), video_id, channel_id, video_type);

CREATE INDEX video_inventory_memberships_first_seen_scan_idx ON public.video_inventory_memberships USING btree (first_seen_scan_id, video_id) WHERE (first_seen_scan_id IS NOT NULL);

CREATE INDEX video_inventory_memberships_last_seen_scan_idx ON public.video_inventory_memberships USING btree (last_seen_scan_id, video_id) WHERE (last_seen_scan_id IS NOT NULL);

CREATE INDEX video_inventory_memberships_latest_clock_idx ON public.video_inventory_memberships USING btree ((greatest(COALESCE(CASE WHEN (membership_status = 'present'::text) THEN last_present_at ELSE absent_since END, '-infinity'::timestamp with time zone), last_seen_at, COALESCE(first_seen_at, '-infinity'::timestamp with time zone), COALESCE(last_present_at, '-infinity'::timestamp with time zone))), video_id, channel_id, video_type);

CREATE INDEX video_inventory_memberships_present_catalog_idx ON public.video_inventory_memberships USING btree (channel_id, video_type, source_published_at DESC NULLS LAST, video_id) WHERE (membership_status = 'present'::text);

CREATE INDEX video_inventory_memberships_video_status_idx ON public.video_inventory_memberships USING btree (video_id, membership_status, video_type, channel_id);

CREATE INDEX video_inventory_typed_reconcile_idx ON public.video_inventory USING btree (video_type, video_id) WHERE (video_type = ANY (ARRAY['short'::text, 'stream'::text]));

CREATE INDEX video_inventory_updated_at_desc_visible_lookup_idx ON public.video_inventory USING btree (updated_at DESC) INCLUDE (channel_id) WHERE (updated_at IS NOT NULL);

CREATE UNIQUE INDEX video_snapshots_collection_attempt_uidx ON public.video_snapshots USING btree (video_id, collection_attempt_id) WHERE (collection_attempt_id IS NOT NULL);

CREATE INDEX video_snapshots_video_recorded_idx ON public.video_snapshots USING btree (video_id, recorded_at DESC);

CREATE INDEX video_type_review_cases_state_page_idx ON public.video_type_review_cases USING btree (state, case_id);

CREATE INDEX videos_channel_category_idx ON public.videos USING btree (channel_id, category_id);

CREATE INDEX videos_checked_at_desc_visible_lookup_idx ON public.videos USING btree (checked_at DESC) INCLUDE (channel_id) WHERE (checked_at IS NOT NULL);

CREATE INDEX youtube_api_request_attempts_phase_endpoint_idx ON public.youtube_api_request_attempts USING btree (quota_day, phase, endpoint, started_at DESC);

CREATE INDEX youtube_api_request_attempts_quota_day_idx ON public.youtube_api_request_attempts USING btree (project_id, quota_bucket, quota_day);

CREATE INDEX youtube_api_request_attempts_retention_idx ON public.youtube_api_request_attempts USING btree (started_at, request_attempt_id);

CREATE INDEX youtube_api_request_attempts_retry_idx ON public.youtube_api_request_attempts USING btree (retry_of_request_attempt_id) WHERE (retry_of_request_attempt_id IS NOT NULL);

CREATE INDEX youtube_api_request_attempts_unfinished_idx ON public.youtube_api_request_attempts USING btree (started_at) WHERE (status = 'started'::text);

CREATE TRIGGER channels_user_work_identity_immutable BEFORE UPDATE OF user_work_id ON public.channels FOR EACH ROW EXECUTE FUNCTION public.guard_channel_user_work_identity();

CREATE TRIGGER hot_operator_source_scope BEFORE INSERT OR UPDATE ON public.operator_jobs FOR EACH ROW EXECUTE FUNCTION public.guard_hot_operator_scope();

CREATE TRIGGER hot_operator_target_scope BEFORE INSERT OR UPDATE ON public.operator_job_targets FOR EACH ROW EXECUTE FUNCTION public.guard_hot_operator_target_scope();

CREATE TRIGGER hot_storage_collection_delete_guard BEFORE DELETE ON public.video_collection_state FOR EACH ROW EXECUTE FUNCTION public.guard_hot_storage_collection_delete();

CREATE TRIGGER hot_storage_playlist_delete_guard BEFORE DELETE ON public.channel_playlist_state FOR EACH ROW EXECUTE FUNCTION public.guard_hot_storage_collection_delete();

CREATE TRIGGER review_case_channel_owner BEFORE INSERT OR UPDATE OF channel_id, user_channel_ref ON public.video_type_review_cases FOR EACH ROW EXECUTE FUNCTION public.bind_channel_user_work_owner();

CREATE TRIGGER saved_member_channel_owner BEFORE INSERT OR UPDATE OF video_id, user_channel_ref ON public.saved_collection_members FOR EACH ROW EXECUTE FUNCTION public.bind_channel_user_work_owner();

CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.channel_label_assignments FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_user_work_backup_limits();

CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.channel_labels FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_user_work_backup_limits();

CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.channels FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_user_work_backup_limits();

CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.folders FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_user_work_backup_limits();

CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.saved_collection_members FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_user_work_backup_limits();

CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.saved_objects FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_user_work_backup_limits();

CREATE TRIGGER user_work_backup_limits AFTER INSERT ON public.video_type_review_history FOR EACH STATEMENT EXECUTE FUNCTION public.enforce_user_work_backup_limits();

CREATE TRIGGER video_inventory_membership_scan_guard BEFORE INSERT OR UPDATE OF channel_id, video_type, membership_status, first_seen_scan_id, last_seen_scan_id, absence_confirmed_by_scan_id ON public.video_inventory_memberships FOR EACH ROW EXECUTE FUNCTION public.enforce_video_inventory_membership_scan();

CREATE TRIGGER video_type_review_canonical_guard BEFORE UPDATE OF video_id, video_type, channel_id ON public.video_inventory FOR EACH ROW EXECUTE FUNCTION public.guard_manual_video_type_decision();

ALTER TABLE ONLY operator_private.operator_job_retry_requests
    ADD CONSTRAINT operator_job_retry_requests_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.operator_jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.channel_collection_state
    ADD CONSTRAINT channel_collection_state_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.channel_label_assignments
    ADD CONSTRAINT channel_label_assignments_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.channel_label_assignments
    ADD CONSTRAINT channel_label_assignments_label_key_fkey FOREIGN KEY (label_key) REFERENCES public.channel_labels(label_key) ON DELETE CASCADE;

ALTER TABLE ONLY public.channel_playlist_state
    ADD CONSTRAINT channel_playlist_inventory_completed_scan_fkey FOREIGN KEY (last_completed_scan_id, channel_id, video_type) REFERENCES public.channel_video_inventory_scans(scan_id, channel_id, video_type) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.channel_snapshots
    ADD CONSTRAINT channel_snapshots_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.channel_playlist_state
    ADD CONSTRAINT channel_video_discovery_state_active_scan_fkey FOREIGN KEY (active_scan_id, channel_id, video_type) REFERENCES public.channel_video_inventory_scans(scan_id, channel_id, video_type) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.channel_playlist_state
    ADD CONSTRAINT channel_video_discovery_state_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.channel_video_inventory_scans
    ADD CONSTRAINT channel_video_inventory_scans_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.channels
    ADD CONSTRAINT channels_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.folders(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.channels
    ADD CONSTRAINT channels_parent_channel_id_fkey FOREIGN KEY (parent_channel_id) REFERENCES public.channels(channel_id);

ALTER TABLE ONLY public.operator_job_targets
    ADD CONSTRAINT operator_job_targets_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.operator_jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.operator_jobs
    ADD CONSTRAINT operator_jobs_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id);

ALTER TABLE ONLY public.operator_jobs
    ADD CONSTRAINT operator_jobs_inventory_scan_fkey FOREIGN KEY (inventory_scan_id, channel_id, inventory_video_type) REFERENCES public.channel_video_inventory_scans(scan_id, channel_id, video_type) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.saved_collection_commands
    ADD CONSTRAINT saved_collection_commands_parent_fkey FOREIGN KEY (collection_kind, collection_id) REFERENCES public.saved_objects(kind, id);

ALTER TABLE ONLY public.saved_collection_members
    ADD CONSTRAINT saved_collection_members_parent_fkey FOREIGN KEY (collection_kind, collection_id) REFERENCES public.saved_objects(kind, id);

ALTER TABLE ONLY public.saved_collection_members
    ADD CONSTRAINT saved_collection_members_restore_owner_ref_fkey FOREIGN KEY (restore_owner_ref) REFERENCES public.user_channel_restore_items(id);

ALTER TABLE ONLY public.saved_collection_members
    ADD CONSTRAINT saved_collection_members_user_channel_ref_fkey FOREIGN KEY (user_channel_ref) REFERENCES public.channels(user_work_id);

ALTER TABLE ONLY public.typed_inventory_receipts
    ADD CONSTRAINT typed_inventory_receipts_scan_id_fkey FOREIGN KEY (scan_id) REFERENCES public.channel_video_inventory_scans(scan_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.typed_video_collection_receipts
    ADD CONSTRAINT typed_video_collection_receipts_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.video_inventory(video_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_channel_restore_items
    ADD CONSTRAINT user_channel_restore_items_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.folders(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.video_collection_state
    ADD CONSTRAINT video_collection_state_channel_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_collection_state
    ADD CONSTRAINT video_collection_state_video_fkey FOREIGN KEY (video_id) REFERENCES public.video_inventory(video_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_inventory_memberships
    ADD CONSTRAINT video_inventory_memberships_absence_confirmed_by_scan_id_fkey FOREIGN KEY (absence_confirmed_by_scan_id) REFERENCES public.channel_video_inventory_scans(scan_id);

ALTER TABLE ONLY public.video_inventory_memberships
    ADD CONSTRAINT video_inventory_memberships_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES public.channels(channel_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_inventory_memberships
    ADD CONSTRAINT video_inventory_memberships_first_seen_scan_id_fkey FOREIGN KEY (first_seen_scan_id) REFERENCES public.channel_video_inventory_scans(scan_id);

ALTER TABLE ONLY public.video_inventory_memberships
    ADD CONSTRAINT video_inventory_memberships_last_seen_scan_id_fkey FOREIGN KEY (last_seen_scan_id) REFERENCES public.channel_video_inventory_scans(scan_id);

ALTER TABLE ONLY public.video_inventory_memberships
    ADD CONSTRAINT video_inventory_memberships_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.video_inventory(video_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_type_review_cases
    ADD CONSTRAINT video_type_review_cases_restore_owner_ref_fkey FOREIGN KEY (restore_owner_ref) REFERENCES public.user_channel_restore_items(id);

ALTER TABLE ONLY public.video_type_review_cases
    ADD CONSTRAINT video_type_review_cases_user_channel_ref_fkey FOREIGN KEY (user_channel_ref) REFERENCES public.channels(user_work_id);

ALTER TABLE ONLY public.video_type_review_cases
    ADD CONSTRAINT video_type_review_cases_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.video_inventory(video_id) ON DELETE SET NULL;

ALTER TABLE ONLY public.video_type_review_history
    ADD CONSTRAINT video_type_review_history_case_id_fkey FOREIGN KEY (case_id) REFERENCES public.video_type_review_cases(case_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.youtube_api_request_attempts
    ADD CONSTRAINT youtube_api_request_attempts_retry_fkey FOREIGN KEY (retry_of_request_attempt_id) REFERENCES public.youtube_api_request_attempts(request_attempt_id) ON DELETE SET NULL;

ALTER TABLE operator_private.operator_job_retry_requests ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_collection_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY channel_collection_state_service_select ON public.channel_collection_state FOR SELECT TO service_role USING (true);

ALTER TABLE public.channel_discovery_queue_control ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_label_assignments ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_labels ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_playlist_state ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_snapshots ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channel_video_inventory_scans ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.channels ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.folders ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.operator_job_targets ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.operator_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.saved_collection_commands ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.saved_collection_members ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.saved_objects ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.sync_runs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.typed_inventory_receipts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.typed_video_collection_receipts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.typed_video_collection_runtime ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.user_channel_restore_items ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_availability_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_availability_incidents ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_collection_cadence ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_collection_control ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_collection_state ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_inventory ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_inventory_memberships ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_snapshots ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_type_review_cases ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.video_type_review_history ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.videos ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.youtube_api_request_attempts ENABLE ROW LEVEL SECURITY;

GRANT USAGE ON SCHEMA operator_private TO service_role;

REVOKE USAGE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO postgres;

GRANT USAGE ON SCHEMA public TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.operator_jobs TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.channel_video_inventory_scans TO service_role;

REVOKE ALL ON FUNCTION public.bind_channel_user_work_owner() FROM PUBLIC;

GRANT ALL ON FUNCTION public.bind_channel_user_work_owner() TO service_role;

REVOKE ALL ON FUNCTION public.enforce_user_work_backup_limits() FROM PUBLIC;

REVOKE ALL ON FUNCTION public.enforce_video_inventory_membership_scan() FROM PUBLIC;

REVOKE ALL ON FUNCTION public.guard_channel_user_work_identity() FROM PUBLIC;

REVOKE ALL ON FUNCTION public.guard_hot_operator_scope() FROM PUBLIC;

GRANT ALL ON FUNCTION public.guard_hot_operator_scope() TO service_role;

REVOKE ALL ON FUNCTION public.guard_hot_operator_target_scope() FROM PUBLIC;

GRANT ALL ON FUNCTION public.guard_hot_operator_target_scope() TO service_role;

REVOKE ALL ON FUNCTION public.guard_hot_storage_collection_delete() FROM PUBLIC;

REVOKE ALL ON FUNCTION public.guard_manual_video_type_decision() FROM PUBLIC;

GRANT SELECT,INSERT ON TABLE operator_private.operator_job_retry_requests TO service_role;

GRANT SELECT ON TABLE public.video_collection_state TO service_role;

GRANT MAINTAIN ON TABLE operator_private.video_collection_state_canonical_repair_lock TO service_role;

GRANT SELECT ON TABLE public.channel_collection_state TO service_role;

GRANT UPDATE (owner_permission,is_hidden,hidden_at,control_epoch) ON TABLE public.channel_collection_state TO service_role;

GRANT USAGE ON SEQUENCE public.channel_collection_epoch_seq TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.channels TO service_role;

GRANT SELECT ON TABLE public.channel_collection_control TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.channel_playlist_state TO service_role;

GRANT SELECT ON TABLE public.channel_discovery_queue TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.channel_discovery_queue_control TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.channel_label_assignments TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.channel_labels TO service_role;

GRANT SELECT,INSERT ON TABLE public.channel_snapshots TO service_role;

GRANT SELECT ON TABLE public.channel_video_inventory_state TO service_role;

GRANT SELECT ON TABLE public.channels_with_control TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.folders TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.video_inventory TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.video_inventory_memberships TO service_role;

GRANT SELECT ON TABLE public.video_type_review_cases TO service_role;

GRANT SELECT ON TABLE public.video_inventory_type_resolution TO service_role;

GRANT SELECT ON TABLE public.hot_video_inventory TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.video_availability_incidents TO service_role;

GRANT SELECT ON TABLE public.open_video_availability TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.operator_job_targets TO service_role;

GRANT SELECT,INSERT ON TABLE public.saved_collection_commands TO service_role;

GRANT SELECT,INSERT,DELETE ON TABLE public.saved_collection_members TO service_role;

GRANT SELECT,INSERT,UPDATE ON TABLE public.saved_objects TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sync_runs TO service_role;

GRANT ALL ON SEQUENCE public.sync_runs_id_seq TO service_role;

GRANT SELECT ON TABLE public.typed_inventory_receipts TO service_role;

GRANT SELECT ON TABLE public.typed_video_collection_receipts TO service_role;

GRANT SELECT ON TABLE public.typed_video_collection_runtime TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.user_channel_restore_items TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.video_availability_events TO service_role;

GRANT ALL ON SEQUENCE public.video_availability_events_id_seq TO service_role;

GRANT ALL ON SEQUENCE public.video_availability_incidents_id_seq TO service_role;

GRANT SELECT ON TABLE public.video_collection_cadence TO service_role;

GRANT SELECT ON TABLE public.video_collection_control TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.video_snapshots TO service_role;

GRANT ALL ON SEQUENCE public.video_snapshots_id_seq TO service_role;

GRANT SELECT ON TABLE public.video_type_review_history TO service_role;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.videos TO service_role;

GRANT SELECT,INSERT,UPDATE ON TABLE public.youtube_api_request_attempts TO service_role;

INSERT INTO public.channel_labels(label_key,name,label_type)
VALUES ('monetized','Monetized','preset');

INSERT INTO public.typed_video_collection_runtime(video_type,runtime_enabled)
VALUES ('short',false),('stream',false);

INSERT INTO public.video_collection_control(singleton,reconciliation_required)
VALUES (true,true);

INSERT INTO public.channel_discovery_queue_control(singleton,reconciliation_required)
VALUES (true,true);

INSERT INTO public.video_collection_cadence(video_type,collection_kind,availability_class,min_age_seconds,max_age_seconds,cadence_seconds,policy_version,enabled,age_bucket,jitter_window_seconds) VALUES
('long','counters','public',0,86400,3600,1,true,NULL,0),
('long','counters','public',86400,604800,7200,1,true,NULL,0),
('long','counters','public',604800,2419200,21600,1,true,NULL,0),
('long','counters','public',2419200,7776000,43200,1,true,NULL,0),
('long','counters','public',7776000,31536000,86400,1,true,NULL,0),
('long','counters','public',31536000,NULL,604800,1,true,NULL,0),
('long','counters','unlisted',0,NULL,2419200,1,true,NULL,0),
('long','metadata','public',0,604800,21600,1,true,NULL,0),
('long','metadata','public',604800,2419200,86400,1,true,NULL,0),
('long','metadata','public',2419200,31536000,604800,1,true,NULL,0),
('long','metadata','public',31536000,NULL,2419200,1,true,NULL,0),
('long','metadata','unlisted',0,NULL,2419200,1,true,NULL,0),
('short','metadata','all',0,7776000,604800,1,true,'age_0_90d',21600),
('short','metadata','all',7776000,NULL,2419200,1,true,'age_91d_plus',21600),
('stream','metadata','all',0,7776000,604800,1,true,'age_0_90d',21600),
('stream','metadata','all',7776000,NULL,2419200,1,true,'age_91d_plus',21600);

GRANT DELETE ON TABLE public.saved_objects,public.saved_collection_commands TO service_role;

GRANT INSERT (case_id,video_id,channel_id,source_detached_at,observed_canonical_type,observed_types,observed_channels,blocked_reason,state,revision,decision_type,decision_reason,decision_at,decision_revision,first_seen_at,last_seen_at,restore_owner_ref)
 ON TABLE public.video_type_review_cases TO service_role;

GRANT INSERT (case_id,revision,video_type,reason,decided_at,request_id,expected_revision)
 ON TABLE public.video_type_review_history TO service_role;

GRANT UPDATE (user_channel_ref,restore_owner_ref)
 ON TABLE public.saved_collection_members,public.video_type_review_cases TO service_role;

GRANT MAINTAIN ON TABLE public.video_type_review_cases,public.video_type_review_history TO service_role;

DO $user_work_restore_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('folders',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_labels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_label_assignments',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_objects',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_collection_members',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('saved_collection_commands',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('video_type_review_cases',ARRAY['SELECT','MAINTAIN']::text[]),
 ('video_type_review_history',ARRAY['SELECT','MAINTAIN']::text[]),
 ('user_channel_restore_items',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_type_review_cases','case_id','INSERT'),
 ('video_type_review_cases','video_id','INSERT'),
 ('video_type_review_cases','channel_id','INSERT'),
 ('video_type_review_cases','source_detached_at','INSERT'),
 ('video_type_review_cases','observed_canonical_type','INSERT'),
 ('video_type_review_cases','observed_types','INSERT'),
 ('video_type_review_cases','observed_channels','INSERT'),
 ('video_type_review_cases','blocked_reason','INSERT'),
 ('video_type_review_cases','state','INSERT'),
 ('video_type_review_cases','revision','INSERT'),
 ('video_type_review_cases','decision_type','INSERT'),
 ('video_type_review_cases','decision_reason','INSERT'),
 ('video_type_review_cases','decision_at','INSERT'),
 ('video_type_review_cases','decision_revision','INSERT'),
 ('video_type_review_cases','first_seen_at','INSERT'),
 ('video_type_review_cases','last_seen_at','INSERT'),
 ('video_type_review_cases','restore_owner_ref','INSERT'),
 ('video_type_review_history','case_id','INSERT'),
 ('video_type_review_history','revision','INSERT'),
 ('video_type_review_history','video_type','INSERT'),
 ('video_type_review_history','reason','INSERT'),
 ('video_type_review_history','decided_at','INSERT'),
 ('video_type_review_history','request_id','INSERT'),
 ('video_type_review_history','expected_revision','INSERT'),
 ('saved_collection_members','user_channel_ref','UPDATE'),
 ('saved_collection_members','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','user_channel_ref','UPDATE'),
 ('video_type_review_cases','restore_owner_ref','UPDATE')
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
 THEN RAISE EXCEPTION 'user_work_restore_acl_invalid'; END IF;
END $user_work_restore_acl$;

GRANT UPDATE (next_due_at,lease_channel_epoch,lease_token,lease_owner,lease_started_at,lease_expires_at,consecutive_failures,last_error_type,updated_at,metadata_due_at,stats_due_at)
 ON TABLE public.video_collection_state TO service_role;

GRANT UPDATE (initial_job_id) ON TABLE public.channel_collection_state TO service_role;

DO $video_failure_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('video_collection_state',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
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
 ('video_collection_state','stats_due_at','UPDATE')
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
END $video_failure_acl$;

GRANT DELETE ON TABLE public.channel_snapshots,public.typed_video_collection_receipts TO service_role;

GRANT UPDATE (recorded_at) ON TABLE public.channel_snapshots,public.typed_video_collection_receipts TO service_role;

GRANT UPDATE (observed_canonical_type,observed_types,observed_channels,blocked_reason,revision)
 ON TABLE public.video_type_review_cases TO service_role;

DO $evidence_retention_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channel_snapshots',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('typed_video_collection_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('channel_snapshots','recorded_at','UPDATE'),
 ('typed_video_collection_receipts','recorded_at','UPDATE')
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
 THEN RAISE EXCEPTION 'evidence_retention_acl_invalid'; END IF;
END $evidence_retention_acl$;

DO $evidence_user_work_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('folders',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_labels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_label_assignments',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_objects',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_collection_members',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('saved_collection_commands',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('video_type_review_cases',ARRAY['SELECT','MAINTAIN']::text[]),
 ('video_type_review_history',ARRAY['SELECT','MAINTAIN']::text[]),
 ('user_channel_restore_items',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_type_review_cases','case_id','INSERT'),
 ('video_type_review_cases','video_id','INSERT'),
 ('video_type_review_cases','channel_id','INSERT'),
 ('video_type_review_cases','source_detached_at','INSERT'),
 ('video_type_review_cases','observed_canonical_type','INSERT'),
 ('video_type_review_cases','observed_types','INSERT'),
 ('video_type_review_cases','observed_channels','INSERT'),
 ('video_type_review_cases','blocked_reason','INSERT'),
 ('video_type_review_cases','state','INSERT'),
 ('video_type_review_cases','revision','INSERT'),
 ('video_type_review_cases','decision_type','INSERT'),
 ('video_type_review_cases','decision_reason','INSERT'),
 ('video_type_review_cases','decision_at','INSERT'),
 ('video_type_review_cases','decision_revision','INSERT'),
 ('video_type_review_cases','first_seen_at','INSERT'),
 ('video_type_review_cases','last_seen_at','INSERT'),
 ('video_type_review_cases','restore_owner_ref','INSERT'),
 ('video_type_review_history','case_id','INSERT'),
 ('video_type_review_history','revision','INSERT'),
 ('video_type_review_history','video_type','INSERT'),
 ('video_type_review_history','reason','INSERT'),
 ('video_type_review_history','decided_at','INSERT'),
 ('video_type_review_history','request_id','INSERT'),
 ('video_type_review_history','expected_revision','INSERT'),
 ('saved_collection_members','user_channel_ref','UPDATE'),
 ('saved_collection_members','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','user_channel_ref','UPDATE'),
 ('video_type_review_cases','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','observed_canonical_type','UPDATE'),
 ('video_type_review_cases','observed_types','UPDATE'),
 ('video_type_review_cases','observed_channels','UPDATE'),
 ('video_type_review_cases','blocked_reason','UPDATE'),
 ('video_type_review_cases','revision','UPDATE')
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
 THEN RAISE EXCEPTION 'user_work_restore_acl_invalid'; END IF;
END $evidence_user_work_acl$;

GRANT UPDATE (attempt_count,last_claim_run_id,last_attempt_at) ON TABLE public.video_collection_state TO service_role;

GRANT INSERT (receipt_scope,transition_id,scan_id,transition_kind,result,source_checked_at,created_at) ON TABLE public.typed_inventory_receipts TO service_role;

DO $direct_video_claim_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('video_collection_state',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
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
 ('video_collection_state','last_attempt_at','UPDATE')
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
END $direct_video_claim_acl$;

DO $direct_scan_receipt_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_inventory_receipts',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_inventory_receipts','receipt_scope','INSERT'),
 ('typed_inventory_receipts','transition_id','INSERT'),
 ('typed_inventory_receipts','scan_id','INSERT'),
 ('typed_inventory_receipts','transition_kind','INSERT'),
 ('typed_inventory_receipts','result','INSERT'),
 ('typed_inventory_receipts','source_checked_at','INSERT'),
 ('typed_inventory_receipts','created_at','INSERT')
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
 THEN RAISE EXCEPTION 'inventory_receipts_storage_acl_invalid'; END IF;
END $direct_scan_receipt_acl$;

GRANT INSERT (video_id,channel_id,stats_due_at,metadata_due_at,stats_source_checked_at,metadata_source_checked_at,next_due_at,policy_version,hydration_needed,paused_reason,updated_at,video_type,typed_source_published_at,typed_collection_status) ON TABLE public.video_collection_state TO service_role;

GRANT UPDATE (channel_id,stats_source_checked_at,metadata_source_checked_at,policy_version,hydration_needed,paused_reason) ON TABLE public.video_collection_state TO service_role;

GRANT INSERT (next_page_token_hash) ON TABLE public.typed_inventory_receipts TO service_role;

GRANT UPDATE (claims,result,next_page_token_hash) ON TABLE public.typed_inventory_receipts TO service_role;

GRANT DELETE ON TABLE public.typed_inventory_receipts,public.youtube_api_request_attempts TO service_role;

DO $direct_initializer_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('video_collection_state',ARRAY['SELECT']::text[])
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
 ('video_collection_state','typed_collection_status','INSERT'),
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
 ('video_collection_state','paused_reason','UPDATE')
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
END $direct_initializer_acl$;

DO $direct_receipt_retention_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_inventory_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_inventory_receipts','receipt_scope','INSERT'),
 ('typed_inventory_receipts','transition_id','INSERT'),
 ('typed_inventory_receipts','scan_id','INSERT'),
 ('typed_inventory_receipts','transition_kind','INSERT'),
 ('typed_inventory_receipts','result','INSERT'),
 ('typed_inventory_receipts','source_checked_at','INSERT'),
 ('typed_inventory_receipts','created_at','INSERT'),
 ('typed_inventory_receipts','next_page_token_hash','INSERT'),
 ('typed_inventory_receipts','claims','UPDATE'),
 ('typed_inventory_receipts','result','UPDATE'),
 ('typed_inventory_receipts','next_page_token_hash','UPDATE')
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
 THEN RAISE EXCEPTION 'inventory_receipts_storage_acl_invalid'; END IF;
END $direct_receipt_retention_acl$;

DO $direct_operational_retention_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('youtube_api_request_attempts',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (SELECT NULL::text,NULL::text,NULL::text WHERE false
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
 THEN RAISE EXCEPTION 'operational_retention_acl_invalid'; END IF;
END $direct_operational_retention_acl$;

DO $direct_typed_receipt_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_video_collection_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_video_collection_receipts','recorded_at','UPDATE')
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
 THEN RAISE EXCEPTION 'evidence_retention_acl_invalid'; END IF;
END $direct_typed_receipt_acl$;

GRANT DELETE ON TABLE public.video_collection_state TO service_role;

GRANT UPDATE (video_type,typed_source_published_at,typed_stream_actual_end_at) ON TABLE public.video_collection_state TO service_role;

GRANT DELETE ON TABLE public.video_type_review_cases,public.video_type_review_history TO service_role;

GRANT DELETE ON TABLE operator_private.operator_job_retry_requests TO service_role;

DO $direct_inventory_cleanup_acl$
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
 ('video_collection_state','typed_collection_status','INSERT'),
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
 ('video_collection_state','typed_stream_actual_end_at','UPDATE')
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
END $direct_inventory_cleanup_acl$;

DO $direct_deleted_review_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('folders',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_labels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_label_assignments',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_objects',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_collection_members',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('saved_collection_commands',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('video_type_review_cases',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('video_type_review_history',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('user_channel_restore_items',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_type_review_cases','case_id','INSERT'),
 ('video_type_review_cases','video_id','INSERT'),
 ('video_type_review_cases','channel_id','INSERT'),
 ('video_type_review_cases','source_detached_at','INSERT'),
 ('video_type_review_cases','observed_canonical_type','INSERT'),
 ('video_type_review_cases','observed_types','INSERT'),
 ('video_type_review_cases','observed_channels','INSERT'),
 ('video_type_review_cases','blocked_reason','INSERT'),
 ('video_type_review_cases','state','INSERT'),
 ('video_type_review_cases','revision','INSERT'),
 ('video_type_review_cases','decision_type','INSERT'),
 ('video_type_review_cases','decision_reason','INSERT'),
 ('video_type_review_cases','decision_at','INSERT'),
 ('video_type_review_cases','decision_revision','INSERT'),
 ('video_type_review_cases','first_seen_at','INSERT'),
 ('video_type_review_cases','last_seen_at','INSERT'),
 ('video_type_review_cases','restore_owner_ref','INSERT'),
 ('video_type_review_history','case_id','INSERT'),
 ('video_type_review_history','revision','INSERT'),
 ('video_type_review_history','video_type','INSERT'),
 ('video_type_review_history','reason','INSERT'),
 ('video_type_review_history','decided_at','INSERT'),
 ('video_type_review_history','request_id','INSERT'),
 ('video_type_review_history','expected_revision','INSERT'),
 ('saved_collection_members','user_channel_ref','UPDATE'),
 ('saved_collection_members','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','user_channel_ref','UPDATE'),
 ('video_type_review_cases','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','observed_canonical_type','UPDATE'),
 ('video_type_review_cases','observed_types','UPDATE'),
 ('video_type_review_cases','observed_channels','UPDATE'),
 ('video_type_review_cases','blocked_reason','UPDATE'),
 ('video_type_review_cases','revision','UPDATE')
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
 THEN RAISE EXCEPTION 'user_work_restore_acl_invalid'; END IF;
END $direct_deleted_review_acl$;

DO $direct_deleted_retry_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('operator_job_retry_requests',ARRAY['SELECT','INSERT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (SELECT NULL::text,NULL::text,NULL::text WHERE false
), roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid
), namespace AS MATERIALIZED (
 SELECT n.oid,n.nspowner,n.nspacl FROM pg_catalog.pg_namespace n
 WHERE n.oid=pg_catalog.to_regnamespace('operator_private')
), relations AS MATERIALIZED (
 SELECT expected.*,relation.oid,relation.relowner,relation.relacl,relation.relkind,relation.relrowsecurity
 FROM required_tables expected LEFT JOIN pg_catalog.pg_class relation
 ON relation.oid=pg_catalog.to_regclass('operator_private.'||expected.table_name)
), table_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')),
 column_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))
SELECT roles.valid AS roles_valid,
 EXISTS(SELECT 1 FROM namespace n WHERE pg_catalog.pg_get_userbyid(n.nspowner)='postgres')
 AND NOT EXISTS(SELECT 1 FROM relations r WHERE r.oid IS NULL OR r.relkind<>'r' OR NOT r.relrowsecurity
  OR pg_catalog.pg_get_userbyid(r.relowner)<>'postgres')
 AND NOT EXISTS(SELECT 1 FROM required_columns required LEFT JOIN relations r USING(table_name)
  WHERE NOT EXISTS(SELECT 1 FROM pg_catalog.pg_attribute a WHERE a.attrelid=r.oid
   AND a.attnum>0 AND NOT a.attisdropped AND a.attname=required.column_name)) AS relations_valid,
 EXISTS(SELECT 1 FROM namespace n WHERE pg_catalog.has_schema_privilege(roles.service_oid,n.oid,'USAGE')
  AND NOT pg_catalog.has_schema_privilege(roles.service_oid,n.oid,'CREATE'))
 AND NOT EXISTS(SELECT 1 FROM namespace n CROSS JOIN LATERAL
  pg_catalog.aclexplode(coalesce(n.nspacl,pg_catalog.acldefault('n',n.nspowner))) acl
  WHERE acl.grantee<>n.nspowner AND (acl.grantee IS DISTINCT FROM roles.service_oid
   OR acl.is_grantable OR acl.privilege_type<>'USAGE'))
 AND NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
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
 THEN RAISE EXCEPTION 'deleted_channel_retention_acl_invalid'; END IF;
END $direct_deleted_retry_acl$;

GRANT UPDATE (last_success_at,profile_metadata_version,typed_collection_status,typed_collection_checked_at) ON TABLE public.video_collection_state TO service_role;

GRANT INSERT (video_id,collection_attempt_id,channel_id,video_type,lease_token,receipt_kind,recorded_at,payload_fingerprint) ON TABLE public.typed_video_collection_receipts TO service_role;

GRANT INSERT (claims) ON TABLE public.typed_inventory_receipts TO service_role;

GRANT USAGE ON SEQUENCE operator_private.inventory_dispatch_seq TO service_role;

GRANT UPDATE (reconcile_queue_cursor,reconcile_inventory_cursor,updated_at) ON TABLE public.typed_video_collection_runtime TO service_role;

GRANT UPDATE (reconciliation_required,last_reconciled_at,updated_at) ON TABLE public.video_collection_control TO service_role;

GRANT UPDATE (decision_type,decision_reason,decision_at,decision_revision,state,last_seen_at) ON TABLE public.video_type_review_cases TO service_role;

GRANT INSERT (typed_source_published_acquired_at) ON public.video_collection_state TO service_role;
GRANT UPDATE (typed_source_published_acquired_at,typed_source_stream_end_acquired_at)
 ON public.video_collection_state TO service_role;


DO $direct_completion_inventory_receipt_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_inventory_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_inventory_receipts','receipt_scope','INSERT'),
 ('typed_inventory_receipts','transition_id','INSERT'),
 ('typed_inventory_receipts','scan_id','INSERT'),
 ('typed_inventory_receipts','transition_kind','INSERT'),
 ('typed_inventory_receipts','result','INSERT'),
 ('typed_inventory_receipts','source_checked_at','INSERT'),
 ('typed_inventory_receipts','created_at','INSERT'),
 ('typed_inventory_receipts','next_page_token_hash','INSERT'),
 ('typed_inventory_receipts','claims','INSERT'),
 ('typed_inventory_receipts','claims','UPDATE'),
 ('typed_inventory_receipts','result','UPDATE'),
 ('typed_inventory_receipts','next_page_token_hash','UPDATE')
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
 THEN RAISE EXCEPTION 'inventory_receipts_storage_acl_invalid'; END IF;
END $direct_completion_inventory_receipt_acl$;

DO $direct_completion_evidence_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channel_snapshots',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('typed_video_collection_receipts',ARRAY['SELECT','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('channel_snapshots','recorded_at','UPDATE'),
 ('typed_video_collection_receipts','video_id','INSERT'),
 ('typed_video_collection_receipts','collection_attempt_id','INSERT'),
 ('typed_video_collection_receipts','channel_id','INSERT'),
 ('typed_video_collection_receipts','video_type','INSERT'),
 ('typed_video_collection_receipts','lease_token','INSERT'),
 ('typed_video_collection_receipts','receipt_kind','INSERT'),
 ('typed_video_collection_receipts','recorded_at','INSERT'),
 ('typed_video_collection_receipts','payload_fingerprint','INSERT'),
 ('typed_video_collection_receipts','recorded_at','UPDATE')
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
 THEN RAISE EXCEPTION 'evidence_retention_acl_invalid'; END IF;
END $direct_completion_evidence_acl$;

DO $direct_completion_review_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('folders',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_labels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_label_assignments',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_objects',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_collection_members',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('saved_collection_commands',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('video_type_review_cases',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('video_type_review_history',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('user_channel_restore_items',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_type_review_cases','case_id','INSERT'),
 ('video_type_review_cases','video_id','INSERT'),
 ('video_type_review_cases','channel_id','INSERT'),
 ('video_type_review_cases','source_detached_at','INSERT'),
 ('video_type_review_cases','observed_canonical_type','INSERT'),
 ('video_type_review_cases','observed_types','INSERT'),
 ('video_type_review_cases','observed_channels','INSERT'),
 ('video_type_review_cases','blocked_reason','INSERT'),
 ('video_type_review_cases','state','INSERT'),
 ('video_type_review_cases','revision','INSERT'),
 ('video_type_review_cases','decision_type','INSERT'),
 ('video_type_review_cases','decision_reason','INSERT'),
 ('video_type_review_cases','decision_at','INSERT'),
 ('video_type_review_cases','decision_revision','INSERT'),
 ('video_type_review_cases','first_seen_at','INSERT'),
 ('video_type_review_cases','last_seen_at','INSERT'),
 ('video_type_review_cases','restore_owner_ref','INSERT'),
 ('video_type_review_history','case_id','INSERT'),
 ('video_type_review_history','revision','INSERT'),
 ('video_type_review_history','video_type','INSERT'),
 ('video_type_review_history','reason','INSERT'),
 ('video_type_review_history','decided_at','INSERT'),
 ('video_type_review_history','request_id','INSERT'),
 ('video_type_review_history','expected_revision','INSERT'),
 ('saved_collection_members','user_channel_ref','UPDATE'),
 ('saved_collection_members','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','user_channel_ref','UPDATE'),
 ('video_type_review_cases','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','observed_canonical_type','UPDATE'),
 ('video_type_review_cases','observed_types','UPDATE'),
 ('video_type_review_cases','observed_channels','UPDATE'),
 ('video_type_review_cases','blocked_reason','UPDATE'),
 ('video_type_review_cases','revision','UPDATE'),
 ('video_type_review_cases','decision_type','UPDATE'),
 ('video_type_review_cases','decision_reason','UPDATE'),
 ('video_type_review_cases','decision_at','UPDATE'),
 ('video_type_review_cases','decision_revision','UPDATE'),
 ('video_type_review_cases','state','UPDATE'),
 ('video_type_review_cases','last_seen_at','UPDATE')
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
 THEN RAISE EXCEPTION 'user_work_restore_acl_invalid'; END IF;
END $direct_completion_review_acl$;

DO $direct_completion_progress_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_video_collection_runtime',ARRAY['SELECT']::text[]),
 ('video_collection_control',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_video_collection_runtime','reconcile_queue_cursor','UPDATE'),
 ('typed_video_collection_runtime','reconcile_inventory_cursor','UPDATE'),
 ('typed_video_collection_runtime','updated_at','UPDATE'),
 ('video_collection_control','reconciliation_required','UPDATE'),
 ('video_collection_control','last_reconciled_at','UPDATE'),
 ('video_collection_control','updated_at','UPDATE')
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
 THEN RAISE EXCEPTION 'collection_completion_acl_invalid'; END IF;
END $direct_completion_progress_acl$;

GRANT INSERT (channel_id,user_work_id,owner_permission,is_hidden,hidden_at) ON TABLE public.channel_collection_state TO service_role;

GRANT INSERT (user_channel_ref) ON TABLE public.video_type_review_cases TO service_role;

GRANT UPDATE (invalidated_at) ON TABLE public.video_collection_control TO service_role;

GRANT UPDATE (video_id,channel_id,source_detached_at) ON TABLE public.video_type_review_cases TO service_role;

DO $direct_workflow_review_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('folders',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_labels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_label_assignments',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_objects',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_collection_members',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('saved_collection_commands',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('video_type_review_cases',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('video_type_review_history',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('user_channel_restore_items',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_type_review_cases','case_id','INSERT'),
 ('video_type_review_cases','video_id','INSERT'),
 ('video_type_review_cases','channel_id','INSERT'),
 ('video_type_review_cases','source_detached_at','INSERT'),
 ('video_type_review_cases','observed_canonical_type','INSERT'),
 ('video_type_review_cases','observed_types','INSERT'),
 ('video_type_review_cases','observed_channels','INSERT'),
 ('video_type_review_cases','blocked_reason','INSERT'),
 ('video_type_review_cases','state','INSERT'),
 ('video_type_review_cases','revision','INSERT'),
 ('video_type_review_cases','decision_type','INSERT'),
 ('video_type_review_cases','decision_reason','INSERT'),
 ('video_type_review_cases','decision_at','INSERT'),
 ('video_type_review_cases','decision_revision','INSERT'),
 ('video_type_review_cases','first_seen_at','INSERT'),
 ('video_type_review_cases','last_seen_at','INSERT'),
 ('video_type_review_cases','restore_owner_ref','INSERT'),
 ('video_type_review_cases','user_channel_ref','INSERT'),
 ('video_type_review_history','case_id','INSERT'),
 ('video_type_review_history','revision','INSERT'),
 ('video_type_review_history','video_type','INSERT'),
 ('video_type_review_history','reason','INSERT'),
 ('video_type_review_history','decided_at','INSERT'),
 ('video_type_review_history','request_id','INSERT'),
 ('video_type_review_history','expected_revision','INSERT'),
 ('saved_collection_members','user_channel_ref','UPDATE'),
 ('saved_collection_members','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','user_channel_ref','UPDATE'),
 ('video_type_review_cases','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','observed_canonical_type','UPDATE'),
 ('video_type_review_cases','observed_types','UPDATE'),
 ('video_type_review_cases','observed_channels','UPDATE'),
 ('video_type_review_cases','blocked_reason','UPDATE'),
 ('video_type_review_cases','revision','UPDATE'),
 ('video_type_review_cases','decision_type','UPDATE'),
 ('video_type_review_cases','decision_reason','UPDATE'),
 ('video_type_review_cases','decision_at','UPDATE'),
 ('video_type_review_cases','decision_revision','UPDATE'),
 ('video_type_review_cases','state','UPDATE'),
 ('video_type_review_cases','last_seen_at','UPDATE'),
 ('video_type_review_cases','video_id','UPDATE'),
 ('video_type_review_cases','channel_id','UPDATE'),
 ('video_type_review_cases','source_detached_at','UPDATE')
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
 THEN RAISE EXCEPTION 'user_work_restore_acl_invalid'; END IF;
END $direct_workflow_review_acl$;

DO $direct_workflow_progress_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('typed_video_collection_runtime',ARRAY['SELECT']::text[]),
 ('video_collection_control',ARRAY['SELECT']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('typed_video_collection_runtime','reconcile_queue_cursor','UPDATE'),
 ('typed_video_collection_runtime','reconcile_inventory_cursor','UPDATE'),
 ('typed_video_collection_runtime','updated_at','UPDATE'),
 ('video_collection_control','reconciliation_required','UPDATE'),
 ('video_collection_control','last_reconciled_at','UPDATE'),
 ('video_collection_control','updated_at','UPDATE'),
 ('video_collection_control','invalidated_at','UPDATE')
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
 THEN RAISE EXCEPTION 'collection_completion_acl_invalid'; END IF;
END $direct_workflow_progress_acl$;

DO $self_hosted_fresh_bootstrap$
DECLARE changed integer;
BEGIN
 IF current_user <> 'postgres' THEN RAISE EXCEPTION 'self_hosted_bootstrap_requires_owner'; END IF;
 IF EXISTS (SELECT 1 FROM public.channels)
 OR EXISTS (SELECT 1 FROM public.video_inventory)
 OR EXISTS (SELECT 1 FROM public.videos)
 OR EXISTS (SELECT 1 FROM public.video_snapshots)
 OR EXISTS (SELECT 1 FROM public.video_collection_state)
 OR EXISTS (SELECT 1 FROM public.channel_playlist_state) THEN
  RAISE EXCEPTION 'self_hosted_bootstrap_requires_empty_application';
 END IF;
 -- These two seed rows describe empty queues only. Normal startup/upgrade
 -- must use Python maintenance and readiness; this block is fresh-install only.
 UPDATE public.video_collection_control SET reconciliation_required=false,
  last_reconciled_at=now(),updated_at=now() WHERE singleton=true;
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'self_hosted_video_bootstrap_control_missing'; END IF;
 UPDATE public.channel_discovery_queue_control SET reconciliation_required=false,
  last_reconciled_at=now(),updated_at=now() WHERE singleton=true;
 GET DIAGNOSTICS changed=ROW_COUNT;
 IF changed<>1 THEN RAISE EXCEPTION 'self_hosted_discovery_bootstrap_control_missing'; END IF;
END;
$self_hosted_fresh_bootstrap$;

SET LOCAL ROLE postgres;


-- Current-source read projections (R07b).
CREATE SCHEMA readable AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA readable FROM PUBLIC;
GRANT USAGE ON SCHEMA readable TO service_role;
CREATE VIEW readable.videos WITH (security_barrier=true, security_invoker=false) AS
SELECT
    s.video_id AS video_id,
    s.channel_id AS channel_id,
    CASE WHEN admitted.metadata THEN s.title END AS title,
    CASE WHEN admitted.counters THEN s.views END AS views,
    s.checked_at AS checked_at,
    CASE WHEN admitted.metadata THEN s.published_at END AS published_at,
    CASE WHEN admitted.metadata THEN s.duration END AS duration,
    CASE WHEN admitted.counters THEN s.comment_count END AS comment_count,
    CASE WHEN admitted.metadata THEN s.tags END AS tags,
    CASE WHEN admitted.availability THEN s.availability_status END AS availability_status,
    CASE WHEN admitted.availability THEN s.privacy_status END AS privacy_status,
    CASE WHEN admitted.availability THEN s.upload_status END AS upload_status,
    s.availability_checked_at AS availability_checked_at,
    CASE WHEN admitted.counters THEN s.like_count END AS like_count,
    CASE WHEN admitted.metadata THEN s.category_id END AS category_id,
    s.metadata_checked_at AS metadata_checked_at,
    CASE WHEN admitted.metadata THEN s.youtube_topic_categories END AS youtube_topic_categories,
    CASE WHEN admitted.metadata THEN s.default_audio_language END AS default_audio_language,
    CASE WHEN admitted.metadata THEN s.default_language END AS default_language,
    CASE WHEN admitted.metadata THEN s.caption_available END AS caption_available,
    CASE WHEN admitted.metadata THEN s.stream_actual_end_at END AS stream_actual_end_at,
    least(CASE WHEN admitted.counters THEN s.checked_at END,CASE WHEN admitted.metadata THEN s.metadata_checked_at END,CASE WHEN admitted.availability THEN s.availability_checked_at END)+interval '696 hours' AS source_expires_at
FROM public.videos s
CROSS JOIN LATERAL (SELECT (isfinite(s.checked_at) AND s.checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS counters,
(isfinite(s.metadata_checked_at) AND s.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS metadata,
(isfinite(s.availability_checked_at) AND s.availability_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS availability) admitted;
ALTER VIEW readable.videos OWNER TO postgres;
GRANT SELECT ON readable.videos TO service_role;

CREATE VIEW readable.channels WITH (security_barrier=true, security_invoker=false) AS
SELECT
    s.channel_id AS channel_id,
    CASE WHEN admitted.profile THEN s.title END AS title,
    CASE WHEN admitted.profile THEN s.subscribers END AS subscribers,
    CASE WHEN admitted.profile THEN s.views END AS views,
    CASE WHEN admitted.profile THEN s.video_count END AS video_count,
    s.added_at AS added_at,
    CASE WHEN admitted.profile THEN s.avatar_url END AS avatar_url,
    s.folder_id AS folder_id,
    s.parent_channel_id AS parent_channel_id,
    CASE WHEN admitted.profile THEN s.uploads_playlist_id END AS uploads_playlist_id,
    s.deep_bypass AS deep_bypass,
    s.deep_bypass_reason AS deep_bypass_reason,
    s.deep_inventory_status AS deep_inventory_status,
    s.deep_inventory_started_at AS deep_inventory_started_at,
    s.deep_inventory_checked_at AS deep_inventory_checked_at,
    s.deep_inventory_error AS deep_inventory_error,
    s.initial_long_inventory_status AS initial_long_inventory_status,
    s.initial_28d_stats_status AS initial_28d_stats_status,
    s.is_hidden AS is_hidden,
    s.hidden_at AS hidden_at,
    s.initial_long_inventory_started_at AS initial_long_inventory_started_at,
    s.initial_28d_stats_started_at AS initial_28d_stats_started_at,
    s.channel_metadata_checked_at AS channel_metadata_checked_at,
    s.channel_metadata_next_after AS channel_metadata_next_after,
    s.channel_metadata_status AS channel_metadata_status,
    s.channel_metadata_started_at AS channel_metadata_started_at,
    s.channel_metadata_error AS channel_metadata_error,
    s.channel_metadata_error_count AS channel_metadata_error_count,
    s.channel_video_count_changed_at AS channel_video_count_changed_at,
    CASE WHEN admitted.country THEN s.country_code END AS country_code,
    s.country_collected_at AS country_collected_at,
    s.initial_long_inventory_error_code AS initial_long_inventory_error_code,
    s.initial_long_inventory_error_at AS initial_long_inventory_error_at,
    s.initial_long_inventory_error_count AS initial_long_inventory_error_count,
    CASE WHEN admitted.profile THEN s.channel_created_at END AS channel_created_at,
    estimate.value AS initial_inventory_estimate,
    s.user_input AS user_input,
    s.user_work_id AS user_work_id,
    s.user_work_restored AS user_work_restored,
    least(CASE WHEN admitted.profile THEN s.channel_metadata_checked_at END,CASE WHEN admitted.country THEN s.country_collected_at END,estimate.oldest_clock)+interval '696 hours' AS source_expires_at
FROM public.channels s
CROSS JOIN LATERAL (SELECT s.channel_metadata_status='ok' AND (isfinite(s.channel_metadata_checked_at) AND s.channel_metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS profile,
 (isfinite(s.country_collected_at) AND s.country_collected_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS country) admitted
LEFT JOIN LATERAL (
 WITH inputs AS (
  SELECT kind,ord,s.initial_inventory_estimate AS estimate,
   s.initial_inventory_estimate->'counts'->kind AS count_json,
   s.initial_inventory_estimate->'statuses'->>kind AS status,
   CASE WHEN s.initial_inventory_estimate ? 'checked_at_by_type'
    THEN s.initial_inventory_estimate->'checked_at_by_type'->>kind
    ELSE s.initial_inventory_estimate->>'checked_at' END AS clock_text
  FROM (VALUES('long',1),('short',2),('stream',3)) kinds(kind,ord)
  WHERE jsonb_typeof(s.initial_inventory_estimate->'video_types')='array'
   AND s.initial_inventory_estimate->'video_types' ? kind
 ), parsed AS (
  SELECT *,CASE WHEN jsonb_typeof(count_json)='number' AND count_json::text ~ '^[0-9]{1,10}$'
   THEN count_json::text::bigint END AS count_value,
   CASE WHEN clock_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?(Z|[+-][0-9]{2}(:?[0-9]{2})?)$'
    AND pg_input_is_valid(clock_text,'timestamptz') THEN clock_text::timestamptz END AS clock
  FROM inputs
 ), admitted AS (
  SELECT *,((status='ok' AND count_value BETWEEN 0 AND 2147483647)
   OR (status='playlist_not_found' AND (count_json='null'::jsonb OR count_value=0)))
   AND (NOT (estimate ? 'checked_at_by_type') OR jsonb_typeof(estimate->'checked_at_by_type')='object')
   AND isfinite(clock) AND clock BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() AS valid
  FROM parsed
 )
 SELECT jsonb_build_object('scope',s.initial_inventory_estimate->'scope',
  'video_types',coalesce(jsonb_agg(kind ORDER BY ord),'[]'::jsonb),
  'counts',coalesce(jsonb_object_agg(kind,CASE WHEN valid THEN count_value END),'{}'::jsonb),
  'statuses',coalesce(jsonb_object_agg(kind,CASE WHEN valid THEN status ELSE 'error' END),'{}'::jsonb),
  'checked_at_by_type',coalesce(jsonb_object_agg(kind,CASE WHEN valid THEN clock_text END),'{}'::jsonb)) AS value,
  min(clock) FILTER(WHERE valid) AS oldest_clock FROM admitted
) estimate ON jsonb_typeof(s.initial_inventory_estimate)='object';
ALTER VIEW readable.channels OWNER TO postgres;
GRANT SELECT ON readable.channels TO service_role;

CREATE VIEW readable.video_inventory_memberships WITH (security_barrier=true, security_invoker=false) AS
SELECT
    s.video_id AS video_id,
    s.channel_id AS channel_id,
    s.video_type AS video_type,
    s.playlist_id AS playlist_id,
    CASE WHEN (isfinite(s.last_present_at) AND s.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.source_published_at END AS source_published_at,
    s.membership_status AS membership_status,
    s.evidence_source AS evidence_source,
    CASE WHEN (isfinite(s.first_seen_at) AND s.first_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.first_seen_at END AS first_seen_at,
    s.last_seen_at AS last_seen_at,
    CASE WHEN (isfinite(s.last_present_at) AND s.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.last_present_at END AS last_present_at,
    s.absent_since AS absent_since,
    CASE WHEN (isfinite(s.first_seen_at) AND s.first_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.first_seen_scan_id END AS first_seen_scan_id,
    s.last_seen_scan_id AS last_seen_scan_id,
    s.absence_confirmed_by_scan_id AS absence_confirmed_by_scan_id,
    s.created_at AS created_at,
    s.updated_at AS updated_at,
    least(s.last_seen_at,CASE WHEN s.membership_status='present' THEN s.last_present_at ELSE s.absent_since END,CASE WHEN (isfinite(s.first_seen_at) AND s.first_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.first_seen_at END,CASE WHEN (isfinite(s.last_present_at) AND s.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.last_present_at END)+interval '696 hours' AS source_expires_at
FROM public.video_inventory_memberships s
WHERE (isfinite(s.last_seen_at) AND s.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AND CASE WHEN s.membership_status='present'
 THEN (isfinite(s.last_present_at) AND s.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) ELSE (isfinite(s.absent_since) AND s.absent_since BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) END;
ALTER VIEW readable.video_inventory_memberships OWNER TO postgres;
GRANT SELECT ON readable.video_inventory_memberships TO service_role;

CREATE VIEW readable.video_inventory WITH (security_barrier=true, security_invoker=false) AS
SELECT s.video_id,s.channel_id,s.discovered_at,s.updated_at,s.video_type,proof.published_at AS published_at,s.skeleton_since,proof.source_expires_at
FROM public.video_inventory s
LEFT JOIN LATERAL (
 SELECT m.source_published_at,m.last_present_at,m.source_expires_at
 FROM readable.video_inventory_memberships m
 WHERE m.video_id=s.video_id AND m.channel_id=s.channel_id AND m.video_type=s.video_type
  AND m.membership_status='present' ORDER BY m.last_present_at DESC,m.playlist_id LIMIT 1
) membership ON true
LEFT JOIN LATERAL (
 SELECT v.published_at,v.metadata_checked_at AS source_checked_at
 FROM readable.videos v JOIN public.video_type_review_cases decision ON decision.video_id=v.video_id
 WHERE v.video_id=s.video_id AND v.channel_id=s.channel_id AND decision.channel_id=s.channel_id
  AND decision.decision_type=s.video_type AND decision.blocked_reason IS NULL
  AND (isfinite(v.metadata_checked_at) AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp())
  AND NOT EXISTS(SELECT 1 FROM readable.video_inventory_memberships foreign_evidence
    WHERE foreign_evidence.video_id=s.video_id AND foreign_evidence.membership_status='present'
      AND foreign_evidence.channel_id<>s.channel_id)
 LIMIT 1
) manual ON true
CROSS JOIN LATERAL (SELECT coalesce(membership.last_present_at,manual.source_checked_at) AS source_checked_at,
 coalesce(membership.source_published_at,manual.published_at) AS published_at,
 least(membership.source_expires_at,CASE WHEN membership.source_published_at IS NULL
   THEN manual.source_checked_at+interval '696 hours' END) AS source_expires_at) proof
WHERE s.skeleton_since IS NULL AND proof.source_checked_at IS NOT NULL
UNION ALL
SELECT s.video_id,s.channel_id,s.discovered_at,s.updated_at,NULL::text AS video_type,NULL::timestamptz AS published_at,s.skeleton_since,NULL::timestamptz AS source_expires_at
FROM public.video_inventory s
LEFT JOIN LATERAL (
 SELECT m.source_published_at,m.last_present_at,m.source_expires_at
 FROM readable.video_inventory_memberships m
 WHERE m.video_id=s.video_id AND m.channel_id=s.channel_id AND m.video_type=s.video_type
  AND m.membership_status='present' ORDER BY m.last_present_at DESC,m.playlist_id LIMIT 1
) membership ON true
LEFT JOIN LATERAL (
 SELECT v.published_at,v.metadata_checked_at AS source_checked_at
 FROM readable.videos v JOIN public.video_type_review_cases decision ON decision.video_id=v.video_id
 WHERE v.video_id=s.video_id AND v.channel_id=s.channel_id AND decision.channel_id=s.channel_id
  AND decision.decision_type=s.video_type AND decision.blocked_reason IS NULL
  AND (isfinite(v.metadata_checked_at) AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp())
  AND NOT EXISTS(SELECT 1 FROM readable.video_inventory_memberships foreign_evidence
    WHERE foreign_evidence.video_id=s.video_id AND foreign_evidence.membership_status='present'
      AND foreign_evidence.channel_id<>s.channel_id)
 LIMIT 1
) manual ON true
CROSS JOIN LATERAL (SELECT coalesce(membership.last_present_at,manual.source_checked_at) AS source_checked_at,
 coalesce(membership.source_published_at,manual.published_at) AS published_at,
 least(membership.source_expires_at,CASE WHEN membership.source_published_at IS NULL
   THEN manual.source_checked_at+interval '696 hours' END) AS source_expires_at) proof
WHERE s.skeleton_since IS NOT NULL OR proof.source_checked_at IS NULL;
ALTER VIEW readable.video_inventory OWNER TO postgres;
GRANT SELECT ON readable.video_inventory TO service_role;

CREATE VIEW readable.video_inventory_type_resolution WITH (security_barrier=true, security_invoker=false) AS
 WITH evidence AS (
         SELECT i.video_id,
            i.channel_id AS canonical_channel_id,
            i.video_type AS canonical_video_type,i.skeleton_since,min(m.source_expires_at) AS source_expires_at,
            COALESCE(array_agg(DISTINCT m.video_type ORDER BY m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_types,
            COALESCE(array_agg(DISTINCT m.channel_id ORDER BY m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_channels,
            count(DISTINCT m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_type_count,
            count(DISTINCT m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_channel_count,
            COALESCE(bool_or(((m.membership_status = 'present'::text) AND (m.channel_id = i.channel_id) AND (m.video_type = i.video_type))), false) AS canonical_membership_supported
           FROM (public.video_inventory i
             LEFT JOIN readable.video_inventory_memberships m ON ((m.video_id = i.video_id)))
          GROUP BY i.video_id, i.channel_id, i.video_type,i.skeleton_since
        ), resolved AS (
         SELECT e.video_id,e.skeleton_since,e.source_expires_at,
            e.canonical_channel_id,
            e.canonical_video_type,
            e.present_membership_types,
            e.present_membership_channels,
            e.present_type_count,
            e.present_channel_count,
            e.canonical_membership_supported,
            COALESCE(((c.decision_type = e.canonical_video_type) AND (c.channel_id = e.canonical_channel_id) AND (c.blocked_reason IS NULL) AND (NOT (EXISTS ( SELECT 1
                   FROM unnest(e.present_membership_channels) x(id)
                  WHERE (x.id <> e.canonical_channel_id))))), false) AS manual_valid
           FROM (evidence e
             LEFT JOIN public.video_type_review_cases c ON ((c.video_id = e.video_id)))
        )
 SELECT video_id,
    canonical_channel_id,
    CASE WHEN skeleton_since IS NULL AND (canonical_membership_supported OR (manual_valid AND metadata.source_checked_at IS NOT NULL)) THEN canonical_video_type END AS canonical_video_type,
    present_membership_types,
    present_membership_channels,
    present_type_count,
    present_channel_count,
    canonical_membership_supported,
        CASE
            WHEN manual_valid THEN 'resolved'::text
            WHEN (present_type_count = 0) THEN 'unverified'::text
            WHEN ((present_type_count > 1) OR (present_channel_count > 1)) THEN 'conflict'::text
            WHEN (NOT canonical_membership_supported) THEN 'canonical_unsupported'::text
            ELSE 'resolved'::text
        END AS resolution_status,
    manual_valid,least(resolved.source_expires_at,CASE WHEN manual_valid THEN metadata.source_checked_at+interval '696 hours' END) AS source_expires_at
   FROM resolved LEFT JOIN LATERAL (SELECT v.metadata_checked_at AS source_checked_at FROM public.videos v
   WHERE manual_valid AND v.video_id=resolved.video_id AND v.channel_id=resolved.canonical_channel_id AND (isfinite(v.metadata_checked_at) AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) LIMIT 1) metadata ON true;
ALTER VIEW readable.video_inventory_type_resolution OWNER TO postgres;
GRANT SELECT ON readable.video_inventory_type_resolution TO service_role;

CREATE VIEW readable.hot_video_inventory WITH (security_barrier=true, security_invoker=false) AS
 SELECT inv.video_id,
    inv.channel_id,
    inv.video_type,
    proof.source_published_at AS published_at,
    proof.last_present_at AS source_checked_at,
    least(resolution.source_expires_at,membership_proof.source_expires_at,proof.last_present_at+interval '696 hours',CASE WHEN membership_proof.source_published_at IS NULL THEN manual_proof.last_present_at+interval '696 hours' END) AS source_expires_at
   FROM ((public.video_inventory inv
     JOIN readable.video_inventory_type_resolution resolution ON (((resolution.video_id = inv.video_id) AND (resolution.canonical_channel_id = inv.channel_id) AND (resolution.resolution_status = 'resolved'::text) AND (resolution.canonical_video_type = inv.video_type) AND ((resolution.manual_valid IS TRUE) OR ((resolution.present_membership_types = ARRAY[inv.video_type]) AND (resolution.present_membership_channels = ARRAY[inv.channel_id]))))))
     LEFT JOIN LATERAL ( SELECT membership.source_published_at,
            membership.last_present_at,membership.source_expires_at
           FROM readable.video_inventory_memberships membership
          WHERE ((membership.video_id = inv.video_id) AND (membership.channel_id = inv.channel_id) AND (membership.membership_status = 'present'::text) AND ((resolution.manual_valid IS TRUE) OR (membership.video_type = inv.video_type)) AND isfinite(membership.last_present_at) AND ((membership.last_present_at >= (statement_timestamp() - '696:00:00'::interval)) AND (membership.last_present_at <= statement_timestamp())))
          ORDER BY membership.last_present_at DESC, membership.video_type, membership.playlist_id
         LIMIT 1) membership_proof ON true)
     LEFT JOIN LATERAL (SELECT v.published_at AS source_published_at,v.metadata_checked_at AS last_present_at
       FROM readable.videos v WHERE resolution.manual_valid IS TRUE AND v.video_id=inv.video_id
       AND v.channel_id=inv.channel_id AND isfinite(v.metadata_checked_at)
       AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
       LIMIT 1) manual_proof ON true
     CROSS JOIN LATERAL (SELECT coalesce(membership_proof.source_published_at,manual_proof.source_published_at) AS source_published_at,
       coalesce(membership_proof.last_present_at,manual_proof.last_present_at) AS last_present_at) proof
   WHERE inv.skeleton_since IS NULL AND proof.last_present_at IS NOT NULL;
ALTER VIEW readable.hot_video_inventory OWNER TO postgres;
GRANT SELECT ON readable.hot_video_inventory TO service_role;

CREATE VIEW readable.channel_playlist_state WITH (security_barrier=true, security_invoker=false) AS
SELECT
    s.channel_id AS channel_id,
    s.video_type AS video_type,
    s.playlist_id AS playlist_id,
    s.runtime_enabled AS runtime_enabled,
    s.status AS status,
    s.next_due_at AS next_due_at,
    s.priority AS priority,
    s.no_new_streak AS no_new_streak,
    CASE WHEN admitted.active_valid THEN s.active_scan_id END AS active_scan_id,
    CASE WHEN admitted.active_valid THEN s.resume_page_token END AS resume_page_token,
    CASE WHEN admitted.active_valid THEN s.resume_started_at END AS resume_started_at,
    CASE WHEN admitted.active_valid THEN s.scan_boundary_at END AS scan_boundary_at,
    s.source_next_after AS source_next_after,
    CASE WHEN admitted.source_valid THEN s.source_checked_at END AS source_checked_at,
    s.lease_token AS lease_token,
    s.lease_owner AS lease_owner,
    s.lease_run_id AS lease_run_id,
    s.lease_expires_at AS lease_expires_at,
    s.last_claim_run_id AS last_claim_run_id,
    s.attempt_count AS attempt_count,
    s.last_error_code AS last_error_code,
    s.last_completion_attempt_id AS last_completion_attempt_id,
    s.last_failure_attempt_id AS last_failure_attempt_id,
    s.state_origin AS state_origin,
    s.legacy_source_status AS legacy_source_status,
    s.created_at AS created_at,
    s.updated_at AS updated_at,
    s.inventory_status AS inventory_status,
    admitted.completed_valid AND s.product_exposure_ready AS product_exposure_ready,
    CASE WHEN admitted.completed_valid THEN s.expected_item_count END AS expected_item_count,
    CASE WHEN admitted.completed_valid THEN s.last_completed_scan_id END AS last_completed_scan_id,
    CASE WHEN admitted.completed_valid THEN s.inventory_completed_at END AS inventory_completed_at,
    s.inventory_last_error_code AS inventory_last_error_code,
    s.inventory_last_error_at AS inventory_last_error_at,
    s.inventory_error_count AS inventory_error_count,
    s.inventory_state_origin AS inventory_state_origin,
    s.inventory_created_at AS inventory_created_at,
    s.inventory_updated_at AS inventory_updated_at,
    s.last_result AS last_result,
    CASE WHEN admitted.active_valid THEN s.discovery_started_at END AS discovery_started_at,
    s.discovery_error AS discovery_error,
    s.discovery_error_count AS discovery_error_count,
    admitted.active_valid AND s.resume_pending AS resume_pending,
    s.lease_channel_epoch AS lease_channel_epoch,
    s.dispatch_ordinal AS dispatch_ordinal,
    least(CASE WHEN admitted.source_valid THEN s.source_checked_at END,CASE WHEN admitted.active_valid THEN least(active.started_at,active.scan_boundary_at,active.source_checked_at) END,CASE WHEN admitted.completed_valid THEN least(completed.started_at,completed.scan_boundary_at,completed.source_checked_at,completed.completed_at,s.source_checked_at,s.inventory_completed_at) END)+interval '696 hours' AS source_expires_at
FROM public.channel_playlist_state s
LEFT JOIN LATERAL (SELECT scan.*,coalesce(
 isfinite(statement_timestamp())
 AND isfinite(scan.started_at)
 AND scan.started_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 AND isfinite(scan.scan_boundary_at)
 AND scan.scan_boundary_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 AND ((isfinite(scan.source_checked_at)
       AND scan.source_checked_at BETWEEN scan.scan_boundary_at AND statement_timestamp())
   OR (scan.source_checked_at IS NULL AND scan.status='running' AND scan.coverage='partial'
       AND scan.pages_completed=0 AND scan.items_seen=0 AND scan.new_memberships_found=0
       AND scan.resume_page_token IS NULL AND scan.last_transition_id IS NULL AND scan.completed_at IS NULL))
 AND (scan.status<>'completed' OR (isfinite(scan.completed_at)
       AND scan.completed_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp())),false) AS hot
 FROM public.channel_video_inventory_scans scan WHERE scan.scan_id=s.active_scan_id) active ON true
LEFT JOIN LATERAL (SELECT scan.*,coalesce(
 isfinite(statement_timestamp())
 AND isfinite(scan.started_at)
 AND scan.started_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 AND isfinite(scan.scan_boundary_at)
 AND scan.scan_boundary_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 AND ((isfinite(scan.source_checked_at)
       AND scan.source_checked_at BETWEEN scan.scan_boundary_at AND statement_timestamp())
   OR (scan.source_checked_at IS NULL AND scan.status='running' AND scan.coverage='partial'
       AND scan.pages_completed=0 AND scan.items_seen=0 AND scan.new_memberships_found=0
       AND scan.resume_page_token IS NULL AND scan.last_transition_id IS NULL AND scan.completed_at IS NULL))
 AND (scan.status<>'completed' OR (isfinite(scan.completed_at)
       AND scan.completed_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp())),false) AS hot
 FROM public.channel_video_inventory_scans scan WHERE scan.scan_id=s.last_completed_scan_id) completed ON true
CROSS JOIN LATERAL (SELECT
 coalesce(active.channel_id=s.channel_id AND active.video_type=s.video_type AND active.playlist_id=s.playlist_id
  AND active.status='running' AND active.coverage='partial' AND active.token_history_verified AND active.hot,false) AS active_valid,
 CASE WHEN s.last_completed_scan_id IS NULL AND s.inventory_state_origin='legacy_long_backfill' AND s.inventory_status='completed'
  THEN coalesce((isfinite(s.source_checked_at) AND s.source_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AND (isfinite(s.inventory_completed_at) AND s.inventory_completed_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()),false)
  ELSE coalesce(completed.channel_id=s.channel_id AND completed.video_type=s.video_type AND completed.playlist_id=s.playlist_id
   AND completed.status='completed' AND completed.coverage='complete'
   AND completed.hot,false) END AS completed_valid,
 (isfinite(s.source_checked_at) AND s.source_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS source_valid) admitted;
ALTER VIEW readable.channel_playlist_state OWNER TO postgres;
GRANT SELECT ON readable.channel_playlist_state TO service_role;

CREATE VIEW readable.channel_video_inventory_state WITH (security_barrier=true, security_invoker=false) AS
 SELECT channel_id,
    video_type,
    playlist_id,
    inventory_status,
    product_exposure_ready,
    expected_item_count,
    active_scan_id,
    last_completed_scan_id,
    source_checked_at,
    inventory_completed_at,
    inventory_last_error_code AS last_error_code,
    inventory_last_error_at AS last_error_at,
    inventory_error_count AS error_count,
    inventory_state_origin AS state_origin,
    inventory_created_at AS created_at,
    inventory_updated_at AS updated_at,
    source_expires_at
   FROM readable.channel_playlist_state;
ALTER VIEW readable.channel_video_inventory_state OWNER TO postgres;
GRANT SELECT ON readable.channel_video_inventory_state TO service_role;

CREATE VIEW readable.channel_control WITH (security_barrier=true, security_invoker=false) AS
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
ALTER VIEW readable.channel_control OWNER TO postgres;
GRANT SELECT ON readable.channel_control TO service_role;
GRANT SELECT ON readable.channel_control TO lic_reader;

CREATE VIEW readable.channels_with_control WITH (security_barrier=true, security_invoker=false) AS
 SELECT channel.channel_id,
    channel.title,
    channel.subscribers,
    channel.views,
    channel.video_count,
    channel.added_at,
    channel.avatar_url,
    channel.folder_id,
    channel.parent_channel_id,
    channel.uploads_playlist_id,
    channel.deep_bypass,
    channel.deep_bypass_reason,
    channel.deep_inventory_status,
    NULL::text AS deep_inventory_page_token,
    channel.deep_inventory_started_at,
    channel.deep_inventory_checked_at,
    channel.deep_inventory_error,
    channel.initial_long_inventory_status,
    channel.initial_28d_stats_status,
    COALESCE(control.is_hidden, true) AS is_hidden,
    control.hidden_at,
    channel.initial_long_inventory_started_at,
    channel.initial_28d_stats_started_at,
    channel.channel_metadata_checked_at,
    channel.channel_metadata_next_after,
    channel.channel_metadata_status,
    channel.channel_metadata_started_at,
    channel.channel_metadata_error,
    channel.channel_metadata_error_count,
    channel.channel_video_count_changed_at,
    channel.country_code,
    channel.country_collected_at,
    channel.initial_long_inventory_error_code,
    channel.initial_long_inventory_error_at,
    channel.initial_long_inventory_error_count,
    channel.channel_created_at,
    channel.initial_inventory_estimate,
    channel.user_input,
    channel.user_work_id,
    channel.user_work_restored,
    COALESCE(control.owner_permission, 'legacy_hold'::text) AS collection_permission,
    control.control_epoch AS collection_control_epoch,
    COALESCE(control.collection_mode, 'operator_hold'::text) AS collection_mode,
    channel.source_expires_at
   FROM (readable.channels channel
     LEFT JOIN readable.channel_control control ON ((control.channel_id = channel.channel_id)));
ALTER VIEW readable.channels_with_control OWNER TO postgres;
GRANT SELECT ON readable.channels_with_control TO service_role;


-- Snapshot source admission (R07c); analytical windows remain caller-owned.
CREATE VIEW readable.video_snapshots WITH (security_barrier=true, security_invoker=false) AS
SELECT s.*,s.recorded_at+interval '696 hours' AS source_expires_at
FROM public.video_snapshots s
WHERE isfinite(s.recorded_at) AND s.recorded_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp();
ALTER VIEW readable.video_snapshots OWNER TO postgres;
GRANT SELECT ON readable.video_snapshots TO service_role;

CREATE VIEW readable.channel_snapshots WITH (security_barrier=true, security_invoker=false) AS
SELECT s.*,s.recorded_at+interval '696 hours' AS source_expires_at
FROM public.channel_snapshots s
WHERE isfinite(s.recorded_at) AND s.recorded_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp();
ALTER VIEW readable.channel_snapshots OWNER TO postgres;
GRANT SELECT ON readable.channel_snapshots TO service_role;


-- Remaining public source projections (R07d); mutation owners retain raw tables.
CREATE VIEW readable.video_type_review_cases WITH (security_barrier=true, security_invoker=false) AS
SELECT c.video_id AS video_id,
    c.channel_id AS channel_id,
    CASE WHEN isfinite(c.last_seen_at) AND c.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN c.observed_canonical_type END AS observed_canonical_type,
    CASE WHEN isfinite(c.last_seen_at) AND c.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN c.observed_types ELSE ARRAY[]::text[] END AS observed_types,
    CASE WHEN isfinite(c.last_seen_at) AND c.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN c.observed_channels ELSE ARRAY[]::text[] END AS observed_channels,
    c.state AS state,
    c.revision AS revision,
    c.first_seen_at AS first_seen_at,
    c.last_seen_at AS last_seen_at,
    CASE WHEN isfinite(c.last_seen_at) AND c.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN c.blocked_reason END AS blocked_reason,
    c.decision_type AS decision_type,
    c.decision_reason AS decision_reason,
    c.decision_at AS decision_at,
    c.decision_revision AS decision_revision,
    c.case_id AS case_id,
    c.source_detached_at AS source_detached_at,
    c.user_channel_ref AS user_channel_ref,
    c.restore_owner_ref AS restore_owner_ref,
    CASE WHEN isfinite(c.last_seen_at) AND c.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN c.last_seen_at+interval '696 hours' END AS source_expires_at
FROM public.video_type_review_cases c;
ALTER VIEW readable.video_type_review_cases OWNER TO postgres;
GRANT SELECT ON readable.video_type_review_cases TO service_role;

CREATE VIEW readable.operator_jobs WITH (security_barrier=true, security_invoker=false) AS
SELECT j.id AS id,
    j.job_type AS job_type,
    j.channel_id AS channel_id,
    j.status AS status,
    j.requested_at AS requested_at,
    j.started_at AS started_at,
    j.updated_at AS updated_at,
    j.finished_at AS finished_at,
    j.lease_token AS lease_token,
    j.lease_owner AS lease_owner,
    j.lease_expires_at AS lease_expires_at,
    j.heartbeat_at AS heartbeat_at,
    j.target_count AS target_count,
    j.processed_count AS processed_count,
    j.updated_count AS updated_count,
    j.skipped_count AS skipped_count,
    j.unavailable_count AS unavailable_count,
    j.error_count AS error_count,
    j.max_items AS max_items,
    j.max_quota_units AS max_quota_units,
    j.quota_units_estimated AS quota_units_estimated,
    j.quota_units_reserved AS quota_units_reserved,
    j.quota_units_observed_estimate AS quota_units_observed_estimate,
    j.max_target_attempts AS max_target_attempts,
    CASE WHEN isfinite(j.requested_at) AND j.requested_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN j.summary_json ELSE '{}'::jsonb END AS summary_json,
    CASE WHEN isfinite(j.requested_at) AND j.requested_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN j.last_error_code END AS last_error_code,
    CASE WHEN isfinite(j.requested_at) AND j.requested_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN j.last_error_summary END AS last_error_summary,
    j.phase AS phase,
    CASE WHEN isfinite(j.requested_at) AND j.requested_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN j.resume_page_token END AS resume_page_token,
    j.max_pages AS max_pages,
    j.pages_processed AS pages_processed,
    j.items_processed AS items_processed,
    j.inventory_inserted_count AS inventory_inserted_count,
    j.inventory_updated_count AS inventory_updated_count,
    j.initial_video_types AS initial_video_types,
    j.inventory_video_type AS inventory_video_type,
    j.retry_not_before AS retry_not_before,
    j.lease_channel_epoch AS lease_channel_epoch,
    j.accepted_epoch AS accepted_epoch,
    j.inventory_scan_id AS inventory_scan_id,
    CASE WHEN isfinite(j.requested_at) AND j.requested_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN j.inventory_checkpoint ELSE '{}'::jsonb END AS inventory_checkpoint,
    j.initial_scope_version AS initial_scope_version,
    j.targets_frozen_at AS targets_frozen_at,
    CASE WHEN isfinite(j.requested_at) AND j.requested_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN j.requested_at+interval '696 hours' END AS source_expires_at
FROM public.operator_jobs j;
ALTER VIEW readable.operator_jobs OWNER TO postgres;
GRANT SELECT ON readable.operator_jobs TO service_role;

CREATE VIEW readable.open_video_availability WITH (security_barrier=true, security_invoker=false) AS
SELECT a.video_id,a.channel_id,
    coalesce(a.latest_status,'unknown_unavailable'::text) AS availability_status,
    CASE WHEN isfinite(a.first_seen_at) AND a.first_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN a.first_seen_at END AS first_seen_at,
    a.last_seen_at,a.attempt_count,
    least(a.last_seen_at,CASE WHEN isfinite(a.first_seen_at) AND a.first_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN a.first_seen_at END)+interval '696 hours' AS source_expires_at
FROM public.video_availability_incidents a
WHERE a.restored_at IS NULL AND isfinite(a.last_seen_at) AND a.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp();
ALTER VIEW readable.open_video_availability OWNER TO postgres;
GRANT SELECT ON readable.open_video_availability TO service_role;

-- The acquired category labels share one document and one acquisition clock.
CREATE TABLE public.youtube_category_reference (
    singleton boolean DEFAULT true PRIMARY KEY CHECK (singleton),
    retrieved_at timestamp with time zone NOT NULL,
    document jsonb NOT NULL,
    CONSTRAINT youtube_category_reference_document CHECK (
        jsonb_typeof(document)='object' AND octet_length(document::text)<=2097152),
    CONSTRAINT youtube_category_reference_clock CHECK (
        document->>'retrieved_at' IS NOT NULL
        AND (document->>'retrieved_at')::timestamptz=retrieved_at)
);
ALTER TABLE public.youtube_category_reference OWNER TO postgres;
ALTER TABLE public.youtube_category_reference ENABLE ROW LEVEL SECURITY;
CREATE INDEX youtube_category_reference_retrieved_idx
    ON public.youtube_category_reference (retrieved_at);
REVOKE ALL ON public.youtube_category_reference FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.youtube_category_reference TO service_role;

CREATE VIEW readable.youtube_category_reference WITH (security_barrier=true, security_invoker=false) AS
SELECT singleton,retrieved_at,document,
    retrieved_at+interval '696 hours' AS source_expires_at
FROM public.youtube_category_reference
WHERE isfinite(retrieved_at)
  AND retrieved_at>statement_timestamp()-interval '696 hours'
  AND retrieved_at<=statement_timestamp();
ALTER VIEW readable.youtube_category_reference OWNER TO postgres;
GRANT SELECT ON readable.youtube_category_reference TO service_role;

-- HTTP reader role projections and exact user-work authority (R07f).

GRANT USAGE ON SCHEMA public TO lic_reader;
GRANT USAGE ON SCHEMA readable TO lic_reader;

CREATE VIEW readable.channel_user_work WITH (security_barrier=true, security_invoker=false) AS
SELECT channel.channel_id,channel.user_work_id,channel.user_input,channel.folder_id,
 channel.parent_channel_id,channel.added_at,coalesce(control.is_hidden,true) AS is_hidden,control.hidden_at
 FROM public.channels channel LEFT JOIN public.channel_collection_state control USING(channel_id);
ALTER VIEW readable.channel_user_work OWNER TO postgres;
GRANT SELECT ON readable.channel_user_work TO service_role;
GRANT SELECT ON readable.channel_user_work TO lic_reader;

CREATE VIEW readable.video_type_user_work WITH (security_barrier=true, security_invoker=false) AS
SELECT case_id,video_id,channel_id,source_detached_at,state,revision,decision_type,decision_reason,decision_at,decision_revision,first_seen_at,last_seen_at,user_channel_ref,restore_owner_ref,blocked_reason IS NULL AS decision_unblocked FROM public.video_type_review_cases;
ALTER VIEW readable.video_type_user_work OWNER TO postgres;
GRANT SELECT ON readable.video_type_user_work TO service_role;
GRANT SELECT ON readable.video_type_user_work TO lic_reader;

CREATE VIEW readable.retained_video_identities WITH (security_barrier=true, security_invoker=false) AS
SELECT video_id,channel_id,skeleton_since IS NOT NULL AS is_skeleton FROM public.video_inventory;
ALTER VIEW readable.retained_video_identities OWNER TO postgres;
GRANT SELECT ON readable.retained_video_identities TO service_role;
GRANT SELECT ON readable.retained_video_identities TO lic_reader;

CREATE VIEW readable.canonical_inventory_counts WITH (security_barrier=true, security_invoker=false) AS
SELECT channel_id,video_type,count(*)::bigint AS canonical_count FROM public.video_inventory GROUP BY channel_id,video_type;
ALTER VIEW readable.canonical_inventory_counts OWNER TO postgres;
GRANT SELECT ON readable.canonical_inventory_counts TO service_role;
GRANT SELECT ON readable.canonical_inventory_counts TO lic_reader;

CREATE VIEW readable.channel_scan_sources WITH (security_barrier=true, security_invoker=false) AS
SELECT scan.scan_id,scan.channel_id,scan.video_type,scan.playlist_id,scan.status,scan.coverage,scan.completed_at,scan.scan_kind,scan.source_checked_at,scan.started_at,scan.resume_page_token,scan.scan_boundary_at,scan.token_history_verified,least(scan.started_at,scan.scan_boundary_at,scan.source_checked_at,scan.completed_at)+interval '696 hours' AS source_expires_at FROM public.channel_video_inventory_scans scan WHERE coalesce(
 isfinite(statement_timestamp())
 AND isfinite(scan.started_at)
 AND scan.started_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 AND isfinite(scan.scan_boundary_at)
 AND scan.scan_boundary_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
 AND ((isfinite(scan.source_checked_at)
       AND scan.source_checked_at BETWEEN scan.scan_boundary_at AND statement_timestamp())
   OR (scan.source_checked_at IS NULL AND scan.status='running' AND scan.coverage='partial'
       AND scan.pages_completed=0 AND scan.items_seen=0 AND scan.new_memberships_found=0
       AND scan.resume_page_token IS NULL AND scan.last_transition_id IS NULL AND scan.completed_at IS NULL))
 AND (scan.status<>'completed' OR (isfinite(scan.completed_at)
       AND scan.completed_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp())),false);
ALTER VIEW readable.channel_scan_sources OWNER TO postgres;
GRANT SELECT ON readable.channel_scan_sources TO service_role;
GRANT SELECT ON readable.channel_scan_sources TO lic_reader;

CREATE VIEW readable.video_hydration_proof WITH (security_barrier=true, security_invoker=false) AS
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
ALTER VIEW readable.video_hydration_proof OWNER TO postgres;
GRANT SELECT ON readable.video_hydration_proof TO service_role;
GRANT SELECT ON readable.video_hydration_proof TO lic_reader;

CREATE VIEW readable.channel_discovery_health WITH (security_barrier=true, security_invoker=false) AS
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
ALTER VIEW readable.channel_discovery_health OWNER TO postgres;
GRANT SELECT ON readable.channel_discovery_health TO service_role;
GRANT SELECT ON readable.channel_discovery_health TO lic_reader;

CREATE VIEW readable.channel_sync_health WITH (security_barrier=true, security_invoker=false) AS
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
ALTER VIEW readable.channel_sync_health OWNER TO postgres;
GRANT SELECT ON readable.channel_sync_health TO service_role;
GRANT SELECT ON readable.channel_sync_health TO lic_reader;

CREATE VIEW readable.video_collection_health WITH (security_barrier=true, security_invoker=false) AS
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
ALTER VIEW readable.video_collection_health OWNER TO postgres;
GRANT SELECT ON readable.video_collection_health TO service_role;
GRANT SELECT ON readable.video_collection_health TO lic_reader;

CREATE VIEW readable.typed_video_collection_health WITH (security_barrier=true, security_invoker=false) AS
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
ALTER VIEW readable.typed_video_collection_health OWNER TO postgres;
GRANT SELECT ON readable.typed_video_collection_health TO service_role;
GRANT SELECT ON readable.typed_video_collection_health TO lic_reader;

CREATE VIEW readable.quota_attempts WITH (security_barrier=true, security_invoker=false) AS
SELECT project_id,quota_day,quota_bucket,quota_units,phase,endpoint,status,http_status,error_type,duration_ms,started_at,request_attempt_id FROM public.youtube_api_request_attempts;
ALTER VIEW readable.quota_attempts OWNER TO postgres;
GRANT SELECT ON readable.quota_attempts TO service_role;
GRANT SELECT ON readable.quota_attempts TO lic_reader;

CREATE VIEW readable.sync_runs WITH (security_barrier=true, security_invoker=false) AS
SELECT run.id,run.run_type,run.status,run.started_at,run.finished_at,run.duration_seconds,run.errors,run.metadata_processed,run.metadata_updated,run.video_count_changed,run.discovery_processed,run.new_videos,run.new_video_stats_updated,run.regular_stats_requested,run.regular_stats_updated,run.stats_updated,run.unavailable_marked,run.missing,CASE WHEN isfinite(run.started_at) AND run.started_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() THEN run.summary_json END AS summary_json,CASE WHEN isfinite(run.started_at) AND run.started_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() AND run.summary_json IS NOT NULL THEN run.started_at+interval '696 hours' END AS source_expires_at FROM public.sync_runs run;
ALTER VIEW readable.sync_runs OWNER TO postgres;
GRANT SELECT ON readable.sync_runs TO service_role;
GRANT SELECT ON readable.sync_runs TO lic_reader;

GRANT SELECT ON readable.videos TO lic_reader;

GRANT SELECT ON readable.channels TO lic_reader;

GRANT SELECT ON readable.video_inventory_memberships TO lic_reader;

GRANT SELECT ON readable.video_inventory TO lic_reader;

GRANT SELECT ON readable.video_inventory_type_resolution TO lic_reader;

GRANT SELECT ON readable.hot_video_inventory TO lic_reader;

GRANT SELECT ON readable.channel_playlist_state TO lic_reader;

GRANT SELECT ON readable.channel_video_inventory_state TO lic_reader;

GRANT SELECT ON readable.channels_with_control TO lic_reader;

GRANT SELECT ON readable.video_snapshots TO lic_reader;

GRANT SELECT ON readable.channel_snapshots TO lic_reader;

GRANT SELECT ON readable.video_type_review_cases TO lic_reader;

GRANT SELECT ON readable.operator_jobs TO lic_reader;

GRANT SELECT ON readable.open_video_availability TO lic_reader;

GRANT SELECT ON readable.youtube_category_reference TO lic_reader;

GRANT SELECT ON public.folders TO lic_reader;
CREATE POLICY lic_reader_select ON public.folders FOR SELECT TO lic_reader USING (true);

GRANT SELECT ON public.channel_labels TO lic_reader;
CREATE POLICY lic_reader_select ON public.channel_labels FOR SELECT TO lic_reader USING (true);

GRANT SELECT ON public.channel_label_assignments TO lic_reader;
CREATE POLICY lic_reader_select ON public.channel_label_assignments FOR SELECT TO lic_reader USING (true);

GRANT SELECT ON public.saved_objects TO lic_reader;
CREATE POLICY lic_reader_select ON public.saved_objects FOR SELECT TO lic_reader USING (true);

GRANT SELECT ON public.saved_collection_members TO lic_reader;
CREATE POLICY lic_reader_select ON public.saved_collection_members FOR SELECT TO lic_reader USING (true);

GRANT SELECT ON public.saved_collection_commands TO lic_reader;
CREATE POLICY lic_reader_select ON public.saved_collection_commands FOR SELECT TO lic_reader USING (true);

GRANT SELECT ON public.user_channel_restore_items TO lic_reader;
CREATE POLICY lic_reader_select ON public.user_channel_restore_items FOR SELECT TO lic_reader USING (true);

GRANT SELECT ON public.video_type_review_history TO lic_reader;
CREATE POLICY lic_reader_select ON public.video_type_review_history FOR SELECT TO lic_reader USING (true);

GRANT INSERT (evidence_expires_at) ON public.video_type_review_cases TO service_role;
GRANT UPDATE (evidence_expires_at) ON public.video_type_review_cases TO service_role;
DO $reader_user_work_acl$
DECLARE checked record;
BEGIN
 SELECT * INTO STRICT checked FROM (
WITH required_tables(table_name,privileges) AS (VALUES
 ('channels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('folders',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_labels',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('channel_label_assignments',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_objects',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[]),
 ('saved_collection_members',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('saved_collection_commands',ARRAY['SELECT','INSERT','DELETE']::text[]),
 ('video_type_review_cases',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('video_type_review_history',ARRAY['SELECT','MAINTAIN','DELETE']::text[]),
 ('user_channel_restore_items',ARRAY['SELECT','INSERT','UPDATE','DELETE']::text[])
), required_columns(table_name,column_name,privilege) AS (VALUES
 ('video_type_review_cases','case_id','INSERT'),
 ('video_type_review_cases','video_id','INSERT'),
 ('video_type_review_cases','channel_id','INSERT'),
 ('video_type_review_cases','source_detached_at','INSERT'),
 ('video_type_review_cases','observed_canonical_type','INSERT'),
 ('video_type_review_cases','observed_types','INSERT'),
 ('video_type_review_cases','observed_channels','INSERT'),
 ('video_type_review_cases','blocked_reason','INSERT'),
 ('video_type_review_cases','state','INSERT'),
 ('video_type_review_cases','revision','INSERT'),
 ('video_type_review_cases','decision_type','INSERT'),
 ('video_type_review_cases','decision_reason','INSERT'),
 ('video_type_review_cases','decision_at','INSERT'),
 ('video_type_review_cases','decision_revision','INSERT'),
 ('video_type_review_cases','first_seen_at','INSERT'),
 ('video_type_review_cases','last_seen_at','INSERT'),
 ('video_type_review_cases','evidence_expires_at','INSERT'),
 ('video_type_review_cases','restore_owner_ref','INSERT'),
 ('video_type_review_cases','user_channel_ref','INSERT'),
 ('video_type_review_history','case_id','INSERT'),
 ('video_type_review_history','revision','INSERT'),
 ('video_type_review_history','video_type','INSERT'),
 ('video_type_review_history','reason','INSERT'),
 ('video_type_review_history','decided_at','INSERT'),
 ('video_type_review_history','request_id','INSERT'),
 ('video_type_review_history','expected_revision','INSERT'),
 ('saved_collection_members','user_channel_ref','UPDATE'),
 ('saved_collection_members','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','user_channel_ref','UPDATE'),
 ('video_type_review_cases','restore_owner_ref','UPDATE'),
 ('video_type_review_cases','observed_canonical_type','UPDATE'),
 ('video_type_review_cases','observed_types','UPDATE'),
 ('video_type_review_cases','observed_channels','UPDATE'),
 ('video_type_review_cases','blocked_reason','UPDATE'),
 ('video_type_review_cases','revision','UPDATE'),
 ('video_type_review_cases','decision_type','UPDATE'),
 ('video_type_review_cases','decision_reason','UPDATE'),
 ('video_type_review_cases','decision_at','UPDATE'),
 ('video_type_review_cases','decision_revision','UPDATE'),
 ('video_type_review_cases','state','UPDATE'),
 ('video_type_review_cases','last_seen_at','UPDATE'),
 ('video_type_review_cases','evidence_expires_at','UPDATE'),
 ('video_type_review_cases','video_id','UPDATE'),
 ('video_type_review_cases','channel_id','UPDATE'),
 ('video_type_review_cases','source_detached_at','UPDATE')
), roles AS (
 SELECT (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='lic_reader') AS reader_oid,
 coalesce((SELECT NOT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin AND NOT rolinherit
  AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members membership WHERE membership.member=reader.oid)
  FROM pg_catalog.pg_roles reader WHERE rolname='lic_reader'),false) AS reader_valid,
 (SELECT oid FROM pg_catalog.pg_roles WHERE rolname='service_role') AS service_oid,
 coalesce((SELECT rolbypassrls AND NOT rolsuper AND NOT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname='service_role'),false) AS valid
), relations AS MATERIALIZED (
 SELECT expected.*,relation.oid,relation.relowner,relation.relacl,relation.relkind,relation.relrowsecurity
 FROM required_tables expected LEFT JOIN pg_catalog.pg_class relation
 ON relation.oid=pg_catalog.to_regclass('public.'||expected.table_name)
), table_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER'),('MAINTAIN')),
 column_privileges(privilege) AS (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES'))
SELECT roles.valid AND roles.reader_valid AS roles_valid,
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
  WHERE acl.grantee<>r.relowner AND (acl.is_grantable OR NOT (
   (acl.grantee=roles.service_oid AND acl.privilege_type=ANY(r.privileges))
   OR (acl.grantee=roles.reader_oid AND r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history')
       AND acl.privilege_type='SELECT'))))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) acl
  WHERE a.attnum>0 AND NOT a.attisdropped AND acl.grantee<>r.relowner
  AND (acl.grantee IS DISTINCT FROM roles.service_oid OR acl.is_grantable OR NOT EXISTS(
   SELECT 1 FROM required_columns required WHERE required.table_name=r.table_name
    AND required.column_name=a.attname AND required.privilege=acl.privilege_type)))
 AND NOT EXISTS(SELECT 1 FROM relations r CROSS JOIN table_privileges p
  WHERE pg_catalog.has_table_privilege(roles.reader_oid,r.oid,p.privilege)
   IS DISTINCT FROM (r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history') AND p.privilege='SELECT'))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.oid
  CROSS JOIN column_privileges p WHERE a.attnum>0 AND NOT a.attisdropped
  AND pg_catalog.has_column_privilege(roles.reader_oid,r.oid,a.attnum,p.privilege)
   IS DISTINCT FROM (r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history') AND p.privilege='SELECT'))
 AND NOT EXISTS(SELECT 1 FROM relations r WHERE r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history')
  AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_policy policy WHERE policy.polrelid=r.oid
    AND policy.polname='lic_reader_select' AND policy.polcmd='r' AND policy.polpermissive
    AND policy.polroles=ARRAY[roles.reader_oid]::oid[]
    AND pg_catalog.pg_get_expr(policy.polqual,policy.polrelid)='true' AND policy.polwithcheck IS NULL))
 AND NOT EXISTS(SELECT 1 FROM relations r JOIN pg_catalog.pg_policy policy ON policy.polrelid=r.oid
  WHERE (roles.reader_oid=ANY(policy.polroles) OR 0=ANY(policy.polroles))
   AND NOT (r.table_name IN ('folders','channel_labels','channel_label_assignments','saved_objects','saved_collection_members','saved_collection_commands','user_channel_restore_items','video_type_review_history') AND policy.polname='lic_reader_select'
    AND policy.polcmd='r' AND policy.polpermissive AND policy.polroles=ARRAY[roles.reader_oid]::oid[]
    AND pg_catalog.pg_get_expr(policy.polqual,policy.polrelid)='true' AND policy.polwithcheck IS NULL)) AS privileges_valid
FROM roles
 ) reader_user_work;
 IF NOT coalesce(checked.roles_valid AND checked.relations_valid AND checked.privileges_valid,false)
 THEN RAISE EXCEPTION 'user_work_restore_acl_invalid'; END IF;
END $reader_user_work_acl$;

-- Release baseline and administrative migration receipts. No API/user data.
CREATE TABLE operator_private.schema_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    baseline_id text NOT NULL CHECK (baseline_id = 'leti-cook-release-01'),
    initial_version integer NOT NULL CHECK (initial_version >= 25),
    current_version integer NOT NULL CHECK (current_version >= initial_version)
);
ALTER TABLE operator_private.schema_state OWNER TO postgres;
ALTER TABLE operator_private.schema_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE operator_private.schema_state FROM PUBLIC,anon,authenticated,authenticator,service_role,lic_reader;

CREATE TABLE operator_private.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 25),
    filename text NOT NULL UNIQUE CHECK (filename ~ '^steps/[0-9]{4}-[a-z][a-z0-9-]*[.]sql$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at timestamp with time zone NOT NULL DEFAULT statement_timestamp() CHECK (isfinite(applied_at))
);
ALTER TABLE operator_private.schema_migrations OWNER TO postgres;
ALTER TABLE operator_private.schema_migrations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE operator_private.schema_migrations FROM PUBLIC,anon,authenticated,authenticator,service_role,lic_reader;

INSERT INTO operator_private.schema_state(singleton,baseline_id,initial_version,current_version)
VALUES (true,'leti-cook-release-01',34,34);

CREATE VIEW readable.schema_version WITH (security_barrier=true,security_invoker=false) AS
SELECT baseline_id,current_version FROM operator_private.schema_state WHERE singleton;
ALTER VIEW readable.schema_version OWNER TO postgres;
REVOKE ALL ON TABLE readable.schema_version FROM PUBLIC,anon,authenticated,authenticator,service_role,lic_reader;
GRANT SELECT ON TABLE readable.schema_version TO service_role,lic_reader;

-- The scheduling predicate checks wall time; no time-dependent CHECK or partial predicate.
CREATE INDEX user_channel_restore_items_reconnect_fair_idx
 ON public.user_channel_restore_items
 (reconnect_last_attempt_at NULLS FIRST,reconnect_requested_at,id)
 WHERE reconnect_state IN ('queued','waiting','running');


-- Bounded inventory chains and fair dispatch; intrinsic integrity only.
ALTER TABLE public.operator_jobs
 ADD CONSTRAINT operator_jobs_id_channel_key UNIQUE(id,channel_id);

CREATE TABLE public.operator_job_inventory_chains (
    job_id uuid NOT NULL,
    channel_id text NOT NULL,
    video_type text NOT NULL CONSTRAINT inventory_chains_type_check CHECK(video_type IN('long','short','stream')),
    scan_id uuid,
    checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_ack jsonb NOT NULL DEFAULT '{}'::jsonb,
    terminal_outcome text CONSTRAINT inventory_chains_terminal_check CHECK(terminal_outcome IN('completed','playlist_not_found')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT inventory_chains_pkey PRIMARY KEY(job_id,video_type),
    CONSTRAINT inventory_chains_parent_fkey FOREIGN KEY(job_id,channel_id) REFERENCES public.operator_jobs(id,channel_id) ON DELETE CASCADE,
    CONSTRAINT inventory_chains_scan_fkey FOREIGN KEY(scan_id,channel_id,video_type)
  REFERENCES public.channel_video_inventory_scans(scan_id,channel_id,video_type)
  DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT inventory_chains_terminal_source_check CHECK(terminal_outcome IS NULL OR (scan_id IS NOT NULL AND checkpoint='{}'::jsonb)),
    CONSTRAINT inventory_chains_checkpoint_check CHECK((checkpoint='{}'::jsonb OR (
  jsonb_typeof(checkpoint)='object'
  AND checkpoint->>'phase' IN('prepared','bound')
  AND checkpoint->>'job_lease_token' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  AND checkpoint->>'transition_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
  AND checkpoint->'reserved_units'='1'::jsonb
  AND ((checkpoint->>'phase'='prepared'
   AND checkpoint-ARRAY['phase','job_lease_token','transition_id','reserved_units']='{}'::jsonb)
   OR (checkpoint->>'phase'='bound' AND scan_id IS NOT NULL
    AND checkpoint->>'playlist_lease_token' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND checkpoint-ARRAY['phase','job_lease_token','transition_id','reserved_units','playlist_lease_token']='{}'::jsonb))
 )) IS TRUE),
    CONSTRAINT inventory_chains_ack_object_check CHECK(jsonb_typeof(last_ack)='object'),
    CONSTRAINT inventory_chains_updated_check CHECK(isfinite(updated_at))
);
ALTER TABLE public.operator_job_inventory_chains ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.operator_job_inventory_chains FROM PUBLIC,lic_reader;
-- Exact column grants follow below.
-- service_role existing BYPASSRLS contract applies; no lic_reader raw grant.
-- PROPOSED ONLY; not executed, not a complete migration.
-- Companion to chains-schema-readiness-retention-plan.md.
-- Merge with table CREATE; do not install the original permissive ACK/ACL first.
-- Existing checkpoint exactness/FKs/RLS remain; intrinsic guards and retention
-- described in the companion are mandatory parts of the SAME accepted release.

ALTER TABLE public.operator_job_inventory_chains
 ADD CONSTRAINT inventory_chains_checkpoint_size CHECK (octet_length(checkpoint::text)<=512),
 ADD CONSTRAINT inventory_chains_last_ack_exact CHECK ((
  last_ack='{}'::jsonb OR (
   jsonb_typeof(last_ack)='object' AND octet_length(last_ack::text)<=2048
   AND last_ack ?& ARRAY['version','kind','channel_id','video_type','transition_id',
     'job_lease_token','scan_id','playlist_lease_token','request_hash',
     'provider_attempts','quota_estimated','outcome','error_code']::text[]
   AND last_ack-ARRAY['version','kind','channel_id','video_type','transition_id',
     'job_lease_token','scan_id','playlist_lease_token','request_hash',
     'provider_attempts','quota_estimated','outcome','error_code']::text[]='{}'::jsonb
   AND last_ack->'version'='1'::jsonb
   AND jsonb_typeof(last_ack->'kind')='string' AND last_ack->>'kind' IN('settled','failed')
   AND jsonb_typeof(last_ack->'channel_id')='string' AND last_ack->>'channel_id'=channel_id
   AND jsonb_typeof(last_ack->'video_type')='string' AND last_ack->>'video_type'=video_type
   AND jsonb_typeof(last_ack->'transition_id')='string'
   AND last_ack->>'transition_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
   AND jsonb_typeof(last_ack->'job_lease_token')='string'
   AND last_ack->>'job_lease_token' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
   AND (last_ack->'scan_id'='null'::jsonb OR (
    jsonb_typeof(last_ack->'scan_id')='string'
    AND last_ack->>'scan_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
   AND (last_ack->'playlist_lease_token'='null'::jsonb OR (
    jsonb_typeof(last_ack->'playlist_lease_token')='string'
    AND last_ack->>'playlist_lease_token' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
   AND jsonb_typeof(last_ack->'request_hash')='string'
   AND last_ack->>'request_hash' ~ '^[0-9a-f]{32}$'
   AND last_ack->'provider_attempts' IN('0'::jsonb,'1'::jsonb)
   AND last_ack->'quota_estimated'='true'::jsonb
   AND jsonb_typeof(last_ack->'outcome')='string'
   AND last_ack->>'outcome' IN('none','page','playlist_not_found','error','quota','deferred','unknown')
   AND (last_ack->'error_code'='null'::jsonb OR (
    jsonb_typeof(last_ack->'error_code')='string'
    AND last_ack->>'error_code' ~ '^[a-z0-9_]{1,120}$'))
  )
 ) IS TRUE);

ALTER TABLE public.operator_jobs
 ADD CONSTRAINT operator_jobs_inventory_chain_version_check CHECK (
  (NOT(summary_json ? 'inventory_chain_version')
   OR summary_json->'inventory_chain_version'='1'::jsonb) IS TRUE);

CREATE INDEX inventory_chains_scan_pin_idx
 ON public.operator_job_inventory_chains(scan_id,job_id,video_type)
 WHERE scan_id IS NOT NULL;
CREATE INDEX inventory_chains_ack_scan_pin_idx
 ON public.operator_job_inventory_chains((last_ack->>'scan_id'),job_id,video_type)
 WHERE jsonb_typeof(last_ack->'scan_id')='string';
CREATE INDEX inventory_chains_bound_transition_pin_idx
 ON public.operator_job_inventory_chains((checkpoint->>'transition_id'),job_id,video_type)
 WHERE checkpoint->>'phase'='bound';
CREATE INDEX inventory_chains_ack_transition_pin_idx
 ON public.operator_job_inventory_chains((last_ack->>'transition_id'),job_id,video_type)
 WHERE last_ack<>'{}'::jsonb;

-- In the fresh CREATE path no column grants exist yet; readiness must also
-- reject any extra column grant in migration/catalog tampering cases.
REVOKE ALL ON public.operator_job_inventory_chains
 FROM PUBLIC,anon,authenticated,lic_reader,service_role;
GRANT SELECT ON public.operator_job_inventory_chains TO service_role;
GRANT INSERT(job_id,channel_id,video_type,scan_id,checkpoint,last_ack,terminal_outcome)
 ON public.operator_job_inventory_chains TO service_role;
GRANT UPDATE(scan_id,checkpoint,last_ack,terminal_outcome,updated_at)
 ON public.operator_job_inventory_chains TO service_role;
-- No direct child DELETE: exactly-three integrity and parent ON DELETE CASCADE.
-- No child scan uniqueness: multiple historical parents may reference one scan.
-- No dynamic clock CHECK: 696h freshness remains native owner/read-time policy.


CREATE FUNCTION public.guard_inventory_chain_identity() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER
    SET search_path TO 'pg_catalog','public','pg_temp'
    AS $body$
BEGIN
 IF NEW.job_id IS DISTINCT FROM OLD.job_id OR NEW.channel_id IS DISTINCT FROM OLD.channel_id
 OR NEW.video_type IS DISTINCT FROM OLD.video_type THEN
  RAISE EXCEPTION 'inventory_chain_identity_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END
$body$;

CREATE FUNCTION public.guard_inventory_chain_parent_version() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER
    SET search_path TO 'pg_catalog','public','pg_temp'
    AS $body$
BEGIN
 IF NEW.summary_json ? 'inventory_chain_version'
 AND NEW.summary_json->'inventory_chain_version' IS DISTINCT FROM '1'::jsonb THEN
  RAISE EXCEPTION 'inventory_chain_version_invalid' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' AND OLD.summary_json->'inventory_chain_version'='1'::jsonb THEN
  IF NEW.summary_json->'inventory_chain_version' IS DISTINCT FROM '1'::jsonb
  OR NEW.job_type IS DISTINCT FROM OLD.job_type OR NEW.channel_id IS DISTINCT FROM OLD.channel_id
  OR NEW.initial_scope_version IS DISTINCT FROM OLD.initial_scope_version THEN
   RAISE EXCEPTION 'inventory_chain_version_immutable' USING ERRCODE='23514';
  END IF;
 END IF;
 IF NEW.summary_json->'inventory_chain_version'='1'::jsonb AND (
  NEW.job_type IS DISTINCT FROM 'channel_initial_long_inventory'
  OR NEW.initial_scope_version IS DISTINCT FROM 1
  OR NEW.initial_video_types IS DISTINCT FROM ARRAY['long','short','stream']::text[]
  OR NEW.max_items IS NOT NULL OR NEW.max_pages IS NOT NULL OR NEW.max_quota_units IS NOT NULL) THEN
  RAISE EXCEPTION 'inventory_chain_parent_scope_invalid' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END
$body$;

CREATE FUNCTION public.guard_inventory_chain_consistency() RETURNS trigger
    LANGUAGE plpgsql SECURITY INVOKER
    SET search_path TO 'pg_catalog','public','pg_temp'
    AS $body$
DECLARE parent_id uuid; parent public.operator_jobs%ROWTYPE;
 child public.operator_job_inventory_chains%ROWTYPE;
 scan public.channel_video_inventory_scans%ROWTYPE;
 receipt public.typed_inventory_receipts%ROWTYPE;
 child_count integer; terminal_count integer:=0; proof jsonb; source_at timestamptz; transition uuid;
BEGIN
 IF TG_TABLE_NAME='operator_job_inventory_chains' THEN
  IF TG_OP='DELETE' THEN parent_id:=OLD.job_id; ELSE parent_id:=NEW.job_id; END IF;
 ELSIF TG_TABLE_NAME='operator_jobs' THEN parent_id:=NEW.id;
 ELSE RAISE EXCEPTION 'inventory_chain_guard_relation_invalid' USING ERRCODE='23514';
 END IF;
 SELECT * INTO parent FROM public.operator_jobs WHERE id=parent_id FOR UPDATE;
 IF NOT FOUND THEN
  IF TG_TABLE_NAME='operator_job_inventory_chains' AND TG_OP='DELETE' THEN RETURN NULL; END IF;
  RAISE EXCEPTION 'inventory_chain_parent_missing' USING ERRCODE='23514';
 END IF;
 SELECT count(*) INTO child_count FROM public.operator_job_inventory_chains WHERE job_id=parent_id;
 IF NOT(parent.summary_json ? 'inventory_chain_version') THEN
  IF child_count<>0 THEN RAISE EXCEPTION 'inventory_chain_marker_required' USING ERRCODE='23514'; END IF;
  RETURN NULL;
 END IF;
 IF parent.summary_json->'inventory_chain_version' IS DISTINCT FROM '1'::jsonb OR child_count<>3
 OR parent.job_type IS DISTINCT FROM 'channel_initial_long_inventory' OR parent.initial_scope_version IS DISTINCT FROM 1
 OR parent.initial_video_types IS DISTINCT FROM ARRAY['long','short','stream']::text[]
 OR parent.max_items IS NOT NULL OR parent.max_pages IS NOT NULL OR parent.max_quota_units IS NOT NULL
 OR parent.inventory_checkpoint<>'{}'::jsonb OR parent.inventory_scan_id IS NOT NULL OR parent.resume_page_token IS NOT NULL THEN
  RAISE EXCEPTION 'inventory_chain_parent_invalid' USING ERRCODE='23514';
 END IF;
 -- Expired copies keep identity/version only; the ordinary owner clears all
 -- three children and the parent in one transaction before this deferred check.
 IF parent.summary_json ?| ARRAY['source_scope_expired','inventory_checkpoint_expired']::text[] THEN
  IF parent.status NOT IN('error','completed') OR parent.lease_token IS NOT NULL
  OR parent.lease_owner IS NOT NULL OR parent.lease_channel_epoch IS NOT NULL OR parent.lease_expires_at IS NOT NULL
  OR parent.summary_json ?| ARRAY['full_inventory_sources','inventory_ack']::text[]
  OR (parent.summary_json ? 'source_scope_expired' AND
   (parent.summary_json<>'{"source_scope_expired":true,"inventory_chain_version":1}'::jsonb
    OR parent.last_error_code IS DISTINCT FROM 'source_scope_expired'))
  OR (NOT(parent.summary_json ? 'source_scope_expired') AND
   (parent.summary_json->'inventory_checkpoint_expired' IS DISTINCT FROM 'true'::jsonb
    OR parent.last_error_code IS DISTINCT FROM 'inventory_checkpoint_expired'))
  OR EXISTS(SELECT 1 FROM public.operator_job_inventory_chains WHERE job_id=parent_id
   AND (channel_id<>parent.channel_id OR scan_id IS NOT NULL OR checkpoint<>'{}'::jsonb
    OR last_ack<>'{}'::jsonb OR terminal_outcome IS NOT NULL)) THEN
   RAISE EXCEPTION 'inventory_chain_expired_copy_invalid' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
 END IF;
 IF jsonb_typeof(parent.summary_json->'full_inventory_sources') IS DISTINCT FROM 'object'
 OR (parent.summary_json->'full_inventory_sources')-ARRAY['long','short','stream']::text[]<>'{}'::jsonb THEN
  RAISE EXCEPTION 'inventory_chain_source_shape_invalid' USING ERRCODE='23514';
 END IF;
 FOR child IN SELECT * FROM public.operator_job_inventory_chains WHERE job_id=parent_id ORDER BY video_type LOOP
  proof:=parent.summary_json->'full_inventory_sources'->child.video_type;
  IF child.channel_id<>parent.channel_id
  OR child.terminal_outcome IS DISTINCT FROM proof->>'terminal_outcome'
  OR (child.checkpoint<>'{}'::jsonb AND (parent.phase<>'inventory' OR parent.targets_frozen_at IS NOT NULL)) THEN
   RAISE EXCEPTION 'inventory_chain_source_identity_invalid' USING ERRCODE='23514';
  END IF;
  IF child.scan_id IS NULL THEN
   IF proof IS NOT NULL THEN RAISE EXCEPTION 'inventory_chain_source_identity_invalid' USING ERRCODE='23514'; END IF;
   CONTINUE;
  END IF;
  SELECT * INTO scan FROM public.channel_video_inventory_scans WHERE scan_id=child.scan_id;
  IF NOT FOUND OR scan.channel_id<>parent.channel_id OR scan.video_type<>child.video_type
  OR scan.scan_kind NOT IN('initial_inventory','full_reconciliation')
  OR NOT isfinite(scan.started_at) OR NOT isfinite(scan.scan_boundary_at)
  OR scan.started_at<parent.requested_at OR scan.scan_boundary_at<parent.requested_at
  OR jsonb_typeof(proof) IS DISTINCT FROM 'object'
  OR proof->>'scan_id' IS DISTINCT FROM child.scan_id::text
  OR NOT coalesce(pg_input_is_valid(proof->>'started_at','timestamptz'),false)
  OR NOT coalesce(pg_input_is_valid(proof->>'scan_boundary_at','timestamptz'),false) THEN
   RAISE EXCEPTION 'inventory_chain_source_identity_invalid' USING ERRCODE='23514';
  END IF;
  IF (proof->>'started_at')::timestamptz IS DISTINCT FROM scan.started_at
  OR (proof->>'scan_boundary_at')::timestamptz IS DISTINCT FROM scan.scan_boundary_at THEN
   RAISE EXCEPTION 'inventory_chain_source_identity_invalid' USING ERRCODE='23514';
  END IF;
  IF child.terminal_outcome IS NOT NULL THEN
   terminal_count:=terminal_count+1;
   IF child.checkpoint<>'{}'::jsonb
   OR NOT coalesce(proof->>'acked_pages' ~ '^[0-9]{1,10}$',false)
   OR NOT coalesce(proof->>'last_transition_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',false)
   OR NOT coalesce(pg_input_is_valid(proof->>'source_checked_at','timestamptz'),false) THEN
    RAISE EXCEPTION 'inventory_chain_terminal_proof_invalid' USING ERRCODE='23514';
   END IF;
   source_at:=(proof->>'source_checked_at')::timestamptz;
   transition:=(proof->>'last_transition_id')::uuid;
   IF (proof->>'acked_pages')::bigint>2147483647 OR (proof->>'acked_pages')::bigint<>scan.pages_completed
   OR NOT isfinite(source_at) OR source_at<scan.scan_boundary_at
   OR (child.terminal_outcome='completed' AND (scan.status<>'completed' OR scan.coverage<>'complete')) THEN
    RAISE EXCEPTION 'inventory_chain_terminal_proof_invalid' USING ERRCODE='23514';
   END IF;
   SELECT * INTO receipt FROM public.typed_inventory_receipts WHERE receipt_scope='scan_transition' AND transition_id=transition;
   IF NOT FOUND OR receipt.scan_id IS DISTINCT FROM child.scan_id OR receipt.source_checked_at IS DISTINCT FROM source_at
   OR receipt.result ? 'source_expired'
   OR (child.terminal_outcome='completed' AND receipt.transition_kind IS DISTINCT FROM 'page')
   OR (child.terminal_outcome='playlist_not_found' AND
    (receipt.transition_kind IS DISTINCT FROM 'failure' OR receipt.result->>'outcome' IS DISTINCT FROM 'playlist_not_found')) THEN
    RAISE EXCEPTION 'inventory_chain_terminal_receipt_invalid' USING ERRCODE='23514';
   END IF;
  END IF;
 END LOOP;
 IF parent.phase IN('hydrate_videos','verify') AND terminal_count<>3 THEN
  RAISE EXCEPTION 'inventory_chain_terminal_scope_required' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END
$body$;

ALTER FUNCTION public.guard_inventory_chain_identity() OWNER TO postgres;
ALTER FUNCTION public.guard_inventory_chain_parent_version() OWNER TO postgres;
ALTER FUNCTION public.guard_inventory_chain_consistency() OWNER TO postgres;
REVOKE ALL ON FUNCTION public.guard_inventory_chain_identity() FROM PUBLIC,anon,authenticated,lic_reader,service_role;
REVOKE ALL ON FUNCTION public.guard_inventory_chain_parent_version() FROM PUBLIC,anon,authenticated,lic_reader,service_role;
REVOKE ALL ON FUNCTION public.guard_inventory_chain_consistency() FROM PUBLIC,anon,authenticated,lic_reader,service_role;
ALTER TABLE public.operator_job_inventory_chains OWNER TO postgres;

CREATE TRIGGER inventory_chain_identity_guard BEFORE UPDATE ON public.operator_job_inventory_chains
 FOR EACH ROW EXECUTE FUNCTION public.guard_inventory_chain_identity();
CREATE TRIGGER inventory_chain_parent_version_guard BEFORE INSERT OR UPDATE ON public.operator_jobs
 FOR EACH ROW EXECUTE FUNCTION public.guard_inventory_chain_parent_version();
CREATE CONSTRAINT TRIGGER inventory_chain_children_consistency_guard
 AFTER INSERT OR UPDATE OR DELETE ON public.operator_job_inventory_chains
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.guard_inventory_chain_consistency();
CREATE CONSTRAINT TRIGGER inventory_chain_parent_consistency_guard
 AFTER INSERT OR UPDATE ON public.operator_jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.guard_inventory_chain_consistency();

ALTER TABLE public.operator_jobs ADD COLUMN last_scheduled_at timestamptz;
ALTER TABLE public.operator_jobs
 ADD CONSTRAINT operator_jobs_last_scheduled_at_check CHECK (
  last_scheduled_at IS NULL OR
  (isfinite(last_scheduled_at) AND last_scheduled_at>=requested_at));
CREATE INDEX operator_jobs_fair_candidate_idx ON public.operator_jobs
 ((coalesce(last_scheduled_at,requested_at)),requested_at,id)
 WHERE status IN ('queued','running') AND isfinite(requested_at);

CREATE INDEX video_collection_state_channel_format_due_idx
 ON public.video_collection_state(channel_id,video_type,next_due_at,video_id)
 WHERE paused_reason IS NULL AND next_due_at IS NOT NULL;

CREATE INDEX channel_collection_state_dispatch_keyset_idx
 ON public.channel_collection_state((channel_id COLLATE "C"));

-- Data lifecycle: actual acquisition clocks and effective evidence expiry.
ALTER TABLE public.video_inventory ADD CONSTRAINT video_inventory_publication_source_pair CHECK
 ((published_at IS NULL AND published_acquired_at IS NULL) OR
  (published_at IS NOT NULL AND published_acquired_at IS NOT NULL AND isfinite(published_acquired_at)));
ALTER TABLE public.video_collection_state ADD CONSTRAINT video_collection_publication_source_pair CHECK
 ((typed_source_published_at IS NULL AND typed_source_published_acquired_at IS NULL) OR
  (typed_source_published_at IS NOT NULL AND typed_source_published_acquired_at IS NOT NULL
   AND isfinite(typed_source_published_acquired_at)));
ALTER TABLE public.video_collection_state ADD CONSTRAINT video_collection_stream_end_source_pair CHECK
 ((typed_stream_actual_end_at IS NULL AND typed_source_stream_end_acquired_at IS NULL) OR
  (typed_stream_actual_end_at IS NOT NULL AND typed_source_stream_end_acquired_at IS NOT NULL
   AND isfinite(typed_source_stream_end_acquired_at)));
ALTER TABLE public.video_type_review_cases ADD CONSTRAINT video_review_evidence_deadline_finite CHECK
 (evidence_expires_at IS NULL OR isfinite(evidence_expires_at));
CREATE INDEX video_inventory_publication_source_idx ON public.video_inventory(published_acquired_at)
 WHERE published_at IS NOT NULL;
CREATE INDEX video_collection_publication_source_idx ON public.video_collection_state(typed_source_published_acquired_at)
 WHERE typed_source_published_at IS NOT NULL;
CREATE INDEX video_collection_stream_end_source_idx ON public.video_collection_state(typed_source_stream_end_acquired_at)
 WHERE typed_stream_actual_end_at IS NOT NULL;
CREATE INDEX video_review_evidence_deadline_idx ON public.video_type_review_cases(evidence_expires_at)
 WHERE observed_canonical_type IS NOT NULL OR cardinality(observed_types)>0
  OR cardinality(observed_channels)>0 OR blocked_reason IS NOT NULL;

CREATE OR REPLACE VIEW readable.video_type_review_cases WITH (security_barrier=true, security_invoker=false) AS
SELECT c.video_id AS video_id,
    c.channel_id AS channel_id,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_canonical_type END AS observed_canonical_type,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_types ELSE ARRAY[]::text[] END AS observed_types,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_channels ELSE ARRAY[]::text[] END AS observed_channels,
    c.state AS state,
    c.revision AS revision,
    c.first_seen_at AS first_seen_at,
    c.last_seen_at AS last_seen_at,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.blocked_reason END AS blocked_reason,
    c.decision_type AS decision_type,
    c.decision_reason AS decision_reason,
    c.decision_at AS decision_at,
    c.decision_revision AS decision_revision,
    c.case_id AS case_id,
    c.source_detached_at AS source_detached_at,
    c.user_channel_ref AS user_channel_ref,
    c.restore_owner_ref AS restore_owner_ref,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.evidence_expires_at END AS source_expires_at,
    coalesce(c.video_id IS NOT NULL
      AND EXISTS(SELECT 1 FROM public.video_inventory i WHERE i.video_id=c.video_id AND i.channel_id=c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.video_collection_state q WHERE q.video_id=c.video_id AND q.channel_id<>c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=c.video_id
        AND m.membership_status='present' AND m.channel_id<>c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.videos v WHERE v.video_id=c.video_id AND v.channel_id<>c.channel_id),false)
      AS decision_unblocked
FROM public.video_type_review_cases c;

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

-- BEGIN automatic Long/Short policy: derived proof, no persisted decision.

-- Canonical query: application/db/inventory/automatic_type_policy.py.

CREATE VIEW public.video_type_automatic_candidates WITH (security_invoker=true, security_barrier=true) AS
SELECT evidence.video_id,
    evidence.channel_id,
    CASE WHEN duration_value.seconds <= 180 THEN 'short'::text ELSE 'long'::text END AS automatic_type,
    least(v.metadata_checked_at, evidence.oldest_clock)+interval '696 hours' AS source_expires_at
FROM (
    SELECT i.video_id,i.channel_id,
        min(least(m.last_seen_at, m.last_present_at)) AS oldest_clock
    FROM public.video_inventory i
    JOIN public.video_inventory_memberships m ON m.video_id=i.video_id AND m.membership_status='present'
    WHERE (i.video_type IS NULL OR i.video_type IN ('long','short'))
      AND NOT EXISTS (
          SELECT 1 FROM public.video_type_review_cases decision
          WHERE decision.video_id=i.video_id AND decision.decision_type IS NOT NULL
      )
    GROUP BY i.video_id,i.channel_id
    HAVING array_agg(DISTINCT m.video_type ORDER BY m.video_type)=ARRAY['long','short']::text[]
       AND bool_and((m.channel_id=i.channel_id
         AND isfinite(m.last_seen_at)
         AND m.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
         AND isfinite(m.last_present_at)
         AND m.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) IS TRUE)
) evidence
JOIN LATERAL (
    SELECT metadata.metadata_checked_at,metadata.duration
    FROM public.videos metadata
    WHERE metadata.video_id=evidence.video_id AND metadata.channel_id=evidence.channel_id
    OFFSET 0
) v ON true
CROSS JOIN LATERAL (
    SELECT regexp_match(v.duration, '^P(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)S)?)?$') AS parts
) duration_iso
CROSS JOIN LATERAL (
    SELECT CASE
      WHEN v.duration ~ '^[0-9]+([.][0-9]+)?$' THEN v.duration::numeric
      WHEN duration_iso.parts IS NOT NULL AND v.duration !~ 'T$' THEN
          coalesce(duration_iso.parts[1]::numeric,0)*86400
        + coalesce(duration_iso.parts[2]::numeric,0)*3600
        + coalesce(duration_iso.parts[3]::numeric,0)*60
        + coalesce(duration_iso.parts[4]::numeric,0)
    END AS seconds
) duration_value
WHERE isfinite(v.metadata_checked_at)
  AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
  AND duration_value.seconds > 0;
ALTER VIEW public.video_type_automatic_candidates OWNER TO postgres;
GRANT SELECT ON public.video_type_automatic_candidates TO service_role;

CREATE OR REPLACE VIEW public.video_inventory_type_resolution WITH (security_invoker='true', security_barrier='true') AS
 WITH evidence AS (
         SELECT i.video_id,
            i.channel_id AS canonical_channel_id,
            i.video_type AS canonical_video_type,
            COALESCE(array_agg(DISTINCT m.video_type ORDER BY m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_types,
            COALESCE(array_agg(DISTINCT m.channel_id ORDER BY m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_channels,
            count(DISTINCT m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_type_count,
            count(DISTINCT m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_channel_count,
            COALESCE(bool_or(((m.membership_status = 'present'::text) AND (m.channel_id = i.channel_id) AND (m.video_type = i.video_type))), false) AS canonical_membership_supported
           FROM (public.video_inventory i
             LEFT JOIN public.video_inventory_memberships m ON ((m.video_id = i.video_id)))
          GROUP BY i.video_id, i.channel_id, i.video_type
        ), resolved AS (
         SELECT e.video_id,
            e.canonical_channel_id,
            e.canonical_video_type,
            e.present_membership_types,
            e.present_membership_channels,
            e.present_type_count,
            e.present_channel_count,
            e.canonical_membership_supported,
            COALESCE(((c.decision_type = e.canonical_video_type) AND (c.channel_id = e.canonical_channel_id) AND (c.blocked_reason IS NULL) AND (NOT (EXISTS ( SELECT 1
                   FROM unnest(e.present_membership_channels) x(id)
                  WHERE (x.id <> e.canonical_channel_id))))), false) AS manual_valid,
            coalesce(automatic.automatic_type=e.canonical_video_type,false) AS automatic_valid
           FROM (evidence e
             LEFT JOIN public.video_type_review_cases c ON ((c.video_id = e.video_id)))
             LEFT JOIN public.video_type_automatic_candidates automatic ON automatic.video_id=e.video_id
              AND automatic.channel_id=e.canonical_channel_id
        )
 SELECT video_id,
    canonical_channel_id,
    canonical_video_type,
    present_membership_types,
    present_membership_channels,
    present_type_count,
    present_channel_count,
    canonical_membership_supported,
        CASE
            WHEN manual_valid OR automatic_valid THEN 'resolved'::text
            WHEN (present_type_count = 0) THEN 'unverified'::text
            WHEN ((present_type_count > 1) OR (present_channel_count > 1)) THEN 'conflict'::text
            WHEN (NOT canonical_membership_supported) THEN 'canonical_unsupported'::text
            ELSE 'resolved'::text
        END AS resolution_status,
    manual_valid,
    automatic_valid
   FROM resolved;

CREATE OR REPLACE VIEW public.hot_video_inventory WITH (security_invoker='true') AS
 SELECT inv.video_id,
    inv.channel_id,
    inv.video_type,
    proof.source_published_at AS published_at,
    proof.last_present_at AS source_checked_at
   FROM ((public.video_inventory inv
     JOIN public.video_inventory_type_resolution resolution ON (((resolution.video_id = inv.video_id) AND (resolution.resolution_status = 'resolved'::text) AND (resolution.canonical_video_type = inv.video_type) AND ((resolution.manual_valid IS TRUE) OR (resolution.automatic_valid IS TRUE) OR ((resolution.present_membership_types = ARRAY[inv.video_type]) AND (resolution.present_membership_channels = ARRAY[inv.channel_id]))))))
     LEFT JOIN LATERAL ( SELECT membership.source_published_at,
            membership.last_present_at
           FROM public.video_inventory_memberships membership
          WHERE ((membership.video_id = inv.video_id) AND (membership.channel_id = inv.channel_id) AND (membership.membership_status = 'present'::text) AND ((resolution.manual_valid IS TRUE) OR (membership.video_type = inv.video_type)) AND isfinite(membership.last_present_at) AND ((membership.last_present_at >= (statement_timestamp() - '696:00:00'::interval)) AND (membership.last_present_at <= statement_timestamp())))
          ORDER BY membership.last_present_at DESC, membership.video_type, membership.playlist_id
         LIMIT 1) membership_proof ON true)
     LEFT JOIN LATERAL (SELECT v.published_at AS source_published_at,v.metadata_checked_at AS last_present_at
       FROM public.videos v WHERE resolution.manual_valid IS TRUE AND v.video_id=inv.video_id
       AND v.channel_id=inv.channel_id AND isfinite(v.metadata_checked_at)
       AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
       LIMIT 1) manual_proof ON true
     CROSS JOIN LATERAL (SELECT coalesce(membership_proof.source_published_at,manual_proof.source_published_at) AS source_published_at,
       coalesce(membership_proof.last_present_at,manual_proof.last_present_at) AS last_present_at) proof
   WHERE inv.skeleton_since IS NULL AND proof.last_present_at IS NOT NULL;

CREATE OR REPLACE VIEW readable.video_inventory_type_resolution WITH (security_barrier=true, security_invoker=false) AS
 WITH evidence AS (
         SELECT i.video_id,
            i.channel_id AS canonical_channel_id,
            i.video_type AS canonical_video_type,i.skeleton_since,min(m.source_expires_at) AS source_expires_at,
            COALESCE(array_agg(DISTINCT m.video_type ORDER BY m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_types,
            COALESCE(array_agg(DISTINCT m.channel_id ORDER BY m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)), ARRAY[]::text[]) AS present_membership_channels,
            count(DISTINCT m.video_type) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_type_count,
            count(DISTINCT m.channel_id) FILTER (WHERE (m.membership_status = 'present'::text)) AS present_channel_count,
            COALESCE(bool_or(((m.membership_status = 'present'::text) AND (m.channel_id = i.channel_id) AND (m.video_type = i.video_type))), false) AS canonical_membership_supported
           FROM (public.video_inventory i
             LEFT JOIN readable.video_inventory_memberships m ON ((m.video_id = i.video_id)))
          GROUP BY i.video_id, i.channel_id, i.video_type,i.skeleton_since
        ), resolved AS (
         SELECT e.video_id,e.skeleton_since,e.source_expires_at,
            e.canonical_channel_id,
            e.canonical_video_type,
            e.present_membership_types,
            e.present_membership_channels,
            e.present_type_count,
            e.present_channel_count,
            e.canonical_membership_supported,
            COALESCE(((c.decision_type = e.canonical_video_type) AND (c.channel_id = e.canonical_channel_id) AND (c.blocked_reason IS NULL) AND (NOT (EXISTS ( SELECT 1
                   FROM unnest(e.present_membership_channels) x(id)
                  WHERE (x.id <> e.canonical_channel_id))))), false) AS manual_valid,
            coalesce(automatic.automatic_type=e.canonical_video_type,false) AS automatic_valid,
            automatic.source_expires_at AS automatic_source_expires_at
           FROM (evidence e
             LEFT JOIN public.video_type_review_cases c ON ((c.video_id = e.video_id)))
             LEFT JOIN public.video_type_automatic_candidates automatic ON automatic.video_id=e.video_id
              AND automatic.channel_id=e.canonical_channel_id
        )
 SELECT video_id,
    canonical_channel_id,
    CASE WHEN skeleton_since IS NULL AND (canonical_membership_supported OR (manual_valid AND metadata.source_checked_at IS NOT NULL)) THEN canonical_video_type END AS canonical_video_type,
    present_membership_types,
    present_membership_channels,
    present_type_count,
    present_channel_count,
    canonical_membership_supported,
        CASE
            WHEN manual_valid OR automatic_valid THEN 'resolved'::text
            WHEN (present_type_count = 0) THEN 'unverified'::text
            WHEN ((present_type_count > 1) OR (present_channel_count > 1)) THEN 'conflict'::text
            WHEN (NOT canonical_membership_supported) THEN 'canonical_unsupported'::text
            ELSE 'resolved'::text
        END AS resolution_status,
    manual_valid,least(resolved.source_expires_at,CASE WHEN manual_valid THEN metadata.source_checked_at+interval '696 hours' END,
      CASE WHEN automatic_valid THEN automatic_source_expires_at END) AS source_expires_at,
    automatic_valid
   FROM resolved LEFT JOIN LATERAL (SELECT v.metadata_checked_at AS source_checked_at FROM public.videos v
   WHERE manual_valid AND v.video_id=resolved.video_id AND v.channel_id=resolved.canonical_channel_id AND (isfinite(v.metadata_checked_at) AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) LIMIT 1) metadata ON true;

CREATE OR REPLACE VIEW readable.hot_video_inventory WITH (security_barrier=true, security_invoker=false) AS
 SELECT inv.video_id,
    inv.channel_id,
    inv.video_type,
    proof.source_published_at AS published_at,
    proof.last_present_at AS source_checked_at,
    least(resolution.source_expires_at,membership_proof.source_expires_at,proof.last_present_at+interval '696 hours',CASE WHEN membership_proof.source_published_at IS NULL THEN manual_proof.last_present_at+interval '696 hours' END) AS source_expires_at
   FROM ((public.video_inventory inv
     JOIN readable.video_inventory_type_resolution resolution ON (((resolution.video_id = inv.video_id) AND (resolution.canonical_channel_id = inv.channel_id) AND (resolution.resolution_status = 'resolved'::text) AND (resolution.canonical_video_type = inv.video_type) AND ((resolution.manual_valid IS TRUE) OR (resolution.automatic_valid IS TRUE) OR ((resolution.present_membership_types = ARRAY[inv.video_type]) AND (resolution.present_membership_channels = ARRAY[inv.channel_id]))))))
     LEFT JOIN LATERAL ( SELECT membership.source_published_at,
            membership.last_present_at,membership.source_expires_at
           FROM readable.video_inventory_memberships membership
          WHERE ((membership.video_id = inv.video_id) AND (membership.channel_id = inv.channel_id) AND (membership.membership_status = 'present'::text) AND ((resolution.manual_valid IS TRUE) OR (membership.video_type = inv.video_type)) AND isfinite(membership.last_present_at) AND ((membership.last_present_at >= (statement_timestamp() - '696:00:00'::interval)) AND (membership.last_present_at <= statement_timestamp())))
          ORDER BY membership.last_present_at DESC, membership.video_type, membership.playlist_id
         LIMIT 1) membership_proof ON true)
     LEFT JOIN LATERAL (SELECT v.published_at AS source_published_at,v.metadata_checked_at AS last_present_at
       FROM readable.videos v WHERE resolution.manual_valid IS TRUE AND v.video_id=inv.video_id
       AND v.channel_id=inv.channel_id AND isfinite(v.metadata_checked_at)
       AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
       LIMIT 1) manual_proof ON true
     CROSS JOIN LATERAL (SELECT coalesce(membership_proof.source_published_at,manual_proof.source_published_at) AS source_published_at,
       coalesce(membership_proof.last_present_at,manual_proof.last_present_at) AS last_present_at) proof
   WHERE inv.skeleton_since IS NULL AND proof.last_present_at IS NOT NULL;

CREATE OR REPLACE VIEW readable.video_type_review_cases WITH (security_barrier=true, security_invoker=false) AS
SELECT c.video_id AS video_id,
    c.channel_id AS channel_id,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_canonical_type END AS observed_canonical_type,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_types ELSE ARRAY[]::text[] END AS observed_types,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_channels ELSE ARRAY[]::text[] END AS observed_channels,
    c.state AS state,
    c.revision AS revision,
    c.first_seen_at AS first_seen_at,
    c.last_seen_at AS last_seen_at,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.blocked_reason END AS blocked_reason,
    c.decision_type AS decision_type,
    c.decision_reason AS decision_reason,
    c.decision_at AS decision_at,
    c.decision_revision AS decision_revision,
    c.case_id AS case_id,
    c.source_detached_at AS source_detached_at,
    c.user_channel_ref AS user_channel_ref,
    c.restore_owner_ref AS restore_owner_ref,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.evidence_expires_at END AS source_expires_at,
    coalesce(c.video_id IS NOT NULL
      AND EXISTS(SELECT 1 FROM public.video_inventory i WHERE i.video_id=c.video_id AND i.channel_id=c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.video_collection_state q WHERE q.video_id=c.video_id AND q.channel_id<>c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=c.video_id
        AND m.membership_status='present' AND m.channel_id<>c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.videos v WHERE v.video_id=c.video_id AND v.channel_id<>c.channel_id),false)
      AS decision_unblocked
FROM public.video_type_review_cases c
WHERE c.state <> 'open' OR c.decision_type IS NOT NULL OR NOT EXISTS (
    SELECT 1 FROM public.video_type_automatic_candidates automatic
    JOIN public.video_inventory i ON i.video_id=automatic.video_id
      AND i.channel_id=automatic.channel_id AND i.video_type=automatic.automatic_type
    WHERE automatic.video_id=c.video_id AND automatic.channel_id=c.channel_id
);

-- END automatic Long/Short policy.

-- The protected readable views use this fixed proof without granting readers
-- access to its raw inputs. A nested security-invoker view would check those
-- inputs as the original HTTP reader, even below a non-invoker outer view.
-- The postgres owner, security barrier, query and existing ACL are unchanged.
ALTER VIEW public.video_type_automatic_candidates SET (security_invoker=false);

-- Publication state is independent of typed playlist identity. The API date
-- may be in the future; metadata_checked_at is its bounded acquisition clock.
ALTER TABLE public.videos ADD COLUMN publication_status text;
ALTER TABLE public.videos ADD COLUMN scheduled_start_at timestamptz;
ALTER TABLE public.videos ADD CONSTRAINT videos_publication_status_check CHECK (
 publication_status IS NULL OR publication_status IN ('upcoming','live','awaiting_completion'));
ALTER TABLE public.videos ADD CONSTRAINT videos_publication_metadata_check CHECK (
 (publication_status IS NULL AND scheduled_start_at IS NULL) OR
 (publication_status IS NOT NULL AND metadata_checked_at IS NOT NULL AND
 (scheduled_start_at IS NULL OR isfinite(scheduled_start_at))));
DROP INDEX public.hot_cleanup_video_metadata_idx;
CREATE INDEX hot_cleanup_video_metadata_idx ON public.videos USING btree
 (COALESCE(metadata_checked_at, '-infinity'::timestamp with time zone), video_id)
 WHERE title IS NOT NULL OR published_at IS NOT NULL OR duration IS NOT NULL OR tags IS NOT NULL
 OR category_id IS NOT NULL OR youtube_topic_categories IS NOT NULL OR default_audio_language IS NOT NULL
 OR default_language IS NOT NULL OR caption_available IS NOT NULL OR stream_actual_end_at IS NOT NULL
 OR publication_status IS NOT NULL OR scheduled_start_at IS NOT NULL;

ALTER TABLE public.typed_video_collection_receipts DROP CONSTRAINT typed_video_collection_receipts_type_check;
ALTER TABLE public.typed_video_collection_receipts ADD CONSTRAINT typed_video_collection_receipts_type_check CHECK ((
 (receipt_kind='identity_refresh' AND video_type IS NULL)
 OR (receipt_kind='stream_probe' AND (video_type IS NULL OR video_type IN ('long','short','stream')))
 OR (video_type IS NOT NULL AND (
  (receipt_kind IN ('metadata','missing') AND video_type IN ('short','stream'))
  OR (receipt_kind='missing' AND video_type='long')))) IS TRUE);

CREATE OR REPLACE VIEW readable.videos WITH (security_barrier=true, security_invoker=false) AS
SELECT
    s.video_id AS video_id,
    s.channel_id AS channel_id,
    CASE WHEN admitted.metadata THEN s.title END AS title,
    CASE WHEN admitted.counters THEN s.views END AS views,
    s.checked_at AS checked_at,
    CASE WHEN admitted.metadata THEN s.published_at END AS published_at,
    CASE WHEN admitted.metadata THEN s.duration END AS duration,
    CASE WHEN admitted.counters THEN s.comment_count END AS comment_count,
    CASE WHEN admitted.metadata THEN s.tags END AS tags,
    CASE WHEN admitted.availability THEN s.availability_status END AS availability_status,
    CASE WHEN admitted.availability THEN s.privacy_status END AS privacy_status,
    CASE WHEN admitted.availability THEN s.upload_status END AS upload_status,
    s.availability_checked_at AS availability_checked_at,
    CASE WHEN admitted.counters THEN s.like_count END AS like_count,
    CASE WHEN admitted.metadata THEN s.category_id END AS category_id,
    s.metadata_checked_at AS metadata_checked_at,
    CASE WHEN admitted.metadata THEN s.youtube_topic_categories END AS youtube_topic_categories,
    CASE WHEN admitted.metadata THEN s.default_audio_language END AS default_audio_language,
    CASE WHEN admitted.metadata THEN s.default_language END AS default_language,
    CASE WHEN admitted.metadata THEN s.caption_available END AS caption_available,
    CASE WHEN admitted.metadata THEN s.stream_actual_end_at END AS stream_actual_end_at,
    least(CASE WHEN admitted.counters THEN s.checked_at END,CASE WHEN admitted.metadata THEN s.metadata_checked_at END,CASE WHEN admitted.availability THEN s.availability_checked_at END)+interval '696 hours' AS source_expires_at,
    CASE WHEN admitted.metadata THEN s.publication_status END AS publication_status,
    CASE WHEN admitted.metadata THEN s.scheduled_start_at END AS scheduled_start_at
FROM public.videos s
CROSS JOIN LATERAL (SELECT (s.publication_status IS NULL AND isfinite(s.checked_at) AND s.checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS counters,
(isfinite(s.metadata_checked_at) AND s.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS metadata,
(isfinite(s.availability_checked_at) AND s.availability_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS availability) admitted;

CREATE OR REPLACE VIEW public.hot_video_inventory WITH (security_invoker='true') AS
 SELECT inv.video_id,
    inv.channel_id,
    inv.video_type,
    proof.source_published_at AS published_at,
    proof.last_present_at AS source_checked_at
   FROM ((public.video_inventory inv
     JOIN public.video_inventory_type_resolution resolution ON (((resolution.video_id = inv.video_id) AND (resolution.resolution_status = 'resolved'::text) AND (resolution.canonical_video_type = inv.video_type) AND ((resolution.manual_valid IS TRUE) OR (resolution.automatic_valid IS TRUE) OR ((resolution.present_membership_types = ARRAY[inv.video_type]) AND (resolution.present_membership_channels = ARRAY[inv.channel_id]))))))
     LEFT JOIN LATERAL ( SELECT membership.source_published_at,
            membership.last_present_at
           FROM public.video_inventory_memberships membership
          WHERE ((membership.video_id = inv.video_id) AND (membership.channel_id = inv.channel_id) AND (membership.membership_status = 'present'::text) AND ((resolution.manual_valid IS TRUE) OR (membership.video_type = inv.video_type)) AND isfinite(membership.last_present_at) AND ((membership.last_present_at >= (statement_timestamp() - '696:00:00'::interval)) AND (membership.last_present_at <= statement_timestamp())))
          ORDER BY membership.last_present_at DESC, membership.video_type, membership.playlist_id
         LIMIT 1) membership_proof ON true)
     LEFT JOIN LATERAL (SELECT v.published_at AS source_published_at,v.metadata_checked_at AS last_present_at
       FROM public.videos v WHERE resolution.manual_valid IS TRUE AND v.video_id=inv.video_id
       AND v.channel_id=inv.channel_id AND isfinite(v.metadata_checked_at)
       AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
       LIMIT 1) manual_proof ON true
     CROSS JOIN LATERAL (SELECT coalesce(membership_proof.source_published_at,manual_proof.source_published_at) AS source_published_at,
       coalesce(membership_proof.last_present_at,manual_proof.last_present_at) AS last_present_at) proof
   WHERE inv.skeleton_since IS NULL AND proof.last_present_at IS NOT NULL
 AND NOT EXISTS(SELECT 1 FROM public.videos publication WHERE publication.video_id=inv.video_id
 AND publication.channel_id=inv.channel_id AND publication.publication_status IS NOT NULL);

CREATE OR REPLACE VIEW readable.hot_video_inventory WITH (security_barrier=true, security_invoker=false) AS
 SELECT inv.video_id,
    inv.channel_id,
    inv.video_type,
    proof.source_published_at AS published_at,
    proof.last_present_at AS source_checked_at,
    least(resolution.source_expires_at,membership_proof.source_expires_at,proof.last_present_at+interval '696 hours',CASE WHEN membership_proof.source_published_at IS NULL THEN manual_proof.last_present_at+interval '696 hours' END) AS source_expires_at
   FROM ((public.video_inventory inv
     JOIN readable.video_inventory_type_resolution resolution ON (((resolution.video_id = inv.video_id) AND (resolution.canonical_channel_id = inv.channel_id) AND (resolution.resolution_status = 'resolved'::text) AND (resolution.canonical_video_type = inv.video_type) AND ((resolution.manual_valid IS TRUE) OR (resolution.automatic_valid IS TRUE) OR ((resolution.present_membership_types = ARRAY[inv.video_type]) AND (resolution.present_membership_channels = ARRAY[inv.channel_id]))))))
     LEFT JOIN LATERAL ( SELECT membership.source_published_at,
            membership.last_present_at,membership.source_expires_at
           FROM readable.video_inventory_memberships membership
          WHERE ((membership.video_id = inv.video_id) AND (membership.channel_id = inv.channel_id) AND (membership.membership_status = 'present'::text) AND ((resolution.manual_valid IS TRUE) OR (membership.video_type = inv.video_type)) AND isfinite(membership.last_present_at) AND ((membership.last_present_at >= (statement_timestamp() - '696:00:00'::interval)) AND (membership.last_present_at <= statement_timestamp())))
          ORDER BY membership.last_present_at DESC, membership.video_type, membership.playlist_id
         LIMIT 1) membership_proof ON true)
     LEFT JOIN LATERAL (SELECT v.published_at AS source_published_at,v.metadata_checked_at AS last_present_at
       FROM readable.videos v WHERE resolution.manual_valid IS TRUE AND v.video_id=inv.video_id
       AND v.channel_id=inv.channel_id AND isfinite(v.metadata_checked_at)
       AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
       LIMIT 1) manual_proof ON true
     CROSS JOIN LATERAL (SELECT coalesce(membership_proof.source_published_at,manual_proof.source_published_at) AS source_published_at,
       coalesce(membership_proof.last_present_at,manual_proof.last_present_at) AS last_present_at) proof
   WHERE inv.skeleton_since IS NULL AND proof.last_present_at IS NOT NULL
 AND NOT EXISTS(SELECT 1 FROM public.videos publication WHERE publication.video_id=inv.video_id
 AND publication.channel_id=inv.channel_id AND publication.publication_status IS NOT NULL);

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
            coalesce(state.typed_collection_status = 'awaiting_completion'
                and state.hydration_needed
                and state.typed_stream_actual_end_at is null
                and video.stream_actual_end_at is null
                and video.publication_status is not null
                and video.metadata_checked_at = state.metadata_source_checked_at
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

-- Header completeness keeps all canonical identities except explicit waiting
-- publications. Missing type/profile proof still makes ordinary totals incomplete.
CREATE VIEW readable.published_inventory_counts WITH (security_barrier=true, security_invoker=false) AS
SELECT inventory.channel_id,inventory.video_type,count(*)::bigint AS canonical_count
FROM public.video_inventory inventory
WHERE NOT EXISTS(SELECT 1 FROM public.videos publication WHERE publication.video_id=inventory.video_id
 AND publication.channel_id=inventory.channel_id AND publication.publication_status IS NOT NULL)
GROUP BY inventory.channel_id,inventory.video_type;
ALTER VIEW readable.published_inventory_counts OWNER TO postgres;
GRANT SELECT ON readable.published_inventory_counts TO service_role;
GRANT SELECT ON readable.published_inventory_counts TO lic_reader;

-- Schema step 0034-all-type-queue-health
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

COMMIT;
