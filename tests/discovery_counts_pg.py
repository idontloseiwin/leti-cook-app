"""Native count/source regressions; called only by the disposable PG17 suite.

Administrative writes create fixture state and inspect evidence, including one
explicit independent full-generation seed. Count tokens, source receipts,
served tokens and count-derived full requests use their production owners.
No provider call, sleep, synthetic success receipt or manual served-token write.
"""
from datetime import timedelta
import os
from uuid import uuid4

from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository
from db.discovery_counts import settle_count_heads
from db.inventory.typed_inventory import TypedInventoryRepository
from youtube_source_fields import parse_dt


CHANNEL = 'UC' + 'fxcountsource'.ljust(22, '0')
VIDEO = 'fxcount0001'


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

    def now():
        return scalar('SELECT clock_timestamp()')

    channel = ChannelSyncRepository(database)
    channel.insert_channel(dict(
        channel_id=CHANNEL, title='Discovery count/source synthetic',
        user_input='https://www.youtube.com/channel/' + CHANNEL,
        initial_long_inventory_status='completed', initial_28d_stats_status='completed',
        video_count=10, channel_metadata_status='ok',
        channel_metadata_checked_at=(now() - timedelta(minutes=1)).isoformat(),
    ), permission='accepted')
    epoch = scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s', (CHANNEL,))
    scope = dict(channel_id=CHANNEL, control_epoch=epoch)
    typed = TypedInventoryRepository(database)
    # This production preparation deliberately creates/enables all three slots.
    # Format selection later in the reader, not runtime_enabled, disables Streams.
    typed.prepare_runtime(limit=1000, enable=True)
    check(scalar('SELECT count(*) FROM public.channel_playlist_state WHERE channel_id=%s AND runtime_enabled',
                 (CHANNEL,)) == 3, 'preparation_has_three_runtime_slots')
    admin.execute("""UPDATE public.channel_playlist_state
        SET next_due_at=now()+interval '7 days',source_next_after=now()+interval '7 days'
        WHERE channel_id=%s""", (CHANNEL,))
    # Synthetic fixture bypass only: real preparation/cutover is tested separately.
    admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,now()) WHERE singleton')

    def count(value):
        observed = now().isoformat()
        result = channel.update_metadata_batch([dict(scope, values=dict(
            video_count=value, channel_metadata_status='ok',
            channel_metadata_checked_at=observed, channel_video_count_changed_at=observed,
        ))])
        check(result == {CHANNEL: 'updated'}, 'channel_count_writer_accepted')
        return scalar('SELECT count_reconcile_token FROM public.channels WHERE channel_id=%s', (CHANNEL,))

    def due(kind):
        return typed.claim_channels([CHANNEL], video_types=[kind], only_due=True,
            worker_id='Discovery count/source regression', run_id=str(uuid4()),
            expected_channel_scopes=[scope])

    def begin(claim):
        scan_id = claim.active_scan_id or str(uuid4())
        boundary = parse_dt(claim.scan_boundary_at) if claim.scan_boundary_at else now()
        if boundary is None:
            raise AssertionError('native_scan_boundary_is_parseable')
        typed.begin_scan(claim, scan_id=scan_id, scan_boundary_at=boundary)
        return scan_id

    def fail(claim, scan_id, *, outcome='playlist_not_found', code='typed_playlist_not_found'):
        return typed.fail_scan(claim, scan_id=scan_id, transition_id=str(uuid4()),
            expected_page_token=claim.resume_page_token, outcome=outcome,
            error_code=code, source_checked_at=now())

    published = now().isoformat()
    token = count(12)
    check(token is not None, 'accepted_count_creates_generation')
    long_claims = due('long')
    check(len(long_claims) == 1, 'count_wakes_future_long_source')
    long_claim = long_claims[0]
    long_scan = begin(long_claim)
    page = typed.settle_page(long_claim, scan_id=long_scan, transition_id=str(uuid4()),
        expected_page_token=None, next_page_token=None, page_status='complete',
        items=[dict(video_id=VIDEO,published_at=published)], source_checked_at=now())
    check(page['new_video_ids'] == [VIDEO], 'real_page_receipt_records_new_canonical_id')
    check(not settle_count_heads(database, CHANNEL, video_types=('long', 'short'))['settled'],
          'unserved_enabled_short_still_blocks_settlement')
    short_claims = due('short')
    check(len(short_claims) == 1, 'count_wakes_future_short_source')
    short_claim = short_claims[0]
    fail(short_claim, begin(short_claim))

    # The unserved Stream slot must neither block settlement nor receive demand.
    decision = settle_count_heads(database, CHANNEL, video_types=('long', 'short'))
    check(decision['settled'] and decision['new_count'] == 1 and decision['full_requested'],
          'two_enabled_heads_settle_with_real_receipt_N_despite_unserved_stream')
    check(scalar("""SELECT count_reconcile_served_token IS NULL
        AND full_reconcile_required_token IS NULL FROM public.channel_playlist_state
        WHERE channel_id=%s AND video_type='stream'""", (CHANNEL,)),
          'disabled_stream_receives_no_count_or_full_ack')

    # Debt derived from the same count is not new source evidence: the 404
    # remains authoritative until its retry deadline or a genuine new signal.
    check(due('short') == [], 'count_derived_full_debt_preserves_same_generation_404_backoff')
    check(scalar("""SELECT full_reconcile_required_token IS NOT DISTINCT FROM full_reconcile_served_token
        FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'""",(CHANNEL,)),
          'confirmed_missing_source_has_no_count_derived_full_debt')
    # An independent fresh full token must still bypass that old source delay.
    # Keep its due clock unchanged so only the new generation permits the claim.
    independent_full=str(uuid4())
    admin.execute("UPDATE public.channel_playlist_state SET full_reconcile_required_token=%s "
        "WHERE channel_id=%s AND video_type='short'",(independent_full,CHANNEL))
    check(due('short') == [], 'absent_source_waits_for_scheduled_retry')
    admin.execute("UPDATE public.channel_playlist_state SET head_next_due_at=now(),next_due_at=now() WHERE channel_id=%s AND video_type='short'",(CHANNEL,))
    full_claims = due('short')
    check(len(full_claims) == 1 and full_claims[0].scan_kind == 'full_reconciliation',
          'scheduled_recheck_serves_independent_full_generation')
    full_claim = full_claims[0]
    full_scan = begin(full_claim)
    fail(full_claim, full_scan)
    check(scalar("""SELECT full_reconcile_required_token IS NOT NULL
        AND full_reconcile_required_token IS NOT DISTINCT FROM full_reconcile_served_token
        AND next_due_at>clock_timestamp() FROM public.channel_playlist_state
        WHERE channel_id=%s AND video_type='short'""", (CHANNEL,)),
          'confirmed_first_page_404_serves_full_intent_and_keeps_retry')
    check(due('short') == [], 'same_full_generation_obeys_404_backoff')

    # A fresh observation can wake the missing source, but its error cannot loop.
    newer = count(13)
    check(newer != token, 'fresh_count_rotates_generation')
    check(due('short') == [], 'fresh_count_does_not_wake_absent_source')
    admin.execute("UPDATE public.channel_playlist_state SET head_next_due_at=now(),next_due_at=now(),full_reconcile_required_token=gen_random_uuid() WHERE channel_id=%s AND video_type='short'",(CHANNEL,))
    new_claims = due('short')
    check(len(new_claims) == 1, 'scheduled_recheck_at_actual_deadline')
    new_claim = new_claims[0]
    fail(new_claim, begin(new_claim), outcome='error', code='youtube_transport_error')
    check(due('short') == [], 'same_generation_transport_error_obeys_backoff')
    check(scalar("""SELECT full_reconcile_required_token IS DISTINCT FROM full_reconcile_served_token
        FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'""", (CHANNEL,)),
          'transport_error_does_not_erase_full_requirement')

    # Long full scans retain their cursor after transient failure. A newer count
    # may wake that continuation once, but cannot turn the old frozen scan token
    # into an unlimited bypass of the retry delay after subsequent failures.
    long_full_claims = due('long')
    check(len(long_full_claims) == 1 and long_full_claims[0].scan_kind == 'full_reconciliation',
          'pending_long_full_is_claimable')
    long_full = long_full_claims[0]
    retained_scan = begin(long_full)
    failed = fail(long_full, retained_scan, outcome='error', code='youtube_transport_error')
    check(failed['scan_retained'], 'long_full_error_retains_native_scan')
    check(due('long') == [], 'retained_long_same_generation_obeys_backoff')
    token_after_failure = count(14)
    check(token_after_failure != newer, 'new_signal_after_retained_failure_has_distinct_token')
    resumed = due('long')
    check(len(resumed) == 1 and resumed[0].active_scan_id == retained_scan,
          'new_count_can_wake_existing_long_continuation')
    continued_scan = begin(resumed[0])
    fail(resumed[0], continued_scan, outcome='error', code='youtube_transport_error')
    check(due('long') == [], 'retained_old_scan_cannot_repeat_fresh_demand_bypass')
    checks.extend(run_credit_checks(admin,database))
    return checks


