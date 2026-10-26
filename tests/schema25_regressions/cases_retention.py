"""Current repository regressions using independent synthetic SQL fixtures."""
from datetime import timedelta
from .fixtures import CHANNEL,expect_error
from .manifest import require

def availability_recheck(f):
    from db.videos.availability import AvailabilityRepository
    f.seed_sql('availability_recheck_seed.sql')
    repo=AvailabilityRepository(f.database)
    f.check(repo.load_recheck_candidate_ids(CHANNEL,['private','unknown_unavailable'],limit=50)==['vid_priv_06','vid_old_005','vid_att3_04','vid_att3_03','vid_day1_02','vid_fresh01','vid_futur07'],'availability_all_order')
    expected=['vid_old_005','vid_att3_04','vid_day1_02','vid_futur07']
    for statuses in (['private','unknown_unavailable'],['unknown_unavailable']):
        f.check(repo.load_recheck_candidate_ids(CHANNEL,statuses,limit=50,recheck_after=timedelta(days=1))==expected,'availability_stale_order')
    for interval in (timedelta(minutes=30),timedelta(days=29)):
        expect_error(f,lambda:repo.load_recheck_candidate_ids(CHANNEL,['private'],limit=5,recheck_after=interval),ValueError)

def playlist_absence(f):
    from db.jobs.operator_jobs import OperatorJobRepository
    from .cases_jobs import estimate_values
    identities=('UCabsentlistsabsent00001','UCabsentlistsbigchannel1','UCabsentlistsunknown0001')
    for i,channel in enumerate(identities):
        f.channel(channel,pending=True)
        values=estimate_values(f,9000 if i==1 else 758,unknown=i==2)
        values['video_count']=33334 if i==1 else 758
        f.update_channel(values,channel)
    before=f.scalar("SELECT initial_inventory_estimate->'statuses' FROM public.channels WHERE channel_id=%s",(identities[0],))
    f.cleanup('channel_inventory_estimate')
    f.check(f.scalar("SELECT initial_inventory_estimate->'statuses' FROM public.channels WHERE channel_id=%s",(identities[0],))==before=={'long':'ok','short':'playlist_not_found','stream':'playlist_not_found'},'absent_lists_retained')
    repo=OperatorJobRepository(f.database)
    small,big,unknown=[repo.estimate_initial_inventory(c) for c in identities]
    f.check(small['estimate_kind']=='upper_bound' and small['items_upper_bound']==758 and small['estimated_pages']==18 and small['auto_hydration_allowed'] is True and small['confirmation_required'] is False,'small_estimate')
    f.check(big['auto_hydration_allowed'] is False and big['confirmation_required'] is True,'large_count_guard')
    f.check(unknown['estimate_kind']=='unknown' and unknown['confirmation_required'] is True,'unknown_size_guard')

def quota_phase_labels(f):
    from operational_copy_policy import quota_label
    from db.quota.youtube_quota import YouTubeQuotaRepository
    repo=YouTubeQuotaRepository(f.database,project_id='schema_ci')
    labels=('typed_inventory_long','typed_inventory_short','typed_inventory_stream','initial_channel_hydration','initial_28d_import','auto_video_collection','typed_playlist_inventory','auto_stats_batch','typed_inventory_other')
    for index,label in enumerate(labels):
        expected='other' if index==8 else label
        f.check(quota_label('phase',label)==expected,'quota_label')
        identity=f'10000000-0000-4000-8000-{index+1:012d}'
        f.check(repo.start_attempt({'request_attempt_id':identity,'quota_bucket':'general','phase':label,'endpoint':'videos','quota_units':1,'status':'started'}) is True,'quota_start')
        f.check(repo.finish_attempt(identity,{'status':'success','finished_at':f.now.isoformat()}) is True,'quota_finish')
        f.check(f.rows('SELECT phase,endpoint,status FROM public.youtube_api_request_attempts WHERE request_attempt_id=%s',(identity,))==[{'phase':expected,'endpoint':'videos','status':'success'}],'quota_persisted_labels')

