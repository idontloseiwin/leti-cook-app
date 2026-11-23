"""Positive addressed membership through the ordinary inventory effect owners.

An addressed response does not finish a scan, mark any other member absent or
refresh channel coverage. The receipt has its own HTTP acquisition clock.
"""
from hashlib import sha256
import json

from psycopg.types.json import Jsonb

from ..discovery import _claims, _update, _uuid, fail, locked_claim, operation
from .inventory_scan_steps import _hot
from .inventory_scan_pages import _INSERT_CANONICAL_EFFECT
from .page_effect_batches import after_page_inventory_inserts, after_page_membership_changes
from ..videos.video_queue_initialization import (ensure_video_collection_state_in_transaction,
                                               ensure_typed_collection_state_in_transaction)

_UPSERT = '''INSERT INTO public.video_inventory_memberships AS m
 (video_id,channel_id,video_type,playlist_id,source_published_at,membership_status,evidence_source,
 first_seen_at,last_seen_at,last_present_at,absent_since,absence_confirmed_by_scan_id,addressed_receipt_id,updated_at)
 VALUES(%(video_id)s,%(channel_id)s,%(video_type)s,%(playlist_id)s,%(published_at)s::timestamptz,
 'present','addressed_membership',%(clock)s,%(clock)s,%(clock)s,NULL,NULL,%(receipt)s::uuid,now())
 ON CONFLICT(video_id,channel_id,video_type) DO UPDATE SET
 playlist_id=excluded.playlist_id,source_published_at=excluded.source_published_at,membership_status='present',
 evidence_source='addressed_membership',first_seen_at=least(m.first_seen_at,excluded.first_seen_at),
 last_seen_at=greatest(m.last_seen_at,excluded.last_seen_at),last_present_at=excluded.last_present_at,
 absent_since=NULL,absence_confirmed_by_scan_id=NULL,addressed_receipt_id=excluded.addressed_receipt_id,updated_at=now()
 WHERE m.last_present_at IS NULL OR excluded.last_present_at>=m.last_present_at
 RETURNING to_jsonb(m)::text AS value'''


