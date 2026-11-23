"""One bounded head observation under the existing playlist lease.

Head receipts observe at most the first fifty IDs. A verified provider 404 can
establish absence of this playlist at the observation time. The membership of
individual videos and the progress of full scans remain independently owned.
"""
from list_observation import annotated, replayed
from hashlib import sha256
from datetime import datetime
import json
from psycopg.types.json import Jsonb

from collection_policy import inventory_source_policy
from ..discovery import operation, fail, _uuid
from ..channel.channel_control import assert_channel_scope
from ..videos.collection_policy import require_cadence_snapshot
from ..videos.video_queue_initialization import (ensure_video_collection_state_in_transaction,
                                                ensure_typed_collection_state_in_transaction)
from .inventory_scan_steps import _hot
from .inventory_scan_pages import _INSERT_CANONICAL_EFFECT, _VIDEO_LOCKS, _AFTER_INSERT
from .addressed_membership import _UPSERT
from .page_effect_batches import after_page_inventory_inserts, after_page_membership_changes

# Trusted aliases: d=playlist, c=channel control. The full token alone never
# creates head demand. A failed exact generation keeps its ordinary retry;
# a newly accepted count generation can make a new bounded head urgent.
# An exact first-page absence lasts until the source's own scheduled retry.
# Only scan pages and first-page reads record a missing list; naming both
# receipt kinds lets every evaluation use the receipts' primary key.
ABSENT_UNTIL_RECHECK_SQL = """coalesce(d.head_last_attempt->>'outcome'='playlist_not_found'
 AND d.head_last_attempt->>'epoch'=c.control_epoch::text
 AND d.head_next_due_at>now()
 AND (d.head_last_attempt->>'confirmed_absent'='true' OR EXISTS(
    SELECT 1 FROM public.typed_inventory_receipts r WHERE r.receipt_scope IN('addressed_membership','scan_transition')
    AND r.transition_id=CASE WHEN pg_input_is_valid(d.head_last_attempt->>'transition_id','uuid')
        THEN (d.head_last_attempt->>'transition_id')::uuid END
    AND r.result->>'error_code'='typed_playlist_not_found'
    AND r.result->>'channel_id'=d.channel_id AND r.result->>'video_type'=d.video_type)),false)"""

HEAD_COUNT_DUE_SQL = """EXISTS(SELECT 1 FROM public.channels changed
 WHERE changed.channel_id=d.channel_id AND changed.count_reconcile_token IS NOT NULL
 AND NOT ("""+ABSENT_UNTIL_RECHECK_SQL+""")
 AND changed.count_reconcile_token IS DISTINCT FROM changed.count_reconcile_settled_token
 AND (d.head_signal_token IS NOT NULL OR
   (EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND enabled)
    AND (changed.count_reconcile_token IS DISTINCT FROM d.count_reconcile_served_token
      OR d.count_reconcile_served_epoch IS DISTINCT FROM c.control_epoch)))
 AND NOT coalesce((coalesce(d.head_next_due_at,d.source_next_after,d.next_due_at)>now()
   AND d.head_last_attempt->>'count_token'=changed.count_reconcile_token::text
   AND d.head_last_attempt->>'epoch'=c.control_epoch::text),false))"""
HEAD_DUE_SQL = "(coalesce(d.head_next_due_at,d.source_next_after,d.next_due_at)<=now() OR (" + HEAD_COUNT_DUE_SQL + "))"
HEAD_BACKOFF_SQL = """coalesce(d.head_last_attempt->>'outcome' IN('playlist_not_found','error')
 AND d.head_next_due_at>now() AND d.head_last_attempt->>'epoch'=c.control_epoch::text
 AND (d.head_last_attempt->>'full_token' IS NOT DISTINCT FROM d.full_reconcile_required_token::text
   OR d.full_reconcile_required_token::text=d.head_last_attempt->>'count_token')
 AND d.head_last_attempt->>'count_token' IS NOT DISTINCT FROM
   (SELECT changed.count_reconcile_token::text FROM public.channels changed WHERE changed.channel_id=d.channel_id),false)"""

