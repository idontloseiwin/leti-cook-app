-- Release-readiness: bounded reconnect queue and canonical catalogue join.
-- Applied inside the administrative upgrade transaction.

CREATE OR REPLACE VIEW readable.hot_video_inventory WITH (security_barrier=true, security_invoker=false) AS
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
                and state.metadata_source_checked_at = state.typed_collection_checked_at
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

ALTER TABLE public.user_channel_restore_items
 ADD COLUMN reconnect_state text NOT NULL DEFAULT 'idle',
 ADD COLUMN reconnect_revision bigint,
 ADD COLUMN reconnect_requested_at timestamptz,
 ADD COLUMN reconnect_not_before timestamptz,
 ADD COLUMN reconnect_last_attempt_at timestamptz,
 ADD COLUMN reconnect_attempt_count integer NOT NULL DEFAULT 0,
 ADD COLUMN reconnect_error_code text,
 ADD COLUMN reconnect_lease_token uuid,
 ADD COLUMN reconnect_lease_expires_at timestamptz,
 ADD CONSTRAINT user_channel_restore_items_reconnect_state_check CHECK
  (reconnect_state IN ('idle','queued','running','waiting','blocked')),
 ADD CONSTRAINT user_channel_restore_items_reconnect_intent_check CHECK
  (((reconnect_state='idle' AND reconnect_revision IS NULL AND reconnect_requested_at IS NULL
     AND reconnect_not_before IS NULL AND reconnect_last_attempt_at IS NULL
     AND reconnect_attempt_count=0 AND reconnect_error_code IS NULL)
    OR (reconnect_state<>'idle' AND reconnect_revision=revision AND NOT is_hidden
     AND reconnect_requested_at IS NOT NULL AND isfinite(reconnect_requested_at))) IS TRUE),
 ADD CONSTRAINT user_channel_restore_items_reconnect_lease_check CHECK
  (((reconnect_state='running' AND reconnect_lease_token IS NOT NULL
     AND reconnect_lease_expires_at IS NOT NULL AND isfinite(reconnect_lease_expires_at))
    OR (reconnect_state<>'running' AND reconnect_lease_token IS NULL AND reconnect_lease_expires_at IS NULL)) IS TRUE),
 ADD CONSTRAINT user_channel_restore_items_reconnect_wait_check CHECK
  (((reconnect_state='waiting' AND reconnect_not_before IS NOT NULL AND isfinite(reconnect_not_before))
    OR (reconnect_state<>'waiting' AND reconnect_not_before IS NULL)) IS TRUE),
 ADD CONSTRAINT user_channel_restore_items_reconnect_attempt_check CHECK
  (reconnect_attempt_count>=0 AND (reconnect_last_attempt_at IS NULL OR isfinite(reconnect_last_attempt_at))),
 ADD CONSTRAINT user_channel_restore_items_reconnect_error_check CHECK
  (reconnect_error_code IS NULL OR reconnect_error_code ~ '^[a-z0-9_]{1,120}$');

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

CREATE OR REPLACE FUNCTION public.guard_hot_operator_scope() RETURNS trigger
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
