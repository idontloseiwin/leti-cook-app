"""Borrowed review evidence effects at the source-write boundary.

The caller supplies one source event inside its original transaction. This
module never opens a connection, commits, retries or adjusts a canonical type.
Owner-binding remains a named dependency of the retained case INSERT/detach.
"""
from ..base.postgres import DatabaseServerError

_ADVISORY = 'SELECT pg_advisory_xact_lock(hashtextextended(%s::text,962106)) AS acquired'
_INVENTORY = '''SELECT channel_id,video_type FROM public.video_inventory i WHERE video_id=%s::text
 AND (i.skeleton_since IS NULL OR EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=i.video_id))'''
_PRESENT = 'SELECT EXISTS(SELECT 1 FROM public.video_type_review_cases WHERE video_id=%s::text) AS present'
_WITHDRAW = """UPDATE public.video_type_review_cases SET observed_canonical_type=NULL,
 observed_types=ARRAY[]::text[],observed_channels=ARRAY[]::text[],blocked_reason=NULL,evidence_expires_at=NULL,
 revision=revision+CASE WHEN observed_canonical_type IS NOT NULL OR cardinality(observed_types)>0
 OR cardinality(observed_channels)>0 OR blocked_reason IS NOT NULL THEN 1 ELSE 0 END
 WHERE video_id=%s::text"""
_EVIDENCE = """SELECT coalesce(array_agg(DISTINCT video_type ORDER BY video_type),ARRAY[]::text[]) AS types,
 coalesce(array_agg(DISTINCT channel_id ORDER BY channel_id),ARRAY[]::text[]) AS channels,
 to_jsonb(min(least(last_present_at,last_seen_at))+interval '696 hours')#>>'{}' AS evidence_expires_at
 FROM public.video_inventory_memberships WHERE video_id=%s::text AND membership_status='present'
 AND isfinite(last_present_at) AND last_present_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND isfinite(last_seen_at) AND last_seen_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()"""
_BLOCKED = """SELECT CASE WHEN EXISTS(SELECT 1 FROM unnest(%(channels)s::text[]) AS c(id)
 WHERE c.id<>%(channel_id)s::text)
 OR EXISTS(SELECT 1 FROM public.videos WHERE video_id=%(video_id)s::text AND channel_id<>%(channel_id)s::text)
 OR EXISTS(SELECT 1 FROM public.video_collection_state WHERE video_id=%(video_id)s::text AND channel_id<>%(channel_id)s::text)
 THEN 'channel_conflict' END AS blocked"""
# This aggregate inherits every contributing membership's remaining lifetime.
# Local recomputation never grants those copies another retention window. Raw
# identity conflicts still fence decisions, but are not fresh API observations.
_UPSERT = """INSERT INTO public.video_type_review_cases AS c(
 video_id,channel_id,observed_canonical_type,observed_types,observed_channels,state,blocked_reason,evidence_expires_at)
 VALUES(%(video_id)s::text,%(channel_id)s::text,
 CASE WHEN %(removed)s::boolean IS NOT TRUE AND %(evidence_expires_at)s::timestamptz IS NOT NULL
 AND %(video_type)s::text=ANY(%(types)s::text[]) THEN %(video_type)s::text END,
 CASE WHEN %(evidence_expires_at)s::timestamptz IS NOT NULL THEN %(types)s::text[] ELSE ARRAY[]::text[] END,
 CASE WHEN %(evidence_expires_at)s::timestamptz IS NOT NULL THEN %(channels)s::text[] ELSE ARRAY[]::text[] END,'open',
 CASE WHEN %(evidence_expires_at)s::timestamptz IS NOT NULL AND EXISTS(
 SELECT 1 FROM unnest(%(channels)s::text[]) channel(id) WHERE channel.id<>%(channel_id)s::text)
 THEN 'channel_conflict' END,%(evidence_expires_at)s::timestamptz)
 ON CONFLICT(video_id) DO UPDATE SET observed_canonical_type=excluded.observed_canonical_type,
 observed_types=excluded.observed_types,observed_channels=excluded.observed_channels,
 evidence_expires_at=excluded.evidence_expires_at,
 blocked_reason=CASE WHEN c.channel_id<>excluded.channel_id AND excluded.evidence_expires_at IS NOT NULL
 AND cardinality(excluded.observed_channels)>0 THEN 'channel_conflict' ELSE excluded.blocked_reason END,
 state=CASE WHEN %(removed)s::boolean THEN c.state WHEN c.decision_type IS NOT NULL AND c.channel_id=excluded.channel_id
 AND excluded.blocked_reason IS NULL
 AND NOT EXISTS(SELECT 1 FROM public.videos v WHERE v.video_id=excluded.video_id AND v.channel_id<>excluded.channel_id)
 AND NOT EXISTS(SELECT 1 FROM public.video_collection_state q WHERE q.video_id=excluded.video_id AND q.channel_id<>excluded.channel_id)
 THEN 'resolved' ELSE 'open' END,
 revision=c.revision+CASE WHEN (c.observed_canonical_type,c.observed_types,c.observed_channels,c.blocked_reason)
 IS DISTINCT FROM (excluded.observed_canonical_type,excluded.observed_types,excluded.observed_channels,
 CASE WHEN c.channel_id<>excluded.channel_id AND excluded.evidence_expires_at IS NOT NULL
 AND cardinality(excluded.observed_channels)>0 THEN 'channel_conflict' ELSE excluded.blocked_reason END) THEN 1 ELSE 0 END,
 last_seen_at=CASE WHEN %(removed)s::boolean THEN c.last_seen_at ELSE clock_timestamp() END"""
