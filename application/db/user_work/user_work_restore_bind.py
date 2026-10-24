"""Pending-channel binding and restored ownership transfer.

The backup repository delegates binding here. Intrinsic owner-binding and
backup-limit guards stay authoritative; all native validation, locks and writes
share one bounded transaction. The two ownership updates require separately
reviewed column grants on Saved members and review cases. No provider calls or
retained business-function delegation.
"""
from __future__ import annotations
from contextlib import contextmanager
import re

from psycopg.types.json import Jsonb
from ..channel.channel_workflow_inserts import insert_restored_channel
from ..base.postgres import DatabaseServerError, Operation
from .user_work_restore import REBASE_PENDING_PARENT_SQL

_ERRORS=frozenset(('channel_collection_scope_lost','channel_inventory_estimate_clock_conflict',
 'channel_inventory_estimate_invalid','hot_source_clock_out_of_window','hot_storage_active_work',
 'user_work_adopt_invalid','user_work_bind_request_invalid','user_work_channel_data_invalid',
 'user_work_identity_immutable','user_work_limit_reached'))
_CHANNEL=re.compile(r'UC[A-Za-z0-9_-]{22}\Z')


def _reject(code):
    raise DatabaseServerError(code if code in _ERRORS else 'database_operation_failed',code='P0001')


@contextmanager
def _transaction(database,transaction,name):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        with database.transaction(Operation(name,total_timeout_ms=60_000,error_codes=_ERRORS)) as tx:yield tx


def _transfer(tx,identity,user_work_id,*,adopt):
    args=tx.fetch_one('SELECT %s::uuid AS id,%s::uuid AS owner',(identity,user_work_id))
    if args['id'] is None or args['owner'] is None:_reject('user_work_adopt_invalid')
    present=tx.fetch_one('SELECT EXISTS(SELECT 1 FROM public.channels WHERE user_work_id=%s::uuid '
        'AND (NOT %s::boolean OR user_work_restored)) AND EXISTS('
        'SELECT 1 FROM public.user_channel_restore_items WHERE id=%s::uuid) AS present',
        (args['owner'],adopt,args['id']))
    if not present['present']:_reject('user_work_adopt_invalid')
    tx.execute('UPDATE public.saved_collection_members SET user_channel_ref=%s::uuid,restore_owner_ref=NULL '
               'WHERE restore_owner_ref=%s::uuid',(args['owner'],args['id']))
    tx.execute('UPDATE public.video_type_review_cases SET user_channel_ref=%s::uuid,restore_owner_ref=NULL '
               'WHERE restore_owner_ref=%s::uuid',(args['owner'],args['id']))


def adopt_restored_channel_user_work(database,identity,*,transaction=None):
    with _transaction(database,transaction,'adopt_restored_channel_user_work') as tx:
        _transfer(tx,identity,identity,adopt=True)


def merge_restored_channel_user_work(database,identity,user_work_id,*,transaction=None):
    with _transaction(database,transaction,'merge_restored_channel_user_work') as tx:
        _transfer(tx,identity,user_work_id,adopt=False)


_DATA_SHAPE="""SELECT
 (SELECT array_agg(k ORDER BY k COLLATE "C") FROM jsonb_object_keys(data) k) IS DISTINCT FROM
 ARRAY['added_at','avatar_url','channel_created_at','channel_id','channel_metadata_checked_at',
 'channel_metadata_error','channel_metadata_error_count','channel_metadata_next_after','channel_metadata_started_at','channel_metadata_status',
 'country_code','country_collected_at','deep_inventory_error','hidden_at','initial_28d_stats_status','initial_inventory_estimate',
 'initial_long_inventory_status','is_hidden','subscribers','title','uploads_playlist_id','user_input','video_count','views']
 OR data->>'channel_id' IS DISTINCT FROM %s::text
 OR data->>'user_input' IS DISTINCT FROM %s::text
 OR data->'is_hidden' IS DISTINCT FROM 'false'::jsonb
 OR data->'hidden_at' IS DISTINCT FROM 'null'::jsonb
 OR data->'deep_inventory_error' IS DISTINCT FROM 'null'::jsonb
 OR data->'channel_metadata_started_at' IS DISTINCT FROM 'null'::jsonb
 OR data->'channel_metadata_error' IS DISTINCT FROM 'null'::jsonb
 OR data->'channel_metadata_error_count' IS DISTINCT FROM '0'::jsonb
 OR data->>'channel_metadata_status' IS DISTINCT FROM 'ok'
 OR jsonb_typeof(data->'title') IS DISTINCT FROM 'string'
 OR data->>'initial_long_inventory_status' NOT IN('pending','manual_required')
 OR NOT ((data->>'initial_long_inventory_status'='pending' AND data->>'initial_28d_stats_status'='pending')
 OR(data->>'initial_long_inventory_status'='manual_required' AND data->'initial_28d_stats_status'='null'::jsonb)) AS invalid
 FROM (SELECT %s::jsonb AS data) input"""
