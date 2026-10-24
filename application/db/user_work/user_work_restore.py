"""Atomic import and pending-edit transactions used by user-work repositories.

Import retains its ten-table NOWAIT fence, tombstone revision floor, pending
ownership and all current triggers. Native casts/JSONB sizes stay PostgreSQL's;
Python owns branching and validation order. Runtime ACL prerequisites include
Saved tombstone/receipt DELETE and review INSERT/locking rights. No helper
grants rights, calls providers, or retries writes. Binding/ownership live in
the separate user_work_restore_bind module.
"""
from __future__ import annotations
from contextlib import contextmanager
import re

from psycopg.types.json import Jsonb

from ..base.postgres import DatabaseServerError, Operation

# Internal parent changes preserve only the already authorized locator revision.
# A response prepared against the old parent must lose its lease before retry.
REBASE_PENDING_PARENT_SQL = (
    "revision=revision+1, "
    "reconnect_revision=CASE WHEN reconnect_revision=revision THEN revision+1 ELSE reconnect_revision END, "
    "reconnect_state=CASE WHEN reconnect_state='running' THEN 'queued' ELSE reconnect_state END, "
    "reconnect_lease_token=NULL,reconnect_lease_expires_at=NULL"
)

_MAX_BYTES=16*1024*1024
_BATCH=500
_UUID4=re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')
_FP=re.compile(r'[0-9a-f]{64}\Z')
_ERRORS=frozenset(('channel_inventory_estimate_clock_conflict','channel_inventory_estimate_invalid','hot_source_clock_out_of_window',
 'hot_storage_active_work','user_work_channel_invalid','user_work_collection_invalid','user_work_document_invalid',
 'user_work_duplicate_identity','user_work_folder_invalid','user_work_identity_immutable','user_work_import_limit',
 'user_work_label_invalid','user_work_label_reference','user_work_limit_reached','user_work_member_invalid',
 'user_work_parent_cycle','user_work_parent_missing','user_work_review_history_invalid','user_work_review_invalid',
 'user_work_saved_view_invalid','user_work_pending_request_invalid'))
_OPERATION=Operation('import_user_work_backup',total_timeout_ms=60_000,error_codes=_ERRORS)


def _reject(message,code='P0001'):
    raise DatabaseServerError(message if message in _ERRORS else 'database_operation_failed',code=code)


def _keys(value,allowed,message):
    # jsonb_object_keys on a non-object raises before the function's key test.
    if not isinstance(value,dict):_reject('database_operation_failed','22023')
    if set(value) not in allowed:_reject(message)


def _number(value,expected):
    return type(value) in (int,float) and value==expected


def _matches(pattern,value):
    return isinstance(value,str) and pattern.fullmatch(value) is not None


_NATIVE_FACTS='''WITH inputs AS (
 SELECT ordinality AS position,value->>'kind' AS kind,value->>'value' AS text_value,value->'value' AS json_value,
 (value->>'check_future')::boolean AS check_future
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS e(value,ordinality)
), facts AS MATERIALIZED (
 SELECT *, (pg_input_error_info(text_value,kind)).sql_error_code AS error_code FROM inputs
)
SELECT position,error_code,
 CASE WHEN error_code IS NULL AND kind='bigint' THEN text_value::bigint END AS number,
 CASE WHEN error_code IS NULL AND kind='timestamptz' THEN isfinite(text_value::timestamptz) END AS finite,
 CASE WHEN error_code IS NULL AND kind='timestamptz' AND check_future THEN text_value::timestamptz<=clock_timestamp() END AS not_future,
 text_value !~ '[[:cntrl:]]' AS no_controls,text_value=btrim(text_value) AS trimmed,
 octet_length(json_value::text) AS json_bytes
FROM facts ORDER BY position'''


