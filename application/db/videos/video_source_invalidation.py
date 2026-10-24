"""Explicit video clock invalidation under the existing queue owner.

The completion caller holds every affected video_collection_state row from
before capture through source DML, this effect, and lease settlement. All
current source writers/deleters respect that owner. This helper does not claim
standalone equivalence for concurrent arbitrary source SQL outside that owner.
Keep the original bulk UPDATE/UPSERT as one statement. PostgreSQL owns OLD/NEW
clock comparison and the exact transaction-now lease predicate; Python selects
the one fixed singleton write. No wall-clock implication is assumed.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from ..base.postgres import DatabaseError

_CAPTURE = """WITH ids AS (SELECT unnest(%s::text[]) AS video_id), captured AS (
 SELECT ids.video_id,video.video_id IS NOT NULL AS source_existed,
 video.checked_at,video.metadata_checked_at
 FROM ids LEFT JOIN public.videos video USING(video_id))
 SELECT count(*)::integer AS count,
 coalesce(jsonb_agg(to_jsonb(captured)),'[]'::jsonb)::text AS snapshot_text FROM captured"""
_FACTS = """WITH previous AS (
 SELECT * FROM jsonb_to_recordset(%s::jsonb) input(video_id text,source_existed boolean,
 checked_at timestamptz,metadata_checked_at timestamptz)), changed AS (
 SELECT previous.video_id FROM previous JOIN public.videos video USING(video_id)
 WHERE previous.source_existed AND (previous.checked_at IS DISTINCT FROM video.checked_at
 OR previous.metadata_checked_at IS DISTINCT FROM video.metadata_checked_at))
 SELECT (SELECT count(*)::integer FROM previous JOIN public.videos video USING(video_id)) AS present,
 EXISTS(SELECT 1 FROM changed WHERE NOT EXISTS(
 SELECT 1 FROM public.video_collection_state state WHERE state.video_id=changed.video_id
 AND state.lease_token IS NOT NULL AND state.lease_expires_at>now())) AS required"""
_INVALIDATE = """UPDATE public.video_collection_control
 SET reconciliation_required=true,invalidated_at=now(),updated_at=now()
 WHERE singleton=true AND reconciliation_required=false"""

@contextmanager
def source_clock_invalidation(transaction,video_ids):
    """Wrap one successful full-count source statement under held queue locks.

    The native OLD snapshot cannot escape this borrow. The caller raises its
    original count error inside the context and settles leases only after it.
    A failed statement/count check never reaches the effect. No retry occurs.
    """
    if (not isinstance(video_ids,(list,tuple)) or len(video_ids)>1000
            or any(type(value) is not str for value in video_ids)
            or len(set(video_ids))!=len(video_ids)):
        raise ValueError('video_source_invalidation_ids_invalid')
    with transaction.borrow():
        if not video_ids:
            yield
            return
        row=transaction.fetch_one(_CAPTURE,(list(video_ids),))
        if (not isinstance(row,Mapping) or set(row)!={'count','snapshot_text'}
                or type(row['count']) is not int or row['count']!=len(video_ids)
                or type(row['snapshot_text']) is not str):
            raise DatabaseError('database_result_shape_invalid')
        captured=row['snapshot_text']
        yield
        facts=transaction.fetch_one(_FACTS,(captured,))
        if (not isinstance(facts,Mapping) or set(facts)!={'present','required'}
                or type(facts['present']) is not int or facts['present']!=len(video_ids)
                or type(facts['required']) is not bool):
            raise DatabaseError('database_result_shape_invalid')
        if facts['required']:
            changed=transaction.execute(_INVALIDATE)
            if type(changed) is not int or changed not in (0,1):
                raise DatabaseError('database_result_shape_invalid')
