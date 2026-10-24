"""Complete the original ABSENT statement before its bounded review effects.

The page owner holds the exact playlist and current scan. A first locking pass
freezes all matching source rows; a second ordinary cursor declares the native
OLD snapshot before the original single UPDATE. Only actual NEW rows drive the
normal-review branch. The fixed status change rules out withdrawal/restoration.
"""
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseError
from .review_effect_batches import refresh_review_cases_batch

_KEYS=frozenset(('channel_id','video_type','scan_id','scan_boundary_at','source_checked_at'))
_ROW_KEYS=frozenset(('video_id','channel_id','video_type','old_text'))


def _statements():
    # Retain the exact native predicate, casts, SET targets and UPDATE receipt.
    from .inventory_scan_pages import _ABSENT
    predicate=_ABSENT.split('    WHERE ',1)[1]
    lock='SELECT video_id,channel_id,video_type FROM public.video_inventory_memberships WHERE '+predicate+' FOR NO KEY UPDATE'
    old='SELECT video_id,channel_id,video_type,to_jsonb(membership)::text AS old_text FROM public.video_inventory_memberships membership WHERE '+predicate
    return lock,old,_ABSENT


_NEW="""SELECT source.video_id,source.channel_id,source.video_type,to_jsonb(membership)::text AS new_text,
 to_jsonb(membership)=source.old_text::jsonb || jsonb_build_object(
 'membership_status','absent','absent_since',%(source_checked_at)s::timestamptz,
 'absence_confirmed_by_scan_id',%(scan_id)s::uuid,'updated_at',now()) AS exact
 FROM jsonb_to_recordset(%(rows)s::jsonb) source(video_id text,channel_id text,video_type text,old_text text)
 JOIN public.video_inventory_memberships membership
 ON membership.video_id=source.video_id AND membership.channel_id=source.channel_id AND membership.video_type=source.video_type"""


def mark_absent_memberships(transaction,arguments):
    """Only the existing page owner may supply its held playlist/scan inputs."""
    with transaction.borrow():
        parameters={key:arguments[key] for key in _KEYS}
        affected=0
        with transaction.inventory_absence_changes(parameters) as batches:
            for previous in batches:
                identities=[]
                for row in previous:
                    if (type(row) is not dict or set(row)!=_ROW_KEYS
                            or any(type(value) is not str for value in row.values())
                            or row['channel_id']!=parameters['channel_id'] or row['video_type']!=parameters['video_type']):
                        raise DatabaseError('database_result_shape_invalid')
                    identities.append((row['video_id'],row['channel_id'],row['video_type']))
                if len(set(identities))!=len(identities):
                    raise DatabaseError('database_result_shape_invalid')
                current=transaction.fetch_all(_NEW,{**parameters,'rows':Jsonb(previous)},max_rows=50)
                actual={}
                for row in current:
                    if (type(row) is not dict or set(row)!=set(('video_id','channel_id','video_type','new_text','exact'))
                            or any(type(row[key]) is not str for key in ('video_id','channel_id','video_type','new_text'))
                            or row['exact'] is not True):
                        raise DatabaseError('database_result_shape_invalid')
                    key=(row['video_id'],row['channel_id'],row['video_type'])
                    if key in actual:
                        raise DatabaseError('database_result_shape_invalid')
                    actual[key]=row
                if set(actual)!=set(identities):
                    raise DatabaseError('database_result_shape_invalid')
                # The exact full native NEW equality above proves present->absent,
                # unchanged identity, and no other changed field. The original
                # membership hook therefore skips both erasure and restoration.
                refresh_review_cases_batch(transaction,[video_id for video_id,_,_ in identities])
                affected+=len(previous)
        return affected
