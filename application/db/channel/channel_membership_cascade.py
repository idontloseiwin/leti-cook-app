"""Explicit membership cascade under final hidden-channel parent row locks.

The original owner already proved no canonical inventory/video/snapshot rows and
finished its child mutations and hidden-epoch fences. Parent FOR UPDATE excludes
new FK references; a fixed snapshot cursor follows concurrent moved keys using
native FOR UPDATE/EPQ. Actual DELETE receipts alone drive withdrawal. None of
these child rows is added to the original channel-delete receipt.
"""
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseError

_MEMBERS = """SELECT video_id,channel_id,video_type FROM public.video_inventory_memberships
 WHERE channel_id=ANY(%s::text[]) FOR UPDATE"""
_DELETE = """DELETE FROM public.video_inventory_memberships membership
 USING jsonb_to_recordset(%s::jsonb) held(video_id text,channel_id text,video_type text)
 WHERE membership.video_id=held.video_id AND membership.channel_id=held.channel_id
 AND membership.video_type=held.video_type AND membership.channel_id=ANY(%s::text[])
 RETURNING membership.video_id,to_jsonb(membership)::text AS old_text"""
_ADVISORY = """SELECT pg_advisory_xact_lock(hashtextextended(input.video_id,962106)) AS acquired
 FROM unnest(%s::text[]) WITH ORDINALITY input(video_id,ordinality) ORDER BY input.ordinality"""
_WITHDRAW = """UPDATE public.video_type_review_cases c SET observed_canonical_type=NULL,
 observed_types=ARRAY[]::text[],observed_channels=ARRAY[]::text[],blocked_reason=NULL,evidence_expires_at=NULL,
 revision=revision+CASE WHEN observed_canonical_type IS NOT NULL OR cardinality(observed_types)>0
 OR cardinality(observed_channels)>0 OR blocked_reason IS NOT NULL THEN 1 ELSE 0 END
 WHERE c.video_id=ANY(%s::text[]) AND EXISTS(SELECT 1 FROM public.video_inventory i WHERE i.video_id=c.video_id)"""
_REMAINS = 'SELECT EXISTS(SELECT 1 FROM public.video_inventory_memberships WHERE channel_id=ANY(%s::text[])) AS present'


def drain_channel_memberships(transaction,channel_ids):
    """A fixed snapshot with <=50-row buffers and the original shrinking budget."""
    with transaction.borrow():
        if not channel_ids:
            return
        expected=0
        # Lock every selected source row before any review/advisory effect. The
        # parent FOR UPDATE blocks new/moved-in channel FK references; this set
        # therefore stays fixed after native EPQ has followed moved-away keys.
        with transaction.channel_membership_cascade_rows(channel_ids=channel_ids) as batches:
            for held in batches:
                for row in held:
                    if (set(row)!={'video_id','channel_id','video_type'} or
                            any(type(value) is not str for value in row.values()) or row['channel_id'] not in channel_ids):
                        raise DatabaseError('database_result_shape_invalid')
                expected+=len(held)
        deleted=0
        with transaction.channel_membership_cascade_rows(channel_ids=channel_ids) as batches:
            for held in batches:
                for row in held:
                    if (set(row)!={'video_id','channel_id','video_type'} or
                            any(type(value) is not str for value in row.values()) or row['channel_id'] not in channel_ids):
                        raise DatabaseError('database_result_shape_invalid')
                receipts=transaction.returning_all(_DELETE,(Jsonb(held),channel_ids),max_rows=50)
                if len(receipts)!=len(held) or any(set(row)!={'video_id','old_text'}
                        or type(row['video_id']) is not str or type(row['old_text']) is not str for row in receipts):
                    raise DatabaseError('database_result_shape_invalid')
                deleted+=len(receipts)
                # Each original DELETE event is source_removed=True: it ignores
                # remaining membership rows, clears only observation fields and
                # increments revision only once. Repeated video IDs commute.
                ids=list(dict.fromkeys(row['video_id'] for row in receipts))
                transaction.fetch_all(_ADVISORY,(ids,),max_rows=50)
                transaction.execute(_WITHDRAW,(ids,))
        if deleted!=expected or transaction.fetch_one(_REMAINS,(channel_ids,))['present']:
            raise DatabaseError('database_result_shape_invalid')
