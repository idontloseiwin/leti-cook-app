"""Page-only50-row effects with native facts and set writes at original stages.

The page owns every unique962106 advisory ID before canonical/membership DML.
Restoration keeps queue NOWAIT ownership and current lease tests; native source
UPDATEs finish before their nested queue/review stage. Nested and outer review
passes stay separate. This module never imports repair table-lock assumptions.
"""
import re
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseError,DatabaseServerError
from . import inventory_review_events as events,inventory_incident_queue_effects as queue
from ..reviews import video_type_review_decisions as manual
from ..channel.channel_read_policy import channel_pause_row
from ..videos.typed_video_read_policy import typed_identity_row
from ..videos.video_queue_initialization import ensure_video_collection_state_in_transaction, ensure_typed_collection_state_in_transaction
from .review_effect_batches import refresh_review_cases_batch,withdraw_review_cases_batch


def _named(sql):return re.sub(r'%\(([^)]+)\)s',lambda m:'source.'+m[1],sql)
_INPUT="jsonb_to_recordset(%s::jsonb) source(video_id text,channel_id text,video_type text,published_at text,published_acquired_at text)"
_IDS="unnest(%s::text[]) WITH ORDINALITY input(video_id,ordinality)"
_RAW="jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinality) CROSS JOIN LATERAL jsonb_to_record(raw.value) event(operation text,old_text text,new_text text)"
_member=events._MEMBER_FACTS.replace('%s::jsonb','event.old_text::jsonb',1).replace('%s::jsonb','event.new_text::jsonb',1)
_MEMBER_FACTS="SELECT raw.ordinality,event.operation,facts.*,restore.eligible FROM "+_RAW+" CROSS JOIN LATERAL ("+_member+") facts LEFT JOIN LATERAL ("+events._CAN_RESTORE.replace('%s::jsonb','event.new_text::jsonb')+") restore ON event.operation='insert' OR facts.withdrawal IS NOT TRUE ORDER BY raw.ordinality"
_QUEUE_LOCK="SELECT s.video_id,to_jsonb(s)::text AS value FROM "+_IDS+" JOIN public.video_collection_state s ON s.video_id=input.video_id ORDER BY input.ordinality FOR UPDATE OF s NOWAIT"
_RESTORE_LIVE='SELECT input.video_id,lease.live FROM '+_IDS+' CROSS JOIN LATERAL ('+events._QUEUE_LIVE.replace('%s::text','input.video_id')+') lease ORDER BY input.ordinality'
_MANUAL_LIVE="SELECT source.video_id,lease.live FROM jsonb_to_recordset(%s::jsonb) source(video_id text,value text) CROSS JOIN LATERAL ("+manual._QUEUE_LIVE.replace('%s::jsonb','source.value::jsonb')+') lease'
_RESTORE="""UPDATE public.video_inventory i SET video_type=held.video_type,published_at=held.source_published_at,
 published_acquired_at=held.published_acquired_at,skeleton_since=NULL
 FROM (WITH incoming AS MATERIALIZED(
 SELECT raw.ordinality,n.* FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinality)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) source(new_text text)
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory_memberships,source.new_text::jsonb) n),
 previous AS MATERIALIZED(SELECT i.video_id,to_jsonb(i)::text AS old_text,incoming.ordinality
 FROM public.video_inventory i JOIN incoming ON i.video_id=incoming.video_id
 WHERE i.channel_id=incoming.channel_id AND i.video_type IS NULL ORDER BY incoming.ordinality FOR NO KEY UPDATE OF i)
 SELECT previous.video_id,previous.old_text,incoming.channel_id,incoming.video_type,incoming.source_published_at,
 CASE WHEN incoming.source_published_at IS NOT NULL THEN incoming.last_present_at END AS published_acquired_at
 FROM incoming JOIN previous ON previous.video_id=incoming.video_id) held
 WHERE i.video_id=held.video_id AND i.channel_id=held.channel_id AND i.video_type IS NULL
 RETURNING i.video_id,held.old_text,to_jsonb(i)::text AS new_text"""
_INV_RAW="jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinality) CROSS JOIN LATERAL jsonb_to_record(raw.value) event(old_text text,new_text text) CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory,event.old_text::jsonb) previous CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory,event.new_text::jsonb) incoming"
_RESTORED_FACTS="""SELECT raw.ordinality,incoming.video_id,incoming.channel_id,incoming.video_type,incoming.published_at::text AS published_at,incoming.published_acquired_at::text AS published_acquired_at,
 previous.video_id=incoming.video_id AND previous.channel_id=incoming.channel_id AND previous.video_type IS NULL
 AND incoming.video_type IN('long','short','stream') AND to_jsonb(incoming)=to_jsonb(previous)||jsonb_build_object('video_type',incoming.video_type,'published_at',incoming.published_at,'published_acquired_at',incoming.published_acquired_at,'skeleton_since',NULL) AS exact,
 EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=incoming.video_id AND c.channel_id=incoming.channel_id AND c.decision_type=incoming.video_type) AS manual,
 EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=incoming.video_id AND c.channel_id=incoming.channel_id AND c.decision_type=incoming.video_type AND c.observed_canonical_type=incoming.video_type AND c.blocked_reason IS NULL) AS review_skip FROM """+_INV_RAW+' ORDER BY raw.ordinality'
