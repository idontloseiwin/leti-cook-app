"""Finite canonical UPDATE effects after this owner's locked decision write.

The decision owner already holds advisory, queue, inventory and case locks, has
written the matching decision/observed type and checks the final channel fence.
Actual OLD/NEW retain native casts. The existing NULL-to-type queue restoration
has priority; every other admitted change uses the original manual queue owner.
The native review predicate is proved before omitting its redundant no-op reads.
"""
from ..base.postgres import DatabaseError
from .inventory_incident_queue_effects import _RESTORE_TYPE
from ..reviews.video_type_review_decisions import _apply_manual_video_type_queue_after_decision

_FACTS="""WITH previous AS(SELECT (jsonb_populate_record(NULL::public.video_inventory,%s::jsonb)).*),
 incoming AS(SELECT (jsonb_populate_record(NULL::public.video_inventory,%s::jsonb)).*)
 SELECT incoming.video_id,incoming.channel_id,incoming.video_type,
 incoming.published_at::text AS published_at,incoming.published_acquired_at::text AS published_acquired_at,previous.video_type IS NULL AS restore_type,
 previous.video_id=incoming.video_id AND previous.channel_id=incoming.channel_id
 AND previous.video_type IS DISTINCT FROM incoming.video_type
 AND incoming.video_type IN('long','short','stream')
 AND to_jsonb(previous)-ARRAY['video_type','updated_at']=to_jsonb(incoming)-ARRAY['video_type','updated_at'] AS exact,
 EXISTS(SELECT 1 FROM public.video_type_review_cases c WHERE c.video_id=incoming.video_id
 AND c.channel_id=incoming.channel_id AND c.decision_type=incoming.video_type
 AND c.observed_canonical_type IS NOT DISTINCT FROM (CASE WHEN isfinite(c.evidence_expires_at)
 AND c.evidence_expires_at>c.decision_at AND c.evidence_expires_at<=c.decision_at+interval '696 hours'
 THEN incoming.video_type END) AND c.blocked_reason IS NULL) AS review_skip
 FROM previous CROSS JOIN incoming"""


def after_manual_decision_inventory(transaction,*,old_text,new_text):
    if type(old_text) is not str or type(new_text) is not str:
        raise ValueError('manual_decision_effect_invalid')
    with transaction.borrow():
        facts=transaction.fetch_one(_FACTS,(old_text,new_text))
        if (type(facts) is not dict or set(facts)!={'video_id','channel_id','video_type','published_at','published_acquired_at','restore_type','exact','review_skip'}
                or facts['exact'] is not True or facts['review_skip'] is not True
                or type(facts['restore_type']) is not bool or type(facts['video_id']) is not str
                or type(facts['channel_id']) is not str or facts['video_type'] not in ('long','short','stream')
                or any(facts[key] is not None and type(facts[key]) is not str for key in ('published_at','published_acquired_at'))):
            raise DatabaseError('database_result_shape_invalid')
        args={key:facts[key] for key in ('video_id','channel_id','video_type','published_at','published_acquired_at')}
        if facts['restore_type']:
            restored=transaction.execute(_RESTORE_TYPE,args)
            if type(restored) is not int or restored not in (0,1):raise DatabaseError('database_result_shape_invalid')
            if restored==1:return
        _apply_manual_video_type_queue_after_decision(transaction,{key:args[key] for key in ('video_id','channel_id','video_type')})
