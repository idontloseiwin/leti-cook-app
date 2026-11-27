-- Existing observations keep an unknown enqueue time. Neither their source
-- time nor the time of this migration is evidence of when they were queued.
ALTER TABLE public.alert_observations ADD COLUMN enqueued_at timestamptz;
ALTER TABLE public.alert_observations ALTER COLUMN enqueued_at SET DEFAULT statement_timestamp();
CREATE INDEX alert_observations_enqueued ON public.alert_observations(enqueued_at);

CREATE OR REPLACE FUNCTION public.capture_alert_observations() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
BEGIN
    IF NOT EXISTS (SELECT FROM public.alert_rules r WHERE r.enabled
        AND r.definition->>'event' = ANY(string_to_array(TG_ARGV[0], ','))) THEN
        RETURN NULL;
    END IF;
    -- Each trigger supplies a closed input projection. Only names and SQL
    -- literals installed with this migration enter the statement.
    BEGIN
    EXECUTE format($query$
        WITH facts AS MATERIALIZED (%s)
        INSERT INTO public.alert_observations
            (rule_id,version,event,subject,channel_id,channel_ref,source_key,observed_at,
             available_from,rule_started_at,evidence_expires_at,definition,video_type,published_at,duration)
        SELECT r.id,r.version,f.event,f.subject,c.channel_id,c.user_work_id,f.source_key,f.at,
               greatest(coalesce(c.added_at, r.started_at - interval '696 hours'),CASE WHEN r.definition->'scope'->>'kind'='folder' THEN c.alert_folder_since
                        WHEN r.definition->'scope'->>'kind'='label' THEN assignment.created_at END,
                        CASE WHEN r.definition ? 'min_duration' OR r.definition ? 'max_duration' THEN f.duration_since END),
               r.started_at,least(f.at + interval '696 hours',f.evidence_expires_at,
                 CASE WHEN f.event='video.published' OR r.definition ? 'min_duration' OR r.definition ? 'max_duration'
                      THEN f.metadata_expires_at END),r.definition,f.video_type,
               CASE WHEN f.event='video.published' THEN f.published_at END,
               CASE WHEN r.definition ? 'min_duration' OR r.definition ? 'max_duration' THEN f.duration END
        FROM facts f JOIN public.channels c ON c.channel_id=f.channel_id AND NOT c.is_hidden
        JOIN readable.channel_user_work visible ON visible.channel_id=c.channel_id AND NOT visible.is_hidden
        JOIN public.alert_rules r ON r.enabled AND r.definition->>'event'=f.event
         AND r.started_at < f.at AND r.definition->>'video_type' IS NOT DISTINCT FROM f.video_type
         AND (r.scope_owner IS NULL OR r.scope_owner=c.user_work_id)
        LEFT JOIN public.folders folder ON r.definition->'scope'->>'kind'='folder'
         AND folder.id=c.folder_id AND folder.created_at=r.scope_created_at
        LEFT JOIN public.channel_labels label ON r.definition->'scope'->>'kind'='label'
         AND label.label_key=r.definition->'scope'->>'id' AND label.created_at=r.scope_created_at
        LEFT JOIN public.channel_label_assignments assignment ON assignment.channel_id=c.channel_id
         AND r.definition->'scope'->>'kind'='label' AND assignment.label_key=r.definition->'scope'->>'id'
        WHERE isfinite(f.at) AND f.at BETWEEN statement_timestamp()-interval '696 hours' AND clock_timestamp()
          -- Ineligible samples stay in source history for the next comparison.
          AND CASE
            WHEN f.event='video.published' THEN f.published_at>r.started_at
            WHEN f.measured_value IS NULL THEN true
            WHEN NOT coalesce(pg_input_is_valid(r.definition->>'threshold','bigint'),false) THEN true
            WHEN r.definition->>'direction'='up' THEN f.measured_value >= (r.definition->>'threshold')::bigint
            WHEN r.definition->>'direction'='down' THEN f.measured_value <= (r.definition->>'threshold')::bigint
            ELSE true END
          AND CASE r.definition->'scope'->>'kind'
            WHEN 'channel' THEN c.channel_id=r.definition->'scope'->>'id'
            WHEN 'folder' THEN folder.id::text=r.definition->'scope'->>'id'
            WHEN 'label' THEN label.label_key IS NOT NULL AND assignment.channel_id IS NOT NULL
            WHEN 'video' THEN f.subject=r.definition->'scope'->>'id'
            ELSE false END
        ON CONFLICT (rule_id,version,source_key) DO NOTHING
    $query$, TG_ARGV[1]);
    EXCEPTION WHEN data_exception OR integrity_constraint_violation THEN
        -- The source fact belongs to collection. Expose an observation gap if
        -- the optional delivery record cannot represent this statement.
        UPDATE public.alert_rules SET gap_count=gap_count+1,last_gap_at=statement_timestamp()
         WHERE enabled AND definition->>'event'=ANY(string_to_array(TG_ARGV[0], ','));
    END;
    RETURN NULL;
