#!/usr/bin/env python3
"""Initial queue retry and per-video regular handoff on disposable PG17 only.

Uses the same explicit safety/cleanup harness as check_initial_video_batch.
Four tiny cases, at most1000 synthetic rows, no provider requests or schema/ACL
changes. Current public repositories perform claims, completions and settlement.
"""
import sys
from pathlib import Path
from datetime import timedelta
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'application')]
import check_initial_video_batch as harness
from db.videos.video_collection import VideoCollectionRepository
from db.jobs.operator_jobs import OperatorJobRepository
from db.inventory.typed_inventory import TypedInventoryRepository
from db.videos.video_queue_maintenance import backfill_video_collection_state
from psycopg.types.json import Jsonb
C=harness.CHANNEL
J=harness.JOB
T=harness.TOKEN


def seed(probe,count=4):
    ids=harness.seed_chunk(probe,count)
    old=probe.now-timedelta(days=2)
    with probe.admin.transaction():
        for index,kind in enumerate(('long','short','stream')[:min(count,3)]):
            video=ids[index];playlist={'long':'UULF','short':'UUSH','stream':'UULV'}[kind]+C[2:]
            probe.admin.execute("UPDATE public.video_inventory SET video_type=%s WHERE video_id=%s",(kind,video))
            probe.admin.execute("UPDATE public.operator_job_targets SET video_type=%s WHERE job_id=%s::uuid AND video_id=%s",(kind,J,video))
            probe.admin.execute("""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,
                inventory_status,last_completed_scan_id) VALUES(%s,%s,%s,true,'completed',%s::uuid)
                ON CONFLICT(channel_id,video_type) DO UPDATE SET playlist_id=excluded.playlist_id,
                runtime_enabled=true,inventory_status='completed',last_completed_scan_id=excluded.last_completed_scan_id""",(C,kind,playlist,harness.SCANS[kind]))
            if kind!='long':
                probe.admin.execute("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
                    source_published_at,first_seen_at,last_seen_at,last_present_at,first_seen_scan_id,last_seen_scan_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid,%s::uuid)""",
                    (video,C,kind,playlist,old,old,probe.now,probe.now,harness.SCANS[kind],harness.SCANS[kind]))
    jobs=OperatorJobRepository(probe.database);videos=VideoCollectionRepository(probe.database)
    prepared=probe.call(jobs.prepare_initial_hydration_chunk,J,T,limit=50)
    claims=probe.call(videos.claim_initial_video_ids,ids,job_id=J,channel_id=C,worker_id='initial_chunk_probe',run_id=T)
    harness.fixtures.require(len(claims)==count,'handoff_first_claim_all')
    return ids,jobs,videos,claims


def complete(probe,videos,claims,*,regular=False):
    commands=[]
    for claim in claims:
        row=dict(video_id=claim.video_id,channel_id=C,video_type=claim.video_type,lease_token=claim.lease_token,
            collection_attempt_id=str(uuid4()),recorded_at=(probe.now if regular else probe.now-timedelta(seconds=1)).isoformat(),
            title='handoff fixture',published_at=(probe.now-timedelta(days=2)).isoformat(),duration='PT2M',tags=[],
            category_id='22',views=100,like_count=2,comment_count=0,availability_status='public',privacy_status='public',
            upload_status='processed',availability_source='initial_probe',availability_checked_at=None,
            availability_restored_at=None,youtube_topic_categories=[],default_audio_language=None,default_language=None,
            caption_available=False,stream_actual_end_at=(probe.now-timedelta(days=1)).isoformat() if claim.video_type=='stream' else None)
        commands.append(dict(operation='complete_metadata' if claim.video_type=='long' else 'complete_typed_metadata',rows=[row]))
    if regular:
        return sum(probe.call(videos.complete_metadata if command['operation']=='complete_metadata'
            else videos.complete_typed_metadata,command['rows']) for command in commands)
    return probe.call(videos.complete_initial_batch,commands)


def fail(probe,videos,claims,seconds=3600):
    return probe.call(videos.fail_batch,[dict(video_id=c.video_id,video_type=c.video_type,lease_token=c.lease_token,
        error_type='youtube_view_count_absent',retry_after_seconds=seconds) for c in claims])


def settle(probe,jobs):
    return probe.call(jobs.settle_initial_hydration_chunk,J,T,observed_quota_units=1,error_code=None,summary={})


def retry(probe):
    probe.reset();ids,jobs,videos,claims=seed(probe,1)
    fail(probe,videos,claims)
    # Job count is diagnostic: it cannot override the queue's retry deadline.
    probe.admin.execute('UPDATE public.operator_job_targets SET job_attempts=3 WHERE job_id=%s::uuid',(J,))
    job=settle(probe,jobs)
    status=probe.admin.scalar("SELECT status FROM public.operator_job_targets WHERE job_id=%s::uuid",(J,))
    probe.check('initial_error_keeps_pending_and_parent_due',status=='pending' and job['status']=='queued'
        and job['retry_not_before'] is not None and job['error_count']==0)
    probe.check('initial_not_reclaimed_before_common_deadline',probe.call(jobs.claim_initial_hydration,C,'initial_chunk_probe') is None)
    # Race guard at the final claim still respects a newer shared error deadline.
    probe.admin.execute("""UPDATE public.operator_jobs SET status='running',lease_token=%s::uuid,lease_owner='initial_chunk_probe',
        lease_channel_epoch=accepted_epoch,lease_expires_at=now()+interval '5 minutes' WHERE id=%s::uuid""",(T,J))
    probe.admin.execute("UPDATE public.video_collection_state SET last_claim_run_id=NULL WHERE video_id=%s",(ids[0],))
    result=probe.call(videos.claim_initial_video_ids,ids,job_id=J,channel_id=C,worker_id='initial_chunk_probe',run_id=T)
    probe.check('initial_claim_rechecks_backoff',result==[])
    prepared=probe.call(jobs.prepare_initial_hydration_chunk,J,T)
    probe.check('initial_prepare_defers_without_attempt',prepared['video_ids']==[] and prepared.get('deferred') is True
        and probe.admin.scalar('SELECT job_attempts FROM public.operator_job_targets WHERE job_id=%s::uuid',(J,))==3)
    # Once the single queue deadline expires, the first load can resume normally.
    probe.admin.execute("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 second' WHERE video_id=%s",(ids[0],))
    probe.admin.execute("UPDATE public.operator_jobs SET retry_not_before=now()-interval '1 second' WHERE id=%s::uuid",(J,))
    job=probe.call(jobs.claim_initial_hydration,C,'initial_chunk_probe')
    prepared=probe.call(jobs.prepare_initial_hydration_chunk,J,job['lease_token'])
    probe.check('initial_due_can_retry_past_diagnostic_attempt_count',prepared['video_ids']==ids)

    # A normal discovery sweep while the initial job failed disables playlists.
    # Retrying the frozen video chunk must not depend on restarting discovery.
    probe.reset();ids,jobs,videos,claims=seed(probe,3)
    fail(probe,videos,claims);settle(probe,jobs);pause_discovery(probe)
    probe.admin.execute("UPDATE public.operator_jobs SET status='queued',retry_not_before=now()-interval '1 second' WHERE id=%s::uuid",(J,))
    probe.admin.execute("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 second' WHERE video_id=ANY(%s::text[])",(ids,))
    job=probe.call(jobs.claim_initial_hydration,C,'initial_chunk_probe')
    unowned=probe.call(videos.claim_initial_video_ids,ids[1:],job_id=J,channel_id=C,
        worker_id='initial_chunk_probe',run_id=job['lease_token'])
    probe.check('paused_discovery_pending_requires_prepared_chunk',unowned==[])
    prepared=probe.call(jobs.prepare_initial_hydration_chunk,J,job['lease_token'])
    regular=probe.call(videos.claim_due_batch,limit=50,worker_id='regular_probe',run_id=str(uuid4()))
    probe.check('paused_discovery_does_not_admit_pending_to_regular',regular['rows']==[])
    claims=probe.call(videos.claim_initial_video_ids,prepared['video_ids'],job_id=J,channel_id=C,
        worker_id='initial_chunk_probe',run_id=job['lease_token'])
    probe.check('paused_discovery_initial_retry_claims_all_types',{c.video_id for c in claims}==set(ids))
    complete(probe,videos,claims)
    result=probe.call(jobs.settle_initial_hydration_chunk,J,job['lease_token'],observed_quota_units=1,error_code=None,summary={})
    probe.check('paused_discovery_initial_completion_is_independent',
        probe.admin.scalar("SELECT count(*) FROM public.operator_job_targets WHERE job_id=%s::uuid AND status='updated'",(J,))==3
        and probe.admin.scalar("SELECT count(*) FROM public.channel_playlist_state WHERE channel_id=%s AND NOT runtime_enabled",(C,))==3)


def pause_discovery(probe):
    """Reproduce the real error-owner sweep, preserving completed scan proof."""
    probe.admin.execute("UPDATE public.operator_jobs SET status='error' WHERE id=%s::uuid",(J,))
    probe.call(TypedInventoryRepository(probe.database).prepare_runtime,limit=50,enable=True)
    harness.fixtures.require(probe.admin.scalar("SELECT count(*) FROM public.channel_playlist_state WHERE channel_id=%s AND NOT runtime_enabled",(C,))==3,
        'initial_error_discovery_paused')


def handoff(probe):
    for status in ('queued','error'):
        probe.reset();ids,jobs,videos,claims=seed(probe)
        complete(probe,videos,claims[:3]);fail(probe,videos,claims[3:]);settle(probe,jobs)
        pause_discovery(probe)
        probe.admin.execute("UPDATE public.operator_jobs SET status=%s WHERE id=%s::uuid",(status,J))
        probe.admin.execute("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 second' WHERE video_id=ANY(%s::text[])",(ids,))
        result=probe.call(videos.claim_due_batch,limit=50,worker_id='regular_probe',run_id=str(uuid4()))
        probe.check('regular_all_types_while_initial_'+status,{c.video_id for c in result['rows']}==set(ids[:3])
            and result['paused_count']==0)
        probe.check('regular_never_claims_unfinished_initial_'+status,
            probe.admin.scalar('SELECT lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s',(ids[3],)) is True)
        epoch=probe.admin.scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(C,))
        forced=probe.call(videos.claim_video_metadata_ids,[ids[3]],channel_scopes=[dict(channel_id=C,control_epoch=epoch)],
            video_channels={ids[3]:C},worker_id='availability_probe',run_id=str(uuid4()))
        probe.check('availability_cannot_bypass_pending_initial_'+status,forced==[])

        targets_before=probe.admin.scalar('SELECT jsonb_agg(to_jsonb(target) ORDER BY video_id) FROM public.operator_job_targets target WHERE job_id=%s::uuid',(J,))
        completed=complete(probe,videos,result['rows'],regular=True)
        targets_after=probe.admin.scalar('SELECT jsonb_agg(to_jsonb(target) ORDER BY video_id) FROM public.operator_job_targets target WHERE job_id=%s::uuid',(J,))
        probe.check('regular_writes_all_types_without_initial_reentry_'+status,completed==3 and targets_before==targets_after
            and probe.admin.scalar('SELECT count(*) FROM public.video_snapshots')==6)



def fences(probe):
    for change in ('hidden','hold','epoch','blocked','wrong_type'):
        probe.reset();ids,jobs,videos,claims=seed(probe)
        complete(probe,videos,claims[:3]);fail(probe,videos,claims[3:]);settle(probe,jobs)
        pause_discovery(probe)
        probe.admin.execute("UPDATE public.operator_jobs SET status='queued' WHERE id=%s::uuid",(J,))
        if change=='hidden':probe.admin.execute('UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=now() WHERE channel_id=%s',(C,))
        elif change=='hold':probe.admin.execute("UPDATE public.channel_collection_state SET owner_permission='manual_required' WHERE channel_id=%s",(C,))
        elif change=='epoch':probe.admin.execute('UPDATE public.channel_collection_state SET control_epoch=control_epoch+1 WHERE channel_id=%s',(C,))
        elif change=='blocked':probe.admin.execute("UPDATE public.operator_jobs SET status='blocked' WHERE id=%s::uuid",(J,))
        else:probe.admin.execute("UPDATE public.operator_job_targets SET video_type=CASE WHEN video_type='long' THEN 'short' ELSE 'long' END WHERE job_id=%s::uuid AND video_id=ANY(%s::text[])",(J,ids[:3]))
        probe.admin.execute("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 second' WHERE video_id=ANY(%s::text[])",(ids,))
        result=probe.call(videos.claim_due_batch,limit=50,worker_id='regular_probe',run_id=str(uuid4()))
        probe.check('regular_handoff_fence_'+change,result['rows']==[])


def recovery(probe):
    probe.reset();ids,jobs,videos,claims=seed(probe)
    complete(probe,videos,claims[:3]);fail(probe,videos,claims[3:]);settle(probe,jobs)
    probe.admin.execute("UPDATE public.operator_jobs SET status='error' WHERE id=%s::uuid",(J,))
    probe.admin.execute("UPDATE public.video_collection_state SET paused_reason='channel_inventory_not_ready',next_due_at=NULL WHERE video_id=%s",(ids[0],))
    probe.call(backfill_video_collection_state,probe.database,50)
    probe.check('ready_long_recovers_old_initial_pause',probe.admin.scalar('SELECT paused_reason IS NULL AND next_due_at IS NOT NULL FROM public.video_collection_state WHERE video_id=%s',(ids[0],)) is True)
    # A repeated ensure must not erase an ordinary error backoff or re-pause a
    # successful Long just because another frozen target has failed.
    before=probe.admin.scalar('SELECT to_jsonb(state) FROM public.video_collection_state state WHERE video_id=%s',(ids[0],))
    probe.call(backfill_video_collection_state,probe.database,50)
    after=probe.admin.scalar('SELECT to_jsonb(state) FROM public.video_collection_state state WHERE video_id=%s',(ids[0],))
    probe.check('handoff_backfill_settles_without_perpetual_rewrite',before==after)


CASES={'retry':'Initial preparation and final video claim respect shared deadlines; no separate retry exhaustion.',
       'handoff':'Completed Long/Short/Stream targets refresh while parent initial queued or error.',
       'fences':'Hidden, held, stale generation, blocked and wrong typed target remain closed.',
       'recovery':'Previously paused ready Long recovers once through current queue maintenance.'}
RUNNERS={'retry':retry,'handoff':handoff,'fences':fences,'recovery':recovery}
if __name__=='__main__':
    harness.CASES=CASES;harness.RUNNERS=RUNNERS
    raise SystemExit(harness.main())
