-- BEGIN automatic Long/Short policy: derived proof, no persisted decision.

-- Canonical query: application/db/inventory/automatic_type_policy.py.

CREATE VIEW public.video_type_automatic_candidates WITH (security_invoker=true, security_barrier=true) AS
SELECT i.video_id,
    i.channel_id,
    CASE WHEN duration_value.seconds <= 180 THEN 'short'::text ELSE 'long'::text END AS automatic_type,
    least(v.metadata_checked_at, evidence.oldest_clock)+interval '696 hours' AS source_expires_at
FROM public.video_inventory i
JOIN public.videos v ON v.video_id=i.video_id AND v.channel_id=i.channel_id
JOIN LATERAL (
    SELECT min(least(m.last_seen_at, m.last_present_at)) AS oldest_clock
    FROM public.video_inventory_memberships m
    WHERE m.video_id=i.video_id AND m.membership_status='present'
    HAVING array_agg(DISTINCT m.video_type ORDER BY m.video_type)=ARRAY['long','short']::text[]
       AND bool_and((m.channel_id=i.channel_id
         AND isfinite(m.last_seen_at)
         AND m.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
         AND isfinite(m.last_present_at)
         AND m.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) IS TRUE)
) evidence ON true
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
  AND (i.video_type IS NULL OR i.video_type IN ('long','short'))
  AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
  AND duration_value.seconds > 0
  AND NOT EXISTS (
      SELECT 1 FROM public.video_type_review_cases decision
      WHERE decision.video_id=i.video_id AND decision.decision_type IS NOT NULL
  );
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

-- END automatic Long/Short policy