class _Facts:
    """Soft native casts batch facts; errors are raised only at their old position."""
    def __init__(self):self.requests=[];self.index={};self.values=[]
    def add(self,key,kind,value,*,check_future=False):
        if kind not in {'text','bigint','timestamptz'}:raise ValueError('user_work_native_type_invalid')
        self.index[key]=len(self.requests)
        self.requests.append({'kind':kind,'value':value,'check_future':check_future})
    def load(self,tx):
        for start in range(0,len(self.requests),_BATCH):
            batch=self.requests[start:start+_BATCH]
            rows=tx.fetch_all(_NATIVE_FACTS,(Jsonb(batch),),max_rows=_BATCH)
            if len(rows)!=len(batch) or any(row['position']!=i for i,row in enumerate(rows,1)):
                raise RuntimeError('user_work_native_result_invalid')
            self.values.extend(rows)
    def get(self,key):return self.values[self.index[key]]
    def cast(self,key):
        row=self.get(key)
        if row['error_code'] is not None:_reject('database_operation_failed',row['error_code'])
        return row


def _facts(document,tx):
    result=_Facts()
    for i,item in enumerate(document['folders']):
        if isinstance(item,dict):result.add(('folder',i),'text',item.get('name'))
    for i,item in enumerate(document['channels']):
        if isinstance(item,dict):
            for j,view in enumerate(item.get('saved_views',[]) if isinstance(item.get('saved_views'),list) else []):
                if isinstance(view,dict):
                    for key,kind in (('created_at','timestamptz'),('updated_at','timestamptz'),('revision','bigint'),('conditions','text'),('name','text')):
                        result.add(('view',i,j,key),kind,view.get(key))
    for i,item in enumerate(document['reviews']):
        if isinstance(item,dict):
            result.add(('review',i,'revision'),'bigint',item.get('revision'))
            history=item.get('history',[]) if isinstance(item.get('history'),list) else []
            for j,entry in enumerate(history):
                result.add(('history',i,j,'decided_at'),'timestamptz',entry.get('decided_at') if isinstance(entry,dict) else None)
                result.add(('history',i,j,'revision'),'bigint',entry.get('revision') if isinstance(entry,dict) else None)
    result.load(tx)
    return result


def _header(tx,document):
    info=tx.fetch_one('SELECT jsonb_typeof(%s::jsonb) AS kind,octet_length(%s::jsonb::text) AS bytes',
                     (None if document is None else Jsonb(document),None if document is None else Jsonb(document)))
    if info['kind']!='object' or info['bytes']>_MAX_BYTES:_reject('user_work_document_invalid')
    _keys(document,[{'channels','collections','folders','labels','reviews','schema_version'}],'user_work_document_invalid')
    if not _number(document['schema_version'],2) or any(not isinstance(document[k],list) for k in ('channels','collections','folders','labels','reviews')):
        _reject('user_work_document_invalid')
    if any(len(document[k])>limit for k,limit in (('channels',10000),('folders',1000),('labels',1001),('collections',1000),('reviews',1000))):
        _reject('user_work_import_limit')
    # The retained function checks hidden_at before its table locks. Each native
    # fact evaluates clock_timestamp(), never a client clock or transaction now.
    # Batching moves the evaluation within this pre-lock validation phase; exact
    # near-now scheduling equality with the sequential PL/pgSQL loop is not claimed.
    hidden=_Facts()
    for i,item in enumerate(document['channels']):
        if isinstance(item,dict):hidden.add(('hidden',i),'timestamptz',item.get('hidden_at'),check_future=True)
    hidden.load(tx)
    counts={'pending_channels':len(document['channels']),'saved_objects':len(document['collections']),'members':0}
    assignments=history=0
    for i,item in enumerate(document['channels']):
        if (not isinstance(item,dict) or not isinstance(item.get('saved_views'),list)
            or not isinstance(item.get('label_keys'),list) or type(item.get('is_hidden')) is not bool):_reject('user_work_channel_invalid')
        keys={'added_at','folder_id','id','is_hidden','label_keys','parent_ref','saved_views','user_input'}
        _keys(item,[keys,keys|{'hidden_at'}],'user_work_channel_invalid')
        if item.get('hidden_at') is not None:
            if not item['is_hidden'] or not isinstance(item['hidden_at'],str):_reject('user_work_channel_invalid')
            stamp=hidden.cast(('hidden',i))
            if stamp['finite'] is not True or stamp['not_future'] is not True:_reject('user_work_channel_invalid')
        counts['saved_objects']+=len(item['saved_views'])
        assignments+=len(item['label_keys'])
    for item in document['collections']:
        if not isinstance(item,dict) or not isinstance(item.get('members'),list):_reject('user_work_collection_invalid')
        counts['members']+=len(item['members'])
    for item in document['reviews']:
        if not isinstance(item,dict) or not isinstance(item.get('history'),list):_reject('user_work_review_invalid')
        history+=len(item['history'])
    if counts['saved_objects']>1000 or counts['members']>100000 or history>10000 or assignments>100000:_reject('user_work_import_limit')
    return counts,_facts(document,tx)