def retention_scrub(f):
    from db.videos.video_queue_ensure import ensure_video_collection_state
    f.channel()
    f.seed_sql('review_retention_seed.sql')
    page=f.cleanup('review_evidence')
    f.check(page.scrubbed==1,'one_scrub')
    f.check(f.rows('SELECT video_id,revision,cardinality(observed_types) AS n FROM public.video_type_review_cases ORDER BY video_id')==[{'video_id':'vid_fresh01','revision':4,'n':2},{'video_id':'vid_stale02','revision':5,'n':0}],'review_scrub_rows')
    f.check(f.cleanup('review_evidence').scrubbed==0,'scrub_idempotent')
    ensure_video_collection_state(f.database,['vid_priv_03'],[{'channel_id':CHANNEL,'control_epoch':f.epoch()}])
    f.check(f.scalar("SELECT paused_reason FROM public.video_collection_state WHERE video_id='vid_priv_03'")=='availability_unsupported','no_status_copy')
    # A profile refresh marked running keeps the confirmed profile; once its
    # clock is past the window it is scrubbed like any other.
    from db.channel.channel_sync import ChannelSyncRepository
    f.rows("UPDATE public.channels SET title='Confirmed',subscribers=123,channel_metadata_status='ok',channel_metadata_checked_at=now()-interval '1 hour' WHERE channel_id=%s",(CHANNEL,))
    ChannelSyncRepository(f.database).mark_metadata_running([{'channel_id':CHANNEL,'collection_control_epoch':f.epoch(),'control_epoch':f.epoch()}],f.now.isoformat())
    f.cleanup('channel_profile')
    f.check(f.rows('SELECT title,subscribers,channel_metadata_status FROM public.channels WHERE channel_id=%s',(CHANNEL,))==[{'title':'Confirmed','subscribers':123,'channel_metadata_status':'running'}],'running_profile_kept')
    f.rows("UPDATE public.channels SET channel_metadata_checked_at=now()-interval '700 hours' WHERE channel_id=%s",(CHANNEL,))
    f.check(f.cleanup('channel_profile').scrubbed==1 and f.scalar('SELECT title FROM public.channels WHERE channel_id=%s',(CHANNEL,)) is None,'old_running_profile_scrubbed')

