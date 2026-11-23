"""Real initial/regular owners recover exact Long+Short conflicts on fresh PG17.

Only fixture creation uses administrator DML. Once preparation starts, normal
restricted queue/job owners perform every transition; General is synthetic.
"""
from datetime import timedelta

from native_pipeline_trace import _initial_fixture, collector, profile
from native_unified_availability import CHANNEL, IDS, KINDS, claim, seed, state
from db.jobs.operator_jobs import OperatorJobRepository
from db.videos.video_collection import VideoCollectionRepository
from db.videos.video_typed_reconciliation import reconcile_typed_video_collection_state
from collection.initial_video_hydration import run_initial_video_window


WORKER = 'unified-availability-proof'


def _membership(f, video, kind):
    """Use the fixture's already completed scan; keep its frozen source proof."""
    f.rows("""INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,
      source_published_at,first_seen_scan_id,last_seen_scan_id,membership_status,
      first_seen_at,last_seen_at,last_present_at)
      SELECT %s,state.channel_id,state.video_type,state.playlist_id,%s,
      scan.scan_id,scan.scan_id,'present',scan.source_checked_at,scan.source_checked_at,scan.source_checked_at
      FROM public.channel_playlist_state state JOIN public.channel_video_inventory_scans scan
      ON scan.scan_id=state.last_completed_scan_id WHERE state.channel_id=%s AND state.video_type=%s
      ON CONFLICT(video_id,channel_id,video_type) DO UPDATE SET membership_status='present',
      last_seen_scan_id=excluded.last_seen_scan_id,last_seen_at=excluded.last_seen_at,
      last_present_at=excluded.last_present_at""", (video, f.now-timedelta(days=200), CHANNEL, kind))


def _provider(published, duration, requested):
    def general(ids, reason):
        requested.extend(ids)
        rows=[profile(identity,published) for identity in ids]
        for row in rows:
            row['contentDetails']['duration']=duration
            if row['id']==IDS['stream']:
                row['liveStreamingDetails']={'actualEndTime':(published+timedelta(hours=1)).isoformat()}
        return rows
    return general


def _no_stats(*args):
    raise AssertionError('identity_and_hydration_use_only_general')


def _initial_transition(f, original, duration, expected, *, queue_missing=False, crossing_before_verify=False):
    job,token,published=_initial_fixture(f)
    video=IDS[original]
    for kind in ('long','short'):
        _membership(f,video,kind)
    # A new profile has no duration/counter authority before the first General.
    f.rows('UPDATE public.videos SET duration=NULL,views=NULL,like_count=NULL,comment_count=NULL,checked_at=NULL WHERE video_id=%s',(video,))
    if queue_missing:
        f.rows('DELETE FROM public.video_collection_state WHERE video_id=%s',(video,))
    jobs=OperatorJobRepository(f.database)
    prepared=jobs.prepare_initial_hydration_chunk(job,token,limit=50)
    f.check(video in prepared['video_ids'],'unresolved_conflict_keeps_initial_target')
    requested=[]
    service=collector(VideoCollectionRepository(f.database),_provider(published,duration,requested),_no_stats)
    result=run_initial_video_window(service,job_id=job,channel_id=CHANNEL,
        video_ids=prepared['video_ids'],worker_id=WORKER,run_id=token)
    f.check(not result.get('settlement_unknown') and not result.get('errors'),'initial_general_committed')
    f.check(requested.count(video)==1,'initial_conflict_one_general_request')
    f.check(f.scalar('SELECT video_type FROM public.video_inventory WHERE video_id=%s',(video,))==expected,
        'general_supplied_duration_selects_canonical')
    f.check(f.scalar("SELECT count(*) FROM public.typed_video_collection_receipts WHERE video_id=%s AND receipt_kind='identity_refresh'",(video,))==1,
        'initial_conflict_uses_identity_receipt')
    settled=jobs.settle_initial_hydration_chunk(job,token,observed_quota_units=1,error_code=None,summary=result)
    target=f.rows('SELECT video_type,status,result_code FROM public.operator_job_targets WHERE job_id=%s AND video_id=%s',(job,video))[0]
    f.check(target==dict(video_type=expected,status='checked',result_code='type_identity_checked'),
        'frozen_target_type_rebound_without_retry_error')
    f.check(settled['phase']=='verify' and settled['status']=='queued','initial_does_not_repeat_identity_hydration')
    if crossing_before_verify:
        # A regular cycle is already allowed for this checked target. Its next
        # real General crosses the boundary before the initial job verifies.
        regular=claim(f,kinds=(expected,),operation='metadata')['rows']
        f.check(any(row.video_id==video for row in regular),'checked_target_can_run_before_verify')
        service=collector(VideoCollectionRepository(f.database),_provider(published,'PT3M1S',requested),_no_stats)
        refreshed=service.process_common_claims(regular)
        f.check(not refreshed.get('errors') and not refreshed.get('settlement_unknown'),'crossing_general_committed')
        f.check(f.scalar('SELECT video_type FROM public.video_inventory WHERE video_id=%s',(video,))=='long',
            'regular_general_crossed_boundary')
        f.check(f.scalar('SELECT video_type FROM public.operator_job_targets WHERE job_id=%s AND video_id=%s',(job,video))=='short',
            'checked_target_still_has_previous_type_before_verify')
        expected='long'
    verifier=jobs.claim_initial_hydration(CHANNEL,WORKER)
    f.check(verifier is not None and verifier['phase']=='verify','normal_owner_claims_verification')
    verified=jobs.verify_initial_hydration(job,verifier['lease_token'])
    f.check(verified['status']=='completed' and not verified['summary_json']['current_profile_ready'],
        'initial_checked_handoff_not_false_profile_ready')
    f.check(f.scalar('SELECT video_type FROM public.operator_job_targets WHERE job_id=%s AND video_id=%s',(job,video))==expected,
        'verification_keeps_target_in_current_automatic_type')
    if not crossing_before_verify:
        regular=claim(f,kinds=(expected,),operation='metadata')['rows']
        selected=[row for row in regular if row.video_id==video]
        f.check(len(selected)==1 and selected[0].collection_mode in ('hydrate','metadata'),
            'next_regular_cycle_hydrates_resolved_type')
        refreshed=service.process_common_claims(regular)
        f.check(not refreshed.get('errors') and not refreshed.get('settlement_unknown'),'regular_general_committed')
    after=state(f,video)
    f.check(after['video_type']==expected and not after['hydration_needed'] and after['stats_due_at'] is not None,
        'regular_profile_schedules_normal_stats')
    f.check(f.scalar('SELECT views FROM public.videos WHERE video_id=%s',(video,))==23,'regular_counters_saved')
    f.check(f.scalar('SELECT count(*) FROM public.video_type_review_history')==0,'automatic_initial_is_not_manual_work')


