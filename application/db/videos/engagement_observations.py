"""Resolve optional counters under the video writer's existing queue lock.

A held value keeps its acquisition clock. Snapshot and current-row writes share
one decision, including on a metadata response with a later Batch view count.
"""

RESULT_FIELDS = (',catalog_likes bigint,catalog_comments bigint,like_count_source_at timestamptz,'
                 'comment_count_source_at timestamptz,engagement_report_mask smallint,engagement_report_at timestamptz')


def engagement_projection(*, profile):
    general = "coalesce(input.general_statistics,true)" if profile else "coalesce(input.general_statistics,false)"
    observed = "coalesce(input.metadata_observed_at,input.recorded_at)" if profile else "input.recorded_at"
    joins = [f"""LEFT JOIN public.videos previous ON previous.video_id=input.video_id
      CROSS JOIN LATERAL (SELECT {general} AS general, {observed} AS observed) source"""]
    values = ["""'engagement_report_mask', CASE WHEN source.general THEN
        (CASE WHEN input.like_count IS NULL THEN 0 ELSE 1 END + CASE WHEN input.comment_count IS NULL THEN 0 ELSE 2 END)
        ELSE previous.engagement_report_mask END,
        'engagement_report_at', CASE WHEN source.general THEN source.observed ELSE previous.engagement_report_at END"""]
    for field, catalog, bit in (('like_count','catalog_likes',1), ('comment_count','catalog_comments',2)):
        clock = f"coalesce(previous.{field}_source_at,previous.checked_at)"
        # Only an ambiguous zero with no current value needs its most recent known sample.
        prior = f"""coalesce(CASE WHEN {clock} BETWEEN input.recorded_at-interval '696 hours'
            AND input.recorded_at THEN previous.{field} END, (SELECT h.{field} FROM public.video_snapshots h
            WHERE h.video_id=input.video_id AND h.{field} IS NOT NULL
              AND h.recorded_at BETWEEN input.recorded_at-interval '696 hours' AND input.recorded_at
              AND (h.engagement_observed_at IS NULL OR h.engagement_observed_at
                   BETWEEN input.recorded_at-interval '696 hours' AND input.recorded_at)
            ORDER BY h.recorded_at DESC,h.id DESC LIMIT 1))"""
        joins.append(f"""CROSS JOIN LATERAL (SELECT (NOT source.general AND input.{field}=0 AND
            ((previous.engagement_report_mask=-1 AND previous.metadata_checked_at IS NOT NULL) OR
             (previous.engagement_report_at BETWEEN input.recorded_at-interval '696 hours' AND input.recorded_at
              AND (previous.engagement_report_mask & {bit})=0)
             OR {prior}>0)) IS TRUE AS hold) {field}_decision""")
        hold = field + '_decision.hold'
        values.append(f"""'{field}', CASE WHEN NOT {hold} THEN input.{field} END,
            '{catalog}', CASE WHEN {hold} AND {clock} BETWEEN input.recorded_at-interval '696 hours'
                AND input.recorded_at THEN previous.{field} WHEN NOT {hold} THEN input.{field} END,
            '{field}_source_at', CASE WHEN {hold} AND previous.{field} IS NOT NULL
                AND {clock} BETWEEN input.recorded_at-interval '696 hours' AND input.recorded_at THEN {clock}
                WHEN NOT {hold} AND source.general AND source.observed<>input.recorded_at
                AND input.{field} IS NOT NULL THEN source.observed END""")
    return 'to_jsonb(input) || jsonb_build_object(' + ','.join(values) + ')', '\n'.join(joins)