_RESTORE_QUEUE=_named(queue._RESTORE_TYPE).replace(' WHERE video_id=',' FROM '+_INPUT+' WHERE public.video_collection_state.video_id=',1)+' RETURNING public.video_collection_state.video_id'
_DELETE_QUEUE='DELETE FROM public.video_collection_state WHERE video_id=ANY(%s::text[]) AND availability_first_missing_at IS NULL AND availability_resume_observed_at IS NULL'
_PAUSE_FACTS='SELECT input.video_id,policy.reason FROM '+_IDS+' CROSS JOIN LATERAL ('+queue._CHANNEL_PAUSE.replace('%s::text','input.video_id')+') policy ORDER BY input.ordinality'
_PAUSE=queue._PAUSE.replace('%s::text','source.reason',1).replace('%s::text','source.video_id',1).replace(' WHERE video_id='," FROM jsonb_to_recordset(%s::jsonb) source(video_id text,reason text) WHERE public.video_collection_state.video_id=",1)
_MANUAL_SOURCE='SELECT source.video_id,profile.* FROM '+_INPUT+' LEFT JOIN LATERAL ('+_named(manual._QUEUE_SOURCE)+') profile ON true'
_MANUAL_LONG='SELECT source.video_id,policy.* FROM '+_INPUT+' CROSS JOIN LATERAL ('+_named(manual._QUEUE_LONG)+') policy'
_MANUAL_TYPED='SELECT source.video_id,policy.* FROM '+_INPUT+' CROSS JOIN LATERAL ('+_named(manual._QUEUE_ELIGIBLE)+') policy'
_PLAN="jsonb_to_recordset(%s::jsonb) source(ordinality bigint,video_id text,channel_id text,video_type text,pause text,has_current boolean,published_at text,published_acquired_at text,stream_end text,stream_end_acquired_at text)"
_values,_conflict=manual._QUEUE_INSERT.split(' VALUES(',1)[1].rsplit(') ON CONFLICT',1)
_MANUAL_INSERT=manual._QUEUE_INSERT.split(' VALUES(',1)[0]+' SELECT '+_named(_values)+' FROM '+_PLAN+' WHERE true ORDER BY source.ordinality ON CONFLICT'+_conflict
_MANUAL_UPDATE=_named(manual._QUEUE_UPDATE).replace(' WHERE s.video_id=',' FROM '+_PLAN+' WHERE s.video_id=',1)
_NEW_INVENTORY="""SELECT raw.ordinality,source.video_id,incoming.video_id=source.video_id AND incoming.video_type IN('long','short','stream') AS exact,
 incoming.video_type FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinality)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) source(video_id text,effect_new text)
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory,source.effect_new::jsonb) incoming ORDER BY raw.ordinality"""

_RESUME_RETURNED="""UPDATE public.video_collection_state state
 SET availability_resume_observed_at=greatest(state.availability_resume_observed_at,incoming.last_present_at),
 hydration_needed=true,metadata_due_at=least(state.metadata_due_at,incoming.last_present_at),
 next_due_at=least(state.next_due_at,incoming.last_present_at),
 paused_reason=CASE WHEN state.paused_reason='availability_retry_expired' THEN NULL ELSE state.paused_reason END,
 updated_at=now()
 FROM jsonb_array_elements(%s::jsonb) raw(value)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) event(old_text text,new_text text)
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory_memberships,event.new_text::jsonb) incoming
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory_memberships,event.old_text::jsonb) previous
 WHERE state.video_id=incoming.video_id AND state.channel_id=incoming.channel_id
 AND (previous.membership_status='absent' OR state.availability_first_missing_at IS NOT NULL
      OR state.availability_resume_observed_at IS NOT NULL)
 AND incoming.membership_status='present' AND previous.membership_status IS DISTINCT FROM 'present'
 AND isfinite(incoming.last_present_at)
 AND (state.availability_first_missing_at IS NULL OR incoming.last_present_at>=state.availability_first_missing_at)
 AND incoming.last_present_at<=clock_timestamp()
 AND NOT EXISTS(SELECT 1 FROM public.videos video WHERE video.video_id=state.video_id
     AND video.availability_checked_at>=incoming.last_present_at)
 AND (state.paused_reason IS NULL OR state.paused_reason='availability_retry_expired')"""