def detached_reviews(f):
    from db.reviews.video_type_reviews import VideoTypeReviewsRepository
    repo=VideoTypeReviewsRepository(f.database)
    f.rows("INSERT INTO public.video_type_review_cases(video_id,channel_id,observed_canonical_type,observed_types,observed_channels,state,source_detached_at) VALUES(NULL,NULL,NULL,ARRAY[]::text[],ARRAY[]::text[],'open',now())")
    f.check(repo.has_open() is False and repo.list_cases('open',101)==[] and len(repo.list_cases('all',101))==1,'detached_visibility')
    f.channel()
    f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at) VALUES('attached001',%s,'long',now()-interval '10 days',now())",(CHANNEL,))
    f.rows("INSERT INTO public.video_type_review_cases(video_id,channel_id,observed_canonical_type,observed_types,observed_channels,state,evidence_expires_at) VALUES('attached001',%s,'long',ARRAY['long','short'],ARRAY[%s],'open',now()+interval '696 hours')",(CHANNEL,CHANNEL))
    # A retained open case alone is not a current contradiction.
    f.check(repo.has_open() is False and repo.list_cases('open',101)==[],'unverified_not_open')
    waiting=repo.list_cases('awaiting',101)
    f.check(len(waiting)==1 and waiting[0]['review_status']=='awaiting_refresh'
            and waiting[0]['can_decide'] is False,'unverified_awaiting')
    f.check(len(repo.list_cases('all',101))==2,'attached_history_visible')

    def add_membership(index,video_type,prefix):
        scan=f'10000000-0000-4000-8000-{index:012d}'
        playlist=prefix+CHANNEL[2:]
        f.rows("INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,completed_at,source_checked_at,pages_completed,items_seen) VALUES(%s,%s,%s,%s,'full_reconciliation','completed','complete',now(),now(),1,1)",(scan,CHANNEL,video_type,playlist))
        f.rows("INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_scan_id,last_seen_scan_id,membership_status,first_seen_at,last_seen_at,last_present_at) VALUES('attached001',%s,%s,%s,now()-interval '10 days',%s,%s,'present',now(),now(),now())",(CHANNEL,video_type,playlist,scan,scan))

    add_membership(31,'long','UULF')
    add_membership(32,'short','UUSH')
    waiting=repo.list_cases('awaiting',101)
    f.check(repo.has_open() is False and repo.list_cases('open',101)==[]
            and len(waiting)==1 and waiting[0]['observed_types']==['long','short']
            and waiting[0]['can_decide'] is False,'long_short_awaiting_metadata')

    # Fresh evidence outside automatic Long/Short resolution remains visible.
    add_membership(33,'stream','UULV')
    active=repo.list_cases('open',101)
    f.check(repo.has_open() is True and len(active)==1 and active[0]['video_id']=='attached001'
            and active[0]['review_status']=='needs_review' and active[0]['can_decide'] is True
            and active[0]['observed_types']==['long','short','stream'],'fresh_conflict_visible')
    f.rows("UPDATE public.video_inventory_memberships SET first_seen_at=now()-interval '30 days',last_seen_at=now()-interval '30 days',last_present_at=now()-interval '30 days' WHERE video_id='attached001'")
    f.check(repo.has_open() is False and repo.list_cases('open',101)==[],'expired_conflict_not_open')
    f.check(len(repo.list_cases('awaiting',101))==1 and len(repo.list_cases('all',101))==2,'expired_history_retained')

def operational_retention(f):
    f.seed_sql('operational_retention_seed.sql')
    for target,table,key,old,recent in (('sync_runs','sync_runs','id',900001,900002),('quota_attempts','youtube_api_request_attempts','request_attempt_id','a0000000-0000-4000-8000-00000000000a','a0000000-0000-4000-8000-00000000000b')):
        page=f.cleanup(target)
        f.check(page.deleted==1 and page.target==target and page.deferred==0,'operational_receipt')
        f.check(f.scalar('SELECT count(*) FROM public.'+table+' WHERE '+key+'=%s',(old,))==0,'expired_removed')
        f.check(f.scalar('SELECT count(*) FROM public.'+table+' WHERE '+key+'=%s',(recent,))==1,'recent_preserved')

def inventory_window(f):
    # Exact original 1500+1500 core rows, all sixteen intrinsic hooks remain ON.
    f.seed_sql('inventory_window_seed.sql')
    f.check(f.scalar('SELECT count(*) FROM public.video_inventory')==1500 and f.scalar('SELECT count(*) FROM public.video_inventory_memberships')==1500,'large_fixture_exact')
    page=f.cleanup('memberships',limit=100)
    # Only memberships with an expired or future clock are selected, oldest first:
    # the five stale rows fit in one page, and the 1495 fresh ones are never read.
    f.check(page.scanned==5 and page.complete and page.next_cursor is None,'only_stale_rows_selected')
    pages=0;deleted=0
    while True:
        pages+=1;deleted+=page.deleted
        f.check(page.scanned<=100,'page_work_bound')
        if page.complete:break
        require(pages<3,'finite_pages')
        page=f.cleanup('memberships',after_id=page.next_cursor,limit=100)
    f.check(pages==1 and deleted==5,'window_pages_and_deletions')
    f.check(f.scalar('SELECT count(*) FROM public.video_inventory_memberships')==1495,'window_survivors')

