"""Actual public enqueue/claim/remove/retention and retry lifecycle, no provider."""
from datetime import timedelta
from db.jobs.operator_jobs import OperatorJobRepository
from db.channel.channel_trash import ChannelTrashRepository
from .fixtures import CHANNEL,expect_error
from db.base.postgres import DatabaseError
from db.base.read_context import http_read_scope
from hot_storage import capture_read_lifetime,utc_instant

def estimate_values(f,count,*,unknown=False):
    stamp=(f.now-timedelta(minutes=1)).isoformat()
    # The real channel metadata reply supplies the total and its source clock together.
    # Reader estimates must not use a synthetic unclocked API count.
    return {'video_count':count,'channel_metadata_status':'ok','channel_metadata_checked_at':stamp,
      'initial_inventory_estimate':{'scope':'all','video_types':['long','short','stream'],
      'counts':{'long':count,'short':None,'stream':None},'statuses':{'long':'ok','short':'playlist_not_found','stream':'error' if unknown else 'playlist_not_found'},
      'checked_at_by_type':{'long':stamp,'short':stamp,'stream':None if unknown else stamp}}}

def initial_job(f,channel):
    f.channel(channel,pending=True);f.update_channel(estimate_values(f,40),channel)
    repo=OperatorJobRepository(f.database);estimate=repo.estimate_initial_inventory(channel)
    result=repo.enqueue_initial_inventory(channel,expected_epoch=f.epoch(channel),expected_items=estimate['estimated_items'],expected_pages=estimate['estimated_pages'],expected_quota_units=estimate['estimated_quota_units'],max_items=None,max_pages=None,max_quota_units=None,max_attempts=3,confirmed=False)
    claimed=repo.claim('schema-ci',lease_seconds=1200)
    f.check(result['created'] is True and claimed['id']==result['job']['id'],'initial_claim')
    return repo,claimed

def removal_stops_job(f):
    channel='UCremovalstopsjob0000001';repo,job=initial_job(f,channel)
    result=ChannelTrashRepository(f.database).remove(channel)
    f.check(result['outcome']=='removed','channel_removed')
    f.check(f.scalar("SELECT channel.is_hidden IS FALSE AND control.is_hidden IS TRUE FROM public.channels channel JOIN public.channel_collection_state control USING(channel_id) WHERE channel.channel_id=%s",(channel,)) is True,'trash_canonical_hidden_legacy_visible')
    with http_read_scope(),capture_read_lifetime() as lifetime:
        titles=ChannelTrashRepository(f.database).removed_titles([result['id']])
    f.check(len(titles)==1 and titles[0]['user_work_id']==result['id'] and titles[0]['title']=='Schema CI synthetic','trash_current_title_after_remove')
    deadline=f.now+timedelta(days=29,minutes=-1)
    f.check(utc_instant(titles[0]['source_expires_at'])==deadline and lifetime.expires_at==deadline and deadline>f.now,'trash_title_original_source_deadline')
    row=f.rows('SELECT * FROM public.operator_jobs WHERE id=%s',(job['id'],))[0]
    f.check(row['status']=='suspended' and all(row[k] is None for k in ('lease_token','lease_owner','lease_expires_at','lease_channel_epoch')) and row['finished_at'] is not None and row['last_error_code']=='channel_sync_not_ready','job_suspended')
    f.check(repo.list_active()==[],'no_active_removed_job')
    # A removed terminal job has no lease; the public owner rejects late release.
    expect_error(f,lambda:repo.release(job['id'],job['lease_token'],error_code='operator_worker_runtimeerror'),DatabaseError,code='P0001')
    f.check(f.rows('SELECT * FROM public.operator_jobs WHERE id=%s',(job['id'],))[0]==row,'late_release_cannot_reopen')

