"""Finite canonical-repair effects under its pre-existing table lock protocol.

Only actual long/NULL -> short/stream receipts from the single repair UPDATE
enter here. Inventory, membership, playlist, scan and queue relations are held
by the original repair owner. Independent video effects are processed in bounded
1000-row batches; source writes finish before any queue or review effect.
"""
import re
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseError,DatabaseServerError
from . import inventory_incident_queue_effects as queue
from ..reviews import video_review_evidence as review
from ..reviews import video_type_review_decisions as decisions
from ..videos.typed_video_read_policy import typed_identity_row

_INPUT="jsonb_to_recordset(%s::jsonb) input(video_id text,channel_id text,video_type text,published_at text,published_acquired_at text)"
_EVENTS="""jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinality)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) event(old_text text,new_text text)
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory,event.old_text::jsonb) previous
 CROSS JOIN LATERAL jsonb_populate_record(NULL::public.video_inventory,event.new_text::jsonb) incoming"""
_FACTS="""SELECT raw.ordinality,incoming.video_id,incoming.channel_id,incoming.video_type,
 incoming.published_at::text AS published_at,incoming.published_acquired_at::text AS published_acquired_at,previous.video_type IS NULL AS restore_type,
 previous.video_id=incoming.video_id AND previous.channel_id=incoming.channel_id
 AND (previous.video_type IS NULL OR previous.video_type='long') AND incoming.video_type IN('short','stream')
 AND to_jsonb(incoming)=to_jsonb(previous)||jsonb_build_object('video_type',incoming.video_type,'updated_at',now()) AS exact,
 EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=incoming.video_id
 AND c.channel_id=incoming.channel_id AND c.decision_type=incoming.video_type) AS manual
 FROM """+_EVENTS+' ORDER BY raw.ordinality'


def _named(statement,prefix='input'):
    return re.sub(r'%\(([^)]+)\)s',lambda match:prefix+'.'+match[1],statement)


_RESTORE=_named(queue._RESTORE_TYPE).replace(' WHERE video_id=', ' FROM '+_INPUT+' WHERE public.video_collection_state.video_id=',1)+' RETURNING public.video_collection_state.video_id'
_DELETE="DELETE FROM public.video_collection_state WHERE video_id=ANY(%s::text[])"
_MANUAL_LOCK="""SELECT video_id,lease_token IS NOT NULL AND lease_expires_at>clock_timestamp() AS live
 FROM public.video_collection_state WHERE video_id=ANY(%s::text[]) FOR UPDATE NOWAIT"""
_MANUAL_FACTS='SELECT input.video_id,source.*,identity.identity_facts FROM '+_INPUT+"""
 LEFT JOIN LATERAL ("""+_named(decisions._QUEUE_SOURCE)+""") source ON true
 CROSS JOIN LATERAL ("""+_named(decisions._QUEUE_ELIGIBLE)+') identity'
_PLAN_INPUT="jsonb_to_recordset(%s::jsonb) input(video_id text,channel_id text,video_type text,pause text,has_current boolean,published_at text,published_acquired_at text,stream_end text,stream_end_acquired_at text)"
_insert_values,_insert_conflict=decisions._QUEUE_INSERT.split(' VALUES(',1)[1].rsplit(') ON CONFLICT',1)
_MANUAL_INSERT=decisions._QUEUE_INSERT.split(' VALUES(',1)[0]+' SELECT '+_named(_insert_values)+' FROM '+_PLAN_INPUT+' WHERE true ON CONFLICT'+_insert_conflict
_MANUAL_UPDATE=_named(decisions._QUEUE_UPDATE).replace(' WHERE s.video_id=', ' FROM '+_PLAN_INPUT+' WHERE s.video_id=',1)
_REVIEW_DECISION="""SELECT input.video_id,EXISTS(SELECT 1 FROM public.video_type_review_cases c
 WHERE c.video_id=input.video_id AND c.channel_id=input.channel_id AND c.decision_type=input.video_type
 AND c.observed_canonical_type=input.video_type AND c.blocked_reason IS NULL) AS present FROM """+_INPUT
_ADVISORY="""SELECT pg_advisory_xact_lock(hashtextextended(input.video_id,962106)) AS acquired
 FROM unnest(%s::text[]) WITH ORDINALITY input(video_id,ordinality) ORDER BY input.ordinality"""
_REVIEW_FACTS='SELECT input.*,evidence.*,blocked.blocked,EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=input.video_id) AS present FROM '+_INPUT+"""
 CROSS JOIN LATERAL ("""+review._EVIDENCE.replace('%s::text','input.video_id')+""") evidence
 CROSS JOIN LATERAL ("""+_named(review._BLOCKED).replace('input.channels','evidence.channels')+') blocked'
_REVIEW_INPUT="jsonb_to_recordset(%s::jsonb) input(video_id text,channel_id text,video_type text,removed boolean,types text[],channels text[],blocked text,evidence_expires_at timestamptz)"
_upsert_values,_upsert_conflict=review._UPSERT.split(' VALUES(',1)[1].rsplit(')\n ON CONFLICT',1)
# ON CONFLICT can only reference EXCLUDED/c, not the INSERT SELECT input alias.
# Every admitted repair event is the normal source_removed=False branch.
_upsert_conflict=_upsert_conflict.replace('%(removed)s::boolean','false')
_REVIEW_UPSERT=review._UPSERT.split(' VALUES(',1)[0]+' SELECT '+_named(_upsert_values)+' FROM '+_REVIEW_INPUT+' WHERE true\n ON CONFLICT'+_upsert_conflict