def trashed_channel_removal(f):
    # A channel kept in the trash past the window is removed with everything it
    # owns, even when its catalogue outgrew one page; collection never waits on it.
    from db.channel.channel_control import set_channel_trash_state
    from db.videos.video_queue_ensure import ensure_video_collection_state
    from db.hot.hot_retention import HotRetentionRepository
    from hot_retention import HotCleanupCycle
    f.channel()
    f.seed_sql('trashed_channel_seed.sql')
    ids=[r['video_id'] for r in f.rows('SELECT video_id FROM public.video_inventory WHERE channel_id=%s ORDER BY video_id',(CHANNEL,))]
    ensure_video_collection_state(f.database,ids,[{'channel_id':CHANNEL,'control_epoch':f.epoch()}])
    set_channel_trash_state(f.database,channel_id=CHANNEL,restore=False)
    # 700 hours later: the trash age and every source clock the channel owns.
    for sql in ("UPDATE public.channel_collection_state SET hidden_at=now()-interval '700 hours' WHERE channel_id=%(c)s",
                "UPDATE public.channels SET channel_metadata_checked_at=now()-interval '700 hours' WHERE channel_id=%(c)s",
                "UPDATE public.channel_snapshots SET recorded_at=now()-interval '700 hours' WHERE channel_id=%(c)s",
                "UPDATE public.video_inventory SET published_acquired_at=now()-interval '700 hours' WHERE channel_id=%(c)s",
                "UPDATE public.video_inventory_memberships SET first_seen_at=now()-interval '701 hours',last_seen_at=now()-interval '700 hours',last_present_at=now()-interval '700 hours' WHERE channel_id=%(c)s",
                "UPDATE public.videos SET checked_at=now()-interval '700 hours',metadata_checked_at=now()-interval '700 hours' WHERE channel_id=%(c)s",
                "UPDATE public.video_snapshots s SET recorded_at=now()-interval '700 hours' FROM public.video_inventory i WHERE i.video_id=s.video_id AND i.channel_id=%(c)s"):
        f.rows(sql,{'c':CHANNEL})
    owned=('channels','channel_collection_state','channel_playlist_state','channel_video_inventory_scans','channel_snapshots',
           'video_inventory','video_inventory_memberships','videos','video_collection_state','video_search_projection')
    def remaining():
        return {t:f.scalar('SELECT count(*) FROM public.'+t+' WHERE channel_id=%s',(CHANNEL,)) for t in owned}
    f.check(remaining()['video_inventory']==110 and remaining()['video_collection_state']==110,'trashed_fixture_exact')
    cycle=HotCleanupCycle(HotRetentionRepository(f.database))
    passes=0
    while remaining()['channels'] and passes<4:
        passes+=1
        while True:
            summary=cycle.run_slice(max_batches=100,max_seconds=60)
            f.check(summary['status']!='error','trashed_cleanup_pass')
            if summary['status']=='scan_exhausted' or summary['stop_reason']=='deferred_or_no_progress':break
        cycle.reset()
    f.check(passes<=2,'trashed_channel_passes')
    f.check(set(remaining().values())=={0},'trashed_channel_removed')
    f.check(f.scalar("SELECT count(*) FROM public.video_snapshots WHERE video_id LIKE 't%%'")==0,'trashed_snapshots_removed')

def inventory_queue_seek(f):
    # Each inventory row reads only its own queue row, also when no queue row has
    # typed fields: the page reads O(page) queue rows, never the queue's tail.
    from db.retention import inventory_retention as cleanup
    f.channel()
    f.seed_sql('inventory_queue_seed.sql')
    p=dict(target='inventory',after_branch=None,after_clock=None,after_video=None,after_channel=None,after_type=None,
           cutoff=f.scalar("SELECT now()-interval '696 hours'"),upper=f.scalar('SELECT now()'),size=1000)
    plan=f.rows('EXPLAIN (ANALYZE, FORMAT JSON) '+cleanup._INVENTORY_PAGE,p)[0]['QUERY PLAN'][0]['Plan']
    def nodes(node):
        yield node
        for child in node.get('Plans',()):yield from nodes(child)
    queue=[n for n in nodes(plan) if n.get('Relation Name')=='video_collection_state']
    read=sum((n['Actual Rows']+n.get('Rows Removed by Filter',0))*n['Actual Loops'] for n in queue)
    f.check(queue and read<=3*150,'inventory_queue_seek_bound')
    page=f.cleanup('inventory',limit=100)
    f.check(page.scanned==0 and page.complete and page.deferred==0,'inventory_queue_noop_page')

