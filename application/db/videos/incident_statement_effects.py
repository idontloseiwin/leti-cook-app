"""Finite incident batch effects at the original source statement boundary.

Each receipt carries actual native OLD/NEW text. The supported owners have at
most one effective event per video in a statement (the open-incident unique
index and their existing ownership locks prove that). Source constraints and
all native casts have already run. The caller retains its original lock order,
receipts, deadline and final fences; this module never commits or retries.
"""
from psycopg.types.json import Jsonb
from ..base.postgres import DatabaseError
from ..inventory.inventory_incident_queue_effects import _INCIDENT_FACTS

_NATIVE_FACTS = _INCIDENT_FACTS.replace('%s::jsonb','event.old_text::jsonb',1).replace('%s::jsonb','event.new_text::jsonb',1)
_FACTS = """SELECT raw.ordinality,event.operation,facts.*,state.video_type AS state_type
 FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY raw(value,ordinality)
 CROSS JOIN LATERAL jsonb_to_record(raw.value) event(operation text,old_text text,new_text text)
 CROSS JOIN LATERAL (""" + _NATIVE_FACTS + """) facts
 LEFT JOIN public.video_collection_state state ON state.video_id=
 CASE WHEN event.operation='delete' THEN facts.old_video_id ELSE facts.new_video_id END
 ORDER BY raw.ordinality"""


def _shape():
    raise DatabaseError('database_result_shape_invalid')


def after_incident_statement(transaction, events):
    """Replay one <=1000-row original statement, with no duplicate active video."""
    if type(events) is not list or len(events) > 1000:
        raise ValueError('incident_statement_events_invalid')
    from ..inventory.inventory_incident_queue_effects import _input
    for event in events:
        if type(event) is not dict or set(event) != {'operation','old_text','new_text'}:
            raise ValueError('incident_statement_events_invalid')
        _input(**event)
    with transaction.borrow():
        if not events:
            return
        facts=transaction.fetch_all(_FACTS,(Jsonb(events),),max_rows=len(events))
        if len(facts) != len(events):
            _shape()
        active=[]
        for index,row in enumerate(facts,1):
            if set(row) != {'ordinality','operation','old_video_id','new_video_id','old_closed','new_closed','withdrawal','state_type'}:
                _shape()
            if row['ordinality'] != index or row['operation'] != events[index-1]['operation']:
                _shape()
            for name in ('old_closed','new_closed','withdrawal'):
                if type(row[name]) is not bool:
                    _shape()
            for name in ('old_video_id','new_video_id','state_type'):
                if row[name] is not None and type(row[name]) is not str:
                    _shape()
            operation=row['operation']
            if operation=='update' and row['withdrawal']:
                continue
            if operation=='delete' and row['old_closed']:
                continue
            if operation!='delete' and row['new_closed'] and (operation=='insert' or row['old_closed']):
                continue
            if row['state_type'] in ('short','stream'):
                continue
            video_id=row['old_video_id'] if operation=='delete' else row['new_video_id']
            if video_id is None:
                # A native incident video_id is NOT NULL; reject corrupt receipts.
                _shape()
            if video_id in active:
                _shape()
            active.append(video_id)
        # History insertion, closure and retention removal never own scheduling.
        # The successful General completion settles the same locked queue row.
        return