_DETACH_ADVISORY = 'SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text,962106)) AS acquired'
_DETACH_CASE = 'SELECT case_id::text AS case_id FROM public.video_type_review_cases WHERE video_id=%s::text FOR UPDATE NOWAIT'
_DETACH = """UPDATE public.video_type_review_cases SET video_id=NULL,channel_id=NULL,
 observed_canonical_type=NULL,observed_types=ARRAY[]::text[],observed_channels=ARRAY[]::text[],
 blocked_reason=NULL,evidence_expires_at=NULL,source_detached_at=clock_timestamp(),revision=revision+1 WHERE case_id=%s::uuid"""


def refresh_review_case(transaction, video_id, *, source_removed=False):
    """Retain advisory serialization and native conflict-time case values."""
    if source_removed is not None and type(source_removed) is not bool:
        raise ValueError('review_source_removed_invalid')
    with transaction.borrow():
        transaction.fetch_one(_ADVISORY, (video_id,))
        # Removing evidence never creates a case. Under the same advisory lock,
        # an absent case proves a no-op without rereading the inventory row.
        if source_removed is True and not transaction.fetch_one(_PRESENT, (video_id,))['present']:
            return
        inventory = transaction.fetch_one(_INVENTORY, (video_id,))
        if inventory is None:
            return
        if source_removed is True:
            transaction.execute(_WITHDRAW, (video_id,))
            return
        args = dict(video_id=video_id, removed=source_removed, **inventory)
        args.update(transaction.fetch_one(_EVIDENCE, (video_id,)))
        args.update(transaction.fetch_one(_BLOCKED, args))
        # DISTINCT native array_agg yields a non-null one-dimensional text list.
        # No array bounds, SQL casts or freshness decisions move into Python.
        conflict = (len(args['types']) > 1 or args['blocked'] is not None
                    or bool(args['types']) and (args['video_type'] is None
                                               or args['video_type'] not in args['types']))
        if conflict is False and not transaction.fetch_one(_PRESENT, (video_id,))['present']:
            return
        transaction.execute(_UPSERT, args)


def detach_review_source(transaction, video_id):
    """BEFORE-delete effect; caller must still perform the guarded deletion."""
    with transaction.borrow():
        acquired = transaction.fetch_one(_DETACH_ADVISORY, (video_id,))['acquired']
        # The original IF NOT keeps SQL NULL distinct from false.
        if acquired is False:
            raise DatabaseServerError('hot_storage_active_work', code='55P03')
        case = transaction.fetch_one(_DETACH_CASE, (video_id,))
        if case is not None:
            transaction.execute(_DETACH, (case['case_id'],))
