"""Per-row inventory/incident AFTER effect in the caller transaction.

Inputs are full native OLD/NEW JSONB text after the original typed source DML.
The caller must preserve its row event order, locks and statement atomicity;
this module neither captures a batch nor claims a generic writer replacement.
Source owners compose this effect with nested membership/review effects; native
integrity guards remain on the source statements.
"""
from collections.abc import Mapping
from ..base.postgres import DatabaseError
from ..videos.video_queue_initialization import ensure_video_collection_state_in_transaction
from ..reviews.video_type_review_decisions import apply_manual_video_type_queue

_INVENTORY_FACTS="""WITH previous AS(SELECT (jsonb_populate_record(NULL::public.video_inventory,%s::jsonb)).*),
 incoming AS(SELECT (jsonb_populate_record(NULL::public.video_inventory,%s::jsonb)).*)
 SELECT incoming.video_id,incoming.channel_id,incoming.video_type,to_jsonb(incoming.published_at)#>>'{}' AS published_at,
 to_jsonb(incoming.published_acquired_at)#>>'{}' AS published_acquired_at,
 incoming.video_id=previous.video_id AND incoming.channel_id=previous.channel_id AND incoming.video_type IS NULL AS withdrawal,
 previous.video_type IS NULL AND incoming.video_type IN('long','short','stream') AND incoming.channel_id=previous.channel_id AS restore_type,
 incoming.channel_id IS DISTINCT FROM previous.channel_id OR incoming.video_type IS DISTINCT FROM previous.video_type AS changed
 FROM previous CROSS JOIN incoming"""
_RESTORE_TYPE="""UPDATE public.video_collection_state SET video_type=%(video_type)s::text,
 typed_collection_status=CASE WHEN %(video_type)s::text='long' THEN NULL ELSE coalesce(typed_collection_status,'pending') END,
 typed_collection_checked_at=CASE WHEN %(video_type)s::text='long' THEN NULL ELSE typed_collection_checked_at END,
 typed_source_published_at=CASE WHEN %(video_type)s::text<>'long' THEN %(published_at)s::timestamptz END,
 typed_source_published_acquired_at=CASE WHEN %(video_type)s::text<>'long' THEN %(published_acquired_at)s::timestamptz END,
 typed_stream_actual_end_at=CASE WHEN %(video_type)s::text='stream' THEN typed_stream_actual_end_at END,
 typed_source_stream_end_acquired_at=CASE WHEN %(video_type)s::text='stream' THEN typed_source_stream_end_acquired_at END,
 paused_reason=paused_reason
 WHERE video_id=%(video_id)s::text AND (lease_token IS NULL OR lease_expires_at<=clock_timestamp())"""
_STATE="SELECT video_type FROM public.video_collection_state WHERE video_id=%s::text"
_MANUAL="""SELECT EXISTS(SELECT 1 FROM public.video_type_review_cases
 WHERE video_id=%(video_id)s::text AND channel_id=%(channel_id)s::text AND decision_type=%(video_type)s::text) AS present"""
_DELETE="DELETE FROM public.video_collection_state WHERE video_id=%s::text AND availability_first_missing_at IS NULL AND availability_resume_observed_at IS NULL"
_INCIDENT_FACTS="""WITH previous AS(SELECT (jsonb_populate_record(NULL::public.video_availability_incidents,%s::jsonb)).*),
 incoming AS(SELECT (jsonb_populate_record(NULL::public.video_availability_incidents,%s::jsonb)).*)
 SELECT previous.video_id AS old_video_id,incoming.video_id AS new_video_id,
 previous.restored_at IS NOT NULL AS old_closed,incoming.restored_at IS NOT NULL AS new_closed,
 incoming.video_id IS NOT DISTINCT FROM previous.video_id AND incoming.channel_id IS NOT DISTINCT FROM previous.channel_id
 AND incoming.restored_at IS NOT DISTINCT FROM previous.restored_at
 AND incoming.first_seen_at IS NOT DISTINCT FROM previous.first_seen_at
 AND incoming.last_seen_at IS NOT DISTINCT FROM previous.last_seen_at
 AND incoming.attempt_count IS NOT DISTINCT FROM previous.attempt_count
 AND (incoming.first_status IS NULL OR incoming.first_status IS NOT DISTINCT FROM previous.first_status)
 AND (incoming.source_first_seen IS NULL OR incoming.source_first_seen IS NOT DISTINCT FROM previous.source_first_seen)
 AND (incoming.latest_status IS NULL OR incoming.latest_status IS NOT DISTINCT FROM previous.latest_status)
 AND (incoming.source_latest_seen IS NULL OR incoming.source_latest_seen IS NOT DISTINCT FROM previous.source_latest_seen)
 AND (incoming.restored_status IS NULL OR incoming.restored_status IS NOT DISTINCT FROM previous.restored_status)
 AND (incoming.source_restored IS NULL OR incoming.source_restored IS NOT DISTINCT FROM previous.source_restored) AS withdrawal
 FROM previous CROSS JOIN incoming"""
