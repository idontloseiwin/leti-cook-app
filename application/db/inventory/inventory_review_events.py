"""Finite inventory/membership row effects in original trigger order.

Inputs are actual native OLD/NEW row texts, after their source statement. This
module does not capture arbitrary batches or dispatch unobserved/cascaded rows.
The caller preserves statement boundaries, actual row event order and receipts.
Source owners route queue and review effects together at this boundary. The
owner binder and all native integrity guards remain enabled.
"""
from collections.abc import Mapping
from ..base.postgres import DatabaseError
from .inventory_incident_queue_effects import after_inventory_write
from ..reviews.video_review_evidence import refresh_review_case

_INVENTORY_FACTS="""WITH previous AS(SELECT (jsonb_populate_record(NULL::public.video_inventory,%s::jsonb)).*),
 incoming AS(SELECT (jsonb_populate_record(NULL::public.video_inventory,%s::jsonb)).*)
 SELECT incoming.video_id,previous.video_id AS old_video_id,
 incoming.video_id=previous.video_id AND incoming.channel_id=previous.channel_id AND incoming.video_type IS NULL AS withdrawal,
 incoming.video_id=previous.video_id AND incoming.channel_id=previous.channel_id AS same_identity,
 previous.video_id IS DISTINCT FROM incoming.video_id AS identity_changed FROM previous CROSS JOIN incoming"""
_DECISION="""SELECT EXISTS(SELECT 1 FROM public.video_type_review_cases c CROSS JOIN
 jsonb_populate_record(NULL::public.video_inventory,%s::jsonb) incoming WHERE c.video_id=incoming.video_id
 AND c.channel_id=incoming.channel_id AND c.decision_type=incoming.video_type
 AND c.observed_canonical_type=incoming.video_type AND c.blocked_reason IS NULL) AS present"""
_MEMBER_FACTS="""WITH previous AS(SELECT (jsonb_populate_record(NULL::public.video_inventory_memberships,%s::jsonb)).*),
 incoming AS(SELECT (jsonb_populate_record(NULL::public.video_inventory_memberships,%s::jsonb)).*)
 SELECT incoming.video_id,previous.video_id AS old_video_id,
 (to_jsonb(incoming)-ARRAY['first_seen_at','first_seen_scan_id','last_present_at','source_published_at'])
 =(to_jsonb(previous)-ARRAY['first_seen_at','first_seen_scan_id','last_present_at','source_published_at'])
 AND (incoming.first_seen_at IS NULL OR incoming.first_seen_at IS NOT DISTINCT FROM previous.first_seen_at)
 AND (incoming.first_seen_scan_id IS NULL OR incoming.first_seen_scan_id IS NOT DISTINCT FROM previous.first_seen_scan_id)
 AND (incoming.last_present_at IS NULL OR incoming.last_present_at IS NOT DISTINCT FROM previous.last_present_at)
 AND (incoming.source_published_at IS NULL OR incoming.source_published_at IS NOT DISTINCT FROM previous.source_published_at) AS withdrawal,
 previous.video_id IS DISTINCT FROM incoming.video_id AS identity_changed FROM previous CROSS JOIN incoming"""
_CAN_RESTORE="""SELECT incoming.membership_status='present' AND isfinite(incoming.last_present_at)
 AND incoming.last_present_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND isfinite(incoming.last_seen_at)
 AND incoming.last_seen_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND EXISTS(SELECT 1 FROM public.video_inventory i WHERE i.video_id=incoming.video_id
 AND i.channel_id=incoming.channel_id AND i.video_type IS NULL)
 AND NOT EXISTS(SELECT 1 FROM public.video_inventory_memberships m WHERE m.video_id=incoming.video_id
 AND m.membership_status='present' AND isfinite(m.last_present_at)
 AND m.last_present_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND isfinite(m.last_seen_at)
 AND m.last_seen_at BETWEEN clock_timestamp()-interval '696 hours' AND clock_timestamp()
 AND (m.channel_id<>incoming.channel_id OR m.video_type<>incoming.video_type)) AS eligible
 FROM jsonb_populate_record(NULL::public.video_inventory_memberships,%s::jsonb) incoming"""
_QUEUE_LOCK='SELECT video_id FROM public.video_collection_state WHERE video_id=%s::text FOR UPDATE NOWAIT'
_QUEUE_LIVE="""SELECT EXISTS(SELECT 1 FROM public.video_collection_state WHERE video_id=%s::text
 AND lease_token IS NOT NULL AND lease_expires_at>clock_timestamp()) AS live"""
