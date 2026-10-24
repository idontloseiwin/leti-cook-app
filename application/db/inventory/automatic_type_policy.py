"""Canonical SQL proof for the exact, fresh Long/Short playlist overlap.

The candidate is a derived proposal, not a manual decision or a source of new
YouTube evidence. Consumers must match it to the current canonical type before
admitting reads. The schema embeds this query verbatim; its consistency test
prevents a second interpretation of the policy in Python and PostgreSQL.
"""

# Group raw present memberships before reading duration. The aggregate remains
# inline so exact-video predicates can still reach its indexed input. The
# metadata LATERAL barrier keeps duration parsing behind the overlap proof.
AUTOMATIC_TYPE_CANDIDATES_SQL = r"""SELECT evidence.video_id,
    evidence.channel_id,
    CASE WHEN duration_value.seconds <= 180 THEN 'short'::text ELSE 'long'::text END AS automatic_type,
    least(v.metadata_checked_at, evidence.oldest_clock)+interval '696 hours' AS source_expires_at
FROM (
    SELECT i.video_id,i.channel_id,
        min(least(m.last_seen_at, m.last_present_at)) AS oldest_clock
    FROM public.video_inventory i
    JOIN public.video_inventory_memberships m ON m.video_id=i.video_id AND m.membership_status='present'
    WHERE (i.video_type IS NULL OR i.video_type IN ('long','short'))
      AND NOT EXISTS (
          SELECT 1 FROM public.video_type_review_cases decision
          WHERE decision.video_id=i.video_id AND decision.decision_type IS NOT NULL
      )
    GROUP BY i.video_id,i.channel_id
    HAVING array_agg(DISTINCT m.video_type ORDER BY m.video_type)=ARRAY['long','short']::text[]
       AND bool_and((m.channel_id=i.channel_id
         AND isfinite(m.last_seen_at)
         AND m.last_seen_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
         AND isfinite(m.last_present_at)
         AND m.last_present_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()) IS TRUE)
) evidence
JOIN LATERAL (
    SELECT metadata.metadata_checked_at,metadata.duration
    FROM public.videos metadata
    WHERE metadata.video_id=evidence.video_id AND metadata.channel_id=evidence.channel_id
    OFFSET 0
) v ON true
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
  AND v.metadata_checked_at BETWEEN statement_timestamp()-interval '696 hours' AND statement_timestamp()
  AND duration_value.seconds > 0"""
