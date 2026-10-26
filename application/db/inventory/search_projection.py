"""Bounded refresh of the research catalogue read model (schema step 0035).

Two statements in one transaction, as the backend role: take queue markers,
then recompute those videos and expired rows from the protected views. Readers
never wait for it: a row that is queued or past its deadline is read from the
protected views, so a late or failed refresh costs speed only. Nothing here
returns video data.
"""
from hot_storage import RETENTION_HOURS
from ..base.postgres import Operation
from ..reads.research_sql import DURATION_PATTERN, casefold_parameters, casefold_sql, duration_seconds_sql

REFRESH_LIMIT = 2000
# Many CTE branches: JIT compilation would cost more than the refresh itself.
_REFRESH = Operation('research_projection_refresh', statement_timeout_ms=10_000,
                     lock_timeout_ms=1_000, total_timeout_ms=15_000, disable_jit=True)

# A marker is taken only at the generation this statement sees. A write that
# bumps it concurrently keeps its marker; a write that waits for this
# transaction's row lock adds a new one after it commits. Either way a later
# reader sees the video as queued until the next refresh.
_DEQUEUE = """DELETE FROM public.video_search_projection_queue q
 USING (SELECT video_id,generation FROM public.video_search_projection_queue ORDER BY video_id LIMIT %(limit)s) taken
 WHERE q.video_id=taken.video_id AND q.generation=taken.generation RETURNING q.video_id"""

# The type-proof views compare these base clocks with statement_timestamp();
# each can change an answer at t, at t + window (expiry) or at t - window
# (future evidence). A row stays current until the first such instant.
_UPDATE = """UPDATE public.video_search_projection p SET inventory_type=c.inventory_type,hot_present=c.hot_present,
 identity_clock=c.source_checked_at,identity_expiry=c.source_expires_at,title_key=c.title_key,
 duration_seconds=c.duration_seconds,valid_until=coalesce(c.valid_until,'infinity'),refreshed_at=statement_timestamp()
FROM (
 WITH todo AS MATERIALIZED (
  SELECT unnest(%(queued)s::text[]) AS video_id
  UNION SELECT expired.video_id FROM (SELECT x.video_id FROM public.video_search_projection x
   WHERE x.valid_until<=statement_timestamp() ORDER BY x.valid_until LIMIT %(limit)s) expired
 )
 SELECT t.video_id,i.video_type AS inventory_type,h.video_id IS NOT NULL AS hot_present,
  h.source_checked_at,h.source_expires_at,
  {title_key} AS title_key,
  {duration} AS duration_seconds,
  (SELECT min(boundary.at) FROM (
     SELECT unnest(ARRAY[x.discovered_at,x.skeleton_since,x.updated_at]) AS at
      FROM public.video_inventory x WHERE x.video_id=t.video_id
     UNION ALL SELECT unnest(ARRAY[m.absent_since,m.created_at,m.first_seen_at,m.last_present_at,m.last_seen_at,
      m.source_published_at,m.updated_at]) FROM public.video_inventory_memberships m WHERE m.video_id=t.video_id
     UNION ALL SELECT unnest(ARRAY[s.published_at,s.metadata_checked_at,s.checked_at,s.availability_checked_at,
      s.stream_actual_end_at,s.scheduled_start_at]) FROM public.videos s WHERE s.video_id=t.video_id) clock
   CROSS JOIN LATERAL (VALUES (clock.at),(clock.at+%(window)s::interval),(clock.at-%(window)s::interval)) boundary(at)
   WHERE isfinite(boundary.at) AND boundary.at>statement_timestamp()) AS valid_until
 FROM todo t
 LEFT JOIN LATERAL (SELECT x.video_type,x.channel_id FROM readable.video_inventory x WHERE x.video_id=t.video_id LIMIT 1) i ON true
 LEFT JOIN LATERAL (SELECT h0.video_id,h0.source_checked_at,h0.source_expires_at FROM readable.hot_video_inventory h0
  WHERE h0.video_id=t.video_id AND h0.channel_id=i.channel_id AND h0.video_type=i.video_type LIMIT 1) h ON true
 LEFT JOIN LATERAL (SELECT v.title,v.duration FROM readable.videos v WHERE v.video_id=t.video_id LIMIT 1) v ON true
 LEFT JOIN LATERAL (SELECT upper(btrim(v.duration)) AS text LIMIT 1) duration_text ON true
 LEFT JOIN LATERAL (SELECT regexp_match(duration_text.text,%(duration_pattern)s) AS parts LIMIT 1) duration_parts ON true
) c WHERE p.video_id=c.video_id""".format(
    title_key=casefold_sql("v.title"),
    duration=duration_seconds_sql("duration_text.text", "duration_parts.parts"))


class SearchProjectionRepository:
    def __init__(self, database):
        self.database = database

    def refresh(self, limit=REFRESH_LIMIT):
        """Refresh at most `limit` queued and `limit` expired rows; returns the
        number of markers taken plus rows refreshed, zero when nothing waits."""
        return sum(self.refresh_counts(limit))

    def refresh_counts(self, limit=REFRESH_LIMIT):
        """The same refresh; returns (markers taken, rows refreshed) separately.
        Both below `limit` means the batch took all the work it found."""
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError('research_projection_limit_invalid')
        with self.database.transaction(_REFRESH) as tx:
            queued = [row['video_id'] for row in tx.returning_all(_DEQUEUE, {'limit': limit}, max_rows=limit)]
            refreshed = tx.execute(_UPDATE, {'queued': queued, 'limit': limit, 'window': f'{RETENTION_HOURS} hours',
                                             'duration_pattern': DURATION_PATTERN, **casefold_parameters()})
        if type(refreshed) is not int or refreshed < 0:
            raise ValueError('research_projection_result_invalid')
        return len(queued), refreshed
