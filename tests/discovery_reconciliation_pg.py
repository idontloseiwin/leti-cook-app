"""Real Discovery obligation isolation/retry/pause boundaries; disposable PG17 only."""
from datetime import timedelta
from hashlib import sha256
import os
from types import SimpleNamespace
from uuid import uuid4

from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository, operation
from db.discovery_budget import DiscoveryBudgetRepository
from db.discovery_observations import resume_matching
from db.inventory.typed_inventory import TypedInventoryRepository
from db.videos.video_collection import VideoCollectionRepository
from db.videos.video_queue_initialization import ensure_video_collection_state_in_transaction
from db.websub import WebSubRepository
from discovery_policy import DiscoverySettings
from discovery_service import DiscoveryService


CHANNEL='UC'+'sourceprobe'.ljust(22,'0')
OTHER='UC'+'sourceother'.ljust(22,'0')


def run_checks(admin,database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
        raise RuntimeError('disposable_cluster_required')
    checks=[]

    def scalar(sql,args=()):
        row=admin.execute(sql,args).fetchone()
        return next(iter(row.values())) if isinstance(row,dict) else row[0]

    def check(value,name):
        if not value:raise AssertionError(name)
        checks.append(name)

    class NoDatabase:
        def __getattr__(self,name):
            raise AssertionError('empty_resume_must_not_touch_database')

    check(resume_matching(NoDatabase(),CHANNEL,[])==0,'empty_page_resume_is_noop_without_database')

    def now():return scalar('SELECT clock_timestamp()')

    def event(identity,channel=CHANNEL,kind='entry'):
        return scalar('INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind) '
            'VALUES(%s,%s,%s,%s) RETURNING event_id',
            (sha256(str(uuid4()).encode()).hexdigest(),channel,identity,kind))

    def take(ids):
        # Shared disposable test DB only: isolate this fixture's exact due set.
        admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending'")
        admin.execute("UPDATE public.websub_events SET next_try_at=now()-interval '1 second' WHERE event_id=ANY(%s)",(ids,))
        return [row for row in repo.claim_event_batch(50) if int(row['work_id']) in ids]

    def start(rows,*,metadata=False):
        request=str(uuid4());at=now()
        repo.start_request(rows,request,at)
        observed=now()
        if metadata:repo.complete_attempt(rows,request,observed)
        return request,observed

    channels=ChannelSyncRepository(database)
    for channel in (CHANNEL,OTHER):
        channels.insert_channel(dict(channel_id=channel,title='Discovery review synthetic',
            user_input='https://www.youtube.com/channel/'+channel,
            initial_long_inventory_status='completed',initial_28d_stats_status='completed'),permission='accepted')
    TypedInventoryRepository(database).prepare_runtime(limit=1000,enable=True)
    # Fixture-only enablement. The deployment activation contract has its own
    # dedicated tests; this does not pretend to exercise a prepared Discovery host.
    admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,clock_timestamp())')
    repo=DiscoveryRepository(database)

    good=[f'fxmix{i:06}' for i in range(49)];bad='fxmixwrong1'
    for identity in good:
        admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(identity,CHANNEL))
    admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(bad,OTHER))
    with database.transaction(operation('discovery_fixture_queue')) as tx:
        ensure_video_collection_state_in_transaction(tx,good+[bad])
    ids=[event(bad),*[event(identity) for identity in good]]
    admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending' AND NOT(event_id=ANY(%s))",(ids,))
    reached=[]

    def forbidden(*args,**kwargs):raise AssertionError('mixed_known_batch_must_not_call_provider')

    service=DiscoveryService(repo,SimpleNamespace(fetch_metadata=forbidden),
        DiscoveryBudgetRepository(database,project_id='Discovery review mixed',settings=DiscoverySettings(),interval_minutes=10),
        settings=DiscoverySettings(),fetch_membership=forbidden,worker_id='Discovery mixed',run_id=str(uuid4()))
    # Native claim and target classification; stop at the existing VIDEO-owner
    # handoff boundary, whose actual writer is covered by hydration tests.
    service._hydrate=lambda rows,admission,report,**kwargs:(reached.extend(rows) or iter(()))
    result=service.run_once(limit=50)
    check(result['claimed']==50 and result['settled']==1,'mixed50_one_rejected_and49_continue')
    check(not reached,'known_events_use_ordinary_queue_without_targeted_fetch')
    check(scalar("SELECT status='skipped' AND outcome='channel_mismatch' AND claim_token IS NULL FROM public.websub_events WHERE event_id=%s",(ids[0],)),
          'bad_envelope_is_rejection_not_writer_ack')
    check(scalar("SELECT count(*) FROM public.websub_events WHERE event_id=ANY(%s) AND status='pending' AND claim_token IS NULL AND wait_reason='owner_busy'",(ids[1:],))==49,
          'known_events_wait_for_ordinary_owner_with_no_lease')

    # One ID addressed by correct and incorrect channel envelopes stays distinct.
    pair=[event(good[0],OTHER),event(good[0],CHANNEL)]
    rows=take(pair);facts=repo.target_state(rows)
    check(len(facts)==2 and facts[str(pair[0])]['channel_mismatch'] and not facts[str(pair[1])]['channel_mismatch'],
          'target_facts_keyed_by_work_not_shared_video_id')
    repo.terminal_observation([r for r in rows if r['work_id']==str(pair[0])],'channel_mismatch')
    repo.defer([r for r in rows if r['work_id']==str(pair[1])],'owner_busy',now()+timedelta(days=1))

    # Canonical identity is inserted by another owner after the initial read.
    race_ids=['fxracegood1','fxracewrong']
    events=[event(i) for i in race_ids];rows=take(events)
    check(not any(fact['channel_mismatch'] for fact in repo.target_state(rows).values()),'race_initial_target_read_has_no_conflict')
    request,observed=start(rows)
    admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type) VALUES(%s,%s,'long')",(race_ids[1],OTHER))
    membership=repo.record_membership(rows,request_id=request,video_type='short',playlist_id='UUSH'+CHANNEL[2:],
        source_checked_at=observed,transition_id=str(uuid4()),items=[dict(video_id=i) for i in race_ids])
    check(membership['accepted_work_ids']==[str(events[0])] and membership['rejected_work_ids']==[str(events[1])],
          'addressed_race_isolates_bad_id_under_canonical_locks')
    check(scalar('SELECT channel_id FROM public.video_inventory WHERE video_id=%s',(race_ids[1],))==OTHER,
          'addressed_race_never_reassigns_channel')
    check(scalar('SELECT video_type FROM public.video_inventory WHERE video_id=%s',(race_ids[0],))=='short',
          'addressed_race_commits_good_neighbour')

    # A deleted envelope may finish only from its own accepted empty response.
    deleted,entry=event('fxdelete001',kind='deleted'),event('fxdelete002')
    rows=take([deleted,entry]);request,observed=start(rows,metadata=True)
    done=repo.terminal_observation(rows,'unknown_deletion_checked',request_id=request,observed_at=observed,items=[])
    check(done==[str(deleted)] and scalar('SELECT status FROM public.websub_events WHERE event_id=%s',(entry,))=='pending',
          'empty_response_rejects_only_exact_deleted_envelope')
    repo.defer([r for r in rows if r['work_id']==str(entry)],'metadata_not_returned',now()+timedelta(days=1),request_id=request)

    stream=event('fxstream001');rows=take([stream]);request,observed=start(rows)
    repo.mark_disabled_probe(rows,request,observed)
    done=repo.terminal_observation(rows,'format_off',request_id=request,observed_at=observed,
        playlist_id='UULV'+CHANNEL[2:],items=[dict(video_id='fxstream001')],enabled_types=('long','short'))
    check(done==[str(stream)] and scalar('SELECT count(*) FROM public.video_inventory WHERE video_id=%s',('fxstream001',))==0,
          'positive_disabled_stream_proof_settles_without_catalogue_write')
    check(scalar('SELECT disabled_stream_probe_at FROM public.websub_events WHERE event_id=%s',(stream,))==observed,
          'disabled_probe_observation_is_durable')

    unavailable=event(good[1])
    admin.execute("UPDATE public.video_collection_state SET paused_reason='availability_retry_expired' WHERE video_id=%s",(good[1],))
    rows=take([unavailable]);repo.pause(rows,'availability_retry_expired')
    check(scalar("SELECT status='pending' AND pause_reason='availability_retry_expired' AND wait_reason='availability_retry_expired' FROM public.websub_events WHERE event_id=%s",(unavailable,)),
          'expired_availability_preserves_exact_pause_reason_without_ack')
    check(take([unavailable])==[],'availability_pause_is_not_polled_forever')

    # A canonical open incident is stronger evidence than the queue's retry state.
    admin.execute("""INSERT INTO public.video_availability_incidents(video_id,channel_id,first_status,latest_status,
        first_seen_at,last_seen_at,source_first_seen,source_latest_seen)
        VALUES(%s,%s,'unknown_unavailable','unknown_unavailable',now(),now(),
               'typed_video_collection_missing','typed_video_collection_missing')""",(good[1],CHANNEL))
    removed=event(good[1],kind='deleted')
    claims=take([removed])
    check(repo.resolve_collected(claims,enabled_types=('long','short'))==[str(removed)],
          'confirmed_unavailable_deletion_closes_even_after_retry_expiry')
    review=event('fxreview001')
    rows=take([review])
    request,_=start(rows,metadata=True)
    request,observed=start(rows)
    repo.record_membership_check(rows,request,observed)
    repo.defer(rows,'membership_not_observed',now()+timedelta(hours=6),lifecycle_observed=True)
    check(scalar("""SELECT review_required AND status='pending' AND next_try_at>=now()+interval '23 hours 59 minutes'
        FROM public.websub_events WHERE event_id=%s""",(review,)), 'first_complete_unmatched_check_enters_daily_review')

    # Phase failures are durable and independent of evidence/other-phase success.
    fault=event('fxfault0001');rows=take([fault]);request,_=start(rows)
    repo.defer(rows,'retry',now(),request_id=request,technical_phase='metadata')
    check(scalar('SELECT technical_metadata_failures=1 AND discovery_attempts=0 FROM public.websub_events WHERE event_id=%s',(fault,)),
          'transport_failure_increments_fault_not_evidence')
    rows=take([fault]);request,observed=start(rows,metadata=True)
    request,observed=start(rows)
    repo.defer(rows,'retry',now(),request_id=request,technical_phase='membership')
    rows=take([fault]);request,observed=start(rows,metadata=True)
    check(scalar('SELECT technical_metadata_failures=0 AND technical_membership_failures=1 AND discovery_attempts=2 FROM public.websub_events WHERE event_id=%s',(fault,)),
          'metadata_success_does_not_erase_membership_streak')
    request,observed=start(rows);repo.record_membership_check(rows,request,observed)
    check(scalar('SELECT technical_membership_failures FROM public.websub_events WHERE event_id=%s',(fault,))==0,
          'completed_membership_stage_resets_own_streak')
    repo.defer(rows,'membership_not_observed',now()+timedelta(days=1))

    paused=event('fxpause0001')
    admin.execute("UPDATE public.websub_events SET received_at=now()-interval '31 days' WHERE event_id=%s",(paused,))
    check(take([paused])==[],'old_unresolved_work_enters_durable_pause')
    before=scalar('SELECT received_at FROM public.websub_events WHERE event_id=%s',(paused,))
    check(repo.resume(CHANNEL,['fxpause0001'])==1,'explicit_resume_opens_one_bounded_attempt')
    rows=take([paused]);check(len(rows)==1,'paused_work_can_resume_without_rewriting_age')
    repo.defer(rows,'budget',now()+timedelta(minutes=10))
    rows=take([paused]);check(len(rows)==1,'budget_refusal_does_not_consume_resume')
    repo.defer(rows,'retry',now(),technical_phase='metadata')
    rows=take([paused]);check(len(rows)==1,'pre_http_failure_does_not_consume_resume')
    request,observed=start(rows,metadata=True)
    repo.defer(rows,'metadata_not_returned',now(),request_id=request)
    check(take([paused])==[],'one_meaningful_unresolved_response_repauses_old_work')
    check(scalar('SELECT received_at FROM public.websub_events WHERE event_id=%s',(paused,))==before,'resume_preserves_original_receive_clock')
    # Fresh accepted delivery resumes, duplicate redelivery never refills it.
    notice=SimpleNamespace(dedupe_key=sha256(str(uuid4()).encode()).hexdigest(),channel_id=CHANNEL,
        video_id='fxpause0001',kind='entry',published_at=now(),updated_at=now())
    ingress=WebSubRepository(database)
    check(ingress.delivery(CHANNEL,[notice],size=1)==1,'fresh_websub_resume_hook_accepted')
    token=scalar('SELECT resume_token FROM public.websub_events WHERE event_id=%s',(paused,))
    check(ingress.delivery(CHANNEL,[notice],size=1)==0 and scalar('SELECT resume_token FROM public.websub_events WHERE event_id=%s',(paused,))==token,
          'duplicate_websub_does_not_refill_resume_token')
    check(len(take([paused]))==1,'fresh_websub_wakes_paused_original_obligation')

    # A valid complete custom profile must not be checked against unrelated
    # defaults reconstructed inside the database adapter.
    custom=DiscoverySettings(retry_first_seconds=120,retry_second_seconds=300,
        retry_third_seconds=600,retry_sparse_seconds=600,review_retry_seconds=3600)
    custom_id=event('fxcustom001')
    admin.execute("UPDATE public.websub_events SET received_at=now()-interval '8 days' WHERE event_id=%s",(custom_id,))
    admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending' AND event_id<>%s",(custom_id,))
    rows=[r for r in repo.claim_event_batch(50,settings=custom) if r['work_id']==str(custom_id)]
    check(len(rows)==1,'custom_full_policy_admitted_by_claim')
    request,observed=start(rows,metadata=True)
    check(repo.release_failed_owner(rows,settings=custom)==[],'custom_full_policy_accepted_by_owner_recovery')
    before=now()
    repo.defer(rows,'metadata_not_returned',before+timedelta(seconds=120),request_id=request,settings=custom)
    after=now();due=scalar('SELECT next_try_at FROM public.websub_events WHERE event_id=%s',(custom_id,))
    check(before+timedelta(hours=1)<=due<=after+timedelta(hours=1),
          'custom_review_delay_uses_one_hour_not_default_day_or_sparse_limit')

    # Lifecycle cadence is subordinate to the effective review age unless an
    # accepted future date currently pauses that age. No type is inferred here.
    for identity,future_minutes in (('fxlifeold01',None),('fxlifefut01',45)):
        event_id=event(identity)
        admin.execute("UPDATE public.websub_events SET received_at=now()-interval '8 days' WHERE event_id=%s",(event_id,))
        rows=take([event_id]);request,observed=start(rows,metadata=True)
        future=observed+timedelta(minutes=future_minutes) if future_minutes else None
        proposed=future if future is not None else observed+timedelta(minutes=15)
        before=now()
        repo.defer(rows,'lifecycle_wait',proposed,request_id=request,
            future_until=future,lifecycle_observed=True)
        after=now();due=scalar('SELECT next_try_at FROM public.websub_events WHERE event_id=%s',(event_id,))
        check(due==proposed if future is not None else before+timedelta(days=1)<=due<=after+timedelta(days=1),
              'active_future_keeps_confirmed_start_deadline' if future is not None else 'old_unknown_lifecycle_uses_daily_interval')
        check(scalar('SELECT review_required FROM public.websub_events WHERE event_id=%s',(event_id,)),
              'future_pause_preserves_previous_review_age' if future is not None else 'old_unknown_lifecycle_remains_review_required')

    # Actual source receipt, native VIDEO claim, binding and lifecycle writer;
    # recovery must keep the owner's due intact and only slow this obligation.
    identity='fxlifeown01';event_id=event(identity)
    admin.execute("UPDATE public.websub_events SET received_at=now()-interval '8 days' WHERE event_id=%s",(event_id,))
    rows=take([event_id]);request,observed=start(rows)
    repo.record_membership(rows,request_id=request,video_type='long',playlist_id='UULF'+CHANNEL[2:],
        source_checked_at=observed,transition_id=str(uuid4()),items=[dict(video_id=identity)])
    video_repo=VideoCollectionRepository(database)
    owned=video_repo.claim_targeted_batch([identity],worker_id='Discovery lifecycle review',run_id=str(uuid4()),
        video_types=('long','short'),verify=True)['rows']
    check(len(owned)==1,'old_lifecycle_has_real_video_owner')
    attempt=str(uuid4());lease=str(owned[0].lease_token)
    repo.bind_owner(rows,{identity:dict(lease_token=lease,attempt_id=attempt)})
    observed=now()
    check(video_repo.complete_stream_lifecycle([dict(video_id=identity,channel_id=CHANNEL,video_type='long',
        lease_token=lease,collection_attempt_id=attempt,recorded_at=observed.isoformat(),
        stream_actual_end_at=None,availability_status='public',privacy_status='public',upload_status='processed',
        title='Synthetic unfinished publication',published_at=(observed-timedelta(hours=1)).isoformat(),
        duration='PT0S',publication_status='live',scheduled_start_at=None)])==1,
        'old_lifecycle_completed_through_native_probe_writer')
    canonical_due=scalar('SELECT next_due_at FROM public.video_collection_state WHERE video_id=%s',(identity,))
    check(canonical_due<observed+timedelta(days=1),'native_lifecycle_cadence_is_shorter_than_review_floor')
    before=now();released=repo.release_failed_owner(rows);after=now()
    due=scalar('SELECT next_try_at FROM public.websub_events WHERE event_id=%s',(event_id,))
    check(released==[str(event_id)] and max(canonical_due,before+timedelta(days=1))<=due<=max(canonical_due,after+timedelta(days=1)),
          'old_owner_stream_probe_uses_same_daily_floor_without_advancing_owner')
    check(scalar('SELECT next_due_at FROM public.video_collection_state WHERE video_id=%s',(identity,))==canonical_due,
          'discovery_review_does_not_rewrite_canonical_video_schedule')
    return checks
