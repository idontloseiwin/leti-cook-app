"""PG17 membership UPSERT capture, not a portable action API.

Native ON CONFLICT arbitration admits and locks the actual conflict row. Its
same-value updated_at write preserves the domain tuple and does not target the
scan BEFORE UPDATE hook. xmax=0 distinguishes the INSERT event from a
same-value conflict UPDATE. The runtime connection guard restricts this
implementation to PostgreSQL major17; a newer major requires renewed
compatibility review. There is no domain sentinel.
"""
from ..base.postgres import DatabaseError
from .page_effect_batches import after_page_membership_changes

_COLUMNS=('video_id','channel_id','video_type','playlist_id','source_published_at','membership_status',
 'evidence_source','first_seen_at','last_seen_at','last_present_at','absent_since','first_seen_scan_id',
 'last_seen_scan_id','absence_confirmed_by_scan_id','updated_at')
# Imported lazily below: the public page owner imports this helper at dispatch.

def _statements():
    from .inventory_scan_pages import _UPSERT_MEMBERSHIPS
    before,conflict=_UPSERT_MEMBERSHIPS.split(' DO UPDATE SET ',1)
    targets,predicate=conflict.rsplit('    WHERE ',1)
    source=before.split('    SELECT ',1)[1].rsplit('    ON CONFLICT',1)[0]
    acquire=before+' DO UPDATE SET updated_at=membership.updated_at\n    WHERE '+predicate
    acquire+=' RETURNING membership.video_id,(membership.xmax=0) AS inserted,to_jsonb(membership)::text AS value'
    finalize='UPDATE public.video_inventory_memberships AS membership SET '+targets
    finalize+=' FROM (SELECT '+source+') AS excluded('+','.join(_COLUMNS)+')'
    finalize+=' WHERE membership.video_id=excluded.video_id AND membership.channel_id=excluded.channel_id'
    finalize+=' AND membership.video_type=excluded.video_type AND membership.video_id=ANY(%(effect_conflict_ids)s::text[])'
    finalize+=' AND ('+predicate.strip()+')'
    finalize+=' RETURNING membership.video_id,to_jsonb(membership)::text AS value'
    return acquire,finalize


def observe_page_memberships(transaction,parameters):
    """At most50 unique input videos, already advisory-owned by page settlement."""
    with transaction.borrow():
        acquire,finalize=_statements()
        admitted=transaction.returning_all(acquire,parameters,max_rows=50)
        seen=set()
        conflicts=[]
        for row in admitted:
            if (set(row)!={'video_id','inserted','value'} or type(row['video_id']) is not str
                    or type(row['inserted']) is not bool or type(row['value']) is not str or row['video_id'] in seen):
                raise DatabaseError('database_result_shape_invalid')
            seen.add(row['video_id'])
            if not row['inserted']:
                conflicts.append(row['video_id'])
        changed={}
        if conflicts:
            rows=transaction.returning_all(finalize,dict(parameters,effect_conflict_ids=conflicts),max_rows=50)
            for row in rows:
                if (set(row)!={'video_id','value'} or row['video_id'] not in conflicts
                        or row['video_id'] in changed or type(row['value']) is not str):
                    raise DatabaseError('database_result_shape_invalid')
                changed[row['video_id']]=row['value']
            if set(changed)!=set(conflicts):
                raise DatabaseError('database_result_shape_invalid')
        # RETURNING keeps acquisition event order; never sort or rescan the table.
        events=[]
        for row in admitted:
            events.append(dict(operation='insert' if row['inserted'] else 'update',
                old_text=None if row['inserted'] else row['value'],new_text=row['value'] if row['inserted'] else changed[row['video_id']]))
        if events:after_page_membership_changes(transaction,events)