def record_addressed_membership(database, claims, *, request_id, video_type, playlist_id,
                                source_checked_at, transition_id, items):
    claims = _claims(claims)
    request_id, transition_id = _uuid(request_id), _uuid(transition_id)
    if video_type not in ('long','short','stream') or not isinstance(items, list) or not 1 <= len(items) <= 50:
        fail('discovery_membership_invalid')
    if any(not isinstance(item, dict) or set(item)-{'video_id','published_at'} or not isinstance(item.get('video_id'),str)
           for item in items) or len({item['video_id'] for item in items}) != len(items):
        fail('discovery_membership_invalid')
    with database.transaction(operation('discovery_addressed_membership')) as tx:
        tx.fetch_one('SELECT pg_advisory_xact_lock(hashtextextended(%s,781631247))', (transition_id,))
        held = [(claim,locked_claim(tx,claim)) for claim in claims]
        channels = {row['channel_id'] for _,row in held}
        epochs = {row['claim_channel_epoch'] for _,row in held}
        if len(channels)!=1 or len(epochs)!=1:
            fail('discovery_membership_scope_invalid')
        channel_id, epoch = next(iter(channels)),next(iter(epochs))
        expected_playlist={'long':'UULF','short':'UUSH','stream':'UULV'}[video_type]+channel_id[2:]
        if playlist_id != expected_playlist or {i['video_id'] for i in items}!={r['video_id'] for _,r in held}:
            fail('discovery_membership_scope_invalid')
        if any(str(row['source_request_id'])!=request_id or row['work_phase']=='owner_wait' for _,row in held):
            fail('discovery_request_stale')
        _hot(tx,source_checked_at)
        earliest=max(row['source_request_started_at'] for _,row in held)
        if not tx.fetch_one('SELECT %s::timestamptz>=%s::timestamptz AND %s::timestamptz<=clock_timestamp() AS valid',
                            (source_checked_at,earliest,source_checked_at))['valid']:
            fail('discovery_source_clock_invalid')
        fingerprint=sha256(json.dumps([channel_id,epoch,video_type,playlist_id,request_id,str(source_checked_at),items],
                                     sort_keys=True,separators=(',',':')).encode()).hexdigest()
        replay=tx.fetch_one("SELECT result FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND transition_id=%s::uuid",(transition_id,))
        if replay:
            if replay['result'].get('request_hash')!=fingerprint:
                fail('discovery_receipt_conflict')
            return dict(replay['result'],replayed=True)
        # Ordinary page writers acquire this source before their per-ID locks.
        # Follow that order when adding the durable full intent, without taking
        # over or modifying an in-flight source lease or its continuation.
        source=tx.fetch_one('SELECT channel_id FROM public.channel_playlist_state '
            'WHERE channel_id=%s AND video_type=%s FOR UPDATE',(channel_id,video_type))
        if source is None:
            fail('discovery_source_unprepared')
        for item in sorted(items,key=lambda i:i['video_id']):
            tx.fetch_one('SELECT pg_advisory_xact_lock(hashtextextended(%s,962106))',(item['video_id'],))
        # An ID may have been recorded by another channel since target_state.
        # Isolate that exact envelope under the canonical ID locks. It is a
        # rejection, not a VIDEO receipt, and must not roll back its neighbours.
        mismatches={row['video_id'] for row in tx.fetch_all('SELECT video_id FROM public.video_inventory '
            'WHERE video_id=ANY(%s::text[]) AND channel_id<>%s',
            ([item['video_id'] for item in items],channel_id),max_rows=50)}
        rejected=[]
        if mismatches:
            from ..discovery_observations import terminal
            for claim,row in held:
                if row['video_id'] in mismatches:
                    terminal(tx,claim,'channel_mismatch')
                    rejected.append(claim['work_id'])
            held=[pair for pair in held if pair[1]['video_id'] not in mismatches]
            items=[item for item in items if item['video_id'] not in mismatches]
        accepted=[claim['work_id'] for claim,_ in held]
        if not items:
            return dict(channel_id=channel_id,control_epoch=epoch,video_type=video_type,playlist_id=playlist_id,
                video_ids=[],new_video_ids=[],accepted_work_ids=[],rejected_work_ids=rejected,
                request_id=request_id,request_hash=fingerprint,replayed=False)
        inserted=tx.returning_all(_INSERT_CANONICAL_EFFECT,dict(items=Jsonb(items),channel_id=channel_id,
            video_type=video_type,source_checked_at=source_checked_at),max_rows=50)
        after_page_inventory_inserts(tx,inserted)
        from ..discovery_counts import credit_candidates
        result=dict(channel_id=channel_id,control_epoch=epoch,video_type=video_type,playlist_id=playlist_id,
                    video_ids=[i['video_id'] for i in items],new_video_ids=[i['video_id'] for i in inserted],
                    count_credit_candidates=credit_candidates([i['video_id'] for i in inserted],items,source_checked_at),
                    accepted_work_ids=accepted,rejected_work_ids=rejected,
                    request_id=request_id,request_hash=fingerprint,replayed=False)
        tx.execute('INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,result,source_checked_at) '
                   "VALUES('addressed_membership',%s::uuid,%s::jsonb,%s)",(transition_id,Jsonb(result),source_checked_at))
        for item in items:
            params=dict(video_id=item['video_id'],channel_id=channel_id,video_type=video_type,
                        playlist_id=playlist_id,published_at=item.get('published_at'),clock=source_checked_at,receipt=transition_id)
            old=tx.fetch_one('SELECT to_jsonb(m)::text AS value FROM public.video_inventory_memberships m '
                'WHERE video_id=%s AND channel_id=%s AND video_type=%s FOR UPDATE',
                (item['video_id'],channel_id,video_type))
            new=tx.returning_one(_UPSERT,params)
            if new:
                after_page_membership_changes(tx,[dict(operation='update' if old else 'insert',
                    old_text=old['value'] if old else None,new_text=new['value'])])
        ids=[i['video_id'] for i in items]
        ensure_video_collection_state_in_transaction(tx,ids)
        if video_type!='long':
            ensure_typed_collection_state_in_transaction(tx,ids)
        # Fresh positive evidence supersedes a previous absent-list cooldown.
        tx.execute("""UPDATE public.channel_playlist_state SET head_next_due_at=least(head_next_due_at,%s)
            WHERE channel_id=%s AND video_type=%s AND head_last_attempt->>'outcome'='playlist_not_found'
            AND head_last_attempt->>'epoch'=%s AND CASE
              WHEN pg_input_is_valid(head_last_attempt->>'source_checked_at','timestamptz')
              THEN (head_last_attempt->>'source_checked_at')::timestamptz<=%s END""",
            (source_checked_at,channel_id,video_type,str(epoch),source_checked_at))
        tx.execute('''UPDATE public.channel_playlist_state p SET
            full_reconcile_required_token=CASE WHEN full_reconcile_required_token IS DISTINCT FROM full_reconcile_served_token
                THEN full_reconcile_required_token ELSE gen_random_uuid() END,
            next_due_at=least(next_due_at,clock_timestamp())
            WHERE channel_id=%s AND video_type=%s AND NOT EXISTS(
                SELECT 1 FROM public.channel_video_inventory_scans s WHERE s.channel_id=p.channel_id
                AND s.video_type=p.video_type AND s.discovery_channel_epoch=%s
                AND s.status='completed' AND s.coverage='complete'
                AND s.scan_kind IN('initial_inventory','full_reconciliation'))''',(channel_id,video_type,epoch))
        tx.execute('UPDATE public.video_collection_state SET discovery_hydration_requested_at=coalesce('
            'discovery_hydration_requested_at,%s) WHERE video_id=ANY(%s::text[]) AND channel_id=%s',
            (source_checked_at,ids,channel_id))
        for claim,_ in held:
            _update(tx,claim,"membership_receipt_id=%s::uuid,work_phase='handoff',wait_reason=NULL,technical_membership_failures=0",(transition_id,))
        if inserted:
            from ..discovery_observations import resume_matching
            resume_matching(tx,channel_id,[item['video_id'] for item in inserted])
    return result
