-- Folder and label timestamps are user-work data: an import may preserve a
-- timestamp, including NULL. This private binding distinguishes their lifetimes.
ALTER TABLE public.folders ADD COLUMN alert_identity uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE public.channel_labels ADD COLUMN alert_identity uuid NOT NULL DEFAULT gen_random_uuid();

-- A pending channel reference can resolve to a channel that already has
-- measurements. Its old history is outside this restored rule's scope.
ALTER TABLE public.alert_rules ADD COLUMN restored_scope_since timestamptz;

UPDATE public.alert_rules r SET scope_owner=f.alert_identity
    FROM public.folders f WHERE r.deleted_at IS NULL AND r.definition->'scope'->>'kind'='folder'
    AND f.id::text=r.definition->'scope'->>'id' AND f.created_at IS NOT DISTINCT FROM r.scope_created_at;
UPDATE public.alert_rules r SET scope_owner=l.alert_identity
    FROM public.channel_labels l WHERE r.deleted_at IS NULL AND r.definition->'scope'->>'kind'='label'
    AND l.label_key=r.definition->'scope'->>'id' AND l.created_at=r.scope_created_at;

-- A regular restore updates channel_collection_state, independently of the
-- legacy visibility mirror on channels. Every scope uses this same boundary.
ALTER TABLE public.channel_collection_state ADD COLUMN alert_visible_since timestamptz;
ALTER TABLE public.channel_collection_state ALTER COLUMN alert_visible_since SET DEFAULT clock_timestamp();
CREATE FUNCTION public.alert_visibility_clock() RETURNS trigger
    LANGUAGE plpgsql SET search_path TO 'pg_catalog', 'pg_temp' AS $$
BEGIN
    NEW.alert_visible_since := clock_timestamp();
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.alert_visibility_clock() FROM PUBLIC;
CREATE TRIGGER alert_visibility_clock BEFORE UPDATE OF is_hidden ON public.channel_collection_state
    FOR EACH ROW WHEN (OLD.is_hidden IS DISTINCT FROM NEW.is_hidden)
    EXECUTE FUNCTION public.alert_visibility_clock();

DROP TRIGGER alert_folder_clock ON public.channels;
CREATE TRIGGER alert_folder_clock BEFORE UPDATE OF folder_id ON public.channels
    FOR EACH ROW WHEN (OLD.folder_id IS DISTINCT FROM NEW.folder_id)
    EXECUTE FUNCTION public.alert_folder_clock();

-- A transaction which started before an observation may assign a label
-- afterwards. Record the time of that assignment rather than transaction start.
ALTER TABLE public.channel_label_assignments ALTER COLUMN created_at SET DEFAULT clock_timestamp();

CREATE OR REPLACE FUNCTION public.capture_alert_observations() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path TO 'pg_catalog', 'pg_temp'
    AS $$