def receipt_scan_index(f):
    # Scan cleanup finds receipts by scan_id alone: its probe and the FK cascade
    # of the scan delete. With sequential scans off, both must use the scan_id
    # index (the scan_transition partial index cannot serve them).
    import json
    f.channel()
    scan='10000000-0000-4000-8000-000000000041'
    f.rows("INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,completed_at,source_checked_at,pages_completed,items_seen) VALUES(%s,%s,'long',%s,'full_reconciliation','completed','complete',now(),now(),1,1)",(scan,CHANNEL,'UULF'+CHANNEL[2:]))
    def indexed(sql):
        with f.admin.transaction():
            f.rows('SET LOCAL enable_seqscan=off')
            plan=f.rows('EXPLAIN (FORMAT JSON) '+sql)[0]['QUERY PLAN']
        nodes=[]
        def walk(node):
            nodes.append(node)
            for child in node.get('Plans',()):walk(child)
        walk(plan[0]['Plan'])
        return ('typed_inventory_receipts_scan_id_idx' in {n.get('Index Name') for n in nodes}
                and not any(n['Node Type']=='Seq Scan' and n.get('Relation Name')=='typed_inventory_receipts' for n in nodes))
    f.check(indexed("SELECT EXISTS(SELECT 1 FROM public.typed_inventory_receipts WHERE scan_id='"+scan+"'::uuid)"),'receipt_probe_indexed')
    f.check(indexed("DELETE FROM ONLY public.typed_inventory_receipts WHERE '"+scan+"'::uuid OPERATOR(pg_catalog.=) scan_id"),'receipt_cascade_indexed')
    f.rows('DELETE FROM public.channel_video_inventory_scans WHERE scan_id=%s',(scan,))
    f.check(f.scalar('SELECT count(*) FROM public.channel_video_inventory_scans WHERE scan_id=%s',(scan,))==0,'scan_deleted')

def growth_window_from_last_snapshot(f):
    # The 28 days end at each video's last snapshot, not at the read: a snapshot
    # at or before that start gives a full window (28 to 29 days), a later one
    # a partial window. The ready row passes the application's own contract.
    from datetime import timedelta
    from db.reads.analytics_read_queries import read_video_view_growth_28d
    from channel_video_growth import _normalized_growth
    f.channel()
    scan='10000000-0000-4000-8000-000000000051'
    end=f.scalar("SELECT date_trunc('second',now())-interval '1 hour'")
    f.rows("INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,completed_at,source_checked_at,pages_completed,items_seen) VALUES(%s,%s,'long',%s,'full_reconciliation','completed','complete',%s,%s,1,3)",(scan,CHANNEL,'UULF'+CHANNEL[2:],end,end))
    starts={'g0000000001':(674,400),'g0000000002':(600,600),'g0000000003':(672,500)}
    for video,(hours,views) in starts.items():
        f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at) VALUES(%s,%s,'long',%s-interval '100 days',%s)",(video,CHANNEL,end,end))
        f.rows("INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_scan_id,last_seen_scan_id,membership_status,first_seen_at,last_seen_at,last_present_at) VALUES(%s,%s,'long',%s,%s-interval '100 days',%s,%s,'present',%s,%s,%s)",(video,CHANNEL,'UULF'+CHANNEL[2:],end,scan,scan,end,end,end))
        f.rows("INSERT INTO public.videos(video_id,channel_id,title,views,checked_at,published_at,duration,metadata_checked_at) VALUES(%s,%s,'Growth',1000,%s,%s-interval '100 days','PT5M',%s)",(video,CHANNEL,end,end,end))
        f.rows('INSERT INTO public.video_snapshots(video_id,views,recorded_at) VALUES(%s,1000,%s),(%s,%s,%s)',(video,end,video,views,end-timedelta(hours=hours)))
    rows={r['video_id']:r for r in read_video_view_growth_28d(f.database,channel_id=CHANNEL,video_ids=list(starts),as_of=f.scalar('SELECT now()').isoformat())}
    def shape(video):
        r=rows[video];return (r['growth_status'],r['delta_views'],r['actual_interval_seconds'],r['boundary_distance_seconds'],r['tracked_days'])
    f.check(shape('g0000000001')==('ready',600,674*3600,2*3600,None),'growth_ready_before_window_start')
    f.check(shape('g0000000003')==('ready',500,672*3600,0,None),'growth_ready_at_window_start')
    f.check(shape('g0000000002')==('partial',400,600*3600,None,25),'growth_partial_inside_window')
    f.check(_normalized_growth(rows['g0000000001'])['status']=='ready','growth_ready_contract')