_DATA_CLOCKS="""SELECT NOT coalesce(isfinite(source_clock) AND source_clock BETWEEN clock_timestamp()-interval '5 minutes' AND clock_timestamp(),false)
 OR (data->>'country_collected_at')::timestamptz IS DISTINCT FROM source_clock
 OR NOT coalesce(isfinite((data->>'channel_metadata_next_after')::timestamptz)
 AND (data->>'channel_metadata_next_after')::timestamptz BETWEEN source_clock AND source_clock+interval '504 hours',false)
 OR jsonb_typeof(data->'initial_inventory_estimate') IS DISTINCT FROM 'object' AS invalid
 FROM (SELECT %s::jsonb AS data,%s::timestamptz AS source_clock) input"""
_VIEW_INSERT="""INSERT INTO public.saved_objects(kind,id,name,channel_id,conditions,create_fingerprint,revision,created_at,updated_at)
 SELECT 'view',(item->>'id')::uuid,item->>'name',%s::text,item->'conditions',item->>'create_fingerprint',
 (item->>'revision')::bigint,(item->>'created_at')::timestamptz,(item->>'updated_at')::timestamptz
 FROM (SELECT %s::jsonb AS item) input"""
_CHANNEL_INSERT="""INSERT INTO public.channels(channel_id,user_input,user_work_id,user_work_restored,folder_id,parent_channel_id,is_hidden,hidden_at,added_at,
 title,avatar_url,uploads_playlist_id,subscribers,views,video_count,country_code,country_collected_at,channel_created_at,
 channel_metadata_checked_at,channel_metadata_next_after,channel_metadata_status,channel_metadata_started_at,channel_metadata_error,channel_metadata_error_count,
 initial_inventory_estimate,initial_long_inventory_status,initial_28d_stats_status,deep_inventory_error)
 SELECT %s::text,%s::text,%s::uuid,true,%s::uuid,%s::text,%s::boolean,
 CASE WHEN %s::boolean THEN coalesce(%s::timestamptz,clock_timestamp()) END,%s::timestamptz,
 data->>'title',data->>'avatar_url',data->>'uploads_playlist_id',
 (data->>'subscribers')::bigint,(data->>'views')::bigint,(data->>'video_count')::bigint,
 data->>'country_code',(data->>'country_collected_at')::timestamptz,(data->>'channel_created_at')::timestamptz,
 %s::timestamptz,(data->>'channel_metadata_next_after')::timestamptz,'ok',NULL,NULL,0,
 data->'initial_inventory_estimate',data->>'initial_long_inventory_status',data->>'initial_28d_stats_status',NULL
 FROM (SELECT %s::jsonb AS data) input"""


def _views(tx,views,channel_id,*,skip_existing):
    # Stored pending rows cap this array at1000. Retain the sequential check/insert:
    # bulk ON CONFLICT changes malformed/duplicate stored-view error precedence.
    for view in views:
        if skip_existing and tx.fetch_one("SELECT EXISTS(SELECT 1 FROM public.saved_objects WHERE kind='view' "
            "AND id=(%s::jsonb->>'id')::uuid) AS present",(Jsonb(view),))['present']:continue
        tx.execute(_VIEW_INSERT,(channel_id,Jsonb(view)))


