-- Data lifecycle: retained values carry their actual observation clocks.
ALTER TABLE public.video_inventory ADD COLUMN published_acquired_at timestamptz;
ALTER TABLE public.video_collection_state
 ADD COLUMN typed_source_published_acquired_at timestamptz,
 ADD COLUMN typed_source_stream_end_acquired_at timestamptz;
ALTER TABLE public.video_type_review_cases ADD COLUMN evidence_expires_at timestamptz;

-- A saved, exact API observation revalidates the legacy value. This does not
-- manufacture the original copy clock, use job completion time or call YouTube.
CREATE TEMP TABLE lifecycle27_source_proofs ON COMMIT DROP AS
WITH
-- BEGIN LEGACY_PROVENANCE_CTE
legacy_inventory_proof AS MATERIALIZED (
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
)
-- END LEGACY_PROVENANCE_CTE
SELECT 'inventory'::text AS kind,video_id AS identity,acquired_at AS source_time FROM legacy_inventory_proof
UNION ALL SELECT 'queue_publication',video_id,acquired_at FROM legacy_queue_publication_proof
UNION ALL SELECT 'queue_end',video_id,acquired_at FROM legacy_queue_end_proof
UNION ALL SELECT 'review',case_id::text,expires_at FROM legacy_review_proof;
DO $lifecycle27$
BEGIN
 IF EXISTS(SELECT 1 FROM lifecycle27_source_proofs WHERE source_time IS NULL OR NOT isfinite(source_time)) THEN
  RAISE EXCEPTION 'legacy_source_provenance_unconfirmed';
 END IF;
END $lifecycle27$;
UPDATE public.video_inventory i SET published_acquired_at=p.source_time
 FROM lifecycle27_source_proofs p WHERE p.kind='inventory' AND p.identity=i.video_id;
UPDATE public.video_collection_state q SET typed_source_published_acquired_at=p.source_time
 FROM lifecycle27_source_proofs p WHERE p.kind='queue_publication' AND p.identity=q.video_id;
UPDATE public.video_collection_state q SET typed_source_stream_end_acquired_at=p.source_time
 FROM lifecycle27_source_proofs p WHERE p.kind='queue_end' AND p.identity=q.video_id;
UPDATE public.video_type_review_cases c SET evidence_expires_at=p.source_time
 FROM lifecycle27_source_proofs p WHERE p.kind='review' AND p.identity=c.case_id::text;

-- Old typed missing results renewed the availability clock but retained details
-- absent from that response. Remove only those unconfirmed details; keep the
-- missing observation, metadata, counters, source clocks and scheduling intact.
-- BEGIN LEGACY_AVAILABILITY_REPAIR
UPDATE public.videos SET privacy_status=NULL,upload_status=NULL
 WHERE availability_status='unknown_unavailable'
  AND (privacy_status IS NOT NULL OR upload_status IS NOT NULL);
-- END LEGACY_AVAILABILITY_REPAIR

ALTER TABLE public.video_inventory ADD CONSTRAINT video_inventory_publication_source_pair CHECK
 ((published_at IS NULL AND published_acquired_at IS NULL) OR
  (published_at IS NOT NULL AND published_acquired_at IS NOT NULL AND isfinite(published_acquired_at)));
ALTER TABLE public.video_collection_state ADD CONSTRAINT video_collection_publication_source_pair CHECK
 ((typed_source_published_at IS NULL AND typed_source_published_acquired_at IS NULL) OR
  (typed_source_published_at IS NOT NULL AND typed_source_published_acquired_at IS NOT NULL
   AND isfinite(typed_source_published_acquired_at)));
ALTER TABLE public.video_collection_state ADD CONSTRAINT video_collection_stream_end_source_pair CHECK
 ((typed_stream_actual_end_at IS NULL AND typed_source_stream_end_acquired_at IS NULL) OR
  (typed_stream_actual_end_at IS NOT NULL AND typed_source_stream_end_acquired_at IS NOT NULL
   AND isfinite(typed_source_stream_end_acquired_at)));
ALTER TABLE public.video_type_review_cases ADD CONSTRAINT video_review_evidence_deadline_finite CHECK
 (evidence_expires_at IS NULL OR isfinite(evidence_expires_at));
CREATE INDEX video_inventory_publication_source_idx ON public.video_inventory(published_acquired_at)
 WHERE published_at IS NOT NULL;
CREATE INDEX video_collection_publication_source_idx ON public.video_collection_state(typed_source_published_acquired_at)
 WHERE typed_source_published_at IS NOT NULL;
CREATE INDEX video_collection_stream_end_source_idx ON public.video_collection_state(typed_source_stream_end_acquired_at)
 WHERE typed_stream_actual_end_at IS NOT NULL;
CREATE INDEX video_review_evidence_deadline_idx ON public.video_type_review_cases(evidence_expires_at)
 WHERE observed_canonical_type IS NOT NULL OR cardinality(observed_types)>0
  OR cardinality(observed_channels)>0 OR blocked_reason IS NOT NULL;
GRANT INSERT (typed_source_published_acquired_at) ON public.video_collection_state TO service_role;
GRANT UPDATE (typed_source_published_acquired_at,typed_source_stream_end_acquired_at)
 ON public.video_collection_state TO service_role;
GRANT INSERT (evidence_expires_at) ON public.video_type_review_cases TO service_role;
GRANT UPDATE (evidence_expires_at) ON public.video_type_review_cases TO service_role;

CREATE OR REPLACE VIEW readable.video_type_review_cases WITH (security_barrier=true, security_invoker=false) AS
SELECT c.video_id AS video_id,
    c.channel_id AS channel_id,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_canonical_type END AS observed_canonical_type,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_types ELSE ARRAY[]::text[] END AS observed_types,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.observed_channels ELSE ARRAY[]::text[] END AS observed_channels,
    c.state AS state,
    c.revision AS revision,
    c.first_seen_at AS first_seen_at,
    c.last_seen_at AS last_seen_at,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.blocked_reason END AS blocked_reason,
    c.decision_type AS decision_type,
    c.decision_reason AS decision_reason,
    c.decision_at AS decision_at,
    c.decision_revision AS decision_revision,
    c.case_id AS case_id,
    c.source_detached_at AS source_detached_at,
    c.user_channel_ref AS user_channel_ref,
    c.restore_owner_ref AS restore_owner_ref,
    CASE WHEN isfinite(c.evidence_expires_at) AND c.evidence_expires_at > statement_timestamp() AND c.evidence_expires_at <= statement_timestamp()+interval '696 hours' THEN c.evidence_expires_at END AS source_expires_at,
    coalesce(c.video_id IS NOT NULL
      AND EXISTS(SELECT 1 FROM public.video_inventory i WHERE i.video_id=c.video_id AND i.channel_id=c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.video_collection_state q WHERE q.video_id=c.video_id AND q.channel_id<>c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=c.video_id
        AND m.membership_status='present' AND m.channel_id<>c.channel_id)
      AND NOT EXISTS(SELECT 1 FROM public.videos v WHERE v.video_id=c.video_id AND v.channel_id<>c.channel_id),false)
      AS decision_unblocked
FROM public.video_type_review_cases c;
