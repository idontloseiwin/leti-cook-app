"""Discovery storage inside the parent's disposable PG17 fixture only."""
from datetime import timedelta
from hashlib import sha256
import os
from uuid import uuid4

from psycopg.types.json import Jsonb

from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository
from db.discovery_counts import observe_count_change,settle_count_heads
from db.base.postgres import Operation,DatabaseServerError
from db.videos.video_collection import VideoCollectionRepository

CHANNEL='UC'+'fxstorage'.ljust(22,'0')


def run_checks(admin,database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
        raise RuntimeError('disposable_cluster_required')
    def scalar(sql,args=()):
        row=admin.execute(sql,args).fetchone()
        return next(iter(row.values())) if isinstance(row,dict) else row[0]
    checks=[]
    def check(ok,name):
        if not ok:raise AssertionError(name)
        checks.append(name)
    ChannelSyncRepository(database).insert_channel(dict(channel_id=CHANNEL,title='Discovery storage synthetic',
        user_input='https://www.youtube.com/channel/'+CHANNEL,initial_long_inventory_status='completed',
        initial_28d_stats_status='completed'),permission='accepted')
    epoch=scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(CHANNEL,))
    admin.execute("UPDATE public.channel_playlist_state SET runtime_enabled=true,status='ready',next_due_at=now()+interval '7 days' WHERE channel_id=%s",(CHANNEL,))
    # Native channel creation may initially create only Long; add the typed source.
    admin.execute("INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at) VALUES(%s,'short',%s,true,'ready',now()+interval '7 days') ON CONFLICT DO NOTHING",(CHANNEL,'UUSH'+CHANNEL[2:]))
    repo=DiscoveryRepository(database)
    # Fixture-only enablement for storage proofs. Prepared-host admission and
    # activation idempotence are exercised by discovery_activation_pg.
    admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,clock_timestamp())')
    first=scalar('SELECT first_activated_at FROM public.discovery_runtime_control WHERE singleton')
    check(scalar('SELECT enabled AND first_activated_at IS NOT NULL FROM public.discovery_runtime_control WHERE singleton'),
          'synthetic_storage_fixture_enabled')
    def event(video,**values):
        digest=sha256(str(uuid4()).encode()).hexdigest()
        return scalar('''INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind)
            VALUES(%s,%s,%s,'entry') RETURNING event_id''',(digest,CHANNEL,video))
    identity='fxs00000001';event_id=event(identity)
    claimed=repo.claim_event_batch(50)
    selected=[r for r in claimed if int(r['work_id'])==event_id]
    check(len(selected)==1,'event_claimed_exact_identity')
    started=scalar('SELECT clock_timestamp()');request=str(uuid4())
    repo.start_request(selected,request,started)
    repo.complete_attempt(selected,request);repo.complete_attempt(selected,request)
    check(scalar('SELECT discovery_attempts FROM public.websub_events WHERE event_id=%s',(event_id,))==1,'metadata_round_idempotent')
    receipt=str(uuid4());observed=scalar('SELECT clock_timestamp()')
    result=repo.record_membership(selected,request_id=request,video_type='short',playlist_id='UUSH'+CHANNEL[2:],
        source_checked_at=observed,transition_id=receipt,items=[{'video_id':identity}])
    check(result['new_video_ids']==[identity],'addressed_member_inserted_without_full_scan')
    check(scalar('SELECT discovery_hydration_requested_at IS NOT NULL FROM public.video_collection_state WHERE video_id=%s',(identity,)),
          'addressed_member_has_durable_hydration')
    check(scalar("SELECT full_reconcile_required_token IS NOT NULL AND inventory_status<>'completed' FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(CHANNEL,)),
          'first_format_requires_full_without_false_completion')
    late=event(identity)
    video_repo=VideoCollectionRepository(database)
    video_claims=video_repo.claim_targeted_batch([identity],worker_id='Discovery storage',run_id=str(uuid4()),video_types=('long','short'),verify=True)['rows']
    check(len(video_claims)==1,'addressed_short_claimable_before_full_scan')
    attempt=str(uuid4());lease=str(video_claims[0].lease_token)
    repo.bind_owner(selected,{identity:{'lease_token':lease,'attempt_id':attempt}})
    check(repo.acknowledge(selected)==[],'handoff_not_ack')
    # Exact accepted native receipt shape; actual writer-path is separately run
    # by discovery_hydration_pg against the same canonical completion owner.
    admin.execute('''INSERT INTO public.typed_video_collection_receipts(video_id,collection_attempt_id,channel_id,
        video_type,lease_token,receipt_kind,recorded_at,payload_fingerprint) VALUES(%s,%s,%s,'short',%s,'metadata',now(),%s)''',
        (identity,attempt,CHANNEL,lease,'a'*32))
    check(repo.acknowledge(selected)==[str(event_id)],'exact_owner_receipt_ack')
    check(scalar('SELECT status FROM public.websub_events WHERE event_id=%s',(late,))=='pending','late_event_not_absorbed_by_old_ack')
    admin.execute('''UPDATE public.video_collection_state SET lease_token=NULL,lease_channel_epoch=NULL,
        lease_owner=NULL,lease_started_at=NULL,lease_expires_at=NULL WHERE video_id=%s''',(identity,))
    newer=repo.claim_event_batch(50);newer=[r for r in newer if int(r['work_id'])==late]
    new_video_claim=video_repo.claim_targeted_batch([identity],worker_id='Discovery crash',run_id=str(uuid4()),video_types=('long','short'),verify=True)['rows'][0]
    crash_attempt=str(uuid4())
    repo.bind_owner(newer,{identity:{'lease_token':str(new_video_claim.lease_token),'attempt_id':crash_attempt}})
    admin.execute("UPDATE public.video_collection_state SET lease_started_at=now()-interval '2 minutes',lease_expires_at=now()-interval '1 minute' WHERE video_id=%s",(identity,))
    check(repo.release_failed_owner(newer)==[str(late)],'expired_unknown_owner_fenced_not_stuck')
    check(scalar('SELECT lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s',(identity,)),
          'late_old_writer_lease_invalidated')
    check(scalar('SELECT status FROM public.websub_events WHERE event_id=%s',(late,))=='pending','crash_retry_not_false_ack')

    now=scalar('SELECT clock_timestamp()')-timedelta(seconds=5)
    admin.execute('UPDATE public.channels SET video_count=10,channel_metadata_checked_at=%s,channel_metadata_status=%s WHERE channel_id=%s',(now-timedelta(hours=1),'ok',CHANNEL))
    def count(old,new,at):
        with database.transaction(Operation('discovery_count_fixture')) as tx:
            tx.fetch_one('SELECT channel_id FROM public.channels WHERE channel_id=%s FOR UPDATE',(CHANNEL,))
            observe_count_change(tx,Jsonb(dict(video_count=old,channel_metadata_checked_at=(at-timedelta(minutes=1)).isoformat())),
                Jsonb(dict(channel_id=CHANNEL,video_count=new,channel_metadata_checked_at=at.isoformat(),channel_metadata_status='ok')))
        return scalar('SELECT count_reconcile_token FROM public.channels WHERE channel_id=%s',(CHANNEL,))
    a=count(10,11,now);b=count(11,12,now+timedelta(seconds=1))
    check(a!=b,'successive_count_changes_have_distinct_generation')
    check(scalar('SELECT count_reconcile_previous FROM public.channels WHERE channel_id=%s',(CHANNEL,))==10,'unsettled_count_preserves_original_baseline')
    admin.execute('UPDATE public.channel_playlist_state SET count_reconcile_served_token=%s,count_reconcile_served_epoch=%s WHERE channel_id=%s AND runtime_enabled',(b,epoch,CHANNEL))
    decision=settle_count_heads(database,CHANNEL,video_types=('long','short'))
    check(decision['full_requested'],'unexplained_positive_delta_requests_full')
    check(not settle_count_heads(database,CHANNEL,video_types=('long','short'))['settled'],'same_residual_gap_not_repeated_full')
    check(scalar('SELECT count_reconcile_settled_token FROM public.channels WHERE channel_id=%s',(CHANNEL,))==b,'count_ack_with_full_intent_atomic')

    legacy=event('fxs00000002')
    admin.execute("UPDATE public.websub_events SET received_at=%s,status='done',outcome='discovery_requested',processed_at=now(),work_phase='settled' WHERE event_id=%s",(first-timedelta(days=2),legacy))
    pending=event('fxs00000003')
    admin.execute('UPDATE public.websub_events SET received_at=%s WHERE event_id=%s',(first-timedelta(days=1),pending))
    campaign=repo.create_repair_campaign(str(uuid4()))
    replay=repo.create_repair_campaign(str(uuid4()))
    check(replay['campaign_id']==campaign['campaign_id'] and replay['replayed'],'repair_one_frozen_window')
    try:
        repo.create_repair_campaign(str(uuid4()),lookback_days=6)
    except DatabaseServerError:
        check(True,'changed_repair_window_cannot_refill_same_cutover_budget')
    else:
        raise AssertionError('changed_repair_window_cannot_refill_same_cutover_budget')
    check(scalar('SELECT count(*) FROM public.discovery_repair_targets WHERE campaign_id=%s AND video_id=%s',(campaign['campaign_id'],'fxs00000002'))==1,
          'legacy_terminal_selected_by_original_receive_time')
    check(scalar('SELECT count(*) FROM public.discovery_repair_targets WHERE campaign_id=%s AND video_id=%s',(campaign['campaign_id'],'fxs00000003'))==0,
          'precutover_pending_uses_new_path_not_repair')

    aged=event('fxs00000004')
    admin.execute("UPDATE public.websub_events SET received_at=now()-interval '8 days' WHERE event_id=%s",(aged,))
    selected=[r for r in repo.claim_event_batch(50) if int(r['work_id'])==aged]
    check(len(selected)==1 and selected[0]['review_required'],'seven_day_review_mark_is_durable')
    started=scalar('SELECT clock_timestamp()');request=str(uuid4())
    repo.start_request(selected,request,started);repo.complete_attempt(selected,request,started)
    future=started+timedelta(hours=2)
    repo.defer(selected,'lifecycle_wait',started,lifecycle_observed=True,future_until=future)
    before=scalar('SELECT future_wait_started_at FROM public.websub_events WHERE event_id=%s',(aged,))
    selected=[r for r in repo.claim_event_batch(50) if int(r['work_id'])==aged]
    repo.defer(selected,'quota',started)
    check(scalar('SELECT future_wait_started_at FROM public.websub_events WHERE event_id=%s',(aged,))==before,
          'quota_does_not_erase_observed_future_pause')
    check(scalar('SELECT future_wait_until FROM public.websub_events WHERE event_id=%s',(aged,))==future,
          'future_pause_keeps_original_confirmed_end')
    check(scalar('SELECT review_required FROM public.websub_events WHERE event_id=%s',(aged,)),
          'future_pause_does_not_erase_previous_unresolved_age')
    admin.execute("INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at) VALUES(%s,'stream',%s,true,'ready',now()+interval '7 days') ON CONFLICT DO NOTHING",(CHANNEL,'UULV'+CHANNEL[2:]))
    def stream_obligation(identity):
        event_id=event(identity)
        selected=[r for r in repo.claim_event_batch(50) if int(r['work_id'])==event_id]
        started=scalar('SELECT clock_timestamp()');request=str(uuid4());receipt=str(uuid4())
        repo.start_request(selected,request,started)
        repo.record_membership(selected,request_id=request,video_type='stream',playlist_id='UULV'+CHANNEL[2:],
            source_checked_at=started,transition_id=receipt,items=[{'video_id':identity}])
        return event_id,selected,receipt
    known_stream,selected,_=stream_obligation('fxs00000005')
    check(repo.settle_disabled(selected,enabled_types=('long','short'))==[str(known_stream)],
          'confirmed_disabled_stream_settles_without_retries')
    stale_stream,selected,receipt=stream_obligation('fxs00000006')
    admin.execute("UPDATE public.typed_inventory_receipts SET result='{\"source_expired\":true}'::jsonb WHERE transition_id=%s",(receipt,))
    check(repo.settle_disabled(selected,enabled_types=('long','short'))==[],
          'stale_type_proof_cannot_settle_disabled_format')
    check(scalar('SELECT status FROM public.websub_events WHERE event_id=%s',(stale_stream,))=='pending',
          'unconfirmed_format_retains_original_obligation')
    return checks