_EMPTY='''SELECT NOT (
 EXISTS(SELECT 1 FROM public.channels) OR EXISTS(SELECT 1 FROM public.folders)
 OR EXISTS(SELECT 1 FROM public.channel_labels WHERE label_key<>'monetized' OR name<>'Monetized' OR label_type<>'preset')
 OR EXISTS(SELECT 1 FROM public.channel_label_assignments) OR EXISTS(SELECT 1 FROM public.saved_objects WHERE deleted_at IS NULL)
 OR EXISTS(SELECT 1 FROM public.saved_collection_members)
 OR EXISTS(SELECT 1 FROM public.saved_collection_commands c WHERE NOT EXISTS(SELECT 1 FROM public.saved_objects o
  WHERE o.kind=c.collection_kind AND o.id=c.collection_id AND o.deleted_at IS NOT NULL))
 OR EXISTS(SELECT 1 FROM public.video_type_review_cases) OR EXISTS(SELECT 1 FROM public.video_type_review_history)
 OR EXISTS(SELECT 1 FROM public.user_channel_restore_items)) AS empty'''
_FOLDERS='''INSERT INTO public.folders(id,name,created_at)
 SELECT (item->>'id')::uuid,item->>'name',(item->>'created_at')::timestamptz
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS e(item,ord) ORDER BY ord'''
_LABELS='''INSERT INTO public.channel_labels(label_key,name,label_type,created_at)
 SELECT item->>'label_key',item->>'name',item->>'label_type',(item->>'created_at')::timestamptz
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS e(item,ord) ORDER BY ord'''
_PENDING='''INSERT INTO public.user_channel_restore_items(id,user_input,folder_id,parent_ref,is_hidden,hidden_at,added_at,label_keys,saved_views)
 SELECT (item->>'id')::uuid,item->>'user_input',(item->>'folder_id')::uuid,(item->>'parent_ref')::uuid,
 (item->>'is_hidden')::boolean,CASE WHEN (item->>'is_hidden')::boolean THEN (item->>'hidden_at')::timestamptz END,
 (item->>'added_at')::timestamptz,ARRAY(SELECT jsonb_array_elements_text(item->'label_keys')),
 (SELECT coalesce(jsonb_agg(CASE WHEN %s::jsonb ? ('view:'||(v->>'id'))
 THEN jsonb_set(v,'{revision}',to_jsonb(greatest((v->>'revision')::bigint,(%s::jsonb->>('view:'||(v->>'id')))::bigint+1)))
 ELSE v END ORDER BY vo),'[]'::jsonb) FROM jsonb_array_elements(item->'saved_views') WITH ORDINALITY AS views(v,vo))
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS e(item,ord) ORDER BY ord'''
_COLLECTION='''INSERT INTO public.saved_objects(kind,id,name,create_fingerprint,revision,member_count,created_at,updated_at)
 SELECT 'collection',(item->>'id')::uuid,item->>'name',item->>'create_fingerprint',
 greatest((item->>'revision')::bigint,coalesce((%s::jsonb->>('collection:'||(item->>'id')))::bigint+1,0)),
 jsonb_array_length(item->'members'),(item->>'created_at')::timestamptz,(item->>'updated_at')::timestamptz
 FROM (SELECT %s::jsonb AS item) input'''
