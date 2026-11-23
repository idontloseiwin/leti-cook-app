"""Atomic Saved lifecycle and membership protocols used by repositories.

Creation uses a visibility-only final control-row fence, including manual-hold
channels; its reviewed column grants do not require accepted-owner eligibility.
Collection creation has no control-row fence. Helpers may borrow a caller
transaction; no write or ambiguous acknowledgement is retried. Owner-binding
and backup-limit triggers remain intrinsic database integrity guards.
"""
from __future__ import annotations

from contextlib import contextmanager
import re

from psycopg.types.json import Jsonb

from ..base.postgres import DatabaseServerError, Operation

_FINGERPRINT=re.compile(r'[0-9a-f]{64}\Z')
_VIDEO=re.compile(r'[A-Za-z0-9_-]{11}\Z')
_CHANNEL=re.compile(r'UC[A-Za-z0-9_-]{22}\Z')
_REQUEST=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')
_ERRORS=frozenset({'user_work_limit_reached','channel_collection_scope_lost'})
_LOCK_OBJECT='SELECT to_jsonb(o) AS object FROM public.saved_objects o WHERE kind=%s AND id=%s::uuid FOR UPDATE OF o'
_INSERT_OBJECT='''INSERT INTO public.saved_objects AS o(kind,id,name,channel_id,conditions,create_fingerprint)
VALUES(%s,%s::uuid,%s,%s,%s::jsonb,%s) RETURNING to_jsonb(o) AS object'''
_PREPARE_OBJECT='''WITH args AS (
 SELECT %s::text AS kind,%s::uuid AS id,%s::text AS name,%s::bigint AS expected_revision,
        %s::text AS fingerprint,%s::text AS channel_id,%s::jsonb AS conditions
) SELECT args.*, name=btrim(name) AND name !~ '[[:cntrl:]]' AS name_clean,
         octet_length(conditions::text) AS conditions_bytes,conditions IS NULL AS conditions_sql_null
FROM args'''
_PREPARE_MEMBERS='''SELECT %s::uuid AS id,%s::uuid AS request_id,%s::text AS action,
 %s::text[] AS video_ids,%s::bigint AS expected_revision,%s::text AS fingerprint'''
# A member saved before its video was tracked has no stored owner; every
# read derives one from the inventory (research_sql scope_members, same
# expression). A write of the collection stores that derived owner, so a video
# that later moves to another channel keeps its member where it was. The owner
# trigger sees the same inventory; a restore-pending owner is never touched.
_BIND_DERIVED_OWNERS='''UPDATE public.saved_collection_members AS m SET user_channel_ref=derived.owner
FROM (SELECT d.video_id,(SELECT u.user_work_id FROM readable.video_inventory k
 JOIN readable.channel_user_work u ON u.channel_id=k.channel_id WHERE k.video_id=d.video_id LIMIT 1) AS owner
 FROM public.saved_collection_members d
 WHERE d.collection_id=%s::uuid AND d.user_channel_ref IS NULL AND d.restore_owner_ref IS NULL) AS derived
WHERE m.collection_id=%s::uuid AND m.video_id=derived.video_id AND derived.owner IS NOT NULL
 AND m.user_channel_ref IS NULL AND m.restore_owner_ref IS NULL'''


# Fixed public finalization writes the object and command receipt together.
_FINALIZE_CHANGED_MEMBERS_KEYS = frozenset({
    'id', 'request_id', 'fingerprint', 'action', 'requested_count',
    'changed_count', 'delta', 'now',
})
_FINALIZE_CHANGED_MEMBERS = """WITH changed_object AS (
 UPDATE public.saved_objects AS o
 SET revision=revision+1,member_count=member_count+%(delta)s,updated_at=%(now)s::timestamptz
 WHERE kind='collection' AND id=%(id)s::uuid
 RETURNING to_jsonb(o) AS object,o.revision
), written_receipt AS (
 INSERT INTO public.saved_collection_commands(collection_id,request_id,request_fingerprint,action,
    requested_count,changed_count,applied_revision)
 SELECT %(id)s::uuid,%(request_id)s::uuid,%(fingerprint)s,%(action)s,
    %(requested_count)s,%(changed_count)s,changed_object.revision FROM changed_object
 RETURNING collection_id
)
SELECT changed_object.object FROM changed_object CROSS JOIN written_receipt LIMIT 2"""


