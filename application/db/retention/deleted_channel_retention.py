"""Channel-trash cleanup; caller owns the maintenance transaction.

The retention cutoff, child-deletion order, per-candidate rollback and final
hidden-generation fence match the retained contract. Pending-restore cleanup
keeps its legacy whole-owner deletes; it is not a new bounded-write guarantee.
"""
from ..base.postgres import DatabaseServerError
from ..channel.channel_membership_cascade import drain_channel_memberships
from ..inventory.inventory_incident_queue_effects import after_availability_incident_write
from ..user_work.user_work_restore import REBASE_PENDING_PARENT_SQL
from .inventory_retention import video_payload

ERRORS = frozenset({'channel_trash_invalid_cleanup', 'channel_collection_scope_lost'})
_HEADER = """WITH args AS (SELECT %s::text AS target, %s::text AS after_id, %s::integer AS limit),
 clock AS MATERIALIZED (SELECT statement_timestamp() AS at)
 SELECT args.*, to_jsonb(at)#>>'{}' AS at,
 to_jsonb(at-interval '696 hours')#>>'{}' AS cutoff FROM args CROSS JOIN clock"""
_PAGE = """SELECT channel_id FROM public.channels_with_control
 WHERE is_hidden AND hidden_at<=%(cutoff)s::timestamptz AND channel_id>coalesce(%(after_id)s::text,'')
 ORDER BY channel_id LIMIT %(limit)s::integer"""
_LOCK = """SELECT c.channel_id,c.user_work_id::text AS owner,s.control_epoch,
 c.title IS NOT NULL OR c.avatar_url IS NOT NULL OR c.views IS NOT NULL OR c.subscribers IS NOT NULL
 OR c.video_count IS NOT NULL OR c.country_code IS NOT NULL OR c.uploads_playlist_id IS NOT NULL
 OR c.channel_created_at IS NOT NULL OR c.initial_inventory_estimate IS NOT NULL AS has_payload
 FROM public.channels c JOIN public.channel_collection_state s ON s.channel_id=c.channel_id
 WHERE c.channel_id=%(channel)s::text AND s.is_hidden AND s.hidden_at<=%(cutoff)s::timestamptz
 FOR UPDATE OF c NOWAIT"""
_BUSY = """SELECT EXISTS(SELECT 1 FROM public.video_collection_state WHERE channel_id=%(channel)s::text AND lease_token IS NOT NULL AND lease_expires_at>%(at)s::timestamptz)
 OR EXISTS(SELECT 1 FROM public.channel_playlist_state WHERE channel_id=%(channel)s::text AND lease_token IS NOT NULL AND lease_expires_at>%(at)s::timestamptz)
 OR EXISTS(SELECT 1 FROM public.operator_jobs WHERE channel_id=%(channel)s::text AND lease_token IS NOT NULL AND lease_expires_at>%(at)s::timestamptz)
 OR EXISTS(SELECT 1 FROM public.video_inventory WHERE channel_id=%(channel)s::text AND skeleton_since IS NULL)
 OR EXISTS(SELECT 1 FROM public.videos v WHERE v.channel_id=%(channel)s::text AND """+video_payload('v')+""")
 OR EXISTS(SELECT 1 FROM public.channel_snapshots WHERE channel_id=%(channel)s::text) AS busy"""
_COLLECTION = """SELECT DISTINCT collection_id FROM public.saved_collection_members
 WHERE user_channel_ref=%(owner)s::uuid ORDER BY collection_id LIMIT 1"""
_MEMBER_DELETE = """DELETE FROM public.saved_collection_members WHERE ctid IN
 (SELECT ctid FROM public.saved_collection_members WHERE collection_id=%(collection)s::uuid
 AND user_channel_ref=%(owner)s::uuid LIMIT %(limit)s::integer)"""
_MEMBER_COUNT = """UPDATE public.saved_objects SET member_count=member_count-%(count)s::integer,
 revision=revision+1,updated_at=%(at)s::timestamptz WHERE kind='collection' AND id=%(collection)s::uuid"""