_MEMBERS='''INSERT INTO public.saved_collection_members(collection_id,video_id,added_at,restore_owner_ref)
 SELECT %s::uuid,item->>'video_id',(item->>'added_at')::timestamptz,(item->>'channel_ref')::uuid
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS e(item,ord) ORDER BY ord'''
_REVIEW='''INSERT INTO public.video_type_review_cases(case_id,video_id,channel_id,source_detached_at,
 observed_canonical_type,observed_types,observed_channels,blocked_reason,state,revision,
 decision_type,decision_reason,decision_at,decision_revision,first_seen_at,last_seen_at,restore_owner_ref)
 SELECT (item->>'case_id')::uuid,NULL,NULL,clock_timestamp(),NULL,ARRAY[]::text[],ARRAY[]::text[],NULL,
 item->>'state',(item->>'revision')::bigint,item->>'decision_type',item->>'decision_reason',
 (item->>'decision_at')::timestamptz,(item->>'decision_revision')::bigint,
 user_at,user_at,(item->>'channel_ref')::uuid
 FROM (SELECT %s::jsonb AS item) input CROSS JOIN LATERAL (
 SELECT coalesce(min((value->>'decided_at')::timestamptz),(item->>'decision_at')::timestamptz,clock_timestamp()) AS user_at
 FROM jsonb_array_elements(item->'history')) clocks'''
_HISTORY='''INSERT INTO public.video_type_review_history(case_id,revision,video_type,reason,decided_at,request_id,expected_revision)
 SELECT %s::uuid,(item->>'revision')::bigint,item->>'video_type',item->>'reason',
 (item->>'decided_at')::timestamptz,(item->>'request_id')::uuid,(item->>'expected_revision')::bigint
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS e(item,ord) ORDER BY ord'''


def _insert_prefix(rows,validate,flush):
    """Flush valid earlier rows before raising a later validation failure.

    This keeps an earlier native/constraint failure ahead of a later domain
    failure. Input ordinal order is explicit in every batch INSERT.
    """
    pending=[]
    for index,item in enumerate(rows):
        try:validate(index,item)
        except DatabaseServerError:
            if pending:flush(pending)
            raise
        pending.append(item)
        if len(pending)==_BATCH:flush(pending);pending=[]
    if pending:flush(pending)


def _folder(item,fact):
    _keys(item,[{'created_at','id','name'}],'user_work_folder_invalid')
    if not isinstance(item.get('name'),str) or len(item['name'])==0 or fact['no_controls'] is False:_reject('user_work_folder_invalid')


def _label(item):
    _keys(item,[{'created_at','label_key','label_type','name'}],'user_work_label_invalid')
    if item.get('label_type')=='preset' and item.get('label_key')!='monetized':_reject('user_work_label_invalid')
    if item.get('label_type')=='custom':
        key=item.get('label_key')
        # SQL regex on JSON null yields NULL, not true; INSERT owns NOT NULL.
        if key is not None and (not isinstance(key,str) or not key.startswith('custom:') or not _matches(_UUID4,key[7:])):
            _reject('user_work_label_invalid')