# The list's own periodic check has come due. A count signal pulls the head
# clock to now, and a refused claim is released with the scan clock moved a
# few seconds ahead, so neither clock can say whether the list would have been
# checked without the signal. The next periodic time the last read of the
# start worked out for itself can: nothing but another read of the start moves
# it. A waiting signal therefore follows the list's own pace, and neither a
# new count generation nor a refusal can turn into a free read. A list whose
# start was never read is checked once when its scan clock comes due; a last
# read without a recorded time allows no repeat.
HEAD_SCHEDULED_SQL = """coalesce(CASE
 WHEN coalesce(pg_input_is_valid(d.head_last_attempt->>'next_due_at','timestamptz'),false)
   THEN (d.head_last_attempt->>'next_due_at')::timestamptz<=now()
 WHEN coalesce(pg_input_is_valid(d.head_last_attempt->>'source_checked_at','timestamptz'),false) THEN false
 ELSE d.next_due_at<=now() END,false)"""


def _periodic_due(policy):
    """The next periodic read of the start, kept where a signal cannot move it."""
    return policy.next_due_at.isoformat() if isinstance(policy.next_due_at,datetime) else None

_SOURCE = """SELECT to_jsonb(d) AS source,c.control_epoch,
 (""" + HEAD_DUE_SQL + """) AS head_due,(""" + HEAD_COUNT_DUE_SQL + """) AS head_signal,
 (""" + HEAD_SCHEDULED_SQL + """) AS head_scheduled,
 EXISTS(SELECT 1 FROM public.discovery_runtime_control WHERE singleton AND enabled) AS enabled,
 to_jsonb(s) AS scan
 FROM public.channel_playlist_state d JOIN public.channel_collection_state c USING(channel_id)
 LEFT JOIN public.channel_video_inventory_scans s ON s.scan_id=d.active_scan_id AND s.status='running'
 WHERE d.channel_id=%s AND d.video_type=%s"""


def _owned(tx, claim, *, lock=False):
    row=tx.fetch_one(_SOURCE + (' FOR UPDATE OF d' if lock else ''), (claim.channel_id,claim.video_type))
    if row is None:fail('inventory_head_source_missing')
    source=row['source']
    if (source['status']!='leased' or str(source['lease_token'])!=claim.lease_token
            or source['lease_channel_epoch']!=claim.lease_channel_epoch
            or row['control_epoch']!=claim.lease_channel_epoch
            or not tx.fetch_one('SELECT %s::timestamptz>clock_timestamp() AS valid',
                                 (source['lease_expires_at'],))['valid']):
        fail('inventory_head_lease_not_owned')
    return row


def plan_page(database, claim):
    with database.transaction(operation('inventory_head_plan')) as tx:
        row=_owned(tx,claim)
        source,scan=row['source'],row['scan']
        count=scan['count_reconcile_token'] if scan else source['discovery_attempt_count_token']
        full=scan['full_reconcile_token'] if scan else source['discovery_attempt_full_token']
        epoch=scan['discovery_channel_epoch'] if scan else source['lease_channel_epoch']
        flags=dict(full=claim.scan_kind in ('initial_inventory','full_reconciliation'),
            event=bool(row['enabled'] and ((count is not None and
                (count!=source['count_reconcile_served_token'] or epoch!=source['count_reconcile_served_epoch']))
                or (full is not None and full!=source['full_reconcile_served_token']))))
        request = source.get('manual_list_check') or {}
        manual = (row['enabled'] and request.get('epoch') == claim.lease_channel_epoch
                  and request.get('state') in ('queued','running') and flags['full'])
        previous = source.get('head_last_attempt') or {}
        prior = previous.get('transition_id') if previous.get('epoch') == claim.lease_channel_epoch else None
        schedules = getattr(getattr(tx, 'cadence_snapshot', None), 'inventory_by_type', None)
        period = getattr(getattr(schedules, claim.video_type, None), 'reconciliation_seconds', None)
        return dict(flags=flags,manual=manual,enabled=row['enabled'],prior_head=prior,period_seconds=period,
                    head_due=row['head_due'],head_signal=row['head_signal'],
                    head_scheduled=row['head_scheduled'],
                    progressed=bool(scan and scan['pages_completed']>0))