_CHILD_DELETES = (
    'DELETE FROM public.video_type_review_history WHERE ctid IN (SELECT h.ctid FROM public.video_type_review_history h JOIN public.video_type_review_cases r ON r.case_id=h.case_id WHERE r.user_channel_ref=%(owner)s::uuid LIMIT %(limit)s::integer)',
    'DELETE FROM public.video_type_review_cases r WHERE r.ctid IN (SELECT r2.ctid FROM public.video_type_review_cases r2 WHERE r2.user_channel_ref=%(owner)s::uuid AND NOT EXISTS(SELECT 1 FROM public.video_type_review_history h WHERE h.case_id=r2.case_id) LIMIT %(limit)s::integer)',
    "DELETE FROM public.saved_objects WHERE ctid IN (SELECT ctid FROM public.saved_objects WHERE kind='view' AND channel_id=%(channel)s::text LIMIT %(limit)s::integer)",
    'DELETE FROM public.operator_job_targets WHERE ctid IN (SELECT t.ctid FROM public.operator_job_targets t JOIN public.operator_jobs j ON j.id=t.job_id WHERE j.channel_id=%(channel)s::text LIMIT %(limit)s::integer)',
    'DELETE FROM operator_private.operator_job_retry_requests WHERE ctid IN (SELECT r.ctid FROM operator_private.operator_job_retry_requests r JOIN public.operator_jobs j ON j.id=r.job_id WHERE j.channel_id=%(channel)s::text LIMIT %(limit)s::integer)',
    'DELETE FROM public.operator_jobs j WHERE j.ctid IN (SELECT j2.ctid FROM public.operator_jobs j2 WHERE j2.channel_id=%(channel)s::text AND NOT EXISTS(SELECT 1 FROM public.operator_job_targets t WHERE t.job_id=j2.id) AND NOT EXISTS(SELECT 1 FROM operator_private.operator_job_retry_requests r WHERE r.job_id=j2.id) LIMIT %(limit)s::integer)',
    'DELETE FROM public.video_availability_events WHERE ctid IN (SELECT ctid FROM public.video_availability_events WHERE channel_id=%(channel)s::text LIMIT %(limit)s::integer)',
    'DELETE FROM public.video_availability_incidents WHERE ctid IN (SELECT ctid FROM public.video_availability_incidents WHERE channel_id=%(channel)s::text LIMIT %(limit)s::integer)',
    'DELETE FROM public.channel_label_assignments WHERE ctid IN (SELECT ctid FROM public.channel_label_assignments WHERE channel_id=%(channel)s::text LIMIT %(limit)s::integer)',
    'DELETE FROM public.channel_playlist_state WHERE channel_id=%(channel)s::text',
    'DELETE FROM public.typed_inventory_receipts WHERE ctid IN (SELECT r.ctid FROM public.typed_inventory_receipts r JOIN public.channel_video_inventory_scans s ON s.scan_id=r.scan_id WHERE s.channel_id=%(channel)s::text LIMIT %(limit)s::integer)',
    'DELETE FROM public.channel_video_inventory_scans s WHERE s.ctid IN (SELECT s2.ctid FROM public.channel_video_inventory_scans s2 WHERE s2.channel_id=%(channel)s::text AND NOT EXISTS(SELECT 1 FROM public.typed_inventory_receipts r WHERE r.scan_id=s2.scan_id) LIMIT %(limit)s::integer)',
    # Expiry keeps a video as an identity-only inventory row (and a video row
    # without API data) so user decisions stay bound to it; no other target
    # removes them. Once nothing refers to a video, it goes with its channel;
    # its queue and read-model rows follow the inventory row.
    'DELETE FROM public.videos v WHERE v.ctid IN (SELECT v2.ctid FROM public.videos v2 WHERE v2.channel_id=%(channel)s::text AND NOT '+video_payload('v2')+' AND NOT EXISTS(SELECT 1 FROM public.video_snapshots s WHERE s.video_id=v2.video_id) LIMIT %(limit)s::integer)',
    'DELETE FROM public.video_inventory i WHERE i.ctid IN (SELECT i2.ctid FROM public.video_inventory i2 WHERE i2.channel_id=%(channel)s::text AND i2.skeleton_since IS NOT NULL'
    ' AND NOT EXISTS(SELECT 1 FROM public.videos v WHERE v.video_id=i2.video_id) AND NOT EXISTS(SELECT 1 FROM public.video_snapshots s WHERE s.video_id=i2.video_id)'
    ' AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=i2.video_id) AND NOT EXISTS(SELECT 1 FROM public.typed_video_collection_receipts r WHERE r.video_id=i2.video_id)'
    ' AND NOT EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=i2.video_id) LIMIT %(limit)s::integer)',
)

_INCIDENT_DELETE = _CHILD_DELETES[7] + ' RETURNING to_jsonb(video_availability_incidents)::text AS old_text,restored_at IS NULL AS was_open'

_CHILDREN = """SELECT EXISTS(SELECT 1 FROM public.saved_collection_members WHERE user_channel_ref=%(owner)s::uuid)
 OR EXISTS(SELECT 1 FROM public.video_type_review_cases WHERE user_channel_ref=%(owner)s::uuid)
 OR EXISTS(SELECT 1 FROM public.saved_objects WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.operator_jobs WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.video_availability_events WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.video_availability_incidents WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.channel_label_assignments WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.channel_playlist_state WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.channel_video_inventory_scans WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.video_collection_state WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.video_inventory WHERE channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.videos WHERE channel_id=%(channel)s::text) AS remaining"""