def _reject(code='22023'):
    # Retained validation messages are deliberately not public tokens.
    raise DatabaseServerError('database_operation_failed',code=code)


def _matches(pattern,value):
    return isinstance(value,str) and pattern.fullmatch(value) is not None


def _json(value):
    return value if value is None or isinstance(value,Jsonb) else Jsonb(value)


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():
            yield transaction
    else:
        # Reviewed SQL plus owner-binding/backup-limit triggers never change
        # statement_timeout. Borrowed callers must make their own opt-in.
        with database.transaction(Operation(name,statement_timeout_ms=60_000,
            total_timeout_ms=60_000,error_codes=_ERRORS,cache_statement_timeout=True)) as owned:
            yield owned


def _prepare_object(transaction,kind,object_id,*,name=None,expected_revision=None,
                    fingerprint=None,channel_id=None,conditions=None):
    # Native casts preserve UUID/bigint dispatch failures and PostgreSQL's
    # printed JSONB byte bound. btrim/POSIX control classification stays native.
    return transaction.fetch_one(_PREPARE_OBJECT,
        (kind,object_id,name,expected_revision,fingerprint,channel_id,_json(conditions)))


def _validate_identity(args):
    if args['kind'] not in {'view','collection'} or args['id'] is None:_reject()


def _validate_name(args):
    if not isinstance(args['name'],str) or not 1<=len(args['name'])<=120 or args['name_clean'] is not True:_reject()


def _validate_revision(args):
    if args['expected_revision'] is None or args['expected_revision']<1:_reject()


def _object(transaction,kind,object_id):
    row=transaction.fetch_one(_LOCK_OBJECT,(kind,object_id))
    return None if row is None else row['object']


def _public(row):
    result=dict(row)
    result.pop('create_fingerprint',None)
    return result


def create_saved_object(database, *, kind, object_id, name, fingerprint,
                        channel_id=None, conditions=None, transaction=None):
    """Advisory identity serialization, immutable fingerprint and final fence."""
    with _transaction(database,transaction,'create_saved_object') as tx:
        args=_prepare_object(tx,kind,object_id,name=name,fingerprint=fingerprint,channel_id=channel_id,conditions=conditions)
        _validate_identity(args)
        _validate_name(args)
        if not _matches(_FINGERPRINT,args['fingerprint']):_reject()
        if args['kind']=='collection':
            if args['channel_id'] is not None or not args['conditions_sql_null']:_reject()
        elif ((args['channel_id'] is not None and not _matches(_CHANNEL,args['channel_id'])) or args['conditions_sql_null']
              or not isinstance(args['conditions'],dict) or args['conditions_bytes']>32768):_reject()
        tx.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended('saved-' || %s::text || ':' || %s::uuid::text,0)) AS locked",
                     (args['kind'],args['id']))
        row=_object(tx,args['kind'],args['id'])
        if row is not None:
            if row['deleted_at'] is not None:return {'outcome':'gone'}
            if row['create_fingerprint']!=args['fingerprint']:return {'outcome':'conflict'}
        else:
            try:
                with tx.savepoint():
                    epoch=None
                    if args['kind']=='view' and args['channel_id'] is not None:
                        scope=tx.fetch_one('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s AND NOT is_hidden',
                                          (args['channel_id'],))
                        if scope is None:return {'outcome':'channel_unavailable'}
                        epoch=scope['control_epoch']
                    row=tx.returning_one(_INSERT_OBJECT,(args['kind'],args['id'],args['name'],args['channel_id'],
                                                        _json(conditions),args['fingerprint']))['object']
                    if args['kind']=='view' and args['channel_id'] is not None:
                        # Child writes first; hold visibility/epoch through COMMIT.
                        scope=tx.fetch_one('SELECT is_hidden,control_epoch FROM public.channel_collection_state WHERE channel_id=%s FOR SHARE',
                                          (args['channel_id'],))
                        if scope is None or scope['is_hidden'] or scope['control_epoch']!=epoch:
                            raise DatabaseServerError('channel_collection_scope_lost',code='P0001')
            except DatabaseServerError as error:
                if error.code=='P0001' and error.message=='channel_collection_scope_lost':
                    return {'outcome':'channel_unavailable'}
                raise
        return {'outcome':'ready','object':_public(row)}