def prepare_head(database, claim, *, transition_id, started_at, serve_signal=True):
    """Bind one head request to the lease.

    ``serve_signal=False`` is the list's own periodic check made while the
    urgent-check allowance is used up. It carries no count generation, so its
    settlement neither serves nor clears the waiting signal; an admitted urgent
    head still has to do that.
    """
    if type(serve_signal) is not bool:fail('inventory_head_request_invalid')
    transition_id=_uuid(transition_id)
    with database.transaction(operation('inventory_head_prepare')) as tx:
        row=_owned(tx,claim,lock=True)
        source=row['source']
        _hot(tx,started_at)
        identity=dict(transition_id=transition_id,lease_token=claim.lease_token,epoch=claim.lease_channel_epoch,
            count_token=source['discovery_attempt_count_token'] if serve_signal else None,
            full_token=source['discovery_attempt_full_token'],
            signal_token=source['head_signal_token'] if serve_signal else None,started_at=started_at.isoformat())
        tx.execute('UPDATE public.channel_playlist_state SET head_pending_request=%s '
                   'WHERE channel_id=%s AND video_type=%s', (Jsonb(identity),claim.channel_id,claim.video_type))
        assert_channel_scope(tx,claim.channel_id,claim.lease_channel_epoch)
    return identity


def observe_scan_head(tx, source, scan, *, transition_id, source_checked_at, outcome, new_count=0, error_code=None):
    """A real first page also checks the head; later pages never renew it."""
    if scan['pages_completed'] or scan['resume_page_token'] is not None:return
    # Existing scan repository inputs have already passed the native timestamp
    # checks, but their JSON-shaped boundary carries the timestamp as text.
    checked_at=datetime.fromisoformat(source_checked_at.replace('Z','+00:00')) if isinstance(source_checked_at,str) else source_checked_at
    policy=inventory_source_policy(scan['video_type'],'discovery',
        'boundary' if outcome=='observed' else outcome,new_count,source['head_no_new_streak'],
        source['head_error_count']+1,source['head_error_count'],error_code,checked_at,
        schedule=getattr(require_cadence_snapshot(tx).inventory_by_type,scan['video_type']))
    request=dict(transition_id=transition_id,epoch=source['lease_channel_epoch'] if outcome!='observed' else scan.get('discovery_channel_epoch'),
        count_token=source.get('discovery_attempt_count_token') if outcome!='observed' else scan.get('count_reconcile_token'),
        full_token=source.get('discovery_attempt_full_token') if outcome!='observed' else scan.get('full_reconcile_token'),
        source_checked_at=str(source_checked_at),outcome=outcome,next_due_at=_periodic_due(policy),
        confirmed_absent=outcome=='playlist_not_found' and error_code=='typed_playlist_not_found')
    tx.execute('''UPDATE public.channel_playlist_state SET head_next_due_at=%s,head_no_new_streak=%s,
        head_error_count=CASE WHEN %s THEN head_error_count+1 ELSE 0 END,head_last_attempt=%s,
        head_signal_token=CASE WHEN %s AND head_signal_token IS NOT DISTINCT FROM %s::uuid THEN NULL ELSE head_signal_token END
        WHERE channel_id=%s AND video_type=%s''',
        (policy.next_due_at,policy.no_new_streak,outcome=='error',Jsonb(request),outcome!='error',
         scan.get('count_reconcile_token'),scan['channel_id'],scan['video_type']))


