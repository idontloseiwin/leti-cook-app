"""Exact queue probes that also bound work on a newly populated queue.

Seek one existing key through the primary-key order before probing its row.
The exact match stays on the outer query: locking callers must not acquire a
neighbouring row on a miss. Callers retain their NOWAIT or SKIP LOCKED protocol.
"""

_SEEK = """SELECT q.video_id FROM public.video_collection_state q
 WHERE q.video_id>=%(video_id)s ORDER BY q.video_id LIMIT 1"""
_MATCH = " WHERE qs.video_id=%(video_id)s AND %(video_id)s=(" + _SEEK + ") LIMIT 1"
QUEUE_PAYLOAD_SQL = "SELECT to_jsonb(qs)::text AS value FROM public.video_collection_state qs" + _MATCH
QUEUE_LEASE_SQL = """SELECT lease_token IS NOT NULL AS has_token,
 to_jsonb(lease_expires_at)#>>'{}' AS expires_at FROM public.video_collection_state qs""" + _MATCH
QUEUE_EXISTS_SQL = "SELECT coalesce(%(video_id)s=(" + _SEEK + "),false) AS present"