# One fixed statement preserves the original predicate and two SET targets. The
# materialized locking source captures latest OLD under the original UPDATE's
# non-key lock; RETURNING carries the actual NEW without a later table rescan.
# All guards execute on the real UPDATE, and a raced nonmatching row emits none.
_RESTORE="""UPDATE public.video_inventory i
 SET video_type=held.video_type,published_at=held.source_published_at,
 published_acquired_at=held.published_acquired_at,skeleton_since=NULL
 FROM (WITH incoming AS MATERIALIZED(
 SELECT (jsonb_populate_record(NULL::public.video_inventory_memberships,%s::jsonb)).*),
 previous AS MATERIALIZED(SELECT i.video_id,to_jsonb(i)::text AS old_text
 FROM public.video_inventory i CROSS JOIN incoming
 WHERE i.video_id=incoming.video_id AND i.channel_id=incoming.channel_id AND i.video_type IS NULL
 FOR NO KEY UPDATE OF i)
 SELECT previous.video_id,previous.old_text,incoming.channel_id,incoming.video_type,incoming.source_published_at,
 CASE WHEN incoming.source_published_at IS NOT NULL THEN incoming.last_present_at END AS published_acquired_at
 FROM incoming CROSS JOIN previous) held
 WHERE i.video_id=held.video_id AND i.channel_id=held.channel_id AND i.video_type IS NULL
 RETURNING held.old_text,to_jsonb(i)::text AS new_text"""


def _input(operation,old_text,new_text,*,membership=False):
    choices=('insert','update','delete') if membership else ('insert','update')
    if (type(operation) is not str or operation not in choices
            or (old_text is not None and type(old_text) is not str)
            or (new_text is not None and type(new_text) is not str)
            or (operation=='insert' and old_text is not None)
            or (operation!='insert' and old_text is None)
            or (operation=='delete' and new_text is not None)
            or (operation!='delete' and new_text is None)):
        raise ValueError('inventory_review_event_invalid')


def _row(value,keys):
    if not isinstance(value,Mapping) or set(value)!=set(keys):
        raise DatabaseError('database_result_shape_invalid')
    return value


def _facts(tx,statement,old_text,new_text,*,inventory=False):
    names=('video_id','old_video_id','withdrawal','same_identity','identity_changed') if inventory else ('video_id','old_video_id','withdrawal','identity_changed')
    row=_row(tx.fetch_one(statement,(old_text,new_text)),names)
    for key in ('video_id','old_video_id'):
        if row[key] is not None and type(row[key]) is not str:
            raise DatabaseError('database_result_shape_invalid')
    for key in names[2:]:
        if row[key] is not None and type(row[key]) is not bool:
            raise DatabaseError('database_result_shape_invalid')
    return row


def _inventory_review(tx,operation,old_text,new_text):
    facts=_facts(tx,_INVENTORY_FACTS,old_text,new_text,inventory=True)
    if operation=='update':
        if facts['withdrawal'] is True:
            refresh_review_case(tx,facts['video_id'],source_removed=True)
            return
        if facts['same_identity'] is True:
            decision=_row(tx.fetch_one(_DECISION,(new_text,)),('present',))['present']
            if decision is True:
                return
    refresh_review_case(tx,facts['video_id'])
    if operation=='update' and facts['identity_changed'] is True:
        refresh_review_case(tx,facts['old_video_id'])


def after_inventory_source_write(transaction,*,operation,old_text=None,new_text):
    """One observed INSERT / UPDATE OF video_type,channel_id; queue then review."""
    _input(operation,old_text,new_text)
    with transaction.borrow():
        after_inventory_write(transaction,operation=operation,old_text=old_text,new_text=new_text)
        _inventory_review(transaction,operation,old_text,new_text)


def after_membership_source_write(transaction,*,operation,old_text=None,new_text=None):
    """One observed membership event, including its nested canonical UPDATE."""
    _input(operation,old_text,new_text,membership=True)
    tx=transaction
    with tx.borrow():
        facts=_facts(tx,_MEMBER_FACTS,old_text,new_text)
        if operation=='delete':
            refresh_review_case(tx,facts['old_video_id'],source_removed=True)
            return
        if operation=='update' and facts['withdrawal'] is True:
            refresh_review_case(tx,facts['video_id'],source_removed=True)
            return
        can_restore=_row(tx.fetch_one(_CAN_RESTORE,(new_text,)),('eligible',))['eligible']
        if can_restore is True:
            tx.fetch_one(_QUEUE_LOCK,(facts['video_id'],))
            live=_row(tx.fetch_one(_QUEUE_LIVE,(facts['video_id'],)),('live',))['live']
            if live is False:
                receipt=tx.returning_one(_RESTORE,(new_text,))
                if receipt is not None:
                    receipt=_row(receipt,('old_text','new_text'))
                    after_inventory_source_write(tx,operation='update',**receipt)
        from .automatic_type_reconciliation import reconcile_automatic_video_types
        reconcile_automatic_video_types(tx,[facts["video_id"]])
        from ..videos.video_queue_initialization import ensure_video_collection_state_in_transaction, ensure_typed_collection_state_in_transaction
        ensure_typed_collection_state_in_transaction(tx,[facts["video_id"]])
        refresh_review_case(tx,facts['video_id'])
        if operation=='update' and facts['identity_changed'] is True:
            refresh_review_case(tx,facts['old_video_id'])