def evidence_page_locks(f):
    # Expired snapshots are removed a page at a time: a live lease or a queue row
    # another transaction holds defers its video, a held snapshot defers itself,
    # and the rest of the page is deleted. Released rows go on the next page.
    import psycopg
    from .fixtures import dsns
    from db.hot.hot_retention import HotRetentionRepository
    f.channel()
    videos=['e%010d'%n for n in range(1,6)]
    for video in videos:
        f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,CHANNEL))
        f.rows("INSERT INTO public.video_snapshots(video_id,views,recorded_at) VALUES(%s,1,now()-interval '700 hours'),(%s,2,now()-interval '701 hours')",(video,video))
    f.rows("INSERT INTO public.video_snapshots(video_id,views,recorded_at) VALUES(%s,3,now()-interval '1 hour')",(videos[3],))
    # e1 under a live lease, e2 and e3 free, e4 without a queue row, e5 with an expired lease.
    for video in (videos[0],videos[1],videos[2],videos[4]):
        f.rows("INSERT INTO public.video_collection_state(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,CHANNEL))
    lease="UPDATE public.video_collection_state SET lease_token=gen_random_uuid(),lease_owner='other-collector',lease_started_at=now()+%s::interval,lease_expires_at=now()+%s::interval WHERE video_id=%s"
    f.rows(lease,('-1 minute','5 minutes',videos[0]))
    f.rows(lease,('-10 minutes','-1 second',videos[4]))
    held=psycopg.connect(dsns()[0],connect_timeout=5)
    try:
        # Another transaction holds e2's queue row and one of e3's snapshots.
        held.execute('SELECT 1 FROM public.video_collection_state WHERE video_id=%s FOR UPDATE',(videos[1],))
        held.execute('SELECT 1 FROM public.video_snapshots WHERE id=(SELECT min(id) FROM public.video_snapshots WHERE video_id=%s) FOR UPDATE',(videos[2],))
        repo=HotRetentionRepository(f.database)
        page=repo.cleanup_page('video_snapshots',limit=100)
        f.check((page.scanned,page.deleted,page.deferred,len(page.priority_references))==(10,5,5,5),'evidence_page_held_deferred')
        left=f.rows("SELECT video_id,count(*) AS n FROM public.video_snapshots WHERE recorded_at<now()-interval '696 hours' GROUP BY video_id ORDER BY video_id")
        f.check([(r['video_id'],r['n']) for r in left]==[(videos[0],2),(videos[1],2),(videos[2],1)],'evidence_page_deleted_rest')
    finally:
        held.rollback();held.close()
    f.rows("UPDATE public.video_collection_state SET lease_started_at=now()-interval '10 minutes',lease_expires_at=now()-interval '1 second' WHERE video_id=%s",(videos[0],))
    page=repo.cleanup_page('video_snapshots',limit=100)
    f.check((page.deleted,page.deferred)==(5,0),'evidence_page_released_deleted')
    f.check(f.rows('SELECT video_id,views FROM public.video_snapshots')==[{'video_id':videos[3],'views':3}],'evidence_page_fresh_kept')