def _bind(tx,identity,expected_revision,channel_id,channel_data,reconnect_lease_token=None):
    args=tx.fetch_one('SELECT %s::uuid AS id,%s::bigint AS revision,%s::text AS channel_id',
                      (identity,expected_revision,channel_id))
    identity,revision,channel_id=args['id'],args['revision'],args['channel_id']
    if identity is None or revision is None or revision<1 or channel_id is None or not _CHANNEL.fullmatch(channel_id):
        _reject('user_work_bind_request_invalid')
    if not tx.fetch_one('SELECT pg_try_advisory_xact_lock(815152610,1) AS locked')['locked']:return {'outcome':'busy'}
    item=tx.fetch_one("SELECT id,revision,user_input,parent_ref,folder_id,label_keys,saved_views,is_hidden,"
        "to_jsonb(hidden_at)#>>'{}' AS hidden_at,to_jsonb(added_at)#>>'{}' AS added_at, "
        "reconnect_state,reconnect_revision,reconnect_lease_token::text AS reconnect_lease_token, "
        "reconnect_lease_expires_at>clock_timestamp() AS reconnect_lease_live "
        'FROM public.user_channel_restore_items WHERE id=%s::uuid FOR UPDATE NOWAIT',(identity,))
    if item is None:
        if tx.fetch_one('SELECT EXISTS(SELECT 1 FROM public.channels WHERE channel_id=%s AND user_work_id=%s::uuid) AS present',
                        (channel_id,identity))['present']:
            return {'outcome':'ready','id':str(identity),'replayed':True}
        return {'outcome':'not_found'}
    if item['revision']!=revision:return {'outcome':'conflict'}
    if item.get('reconnect_state','idle')!='idle':
        if reconnect_lease_token is None:return {'outcome':'busy'}
        if (item.get('reconnect_state')!='running' or item.get('reconnect_revision')!=revision
            or item.get('reconnect_lease_token')!=reconnect_lease_token or item.get('reconnect_lease_live') is not True):
            return {'outcome':'lease_lost'}
    elif reconnect_lease_token is not None:return {'outcome':'lease_lost'}
    if item['user_input'] is None:return {'outcome':'needs_input'}
    parent_id=None
    if item['parent_ref'] is not None:
        parent=tx.fetch_one('SELECT channel_id FROM public.channels WHERE user_work_id=%s::uuid FOR SHARE NOWAIT',(item['parent_ref'],))
        if parent is None:return {'outcome':'parent_pending'}
        parent_id=parent['channel_id']
    channel=tx.fetch_one('SELECT user_work_id,folder_id,parent_channel_id FROM public.channels WHERE channel_id=%s FOR UPDATE NOWAIT',(channel_id,))
    if channel is not None:
        if parent_id is not None:
            if parent_id==channel_id or tx.fetch_one('WITH RECURSIVE up(id) AS('
                'SELECT c.parent_channel_id FROM public.channels c WHERE c.channel_id=%s UNION '
                'SELECT c.parent_channel_id FROM public.channels c JOIN up ON c.channel_id=up.id) '
                'SELECT EXISTS(SELECT 1 FROM up WHERE id=%s) AS cycle',(parent_id,channel_id))['cycle']:
                parent_id=None
        if (channel['folder_id'] is None and item['folder_id'] is not None) or (channel['parent_channel_id'] is None and parent_id is not None):
            tx.execute('UPDATE public.channels SET folder_id=coalesce(%s::uuid,%s::uuid),'
                'parent_channel_id=coalesce(%s::text,%s::text) WHERE channel_id=%s',
                (channel['folder_id'],item['folder_id'],channel['parent_channel_id'],parent_id,channel_id))
        tx.execute('INSERT INTO public.channel_label_assignments(channel_id,label_key) SELECT %s,key FROM unnest(%s::text[]) key '
            'WHERE EXISTS(SELECT 1 FROM public.channel_labels WHERE label_key=key) ON CONFLICT DO NOTHING',(channel_id,item['label_keys']))
        _views(tx,item['saved_views'],channel_id,skip_existing=True)
        tx.execute("UPDATE public.user_channel_restore_items SET parent_ref=%s::uuid," +
            REBASE_PENDING_PARENT_SQL + " WHERE parent_ref=%s::uuid",
                   (channel['user_work_id'],identity))
        _transfer(tx,identity,channel['user_work_id'],adopt=False)
        tx.execute('DELETE FROM public.user_channel_restore_items WHERE id=%s::uuid',(identity,))
        return {'outcome':'ready','id':str(identity),'replayed':False,'merged':True}
    data=None if channel_data is None else Jsonb(channel_data)
    info=tx.fetch_one('SELECT jsonb_typeof(%s::jsonb) AS kind,octet_length(%s::jsonb::text) AS bytes',(data,data))
    if info['kind']!='object' or info['bytes']>131072:return {'outcome':'channel_unavailable'}
    if tx.fetch_one(_DATA_SHAPE,(channel_id,item['user_input'],data))['invalid'] is True:_reject('user_work_channel_data_invalid')
    # Keep native out-of-Python-range timestamps as wire text until SQL checks
    # finite/fresh. Decoding year10000 or infinity as datetime would raise before
    # the retained domain check and change its P0001 result.
    source=tx.fetch_one("SELECT to_jsonb((%s::jsonb->>'channel_metadata_checked_at')::timestamptz)#>>'{}' AS value",(data,))['value']
    if tx.fetch_one(_DATA_CLOCKS,(data,source))['invalid'] is True:_reject('user_work_channel_data_invalid')
    # Native casts occur in the retained field order and after its type predicate.
    for field in ('subscribers','views','video_count'):
        if tx.fetch_one("SELECT data->%s<>'null'::jsonb AND (jsonb_typeof(data->%s)<>'number' OR (data->>%s)::bigint<0) AS invalid "
            "FROM (SELECT %s::jsonb AS data) input",(field,field,field,data))['invalid'] is True:
            _reject('user_work_channel_data_invalid')
    conflict=tx.fetch_one('SELECT EXISTS(SELECT 1 FROM public.channels WHERE user_work_id=%s::uuid) '
        'OR EXISTS(SELECT 1 FROM public.channel_label_assignments WHERE channel_id=%s) '
        "OR EXISTS(SELECT 1 FROM public.saved_objects WHERE kind='view' AND channel_id=%s) AS present",(identity,channel_id,channel_id))
    if parent_id==channel_id or conflict['present']:return {'outcome':'conflict'}
    if not tx.fetch_one("SELECT coalesce(isfinite(%s::timestamptz) AND %s::timestamptz BETWEEN clock_timestamp()-interval '696 hours' "
                       'AND clock_timestamp(),false) AS fresh',(source,source))['fresh']:
        return {'outcome':'channel_unavailable'}
    if tx.fetch_one('SELECT EXISTS(SELECT 1 FROM unnest(%s::text[]) key WHERE NOT EXISTS('
        'SELECT 1 FROM public.channel_labels WHERE label_key=key)) OR EXISTS('
        "SELECT 1 FROM jsonb_array_elements(%s::jsonb) spec JOIN public.saved_objects existing ON existing.kind='view' "
        "AND existing.id=(spec->>'id')::uuid) AS present",(item['label_keys'],Jsonb(item['saved_views'])))['present']:
        return {'outcome':'conflict'}
    insert_restored_channel(tx,(channel_id,item['user_input'],identity,item['folder_id'],parent_id,item['is_hidden'],
        item['is_hidden'],item['hidden_at'],item['added_at'],source,data))
    tx.execute('INSERT INTO public.channel_label_assignments(channel_id,label_key) SELECT %s,key FROM unnest(%s::text[]) key',
               (channel_id,item['label_keys']))
    _views(tx,item['saved_views'],channel_id,skip_existing=False)
    _transfer(tx,identity,identity,adopt=True)
    tx.execute('DELETE FROM public.user_channel_restore_items WHERE id=%s::uuid',(identity,))
    updated=tx.execute("UPDATE public.channel_collection_state SET owner_permission=CASE WHEN %s::jsonb->>'initial_long_inventory_status'='pending' "
        "THEN 'accepted' ELSE 'manual_required' END,is_hidden=%s::boolean,hidden_at=CASE WHEN %s::boolean THEN coalesce(%s::timestamptz,clock_timestamp()) END,"
        "control_epoch=nextval('public.channel_collection_epoch_seq') WHERE channel_id=%s AND user_work_id=%s::uuid AND owner_permission='legacy_hold'",
        (data,item['is_hidden'],item['is_hidden'],item['hidden_at'],channel_id,identity))
    if updated==0:_reject('channel_collection_scope_lost')
    return {'outcome':'ready','id':str(identity),'replayed':False}


def bind_user_channel_restore_item(database,*,identity,expected_revision,channel_id,channel_data=None,transaction=None,reconnect_lease_token=None):
    with _transaction(database,transaction,'bind_user_channel_restore_item') as tx:
        try:
            with tx.savepoint():return _bind(tx,identity,expected_revision,channel_id,channel_data,reconnect_lease_token)
        except DatabaseServerError as error:
            if error.code=='55P03':return {'outcome':'busy'}
            raise
