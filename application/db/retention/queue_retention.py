"""Exact queue probes that also bound work on a newly populated queue.

Seek one existing key through the primary-key order before probing its row.
The exact match stays on the outer query: locking callers must not acquire a
neighbouring row on a miss. Callers retain their NOWAIT or SKIP LOCKED protocol.
"""
from psycopg.types.json import Jsonb

_SEEK = """SELECT q.video_id FROM public.video_collection_state q
 WHERE q.video_id>=%(video_id)s ORDER BY q.video_id LIMIT 1"""
_MATCH = " WHERE qs.video_id=%(video_id)s AND %(video_id)s=(" + _SEEK + ") LIMIT 1"
QUEUE_PAYLOAD_SQL = "SELECT to_jsonb(qs)::text AS value FROM public.video_collection_state qs" + _MATCH
QUEUE_LEASE_SQL = """SELECT lease_token IS NOT NULL AS has_token,
 to_jsonb(lease_expires_at)#>>'{}' AS expires_at FROM public.video_collection_state qs""" + _MATCH
QUEUE_EXISTS_SQL = "SELECT coalesce(%(video_id)s=(" + _SEEK + "),false) AS present"

# A page of videos at once. SKIP LOCKED instead of a per-row NOWAIT: a queue
# row another transaction holds is present but not returned. The lease clock is
# sampled after the row locks, in its own statement.
_PAGE_LEASES = """SELECT v.video_id,EXISTS(SELECT 1 FROM public.video_collection_state p WHERE p.video_id=v.video_id) AS present,
 q.has_token,q.expires_at FROM unnest(%(videos)s::text[]) WITH ORDINALITY v(video_id,ord)
 LEFT JOIN LATERAL(SELECT qs.lease_token IS NOT NULL AS has_token,to_jsonb(qs.lease_expires_at)#>>'{}' AS expires_at
  FROM public.video_collection_state qs WHERE qs.video_id=v.video_id FOR UPDATE SKIP LOCKED) q ON true ORDER BY v.ord"""
_PAGE_ACTIVE = """SELECT l.video_id,l.expires_at::timestamptz>clock_timestamp() AS active
 FROM jsonb_to_recordset(%(leases)s::jsonb) l(video_id text,expires_at text)"""


def busy_videos(tx, videos):
    """Lock the queue rows of these videos; return the ones a collector owns.

    A video is busy when another transaction holds its queue row or its lease
    has not expired. A video without a queue row is not busy.
    """
    videos = sorted(set(videos))
    if not videos:
        return set()
    leases = tx.fetch_all(_PAGE_LEASES, dict(videos=videos), max_rows=len(videos))
    busy = {lease['video_id'] for lease in leases if lease['present'] and lease['has_token'] is None}
    held = [dict(video_id=lease['video_id'], expires_at=lease['expires_at']) for lease in leases if lease['has_token']]
    if held:
        busy |= {row['video_id'] for row in tx.fetch_all(_PAGE_ACTIVE, dict(leases=Jsonb(held)), max_rows=len(held))
                 if row['active'] is True}
    return busy
