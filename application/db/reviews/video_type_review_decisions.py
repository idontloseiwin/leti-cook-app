"""Manual decision transaction with explicit inventory queue/review effects.

Replay precedes current source/lease checks. Savepoint recovery converts only
known NOWAIT55P03 to busy; final scope loss rolls back all. Canonical type
changes invoke existing inventory effects once, including its NULL-to-type
early branch. First same-canonical-type decisions use the borrowed manual queue
operation below. No business RPC, automatic retry or new grants.
"""
from ..videos.typed_video_read_policy import (TYPED_IDENTITY_NAMED_SQL, typed_identity_row)
from ..channel.channel_read_policy import (CHANNEL_FACTS_RELATION_SQL, channel_pause_row)
from contextlib import contextmanager
from ..channel.channel_control import assert_channel_scope
from ..base.operation_contracts import get_operation
from ..base.postgres import DatabaseError,DatabaseServerError,Operation
from .video_type_review_queries import get_case_in_transaction, _CASE_FROM

_INPUT="""WITH args AS MATERIALIZED(SELECT %s::uuid AS case_id,%s::uuid AS request_id,
 %s::bigint AS expected_revision,%s::text AS video_type,btrim(%s::text) AS reason)
 SELECT case_id::text,request_id::text,expected_revision,video_type,reason,
 case_id IS NULL OR request_id IS NULL
 OR request_id::text !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
 OR expected_revision IS NULL OR expected_revision NOT BETWEEN 1 AND 9007199254740990
 OR video_type IS NULL OR video_type NOT IN('long','short','stream')
 OR reason IS NULL OR char_length(reason) NOT BETWEEN 1 AND 1000 OR reason ~ '[[:cntrl:]]' AS invalid
 FROM args"""
_CASE="SELECT video_id,channel_id,revision,decision_type FROM public.video_type_review_cases WHERE case_id=%s::uuid"
_ADVISORY="SELECT pg_try_advisory_xact_lock(hashtextextended(%s::text,962106)) AS acquired"
_RECEIPT="""SELECT revision,expected_revision<>%(expected_revision)s::bigint OR video_type<>%(video_type)s::text
 OR reason<>%(reason)s::text AS conflict FROM public.video_type_review_history
 WHERE case_id=%(case_id)s::uuid AND request_id=%(request_id)s::uuid"""
_QUEUE="SELECT video_id,channel_id,to_jsonb(s)::text AS value FROM public.video_collection_state s WHERE video_id=%s::text FOR UPDATE NOWAIT"
_QUEUE_LIVE="""SELECT s.lease_token IS NOT NULL AND s.lease_expires_at>clock_timestamp() AS live
 FROM jsonb_populate_record(NULL::public.video_collection_state,%s::jsonb) s"""
_INVENTORY="SELECT video_id,channel_id,video_type,to_jsonb(i)::text AS effect_old FROM public.video_inventory i WHERE video_id=%s::text FOR UPDATE NOWAIT"
_CASE_LOCK=_CASE+' FOR UPDATE NOWAIT'
_CONFLICT="""SELECT EXISTS(SELECT 1 FROM public.video_inventory_memberships m
 WHERE m.video_id=%(video_id)s::text AND m.membership_status='present' AND m.channel_id<>%(channel_id)s::text)
 OR EXISTS(SELECT 1 FROM public.videos v WHERE v.video_id=%(video_id)s::text AND v.channel_id<>%(channel_id)s::text) AS blocked"""
_ELIGIBILITY="SELECT presentation.review_status"+_CASE_FROM+" WHERE c.case_id=%s::uuid"
_CONTEXT="""SELECT (SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s::text) AS epoch,
 clock_timestamp()::text AS decided_at,%s::bigint+1 AS revision"""
