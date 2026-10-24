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