def _shape():
    raise DatabaseError('database_result_shape_invalid')


def _rows_by_id(rows,ids,keys,*,complete=True):
    result={}
    for row in rows:
        if type(row) is not dict or set(row)!=set(keys) or type(row.get('video_id')) is not str or row['video_id'] not in ids or row['video_id'] in result:
            _shape()
        result[row['video_id']]=row
    if complete and set(result)!=set(ids):
        _shape()
    return result


def _manual(transaction,targets):
    ids=[row['video_id'] for row in targets]
    held=_rows_by_id(transaction.fetch_all(_MANUAL_LOCK,(ids,),max_rows=len(ids)),ids,('video_id','live'),complete=False)
    for row in held.values():
        if row['live'] is not None and type(row['live']) is not bool:_shape()
        if row['live'] is True:raise DatabaseServerError('manual_video_type_collection_busy',code='55P03')
    facts=_rows_by_id(transaction.fetch_all(_MANUAL_FACTS,(Jsonb(targets),),max_rows=len(ids)),ids,
        ('video_id','published_at','published_acquired_at','stream_end','stream_end_acquired_at','has_current','availability_status','identity_facts'))
    plans=[]
    for target in targets:
        fact=facts[target['video_id']]
        pause=None if typed_identity_row({'identity_facts':fact['identity_facts']})['eligible'] else 'typed_identity_not_eligible'
        plans.append(dict(target,pause=pause,has_current=fact['has_current'],published_at=fact['published_at'],published_acquired_at=fact['published_acquired_at'],stream_end=fact['stream_end'],stream_end_acquired_at=fact['stream_end_acquired_at']))
    transaction.execute(_MANUAL_INSERT,(Jsonb(plans),))
    transaction.execute(_MANUAL_UPDATE,(Jsonb(plans),))


def _review(transaction,targets):
    ids=[row['video_id'] for row in targets]
    skip=_rows_by_id(transaction.fetch_all(_REVIEW_DECISION,(Jsonb(targets),),max_rows=len(ids)),ids,('video_id','present'))
    if any(type(row['present']) is not bool for row in skip.values()):_shape()
    active=[row for row in targets if not skip[row['video_id']]['present']]
    if not active:return
    ids=[row['video_id'] for row in active]
    transaction.fetch_all(_ADVISORY,(ids,),max_rows=len(ids))
    facts=_rows_by_id(transaction.fetch_all(_REVIEW_FACTS,(Jsonb(active),),max_rows=len(ids)),ids,
        ('video_id','channel_id','video_type','published_at','published_acquired_at','types','channels','blocked','present','evidence_expires_at'))
    plans=[]
    for target in active:
        fact=facts[target['video_id']]
        if (type(fact['types']) is not list or type(fact['channels']) is not list or type(fact['present']) is not bool
                or fact['evidence_expires_at'] is not None and type(fact['evidence_expires_at']) is not str):_shape()
        conflict=(len(fact['types'])>1 or fact['blocked'] is not None or bool(fact['types']) and (fact['video_type'] is None or fact['video_type'] not in fact['types']))
        if conflict is False and not fact['present']:continue
        plans.append(dict(video_id=fact['video_id'],channel_id=fact['channel_id'],video_type=fact['video_type'],removed=False,
            types=fact['types'],channels=fact['channels'],blocked=fact['blocked'],evidence_expires_at=fact['evidence_expires_at']))
    if plans:transaction.execute(_REVIEW_UPSERT,(Jsonb(plans),))


def after_canonical_repair_statement(transaction,receipts):
    """Only this repair's held relations permit independent per-video batching."""
    if type(receipts) is not list or len(receipts)>5000:raise ValueError('canonical_repair_receipts_invalid')
    with transaction.borrow():
        seen=set()
        for start in range(0,len(receipts),1000):
            events=receipts[start:start+1000]
            if any(type(row) is not dict or set(row)!={'old_text','new_text'} or any(type(value) is not str for value in row.values()) for row in events):_shape()
            facts=transaction.fetch_all(_FACTS,(Jsonb(events),),max_rows=len(events))
            if len(facts)!=len(events):_shape()
            targets=[]
            restore=[]
            manual={}
            for index,row in enumerate(facts,1):
                if (type(row) is not dict or set(row)!={'ordinality','video_id','channel_id','video_type','published_at','published_acquired_at','restore_type','exact','manual'}
                        or row['ordinality']!=index or row['exact'] is not True
                        or type(row['restore_type']) is not bool or type(row['manual']) is not bool
                        or type(row['video_id']) is not str or type(row['channel_id']) is not str
                        or row['video_type'] not in ('short','stream') or row['video_id'] in seen):_shape()
                seen.add(row['video_id'])
                target={key:row[key] for key in ('video_id','channel_id','video_type','published_at','published_acquired_at')}
                targets.append(target)
                manual[row['video_id']]=row['manual']
                if row['restore_type']:restore.append(target)
            restored={}
            if restore:
                restored=_rows_by_id(transaction.returning_all(_RESTORE,(Jsonb(restore),),max_rows=len(restore)),[row['video_id'] for row in restore],('video_id',),complete=False)
            remaining=[row for row in targets if row['video_id'] not in restored]
            manual_targets=[row for row in remaining if manual[row['video_id']]]
            if manual_targets:_manual(transaction,manual_targets)
            deleted=[row['video_id'] for row in remaining if not manual[row['video_id']]]
            if deleted:transaction.execute(_DELETE,(deleted,))
            _review(transaction,targets)