_DECISION="""UPDATE public.video_type_review_cases SET decision_type=%(video_type)s::text,decision_reason=%(reason)s::text,
 decision_at=%(decided_at)s::timestamptz,decision_revision=%(revision)s::bigint,revision=%(revision)s::bigint,
 observed_canonical_type=CASE WHEN isfinite(evidence_expires_at) AND evidence_expires_at>%(decided_at)s::timestamptz AND evidence_expires_at<=%(decided_at)s::timestamptz+interval '696 hours' THEN %(video_type)s::text END,state='resolved',blocked_reason=NULL WHERE case_id=%(case_id)s::uuid"""
_HISTORY="""INSERT INTO public.video_type_review_history(case_id,revision,video_type,reason,decided_at,request_id,expected_revision)
 VALUES(%(case_id)s::uuid,%(revision)s::bigint,%(video_type)s::text,%(reason)s::text,%(decided_at)s::timestamptz,
 %(request_id)s::uuid,%(expected_revision)s::bigint)"""
_CANONICAL="""UPDATE public.video_inventory SET video_type=%(video_type)s::text,skeleton_since=NULL,updated_at=%(decided_at)s::timestamptz
 WHERE video_id=%(video_id)s::text AND video_type IS DISTINCT FROM %(video_type)s::text"""
_CANONICAL_EFFECT=_CANONICAL+" RETURNING to_jsonb(video_inventory)::text AS value"
_QUEUE_DECISION="""SELECT EXISTS(SELECT 1 FROM public.video_type_review_cases WHERE video_id=%(video_id)s::text
 AND channel_id=%(channel_id)s::text AND decision_type=%(video_type)s::text) AS present"""
_QUEUE_SOURCE="""SELECT i.published_at::text AS published_at,i.published_acquired_at::text AS published_acquired_at,
 v.stream_actual_end_at::text AS stream_end,
 CASE WHEN v.stream_actual_end_at IS NOT NULL THEN v.metadata_checked_at::text END AS stream_end_acquired_at,
 v.video_id IS NOT NULL AS has_current,v.availability_status
 FROM public.video_inventory i LEFT JOIN public.videos v ON v.video_id=i.video_id WHERE i.video_id=%(video_id)s::text"""
_QUEUE_LONG=('SELECT EXISTS(SELECT 1 FROM public.open_video_availability WHERE video_id=%(video_id)s::text) AS open_incident,(SELECT policy_facts FROM (' + CHANNEL_FACTS_RELATION_SQL + ') ch WHERE ch.channel_id=%(channel_id)s::text) AS policy_facts')
_QUEUE_ELIGIBLE=TYPED_IDENTITY_NAMED_SQL
_QUEUE_INSERT="""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,typed_collection_status)
 VALUES(%(video_id)s::text,%(channel_id)s::text,%(video_type)s::text,
 CASE WHEN %(video_type)s::text<>'long' THEN 'pending' END) ON CONFLICT(video_id) DO NOTHING"""