END;
$$;

DROP TRIGGER alert_video_observations ON public.video_snapshots;
CREATE TRIGGER alert_video_observations AFTER INSERT ON public.video_snapshots
REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION public.capture_alert_observations(
'video.views,video.likes,video.comments,video.published',
'SELECT metric.event,s.video_id AS subject,v.channel_id,s.id::text AS source_key,metric.at,
        h.video_type,v.published_at,v.duration,metric.measured_value,
        vclock.alert_duration_since AS duration_since,
        least(h.source_expires_at, v.availability_checked_at + interval ''696 hours'') AS evidence_expires_at,
        v.metadata_checked_at + interval ''696 hours'' AS metadata_expires_at
 FROM inserted s JOIN public.videos vclock ON vclock.video_id=s.video_id
 JOIN LATERAL (SELECT v0.* FROM readable.videos v0 WHERE v0.video_id=s.video_id LIMIT 1) v ON true
 JOIN LATERAL (SELECT h0.video_type,h0.source_expires_at FROM readable.hot_video_inventory h0
    WHERE h0.video_id=s.video_id AND h0.channel_id=v.channel_id LIMIT 1) h ON true
 CROSS JOIN LATERAL (VALUES
   (''video.views'',s.recorded_at,s.views IS NOT NULL,s.views),
   (''video.likes'',coalesce(s.engagement_observed_at,s.recorded_at),s.like_count IS NOT NULL,s.like_count),
   (''video.comments'',coalesce(s.engagement_observed_at,s.recorded_at),s.comment_count IS NOT NULL,s.comment_count),
   (''video.published'',s.recorded_at,v.published_at IS NOT NULL,NULL::bigint)
 ) metric(event,at,present,measured_value)
 WHERE metric.present AND v.availability_status=''public'' AND v.publication_status IS NULL
   AND (v.published_at IS NULL OR v.published_at<=metric.at)
   AND (metric.event<>''video.published'' OR NOT EXISTS (
       SELECT FROM public.video_inventory_memberships m
       JOIN public.channel_video_inventory_scans initial_scan ON initial_scan.scan_id=m.first_seen_scan_id
       WHERE m.video_id=s.video_id AND m.channel_id=v.channel_id AND m.video_type=h.video_type
       AND initial_scan.scan_kind=''initial_inventory''))
   AND h.video_type IN (''long'',''short'')');

DROP TRIGGER alert_channel_observations ON public.channel_snapshots;
CREATE TRIGGER alert_channel_observations AFTER INSERT ON public.channel_snapshots
REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION public.capture_alert_observations(
'channel.subscribers',
'SELECT ''channel.subscribers'' AS event,s.channel_id AS subject,s.channel_id,s.subscribers AS measured_value,
        s.channel_id || '':'' || extract(epoch FROM s.recorded_at)::text AS source_key,s.recorded_at AS at,NULL::text AS video_type,
        NULL::timestamptz AS published_at,NULL::text AS duration,NULL::timestamptz AS evidence_expires_at,
        NULL::timestamptz AS duration_since,NULL::timestamptz AS metadata_expires_at
 FROM inserted s WHERE s.source=''youtube_channel_metadata_refresh'' AND s.subscribers IS NOT NULL');

DROP TRIGGER alert_availability_observations ON public.video_availability_events;
CREATE TRIGGER alert_availability_observations AFTER INSERT ON public.video_availability_events
REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION public.capture_alert_observations(
'video.unavailable,video.restored',
'SELECT CASE s.event_type WHEN ''restored'' THEN ''video.restored'' ELSE ''video.unavailable'' END AS event,
        s.video_id AS subject,s.channel_id,NULL::bigint AS measured_value,s.id::text AS source_key,greatest(s.observed_at,s.created_at) AS at,i.video_type,
        NULL::timestamptz AS published_at,NULL::text AS duration,
        least(s.observed_at,proof.last_present_at) + interval ''696 hours'' AS evidence_expires_at,
        NULL::timestamptz AS duration_since,NULL::timestamptz AS metadata_expires_at
 FROM inserted s JOIN public.video_inventory i ON i.video_id=s.video_id AND i.channel_id=s.channel_id
 JOIN LATERAL (SELECT m.last_present_at FROM public.video_inventory_memberships m
   WHERE m.video_id=i.video_id AND m.channel_id=i.channel_id AND m.video_type=i.video_type
   AND m.last_present_at<=statement_timestamp() ORDER BY m.last_present_at DESC NULLS LAST LIMIT 1) proof
   ON proof.last_present_at IS NOT NULL
 WHERE i.video_type IN (''long'',''short'') AND s.event_type IN (''unavailable_detected'',''restored'')
   AND (s.event_type<>''restored'' OR s.to_status=''public'')');