def removed_job_scan_cleanup(f):
    channel='UCremovedscanremovedscan';repo,job=initial_job(f,channel)
    scan='10000000-0000-4000-8000-000000000026'
    f.rows("INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,items_seen,new_memberships_found,token_history_verified,error_code,error_at) VALUES(%s,%s,'long','UULFremovedscanremovedscan','initial_inventory','failed','partial',NULL,NULL,false,'hot_source_expired',now())",(scan,channel))
    f.rows('UPDATE public.operator_jobs SET inventory_scan_id=%s WHERE id=%s',(scan,job['id']))
    f.rows("UPDATE public.channel_playlist_state SET runtime_enabled=true,next_due_at=now(),status='leased',lease_token='10000000-0000-4000-8000-000000000027',lease_owner='schema-ci',lease_run_id='schema-ci',lease_expires_at=now()+interval '900 seconds',lease_channel_epoch=%s WHERE channel_id=%s AND video_type='long'",(f.epoch(channel),channel))
    f.check(f.scalar('SELECT source_checked_at IS NULL FROM public.channel_video_inventory_scans WHERE scan_id=%s',(scan,)) is True,'scan_has_no_source_clock')
    ChannelTrashRepository(f.database).remove(channel)
    f.check(f.scalar("SELECT lease_token IS NULL AND lease_expires_at IS NULL AND status IN('ready','paused') FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='long'",(channel,)) is True,'playlist_released')
    f.check(f.cleanup('inventory_scans').deferred==1,'linked_scan_deferred')
    f.cleanup('operator_jobs')
    f.check(f.scalar('SELECT inventory_scan_id IS NULL FROM public.operator_jobs WHERE id=%s',(job['id'],)) is True,'job_scan_detached')
    f.check(f.cleanup('inventory_scans').deferred==0 and f.scalar('SELECT count(*) FROM public.channel_video_inventory_scans WHERE scan_id=%s',(scan,))==0,'scan_eventually_removed')

def failed_job_backoff(f):
    channel='UCfailedjobbackofffailed';f.channel(channel)
    f.rows("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at) SELECT 'fb'||lpad(g::text,9,'0'),%s,'long',now()-interval '10 days',now() FROM generate_series(1,3) g",(channel,))
    repo=OperatorJobRepository(f.database)
    job=repo.enqueue_current_stats(channel,expected_epoch=f.epoch(channel),max_items=100,max_quota_units=100,max_target_attempts=3)['job']['id']
    for _ in range(3):
        claimed=repo.claim('schema-ci',lease_seconds=60)
        f.check(claimed['id']==job,'backoff_claim')
        chunk=repo.prepare_chunk(job,claimed['lease_token'],limit=50,lease_seconds=60)
        f.check(len(chunk['video_ids'])==3,'backoff_chunk')
        repo.release(job,claimed['lease_token'],error_code='operator_worker_runtimeerror')
        f.check(f.scalar("SELECT status='queued' AND retry_not_before>clock_timestamp()+interval '4 minutes' FROM public.operator_jobs WHERE id=%s",(job,)) is True,'retry_delay')
        f.check(repo.claim('schema-ci',lease_seconds=60) is None,'early_claim_denied')
        f.rows("UPDATE public.operator_jobs SET retry_not_before=clock_timestamp()-interval '1 second' WHERE id=%s",(job,))
    claimed=repo.claim('schema-ci',lease_seconds=60)
    f.check(repo.prepare_chunk(job,claimed['lease_token'],limit=50,lease_seconds=60)['video_ids']==[],'exhausted_not_retried')
    f.check(f.scalar("SELECT status='completed' AND error_count=3 FROM public.operator_jobs WHERE id=%s",(job,)) is True,'job_completed')
    f.check(f.scalar("SELECT count(*)=3 AND bool_and(status='error' AND result_code='retry_limit_reached' AND job_attempts<=3) FROM public.operator_job_targets WHERE job_id=%s",(job,)) is True,'target_attempt_bound')

def claim_window_skips_leases(f):
    # The due window leaves out rows under a live lease, so a thousand abandoned
    # leases cannot fill it and starve the collector of the free rows behind them.
    from db.videos.video_scheduled_claims import _WINDOW
    f.channel()
    f.seed_sql('claim_window_seed.sql')
    ids=f.rows(_WINDOW[False])[0]['ids']
    free=['w%010d' % v for v in range(1001,1006)]
    f.check(ids==free,'claim_window_free_rows')
    f.check(f.scalar("SELECT count(*) FROM public.video_collection_state WHERE lease_token IS NOT NULL AND lease_expires_at>now()")==1000,'claim_window_leases_kept')
    # An expired lease is due work again.
    f.rows("UPDATE public.video_collection_state SET lease_started_at=now()-interval '10 minutes',lease_expires_at=now()-interval '1 second' WHERE video_id='w0000000001'")
    f.check(f.rows(_WINDOW[False])[0]['ids'][0]=='w0000000001','claim_window_expired_lease_due')
