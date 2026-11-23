"""Shared positive heads through the real owners on disposable PostgreSQL 17.

No network or source scan completion. A late real fixture event remains pending;
a first page records positive observations without claiming complete list coverage.
"""
from datetime import timedelta
from functools import partial
from hashlib import sha256
import os
from uuid import uuid4

from collection.video_payloads import build_video_collection_metadata_payload, build_video_counter_payload
from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository
from db.discovery_budget import DiscoveryBudgetRepository
from db.inventory.typed_inventory import TypedInventoryRepository
from db.videos.video_collection import VideoCollectionRepository
from discovery_policy import DiscoverySettings
from discovery_service import DiscoveryService
from video_collection_service import VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid
from youtube_source_fields import parse_dt


CHANNEL = 'UC' + 'fxsharedhead'.ljust(22, '0')
IDS = ['fxhead00001', 'fxhead00002']
UNREQUESTED = 'fxheadother'


def run_checks(admin, database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE') != '1':
        raise RuntimeError('disposable_cluster_required')

    checks = []

    def scalar(statement, parameters=()):
        row = admin.execute(statement, parameters).fetchone()
        return next(iter(row.values())) if isinstance(row, dict) else row[0]

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def forbidden(*args, **kwargs):
        raise AssertionError('unexpected_provider_request')

    now = scalar('SELECT clock_timestamp()')
    channel = ChannelSyncRepository(database)
    channel.insert_channel(dict(channel_id=CHANNEL, title='Discovery shared head synthetic',
        user_input='https://www.youtube.com/channel/' + CHANNEL,
        initial_long_inventory_status='completed', initial_28d_stats_status='completed'), permission='accepted')
    epoch = scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s', (CHANNEL,))
    # Completed original initial ownership; new first-format evidence still has
    # to create its full-coverage intent rather than pretending that scan ran.
    job = str(uuid4())
    admin.execute("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,phase,initial_scope_version,
        initial_video_types,max_items,max_pages,max_quota_units,accepted_epoch,error_count)
        VALUES(%s,'channel_initial_long_inventory',%s,'completed','verify',0,ARRAY['long'],2500,50,100,%s,0)""",
        (job, CHANNEL, epoch))
    admin.execute('UPDATE public.channel_collection_state SET initial_job_id=%s WHERE channel_id=%s', (job, CHANNEL))
    TypedInventoryRepository(database).prepare_runtime(limit=1000, enable=True)
    discovery = DiscoveryRepository(database)
    # Fixture-only activation: this disposable test exercises grouping, not
    # the prepared-host/current-epoch cutover gate tested in coverage fixtures.
    admin.execute("UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,clock_timestamp())")
    # Other helpers share this disposable database; exclude their unrelated due
    # events from this precise two-ID unit without manufacturing event outcomes.
    admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending'")

    def event(identity):
        return scalar("""INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind)
            VALUES(%s,%s,%s,'entry') RETURNING event_id""",
            (sha256(str(uuid4()).encode()).hexdigest(), CHANNEL, identity))

    # Anchor the due window so an earlier fixture cannot enter this request.
    admin.execute("UPDATE public.video_collection_state SET next_due_at=now()+interval '2 days' WHERE next_due_at IS NOT NULL")
    fill_ids=['fxfillprof1','fxfillstat1']
    def seed_video(identity, seconds, *, metadata=False):
        due=now+timedelta(seconds=seconds)
        previous=now-timedelta(days=1)
        admin.execute("""INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at)
            VALUES(%s,%s,'long',%s,%s)""",(identity,CHANNEL,previous,previous))
        admin.execute("""INSERT INTO public.videos(video_id,channel_id,published_at,checked_at,metadata_checked_at,
            availability_checked_at,availability_status,privacy_status,upload_status,views,like_count,comment_count)
            VALUES(%s,%s,%s,%s,%s,%s,'public','public','processed',1,1,0)""",
            (identity,CHANNEL,previous,previous,previous,previous))
        admin.execute("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,hydration_needed,
            profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,stats_source_checked_at,metadata_source_checked_at)
            VALUES(%s,%s,'long',false,1,%s,%s,%s,%s,%s)""",(identity,CHANNEL,due,
            due if metadata else now+timedelta(days=2),due,previous,previous))
    seed_video(fill_ids[0],-60,metadata=True)
    seed_video(fill_ids[1],-120)
    original = [event(identity) for identity in IDS]
    late = []
    calls = []

    def metadata(identities, phase):
        check(list(identities) == IDS+fill_ids, 'notifications_lead_then_due_metadata_and_counters')
        calls.append(('metadata', tuple(identities)))
        return [dict(id=identity, snippet=dict(channelId=CHANNEL, title='Synthetic shared Short',
            publishedAt=(now - timedelta(hours=1)).isoformat()), contentDetails=dict(duration='PT1M'),
            status=dict(privacyStatus='public', uploadStatus='processed'),
            statistics=dict(viewCount='12', likeCount='1', commentCount='0')) for identity in identities]

    def page(*identities):
        return {'items': [dict(contentDetails=dict(videoId=identity)) for identity in identities]}

    def membership(playlist, identity):
        check(playlist == 'UUSH' + CHANNEL[2:], 'short_confirmation_never_probes_other_formats')
        calls.append(('membership', identity))
        if identity is None:
            check(scalar("""SELECT count(DISTINCT source_request_id)=1
                AND bool_and(source_request_id IS NOT NULL) FROM public.websub_events
                WHERE event_id=ANY(%s)""", (original,)), 'shared_request_frozen_for_both_original_events')
            late.append(event(IDS[0]))
            return dict(page(IDS[0], UNREQUESTED), pageInfo={'totalResults':17}, nextPageToken='unused-deeper-page')
        check(identity == IDS[1], 'only_deep_remainder_gets_exact_request')
        return page(identity)

    video = VideoCollectionService(VideoCollectionRepository(database), fetch_full=forbidden,
        fetch_metadata=metadata, fetch_counters=forbidden, fetch_initial_profiles=forbidden,
        build_full_payload=partial(build_video_collection_metadata_payload, batch_stats_enabled=True,
            parse_dt=parse_dt, view_count_absent_error=YouTubeViewCountAbsent,
            view_count_invalid_error=YouTubeViewCountInvalid),
        build_counter_payload=build_video_counter_payload, is_quota_exhausted=lambda error: False)
    settings = DiscoverySettings()
    project = 'discovery-coalescing-' + str(uuid4())
    budget = DiscoveryBudgetRepository(database, project_id=project, settings=settings, interval_minutes=10)
    service = DiscoveryService(discovery, video, budget, settings=settings, fetch_membership=membership,
        worker_id='Discovery shared head', run_id=str(uuid4()),claim_fill=video.repository.claim_due_fill)
    result = service.run_once()
    check(result['claimed'] == 2 and result['discovered'] == 2 and result['settled'] == 2,
          'both_original_ids_complete_through_real_video_owner')
    check(calls == [('metadata', tuple(IDS+fill_ids)), ('membership', None), ('membership', IDS[1])],
          'one_shared_head_one_deep_request_no_second_metadata_download')
    check(scalar("SELECT count(*) FROM public.websub_events WHERE event_id=ANY(%s) AND status='done' AND outcome='writer_receipt'",
                 (original,)) == 2, 'exact_original_event_receipts_are_done')
    check(scalar("SELECT count(*) FROM public.videos WHERE video_id=ANY(%s) AND views=12",(fill_ids,))==2,
          'both_ordinary_claims_finished_from_same_general_response')
    check(scalar("SELECT metadata_source_checked_at<%s FROM public.video_collection_state WHERE video_id=%s",
                 (now,fill_ids[1])), 'counter_only_fill_preserves_metadata_clock')
    check(len(late) == 1 and scalar('SELECT status FROM public.websub_events WHERE event_id=%s', (late[0],)) == 'pending',
          'event_received_during_shared_http_not_acknowledged')
    check(scalar('SELECT count(*) FROM public.video_inventory WHERE video_id=%s', (UNREQUESTED,)) == 1,
          'daily_head_records_other_positive_memberships')
    check(scalar("SELECT count(*) FROM public.video_inventory WHERE video_id=ANY(%s) AND channel_id=%s AND video_type='short'",
                 (IDS, CHANNEL)) == 2, 'requested_videos_have_confirmed_short_membership')
    check(scalar("SELECT count(*) FROM public.typed_inventory_receipts WHERE receipt_scope='addressed_membership' AND result->>'channel_id'=%s AND NOT(result ? 'observation_kind')",
                 (CHANNEL,)) == 2, 'head_and_exact_positive_receipts_are_separate_from_scans')
    check(scalar('SELECT count(*) FROM public.channel_video_inventory_scans WHERE channel_id=%s', (CHANNEL,)) == 0,
          'shared_head_does_not_create_or_finish_scan')
    check(scalar("""SELECT full_reconcile_required_token IS NOT NULL AND inventory_completed_at IS NULL
        FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'""", (CHANNEL,)),
          'first_format_full_intent_remains_without_false_coverage')
    check(scalar('SELECT count(*) FROM public.discovery_budget_grants WHERE project_id=%s', (project,)) == 3,
          'shared_http_charged_once_with_exact_fallback_charge')
    check(scalar("""SELECT head_next_due_at>now() AND (head_last_attempt->>'total_results')::int=17
        FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'""",(CHANNEL,)),
          'shared_first_page_serves_daily_head_and_keeps_provider_total')
    early='fxfillsoon1'
    tail='fxfilltail1'
    late_id='fxfilllate1'
    retry='fxfillwait1'
    for identity,seconds in [(early,40),(tail,400),(late_id,700),(retry,420)]:
        seed_video(identity,seconds)
    admin.execute("UPDATE public.video_collection_state SET last_error_type='provider_rate_limited' WHERE video_id=%s",(retry,))
    scopes=[dict(channel_id=CHANNEL,video_type='long')]
    request=dict(limit=50,worker_id='ordinary fill fixture',run_id=str(uuid4()),scopes=scopes,
                 operation='counters',video_types=('long','short'),fill_final=True)
    batch=video.repository.claim_due_batch(**request)
    check({row.video_id for row in batch['rows']}=={early,tail} and batch['fill_ids']=={tail},
          'one_minute_admission_and_ten_minute_tail_respect_backoff')
    check(not video.repository.claim_due_batch(**{**request,'run_id':str(uuid4())})['rows'],
          'future_work_alone_does_not_start_a_request')
    # A publication can enter waiting after selection. Only its counter write is refused.
    admin.execute("UPDATE public.videos SET publication_status='upcoming' WHERE video_id=%s",(tail,))
    video.complete_observed_counters([(row,dict(id=row.video_id,views=45)) for row in batch['rows']],
                                    observed_at=scalar('SELECT clock_timestamp()'))
    check(scalar('SELECT views FROM public.videos WHERE video_id=%s',(early,))==45
          and scalar('SELECT views FROM public.videos WHERE video_id=%s',(tail,))==1,
          'waiting_publication_does_not_roll_back_the_other_counter')
    admin.execute("UPDATE public.video_collection_state SET next_due_at=now(),stats_due_at=now() WHERE video_id=%s",(tail,))
    waiting=video.repository.claim_due_batch(limit=50,worker_id='publication fixture',run_id=str(uuid4()),
        scopes=scopes,operation='metadata',video_types=('long','short'))['rows']
    check(any(row.video_id==tail and row.collection_mode=='metadata' for row in waiting),
          'waiting_video_only_enters_metadata')
    video.repository.release_discovery_admission(waiting)
    # A waiting marker kept only in the catalogue is seen at the locked prefix.
    admin.execute("""UPDATE public.video_collection_state SET typed_collection_status=NULL,
        last_error_type=NULL,hydration_needed=false,next_due_at=now(),stats_due_at=now(),
        metadata_due_at=now()+interval '1 day' WHERE video_id=%s""", (tail,))
    before_attempt = scalar('SELECT attempt_count FROM public.video_collection_state WHERE video_id=%s', (tail,))
    stats = video.repository.claim_due_batch(**{**request,'run_id':str(uuid4()),'fill_final':False})['rows']
    check(all(row.video_id != tail for row in stats) and scalar("""SELECT lease_token IS NULL
          AND metadata_due_at<=now() AND attempt_count=%s FROM public.video_collection_state
          WHERE video_id=%s""", (before_attempt, tail)), 'catalogue_only_marker_redirects_stats_without_lease')
    video.repository.release_discovery_admission(stats)
    recovered = video.repository.claim_due_batch(**{**request,'operation':'metadata','run_id':str(uuid4()),
                                                   'fill_final':False})['rows']
    check(any(row.video_id==tail and row.collection_mode=='metadata' for row in recovered),
          'promoted_catalogue_only_marker_reaches_general')
    video.repository.release_discovery_admission(recovered)
    admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending'")
    admin.execute("UPDATE public.video_collection_state SET next_due_at=now()+interval '2 days' WHERE next_due_at IS NOT NULL")
    burst=[f'fxr{i:08d}' for i in range(12)]
    events=[event(identity) for identity in burst]
    calls=[]
    def profiles(identities,phase):
        calls.append(('profiles',tuple(identities)))
        views='99' if len(identities)==4 else '12'
        return [dict(id=i,snippet=dict(channelId=CHANNEL,title='Burst fixture',publishedAt=now.isoformat()),
            contentDetails=dict(duration='PT4M'),status=dict(privacyStatus='public',uploadStatus='processed'),
            statistics=dict(viewCount=views,likeCount='1',commentCount='0')) for i in identities]
    video.fetch_metadata=profiles
    run=str(uuid4())
    def concurrent_head(playlist,identity):
        check(identity is None and playlist=='UULF'+CHANNEL[2:],'burst_uses_frozen_long_head')
        for i in burst[:4]:
            seed_video(i,-60,metadata=True)
        regular=video.repository.claim_due_batch(limit=50,worker_id='ordinary burst fixture',run_id=run,
            operation='metadata',scopes=scopes,video_types=('long','short'))['rows']
        check({r.video_id for r in regular}==set(burst[:4]),'ordinary_collector_owns_four_rows_in_same_pass')
        result=video.process_common_claims(regular)
        check(result['updated']==4 and not result['settlement_unknown'],'four_fresher_profiles_committed')
        return dict(page(*burst),pageInfo={'totalResults':12})
    service=DiscoveryService(discovery,video,budget,settings=settings,fetch_membership=concurrent_head,
        worker_id='notification burst fixture',run_id=run)
    result=service.run_once()
    check(result['settled']==12 and result['deferred']==0,'entire_burst_closed_despite_interleaved_writer')
    check(scalar("""SELECT count(*) FROM public.websub_events WHERE event_id=ANY(%s)
          AND status='done' AND claim_token IS NULL""",(events,))==12,'no_fifteen_minute_burst_leases')
    check(scalar('SELECT count(*) FROM public.videos WHERE video_id=ANY(%s) AND views=99',(burst[:4],))==4,
          'older_prefetch_never_overwrites_newer_profile')
    check(len(calls)==2,'burst_and_ordinary_profiles_fetched_once_each')
    # Drive one known premiere with real event, queue and writer transactions.
    # Only the provider is synthetic. Recreate the coordinator for every pass.
    target = burst[4]
    admin.execute("UPDATE public.video_collection_state SET next_due_at=now()+interval '2 days' WHERE next_due_at IS NOT NULL")
    def dispatch():
        coordinator = DiscoveryService(DiscoveryRepository(database), video, budget, settings=settings,
            fetch_membership=forbidden, worker_id='publication signal fixture', run_id=str(uuid4()))
        return coordinator.run_once()

    def due():
        return scalar('SELECT next_due_at FROM public.video_collection_state WHERE video_id=%s', (target,))

    dates = [now + timedelta(days=5), now + timedelta(hours=5), now + timedelta(days=8), None]
    observations = [(target, 'upcoming', date, False) for date in dates]
    observations.append((target, 'completed', None, False))
    other = burst[5]
    observations.extend([(other, 'upcoming', None, False), (other, None, None, False),
                         (other, None, None, True), (other, 'none', None, False)])
    baseline = {identity: scalar('SELECT count(*) FROM public.video_snapshots WHERE video_id=%s', (identity,))
                for identity in (target, other)}
    reads = []
    def announcement(identities, phase):
        check(list(identities) == [target], 'only_signalled_video_queried')
        item = profiles(identities, phase)[0]
        if flag is not None:
            item['snippet']['liveBroadcastContent'] = flag
        if flag == 'upcoming' and planned:
            item['liveStreamingDetails'] = {'scheduledStartTime': planned.isoformat()}
        reads.append(target)
        return [item]

    video.fetch_metadata = announcement
    for index, (target, flag, planned, needs_review) in enumerate(observations):
        waiting = flag not in ('none', 'completed')
        eid = event(target)
        dispatch()
        check(due() <= scalar('SELECT clock_timestamp()'), 'newer_signal_advances_metadata')
        owned = video.repository.claim_due_batch(limit=50, worker_id='ordinary publication fixture',
            run_id=str(uuid4()), operation='metadata', scopes=scopes, video_types=('long','short'))['rows']
        check([row.video_id for row in owned] == [target], 'one_publication_claim')
        result = video.process_common_claims(owned)
        check(result['errors'] == 0 and result['publication_waiting'] == int(waiting),
              'ordinary_owner_settles_publication')
        deadline = due()
        check(scalar("""SELECT consecutive_failures=%s AND last_error_type IS NOT DISTINCT FROM %s::text
            FROM public.video_collection_state WHERE video_id=%s""",
            (int(needs_review), 'publication_evidence_needs_review' if needs_review else None, target)),
            'second_distinct_ambiguous_profile_sets_review_until_recovery')
        if waiting:
            observed = scalar('SELECT metadata_source_checked_at FROM public.video_collection_state WHERE video_id=%s',
                              (target,))
            expected = min(observed + timedelta(days=1), planned) if planned else observed + timedelta(days=1)
            check(deadline == expected and scalar('SELECT scheduled_start_at FROM public.videos WHERE video_id=%s',
                  (target,)) == planned, 'replacement_start_and_one_deadline')
        admin.execute('UPDATE public.websub_events SET next_try_at=now() WHERE event_id=%s', (eid,))
        check(dispatch()['settled'] == 1 and scalar("SELECT status='done' AND outcome='current_metadata' "
              "FROM public.websub_events WHERE event_id=%s", (eid,)), 'persisted_observation_settles_signal')
        replay = admin.execute("""INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind)
            SELECT dedupe_key,channel_id,video_id,kind FROM public.websub_events WHERE event_id=%s
            ON CONFLICT(dedupe_key) DO NOTHING RETURNING event_id""", (eid,)).fetchone()
        check(replay is None and dispatch()['claimed'] == 0, 'duplicate_delivery_stays_settled')
        if waiting:
            retry = video.repository.claim_due_batch(limit=50, worker_id='publication restart fixture',
                run_id=str(uuid4()), operation='metadata', scopes=scopes, video_types=('long','short'))
            check(not retry['rows'], 'no_second_ordinary_read_before_saved_deadline')
        check(due() == deadline and len(reads) == index + 1, 'restart_preserves_ordinary_deadline')
        check(scalar('SELECT count(*) FROM public.video_snapshots WHERE video_id=%s', (target,)) ==
              baseline[target] + int(not waiting), 'only_completed_publication_writes_counters')

    for pause, error in [('manual_hold', None), (None, 'provider_unavailable')]:
        admin.execute('UPDATE public.video_collection_state SET paused_reason=%s,last_error_type=%s WHERE video_id=%s',
                      (pause, error, target))
        eid = event(target)
        previous_due = due()
        before = scalar('SELECT clock_timestamp()')
        result = dispatch()
        after = scalar('SELECT clock_timestamp()')
        review_delay = timedelta(seconds=settings.review_retry_seconds)
        check(result['settled'] == 0 and scalar("""SELECT status='pending' AND review_required
              AND wait_reason='needs_review' AND claim_token IS NULL AND next_try_at BETWEEN %s AND %s
              FROM public.websub_events WHERE event_id=%s""", (before + review_delay, after + review_delay, eid)),
              'held_or_failed_video_uses_review_interval')
        check(due() == previous_due and scalar("""SELECT paused_reason IS NOT DISTINCT FROM %s::text
              AND last_error_type IS NOT DISTINCT FROM %s::text FROM public.video_collection_state WHERE video_id=%s""",
              (pause, error, target)), 'review_preserves_queue_owner_and_retry')
        check(dispatch()['claimed'] == 0, 'restart_respects_review_deadline')
        admin.execute('UPDATE public.video_collection_state SET paused_reason=NULL,last_error_type=NULL WHERE video_id=%s',
                      (target,))
        admin.execute('UPDATE public.websub_events SET next_try_at=now() WHERE event_id=%s', (eid,))
        dispatch()
        check(scalar("SELECT wait_reason='owner_busy' FROM public.websub_events WHERE event_id=%s", (eid,)),
              'recovered_owner_can_process_notification')
        owned = video.repository.claim_due_batch(limit=50, worker_id='recovered publication fixture',
            run_id=str(uuid4()), operation='metadata', scopes=scopes, video_types=('long','short'))['rows']
        check([row.video_id for row in owned] == [target] and video.process_common_claims(owned)['updated'] == 1,
              'recovered_video_records_new_observation')
        admin.execute('UPDATE public.websub_events SET next_try_at=now() WHERE event_id=%s', (eid,))
        check(dispatch()['settled'] == 1 and scalar("SELECT NOT review_required FROM public.websub_events "
              "WHERE event_id=%s", (eid,)), 'recovered_event_clears_review')
    # Reuse the real admission, membership and receipt owners. Interrupt only
    # the optional page credit and the fill acknowledgement after its commit.
    from unittest.mock import patch
    new_id, extra_id = 'fxnoticenew', 'fxextra0001'
    admin.execute("""UPDATE public.video_collection_state SET next_due_at=now()+interval '2 days'
                     WHERE next_due_at IS NOT NULL""")
    seed_video(extra_id, -60)
    eid = event(new_id)
    video.fetch_metadata = profiles
    ordinary_write = video.complete_observed_counters
    def commit_then_lose_ack(rows, *, observed_at):
        check([claim.video_id for claim, _ in rows] == [extra_id], 'one_optional_counter_write')
        check(scalar("SELECT status='done' FROM public.websub_events WHERE event_id=%s", (eid,)),
              'primary_receipt_committed_before_fill')
        ordinary_write(rows, observed_at=observed_at)
        raise RuntimeError('fill_acknowledgement_lost')

    coordinator = DiscoveryService(discovery, video, budget, settings=settings,
        fetch_membership=lambda playlist, identity: page(new_id), worker_id='optional writes fixture',
        run_id=str(uuid4()), claim_fill=video.repository.claim_due_fill)
    with patch.object(discovery, 'observe_head', side_effect=RuntimeError('head credit unavailable')), \
            patch.object(video, 'complete_observed_counters', side_effect=commit_then_lose_ack) as write:
        result = coordinator.run_once()
    check(result['settled']==1 and write.call_count==1, 'optional_write_errors_preserve_primary_outcome')
    check(scalar('SELECT lease_token IS NULL FROM public.video_collection_state WHERE video_id=%s', (extra_id,))
          and scalar('SELECT views FROM public.videos WHERE video_id=%s', (extra_id,))==12,
          'exact_release_preserves_committed_fill')
    check(scalar("SELECT count(*) FROM public.video_snapshots WHERE video_id=%s", (extra_id,))==1
          and coordinator.run_once()['claimed']==0, 'no_repeat_notification_or_fill_write')
    check_engagement_history(admin, video, now, scalar, check, seed_video)
    check_empty_notification(video, discovery, budget, settings, now, scalar, check, event)
    return checks


