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

_TRASHED_OWNED=('channels','channel_collection_state','channel_playlist_state','channel_video_inventory_scans','channel_snapshots',
                'video_inventory','video_inventory_memberships','videos','video_collection_state','video_search_projection')

def _trashed_channel(f):
    """The seeded 110-video channel, queued, moved to the trash and aged 700 hours."""
    from db.channel.channel_control import set_channel_trash_state
    from db.videos.video_queue_ensure import ensure_video_collection_state
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
    def remaining():
        return {t:f.scalar('SELECT count(*) FROM public.'+t+' WHERE channel_id=%s',(CHANNEL,)) for t in _TRASHED_OWNED}
    f.check(remaining()['video_inventory']==110 and remaining()['video_collection_state']==110,'trashed_fixture_exact')
    return ids,remaining

def trashed_channel_removal(f):
    # A channel kept in the trash past the window is removed with everything it
    # owns, even when its catalogue outgrew one page; collection never waits on it.
    from db.hot.hot_retention import HotRetentionRepository
    from hot_retention import HotCleanupCycle
    _,remaining=_trashed_channel(f)
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

def trashed_channel_queue_not_collected(f):
    # Expired videos of a channel in the trash are queued for an identity check
    # like those of any channel (25,625 such rows on 20,000 videos),
    # but no claim path takes them while the channel waits, nothing is written to
    # them, and they leave with the channel.
    from uuid import uuid4
    from db.base.postgres import DatabaseServerError
    from db.hot.hot_retention import HotRetentionRepository
    from db.videos.video_claims import claim_video_collection_ids,claim_video_metadata_collection_ids
    from db.videos.video_collection import VideoCollectionRepository
    from hot_retention import HotCleanupCycle
    ids,remaining=_trashed_channel(f)
    # Only the targets that withdraw an expired type; the channel itself stays.
    cycle=HotCleanupCycle(HotRetentionRepository(f.database),targets=('memberships','inventory'))
    for _ in range(4):
        f.check(cycle.run_slice(max_batches=100,max_seconds=60)['status']!='error','trashed_queue_cleanup_pass')
        if f.scalar('SELECT count(*) FROM public.video_inventory WHERE channel_id=%s AND skeleton_since IS NOT NULL',(CHANNEL,))==110:break
        cycle.reset()
    queue="SELECT coalesce(jsonb_agg(to_jsonb(q) ORDER BY q.video_id),'[]'::jsonb)::text FROM public.video_collection_state q WHERE q.channel_id=%s"
    before=f.scalar(queue,(CHANNEL,))
    f.check(f.scalar("""SELECT count(*) FROM public.video_collection_state WHERE channel_id=%s AND video_type IS NULL
        AND hydration_needed AND paused_reason IS NULL AND next_due_at<=now() AND lease_token IS NULL""",(CHANNEL,))==110
        and remaining()['channels']==1,'trashed_queue_identity_due')
    repository=VideoCollectionRepository(f.database)
    for scopes in (None,[{'channel_id':CHANNEL,'video_type':'unknown'}]):
        options={} if scopes is None else {'scopes':scopes,'operation':'metadata'}
        batch=repository.claim_due_batch(limit=50,worker_id='schema-ci-trash',run_id=str(uuid4()),**options)
        # The due window itself leaves the rows out, before any locked inspection.
        f.check(not batch['rows'] and batch['inspected_count']==0 and batch['paused_count']==0,'trashed_queue_not_claimed')
    page=repository.due_candidate_page(video_type='unknown',run_id=str(uuid4()))
    f.check(page['rows']==[] and page['inspected_count']>=1,'trashed_queue_not_offered')
    scope=[{'channel_id':CHANNEL,'control_epoch':f.epoch()}]
    for claim in (claim_video_collection_ids,claim_video_metadata_collection_ids):
        try:
            claim(f.database,ids[:50],'schema-ci-trash',str(uuid4()),scope)
        except DatabaseServerError as error:
            f.check(str(error)=='channel_collection_scope_lost','trashed_queue_id_claim_refused')
        else:
            raise AssertionError('trashed_queue_id_claim_refused')
    f.check(f.scalar(queue,(CHANNEL,))==before,'trashed_queue_rows_unchanged')
    cycle=HotCleanupCycle(HotRetentionRepository(f.database))
    passes=0
    while remaining()['channels'] and passes<4:
        passes+=1
        while True:
            summary=cycle.run_slice(max_batches=100,max_seconds=60)
            f.check(summary['status']!='error','trashed_cleanup_pass')
            if summary['status']=='scan_exhausted' or summary['stop_reason']=='deferred_or_no_progress':break
        cycle.reset()
    f.check(set(remaining().values())=={0},'trashed_channel_removed')

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