def _membership(tx, items, *, channel_id, video_type, playlist_id, checked_at, receipt):
    payload=Jsonb(items)
    tx.fetch_all(_VIDEO_LOCKS,(payload,),max_rows=50)
    inserted=tx.returning_all(_INSERT_CANONICAL_EFFECT,dict(items=payload,channel_id=channel_id,
        video_type=video_type,source_checked_at=checked_at),max_rows=50)
    after_page_inventory_inserts(tx,inserted)
    facts=tx.fetch_one(_AFTER_INSERT,([r['video_id'] for r in inserted],payload,payload,channel_id,video_type))
    accepted=[]
    for item in items:
        # Canonical mismatch remains a conflict, not another channel's member.
        if not tx.fetch_one('SELECT video_id FROM public.video_inventory WHERE video_id=%s AND channel_id=%s',
                            (item['video_id'],channel_id)):continue
        old=tx.fetch_one('SELECT to_jsonb(m)::text AS value FROM public.video_inventory_memberships m '
            'WHERE video_id=%s AND channel_id=%s AND video_type=%s FOR UPDATE',
            (item['video_id'],channel_id,video_type))
        new=tx.returning_one(_UPSERT,dict(video_id=item['video_id'],channel_id=channel_id,video_type=video_type,
            playlist_id=playlist_id,published_at=item.get('published_at'),clock=checked_at,receipt=receipt))
        if new:after_page_membership_changes(tx,[dict(operation='update' if old else 'insert',
            old_text=old['value'] if old else None,new_text=new['value'])])
        accepted.append(item['video_id'])
    return facts,accepted


def _store_head_items(tx, result, items, channel_id, video_type, playlist, source_checked_at, transition_id, epoch, enabled):
    facts,accepted=_membership(tx,items,channel_id=channel_id,video_type=video_type,
        playlist_id=playlist,checked_at=source_checked_at,receipt=transition_id)
    from ..discovery_counts import credit_candidates
    result.update(video_ids=accepted,canonical_video_ids=accepted,new_video_ids=facts['inserted_ids'],
        observed_video_ids=facts['observed_ids'],inserted=len(facts['inserted_ids']),conflicts=facts['conflicts'],
        already_matching=max(0,len(items)-len(facts['inserted_ids'])-facts['conflicts']),
        count_credit_candidates=credit_candidates(facts['inserted_ids'],items,source_checked_at))
    tx.execute('UPDATE public.typed_inventory_receipts SET result=%s WHERE receipt_scope=\'addressed_membership\' '
        'AND transition_id=%s::uuid',(Jsonb(annotated(result, transition_id)),transition_id))
    ensure_video_collection_state_in_transaction(tx,accepted)
    if video_type!='long':ensure_typed_collection_state_in_transaction(tx,accepted)
    tx.execute('UPDATE public.video_collection_state SET discovery_hydration_requested_at='
        'coalesce(discovery_hydration_requested_at,%s) WHERE video_id=ANY(%s::text[]) AND channel_id=%s',
        (source_checked_at,facts['inserted_ids'],channel_id))
    # A first positive format observation retains real full debt. It
    # cannot promote source readiness; an already running full scan is
    # itself that debt and must keep its original identity.
    tx.execute('''UPDATE public.channel_playlist_state p SET
        full_reconcile_required_token=CASE WHEN full_reconcile_required_token IS DISTINCT FROM full_reconcile_served_token
            THEN full_reconcile_required_token ELSE gen_random_uuid() END,
        next_due_at=least(next_due_at,clock_timestamp())
        WHERE channel_id=%s AND video_type=%s AND active_scan_id IS NULL AND NOT EXISTS(
          SELECT 1 FROM public.channel_video_inventory_scans s WHERE s.channel_id=p.channel_id
          AND s.video_type=p.video_type AND s.status='completed' AND s.coverage='complete'
          AND s.scan_kind IN('initial_inventory','full_reconciliation') AND (NOT %s OR s.discovery_channel_epoch=%s))''',
          (channel_id,video_type,enabled,epoch))