def _view(view,facts,i,j):
    _keys(view,[{'conditions','create_fingerprint','created_at','id','name','revision','updated_at'}],'user_work_saved_view_invalid')
    if (any(not isinstance(view.get(k),str) for k in ('id','name','create_fingerprint','created_at','updated_at'))
        or type(view.get('revision')) not in (int,float)):_reject('user_work_saved_view_invalid')
    for key in ('created_at','updated_at'):
        if facts.cast(('view',i,j,key))['finite'] is not True:_reject('user_work_saved_view_invalid')
    if not _matches(_UUID4,view['id']) or not _matches(_FP,view['create_fingerprint']):_reject('user_work_saved_view_invalid')
    revision=facts.cast(('view',i,j,'revision'))['number']
    if revision is None or not 1<=revision<=9007199254740991:_reject('user_work_saved_view_invalid')
    conditions=view.get('conditions')
    if (not isinstance(conditions,dict) or not _number(conditions.get('version'),1)
        or not isinstance(conditions.get('filters'),dict) or not isinstance(conditions.get('facets'),dict)
        or facts.get(('view',i,j,'conditions'))['json_bytes']>32768):_reject('user_work_saved_view_invalid')
    name=facts.get(('view',i,j,'name'))
    if not 1<=len(view['name'])<=120 or name['trimmed'] is not True or name['no_controls'] is not True:_reject('user_work_saved_view_invalid')


