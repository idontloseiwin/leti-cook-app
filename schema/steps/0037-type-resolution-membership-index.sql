-- Type resolution reads memberships from the base table, through exactly the
-- admission filter and source deadline of readable.video_inventory_memberships
-- (a test compares the two), instead of joining that security_barrier view: a
-- join into it cannot use the memberships index, so every channel-scoped read
-- built it for the whole table. The raw inventory type, the aggregate's group
-- key, is exposed so hot inventory can pass its type into the aggregate.
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
             LEFT JOIN (SELECT s.video_id,s.channel_id,s.video_type,s.membership_status,
               least(s.last_seen_at,CASE WHEN s.membership_status='present' THEN s.last_present_at ELSE s.absent_since END,CASE WHEN (isfinite(s.first_seen_at) AND s.first_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.first_seen_at END,CASE WHEN (isfinite(s.last_present_at) AND s.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) THEN s.last_present_at END)+interval '696 hours' AS source_expires_at
               FROM public.video_inventory_memberships s
               WHERE (isfinite(s.last_seen_at) AND s.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AND CASE WHEN s.membership_status='present'
              THEN (isfinite(s.last_present_at) AND s.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) ELSE (isfinite(s.absent_since) AND s.absent_since BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) END) m ON ((m.video_id = i.video_id)))
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
    automatic_valid,
    canonical_video_type AS inventory_video_type
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
     JOIN readable.video_inventory_type_resolution resolution ON (((resolution.video_id = inv.video_id) AND (resolution.inventory_video_type = inv.video_type) AND (resolution.canonical_channel_id = inv.channel_id) AND (resolution.resolution_status = 'resolved'::text) AND (resolution.canonical_video_type = inv.video_type) AND ((resolution.manual_valid IS TRUE) OR (resolution.automatic_valid IS TRUE) OR ((resolution.present_membership_types = ARRAY[inv.video_type]) AND (resolution.present_membership_channels = ARRAY[inv.channel_id]))))))
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