DECLARE
    delivery_projection text;
    packet public.alert_observations[];
    source_projection text := TG_ARGV[1];
    membership_clock text := CASE WHEN TG_RELID='public.video_availability_events'::regclass
                                  THEN 'f.scope_at' ELSE 'f.at' END;
    retry_sources refcursor;
    source_row jsonb;
    unknown_sources bigint := 0;
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
    LOOP
      BEGIN
        FOR packet IN EXECUTE format($query$
        WITH rules AS MATERIALIZED (
            SELECT r.* FROM public.alert_rules r
            WHERE r.enabled AND r.definition->>'event' = ANY($1)
            ORDER BY r.id FOR KEY SHARE
        ), facts AS MATERIALIZED (%s), matched(rule_id,version,event,subject,channel_id,channel_ref,
            source_key,observed_at,available_from,rule_started_at,evidence_expires_at,definition,
            video_type,published_at,duration) AS (
        SELECT r.id,r.version,f.event,f.subject,c.channel_id,c.user_work_id,f.source_key,f.at,
               boundary.since,
               r.started_at,least(f.at + interval '696 hours',f.evidence_expires_at,
                 CASE WHEN f.event='video.published' OR r.definition ? 'min_duration' OR r.definition ? 'max_duration'
                      THEN f.metadata_expires_at END),r.definition,f.video_type,
               CASE WHEN f.event='video.published' THEN f.published_at END,
               CASE WHEN r.definition ? 'min_duration' OR r.definition ? 'max_duration' THEN f.duration END
        FROM facts f JOIN public.channels c ON c.channel_id=f.channel_id
        JOIN public.channel_collection_state control ON control.channel_id=c.channel_id
         AND control.user_work_id=c.user_work_id AND NOT control.is_hidden
        JOIN rules r ON r.definition->>'event'=f.event
         AND r.started_at < f.at AND r.definition->>'video_type' IS NOT DISTINCT FROM f.video_type
        LEFT JOIN public.folders folder ON r.definition->'scope'->>'kind'='folder'
         AND folder.id=c.folder_id AND folder.alert_identity=r.scope_owner
        LEFT JOIN public.channel_labels label ON r.definition->'scope'->>'kind'='label'
         AND label.label_key=r.definition->'scope'->>'id' AND label.alert_identity=r.scope_owner
        LEFT JOIN public.channel_label_assignments assignment ON assignment.channel_id=c.channel_id
         AND r.definition->'scope'->>'kind'='label' AND assignment.label_key=r.definition->'scope'->>'id'
        CROSS JOIN LATERAL (SELECT greatest(coalesce(c.added_at,
                    greatest(r.started_at,statement_timestamp()-interval '696 hours')-interval '696 hours'),
                        control.alert_visible_since,r.restored_scope_since,
                        CASE WHEN r.definition->'scope'->>'kind'='folder' THEN c.alert_folder_since
                        WHEN r.definition->'scope'->>'kind'='label' THEN assignment.created_at END,
                        CASE WHEN r.definition ? 'min_duration' OR r.definition ? 'max_duration' THEN f.duration_since END) AS since) boundary
        WHERE %s>=boundary.since AND isfinite(f.at) AND f.at BETWEEN statement_timestamp()-interval '696 hours' AND clock_timestamp()
          -- Ineligible samples stay in source history for the next comparison.
          AND CASE
            WHEN f.event='video.published' THEN f.published_at>greatest(r.started_at,boundary.since)
            WHEN f.measured_value IS NULL THEN true
            WHEN NOT coalesce(pg_input_is_valid(r.definition->>'threshold','bigint'),false) THEN true
            WHEN r.definition->>'direction'='up' THEN f.measured_value >= (r.definition->>'threshold')::bigint
            WHEN r.definition->>'direction'='down' THEN f.measured_value <= (r.definition->>'threshold')::bigint
            ELSE true END
          AND CASE
            WHEN NOT (r.definition ?| ARRAY['min_duration','max_duration']) THEN true
            WHEN f.duration IS NULL THEN false
            -- Unusual legacy spellings and damaged definitions stay with the
            -- worker's full parser. Normal provider counters and ISO durations
            -- are filtered here before freezing recipients or charging a gap.
            WHEN octet_length(f.duration)<>length(f.duration) OR length(f.duration)>4300 THEN true
            WHEN (r.definition ? 'min_duration' AND NOT pg_input_is_valid(r.definition->>'min_duration','bigint'))
              OR (r.definition ? 'max_duration' AND NOT pg_input_is_valid(r.definition->>'max_duration','bigint')) THEN true
            ELSE EXISTS (
              SELECT FROM (SELECT upper(regexp_replace(f.duration,'^[[:space:]]+|[[:space:]]+$','','g')) AS value) t
              CROSS JOIN LATERAL (SELECT regexp_match(t.value,
                '^P(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)S)?)?$') AS parts LIMIT 1) parsed
              CROSS JOIN LATERAL (SELECT CASE WHEN t.value ~ '^[0-9]+$' THEN t.value::numeric
                WHEN parsed.parts IS NOT NULL AND t.value ~ '[0-9]' AND right(t.value,1)<>'T'
                THEN coalesce(parts[1]::numeric,0)*86400+coalesce(parts[2]::numeric,0)*3600+
                     coalesce(parts[3]::numeric,0)*60+coalesce(parts[4]::numeric,0) END AS seconds) duration
              WHERE duration.seconds>=coalesce((r.definition->>'min_duration')::bigint,0)
                AND (NOT (r.definition ? 'max_duration') OR duration.seconds<=(r.definition->>'max_duration')::bigint))
            END
          AND CASE r.definition->'scope'->>'kind'
            WHEN 'channel' THEN c.channel_id=r.definition->'scope'->>'id' AND r.scope_owner=c.user_work_id
            WHEN 'folder' THEN folder.id::text=r.definition->'scope'->>'id'
            WHEN 'label' THEN label.label_key IS NOT NULL AND assignment.channel_id IS NOT NULL
            WHEN 'video' THEN f.subject=r.definition->'scope'->>'id' AND r.scope_owner=c.user_work_id
            ELSE false END
        ), deliveries(rule_id,version,event,subject,channel_id,channel_ref,source_key,observed_at,
            available_from,rule_started_at,evidence_expires_at,definition,video_type,published_at,duration,members) AS (
            %s
        )
        -- Freeze each completed projection once, including candidates for other
        -- rules in the same statement. No new snapshot is used to reconstruct
        -- its scope if writing the queue fails. Bound each write and its retry.
        SELECT array_agg(ROW(NULL::bigint,d.rule_id,d.version,d.event,d.subject,d.channel_id,d.channel_ref,
            d.source_key,d.observed_at,d.available_from,d.rule_started_at,d.evidence_expires_at,d.definition,
            d.video_type,d.published_at,d.duration,NULL::timestamptz,NULL::timestamptz,d.members)::public.alert_observations)
        FROM (SELECT deliveries.*,(row_number() OVER ()-1)/256 AS page FROM deliveries) d GROUP BY d.page
    $query$, source_projection, membership_clock, delivery_projection) USING string_to_array(TG_ARGV[0], ','),source_row
    LOOP
        BEGIN
            INSERT INTO public.alert_observations(rule_id,version,event,subject,channel_id,channel_ref,
                source_key,observed_at,available_from,rule_started_at,evidence_expires_at,definition,
                video_type,published_at,duration,members)
            SELECT d.rule_id,d.version,d.event,d.subject,d.channel_id,d.channel_ref,d.source_key,d.observed_at,
                d.available_from,d.rule_started_at,d.evidence_expires_at,d.definition,
                d.video_type,d.published_at,d.duration,d.members FROM unnest(packet) d
            ON CONFLICT (rule_id,version,source_key) DO NOTHING;
        EXCEPTION WHEN data_exception OR integrity_constraint_violation THEN
            -- The entire packet rolled back. Count each lost rule-version/source
            -- candidate once. A replay already queued or delivered is covered.
            WITH attempts AS (
                SELECT DISTINCT t.id,t.version,d.event,d.source_key,d.subject FROM unnest(packet) d
                CROSS JOIN LATERAL (
                    SELECT d.rule_id AS id,d.version WHERE d.rule_id IS NOT NULL
                    UNION ALL SELECT key::uuid,(value->>'version')::integer FROM jsonb_each(d.members)
                ) t
            ), shared_receipts AS MATERIALIZED (
                -- Check all rules for each queued source together. Looking
                -- up its entire recipient list separately for every rule
                -- would repeatedly scan the same backlog during an outage.
                SELECT o.event,o.source_key,member.key AS target,member.value->>'version' AS version
                FROM public.alert_observations o CROSS JOIN LATERAL jsonb_each(o.members) member
                WHERE o.members IS NOT NULL AND o.source_key=ANY(ARRAY(SELECT d.source_key FROM unnest(packet) d))
            ), lost AS (
                SELECT a.id,count(*) AS n FROM attempts a
                WHERE NOT EXISTS (SELECT FROM public.alert_observations o
                    WHERE (o.rule_id,o.version,o.source_key,o.event)=(a.id,a.version,a.source_key,a.event))
                  AND NOT EXISTS (SELECT FROM shared_receipts q WHERE (q.target,q.version,q.source_key,q.event)=
                        (a.id::text,a.version::text,a.source_key,a.event))
                  AND NOT EXISTS (SELECT FROM public.alert_inbox n WHERE (n.rule_id,n.version,n.source_key)=
                        (a.id,a.version,a.source_key))
                  AND (a.event<>'video.published' OR NOT EXISTS (SELECT FROM public.alert_inbox n
                        WHERE n.event='video.published' AND n.rule_id=a.id AND n.subject=a.subject))
                GROUP BY a.id
            )
            UPDATE public.alert_rules r SET gap_count=least(r.gap_count::numeric+lost.n,9223372036854775807)::bigint,last_gap_at=statement_timestamp()
                FROM lost WHERE r.id=lost.id AND r.deleted_at IS NULL;
        END;
        END LOOP;
      EXCEPTION WHEN data_exception OR integrity_constraint_violation THEN
        -- A failed projection has no trustworthy recipients. Retry the native
        -- transition rows individually so healthy neighbours still enqueue.
        IF retry_sources IS NULL THEN
            OPEN retry_sources FOR SELECT to_jsonb(s) FROM inserted s;
            source_projection := replace(TG_ARGV[1], 'FROM inserted ',
                format('FROM jsonb_populate_record(NULL::%s,$2) ',TG_RELID::regclass));
        ELSE
            unknown_sources := unknown_sources+1;
        END IF;
      END;
      EXIT WHEN retry_sources IS NULL;
      FETCH retry_sources INTO source_row;
      EXIT WHEN NOT FOUND;
    END LOOP;
    IF retry_sources IS NOT NULL THEN CLOSE retry_sources; END IF;
    IF unknown_sources>0 THEN
        RAISE WARNING 'Alert capture could not identify recipients for % source row(s)',unknown_sources;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER alert_availability_observations ON public.video_availability_events;
