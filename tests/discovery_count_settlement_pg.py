"""Post-counter source evidence and disabled Stream credits on disposable PG17.

Publication windows remain conservative hints. These fixtures assert application
behaviour, not whether YouTube's channel counter includes completed streams.
"""
from datetime import timedelta
from hashlib import sha256
import os
from uuid import uuid4

from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository,operation
from db.discovery_counts import settle_count_heads
from db.inventory.typed_inventory import TypedInventoryRepository
from db.retention.inventory_copies_retention import _receipts


def run_checks(admin,database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
        raise RuntimeError('disposable_cluster_required')
    checks=[]
    def scalar(sql,args=()):
        row=admin.execute(sql,args).fetchone()
        return next(iter(row.values())) if isinstance(row,dict) else row[0]
    def now():return scalar('SELECT clock_timestamp()')
    def check(ok,name):
        if not ok:raise AssertionError(name)
        checks.append(name)
    repo=DiscoveryRepository(database);typed=TypedInventoryRepository(database);channels=ChannelSyncRepository(database)
    # Synthetic owner setup only; deployment activation has independent tests.
    admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,clock_timestamp()) WHERE singleton')
    scenarios=('late_addressed','old_addressed','stream','stream_old','stream_missing','stream_after_upper','old_epoch')
    for ordinal,scenario in enumerate(scenarios):
        channel='UC'+('countscredit'+str(ordinal)).ljust(22,'0')
        video='fqcredit%03d'%ordinal
        lower=now()-timedelta(minutes=5)
        channels.insert_channel(dict(channel_id=channel,title='Count settlement fixture',
            user_input='https://www.youtube.com/channel/'+channel,
            initial_long_inventory_status='completed',initial_28d_stats_status='completed',
            video_count=10,channel_metadata_status='ok',channel_metadata_checked_at=lower.isoformat()),permission='accepted')
        epoch=scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(channel,))
        typed.prepare_runtime(limit=1000,enable=True)
        def page(kind,identities):
            source=typed.claim_channels([channel],video_types=[kind],only_due=False,worker_id='count-settlement-worker',
                run_id=str(uuid4()),expected_channel_scopes=[dict(channel_id=channel,control_epoch=epoch)])[0]
            scan=str(uuid4());typed.begin_scan(source,scan_id=scan,scan_boundary_at=now())
            return typed.settle_page(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
                next_page_token=None,page_status='complete' if identities else 'empty',
                items=[dict(video_id=i) for i in identities],source_checked_at=now())
        def count(value):
            observed=now()
            channels.update_metadata_batch([dict(channel_id=channel,control_epoch=epoch,values=dict(
                video_count=value,channel_metadata_status='ok',channel_metadata_checked_at=observed.isoformat()))])
            return observed
        def take():
            admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending'")
            event=scalar("INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind) VALUES(%s,%s,%s,'entry') RETURNING event_id",
                (sha256(str(uuid4()).encode()).hexdigest(),channel,video))
            rows=[r for r in repo.claim_event_batch(100) if r['work_id']==str(event)]
            check(len(rows)==1,scenario+'_exact_event')
            request=str(uuid4());repo.start_request(rows,request,now());return rows,request,now()
        page('long',[]);page('short',[])
        publication=(lower-timedelta(days=1) if scenario in ('old_addressed','stream_old') else now()).isoformat()
        if scenario=='stream_missing':publication=None
        upper=count(11) if scenario!='old_epoch' else now()
        if scenario=='stream_after_upper':publication=now().isoformat()
        rows,request,observed=take()
        stream=scenario.startswith('stream')
        if stream:
            args=dict(request_id=request,observed_at=observed,playlist_id='UULV'+channel[2:],
                items=[dict(video_id=video,published_at=publication)],enabled_types=('long','short'))
            if scenario=='stream':
                check(repo.terminal_observation(rows,'format_off',**dict(args,playlist_id='UULF'+channel[2:]))==[],
                      'wrong_playlist_cannot_mint_disabled_credit')
                check(repo.terminal_observation(rows,'format_off',**dict(args,enabled_types=('long','short','stream')))==[],
                      'enabled_stream_cannot_mint_disabled_credit')
            check(repo.terminal_observation(rows,'format_off',**args)==[rows[0]['work_id']],scenario+'_positive_settled')
            saved=scalar("SELECT result FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND transition_id=%s",(request,))
            check(saved['new_video_ids']==[] and saved['first_observed_video_ids']==[video],scenario+'_honest_source_receipt')
            for table in ('video_inventory','video_collection_state','video_inventory_memberships'):
                check(scalar('SELECT count(*) FROM public.'+table+' WHERE video_id=%s',(video,))==0,scenario+'_no_'+table)
            if scenario=='stream':
                # A second notification is a new request, not a new ID or credit.
                more,again,at=take()
                repo.terminal_observation(more,'format_off',**dict(args,request_id=again,observed_at=at))
                repeated=scalar("SELECT result FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND transition_id=%s",(again,))
                check(repeated['first_observed_video_ids']==[] and repeated['count_credit_candidates']==[],
                      'repeated_positive_stream_does_not_mint_second_credit')
            identities=[]
        else:
            receipt=repo.record_membership(rows,request_id=request,video_type='long',playlist_id='UULF'+channel[2:],
                source_checked_at=observed,transition_id=str(uuid4()),items=[dict(video_id=video,published_at=publication)])
            check(observed>upper and receipt['new_video_ids']==[video],scenario+'_post_counter_first_insert')
            identities=[video]
        if scenario=='old_epoch':
            epoch=scalar("UPDATE public.channel_collection_state SET control_epoch=nextval('public.channel_collection_epoch_seq') WHERE channel_id=%s RETURNING control_epoch",(channel,))
            count(11)
        page('long',identities);page('short',[])
        outcome=settle_count_heads(database,channel,video_types=('long','short'))
        expected=1 if scenario in ('late_addressed','stream') else 0
        check(outcome['settled'] and outcome['new_count']==expected and outcome['full_requested']==(expected==0),
              scenario+'_bounded_credit_decision')
        if scenario in ('late_addressed','stream'):
            # Count observation windows do not overlap; a repeated source cannot
            # reuse publication credit even though its receipt remains retained.
            count(12);page('long',identities);page('short',[])
            next_outcome=settle_count_heads(database,channel,video_types=('long','short'))
            check(next_outcome['new_count']==0 and next_outcome['full_requested'],scenario+'_no_credit_reuse')
        if scenario=='stream':
            # Exact existing retention owner scrubs all API-backed source payload.
            expired=now()-timedelta(days=30)
            admin.execute("UPDATE public.typed_inventory_receipts SET source_checked_at=%s WHERE receipt_scope='addressed_membership' AND transition_id=%s",(expired,request))
            with database.transaction(operation('discovery_credit_expiry_fixture')) as tx:
                deleted,scrubbed,deferred=_receipts(tx,dict(a='addressed_membership',b=request,
                    cutoff=(now()-timedelta(days=29)).isoformat(),scan_at=now().isoformat()),{})
            check((deleted,scrubbed,deferred)==(0,1,0),'disabled_credit_uses_existing_29day_payload_scrub')
            check(scalar("SELECT result FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND transition_id=%s",(request,))=={'source_expired':True},
                  'expired_disabled_credit_keeps_no_publication_payload')
    return checks
