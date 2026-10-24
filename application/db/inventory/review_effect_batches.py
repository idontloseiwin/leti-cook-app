"""Finite independent review effects under each video's existing advisory owner.

Page settlement already holds every input advisory lock. ABSENT acquires these
same locks only after its complete source UPDATE. Batches contain at most50
unique videos in original source order. No table lock or repair assumption is
imported. PostgreSQL keeps evidence clocks, array DISTINCT, case defaults and
conflict-time revision/decision values; Python selects the conflict write set.
"""
import re
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseError
from ..reviews import video_review_evidence as original

_MAX=50
_IDS="unnest(%s::text[]) WITH ORDINALITY input(video_id,ordinality)"
_ADVISORY="SELECT pg_advisory_xact_lock(hashtextextended(input.video_id,962106)) AS acquired FROM "+_IDS+' ORDER BY input.ordinality'
def _named(statement):
    return re.sub(r'%\(([^)]+)\)s',lambda match:'source.'+match[1],statement)
_FACTS="""SELECT input.ordinality,source.video_id,source.channel_id,source.video_type,evidence.types,evidence.channels,evidence.evidence_expires_at,
 blocked.blocked,EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=source.video_id) AS present
 FROM """+_IDS+""" JOIN public.video_inventory source ON source.video_id=input.video_id
 CROSS JOIN LATERAL ("""+original._EVIDENCE.replace('%s::text','source.video_id')+""") evidence
 CROSS JOIN LATERAL ("""+_named(original._BLOCKED).replace('source.channels','evidence.channels')+""") blocked
 WHERE source.skeleton_since IS NULL OR EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=source.video_id)
 ORDER BY input.ordinality"""
_INPUT="jsonb_to_recordset(%s::jsonb) source(ordinality bigint,video_id text,channel_id text,video_type text,removed boolean,types text[],channels text[],blocked text,evidence_expires_at timestamptz)"
_values,_conflict=original._UPSERT.split(' VALUES(',1)[1].rsplit(')\n ON CONFLICT',1)
_UPSERT=original._UPSERT.split(' VALUES(',1)[0]+' SELECT '+_named(_values)+' FROM '+_INPUT+' WHERE true ORDER BY source.ordinality\n ON CONFLICT'+_conflict.replace('%(removed)s::boolean','false')
_WITHDRAW="""UPDATE public.video_type_review_cases c SET observed_canonical_type=NULL,
 observed_types=ARRAY[]::text[],observed_channels=ARRAY[]::text[],blocked_reason=NULL,evidence_expires_at=NULL,
 revision=c.revision+CASE WHEN c.observed_canonical_type IS NOT NULL OR cardinality(c.observed_types)>0
 OR cardinality(c.observed_channels)>0 OR c.blocked_reason IS NOT NULL THEN 1 ELSE 0 END
 FROM (SELECT c.case_id FROM """+_IDS+""" JOIN public.video_inventory i ON i.video_id=input.video_id
 JOIN public.video_type_review_cases c ON c.video_id=i.video_id ORDER BY input.ordinality FOR UPDATE OF c) held
 WHERE c.case_id=held.case_id"""


def _identities(ids):
    if type(ids) is not list or len(ids)>_MAX or any(type(value) is not str for value in ids) or len(set(ids))!=len(ids):
        raise ValueError('review_batch_ids_invalid')
    return ids


def _shape():raise DatabaseError('database_result_shape_invalid')


def _lock(tx,ids):
    rows=tx.fetch_all(_ADVISORY,(ids,),max_rows=len(ids))
    if len(rows)!=len(ids):_shape()


def refresh_review_cases_batch(transaction,video_ids):
    ids=_identities(video_ids)
    if not ids:return
    with transaction.borrow():
        _lock(transaction,ids)
        rows=transaction.fetch_all(_FACTS,(ids,),max_rows=len(ids))
        seen=set()
        plans=[]
        for fact in rows:
            if (type(fact) is not dict or set(fact)!={'ordinality','video_id','channel_id','video_type','types','channels','blocked','present','evidence_expires_at'}
                    or type(fact['ordinality']) is not int or not 1<=fact['ordinality']<=len(ids)
                    or fact['video_id']!=ids[fact['ordinality']-1] or fact['video_id'] in seen
                    or type(fact['channel_id']) is not str or fact['video_type'] not in (None,'long','short','stream')
                    or type(fact['types']) is not list or type(fact['channels']) is not list
                    or any(type(value) is not str for value in fact['types']+fact['channels'])
                    or fact['evidence_expires_at'] is not None and type(fact['evidence_expires_at']) is not str
                    or type(fact['present']) is not bool or fact['blocked'] not in (None,'channel_conflict')):_shape()
            seen.add(fact['video_id'])
            conflict=(len(fact['types'])>1 or fact['blocked'] is not None or bool(fact['types']) and
                      (fact['video_type'] is None or fact['video_type'] not in fact['types']))
            if conflict is False and not fact['present']:continue
            plans.append({key:fact[key] for key in ('ordinality','video_id','channel_id','video_type','types','channels','blocked','evidence_expires_at')}|{'removed':False})
        if plans:
            count=transaction.execute(_UPSERT,(Jsonb(plans),))
            if type(count) is not int or count!=len(plans):_shape()


def withdraw_review_cases_batch(transaction,video_ids):
    ids=_identities(video_ids)
    if not ids:return
    with transaction.borrow():
        _lock(transaction,ids)
        count=transaction.execute(_WITHDRAW,(ids,))
        if type(count) is not int or not 0<=count<=len(ids):_shape()