# Same native control projection already accepted in the manual queue helper.
_CHANNEL_PAUSE="""SELECT CASE WHEN coalesce(control.is_hidden,true) THEN 'channel_hidden'
 ELSE CASE coalesce(mode.collection_mode,'operator_hold') WHEN 'active' THEN NULL
 WHEN 'metadata_only' THEN 'channel_no_long_playlist' WHEN 'initial_inventory' THEN 'channel_inventory_not_ready'
 ELSE CASE control.owner_permission WHEN 'manual_required' THEN 'channel_inventory_manual_required'
 ELSE 'channel_inventory_not_ready' END END END AS reason
 FROM public.video_collection_state state JOIN public.channels channel ON channel.channel_id=state.channel_id
 LEFT JOIN public.channel_collection_state control ON control.channel_id=channel.channel_id
 LEFT JOIN public.channel_collection_control mode ON mode.channel_id=channel.channel_id
 WHERE state.video_id=%s::text AND state.video_type='long'"""
_PAUSE="""UPDATE public.video_collection_state SET next_due_at=NULL,paused_reason=%s::text,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,updated_at=now()
 WHERE video_id=%s::text AND video_type='long' AND availability_first_missing_at IS NULL AND availability_resume_observed_at IS NULL
 AND (paused_reason IS NULL OR paused_reason LIKE 'channel_%%')"""


def _row(value,keys):
    if not isinstance(value,Mapping) or set(value)!=set(keys):raise DatabaseError('database_result_shape_invalid')
    return value


def _text(value):
    if value is not None and type(value) is not str:raise DatabaseError('database_result_shape_invalid')
    return value


def _flag(value,*,nullable=False):
    if type(value) is not bool and not(nullable and value is None):raise DatabaseError('database_result_shape_invalid')
    return value is True


def _receipt(value):
    if type(value) is not int or value not in (0,1):raise DatabaseError('database_result_shape_invalid')
    return value


def _input(operation,old_text,new_text,*,inventory=False):
    allowed=('insert','update') if inventory else ('insert','update','delete')
    if (type(operation) is not str or operation not in allowed
            or (old_text is not None and type(old_text) is not str)
            or (new_text is not None and type(new_text) is not str)
            or (operation=='insert' and old_text is not None)
            or (operation!='insert' and old_text is None)
            or (operation=='delete' and new_text is not None)
            or (operation!='delete' and new_text is None)):
        raise ValueError('inventory_incident_queue_event_invalid')


def _state_type(tx,video_id):
    row=tx.fetch_one(_STATE,(video_id,))
    return None if row is None else _text(_row(row,('video_type',))['video_type'])


def _ordinary(tx,video_id,state_type):
    if state_type in ('short','stream'):return
    if video_id is None:return
    ensure_video_collection_state_in_transaction(tx,[video_id])
    row=tx.fetch_one(_CHANNEL_PAUSE,(video_id,))
    if row is None:return
    reason=_text(_row(row,('reason',))['reason'])
    if reason is not None:_receipt(tx.execute(_PAUSE,(reason,video_id)))


def after_inventory_write(transaction,*,operation,old_text=None,new_text):
    """One actual INSERT or UPDATE OF video_type/channel_id event; no receipt."""
    _input(operation,old_text,new_text,inventory=True)
    tx=transaction
    with tx.borrow():
        facts=_row(tx.fetch_one(_INVENTORY_FACTS,(old_text,new_text)),
            ('video_id','channel_id','video_type','published_at','published_acquired_at','withdrawal','restore_type','changed'))
        args={name:_text(facts[name]) for name in ('video_id','channel_id','video_type','published_at','published_acquired_at')}
        withdrawal=_flag(facts['withdrawal'],nullable=True)
        restore=_flag(facts['restore_type'],nullable=True)
        changed=_flag(facts['changed'])
        if operation=='update':
            if withdrawal:return
            if restore and _receipt(tx.execute(_RESTORE_TYPE,args))==1:return
        state_type=_state_type(tx,args['video_id'])
        if operation=='update' and changed:
            if _flag(_row(tx.fetch_one(_MANUAL,args),('present',))['present']):
                apply_manual_video_type_queue(tx,video_id=args['video_id'],channel_id=args['channel_id'],video_type=args['video_type'])
                return
            # The retained retry episode survives a type correction. A live
            # lease still prevents reclassification by the existing guards.
            if tx.execute("UPDATE public.video_collection_state SET video_type=%(video_type)s::text WHERE video_id=%(video_id)s::text AND channel_id=%(channel_id)s::text AND (availability_first_missing_at IS NOT NULL OR availability_resume_observed_at IS NOT NULL) AND (lease_token IS NULL OR lease_expires_at<=clock_timestamp())",args):
                return
            _receipt(tx.execute(_DELETE,(args['video_id'],)))
            state_type=None
        if args['video_type'] in ('short','stream'):return
        _ordinary(tx,args['video_id'],state_type)


def after_availability_incident_write(transaction,*,operation,old_text=None,new_text=None):
    """One actual incident INSERT/UPDATE/DELETE event after the source DML."""
    _input(operation,old_text,new_text)
    tx=transaction
    with tx.borrow():
        facts=_row(tx.fetch_one(_INCIDENT_FACTS,(old_text,new_text)),
            ('old_video_id','new_video_id','old_closed','new_closed','withdrawal'))
        old_id=_text(facts['old_video_id'])
        new_id=_text(facts['new_video_id'])
        old_closed=_flag(facts['old_closed'])
        new_closed=_flag(facts['new_closed'])
        withdrawal=_flag(facts['withdrawal'])
        if operation=='update' and withdrawal:return
        if operation=='delete' and old_closed:return
        if operation!='delete' and new_closed and (operation=='insert' or old_closed):return
        video_id=old_id if operation=='delete' else new_id
        # Incident history does not own the queue or reset unavailable episodes.
        return