def _restore(tx,document):
    counts,facts=_header(tx,document)
    tx.lock_user_work_import()
    if not tx.fetch_one(_EMPTY)['empty']:return {'outcome':'workspace_not_empty'}
    tx.execute('DELETE FROM public.saved_collection_commands c USING public.saved_objects o '
        'WHERE o.kind=c.collection_kind AND o.id=c.collection_id AND o.deleted_at IS NOT NULL')
    tombs=tx.fetch_one("SELECT coalesce(jsonb_object_agg(kind||':'||id,revision),'{}'::jsonb) AS value FROM public.saved_objects WHERE deleted_at IS NOT NULL")['value']
    tx.execute('DELETE FROM public.saved_objects WHERE deleted_at IS NOT NULL')
    _insert_prefix(document['folders'],lambda i,item:_folder(item,facts.get(('folder',i)) if isinstance(item,dict) else {}),
                   lambda rows:tx.execute(_FOLDERS,(Jsonb(rows),)))
    labels=document['labels']
    if not any(isinstance(item,dict) and item.get('label_key')=='monetized' for item in labels):
        tx.execute("DELETE FROM public.channel_labels WHERE label_key='monetized'")
    # SQL DESC places NULL before true before false for the preset predicate.
    labels=sorted(labels,key=lambda item:0 if not isinstance(item,dict) or item.get('label_key') is None else 1 if item.get('label_key')=='monetized' else 2)
    pending=[]
    for item in labels:
        try:_label(item)
        except DatabaseServerError:
            if pending:tx.execute(_LABELS,(Jsonb(pending),))
            raise
        if item.get('label_key')=='monetized':
            if pending:tx.execute(_LABELS,(Jsonb(pending),));pending=[]
            if tx.fetch_one("SELECT EXISTS(SELECT 1 FROM public.channel_labels WHERE label_key='monetized') AS present")['present']:
                tx.execute("UPDATE public.channel_labels SET name=item->>'name',created_at=(item->>'created_at')::timestamptz "
                    "FROM(SELECT %s::jsonb AS item) input WHERE label_key='monetized'",(Jsonb(item),))
                continue
        pending.append(item)
        if len(pending)==_BATCH:tx.execute(_LABELS,(Jsonb(pending),));pending=[]
    if pending:tx.execute(_LABELS,(Jsonb(pending),))
    references=[]
    for start in range(0,len(document['channels']),_BATCH):
        batch=document['channels'][start:start+_BATCH]
        rows=tx.fetch_all('SELECT ord,NOT(EXISTS(SELECT 1 FROM jsonb_array_elements_text(item->\'label_keys\') k '
            'WHERE NOT EXISTS(SELECT 1 FROM public.channel_labels l WHERE l.label_key=k)) OR '
            'jsonb_array_length(item->\'label_keys\')<>(SELECT count(DISTINCT value) FROM jsonb_array_elements_text(item->\'label_keys\'))) AS valid '
            'FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY AS e(item,ord) ORDER BY ord',(Jsonb(batch),),max_rows=_BATCH)
        references.extend(row['valid'] for row in rows)
    def validate_channel(i,item):
        if not references[i]:_reject('user_work_label_reference')
        for j,view in enumerate(item['saved_views']):_view(view,facts,i,j)
    _insert_prefix(document['channels'],validate_channel,
        lambda rows:tx.execute(_PENDING,(Jsonb(tombs),Jsonb(tombs),Jsonb(rows))))
    graph=tx.fetch_one('SELECT EXISTS(SELECT 1 FROM public.user_channel_restore_items child WHERE child.parent_ref IS NOT NULL '
        'AND NOT EXISTS(SELECT 1 FROM public.user_channel_restore_items parent WHERE parent.id=child.parent_ref)) AS missing')
    if graph['missing']:_reject('user_work_parent_missing')
    count=tx.fetch_one('WITH RECURSIVE tree(id) AS(SELECT id FROM public.user_channel_restore_items WHERE parent_ref IS NULL '
        'UNION ALL SELECT child.id FROM public.user_channel_restore_items child JOIN tree parent ON child.parent_ref=parent.id) '
        'SELECT count(*) AS value FROM tree')['value']
    if count!=counts['pending_channels']:_reject('user_work_parent_cycle')
    if tx.fetch_one("SELECT EXISTS(SELECT 1 FROM public.user_channel_restore_items r CROSS JOIN LATERAL jsonb_array_elements(r.saved_views) s "
        "GROUP BY s->>'id' HAVING count(*)>1) AS value")['value']:_reject('user_work_duplicate_identity')
    for item in document['collections']:
        _keys(item,[{'create_fingerprint','created_at','id','members','name','revision','updated_at'}],'user_work_collection_invalid')
        tx.execute(_COLLECTION,(Jsonb(tombs),Jsonb(item)))
        _insert_prefix(item['members'],lambda i,member:_keys(member,[{'added_at','video_id'},{'added_at','channel_ref','video_id'}],'user_work_member_invalid'),
            lambda rows:tx.execute(_MEMBERS,(item.get('id'),Jsonb(rows))))
    for i,item in enumerate(document['reviews']):
        keys={'case_id','decision_at','decision_reason','decision_revision','decision_type','history','revision','state'}
        _keys(item,[keys,keys|{'channel_ref'}],'user_work_review_invalid')
        if item.get('decision_type') is None and not item['history']:_reject('user_work_review_invalid')
        # MIN(history.decided_at) precedes the parent's INSERT and history key checks.
        for j,_entry in enumerate(item['history']):facts.cast(('history',i,j,'decided_at'))
        tx.execute(_REVIEW,(Jsonb(item),))
        def validate_history(j,entry):
            _keys(entry,[{'decided_at','expected_revision','reason','request_id','revision','video_type'}],'user_work_review_history_invalid')
            revision=facts.cast(('history',i,j,'revision'))['number']
            parent=facts.cast(('review',i,'revision'))['number']
            if revision is not None and parent is not None and revision>parent:_reject('user_work_review_history_invalid')
        _insert_prefix(item['history'],validate_history,lambda rows:tx.execute(_HISTORY,(item.get('case_id'),Jsonb(rows))))
    return {'outcome':'ready',**counts}


def import_user_work_backup(database,document,*,transaction=None):
    """Whole import commits once; NOWAIT failure rolls back its entire savepoint."""
    @contextmanager
    def owned():
        if transaction is not None:
            with transaction.borrow():yield transaction
        else:
            with database.transaction(_OPERATION) as tx:yield tx
    with owned() as tx:
        try:
            with tx.savepoint():return _restore(tx,document)
        except DatabaseServerError as error:
            if error.code=='55P03':return {'outcome':'busy'}
            raise