_QUEUE_UPDATE="""UPDATE public.video_collection_state s SET channel_id=%(channel_id)s::text,video_type=%(video_type)s::text,
 metadata_due_at=CASE WHEN (s.availability_first_missing_at IS NOT NULL OR s.availability_resume_observed_at IS NOT NULL)
 OR s.paused_reason='availability_retry_expired' THEN s.metadata_due_at
 WHEN %(pause)s::text IS NULL THEN coalesce(s.metadata_due_at,now()) ELSE s.metadata_due_at END,
 next_due_at=CASE WHEN (s.availability_first_missing_at IS NOT NULL OR s.availability_resume_observed_at IS NOT NULL)
 OR s.paused_reason='availability_retry_expired' THEN s.next_due_at
 WHEN %(pause)s::text IS NULL THEN coalesce(s.next_due_at,s.metadata_due_at,now()) END,
 hydration_needed=NOT %(has_current)s::boolean,
 paused_reason=CASE WHEN (s.availability_first_missing_at IS NOT NULL OR s.availability_resume_observed_at IS NOT NULL)
 OR s.paused_reason='availability_retry_expired' THEN s.paused_reason ELSE %(pause)s::text END,
 lease_channel_epoch=NULL,lease_token=NULL,lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL,
 typed_source_published_at=CASE WHEN %(video_type)s::text<>'long' THEN %(published_at)s::timestamptz END,
 typed_source_published_acquired_at=CASE WHEN %(video_type)s::text<>'long' THEN %(published_acquired_at)s::timestamptz END,
 typed_stream_actual_end_at=CASE WHEN %(video_type)s::text='stream' THEN %(stream_end)s::timestamptz END,
 typed_source_stream_end_acquired_at=CASE WHEN %(video_type)s::text='stream' THEN %(stream_end_acquired_at)s::timestamptz END,
 typed_collection_status=CASE WHEN %(video_type)s::text<>'long' THEN CASE WHEN s.video_type=%(video_type)s::text
 THEN coalesce(s.typed_collection_status,'pending') ELSE 'pending' END END,
 typed_collection_checked_at=CASE WHEN %(video_type)s::text<>'long' AND s.video_type=%(video_type)s::text THEN s.typed_collection_checked_at END,
 updated_at=clock_timestamp() WHERE s.video_id=%(video_id)s::text"""

@contextmanager
def _transaction(database,transaction):
    if transaction is not None:
        with transaction.borrow():yield transaction
    else:
        spec=get_operation('decide_video_type_review')
        with database.transaction(Operation(spec.name,statement_timeout_ms=spec.statement_timeout_ms,
                lock_timeout_ms=spec.lock_timeout_ms,total_timeout_ms=spec.statement_timeout_ms,error_codes=spec.error_codes)) as tx:
            yield tx

def _result(tx,args,outcome,*,mutation=None,missing=False):
    return dict(outcome=outcome,case=None if missing else get_case_in_transaction(tx,args['case_id']),mutation=mutation)

def _busy_queue(tx,video_id):
    state=tx.fetch_one(_QUEUE,(video_id,))
    return state,state is not None and tx.fetch_one(_QUEUE_LIVE,(state['value'],))['live'] is True

def apply_manual_video_type_queue(tx,*,video_id,channel_id,video_type):
    """Borrowed only: caller owns a savepoint and the late channel scope fence."""
    with tx.borrow():
        args=dict(video_id=video_id,channel_id=channel_id,video_type=video_type)
        if tx.fetch_one(_QUEUE_DECISION,args)['present'] is not True:
            raise DatabaseServerError('manual_video_type_queue_decision_missing',code='P0001')
        _apply_manual_video_type_queue_after_decision(tx,args)

def _apply_manual_video_type_queue_after_decision(tx,args):
    # Only the original wrapper or finite locked-owner proof may enter here.
    # Preserve all native queue locks, live clocks, policy facts and writes.
    with tx.borrow():
        video_id,channel_id,video_type=(args[key] for key in ('video_id','channel_id','video_type'))
        _,busy=_busy_queue(tx,video_id)
        if busy:raise DatabaseServerError('manual_video_type_collection_busy',code='55P03')
        source=tx.fetch_one(_QUEUE_SOURCE,args)
        # The native SELECT INTO clears every source variable if no inventory row.
        if source is None:source=dict(published_at=None,published_acquired_at=None,stream_end=None,stream_end_acquired_at=None,has_current=None,availability_status=None)
        pause=None
        if video_type=='long':
            long=channel_pause_row(tx.fetch_one(_QUEUE_LONG,args),field='pause')
            pause=long['pause']
            if not long['open_incident'] and source['has_current'] and source['availability_status'] not in (None,'public','unlisted'):
                pause=pause or 'availability_unsupported'
        elif typed_identity_row(tx.fetch_one(_QUEUE_ELIGIBLE,args))['eligible'] is not True:pause='typed_identity_not_eligible'
        args.update(pause=pause,has_current=source['has_current'],published_at=source['published_at'],published_acquired_at=source['published_acquired_at'],
            stream_end=source['stream_end'],stream_end_acquired_at=source['stream_end_acquired_at'])
        tx.execute(_QUEUE_INSERT,args)
        tx.execute(_QUEUE_UPDATE,args)

