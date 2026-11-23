"""Apply fresh automatic type evidence through the existing inventory/queue owner.

No decision is saved as user work. A busy lease keeps its original type until
its completion; source-based read proofs expire independently of this repair.
Callers own the transaction and final channel fence. Each addressed page is 50.
"""
from psycopg.types.json import Jsonb

_NEEDED = """SELECT i.video_id FROM unnest(%s::text[]) requested(video_id)
 JOIN public.video_inventory i USING(video_id)
 CROSS JOIN LATERAL (SELECT c.automatic_type FROM public.video_type_automatic_candidates c
 WHERE c.video_id=requested.video_id OFFSET 0) choice
 WHERE i.video_type IS DISTINCT FROM choice.automatic_type OR i.skeleton_since IS NOT NULL"""
_ADVISORY = """SELECT video_id,pg_try_advisory_xact_lock(hashtextextended(video_id,962106)) AS owned
 FROM (SELECT DISTINCT unnest(%s::text[]) AS video_id ORDER BY video_id) requested"""
_QUEUE = """SELECT video_id FROM public.video_collection_state
 WHERE video_id=ANY(%s::text[]) ORDER BY video_id FOR UPDATE SKIP LOCKED"""
_INVENTORY = """SELECT i.video_id FROM public.video_inventory i
 WHERE i.video_id=ANY(%(ids)s::text[])
 AND NOT EXISTS(SELECT 1 FROM public.video_collection_state q WHERE q.video_id=i.video_id
 AND (NOT(q.video_id=ANY(%(queue_ids)s::text[]))
 OR (q.lease_token IS NOT NULL AND q.lease_expires_at>clock_timestamp())))
 ORDER BY i.video_id FOR UPDATE OF i SKIP LOCKED"""
_CANDIDATES = """SELECT i.video_id,i.channel_id,c.automatic_type AS video_type,
 m.source_published_at::text AS published_at,m.last_present_at::text AS published_acquired_at,
 v.availability_checked_at::text AS availability_checked_at,
 v.metadata_checked_at::text AS metadata_checked_at,
 (q.hydration_needed IS FALSE AND q.stats_due_at IS NOT NULL
 AND v.views IS NOT NULL AND v.checked_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND v.metadata_checked_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND v.availability_status IN ('public','unlisted')) AS hydrated
 FROM unnest(%s::text[]) requested(video_id)
 JOIN public.video_inventory i USING(video_id)
 CROSS JOIN LATERAL (SELECT candidate.* FROM public.video_type_automatic_candidates candidate
 WHERE candidate.video_id=requested.video_id OFFSET 0) c
 JOIN public.video_inventory_memberships m ON m.video_id=i.video_id AND m.channel_id=i.channel_id
 AND m.video_type=c.automatic_type AND m.membership_status='present'
 JOIN public.videos v ON v.video_id=i.video_id AND v.channel_id=i.channel_id
 LEFT JOIN public.video_collection_state q ON q.video_id=i.video_id AND q.channel_id=i.channel_id
 WHERE i.video_type IS DISTINCT FROM c.automatic_type OR i.skeleton_since IS NOT NULL"""
_DEFINITION = "video_id text,channel_id text,video_type text,published_at timestamptz,published_acquired_at timestamptz,metadata_checked_at timestamptz,availability_checked_at timestamptz,hydrated boolean"
_INVENTORY_UPDATE = """UPDATE public.video_inventory i SET video_type=p.video_type,skeleton_since=NULL,
 published_at=p.published_at,published_acquired_at=CASE WHEN p.published_at IS NOT NULL THEN p.published_acquired_at END
 FROM jsonb_to_recordset(%s::jsonb) p("""+_DEFINITION+""")
 WHERE i.video_id=p.video_id AND i.channel_id=p.channel_id RETURNING i.video_id"""