def check_engagement_history(admin, video, now, scalar, check, seed_video):
    from db.videos.video_collection import VideoCollectionClaim
    account = 'fxengage001'
    seed_video(account, -60)

    def claim():
        token = str(uuid4())
        admin.execute("""UPDATE public.video_collection_state SET lease_token=%s,lease_started_at=now(),
            lease_expires_at=now()+interval '5 minutes',lease_owner='fixture',
            lease_channel_epoch=(SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s)
            WHERE video_id=%s""", (token,CHANNEL,account))
        return VideoCollectionClaim(account,CHANNEL,'counters',token,'','')

    def counters(likes, comments, *, general=False):
        row = dict(id=account,views=177,like_count=likes,comment_count=comments)
        observed = scalar('SELECT clock_timestamp()')
        owner = claim()
        if general:
            video.complete_observed_counters([(owner,row)], observed_at=observed)
        else:
            video.repository.complete_counters([dict(video_id=account, views=177, like_count=likes,
                comment_count=comments, recorded_at=observed.isoformat(), lease_token=owner.lease_token,
                collection_attempt_id=str(uuid4()))])
        return observed

    def pair(table, *, snapshot=False):
        order = ' ORDER BY recorded_at DESC,id DESC LIMIT 1' if snapshot else ''
        return scalar('SELECT jsonb_build_array(like_count,comment_count) FROM '+table+
                      ' WHERE video_id=%s'+order,(account,))

    def expect(current, sample, name):
        check(pair('public.videos') == current and pair('readable.videos') == current
              and pair('public.video_snapshots',snapshot=True) == sample,name)

    counters(300,200,general=True)
    counters(0,0)
    expect([300,200],[None,None],'suspect_batch_zero_keeps_both_values_and_nulls_the_snapshot')
    counters(301,202)
    expect([301,202],[301,202],'both_positive_counters_recover')
    counters(None,None)
    counters(0,0)
    expect([None,None],[None,None],'recent_history_keeps_an_omission_from_validating_a_false_zero')
    counters(0,0,general=True)
    expect([0,0],[0,0],'explicit_general_zero_in_urgent_fill_is_authoritative')
    counters(0,0)
    expect([0,0],[0,0],'reported_zero_validates_following_batch_zero')
    counters(400,100)
    profile_time=scalar('SELECT clock_timestamp()')
    observed=scalar('SELECT clock_timestamp()')
    owner=claim()
    video.repository.complete_metadata([dict(video_id=account,channel_id=CHANNEL,title='Fixture',
        views=200,like_count=0,comment_count=None,youtube_topic_categories=[],recorded_at=observed.isoformat(),
        metadata_observed_at=profile_time.isoformat(),published_at=now.isoformat(),duration='PT1M',
        availability_status='public',privacy_status='public',upload_status='processed',
        lease_token=owner.lease_token,collection_attempt_id=str(uuid4()))])
    expect([0,None],[0,None],'general_metadata_zero_after_positive_and_omitted_comment')
    sid=scalar('SELECT id FROM public.video_snapshots WHERE video_id=%s AND engagement_observed_at=%s',
               (account,profile_time))
    admin.execute("UPDATE public.video_snapshots SET engagement_observed_at=now()-interval '30 days' WHERE id=%s",(sid,))
    check(scalar('SELECT views=200 AND like_count IS NULL FROM readable.video_snapshots WHERE id=%s',(sid,)),
          'earlier_optional_counts_expire_before_the_fallback_views')
    from db.hot.hot_retention import HotRetentionRepository
    HotRetentionRepository(video.repository.database).cleanup_page('snapshot_engagement',limit=100)
    check(scalar('SELECT views=200 AND like_count IS NULL AND engagement_observed_at IS NULL FROM public.video_snapshots WHERE id=%s',(sid,)),
          'cleanup_preserves_newer_views_and_attempt_receipt')
    counters(0,0)
    expect([0,None],[0,None],'metadata_visibility_guides_next_scheduled_batch')
    counters(None,None,general=True)
    counters(0,0)
    expect([None,None],[None,None],'hidden_engagement_never_becomes_zero')
    counters(None,0,general=True)
    counters(0,0)
    expect([None,0],[None,0],'each_counter_has_separate_visibility')
    owner=claim()
    as_of=scalar('SELECT clock_timestamp()')
    count_before=scalar('SELECT count(*) FROM public.video_snapshots WHERE video_id=%s',(account,))
    admin.execute("UPDATE public.videos SET checked_at=now()-interval '30 days' WHERE video_id=%s",(account,))
    video.repository.complete_identity_refresh([dict(video_id=account,channel_id=CHANNEL,video_type='long',
        title='Fixture',published_at=now.isoformat(),duration='PT1M',missing=False,counterless_profile=True,
        engagement_report_mask=3,profile_likes=0,profile_comments=43,availability_status='public',
        privacy_status='public',upload_status='processed',recorded_at=as_of.isoformat(),
        lease_token=owner.lease_token,collection_attempt_id=str(uuid4()))])
    check(scalar('SELECT count(*) FROM public.video_snapshots WHERE video_id=%s',(account,))==count_before,
          'profile_without_views_cannot_create_counter_snapshot')
    HotRetentionRepository(video.repository.database).cleanup_page('video_counters',limit=100)
    check(pair('public.videos')==[0,43] and scalar('SELECT views IS NULL FROM public.videos WHERE video_id=%s',(account,)),
          'separately_observed_engagement_survives_expired_view_cleanup')
    counters(0,0)
    expect([0,43],[0,None],'partial_general_zero_and_positive_guide_next_batch')
    account='fxengage002'
    seed_video(account,-60)
    admin.execute('UPDATE public.videos SET like_count=NULL,comment_count=NULL WHERE video_id=%s',(account,))
    counters(0,0)
    expect([0,0],[0,0],'first_batch_zero_without_contrary_evidence_is_preserved')

    # A views refresh cannot extend the life of a retained older optional value.
    counters(111,222,general=True)
    counters(0,0)
    admin.execute("UPDATE public.videos SET like_count_source_at=now()-interval '30 days' WHERE video_id=%s",(account,))
    check(pair('readable.videos')==[None,222],'held_values_expire_independently_of_views')
    HotRetentionRepository(video.repository.database).cleanup_page('video_like_observation',limit=100)
    check(pair('public.videos')==[None,222],'expired_held_value_physically_cleared')