def _shape():raise DatabaseError('database_result_shape_invalid')
def _count(value,maximum):
    if type(value) is not int or not 0<=value<=maximum:_shape()
def _rows(rows,ids,keys,complete=True):
    out={}
    for row in rows:
        if type(row) is not dict or set(row)!=set(keys) or type(row.get('video_id')) is not str or row['video_id'] not in ids or row['video_id'] in out:_shape()
        out[row['video_id']]=row
    if complete and set(out)!=set(ids):_shape()
    return out

def _queue_lock(tx,ids):
    return _rows(tx.fetch_all(_QUEUE_LOCK,(ids,),max_rows=len(ids)),ids,('video_id','value'),False)

def _ordinary_long(tx,ids):
    if not ids:return
    ensure_video_collection_state_in_transaction(tx,ids)
    facts=_rows(tx.fetch_all(_PAUSE_FACTS,(ids,),max_rows=len(ids)),ids,('video_id','reason'),False)
    plans=[]
    for video_id in ids:
        row=facts.get(video_id)
        if row is not None:
            if row['reason'] is not None and type(row['reason']) is not str:_shape()
            if row['reason'] is not None:plans.append(row)
    if plans:_count(tx.execute(_PAUSE,(Jsonb(plans),)),len(plans))


def _manual_queues(tx,targets):
    if not targets:return
    ids=[row['video_id'] for row in targets]
    held=_queue_lock(tx,ids)
    if held:
        live=_rows(tx.fetch_all(_MANUAL_LIVE,(Jsonb(list(held.values())),),max_rows=len(held)),list(held),('video_id','live'))
        if any(row['live'] is not None and type(row['live']) is not bool for row in live.values()):_shape()
        if any(row['live'] is True for row in live.values()):raise DatabaseServerError('manual_video_type_collection_busy',code='55P03')
    source=_rows(tx.fetch_all(_MANUAL_SOURCE,(Jsonb(targets),),max_rows=len(ids)),ids,('video_id','published_at','published_acquired_at','stream_end','stream_end_acquired_at','has_current','availability_status'))
    longs=[row for row in targets if row['video_type']=='long']
    typed=[row for row in targets if row['video_type']!='long']
    policy={}
    if longs:
        rows=_rows(tx.fetch_all(_MANUAL_LONG,(Jsonb(longs),),max_rows=len(longs)),[r['video_id'] for r in longs],('video_id','open_incident','policy_facts'))
        for video_id,row in rows.items():
            value=channel_pause_row(row,field='pause')
            pause=value['pause']
            facts=source[video_id]
            if facts['has_current'] and facts['availability_status'] not in (None,'public','unlisted'):pause=pause or 'availability_unsupported'
            policy[video_id]=pause
    if typed:
        rows=_rows(tx.fetch_all(_MANUAL_TYPED,(Jsonb(typed),),max_rows=len(typed)),[r['video_id'] for r in typed],('video_id','identity_facts'))
        for video_id,row in rows.items():policy[video_id]=None if typed_identity_row({'identity_facts':row['identity_facts']})['eligible'] is True else 'typed_identity_not_eligible'
    plans=[]
    for index,target in enumerate(targets,1):
        facts=source[target['video_id']]
        plans.append(dict(target,ordinality=index,pause=policy[target['video_id']],has_current=facts['has_current'],published_at=facts['published_at'],published_acquired_at=facts['published_acquired_at'],stream_end=facts['stream_end'],stream_end_acquired_at=facts['stream_end_acquired_at']))
    _count(tx.execute(_MANUAL_INSERT,(Jsonb(plans),)),len(plans))
    _count(tx.execute(_MANUAL_UPDATE,(Jsonb(plans),)),len(plans))


def _restored_inventory(tx,receipts):
    ids=[row['video_id'] for row in receipts]
    facts=_rows(tx.fetch_all(_RESTORED_FACTS,(Jsonb(receipts),),max_rows=len(ids)),ids,('ordinality','video_id','channel_id','video_type','published_at','published_acquired_at','exact','manual','review_skip'))
    targets=[]
    for index,video_id in enumerate(ids,1):
        row=facts[video_id]
        if (row['ordinality']!=index or row['exact'] is not True or type(row['manual']) is not bool or type(row['review_skip']) is not bool
                or type(row['channel_id']) is not str or row['video_type'] not in ('long','short','stream')
                or any(row[key] is not None and type(row[key]) is not str for key in ('published_at','published_acquired_at'))):_shape()
        targets.append({key:row[key] for key in ('video_id','channel_id','video_type','published_at','published_acquired_at')})
    restored=_rows(tx.returning_all(_RESTORE_QUEUE,(Jsonb(targets),),max_rows=len(ids)),ids,('video_id',),False)
    remaining=[row for row in targets if row['video_id'] not in restored]
    _manual_queues(tx,[row for row in remaining if facts[row['video_id']]['manual']])
    ordinary=[row for row in remaining if not facts[row['video_id']]['manual']]
    if ordinary:_count(tx.execute(_DELETE_QUEUE,([row['video_id'] for row in ordinary],)),len(ordinary))
    _ordinary_long(tx,[row['video_id'] for row in ordinary if row['video_type']=='long'])
    refresh_review_cases_batch(tx,[video_id for video_id in ids if not facts[video_id]['review_skip']])


