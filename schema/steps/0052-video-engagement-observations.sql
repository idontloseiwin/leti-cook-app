-- A NULL report has not been observed. Existing rows have unknown provenance
-- until the first statistics-bearing General response. Bits 1 and 2 represent
-- likeCount and commentCount. A value clock overrides the views clock only
-- when an earlier genuine observation is retained.
ALTER TABLE public.videos
    ADD COLUMN engagement_report_mask smallint DEFAULT -1,
    ADD COLUMN engagement_report_at timestamptz,
    ADD COLUMN like_count_source_at timestamptz,
    ADD COLUMN comment_count_source_at timestamptz,
    ADD CONSTRAINT videos_engagement_report_valid CHECK (
        (engagement_report_at IS NULL AND (engagement_report_mask IS NULL OR engagement_report_mask=-1))
        OR (engagement_report_at IS NOT NULL AND engagement_report_mask IS NOT NULL AND engagement_report_mask BETWEEN 0 AND 3));
ALTER TABLE public.videos ALTER COLUMN engagement_report_mask DROP DEFAULT;
CREATE INDEX videos_like_count_source_at_idx ON public.videos (like_count_source_at, video_id) WHERE like_count_source_at IS NOT NULL;
CREATE INDEX videos_comment_count_source_at_idx ON public.videos (comment_count_source_at, video_id) WHERE comment_count_source_at IS NOT NULL;
CREATE INDEX videos_engagement_report_at_idx ON public.videos (engagement_report_at, video_id) WHERE engagement_report_at IS NOT NULL;

CREATE OR REPLACE VIEW readable.videos WITH (security_barrier=true, security_invoker=false) AS
SELECT
    s.video_id AS video_id,
    s.channel_id AS channel_id,
    CASE WHEN admitted.metadata THEN s.title END AS title,
    CASE WHEN admitted.counters THEN s.views END AS views,
    s.checked_at AS checked_at,
    CASE WHEN admitted.metadata THEN s.published_at END AS published_at,
    CASE WHEN admitted.metadata THEN s.duration END AS duration,
    CASE WHEN admitted.counters AND admitted.comment_count THEN s.comment_count END AS comment_count,
    CASE WHEN admitted.metadata THEN s.tags END AS tags,
    CASE WHEN admitted.availability THEN s.availability_status END AS availability_status,
    CASE WHEN admitted.availability THEN s.privacy_status END AS privacy_status,
    CASE WHEN admitted.availability THEN s.upload_status END AS upload_status,
    s.availability_checked_at AS availability_checked_at,
    CASE WHEN admitted.counters AND admitted.like_count THEN s.like_count END AS like_count,
    CASE WHEN admitted.metadata THEN s.category_id END AS category_id,
    s.metadata_checked_at AS metadata_checked_at,
    CASE WHEN admitted.metadata THEN s.youtube_topic_categories END AS youtube_topic_categories,
    CASE WHEN admitted.metadata THEN s.default_audio_language END AS default_audio_language,
    CASE WHEN admitted.metadata THEN s.default_language END AS default_language,
    CASE WHEN admitted.metadata THEN s.caption_available END AS caption_available,
    CASE WHEN admitted.metadata THEN s.stream_actual_end_at END AS stream_actual_end_at,
    least(CASE WHEN admitted.counters AND admitted.comment_count AND s.comment_count IS NOT NULL THEN s.comment_count_source_at END,CASE WHEN admitted.counters AND admitted.like_count AND s.like_count IS NOT NULL THEN s.like_count_source_at END,CASE WHEN admitted.counters THEN s.checked_at END,CASE WHEN admitted.metadata THEN s.metadata_checked_at END,CASE WHEN admitted.availability THEN s.availability_checked_at END)+interval '696 hours' AS source_expires_at,
    CASE WHEN admitted.metadata THEN s.publication_status END AS publication_status,
    CASE WHEN admitted.metadata THEN s.scheduled_start_at END AS scheduled_start_at
FROM public.videos s
CROSS JOIN LATERAL (SELECT (s.publication_status IS NULL AND isfinite(s.checked_at) AND s.checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS counters,
(s.comment_count_source_at IS NULL OR s.comment_count_source_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS comment_count,
(s.like_count_source_at IS NULL OR s.like_count_source_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS like_count,
(isfinite(s.metadata_checked_at) AND s.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS metadata,
(isfinite(s.availability_checked_at) AND s.availability_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) AS availability) admitted;

-- A profile and its fallback view count can arrive at different times.
ALTER TABLE public.video_snapshots ADD COLUMN engagement_observed_at timestamptz;
CREATE INDEX video_snapshots_engagement_expiry_idx ON public.video_snapshots
    (coalesce(engagement_observed_at,'-infinity'::timestamptz),lpad(id::text,20,'0'))
    WHERE engagement_observed_at IS NOT NULL;

CREATE OR REPLACE VIEW readable.video_snapshots WITH (security_barrier=true, security_invoker=false) AS
 SELECT s.id,s.video_id,s.views,s.recorded_at,CASE WHEN engagement.current THEN s.like_count END AS like_count,
 CASE WHEN engagement.current THEN s.comment_count END AS comment_count,s.collection_attempt_id,
 least(s.recorded_at,CASE WHEN engagement.current THEN s.engagement_observed_at END)+interval '696 hours' AS source_expires_at,
 coalesce(s.views_definition_epoch,CASE WHEN s.recorded_at>='2026-08-24T00:00:00Z'::timestamptz THEN 'youtube.views.play_start.20260824' END) AS views_definition_epoch
 FROM public.video_snapshots s
 CROSS JOIN LATERAL (SELECT s.engagement_observed_at IS NULL OR s.engagement_observed_at
   BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp() AS current) engagement
 WHERE isfinite(s.recorded_at)
 AND s.recorded_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp();
