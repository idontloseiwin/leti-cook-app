ALTER TABLE public.alert_observations ADD COLUMN retry_at timestamptz;
CREATE INDEX alert_observations_ready ON public.alert_observations(id) WHERE retry_at IS NULL;
CREATE INDEX alert_observations_retry ON public.alert_observations(retry_at,id) WHERE retry_at IS NOT NULL;
CREATE INDEX alert_observations_rule_retry ON public.alert_observations(rule_id) WHERE retry_at IS NOT NULL;

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
    FROM public.alert_rules r;