def run_credit_checks(admin,database):
    """Actual addressed/source receipts: no invented positive receipt JSON."""
    from hashlib import sha256
    checks=[]
    def scalar(sql,args=()):
        row=admin.execute(sql,args).fetchone()
        return next(iter(row.values())) if isinstance(row,dict) else row[0]
    def now():return scalar('SELECT clock_timestamp()')
    def check(ok,name):
        if not ok:raise AssertionError(name)
        checks.append(name)
    repo=DiscoveryRepository(database)
    typed=TypedInventoryRepository(database)
    channels=ChannelSyncRepository(database)
    for ordinal,old_publication in enumerate((False,True)):
        identity='UC'+('fxcredit'+str(ordinal)).ljust(22,'0')
        video='fxcredit%03d'%ordinal
        lower=now()-timedelta(minutes=1)
        channels.insert_channel(dict(channel_id=identity,title='Count interval fixture',
            user_input='https://www.youtube.com/channel/'+identity,
            initial_long_inventory_status='completed',initial_28d_stats_status='completed',
            video_count=10,channel_metadata_status='ok',channel_metadata_checked_at=lower.isoformat()),permission='accepted')
        epoch=scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(identity,))
        scope=dict(channel_id=identity,control_epoch=epoch)
        typed.prepare_runtime(limit=1000,enable=True)
        def page(kind,ids):
            admin.execute("UPDATE public.channel_playlist_state SET next_due_at=now()-interval '1 second' WHERE channel_id=%s AND video_type=%s",(identity,kind))
            claim=typed.claim_channels([identity],video_types=[kind],only_due=True,worker_id='Credit fixture',run_id=str(uuid4()),expected_channel_scopes=[scope])[0]
            scan=str(uuid4());typed.begin_scan(claim,scan_id=scan,scan_boundary_at=now())
            return typed.settle_page(claim,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
                next_page_token=None,page_status='complete' if ids else 'empty',items=[dict(video_id=v) for v in ids],source_checked_at=now())
        page('long',[]);page('short',[])
        admin.execute("UPDATE public.websub_events SET next_try_at=now()+interval '1 day' WHERE status='pending'")
        event=scalar("INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind) VALUES(%s,%s,%s,'entry') RETURNING event_id",
            (sha256(str(uuid4()).encode()).hexdigest(),identity,video))
        claims=[r for r in repo.claim_event_batch(100) if str(r['work_id'])==str(event)]
        check(len(claims)==1,'addressed_fixture_claim_'+str(ordinal))
        request=str(uuid4());repo.start_request(claims,request,now())
        publication=(lower-timedelta(days=1) if old_publication else now()).isoformat()
        receipt=repo.record_membership(claims,request_id=request,video_type='long',playlist_id='UULF'+identity[2:],
            source_checked_at=now(),transition_id=str(uuid4()),items=[dict(video_id=video,published_at=publication)])
        check(receipt['new_video_ids']==[video] and len(receipt['count_credit_candidates'])==1,'first_insert_receipt_records_publication_'+str(ordinal))
        upper=now().isoformat()
        channels.update_metadata_batch([dict(scope,values=dict(video_count=11,channel_metadata_status='ok',channel_metadata_checked_at=upper))])
        if old_publication:
            page('long',[video]);page('short',[])
            decision=settle_count_heads(database,identity,video_types=('long','short'))
            check(decision['settled'] and decision['new_count']==0 and decision['full_requested'],
                  'old_A_cannot_explain_deep_new_B')
        else:
            check(scalar('SELECT count_reconcile_token=count_reconcile_settled_token FROM public.channels WHERE channel_id=%s',
                         (identity,)), 'new_notification_closes_count_without_playlist_read')
        check(scalar('SELECT count(*) FROM public.video_inventory WHERE video_id=%s',(video,))==1,
              'ineligible_credit_does_not_filter_discovery_'+str(ordinal))
        # The same canonical ID/publication cannot be reused by a later count.
        channels.update_metadata_batch([dict(scope,values=dict(video_count=12,channel_metadata_status='ok',channel_metadata_checked_at=now().isoformat()))])
        repeated=page('long',[video]);page('short',[])
        check(repeated['new_video_ids']==[] and repeated['count_credit_candidates']==[], 'repeat_page_does_not_mint_credit_'+str(ordinal))
        next_decision=settle_count_heads(database,identity,video_types=('long','short'))
        check(next_decision['settled'] and next_decision['new_count']==0 and next_decision['full_requested'],
              'publication_credit_not_reused_across_generations_'+str(ordinal))
    pending_channel='UC'+'fxpending'.ljust(22,'0')
    before=now()-timedelta(minutes=1)
    channels.insert_channel(dict(channel_id=pending_channel,title='Unidentified notification fixture',
        user_input='https://www.youtube.com/channel/'+pending_channel,initial_long_inventory_status='completed',
        initial_28d_stats_status='completed',video_count=6,channel_metadata_status='ok',
        channel_metadata_checked_at=before.isoformat()),permission='accepted')
    pending_event=scalar("""INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind)
        VALUES(%s,%s,'fxunknown01','entry') RETURNING event_id""",(sha256(str(uuid4()).encode()).hexdigest(),pending_channel))
    epoch=scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(pending_channel,))
    def refresh_pending():
        channels.update_metadata_batch([dict(channel_id=pending_channel,control_epoch=epoch,values=dict(video_count=7,
            channel_metadata_status='ok',channel_metadata_checked_at=now().isoformat()))])
        return scalar('SELECT count_reconcile_token FROM public.channels WHERE channel_id=%s',(pending_channel,))
    check(refresh_pending() is None,'unidentified_notification_defers_count_signal')
    admin.execute("UPDATE public.websub_events SET review_required=true WHERE event_id=%s",(pending_event,))
    check(refresh_pending() is not None,'next_observation_reopens_unexplained_count_without_a_second_increase')
    return checks