def after_page_inventory_inserts(transaction,receipts):
    if type(receipts) is not list or len(receipts)>50:raise ValueError('page_insert_effects_invalid')
    if not receipts:return
    if any(type(row) is not dict or set(row)!={'video_id','effect_new'} or any(type(value) is not str for value in row.values()) for row in receipts):_shape()
    ids=[row['video_id'] for row in receipts]
    if len(set(ids))!=len(ids):_shape()
    with transaction.borrow():
        facts=_rows(transaction.fetch_all(_NEW_INVENTORY,(Jsonb(receipts),),max_rows=len(ids)),ids,('ordinality','video_id','exact','video_type'))
        for index,video_id in enumerate(ids,1):
            if facts[video_id]['ordinality']!=index or facts[video_id]['exact'] is not True:_shape()
        # A newly inserted inventory key cannot have committed queue/membership/
        # live-case FK children. Profiles have NO such FK: normal native review
        # facts below must still detect preexisting profile/channel conflicts.
        _ordinary_long(transaction,[video_id for video_id in ids if facts[video_id]['video_type']=='long'])
        refresh_review_cases_batch(transaction,ids)


def after_page_membership_changes(transaction,receipts):
    if type(receipts) is not list or len(receipts)>50:raise ValueError('page_membership_effects_invalid')
    if not receipts:return
    for event in receipts:
        if type(event) is not dict or set(event)!={'operation','old_text','new_text'}:_shape()
        events._input(event['operation'],event['old_text'],event['new_text'],membership=True)
        if event['operation'] not in ('insert','update'):_shape()
    with transaction.borrow():
        facts=transaction.fetch_all(_MEMBER_FACTS,(Jsonb(receipts),),max_rows=len(receipts))
        if len(facts)!=len(receipts):_shape()
        ids=[]
        withdrawn=[]
        restore=[]
        normal=[]
        by_id={}
        for index,(event,row) in enumerate(zip(receipts,facts),1):
            if (type(row) is not dict or set(row)!={'ordinality','operation','video_id','old_video_id','withdrawal','identity_changed','eligible'}
                    or row['ordinality']!=index or row['operation']!=event['operation'] or type(row['video_id']) is not str
                    or row['video_id'] in by_id or row['identity_changed'] is not (event['operation']=='insert')
                    or event['operation']=='update' and row['old_video_id']!=row['video_id']
                    or event['operation']=='insert' and row['old_video_id'] is not None
                    or any(row[key] is not None and type(row[key]) is not bool for key in ('withdrawal','eligible'))):_shape()
            video_id=row['video_id']
            ids.append(video_id)
            by_id[video_id]=event
            if event['operation']=='update' and row['withdrawal'] is True:withdrawn.append(video_id)
            else:
                normal.append(video_id)
                if row['eligible'] is True:restore.append(video_id)
        withdraw_review_cases_batch(transaction,withdrawn)
        if restore:
            _queue_lock(transaction,restore)
            live=_rows(transaction.fetch_all(_RESTORE_LIVE,(restore,),max_rows=len(restore)),restore,('video_id','live'))
            if any(type(row['live']) is not bool for row in live.values()):_shape()
            selected=[video_id for video_id in restore if not live[video_id]['live']]
            if selected:
                rows=transaction.returning_all(_RESTORE,(Jsonb([by_id[video_id] for video_id in selected]),),max_rows=len(selected))
                actual=_rows(rows,selected,('video_id','old_text','new_text'),False)
                if any(type(row[key]) is not str for row in actual.values() for key in ('old_text','new_text')):_shape()
                ordered=[actual[video_id] for video_id in selected if video_id in actual]
                if ordered:_restored_inventory(transaction,ordered)
        # A nested canonical review and the outer membership review are distinct
        # native events; retain both passes and their conflict-time case clocks.
        from .automatic_type_reconciliation import reconcile_automatic_video_types
        reconcile_automatic_video_types(transaction,normal)
        ensure_typed_collection_state_in_transaction(transaction,normal)
        refresh_review_cases_batch(transaction,normal)
        # The separate statement gives the source-clock comparison a snapshot
        # after the shared queue owner is locked. A live provider lease is not
        # a database row lock and is kept intact while recording this signal.
        _queue_lock(transaction,ids)
        _count(transaction.execute(_RESUME_RETURNED,(Jsonb(receipts),)),len(receipts))