def observe_notification_head(database, claims, *, request_id, video_type, playlist_id, observed_at, items, total):
    """Count a validated first page once without adopting a scan or serving an unbound count signal."""
    from ..discovery import _claims, locked_claim
    from ..discovery_observations import request_proof
    from typed_playlist import typed_playlist_id
    with database.transaction(operation('discovery_notification_head')) as tx:
        held = [locked_claim(tx,claim,fresh=True) for claim in _claims(claims)]
        channel,epoch = held[0]['channel_id'],held[0]['claim_channel_epoch']
        if (any(row['channel_id']!=channel or row['claim_channel_epoch']!=epoch for row in held)
                or typed_playlist_id(channel,video_type)!=playlist_id or len(items)>50):
            fail('discovery_membership_scope_invalid')
        for row in held:
            request_proof(tx,row,request_id,observed_at)
        source = tx.fetch_one('''SELECT head_no_new_streak,head_error_count FROM public.channel_playlist_state
            WHERE channel_id=%s AND video_type=%s AND playlist_id=%s AND runtime_enabled
            AND status='ready' AND active_scan_id IS NULL AND head_pending_request IS NULL
            AND (head_last_attempt->>'source_checked_at' IS NULL OR CASE
                WHEN pg_input_is_valid(head_last_attempt->>'source_checked_at','timestamptz')
                THEN (head_last_attempt->>'source_checked_at')::timestamptz<=%s END) FOR UPDATE SKIP LOCKED''',
            (channel,video_type,playlist_id,observed_at))
        if not source:
            return
        result=dict(observation_kind='head_page',channel_id=channel,control_epoch=epoch,
            video_type=video_type,playlist_id=playlist_id,transition_id=request_id,
            outcome='observed',coverage='partial',status='head_observed',provider_attempts=1,
            pages_processed=1,page_items=len(items),total_results=total)
        inserted=tx.execute("""INSERT INTO public.typed_inventory_receipts
            (receipt_scope,transition_id,result,source_checked_at) VALUES('addressed_membership',%s::uuid,%s,%s)
            ON CONFLICT(receipt_scope,transition_id) DO NOTHING""",(request_id,Jsonb(result),observed_at))
        if not inserted:
            return
        _store_head_items(tx,result,items,channel,video_type,playlist_id,observed_at,request_id,epoch,True)
        schedule=getattr(require_cadence_snapshot(tx).inventory_by_type,video_type)
        policy=inventory_source_policy(video_type,'discovery','boundary',result['inserted'],source['head_no_new_streak'],
            0,source['head_error_count'],None,observed_at,schedule=schedule)
        observation=dict(transition_id=request_id,epoch=epoch,outcome='observed',source_checked_at=observed_at.isoformat(),
                         next_due_at=_periodic_due(policy),total_results=total)
        tx.execute('''UPDATE public.channel_playlist_state SET head_last_attempt=%s,head_next_due_at=%s,
            head_no_new_streak=%s,head_error_count=0 WHERE channel_id=%s AND video_type=%s''',
            (Jsonb(observation),policy.next_due_at,policy.no_new_streak,channel,video_type))