CREATE TRIGGER alert_availability_observations AFTER INSERT ON public.video_availability_events
REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION public.capture_alert_observations(
'video.unavailable,video.restored',
'SELECT CASE s.event_type WHEN ''restored'' THEN ''video.restored'' ELSE ''video.unavailable'' END AS event,
        s.video_id AS subject,s.channel_id,NULL::bigint AS measured_value,s.id::text AS source_key,greatest(s.observed_at,s.created_at) AS at,s.observed_at AS scope_at,i.video_type,
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

CREATE OR REPLACE VIEW readable.alert_rules WITH (security_barrier=true) AS
    SELECT r.id,r.version,r.definition,r.enabled,r.started_at,r.gap_count,r.last_gap_at,
           CASE r.definition->'scope'->>'kind'
             WHEN 'folder' THEN EXISTS (SELECT FROM public.folders f WHERE f.id::text=r.definition->'scope'->>'id'
                                        AND f.alert_identity=r.scope_owner)
             WHEN 'label' THEN EXISTS (SELECT FROM public.channel_labels l WHERE l.label_key=r.definition->'scope'->>'id'
                                       AND l.alert_identity=r.scope_owner)
             WHEN 'channel' THEN EXISTS (SELECT FROM public.channels c JOIN public.channel_collection_state control
                 USING(channel_id,user_work_id) WHERE c.user_work_id=r.scope_owner
                 AND c.channel_id=r.definition->'scope'->>'id' AND NOT control.is_hidden)
             WHEN 'video' THEN EXISTS (SELECT FROM readable.hot_video_inventory h JOIN public.channels c USING(channel_id)
                 JOIN public.channel_collection_state control USING(channel_id,user_work_id)
                 WHERE h.video_id=r.definition->'scope'->>'id' AND c.user_work_id=r.scope_owner AND NOT control.is_hidden)
             ELSE false
           END AS source_available,
           EXISTS (SELECT FROM public.alert_observations o WHERE o.retry_at IS NOT NULL AND (o.rule_id=r.id OR (o.members IS NOT NULL AND o.members ? r.id::text))) AS retry_pending, r.scope_owner
    FROM public.alert_rules r WHERE r.deleted_at IS NULL;