_PENDING_UPDATE_OPERATION=Operation('update_user_channel_restore_item',total_timeout_ms=60_000,error_codes=_ERRORS)


@contextmanager
def _pending_transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        with database.transaction(_PENDING_UPDATE_OPERATION) as tx:yield tx


def update_user_channel_restore_item(database,*,identity,expected_revision,user_input,parent_ref,transaction=None):
    """Serialize pending-parent edits, lock NOWAIT and apply the original CAS."""
    with _pending_transaction(database,transaction) as tx:
        try:
            with tx.savepoint():
                args=tx.fetch_one('SELECT %s::uuid AS id,%s::bigint AS revision,%s::text AS user_input,%s::uuid AS parent_ref',
                                 (identity,expected_revision,user_input,parent_ref))
                if args['id'] is None or args['revision'] is None or not 1<=args['revision']<=9007199254740990:
                    _reject('user_work_pending_request_invalid')
                if not tx.fetch_one('SELECT pg_try_advisory_xact_lock(815152610,1) AS locked')['locked']:
                    return {'outcome':'busy'}
                item=tx.fetch_one('SELECT id,user_input,parent_ref,revision FROM public.user_channel_restore_items WHERE id=%s::uuid FOR UPDATE NOWAIT',
                                  (args['id'],))
                if item is None:return {'outcome':'not_found'}
                if item['revision']!=args['revision']:return {'outcome':'conflict'}
                conflicts=tx.fetch_one('SELECT EXISTS(SELECT 1 FROM public.user_channel_restore_items WHERE id<>%s::uuid AND user_input=%s) AS pending,'
                    'EXISTS(SELECT 1 FROM public.channels WHERE user_input=%s) AS tracked',
                    (args['id'],args['user_input'],args['user_input']))
                if ((args['user_input'] is not None and conflicts['pending'])
                    or (args['user_input']!=item['user_input'] and conflicts['tracked'])):
                    return {'outcome':'conflict'}
                if args['parent_ref'] is not None:
                    if args['parent_ref']==args['id']:return {'outcome':'conflict'}
                    if not tx.fetch_one('SELECT EXISTS(SELECT 1 FROM public.user_channel_restore_items WHERE id=%s::uuid) '
                        'OR EXISTS(SELECT 1 FROM public.channels WHERE user_work_id=%s::uuid) AS present',
                        (args['parent_ref'],args['parent_ref']))['present']:
                        return {'outcome':'parent_missing'}
                    cycle=tx.fetch_one('WITH RECURSIVE ancestors(id) AS(SELECT %s::uuid UNION '
                        'SELECT r.parent_ref FROM public.user_channel_restore_items r JOIN ancestors a ON a.id=r.id WHERE r.parent_ref IS NOT NULL) '
                        'SELECT EXISTS(SELECT 1 FROM ancestors WHERE id=%s::uuid) AS cycle',(args['parent_ref'],args['id']))
                    if cycle['cycle']:return {'outcome':'conflict'}
                result=tx.returning_one("UPDATE public.user_channel_restore_items SET user_input=%s,parent_ref=%s::uuid,revision=revision+1, "
                    "reconnect_state='idle',reconnect_revision=NULL,reconnect_requested_at=NULL,reconnect_not_before=NULL, "
                    "reconnect_last_attempt_at=NULL,reconnect_attempt_count=0,reconnect_error_code=NULL, "
                    "reconnect_lease_token=NULL,reconnect_lease_expires_at=NULL "
                    "WHERE id=%s::uuid RETURNING jsonb_build_object('id',id,'user_input',user_input,'parent_ref',parent_ref,'revision',revision) AS value",
                    (args['user_input'],args['parent_ref'],args['id']))['value']
                return {'outcome':'ready','item':result}
        except DatabaseServerError as error:
            if error.code=='55P03':return {'outcome':'busy'}
            raise