def settle_head(database, claim, *, transition_id, items, source_checked_at, outcome, error_code=None,
                confirmed_missing_playlist=None):
    transition_id=_uuid(transition_id)
    if outcome not in ('observed','playlist_not_found','error') or not isinstance(items,(list,tuple)) or len(items)>50:
        fail('inventory_head_result_invalid')
    items=list(items)
    request_fields=[claim.channel_id,claim.video_type,claim.lease_token,claim.lease_channel_epoch,
                    transition_id,items,str(source_checked_at),outcome,error_code]
    if confirmed_missing_playlist is not None:
        if (not isinstance(confirmed_missing_playlist,str) or outcome!='playlist_not_found'
                or error_code!='typed_playlist_not_found' or items):
            fail('inventory_head_result_invalid')
        request_fields.append(confirmed_missing_playlist)
    fingerprint=sha256(json.dumps(request_fields,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with database.transaction(operation('inventory_head_settle')) as tx:
        tx.fetch_one('SELECT pg_advisory_xact_lock(hashtextextended(%s,781631247))',(transition_id,))
        prior=tx.fetch_one("SELECT result FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND transition_id=%s::uuid",(transition_id,))
        _hot(tx,source_checked_at)
        if prior:
            if prior['result'].get('request_hash')!=fingerprint:fail('inventory_head_receipt_conflict')
            return replayed(prior['result'])
        row=_owned(tx,claim,lock=True)
        source=row['source'];request=source['head_pending_request']
        if (not isinstance(request,dict) or request.get('transition_id')!=transition_id
                or request.get('lease_token')!=claim.lease_token or request.get('epoch')!=claim.lease_channel_epoch):
            fail('inventory_head_request_stale')
        if not tx.fetch_one('SELECT %s::timestamptz>=%s::timestamptz AS valid',
                            (source_checked_at,request['started_at']))['valid']:fail('inventory_head_source_before_request')
        playlist=source['playlist_id']
        if confirmed_missing_playlist is not None and confirmed_missing_playlist!=playlist:
            fail('inventory_head_result_invalid')
        result=dict(observation_kind='head_page',channel_id=claim.channel_id,control_epoch=claim.lease_channel_epoch,
            video_type=claim.video_type,playlist_id=playlist,transition_id=transition_id,lease_token=claim.lease_token,
            request_hash=fingerprint,count_reconcile_token=request['count_token'],outcome=outcome,
            coverage='partial',status='head_observed' if outcome=='observed' else outcome,
            provider_attempts=1,pages_processed=1,page_items=len(items),video_ids=[],canonical_video_ids=[],
            new_video_ids=[],inserted=0,already_matching=0,conflicts=0,replayed=False)
        if confirmed_missing_playlist is not None:
            result['provider_http_status']=404
            result['error_code']=error_code
        # Receipt is in the same transaction as memberships/queues and can be
        # referenced by the existing typed positive-evidence policy.
        tx.execute('INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,result,source_checked_at) '
            "VALUES('addressed_membership',%s::uuid,%s,%s)",(transition_id,Jsonb(annotated(result, transition_id)),source_checked_at))
        if outcome=='observed':
            _store_head_items(tx,result,items,claim.channel_id,claim.video_type,playlist,source_checked_at,
                              transition_id,claim.lease_channel_epoch,row['enabled'])
        schedule=getattr(require_cadence_snapshot(tx).inventory_by_type,claim.video_type)
        policy=inventory_source_policy(claim.video_type,'discovery',
            'boundary' if outcome=='observed' else outcome,result['inserted'],source['head_no_new_streak'],
            source['head_error_count']+1,source['head_error_count'],error_code,source_checked_at,schedule=schedule)
        request.update(outcome=outcome,confirmed_absent=confirmed_missing_playlist is not None,source_checked_at=source_checked_at.isoformat(),
                       next_due_at=_periodic_due(policy))
        tx.execute('''UPDATE public.channel_playlist_state SET head_next_due_at=%s,head_no_new_streak=%s,
            head_error_count=CASE WHEN %s THEN head_error_count+1 ELSE 0 END,
            head_last_attempt=%s,head_pending_request=NULL,
            head_signal_token=CASE WHEN %s AND head_signal_token IS NOT DISTINCT FROM %s::uuid THEN NULL ELSE head_signal_token END,
            count_reconcile_served_token=CASE WHEN %s AND %s::uuid IS NOT NULL THEN %s::uuid ELSE count_reconcile_served_token END,
            count_reconcile_served_epoch=CASE WHEN %s AND %s::uuid IS NOT NULL THEN %s ELSE count_reconcile_served_epoch END,
            status='ready',lease_token=NULL,lease_channel_epoch=NULL,lease_owner=NULL,lease_run_id=NULL,lease_expires_at=NULL,
            attempt_count=0,updated_at=clock_timestamp()
            WHERE channel_id=%s AND video_type=%s''',
            (policy.next_due_at,policy.no_new_streak,outcome=='error',Jsonb(request),outcome!='error',request['signal_token'],
             outcome!='error',request['count_token'],request['count_token'],outcome!='error',request['count_token'],
             claim.lease_channel_epoch,claim.channel_id,claim.video_type))
        if confirmed_missing_playlist and request['full_token']:
            tx.execute('UPDATE public.channel_playlist_state SET full_reconcile_served_token=%s::uuid WHERE channel_id=%s AND video_type=%s AND full_reconcile_required_token=%s::uuid',
                       (request['full_token'],claim.channel_id,claim.video_type,request['full_token']))
        assert_channel_scope(tx,claim.channel_id,claim.lease_channel_epoch)
    import support_journal
    support_journal.rows('writer','confirmed',[dict(video_id=value,channel_id=claim.channel_id)
        for value in result['new_video_ids']],reason='head',attempt_id=transition_id)
    return result