def _previous_evidence_page(spec):
    # Expected cursor bounds for the protected read:
    # one future branch bounded by both now and the cursor.
    from db.retention.evidence_retention import _page
    clock=f"coalesce(t.{spec.clock},'-infinity'::timestamptz)"
    base=f"SELECT to_jsonb(t) AS row_data,{clock} AS work_at,{spec.key} AS work_key,{spec.video} AS video_id FROM public.{spec.table} t WHERE ({spec.payload}) AND "
    cursor=f"(%(after_at)s::timestamptz IS NULL OR ({clock},{spec.key})>(%(after_at)s::timestamptz,%(after_key)s::text))"
    order=f" ORDER BY {clock},{spec.key} LIMIT %(limit)s::integer"
    source='('+base+clock+'<%(cutoff)s::timestamptz AND '+cursor+order+') UNION ALL ('+base+clock+'>%(now)s::timestamptz AND '+cursor+order+')'
    return _page(spec).split('FROM (',1)[0]+'FROM ('+source+') candidates ORDER BY candidates.work_at,candidates.work_key LIMIT %(limit)s::integer'

def evidence_page_cursor_branches(f):
    # The "clock from the future" branch of a cleanup page is split on
    # its parameters. For every kind of cursor the page selects what the previous
    # page selected, in the same order; walks across now meet each row once; a
    # cursor before now never bounds the future scan, which starts at now; a
    # generic plan gives the same rows and runs only one future branch.
    import json
    from psycopg import sql
    from db.retention import evidence_retention as ev
    spec=ev._TARGETS['video_snapshots']
    new,old=ev._page(spec),_previous_evidence_page(spec)
    f.check(new!=old and new.count(' UNION ALL ')==2 and old.count(' UNION ALL ')==1,'cursor_branch_reference')
    f.channel()
    video='c0000000001'
    f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,CHANNEL))
    # 40 expired (pairs share a clock), 300 current, 40 from the future, a NULL and an infinite clock.
    f.rows("""INSERT INTO public.video_snapshots(video_id,views,recorded_at)
      SELECT %(v)s::text,n,now()-interval '700 hours'-make_interval(mins=>n/2) FROM generate_series(1,40) n
      UNION ALL SELECT %(v)s::text,n,now()-make_interval(mins=>n*2) FROM generate_series(1,300) n
      UNION ALL SELECT %(v)s::text,n,now()+interval '1 day'+make_interval(mins=>n) FROM generate_series(1,40) n
      UNION ALL SELECT %(v)s::text,0,NULL UNION ALL SELECT %(v)s::text,0,'infinity'""",dict(v=video))
    f.observe()
    stamp=f.rows(ev._CLOCK)[0]
    def params(at=None,key=None,limit=100):
        return dict(target='video_snapshots',cutoff=stamp['cutoff'],now=stamp['at'],after_at=at,after_key=key,limit=limit)
    def fetch(statement,p):
        # Unnamed statements, as the app sends them (prepare_threshold None): a custom plan each time.
        with f.admin.cursor() as cursor:
            cursor.execute(statement,p,prepare=False);return cursor.fetchall()
    def page(statement,p):return [(r['work_at'],r['work_key'],r['row_data'],r['cursor']) for r in fetch(statement,p)]
    def cursor_of(row):
        value=json.loads(row[3]);return value['at'],value['key']
    full=page(new,params(limit=500))
    expected=f.rows("""SELECT lpad(id::text,20,'0') AS key FROM public.video_snapshots
      WHERE coalesce(recorded_at,'-infinity')<%(cutoff)s::timestamptz OR coalesce(recorded_at,'-infinity')>%(now)s::timestamptz
      ORDER BY coalesce(recorded_at,'-infinity'),lpad(id::text,20,'0')""",params())
    f.check(len(full)==82 and [r[1] for r in full]==[r['key'] for r in expected] and full==page(old,params(limit=500)),'cursor_branch_full_order')
    # Position of the last row before each cursor: 0 is the NULL clock, 1..40 expired, 41..80 future, 81 infinity.
    below=f.scalar("SELECT to_jsonb(%s::timestamptz-interval '1 second')#>>'{}'",(stamp['cutoff'],))
    cursors=((None,None,-1),(*cursor_of(full[9]),9),(below,'0',40),(*cursor_of(full[40]),40),(stamp['at'],'',40),
             (*cursor_of(full[60]),60),(*cursor_of(full[81]),81))
    for at,key,position in cursors:
        for limit in (1,10,100):
            rows=page(new,params(at,key,limit))
            f.check(rows==full[position+1:position+1+limit] and rows==page(old,params(at,key,limit)),'cursor_branch_same_rows')
    # Pages chained by their own cursors cross from the expired rows to the future ones.
    for limit in (7,41,100):
        walked,cursor=[],(None,None)
        while True:
            rows=page(new,params(*cursor,limit=limit));walked+=rows
            if len(rows)<limit:break
            cursor=cursor_of(rows[-1])
        f.check(walked==full,'cursor_branch_walk_once')
    def nodes(node):
        yield node
        for child in node.get('Plans',()):yield from nodes(child)
    def scans(p,generic=False):
        # Without sequential, bitmap and sort plans this small table is read as a large one is:
        # ordered scans of the order index (a bitmap scan and a sort win only at this size).
        statement,rows=new,None
        try:
            with f.admin.transaction():
                f.rows('SET LOCAL enable_seqscan=off');f.rows('SET LOCAL enable_bitmapscan=off');f.rows('SET LOCAL enable_sort=off')
                if not generic:plan=fetch('EXPLAIN (ANALYZE, FORMAT JSON) '+statement,p)
                else:
                    # Prepared under a generic plan, the parameter-only conditions are one-time filters.
                    f.rows('SET LOCAL plan_cache_mode=force_generic_plan')
                    names=('target','cutoff','now','after_at','after_key','limit')
                    for index,name in enumerate(names,1):statement=statement.replace('%('+name+')s','$'+str(index))
                    f.rows('PREPARE evidence_page(text,timestamptz,timestamptz,timestamptz,text,integer) AS '+statement)
                    execute=sql.SQL('EXECUTE evidence_page({})').format(sql.SQL(',').join(sql.Literal(p[name]) for name in names)).as_string(f.admin)
                    plan=f.rows('EXPLAIN (ANALYZE, FORMAT JSON) '+execute)
                    rows=[(r['work_at'],r['work_key'],r['row_data'],r['cursor']) for r in f.rows(execute)]
        finally:
            # Only this statement: psycopg keeps its own prepared statements on this connection.
            if generic and f.scalar("SELECT count(*) FROM pg_prepared_statements WHERE name='evidence_page'"):f.rows('DEALLOCATE evidence_page')
        return [n for n in nodes(plan[0]['QUERY PLAN'][0]['Plan']) if n.get('Relation Name')=='video_snapshots'],rows
    for at,key,position in cursors[:5]:
        p=params(at,key,100)
        found,_=scans(p)
        ran=[n for n in found if n['Actual Loops']>0]
        f.check(len(ran)==2 and all(n.get('Index Name')=='hot_evidence_video_snapshots_order_idx' for n in ran)
                and sum('ROW(' in n.get('Index Cond','') for n in ran)==(0 if at is None else 1),'cursor_branch_future_from_now')
        found,rows=scans(p,generic=True)
        f.check(rows==full[position+1:position+101] and len(found)==3
                and sum(n['Actual Loops']==0 for n in found)==1,'cursor_branch_generic_one_future')
    # The real cleanup removes both kinds of rows, page by page, and keeps the current ones.
    removed,after=0,None
    while True:
        result=f.cleanup('video_snapshots',limit=7,after_id=after);removed+=result.deleted
        if result.complete:break
        after=result.next_cursor
    f.check(removed==82 and f.scalar("SELECT count(*) FROM public.video_snapshots WHERE recorded_at BETWEEN %(cutoff)s::timestamptz AND %(now)s::timestamptz",params())==300
            and f.scalar('SELECT count(*) FROM public.video_snapshots')==300,'cursor_branch_cleanup_complete')

