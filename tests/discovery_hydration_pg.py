"""Video hydration, called by the parent's disposable-PG17 runner only.

run_checks(admin, database) uses a fresh synthetic channel, never a provider.
It leaves its bounded synthetic rows for the enclosing temporary database to
discard. No bootstrap, credentials, production DSN, or network client is here.
"""
from datetime import timedelta
from functools import partial
import os
from pathlib import Path
import sys
from uuid import uuid4
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests')]
from collection.discovery_video_hydration import REQUIRED_METADATA_PARTS, reusable_metadata_response
from collection.video_payloads import build_video_collection_metadata_payload, build_video_counter_payload
from db.channel.channel_sync import ChannelSyncRepository
from db.videos.video_collection import VideoCollectionRepository
from db.videos.video_queue_initialization import ensure_video_collection_state_in_transaction
from db.base.postgres import Operation
from video_collection_service import VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid
from youtube_source_fields import parse_dt

CHANNEL='UC'+'fxhydration'.ljust(22,'0')
IDS=['fxh00000001','fxh00000002','fxh00000003']


def run_checks(admin,database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
        raise RuntimeError('disposable_cluster_required')
    def scalar(sql,args=()):
        row=admin.execute(sql,args).fetchone()
        return next(iter(row.values())) if isinstance(row,dict) else row[0]
    if not 170000<=int(scalar('SHOW server_version_num'))<180000:
        raise RuntimeError('pg17_required')
    checks=[]
    def check(ok,label):
        if not ok:raise AssertionError(label)
        checks.append(label)
    def forbidden(*args,**kwargs):raise AssertionError('real_provider_forbidden')
    now=scalar('SELECT clock_timestamp()');old=now-timedelta(hours=2)
    ChannelSyncRepository(database).insert_channel(dict(channel_id=CHANNEL,title='Discovery hydration synthetic',
        user_input='https://www.youtube.com/channel/'+CHANNEL,initial_long_inventory_status='completed',
        initial_28d_stats_status='completed'),permission='accepted')
    job=str(uuid4())
    admin.execute("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,phase,initial_scope_version,
        initial_video_types,max_items,max_pages,max_quota_units,accepted_epoch,error_count)
        VALUES(%s,'channel_initial_long_inventory',%s,'completed','verify',0,ARRAY['long'],2500,50,100,1,0)""",(job,CHANNEL))
    admin.execute('UPDATE public.channel_collection_state SET initial_job_id=%s WHERE channel_id=%s',(job,CHANNEL))
    for identity in IDS:
        admin.execute("""INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at)
            VALUES(%s,%s,'long',%s,%s)""",(identity,CHANNEL,now-timedelta(days=10),old))
    with database.transaction(Operation('ensure_video_collection_state',statement_timeout_ms=8000)) as tx:
        ensure_video_collection_state_in_transaction(tx,IDS)
    check(scalar('SELECT count(*) FROM public.video_collection_state WHERE video_id=ANY(%s) AND discovery_hydration_requested_at IS NOT NULL',(IDS,))==3,
          'ordinary_inventory_initialization_marks_hydration')
    repo=VideoCollectionRepository(database)
    collector=VideoCollectionService(repo,fetch_full=forbidden,fetch_metadata=forbidden,fetch_counters=forbidden,
        fetch_initial_profiles=forbidden,
        build_full_payload=partial(build_video_collection_metadata_payload,batch_stats_enabled=True,parse_dt=parse_dt,
            view_count_absent_error=YouTubeViewCountAbsent,view_count_invalid_error=YouTubeViewCountInvalid),
        build_counter_payload=build_video_counter_payload,is_quota_exhausted=lambda error:False)
    def claim(ids,verify=True):
        return repo.claim_targeted_batch(ids,worker_id='Discovery synthetic',run_id=str(uuid4()),video_types=('long','short'),verify=verify)
    admin.execute("UPDATE public.video_collection_state SET next_due_at=now()+interval '2 days' WHERE video_id=%s",(IDS[0],))
    owned=claim([IDS[0]])['rows']
    check([row.video_id for row in owned]==[IDS[0]],'fresh_addressed_id_claimed_without_unrelated_due')
    busy=claim([IDS[0]])
    check(not busy['rows'] and busy['deferred'][0]['reason']=='video_owner_busy','live_owner_not_stolen')
    check(scalar('SELECT lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s',(IDS[1],)),
          'unrelated_due_id_not_substituted')
    observed=scalar('SELECT clock_timestamp()')-timedelta(seconds=20)
    received=observed+timedelta(seconds=1)
    item=dict(id=IDS[0],snippet=dict(channelId=CHANNEL,title='Discovery original clock',publishedAt=(now-timedelta(days=10)).isoformat()),
        contentDetails=dict(duration='PT10M'),status=dict(privacyStatus='public',uploadStatus='processed'),
        statistics=dict(viewCount='101',likeCount='7',commentCount='2'))
    response=reusable_metadata_response(collector,owned,requested_ids=[IDS[0]],items=[item],
        observed_at=observed,received_at=received,source_request_id=str(uuid4()),requested_parts=REQUIRED_METADATA_PARTS)
    attempt=str(uuid4())
    summary=collector.process_common_claims(owned,attempt_id=attempt,initial_response=response)
    check(summary['updated']==1 and not summary['settlement_unknown'],'prefetch_completed_through_existing_owner')
    check(scalar('SELECT recorded_at=%s FROM public.video_snapshots WHERE video_id=%s AND collection_attempt_id=%s',
                 (observed,IDS[0],attempt)),'durable_receipt_exact_attempt_original_clock')
    check(scalar('SELECT discovery_hydration_requested_at IS NULL FROM public.video_collection_state WHERE video_id=%s',(IDS[0],)),
          'hydration_marker_cleared_only_after_metadata_success')
    check(scalar('SELECT NOT hydration_needed AND lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s',(IDS[0],)),
          'canonical_hydration_and_lease_complete_together')
    missing=claim([IDS[1]])['rows']; failed_attempt=str(uuid4())
    absent=reusable_metadata_response(collector,missing,requested_ids=[IDS[1]],items=[],observed_at=observed,
        received_at=received,source_request_id=str(uuid4()),requested_parts=REQUIRED_METADATA_PARTS)
    collector.process_common_claims(missing,attempt_id=failed_attempt,initial_response=absent)
    check(scalar('SELECT last_failure_attempt_id=%s AND discovery_hydration_requested_at IS NOT NULL AND lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s',
          (failed_attempt,IDS[1])),'first_omission_exact_failure_retains_hydration_demand')
    wait=claim([IDS[1]])
    check(not wait['rows'] and wait['deferred'][0]['reason']=='canonical_retry_wait','omission_backoff_not_overridden')
    admin.execute('UPDATE public.video_collection_state SET discovery_hydration_requested_at=NULL WHERE video_id=%s',(IDS[2],))
    result=claim([IDS[2]],verify=False)
    check(not result['rows'] and result['deferred'][0]['reason']=='no_initial_hydration_demand','tail_requires_marker')
    with admin.transaction():
        admin.execute('SELECT video_id FROM public.video_collection_state WHERE video_id=%s FOR UPDATE',(IDS[2],))
        check(not claim([IDS[2]])['rows'],'concurrent_row_lock_skipped_without_substitute')
    # Current policy treats a past absent Long list as an active channel. Its
    # canonical initializer resumes the old pause after positive discovery.
    admin.execute("UPDATE public.channels SET initial_long_inventory_status='skipped_no_long_playlist' WHERE channel_id=%s",(CHANNEL,))
    admin.execute("UPDATE public.video_collection_state SET paused_reason='channel_no_long_playlist',next_due_at=NULL WHERE video_id=%s",(IDS[2],))
    check(not claim([IDS[2]])['rows'],'legacy_no_long_pause_remains_until_owner_initialization')
    proof=str(uuid4());checked=scalar('SELECT clock_timestamp()');playlist='UULF'+CHANNEL[2:]
    epoch=scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(CHANNEL,))
    admin.execute("INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,result,source_checked_at) VALUES('addressed_membership',%s,%s,%s)",
        (proof,Jsonb(dict(channel_id=CHANNEL,control_epoch=epoch,video_type='long',playlist_id=playlist,video_ids=[IDS[2]])),checked))
    admin.execute("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
        first_seen_at,last_seen_at,last_present_at,evidence_source,addressed_receipt_id)
        VALUES(%s,%s,'long',%s,%s,%s,%s,'addressed_membership',%s)""",(IDS[2],CHANNEL,playlist,checked,checked,checked,proof))
    with database.transaction(Operation('ensure_video_collection_state',statement_timeout_ms=8000)) as tx:
        ensure_video_collection_state_in_transaction(tx,[IDS[2]])
    positive=claim([IDS[2]])
    check([row.video_id for row in positive['rows']]==[IDS[2]],'first_long_uses_current_active_policy_and_canonical_initializer')
    check(scalar("SELECT initial_long_inventory_status='skipped_no_long_playlist' FROM public.channels WHERE channel_id=%s",(CHANNEL,)),
          'addressed_video_never_fabricates_full_channel_coverage')
    from discovery_admission import DiscoveryAdmissionDenied
    observed_fields='next_due_at,metadata_due_at,stats_due_at,consecutive_failures,last_error_type,last_failure_attempt_id'
    before=admin.execute('SELECT '+observed_fields+' FROM public.video_collection_state WHERE video_id=%s',(IDS[2],)).fetchone()
    def denied(*args,**kwargs):
        raise DiscoveryAdmissionDenied(dict(reason='window_budget',next_try_at=now+timedelta(hours=1)))
    collector.fetch_metadata=denied
    result=collector.process_common_claims(positive['rows'],attempt_id=str(uuid4()))
    after=admin.execute('SELECT '+observed_fields+' FROM public.video_collection_state WHERE video_id=%s',(IDS[2],)).fetchone()
    check(result.get('admission_denied') and result['errors']==0,'local_budget_denial_not_provider_error')
    check(before==after,'local_budget_denial_preserves_queue_deadlines_and_failure_counters')
    check(scalar('SELECT lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s',(IDS[2],)),
          'local_budget_denial_releases_exact_lease')
    return dict(checks=len(checks),passed=checks)