_PARENT_CHANNELS = """UPDATE public.channels SET parent_channel_id=NULL WHERE channel_id IN
 (SELECT channel_id FROM public.channels WHERE parent_channel_id=%(channel)s::text LIMIT %(limit)s::integer)"""
_PARENT_PENDING = "UPDATE public.user_channel_restore_items SET parent_ref=NULL," + REBASE_PENDING_PARENT_SQL + """
 WHERE id IN(SELECT id FROM public.user_channel_restore_items WHERE parent_ref=%(owner)s::uuid LIMIT %(limit)s::integer)"""
_PARENTS = """SELECT EXISTS(SELECT 1 FROM public.channels WHERE parent_channel_id=%(channel)s::text)
 OR EXISTS(SELECT 1 FROM public.user_channel_restore_items WHERE parent_ref=%(owner)s::uuid) AS remaining"""
_PENDING_PAGE = """SELECT id FROM public.user_channel_restore_items
 WHERE is_hidden AND hidden_at<=%(cutoff)s::timestamptz ORDER BY id LIMIT %(limit)s::integer"""
_PENDING_LOCK = """SELECT id::text AS id FROM public.user_channel_restore_items
 WHERE id=%(owner)s::uuid AND is_hidden AND hidden_at<=%(cutoff)s::timestamptz FOR UPDATE NOWAIT"""
# One statement freezes the same ID snapshot as the retained FOR SELECT.
# The enforced user_work_backup_limits ceiling is 100000 total members, so it
# also bounds distinct collection IDs, including deleted collection headers.
# No LIMIT truncates the snapshot; corrupt over-limit state fails closed.
_PENDING_COLLECTIONS = """SELECT DISTINCT collection_id FROM public.saved_collection_members
 WHERE restore_owner_ref=%(owner)s::uuid ORDER BY collection_id"""
_FENCE = """SELECT channel_id,is_hidden AND hidden_at IS NOT NULL
 AND hidden_at<=%(cutoff)s::timestamptz AND control_epoch IS NOT DISTINCT FROM %(epoch)s::bigint AS valid
 FROM public.channel_collection_state WHERE channel_id=%(channel_id)s::text FOR SHARE"""


def _lock_collection(tx, params):
    tx.fetch_one("SELECT id FROM public.saved_objects WHERE kind='collection' AND id=%(collection)s::uuid FOR UPDATE",params)


def _children(tx, params):
    removed=0
    collection=tx.fetch_one(_COLLECTION,params)
    if collection:
        p={**params,'collection':collection['collection_id']}
        _lock_collection(tx,p)
        n=tx.execute(_MEMBER_DELETE,p)
        tx.execute(_MEMBER_COUNT,{**p,'count':n})
        removed+=n
    for statement in _CHILD_DELETES:
        if statement == _CHILD_DELETES[7]:
            receipts=tx.returning_all(_INCIDENT_DELETE,params,max_rows=params['limit'])
            removed+=len(receipts)
            for row in receipts:
                # Closed incident DELETE returns before every queue read/write.
                if row['was_open']:
                    after_availability_incident_write(tx,operation='delete',old_text=row['old_text'])
        else:
            removed+=tx.execute(statement,params)
    if tx.fetch_one(_CHILDREN,params)['remaining']:
        return removed,False
    tx.execute(_PARENT_CHANNELS,params)
    tx.execute(_PARENT_PENDING,params)
    return removed,not tx.fetch_one(_PARENTS,params)['remaining']


def _pending(tx,params):
    if tx.fetch_one(_PENDING_LOCK,params) is None:
        return 0
    removed=0
    rows=tx.fetch_all(_PENDING_COLLECTIONS,params,max_rows=100000)
    for item in rows:
        p={**params,'collection':item['collection_id']}
        _lock_collection(tx,p)
        n=tx.execute("DELETE FROM public.saved_collection_members WHERE collection_id=%(collection)s::uuid AND restore_owner_ref=%(owner)s::uuid",p)
        tx.execute(_MEMBER_COUNT,{**p,'count':n})
        removed+=n
    removed+=tx.execute("DELETE FROM public.video_type_review_history h USING public.video_type_review_cases r WHERE r.case_id=h.case_id AND r.restore_owner_ref=%(owner)s::uuid",params)
    removed+=tx.execute("DELETE FROM public.video_type_review_cases WHERE restore_owner_ref=%(owner)s::uuid",params)
    tx.execute("UPDATE public.user_channel_restore_items SET parent_ref=NULL," + REBASE_PENDING_PARENT_SQL +
        " WHERE parent_ref=%(owner)s::uuid",params)
    tx.execute("DELETE FROM public.user_channel_restore_items WHERE id=%(owner)s::uuid",params)
    return removed+1