def rename_saved_object(database, *,kind,object_id,name,expected_revision,transaction=None):
    with _transaction(database,transaction,'rename_saved_object') as tx:
        args=_prepare_object(tx,kind,object_id,name=name,expected_revision=expected_revision)
        _validate_identity(args)
        _validate_name(args)
        _validate_revision(args)
        row=_object(tx,args['kind'],args['id'])
        if row is None:return {'outcome':'not_found'}
        if row['deleted_at'] is not None:return {'outcome':'gone'}
        if row['revision']!=args['expected_revision']:return {'outcome':'conflict'}
        row=tx.returning_one('UPDATE public.saved_objects AS o SET name=%s,revision=revision+1,updated_at=clock_timestamp() '
            'WHERE kind=%s AND id=%s::uuid RETURNING to_jsonb(o) AS object',
            (args['name'],args['kind'],args['id']))['object']
        return {'outcome':'ready','object':_public(row)}


def update_research_definition(database, *,object_id,name,conditions,expected_revision):
    """CAS edit of an existing research; exact uncertain replay is idempotent.

    The channel binding is immutable. A later edit makes an old replay conflict;
    it never overwrites newer work. Existing user-work byte guards own admission.
    """
    with _transaction(database,None,'update_research_definition') as tx:
        args=_prepare_object(tx,'view',object_id,name=name,conditions=conditions,expected_revision=expected_revision)
        _validate_identity(args);_validate_name(args);_validate_revision(args)
        if not isinstance(conditions,dict) or conditions.get('version') not in (3,4) or args['conditions_bytes']>32768:_reject()
        row=_object(tx,'view',object_id)
        if row is None:return {'outcome':'not_found'}
        if row['deleted_at'] is not None:return {'outcome':'gone'}
        # A study stays a study and a canvas a canvas: a converted study is saved as a new item.
        if row['conditions'].get('version')!=conditions['version']:return {'outcome':'conflict'}
        if conditions['version']==3 and (conditions['query']['scope']['kind']=='channel') != (row['channel_id'] is not None):return {'outcome':'conflict'}
        if row['revision']==expected_revision+1 and row['name']==name and row['conditions']==conditions:
            return {'outcome':'ready','object':_public(row)}
        if row['revision']!=expected_revision:return {'outcome':'conflict'}
        result=tx.returning_one('UPDATE public.saved_objects o SET name=%s,conditions=%s::jsonb,revision=revision+1,updated_at=clock_timestamp() '
            "WHERE kind='view' AND id=%s::uuid RETURNING to_jsonb(o) AS object",(name,Jsonb(conditions),object_id))['object']
        return {'outcome':'ready','object':_public(result)}


def delete_saved_object(database, *,kind,object_id,expected_revision,transaction=None):
    with _transaction(database,transaction,'delete_saved_object') as tx:
        args=_prepare_object(tx,kind,object_id,expected_revision=expected_revision)
        _validate_identity(args)
        _validate_revision(args)
        row=_object(tx,args['kind'],args['id'])
        if row is None:return {'outcome':'not_found'}
        if row['deleted_at'] is not None:
            # The retained bigint expression can overflow before comparison.
            if args['expected_revision']==9223372036854775807:_reject('22003')
            return {'outcome':'deleted','id':str(args['id'])} if row['revision']==args['expected_revision']+1 else {'outcome':'conflict'}
        if row['revision']!=args['expected_revision']:return {'outcome':'conflict'}
        if args['kind']=='collection':
            tx.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s::uuid',(args['id'],))
        # Two clock_timestamp calls intentionally match the retained update.
        tx.execute('UPDATE public.saved_objects SET name=NULL,channel_id=NULL,conditions=NULL,member_count=0,'
            'revision=revision+1,deleted_at=clock_timestamp(),updated_at=clock_timestamp() WHERE kind=%s AND id=%s::uuid',
            (args['kind'],args['id']))
        return {'outcome':'deleted','id':str(args['id'])}


