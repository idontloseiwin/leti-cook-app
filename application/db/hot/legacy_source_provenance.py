"""One-time revalidation of schema-26 copies from exact retained API observations.

This does not reconstruct a lost original clock. Only a field-owning observation
of the same identity and value can revalidate a legacy copy. The earliest such
observation is used; local job/row clocks are never substituted. SQL step 27
embeds this exact CTE, checked by focused tests. No connection is opened here.
"""

LEGACY_PROVENANCE_CTE = r"""legacy_inventory_proof AS MATERIALIZED (
 SELECT i.video_id,min(w.acquired_at) AS acquired_at
 FROM public.video_inventory i
 LEFT JOIN LATERAL (
  SELECT m.last_present_at AS acquired_at FROM public.video_inventory_memberships m
  WHERE m.video_id=i.video_id AND m.channel_id=i.channel_id
   AND m.source_published_at=i.published_at AND isfinite(m.last_present_at)
   AND m.last_present_at<=statement_timestamp()
  UNION ALL
  SELECT v.metadata_checked_at FROM public.videos v
  WHERE v.video_id=i.video_id AND v.channel_id=i.channel_id
   AND v.published_at=i.published_at AND isfinite(v.metadata_checked_at)
   AND v.metadata_checked_at<=statement_timestamp()
 ) w ON true
 WHERE i.published_at IS NOT NULL GROUP BY i.video_id
), legacy_queue_publication_proof AS MATERIALIZED (
 SELECT q.video_id,min(w.acquired_at) AS acquired_at
 FROM public.video_collection_state q
 LEFT JOIN LATERAL (
  SELECT m.last_present_at AS acquired_at FROM public.video_inventory_memberships m
  WHERE m.video_id=q.video_id AND m.channel_id=q.channel_id
   AND m.source_published_at=q.typed_source_published_at AND isfinite(m.last_present_at)
   AND m.last_present_at<=statement_timestamp()
  UNION ALL
  SELECT v.metadata_checked_at FROM public.videos v
  WHERE v.video_id=q.video_id AND v.channel_id=q.channel_id
   AND v.published_at=q.typed_source_published_at AND isfinite(v.metadata_checked_at)
   AND v.metadata_checked_at<=statement_timestamp()
 ) w ON true
 WHERE q.typed_source_published_at IS NOT NULL GROUP BY q.video_id
), legacy_queue_end_proof AS MATERIALIZED (
 SELECT q.video_id,v.metadata_checked_at AS acquired_at
 FROM public.video_collection_state q
 LEFT JOIN public.videos v ON v.video_id=q.video_id AND v.channel_id=q.channel_id
  AND v.stream_actual_end_at=q.typed_stream_actual_end_at
  AND isfinite(v.metadata_checked_at) AND v.metadata_checked_at<=statement_timestamp()
 WHERE q.typed_stream_actual_end_at IS NOT NULL
), legacy_review_proof AS MATERIALIZED (
 SELECT c.case_id,CASE WHEN
  c.observed_types <@ coalesce(w.types,ARRAY[]::text[])
  AND c.observed_channels <@ coalesce(w.channels,ARRAY[]::text[])
  AND (c.observed_canonical_type IS NULL OR c.observed_canonical_type=ANY(w.canonical_types))
  AND (c.blocked_reason IS NULL OR (c.blocked_reason='channel_conflict' AND w.foreign_channel))
 THEN w.expires_at END AS expires_at
 FROM public.video_type_review_cases c
 LEFT JOIN LATERAL (
  SELECT array_agg(DISTINCT m.video_type) AS types,
   array_agg(DISTINCT m.channel_id) AS channels,
   array_agg(DISTINCT m.video_type) FILTER(WHERE m.channel_id=c.channel_id) AS canonical_types,
   bool_or(m.channel_id<>c.channel_id) AS foreign_channel,
   min(least(m.last_present_at,m.last_seen_at)+interval '696 hours') AS expires_at
  FROM public.video_inventory_memberships m WHERE m.video_id=c.video_id
   AND m.membership_status='present' AND isfinite(m.last_present_at) AND isfinite(m.last_seen_at)
   AND m.last_present_at<=statement_timestamp() AND m.last_seen_at<=statement_timestamp()
   AND (m.video_type=ANY(c.observed_types) OR m.channel_id=ANY(c.observed_channels)
    OR (m.channel_id=c.channel_id AND m.video_type=c.observed_canonical_type)
    OR (c.blocked_reason='channel_conflict' AND m.channel_id<>c.channel_id))
 ) w ON true
 WHERE c.observed_canonical_type IS NOT NULL OR cardinality(c.observed_types)>0
  OR cardinality(c.observed_channels)>0 OR c.blocked_reason IS NOT NULL
)"""


def legacy_update_copy_window_sql():
    """Generate a schema26-only reader for the two pre-upgrade observations.

    The transition tool embeds this fixed query in the old image's existing
    read-only owner. All ordinary sources and refusal rules stay in force.
    Remove this compatibility reader when schema26 installations are retired.
    """
    from .update_copy_sources import UPDATE_COPY_SOURCE_ROWS, _source_facts, _UNPROVED
    overrides = {
        "inventory_publication": "SELECT acquired_at AS source_at,false AS inclusive FROM legacy_inventory_proof",
        "queue_publication": "SELECT acquired_at AS source_at,false AS inclusive FROM legacy_queue_publication_proof",
        "queue_stream_end": "SELECT acquired_at AS source_at,false AS inclusive FROM legacy_queue_end_proof",
        "review_evidence": "SELECT expires_at AS source_at,true AS inclusive FROM legacy_review_proof",
    }
    sources = '\n UNION ALL '.join(_source_facts(name, overrides.get(name, rows))
        for name, rows in UPDATE_COPY_SOURCE_ROWS.items())
    return """WITH clock AS MATERIALIZED(SELECT clock_timestamp() AS at),
""" + LEGACY_PROVENANCE_CTE + ", sources AS (" + sources + """), facts AS (
 SELECT min(oldest_source_at) AS oldest_source_at,
 coalesce(bool_or(invalid_source_clock),false) AS invalid_source_clock,
 coalesce(bool_or(isfinite(oldest_source_at) AND (oldest_source_at<clock.at-interval '696 hours'
   OR (inclusive AND oldest_source_at=clock.at-interval '696 hours'))),false) AS expired
 FROM sources CROSS JOIN clock)
 SELECT to_jsonb(clock.at)#>>'{}' AS as_of,to_jsonb(facts.oldest_source_at)#>>'{}' AS oldest_source_at,
 facts.expired,(facts.invalid_source_clock OR (""" + _UNPROVED + ")) AS invalid_source_clock FROM facts CROSS JOIN clock"