def initial_long_to_short(f):
    _initial_transition(f,'long','PT3M','short')


def initial_short_without_queue_to_long(f):
    _initial_transition(f,'short','PT3M1S','long',queue_missing=True)


def initial_same_type_handoff(f):
    _initial_transition(f,'long','PT3M1S','long')


def initial_checked_target_crosses_again_before_verify(f):
    _initial_transition(f,'long','PT3M','short',crossing_before_verify=True)


def missing_short_queue_recovers(f):
    _,published=seed(f,operation='metadata')
    video=IDS['short']
    # Seed lacks Long playlist state; this helper creates valid fixture evidence.
    from native_automatic_type_policy import member
    member(f,video,'long')
    f.rows('UPDATE public.videos SET duration=NULL,views=NULL,like_count=NULL,comment_count=NULL,checked_at=NULL WHERE video_id=%s',(video,))
    f.rows('DELETE FROM public.video_collection_state WHERE video_id=%s',(video,))
    reconcile_typed_video_collection_state(f.database,'short',1000)
    initial=state(f,video)
    f.check(initial['hydration_needed'] and initial['stats_due_at'] is None,'gap_bootstrap_does_not_admit_stats')
    rows=claim(f,kinds=('short',),operation='metadata')['rows']
    f.check(len(rows)==1 and rows[0].video_id==video and rows[0].collection_mode=='identity_refresh',
        'gap_claim_refreshes_unknown_duration')
    requested=[]
    service=collector(VideoCollectionRepository(f.database),_provider(published,'PT3M',requested),_no_stats)
    result=service.process_common_claims(rows)
    f.check(not result.get('errors') and not result.get('settlement_unknown'),'gap_identity_committed')
    next_rows=claim(f,kinds=('short',),operation='metadata')['rows']
    f.check(len(next_rows)==1 and next_rows[0].collection_mode in ('hydrate','metadata'),
        'gap_identity_hands_off_without_waiting_identity_interval')
    result=service.process_common_claims(next_rows)
    f.check(not result.get('errors') and not result.get('settlement_unknown'),'gap_regular_committed')
    after=state(f,video)
    f.check(not after['hydration_needed'] and after['stats_due_at'] is not None,'gap_has_regular_stats_deadline')