_QUEUE_UPDATE = """UPDATE public.video_collection_state q SET video_type=p.video_type,
 typed_collection_status=CASE WHEN p.video_type='long' THEN NULL WHEN q.availability_first_missing_at IS NOT NULL THEN 'unavailable'
 WHEN p.hydrated THEN 'hydrated' ELSE 'pending' END,
 typed_collection_checked_at=CASE WHEN p.video_type='long' THEN NULL
 WHEN q.availability_first_missing_at IS NOT NULL THEN coalesce(q.typed_collection_checked_at,p.availability_checked_at)
 ELSE p.metadata_checked_at END,
 typed_source_published_at=CASE WHEN p.video_type<>'long' THEN p.published_at END,
 typed_source_published_acquired_at=CASE WHEN p.video_type<>'long' AND p.published_at IS NOT NULL THEN p.published_acquired_at END,
 typed_stream_actual_end_at=NULL,typed_source_stream_end_acquired_at=NULL,
 hydration_needed=CASE WHEN q.availability_first_missing_at IS NOT NULL OR q.availability_resume_observed_at IS NOT NULL
 THEN q.hydration_needed ELSE NOT p.hydrated END,
 next_due_at=CASE WHEN q.availability_first_missing_at IS NOT NULL OR q.availability_resume_observed_at IS NOT NULL
 OR q.paused_reason='availability_retry_expired' THEN q.next_due_at
 WHEN NOT p.hydrated THEN least(q.next_due_at,clock_timestamp()) ELSE q.next_due_at END,
 metadata_due_at=CASE WHEN q.availability_first_missing_at IS NOT NULL OR q.availability_resume_observed_at IS NOT NULL
 OR q.paused_reason='availability_retry_expired' THEN q.metadata_due_at
 WHEN NOT p.hydrated THEN least(q.metadata_due_at,clock_timestamp()) ELSE q.metadata_due_at END,
 paused_reason=CASE WHEN q.paused_reason IN ('typed_identity_not_eligible','inventory_not_long') THEN NULL ELSE q.paused_reason END,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,updated_at=now()
 FROM jsonb_to_recordset(%s::jsonb) p("""+_DEFINITION+""")
 WHERE q.video_id=p.video_id AND q.channel_id=p.channel_id"""


def reconcile_automatic_video_types(transaction, video_ids):
    """Normalize only addressed, currently provable conflicts; never steal leases."""
    ids=sorted(set(video_ids))
    if len(ids)>1000 or any(not isinstance(value,str) or not value for value in ids):
        raise ValueError('automatic_type_reconciliation_input_invalid')
    changed=[]
    with transaction.borrow():
        for start in range(0,len(ids),50):
            page=[row["video_id"] for row in transaction.fetch_all(_NEEDED,(ids[start:start+50],),max_rows=50)]
            if not page:continue
            held=[row['video_id'] for row in transaction.fetch_all(_ADVISORY,(page,),max_rows=50) if row['owned']]
            if not held:continue
            queued=[row['video_id'] for row in transaction.fetch_all(_QUEUE,(held,),max_rows=50)]
            locked=[row['video_id'] for row in transaction.fetch_all(_INVENTORY,dict(ids=held,queue_ids=queued),max_rows=50)]
            if not locked:continue
            plans=transaction.fetch_all(_CANDIDATES,(locked,),max_rows=50)
            if not plans:continue
            payload=Jsonb(plans)
            actual=transaction.returning_all(_INVENTORY_UPDATE,(payload,),max_rows=50)
            if len(actual)!=len(plans):
                from ..base.postgres import DatabaseError
                raise DatabaseError('automatic_type_reconciliation_changed')
            transaction.execute(_QUEUE_UPDATE,(payload,))
            selected=[row['video_id'] for row in actual]
            # Missing queues use the same initializer as ordinary discovery.
            from ..videos.video_queue_initialization import ensure_video_collection_state_in_transaction
            missing=[identity for identity in selected if identity not in queued]
            if missing:ensure_video_collection_state_in_transaction(transaction,missing)
            from .review_effect_batches import refresh_review_cases_batch
            refresh_review_cases_batch(transaction,selected)
            changed.extend(selected)
    return changed