def cleanup_hot_deleted_channels(transaction, *, target, after_id=None, limit=100, references=None):
    tx=transaction
    with tx.borrow():
        p=tx.fetch_one(_HEADER,(target,after_id,limit))
        # SQL NULL target falls through the original IF when the limit is valid.
        if (p['target'] is not None and p['target']!='deleted_channels') or p['limit'] is None or not 1<=p['limit']<=100:
            raise DatabaseServerError('channel_trash_invalid_cleanup',code='P0001')
        if references is None:
            rows=tx.fetch_all(_PAGE,p,max_rows=p['limit'])
        else:
            rows=tx.fetch_all('SELECT channel_id FROM public.channels_with_control WHERE is_hidden AND hidden_at<=%(cutoff)s::timestamptz AND channel_id=ANY(%(ids)s::text[]) ORDER BY channel_id',
                dict(cutoff=p['cutoff'],ids=[key[1] for key in references if key[0]=='channel']),max_rows=p['limit'])
        removed=deferred=0
        progressing=False
        unresolved=[]
        pending=[]
        scopes=[]
        delete_ids=[]
        for candidate in rows:
            scope=None
            try:
                with tx.savepoint():
                    params={**p,'channel':candidate['channel_id']}
                    row=tx.fetch_one(_LOCK,params)
                    if row is None:
                        continue
                    scope={'channel_id':row['channel_id'],'epoch':row['control_epoch']}
                    params['owner']=row['owner']
                    if tx.fetch_one(_BUSY,params)['busy'] or row['has_payload']:
                        count,done=0,False
                    else:
                        count,done=_children(tx,params)
                # Append only after a successful savepoint. A final fence failure
                # is outside all row catches and rolls back every earlier child.
                scopes.append(scope)
                removed+=count
                if done:
                    delete_ids.append(row['channel_id'])
                else:
                    deferred+=1
                    progressing=progressing or count>0
                    unresolved.append(dict(target=target,key=['channel',candidate['channel_id']]))
            except DatabaseServerError as error:
                if error.code not in ('55P03','40P01'):
                    raise
                deferred+=1
                unresolved.append(dict(target=target,key=['channel',candidate['channel_id']]))
        if p['after_id'] is None:
            if references is None:
                pending=tx.fetch_all(_PENDING_PAGE,p,max_rows=p['limit'])
            else:
                pending=tx.fetch_all('SELECT id::text AS id FROM public.user_channel_restore_items WHERE is_hidden AND hidden_at<=%(cutoff)s::timestamptz AND id=ANY(%(ids)s::uuid[]) ORDER BY id',
                    dict(cutoff=p['cutoff'],ids=[key[1] for key in references if key[0]=='restore']),max_rows=p['limit'])
            for item in pending:
                try:
                    with tx.savepoint():
                        count=_pending(tx,{**p,'owner':item['id']})
                    removed+=count
                except DatabaseServerError as error:
                    if error.code not in ('55P03','40P01'):
                        raise
                    deferred+=1
                    unresolved.append(dict(target=target,key=['restore',str(item['id'])]))
        # Candidate order is the database's channel_id order. Validate each
        # shared fence immediately, so an earlier scope mismatch cannot be
        # masked by a later row's lock timeout. No row-local catch covers this.
        for scope in scopes:
            held=tx.fetch_one(_FENCE,{**scope,'cutoff':p['cutoff']})
            if held is None or held['valid'] is not True:
                raise DatabaseServerError('channel_collection_scope_lost',code='P0001')
        drain_channel_memberships(tx,delete_ids)
        removed+=tx.execute('DELETE FROM public.channels WHERE channel_id=ANY(%s::text[])',(delete_ids,))
        # A removal that advanced but is not finished keeps the page open without
        # a cursor: the pass reads it again at once, so a large catalogue is
        # removed within one pass instead of one page of children per pass.
        complete=references is not None or (len(rows)<p['limit'] and not progressing)
        cursor=rows[-1]['channel_id'] if not complete and len(rows)==p['limit'] else None
        return dict(target=p['target'],as_of=p['at'],cutoff=p['cutoff'],scanned=len(rows)+(len(pending) if references is not None else 0),deleted=removed,scrubbed=0,
                    deferred=deferred,complete=complete,next_cursor=cursor,
                    oldest_remaining_expired_source_at=None,invalid_clock_remaining=0,
                    # Every selected row has proved canonical removal expiry, even
                    # when its later lock is busy. Do not invent a source clock.
                    confirmed_expired_remaining=deferred,unknown_expiry_remaining=0,priority_complete=len(unresolved)<=100,priority_references=unresolved[:100])
