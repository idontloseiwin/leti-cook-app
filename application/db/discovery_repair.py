"""One bounded campaign over original terminal notifications, not fake events."""
from hashlib import sha256
import json
from datetime import timedelta

from .discovery import operation, fail, _uuid
from .channel.channel_read_policy import CHANNEL_CONTROL_SQL


def refresh_campaign_states(tx, *, limit=200):
    """Expose an all-paused remainder without completing or forgiving its debt.

    Campaign locks precede target locks, as they do for budget admission. A
    concurrent resume remains in its target token and is picked up next time.
    Future due dates, active leases and owner handoffs are still pending work.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        fail('discovery_campaign_refresh_limit_invalid')
    runnable = """EXISTS(SELECT 1 FROM public.discovery_repair_targets target
        WHERE target.campaign_id=c.campaign_id AND target.status='pending'
          AND (target.paused_at IS NULL OR target.resume_token IS DISTINCT FROM target.resume_served_token
               OR target.work_phase='owner_wait'
               OR (target.claim_token IS NOT NULL AND target.claim_expires_at>clock_timestamp())))"""
    rows=tx.fetch_all('''SELECT c.campaign_id,c.state FROM public.discovery_repair_campaigns c
        WHERE c.completed_at IS NULL AND (
          (c.state='pending' AND EXISTS(SELECT 1 FROM public.discovery_repair_targets t
              WHERE t.campaign_id=c.campaign_id AND t.status='pending') AND NOT '''+runnable+''')
          OR (c.state='paused' AND '''+runnable+'''))
        ORDER BY c.created_at,c.campaign_id LIMIT %s FOR UPDATE OF c SKIP LOCKED''',(limit,),max_rows=limit)
    result=dict(campaigns_paused=0,campaigns_resumed=0)
    for row in rows:
        state='paused' if row['state']=='pending' else 'pending'
        result['campaigns_paused' if state=='paused' else 'campaigns_resumed']+=tx.execute(
            'UPDATE public.discovery_repair_campaigns SET state=%s WHERE campaign_id=%s::uuid',
            (state,row['campaign_id']))
    return result


def create_campaign(database, campaign_id, *, lookback_days=7, target_limit=300, general_limit=600, statistics_limit=50):
    campaign_id=_uuid(campaign_id)
    if (type(lookback_days) is not int or not 1<=lookback_days<90
            or type(target_limit) is not int or not 1<=target_limit<=1000000
            or any(type(v) is not int or not 0<=v<=1000000 for v in (general_limit,statistics_limit))):
        fail('discovery_repair_definition_invalid')
    with database.transaction(operation('discovery_repair_create')) as tx:
        # Frozen cutover survives process restarts, re-enabling and retries.
        control=tx.fetch_one('SELECT first_activated_at,clock_timestamp() AS now FROM public.discovery_runtime_control WHERE singleton FOR SHARE')
        if control is None or control['first_activated_at'] is None:
            fail('discovery_repair_before_cutover')
        end=control['first_activated_at']
        start=end-timedelta(days=lookback_days)
        definition=[1,str(start),str(end),target_limit,general_limit,statistics_limit,'legacy_terminal_entry']
        digest=sha256(json.dumps(definition,separators=(',',':')).encode()).hexdigest()
        tx.fetch_one('SELECT pg_advisory_xact_lock(1856149,50)')
        prior=tx.fetch_one('SELECT campaign_id,definition_digest FROM public.discovery_repair_campaigns '
            'WHERE (window_end=%s AND definition_version=1) OR campaign_id=%s::uuid',
            (end,campaign_id))
        if prior:
            if prior['definition_digest']!=digest:
                fail('discovery_repair_definition_conflict')
            return {'campaign_id':str(prior['campaign_id']),'replayed':True}
        # Old terminal events expire on received_at after JOURNAL_DAYS. Do not
        # slide the window or report a partial retained window as a complete one.
        from .websub import JOURNAL_DAYS
        if control['now']>=start+timedelta(days=JOURNAL_DAYS):
            fail('discovery_repair_history_expired')
        tx.execute('''INSERT INTO public.discovery_repair_campaigns(campaign_id,definition_digest,definition_version,
            window_start,window_end,target_limit,general_limit,statistics_limit) VALUES(%s::uuid,%s,1,%s,%s,%s,%s,%s)''',
            (campaign_id,digest,start,end,target_limit,general_limit,statistics_limit))
        count=tx.execute('''INSERT INTO public.discovery_repair_targets(campaign_id,channel_id,video_id,source_event_ids,received_at)
            SELECT %s::uuid,e.channel_id,e.video_id,(array_agg(e.event_id ORDER BY e.event_id))[1:1000],min(e.received_at)
            FROM public.websub_events e WHERE e.received_at>=%s AND e.received_at<%s AND e.kind='entry'
            AND e.status='done' AND e.outcome IN('discovery_requested','already_requested')
            AND NOT EXISTS(SELECT 1 FROM public.video_inventory i WHERE i.video_id=e.video_id AND i.channel_id=e.channel_id)
            AND coalesce((SELECT control.typed_runtime_eligible FROM (''' + CHANNEL_CONTROL_SQL + ''') control
                WHERE control.channel_id=e.channel_id),false)
            GROUP BY e.channel_id,e.video_id ORDER BY max(e.received_at) DESC,e.channel_id,e.video_id LIMIT %s''',
            (campaign_id,start,end,target_limit))
        tx.execute('UPDATE public.discovery_repair_campaigns SET target_count=%s WHERE campaign_id=%s::uuid',
                   (count,campaign_id))
    return {'campaign_id':campaign_id,'targets':count,'window_start':start,'window_end':end,'replayed':False}


def coalesce_event_owner(tx):
    """Reuse an already bound actual event's writer, including pending pre-T.

    No copied API data; the target's eventual ACK still checks the exact receipt.
    The fixed 500-row prefix bounds this opportunistic join.
    """
    tx.execute('''UPDATE public.discovery_repair_targets target SET work_phase='owner_wait',
        handoff_owner_kind=event.handoff_owner_kind,handoff_owner_id=event.handoff_owner_id,
        handoff_attempt_id=event.handoff_attempt_id,handoff_lease_token=event.handoff_lease_token,
        handoff_at=event.handoff_at,membership_receipt_id=event.membership_receipt_id,
        handoff_source_request_id=event.handoff_source_request_id,handoff_source_checked_at=event.handoff_source_checked_at,
        discovery_attempt_request_id=event.discovery_attempt_request_id,metadata_source_checked_at=event.metadata_source_checked_at,
        claim_channel_epoch=event.claim_channel_epoch
        FROM (SELECT target_id,channel_id,video_id FROM public.discovery_repair_targets
          WHERE status='pending' AND work_phase<>'owner_wait' AND claim_token IS NULL
          AND (paused_at IS NULL OR resume_token IS DISTINCT FROM resume_served_token)
          ORDER BY next_try_at,target_id LIMIT 500 FOR UPDATE SKIP LOCKED) pending
        CROSS JOIN LATERAL (SELECT e.* FROM public.websub_events e
          JOIN public.channel_collection_state c USING(channel_id)
          WHERE e.channel_id=pending.channel_id AND e.video_id=pending.video_id
          AND e.claim_channel_epoch=c.control_epoch AND e.handoff_attempt_id IS NOT NULL
          AND (e.work_phase='owner_wait' OR e.outcome='writer_receipt')
          ORDER BY e.received_at,e.event_id LIMIT 1) event
        WHERE target.target_id=pending.target_id''')
