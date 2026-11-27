ALTER TABLE public.alert_rules ADD COLUMN deleted_at timestamptz
    CHECK (deleted_at IS NULL OR NOT enabled);
CREATE INDEX alert_rules_deleted ON public.alert_rules(deleted_at,id) WHERE deleted_at IS NOT NULL;
CREATE INDEX alert_observations_rule_ready ON public.alert_observations(rule_id,id) WHERE retry_at IS NULL;
CREATE INDEX alert_observations_rule_due ON public.alert_observations(rule_id,retry_at,id) WHERE retry_at IS NOT NULL;

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
        WITH facts AS MATERIALIZED (%s), rules AS MATERIALIZED (
            SELECT r.* FROM public.alert_rules r
            WHERE r.enabled AND r.definition->>'event' = ANY($1)
            ORDER BY r.id FOR KEY SHARE
        )
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
        ON CONFLICT (rule_id,version,source_key) DO NOTHING
    $query$, TG_ARGV[1]) USING string_to_array(TG_ARGV[0], ',');
    EXCEPTION WHEN data_exception OR integrity_constraint_violation THEN
        -- The source fact belongs to collection. Expose an observation gap if
        -- the optional delivery record cannot represent this statement.
        UPDATE public.alert_rules SET gap_count=gap_count+1,last_gap_at=statement_timestamp()
         WHERE enabled AND definition->>'event'=ANY(string_to_array(TG_ARGV[0], ','));
    END;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE VIEW readable.alert_rules WITH (security_barrier=true) AS
    SELECT r.id,r.version,r.definition,r.enabled,r.started_at,r.gap_count,r.last_gap_at,
           CASE r.definition->'scope'->>'kind'
             WHEN 'folder' THEN EXISTS (SELECT FROM public.folders f WHERE f.id::text=r.definition->'scope'->>'id'
                                        AND f.created_at=r.scope_created_at)
             WHEN 'label' THEN EXISTS (SELECT FROM public.channel_labels l WHERE l.label_key=r.definition->'scope'->>'id'
                                       AND l.created_at=r.scope_created_at)
             ELSE EXISTS (SELECT FROM public.channels c WHERE c.user_work_id=r.scope_owner AND NOT c.is_hidden)
           END AS source_available,
           EXISTS (SELECT FROM public.alert_observations o WHERE o.rule_id=r.id AND o.retry_at IS NOT NULL) AS retry_pending
    FROM public.alert_rules r WHERE r.deleted_at IS NULL;
CREATE OR REPLACE VIEW readable.alert_inbox WITH (security_barrier=true) AS
    SELECT n.* FROM public.alert_inbox n JOIN readable.channel_user_work c
      ON c.channel_id=n.channel_id AND c.user_work_id=n.channel_ref AND NOT c.is_hidden
    JOIN public.alert_rules r ON r.id=n.rule_id AND r.deleted_at IS NULL
    WHERE n.expires_at>statement_timestamp();