def check_empty_notification(video, discovery, budget, settings, now, scalar, check, event):
    invisible='fxnoopen001'
    def hidden_metadata(identities, phase):
        return [dict(id=i,snippet=dict(channelId=CHANNEL,title='Fixture',publishedAt=now.isoformat(),
            liveBroadcastContent='none'),contentDetails=dict(duration='PT6M'),
            status=dict(privacyStatus='public',uploadStatus='processed'), statistics={}) for i in identities]
    video.fetch_metadata=hidden_metadata
    calls=[]
    def absent(playlist,identity):
        calls.append((playlist,identity))
        return {'items':[]}
    watcher = DiscoveryService(discovery,video,budget,settings=settings,fetch_membership=absent,
                               worker_id='empty notification fixture',run_id=str(uuid4()))
    for i in range(2):
        eid=event(invisible)
        result=watcher.run_once()
        check(result['settled']==1 and scalar("""SELECT status='skipped' AND outcome='no_public_stats_or_membership'
            AND NOT review_required AND claim_token IS NULL FROM public.websub_events WHERE event_id=%s""",(eid,)),
            'new_envelope_settles_independently_'+str(i))
        check(watcher.run_once()['claimed']==0,'settled_envelope_does_not_return_'+str(i))
    check(len(calls)==8,'each_new_envelope_checks_both_enabled_formats')