def _flatten_array(value):
    if value is None:return None
    result=[]
    pending=list(reversed(value))
    while pending:
        item=pending.pop()
        if isinstance(item,list):pending.extend(reversed(item))
        else:result.append(item)
    return result


def _membership_result(row,request_id,receipt,*,replayed):
    return {'outcome':'ready','collection':_public(row),'mutation':{
        'request_id':str(request_id),'replayed':replayed,'requested_count':receipt['requested_count'],
        'changed_count':receipt['changed_count'],'applied_revision':receipt['applied_revision']}}


def change_saved_collection_members(database,*,collection_id,request_id,action,video_ids,
                                    expected_revision,fingerprint,transaction=None):
    with _transaction(database,transaction,'change_saved_collection_members') as tx:
        args=tx.fetch_one(_PREPARE_MEMBERS,(collection_id,request_id,action,video_ids,expected_revision,fingerprint))
        ids=_flatten_array(args['video_ids'])
        if (args['id'] is None or args['request_id'] is None or args['action'] not in {'add','remove'}
            or not _matches(_REQUEST,str(args['request_id'])) or args['expected_revision'] is None or args['expected_revision']<1
            or not _matches(_FINGERPRINT,args['fingerprint']) or ids is None or not 1<=len(ids)<=500
            or any(not _matches(_VIDEO,item) for item in ids) or len(set(ids))!=len(ids)):_reject()
        row=_object(tx,'collection',args['id'])
        if row is None:return {'outcome':'not_found'}
        if row['deleted_at'] is not None:return {'outcome':'gone'}
        receipt=tx.fetch_one('SELECT request_fingerprint,requested_count,changed_count,applied_revision '
            'FROM public.saved_collection_commands WHERE collection_id=%s::uuid AND request_id=%s::uuid',
            (args['id'],args['request_id']))
        if receipt is not None:
            if receipt['request_fingerprint']!=args['fingerprint']:return {'outcome':'conflict'}
            return _membership_result(row,args['request_id'],receipt,replayed=True)
        if row['revision']!=args['expected_revision']:return {'outcome':'conflict'}
        now=tx.fetch_one('SELECT clock_timestamp() AS value')['value']
        if args['action']=='add':
            changed=tx.execute('INSERT INTO public.saved_collection_members(collection_id,video_id,added_at) '
                'SELECT %s::uuid,item,%s::timestamptz FROM unnest(%s::text[]) item ORDER BY item '
                'ON CONFLICT(collection_id,video_id) DO NOTHING',(args['id'],now,ids))
        else:
            changed=tx.execute('DELETE FROM public.saved_collection_members WHERE collection_id=%s::uuid AND video_id=ANY(%s::text[])',
                               (args['id'],ids))
        # Owners derived on read become stored with this write (not a membership change).
        tx.execute(_BIND_DERIVED_OWNERS,(args['id'],args['id']))
        if changed>0:
            row=tx.finalize_saved_collection_members(collection_id=args['id'],request_id=args['request_id'],
                fingerprint=args['fingerprint'],action=args['action'],requested_count=len(ids),
                changed_count=changed,delta=changed if args['action']=='add' else -changed,changed_at=now)['object']
        else:
            # A revision-1 no-op fails the existing applied_revision>1 CHECK; retain
            # that baseline constraint outcome instead of silently changing policy.
            tx.execute('INSERT INTO public.saved_collection_commands(collection_id,request_id,request_fingerprint,action,'
                'requested_count,changed_count,applied_revision) VALUES(%s::uuid,%s::uuid,%s,%s,%s,%s,%s)',
                (args['id'],args['request_id'],args['fingerprint'],args['action'],len(ids),changed,row['revision']))
        return _membership_result(row,args['request_id'],{'requested_count':len(ids),'changed_count':changed,
                                                         'applied_revision':row['revision']},replayed=False)