def decide_video_type_review(database,*,case_id,request_id,expected_revision,video_type,reason,transaction=None):
    """One decision/receipt, no retry. Return only after known commit when owned."""
    with _transaction(database,transaction) as tx:
        args=tx.fetch_one(_INPUT,(case_id,request_id,expected_revision,video_type,reason))
        if args.pop('invalid') is True:raise DatabaseServerError('invalid_video_type_review_decision',code='22023')
        case=tx.fetch_one(_CASE,(args['case_id'],))
        if case is None:return _result(tx,args,'not_found',missing=True)
        args['video_id']=case['video_id']
        if case['video_id'] is not None and tx.fetch_one(_ADVISORY,(case['video_id'],))['acquired'] is not True:
            return _result(tx,args,'busy')
        receipt=tx.fetch_one(_RECEIPT,args)
        if receipt is not None:
            if receipt['conflict'] is True:return _result(tx,args,'request_conflict')
            return _result(tx,args,'ready',mutation=dict(request_id=args['request_id'],replayed=True,applied_revision=receipt['revision']))
        if case['video_id'] is None:return _result(tx,args,'source_unavailable')
        try:
            with tx.savepoint():
                state,busy=_busy_queue(tx,args['video_id'])
                if busy:return _result(tx,args,'busy')
                inventory=tx.fetch_one(_INVENTORY,(args['video_id'],))
                if inventory is None:return _result(tx,args,'not_found',missing=True)
                case=tx.fetch_one(_CASE_LOCK,(args['case_id'],))
                if case is None:return _result(tx,args,'not_found',missing=True)
                if case['video_id']!=args['video_id']:return _result(tx,args,'source_unavailable')
                args['channel_id']=inventory['channel_id']
                if (case['channel_id']!=inventory['channel_id'] or (state is not None and state['channel_id']!=inventory['channel_id'])
                        or tx.fetch_one(_CONFLICT,args)['blocked'] is True):
                    return _result(tx,args,'blocked')
                if case['revision']!=args['expected_revision']:return _result(tx,args,'changed')
                # The original receipt check remains first: a known write may be
                # replayed after evidence expires. New decisions require a fresh,
                # currently unresolved contradiction, under the existing locks.
                if case['decision_type'] is None:
                    current = tx.fetch_one(_ELIGIBILITY,(args['case_id'],))
                    if current is None or current['review_status']!='needs_review':
                        return _result(tx,args,'source_unavailable')
                args.update(tx.fetch_one(_CONTEXT,(inventory['channel_id'],case['revision'])))
                need_queue=case['decision_type'] is None or inventory['video_type']!=args['video_type']
                tx.execute(_DECISION,args)
                tx.execute(_HISTORY,args)
                changed=tx.returning_one(_CANONICAL_EFFECT,args)
                if changed is not None:
                    # Queue effects reuse apply_manual_video_type_queue above.
                    # Import after this module is initialized to avoid a cycle.
                    from ..inventory.manual_decision_inventory_effects import after_manual_decision_inventory
                    after_manual_decision_inventory(tx,old_text=inventory['effect_old'],new_text=changed['value'])
                if need_queue and inventory['video_type']==args['video_type']:
                    apply_manual_video_type_queue(tx,video_id=args['video_id'],channel_id=args['channel_id'],video_type=args['video_type'])
                result=_result(tx,args,'ready',mutation=dict(request_id=args['request_id'],replayed=False,applied_revision=args['revision']))
        except DatabaseError as error:
            if error.code!='55P03' or error.outcome_unknown:raise
            return _result(tx,args,'busy')
        assert_channel_scope(tx,args['channel_id'],args['epoch'])
        return result
