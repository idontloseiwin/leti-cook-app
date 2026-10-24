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

def detached_reviews(f):
    from db.reviews.video_type_reviews import VideoTypeReviewsRepository
    repo=VideoTypeReviewsRepository(f.database)
    f.rows("INSERT INTO public.video_type_review_cases(video_id,channel_id,observed_canonical_type,observed_types,observed_channels,state,source_detached_at) VALUES(NULL,NULL,NULL,ARRAY[]::text[],ARRAY[]::text[],'open',now())")
    f.check(repo.has_open() is False and repo.list_cases('open',101)==[] and len(repo.list_cases('all',101))==1,'detached_visibility')
    f.channel()
    f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at) VALUES('attached001',%s,'long',now()-interval '10 days',now())",(CHANNEL,))
    f.rows("INSERT INTO public.video_type_review_cases(video_id,channel_id,observed_canonical_type,observed_types,observed_channels,state,evidence_expires_at) VALUES('attached001',%s,'long',ARRAY['long','short'],ARRAY[%s],'open',now()+interval '696 hours')",(CHANNEL,CHANNEL))
    f.check(repo.has_open() is True and len(repo.list_cases('open',101))==1 and len(repo.list_cases('all',101))==2,'attached_visibility')

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