def initial_discovery_pages_to_hydration(f):
    """Current scope-v1 writes real pages and derives its frozen targets itself."""
    from uuid import uuid4
    from schema25_regressions.cases_jobs import initial_job
    from native_unified_availability import instant
    from db.inventory.typed_inventory import TypedInventoryClaim,TypedInventoryRepository
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type IN('short','stream')")
    jobs,job=initial_job(f,CHANNEL)
    job_id,token=job['id'],job['lease_token']
    jobs.initialize_inventory_chains(job_id,token)
    inventory=TypedInventoryRepository(f.database)
    video=IDS['long'];published=f.now-timedelta(days=200)
    for kind in KINDS:
        prepared=jobs.prepare_inventory_chain(job_id,token,kind)
        f.check(prepared['action']=='inventory','fresh_chain_prepared_'+kind)
        source=jobs.claim_inventory_chain_source(job_id,token,kind,
            transition_id=prepared['transition_id'],worker_id=WORKER,run_id=str(uuid4()))
        f.check(len(source['claims'])==1,'fresh_chain_claimed_'+kind)
        owned=TypedInventoryClaim.from_row(source['claims'][0])
        scan=str(uuid4())
        inventory.begin_scan(owned,scan_id=scan,scan_boundary_at=instant(f))
        bound=jobs.bind_inventory_chain(job_id,token,kind,transition_id=prepared['transition_id'],
            scan_id=scan,playlist_lease_token=owned.lease_token)
        f.check(bound['dispatch_allowed'] is True,'fresh_chain_bound_'+kind)
        page=inventory.settle_page(owned,scan_id=scan,transition_id=prepared['transition_id'],
            expected_page_token=None,next_page_token=None,page_status='complete',
            items=[] if kind=='stream' else [dict(video_id=video,published_at=published.isoformat())],
            source_checked_at=instant(f))
        f.check(page['status']=='completed','fresh_page_accepted_'+kind)
        jobs.acknowledge_inventory_chain(job_id,token,kind,transition_id=prepared['transition_id'])
    frozen=jobs.finish_inventory_chains(job_id,token)
    f.check(frozen['phase']=='hydrate_videos' and frozen['status']=='queued' and frozen['target_count']==1,
        'real_full_pages_freeze_one_conflicting_video')
    f.check(f.scalar('SELECT video_type FROM public.operator_job_targets WHERE job_id=%s AND video_id=%s',(job_id,video))=='long',
        'no_duration_retains_provisional_canonical_target')
    hydrated_job=jobs.claim_initial_hydration(CHANNEL,WORKER)
    f.check(hydrated_job is not None,'fresh_discovery_hydration_claimed')
    token=hydrated_job['lease_token']
    prepared=jobs.prepare_initial_hydration_chunk(job_id,token,limit=50)
    f.check(prepared['video_ids']==[video],'real_pages_target_reaches_general')
    requested=[]
    service=collector(VideoCollectionRepository(f.database),_provider(published,'PT3M',requested),_no_stats)
    result=run_initial_video_window(service,job_id=job_id,channel_id=CHANNEL,
        video_ids=prepared['video_ids'],worker_id=WORKER,run_id=token)
    f.check(not result.get('errors') and not result.get('settlement_unknown'),'fresh_identity_general_committed')
    jobs.settle_initial_hydration_chunk(job_id,token,observed_quota_units=1,error_code=None,summary=result)
    verify=jobs.claim_initial_hydration(CHANNEL,WORKER)
    f.check(verify is not None and verify['phase']=='verify','fresh_hydration_reaches_verify')
    final=jobs.verify_initial_hydration(job_id,verify['lease_token'])
    f.check(final['status']=='completed','fresh_all_type_initial_completed')
    f.check(f.scalar('SELECT video_type FROM public.operator_job_targets WHERE job_id=%s AND video_id=%s',(job_id,video))=='short',
        'fresh_general_rebinds_provisional_target')
    regular=claim(f,kinds=('short',),operation='metadata')['rows']
    f.check(len(regular)==1 and regular[0].video_id==video and regular[0].collection_mode in ('hydrate','metadata'),
        'real_discovery_initial_hands_to_normal_queue')
    result=service.process_common_claims(regular)
    f.check(not result.get('errors') and not result.get('settlement_unknown'),'fresh_regular_hydration_committed')
    f.check(not state(f,video)['hydration_needed'] and state(f,video)['stats_due_at'] is not None,
        'fresh_all_type_discovery_has_normal_stats_schedule')


CASES=(initial_long_to_short,initial_short_without_queue_to_long,initial_same_type_handoff,missing_short_queue_recovers,initial_checked_target_crosses_again_before_verify,initial_discovery_pages_to_hydration)