def current_source_page_locks(f):
    # Expired counters are scrubbed a page at a time, under the same ownership
    # rules as snapshots: a live lease, a queue row or a video row another
    # transaction holds defers the video; fresh counters are never touched.
    import psycopg
    from .fixtures import dsns
    from db.hot.hot_retention import HotRetentionRepository
    f.channel()
    videos=['s%010d'%n for n in range(1,7)]
    for n,video in enumerate(videos,1):
        age='1 hour' if n==6 else '700 hours'
        f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,CHANNEL))
        f.rows("INSERT INTO public.videos(video_id,channel_id,title,views,like_count,checked_at,metadata_checked_at) VALUES(%s,%s,'Counters',100,5,now()-%s::interval,now())",(video,CHANNEL,age))
    # s1 under a live lease, s2 and s3 free, s4 without a queue row, s5 with an expired lease.
    for video in (videos[0],videos[1],videos[2],videos[4]):
        f.rows("INSERT INTO public.video_collection_state(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,CHANNEL))
    lease="UPDATE public.video_collection_state SET lease_token=gen_random_uuid(),lease_owner='other-collector',lease_started_at=now()+%s::interval,lease_expires_at=now()+%s::interval WHERE video_id=%s"
    f.rows(lease,('-1 minute','5 minutes',videos[0]))
    f.rows(lease,('-10 minutes','-1 second',videos[4]))
    def counted():
        return [r['video_id'] for r in f.rows('SELECT video_id FROM public.videos WHERE views IS NOT NULL ORDER BY video_id')]
    held=psycopg.connect(dsns()[0],connect_timeout=5)
    try:
        # Another transaction holds s2's queue row and s3's video row.
        held.execute('SELECT 1 FROM public.video_collection_state WHERE video_id=%s FOR UPDATE',(videos[1],))
        held.execute('SELECT 1 FROM public.videos WHERE video_id=%s FOR UPDATE',(videos[2],))
        repo=HotRetentionRepository(f.database)
        page=repo.cleanup_page('video_counters',limit=100)
        f.check((page.scanned,page.scrubbed,page.deferred,len(page.priority_references))==(5,2,3,3),'source_page_held_deferred')
        f.check(counted()==[videos[0],videos[1],videos[2],videos[5]],'source_page_scrubbed_rest')
    finally:
        held.rollback();held.close()
    f.rows("UPDATE public.video_collection_state SET lease_started_at=now()-interval '10 minutes',lease_expires_at=now()-interval '1 second' WHERE video_id=%s",(videos[0],))
    page=repo.cleanup_page('video_counters',limit=100)
    f.check((page.scrubbed,page.deferred)==(3,0),'source_page_released_scrubbed')
    f.check(counted()==[videos[5]] and f.scalar("SELECT count(*) FROM public.videos WHERE checked_at IS NOT NULL")==6,'source_page_fresh_and_clocks_kept')

def job_targets_page_locks(f):
    # A job whose source expired 700 hours ago: its targets are deleted page by
    # page. The job is decided once under its lock; a target another transaction
    # holds is deferred and deleted on the next page.
    import psycopg
    from .fixtures import dsns
    f.channel()
    videos=['j%010d'%n for n in range(1,5)]
    for video in videos:
        f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(video,CHANNEL))
    guards=(('public.operator_jobs','hot_operator_source_scope'),('public.operator_job_targets','hot_operator_target_scope'))
    # Jobs this old were written before their window closed; only their two age guards are off while they are seeded.
    for table,trigger in guards:f.rows('ALTER TABLE '+table+' DISABLE TRIGGER '+trigger)
    try:
        job=f.scalar("""INSERT INTO public.operator_jobs(job_type,channel_id,status,requested_at,started_at,finished_at,max_items,max_quota_units,
            target_count,processed_count,updated_count,initial_scope_version,phase,last_error_code,summary_json)
            VALUES('channel_current_stats_backfill',%s,'error',now()-interval '700 hours',now()-interval '700 hours',now()-interval '699 hours',
            4,4,4,4,4,0,'video_targets','source_scope_expired','{"source_scope_expired":true}'::jsonb) RETURNING id::text""",(CHANNEL,))
        for video in videos:
            f.rows("INSERT INTO public.operator_job_targets(job_id,video_id,status,job_attempts,completed_at,video_type) VALUES(%s::uuid,%s,'updated',1,now()-interval '699 hours','long')",(job,video))
    finally:
        for table,trigger in guards:f.rows('ALTER TABLE '+table+' ENABLE TRIGGER '+trigger)
    def left():
        return [r['video_id'] for r in f.rows('SELECT video_id FROM public.operator_job_targets WHERE job_id=%s::uuid ORDER BY video_id',(job,))]
    held=psycopg.connect(dsns()[0],connect_timeout=5)
    try:
        held.execute('SELECT 1 FROM public.operator_job_targets WHERE job_id=%s::uuid AND video_id=%s FOR UPDATE',(job,videos[2]))
        page=f.cleanup('operator_job_targets',limit=100)
        f.check((page.scanned,page.deleted,page.deferred)==(4,3,1),'job_targets_held_deferred')
        f.check(left()==[videos[2]],'job_targets_deleted_rest')
    finally:
        held.rollback();held.close()
    page=f.cleanup('operator_job_targets',limit=100)
    f.check((page.deleted,page.deferred,left())==(1,0,[]),'job_targets_released_deleted')
