ALTER TABLE public.alert_observations
    ALTER COLUMN rule_id DROP NOT NULL, ALTER COLUMN version DROP NOT NULL,
    ALTER COLUMN definition DROP NOT NULL, ADD COLUMN members jsonb,
    ADD CONSTRAINT alert_observation_recipients CHECK (
        (members IS NULL AND rule_id IS NOT NULL AND version IS NOT NULL AND definition IS NOT NULL)
        OR (members IS NOT NULL AND jsonb_typeof(members)='object' AND members<>'{}'::jsonb
            AND rule_id IS NULL AND version IS NULL AND definition IS NULL));
CREATE INDEX alert_observations_members ON public.alert_observations USING gin(members) WHERE members IS NOT NULL;
CREATE INDEX alert_observations_shared_ready ON public.alert_observations(id) WHERE members IS NOT NULL AND retry_at IS NULL;
CREATE INDEX alert_observations_shared_due ON public.alert_observations(retry_at,id) WHERE members IS NOT NULL AND retry_at IS NOT NULL;

CREATE OR REPLACE FUNCTION public.capture_alert_observations() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE
    delivery_projection text;
BEGIN
    IF NOT EXISTS (SELECT FROM public.alert_rules r WHERE r.enabled
        AND r.definition->>'event' = ANY(string_to_array(TG_ARGV[0], ','))) THEN
        RETURN NULL;
    END IF;
    IF TG_RELID='public.video_snapshots'::regclass THEN
        IF NOT EXISTS (SELECT FROM public.alert_rules r WHERE r.enabled
            AND r.definition->>'event'=ANY(string_to_array(TG_ARGV[0],','))
            AND (r.definition->'scope'->>'kind'<>'video' OR EXISTS (
                SELECT FROM inserted s WHERE s.video_id=r.definition->'scope'->>'id'))) THEN
            RETURN NULL;
        END IF;
    END IF;
    -- Small recipient sets use the scalar queue. In high fan-out video
    -- batches, store each measured value once with its frozen recipients.
    delivery_projection := 'SELECT m.*,NULL::jsonb FROM matched m';
    IF TG_RELID='public.video_snapshots'::regclass AND EXISTS (
        SELECT FROM public.alert_rules r WHERE r.enabled
          AND r.definition->>'event' IN ('video.views','video.likes','video.comments') OFFSET 4 LIMIT 1) THEN
        delivery_projection := $pack$
            SELECT m.*,NULL::jsonb FROM matched m WHERE event='video.published'
            UNION ALL
            SELECT NULL::uuid,NULL::integer,event,subject,channel_id,channel_ref,source_key,observed_at,
                   min(available_from),min(rule_started_at),evidence_expires_at,NULL::jsonb,
                   video_type,published_at,duration,
                   jsonb_object_agg(rule_id::text,jsonb_build_object('version',version,'rule',definition,
                       'started',rule_started_at,'since',available_from))
            FROM matched WHERE event<>'video.published'
            GROUP BY event,subject,channel_id,channel_ref,source_key,observed_at,
                     evidence_expires_at,video_type,published_at,duration
        $pack$;
    END IF;
    BEGIN
    EXECUTE format($query$
        WITH rules AS MATERIALIZED (
            SELECT r.* FROM public.alert_rules r
            WHERE r.enabled AND r.definition->>'event' = ANY($1)
            ORDER BY r.id FOR KEY SHARE
        ), facts AS MATERIALIZED (%s), matched(rule_id,version,event,subject,channel_id,channel_ref,
            source_key,observed_at,available_from,rule_started_at,evidence_expires_at,definition,
            video_type,published_at,duration) AS (
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
        JOIN rules r ON r.definition->>'event'=f.event
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
        )
        INSERT INTO public.alert_observations(rule_id,version,event,subject,channel_id,channel_ref,
            source_key,observed_at,available_from,rule_started_at,evidence_expires_at,definition,
            video_type,published_at,duration,members)
        %s
        ON CONFLICT (rule_id,version,source_key) DO NOTHING
    $query$, TG_ARGV[1], delivery_projection) USING string_to_array(TG_ARGV[0], ',');
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
'WITH eligible AS MATERIALIZED (
    SELECT batch.* FROM inserted batch WHERE EXISTS (
        SELECT FROM public.video_inventory inv JOIN rules r ON r.definition->>''video_type''=inv.video_type
        WHERE inv.video_id=batch.video_id AND inv.skeleton_since IS NULL
          AND CASE r.definition->>''event''
              WHEN ''video.views'' THEN batch.views IS NOT NULL
              WHEN ''video.likes'' THEN batch.like_count IS NOT NULL
              WHEN ''video.comments'' THEN batch.comment_count IS NOT NULL
              WHEN ''video.published'' THEN true ELSE false END
          AND CASE r.definition->''scope''->>''kind''
              WHEN ''video'' THEN batch.video_id=r.definition->''scope''->>''id''
              WHEN ''channel'' THEN inv.channel_id=r.definition->''scope''->>''id''
              WHEN ''folder'' THEN EXISTS (SELECT FROM public.channels c WHERE c.channel_id=inv.channel_id
                      AND c.folder_id::text=r.definition->''scope''->>''id'')
              WHEN ''label'' THEN EXISTS (SELECT FROM public.channel_label_assignments l
                      WHERE l.channel_id=inv.channel_id AND l.label_key=r.definition->''scope''->>''id'')
              ELSE false END))
 SELECT metric.event,s.video_id AS subject,v.channel_id,s.id::text AS source_key,metric.at,
        h.video_type,v.published_at,v.duration,metric.measured_value,
        vclock.alert_duration_since AS duration_since,
        least(h.source_expires_at, v.availability_checked_at + interval ''696 hours'') AS evidence_expires_at,
        v.metadata_checked_at + interval ''696 hours'' AS metadata_expires_at
 FROM eligible s JOIN public.videos vclock ON vclock.video_id=s.video_id
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

CREATE OR REPLACE VIEW readable.alert_rules WITH (security_barrier=true) AS
    SELECT r.id,r.version,r.definition,r.enabled,r.started_at,r.gap_count,r.last_gap_at,
           CASE r.definition->'scope'->>'kind'
             WHEN 'folder' THEN EXISTS (SELECT FROM public.folders f WHERE f.id::text=r.definition->'scope'->>'id'
                                        AND f.created_at=r.scope_created_at)
             WHEN 'label' THEN EXISTS (SELECT FROM public.channel_labels l WHERE l.label_key=r.definition->'scope'->>'id'
                                       AND l.created_at=r.scope_created_at)
             ELSE EXISTS (SELECT FROM public.channels c WHERE c.user_work_id=r.scope_owner AND NOT c.is_hidden)
           END AS source_available,
           EXISTS (SELECT FROM public.alert_observations o WHERE o.retry_at IS NOT NULL AND (o.rule_id=r.id OR (o.members IS NOT NULL AND o.members ? r.id::text))) AS retry_pending
    FROM public.alert_rules r WHERE r.deleted_at IS NULL;
