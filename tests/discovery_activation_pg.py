"""Actual disabled preparation and cutover, isolated synthetic PG17 sources."""
from datetime import timedelta
from dataclasses import replace
from hashlib import sha256
import json
import os
from unittest.mock import Mock
from uuid import uuid4

from collection.inventory import run_typed_inventory_claim
from collection.inventory_admission import InventoryDiscoveryAdmission
from db.base.postgres import DatabaseServerError,PostgresDatabase
from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository
from db.discovery_activation import activate, prepare, readiness, PreparationContract
from db.inventory.typed_inventory import TypedInventoryRepository
from collection_schedule import discovery_schedules
from discovery_policy import DiscoverySettings
from typed_playlist import typed_playlist_id
from youtube_errors import YouTubePlaylistNotFoundError
from youtube_playlist import fetch_playlist_items_page
from youtube_source_fields import parse_dt

CHANNEL='UC'+'fxactivation'.ljust(22,'0')
HOST=PreparationContract(True,True,True,True,True)
SETTINGS=DiscoverySettings()


def run_checks(admin,database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
        raise RuntimeError('disposable_cluster_required')
    # Use the real explicit seven-day inventory profile, with a one-day absence retry.
    database=PostgresDatabase(database.config,admission=lambda _:None,
        cadence_snapshot=discovery_schedules().cadence_snapshot()).open()
    checks=[]
    def scalar(sql,args=()):
        row=admin.execute(sql,args).fetchone()
        return next(iter(row.values())) if isinstance(row,dict) else row[0]
    def now():return scalar('SELECT clock_timestamp()')
    def check(ok,name):
        if not ok:raise AssertionError(name)
        checks.append(name)
    def rejected(code,call):
        try:call()
        except DatabaseServerError as error:check(code in str(error),code)
        else:raise AssertionError('missing_rejection_'+code)
    # Other test groups deliberately leave unrelated incomplete/leased sources.
    # Exclude those synthetic fixtures without changing their epochs or clocks.
    old=admin.execute('SELECT channel_id,is_hidden,hidden_at FROM public.channel_collection_state').fetchall()
    control=admin.execute('SELECT enabled,first_activated_at FROM public.discovery_runtime_control WHERE singleton').fetchone()
    admin.execute('UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=coalesce(hidden_at,now())')
    admin.execute('UPDATE public.discovery_runtime_control SET enabled=false WHERE singleton')
    try:
        channels=ChannelSyncRepository(database)
        channels.insert_channel(dict(channel_id=CHANNEL,title='Preparation fixture',
            user_input='https://www.youtube.com/channel/'+CHANNEL,
            initial_long_inventory_status='completed',initial_28d_stats_status='completed'),permission='accepted')
        epoch=scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(CHANNEL,))
        typed=TypedInventoryRepository(database);typed.prepare_runtime(limit=1000,enable=True)
        def claim(kind):
            rows=typed.claim_channels([CHANNEL],video_types=[kind],only_due=False,worker_id='Preparation fixture',
                run_id=str(uuid4()),expected_channel_scopes=[dict(channel_id=CHANNEL,control_epoch=epoch)])
            check(len(rows)==1,'source_claim_'+kind)
            return rows[0]
        def begin(source):
            scan=str(uuid4());typed.begin_scan(source,scan_id=scan,scan_boundary_at=now());return scan
        def finish(source,scan):
            return typed.settle_page(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
                next_page_token=None,page_status='empty',items=[],source_checked_at=now())
        historical=[]
        for kind in ('long','short'):
            source=claim(kind);scan=begin(source);finish(source,scan);historical.append(scan)
        # Faithful migrated fixture: old completed scans have no coverage epoch.
        admin.execute('UPDATE public.channel_video_inventory_scans SET discovery_channel_epoch=NULL WHERE scan_id=ANY(%s::uuid[])',(historical,))
        summary=readiness(database,settings=SETTINGS,blocker_limit=1)
        check(not summary['ready'],'legacy_null_epoch_not_falsely_ready')
        check(summary['blocker_count']==2 and summary['blockers_truncated'] and len(summary['blockers'])==1
              and summary['blockers'][0]['channel_id']==CHANNEL
              and summary['blockers'][0]['reason']=='full_epoch_mismatch',
              'bounded_readiness_names_current_blockers_and_truncation')
        rejected('discovery_settings_required',lambda:readiness(database))
        rejected('discovery_blocker_limit_invalid',lambda:readiness(database,settings=SETTINGS,blocker_limit=101))
        rejected('discovery_prepared_host_required',lambda:activate(database,settings=SETTINGS))
        rejected('discovery_prepared_host_required',lambda:activate(database,settings=SETTINGS,
            prepared_host=replace(HOST,workers_drained=False)))
        rejected('discovery_formats_invalid',lambda:activate(database,prepared_host=HOST,settings=SETTINGS,video_types=('long',)))
        rejected('discovery_preparation_incomplete',lambda:activate(database,prepared_host=HOST,settings=SETTINGS))
        original_start=scalar('SELECT started_at FROM public.channel_video_inventory_scans WHERE scan_id=%s',(historical[0],))
        # Readiness is facts, not waiting a fixed seven days or re-stamping scans.
        requested=prepare(database,prepared_host=HOST,limit=2,settings=SETTINGS)
        check(requested=={'requested':2,'enabled':False},'disabled_prepare_requests_real_full_only')
        check(prepare(database,prepared_host=HOST,settings=SETTINGS)['requested']==0,'preparation_idempotent_outstanding_full')
        long=claim('long');check(long.scan_kind=='full_reconciliation','legacy_loaded_channel_never_reinitialised')
        scan=begin(long)
        check(any(b['video_type']=='long' and b['reason']=='source_active'
                  for b in readiness(database,settings=SETTINGS)['blockers']),'active_owner_is_named_blocker')
        rejected('discovery_preparation_incomplete',lambda:activate(database,prepared_host=HOST,settings=SETTINGS))
        finish(long,scan)
        short=claim('short');short_scan=begin(short)
        typed.fail_scan(short,scan_id=short_scan,transition_id=str(uuid4()),expected_page_token=None,
            outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=now())
        summary=readiness(database,settings=SETTINGS)
        check(summary['ready'] and summary['full_sources']==1 and summary['absent_sources']==1,
              'fresh_current_epoch_full_and_actual_404_admit')
        check(scalar('SELECT started_at FROM public.channel_video_inventory_scans WHERE scan_id=%s',(historical[0],))==original_start
              and scalar('SELECT discovery_channel_epoch FROM public.channel_video_inventory_scans WHERE scan_id=%s',(historical[0],)) is None,
              'no_relabeling_or_clock_refresh_of_legacy_evidence')
        missing_checked=scalar('SELECT source_checked_at FROM public.typed_inventory_receipts WHERE scan_id=%s AND transition_kind=\'failure\'',(short_scan,))
        def full_age(age):
            admin.execute('UPDATE public.channel_video_inventory_scans SET started_at=%s WHERE scan_id=%s',(now()-age,scan))
        full_age(timedelta(days=8))
        summary=readiness(database,settings=SETTINGS)
        check(summary['ready'] and summary['grace_sources']==1 and summary['blockers']==[],
              'eight_day_full_is_admitted_with_two_day_cutover_grace')
        check(prepare(database,prepared_host=HOST,settings=SETTINGS)['requested']==0,
              'within_grace_does_not_create_early_full_work')
        full_age(timedelta(days=9,seconds=-60))
        check(readiness(database,settings=SETTINGS)['ready'],'just_inside_nine_days_is_admitted')
        full_age(timedelta(days=9,seconds=1))
        summary=readiness(database,settings=SETTINGS)
        check(not summary['ready'] and summary['blockers'][0]['reason']=='full_coverage_stale',
              'beyond_nine_days_is_a_named_full_coverage_blocker')
        rejected('discovery_preparation_incomplete',lambda:activate(database,prepared_host=HOST,settings=SETTINGS))
        check(prepare(database,prepared_host=HOST,settings=SETTINGS)['requested']==1,
              'beyond_grace_prepares_real_full_work')
        outstanding=scalar("SELECT full_reconcile_required_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='long'",(CHANNEL,))
        full_age(timedelta(days=8))
        check(prepare(database,prepared_host=HOST,settings=SETTINGS)['requested']==0
              and scalar("SELECT full_reconcile_required_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='long'",(CHANNEL,))==outstanding,
              'grace_never_erases_an_existing_full_intent')
        custom=replace(SETTINGS,activation_grace_seconds=3600)
        check(not readiness(database,settings=custom)['ready'],'custom_one_hour_grace_rejects_eight_day_full')
        full_age(timedelta(days=7,minutes=30))
        check(readiness(database,settings=custom)['ready'],'custom_one_hour_grace_accepts_seven_days_plus_half_hour')
        full_age(timedelta(days=29,seconds=1))
        summary=readiness(database,settings=replace(SETTINGS,activation_grace_seconds=60*86400))
        check(not summary['ready'] and summary['blockers'][0]['reason']=='full_evidence_expired',
              'even_large_grace_never_accepts_expired_29day_evidence')
        full_age(timedelta(days=8))
        admin.execute("UPDATE public.typed_inventory_receipts SET source_checked_at=%s WHERE scan_id=%s AND transition_kind='failure'",
                      (now()-timedelta(days=8),short_scan))
        summary=readiness(database,settings=SETTINGS)
        check(not summary['ready'] and summary['blockers'][0]['video_type']=='short'
              and summary['blockers'][0]['reason']=='missing_evidence_stale',
              'full_grace_does_not_extend_not_found_freshness')
        missing_due=now()+timedelta(hours=2)
        admin.execute("UPDATE public.channel_playlist_state SET full_reconcile_served_token=full_reconcile_required_token,last_error_code=NULL,next_due_at=%s WHERE channel_id=%s AND video_type='short'",(missing_due,CHANNEL))
        prepare(database,prepared_host=HOST,settings=SETTINGS)
        check(scalar("SELECT next_due_at FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'",(CHANNEL,))==missing_due,
              'preparation_preserves_explicit_404_backoff_even_without_error_code')
        admin.execute("UPDATE public.typed_inventory_receipts SET source_checked_at=%s WHERE scan_id=%s AND transition_kind='failure'",(missing_checked,short_scan))
        first=activate(database,prepared_host=HOST,settings=SETTINGS)
        check(first is not None and activate(database,prepared_host=HOST,settings=SETTINGS)==first,'actual_gate_activation_idempotent')
        check(scalar("SELECT full_reconcile_required_token FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='long'",(CHANNEL,))==outstanding,
              'activation_preserves_preexisting_full_intent')
        check(database.cadence_snapshot.inventory_by_type.long.reconciliation_seconds==7*86400,
              'activation_grace_never_changes_seven_day_inventory_period')
        admin.execute('UPDATE public.discovery_runtime_control SET enabled=false WHERE singleton')
        epoch=scalar("UPDATE public.channel_collection_state SET control_epoch=nextval('public.channel_collection_epoch_seq') WHERE channel_id=%s RETURNING control_epoch",(CHANNEL,))
        check(not readiness(database,settings=SETTINGS)['ready'],'new_epoch_withdraws_prior_full_and_404_proof')
        # Failures retain their retry schedule even when preparation introduces a
        # real full token for a now-stale source. No tight error/404 retry loop.
        future=now()+timedelta(hours=2)
        admin.execute("UPDATE public.channel_playlist_state SET full_reconcile_served_token=full_reconcile_required_token,last_error_code='youtube_transport_error',next_due_at=%s WHERE channel_id=%s AND video_type='long'",(future,CHANNEL))
        prepare(database,prepared_host=HOST,settings=SETTINGS)
        check(scalar("SELECT next_due_at FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='long'",(CHANNEL,))==future,
              'preparation_preserves_transport_error_backoff')
        summary=readiness(database,settings=SETTINGS)
        check(any(b['video_type']=='long' and b['last_error_code']=='youtube_transport_error'
                  and b['next_due_at'] is not None for b in summary['blockers']),
              'blocked_error_source_exposes_reason_identity_and_retry_clock')
        rejected('discovery_preparation_incomplete',lambda:activate(database,prepared_host=HOST,settings=SETTINGS))
        def long_source_reason():
            return next((b['reason'] for b in readiness(database,settings=SETTINGS)['blockers']
                         if b['video_type']=='long'), None)

        def next_full():
            # Advance the fixture's retry clocks, never change a receipt or fake a successful scan.
            admin.execute("""UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),
                next_due_at=now(),source_next_after=now(),head_next_due_at=now()
                WHERE channel_id=%s AND video_type='long'""",(CHANNEL,))
            result=claim('long')
            check(result.scan_kind=='full_reconciliation','full_generation_requested')
            return result

        for label,scan_epoch in (('unbound',None),('previous',epoch-1),('same',epoch)):
            source=next_full(); scan=begin(source)
            admin.execute('UPDATE public.channel_video_inventory_scans SET discovery_channel_epoch=%s WHERE scan_id=%s',
                          (scan_epoch,scan))
            timeout=typed.fail_scan(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
                outcome='error',error_code='youtube_transport_error',source_checked_at=now())
            check(timeout['scan_retained'] and long_source_reason()=='source_active',
                  label+'_transient_first_page_stays_resumable')
            source=next_full()
            check(source.active_scan_id==scan, label+'_old_scan_reclaimed')
            resumed=typed.begin_scan(source,scan_id=scan,scan_boundary_at=parse_dt(source.scan_boundary_at))
            check(resumed['status']=='running' and resumed['pages_completed']==0 and resumed['replayed'],
                  label+'_collector_can_resume_before_first_page')
            absence=dict(scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
                         outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=now())
            settled=typed.fail_scan(source,**absence)
            check(settled.get('first_page_absence_epoch')==epoch and not settled['scan_retained']
                  and long_source_reason() is None, label+'_current_response_closes_zero_page_full')
            row=admin.execute("""SELECT s.discovery_channel_epoch,s.status,s.coverage,p.active_scan_id,
                p.full_reconcile_required_token IS DISTINCT FROM p.full_reconcile_served_token,
                p.next_due_at>now() FROM public.channel_video_inventory_scans s
                JOIN public.channel_playlist_state p USING(channel_id,video_type) WHERE s.scan_id=%s""",(scan,)).fetchone()
            check(tuple(row)==(scan_epoch,'failed','partial',None,True,True),
                  label+'_retains_frozen_epoch_full_debt_and_retry')
            check(typed.fail_scan(source,**absence)['replayed'], label+'_failure_replay_is_idempotent')

        epoch=scalar("UPDATE public.channel_collection_state SET control_epoch=nextval('public.channel_collection_epoch_seq') WHERE channel_id=%s RETURNING control_epoch",(CHANNEL,))
        check(long_source_reason() is not None,'new_owner_epoch_invalidates_fresh_first_page_absence')
        source=next_full(); scan=begin(source)
        typed.settle_page(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
            next_page_token='opaque-page-2',page_status='continuation',
            items=[dict(video_id='long404test',published_at=now().isoformat())],source_checked_at=now())
        source=claim('long')
        later=typed.fail_scan(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token='opaque-page-2',
            outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=now())
        check(later['scan_retained'] and later['resume_page_token']=='opaque-page-2'
              and 'first_page_absence_epoch' not in later and long_source_reason()=='source_active',
              'later_page_404_preserves_accepted_page_and_cursor')

        def short_reason():
            return next((b['reason'] for b in readiness(database,settings=SETTINGS)['blockers']
                         if b['video_type']=='short'), None)

        def new_short():
            admin.execute("""UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),
                next_due_at=now(),source_next_after=now(),head_next_due_at=now()
                WHERE channel_id=%s AND video_type='short'""",(CHANNEL,))
            source=claim('short')
            return source,begin(source)

        source,scan=new_short()
        typed.settle_page(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
            next_page_token='short-page-2',page_status='continuation',
            items=[dict(video_id='shrt404test',published_at=now().isoformat())],source_checked_at=now())
        source=claim('short')
        later=typed.fail_scan(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token='short-page-2',
            outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=now())
        check(not later['scan_retained'] and short_reason() is not None,
              'unretained_short_404_after_first_page_never_proves_absence')
        source,scan=new_short()
        typed.fail_scan(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
            outcome='playlist_not_found',error_code='bad_source_evidence',source_checked_at=now())
        check(short_reason() is not None,'generic_first_page_failure_never_proves_absence')
        source,scan=new_short()
        typed.fail_scan(source,scan_id=scan,transition_id=str(uuid4()),expected_page_token=None,
            outcome='playlist_not_found',error_code='typed_playlist_not_found',source_checked_at=now())
        check(short_reason() is None,'canonical_first_page_short_absence_remains_usable')
        check_head_proof(admin,typed,claim,now,scalar,check,short_reason)
    finally:
        for row in old:
            if isinstance(row,dict):identity,hidden,hidden_at=row['channel_id'],row['is_hidden'],row['hidden_at']
            else:identity,hidden,hidden_at=row
            admin.execute('UPDATE public.channel_collection_state SET is_hidden=%s,hidden_at=%s WHERE channel_id=%s',(hidden,hidden_at,identity))
        admin.execute('UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=now() WHERE channel_id=%s',(CHANNEL,))
        enabled,first=(control['enabled'],control['first_activated_at']) if isinstance(control,dict) else control
        admin.execute('UPDATE public.discovery_runtime_control SET enabled=%s,first_activated_at=%s WHERE singleton',(enabled,first))
        database.close()
    return checks


def check_head_proof(admin,typed,claim,now,scalar,check,short_reason):
    # Each read uses the ordinary worker: the full window is exhausted, the head is
    # due, and exactly one first page is fetched against a synthetic HTTP transport.
    playlist=typed_playlist_id(CHANNEL,'short')
    def addressed_member(video):
        # Real addressed writer during a previous enabled session. Readiness after
        # draining and switching off must see that the list was subsequently present.
        admin.execute('UPDATE public.discovery_runtime_control SET enabled=true WHERE singleton')
        try:
            repo=DiscoveryRepository(typed.database)
            event=scalar("""INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind)
                VALUES(%s,%s,%s,'entry') RETURNING event_id""",(sha256(video.encode()).hexdigest(),CHANNEL,video))
            claims=[r for r in repo.claim_event_batch(100) if int(r['work_id'])==event]
            check(len(claims)==1,'addressed_source_claimed')
            request=str(uuid4())
            repo.start_request(claims,request,now())
            result=repo.record_membership(claims,request_id=request,video_type='short',playlist_id=playlist,
                source_checked_at=now(),transition_id=str(uuid4()),items=[{'video_id':video}])
            check(result['video_ids']==[video],'addressed_positive_receipt_committed')
        finally:
            admin.execute('UPDATE public.discovery_runtime_control SET enabled=false WHERE singleton')

    def read_head(status=404,*,legacy=False):
        admin.execute("""UPDATE public.channel_playlist_state SET full_reconcile_required_token=gen_random_uuid(),
            head_next_due_at=now(),next_due_at=now() WHERE channel_id=%s AND video_type='short'""",(CHANNEL,))
        owner=claim('short')
        budget=Mock()
        budget.reserve.return_value={'allowed':False,'reason':'window_budget'}
        response=Mock(status_code=status)
        response.json.return_value=({'error':{'errors':[{'reason':'playlistNotFound'}]}} if status!=200
                                    else {'items':[]})
        transport=Mock(return_value=response)
        def fetch(pid,**kwargs):
            if legacy:raise YouTubePlaylistNotFoundError('unverified historical classification')
            return fetch_playlist_items_page(pid,**kwargs,youtube_api_get=transport,api_key=None,attempt_recorder=None,
                timeout_seconds=1,emit_event=Mock(),perf_counter=lambda:0)
        result=run_typed_inventory_claim(repository=typed,claim=owner,fetch_page=fetch,
            playlist_not_found_errors=(YouTubePlaylistNotFoundError,),discovery_admission=InventoryDiscoveryAdmission(budget),now=now)
        budget.reserve.assert_called_once()
        if not legacy:
            transport.assert_called_once()
            params=transport.call_args.kwargs['params']
            check(params['playlistId']==playlist and 'pageToken' not in params,'bounded_typed_first_page_read')
        return result['receipt']

    addressed_member('headpos0001')
    check(short_reason() is not None,'later_confirmed_member_withdraws_scan_absence')
    legacy=read_head(legacy=True)
    check(legacy['outcome']=='playlist_not_found' and 'provider_http_status' not in legacy
          and short_reason() is not None,'old_head_404_neither_certified_nor_rescued_by_older_scan')
    fresh=read_head()
    check(fresh['provider_http_status']==404 and short_reason() is None,'strict_head_404_admits_current_absence')
    addressed_member('headpos0002')
    check(short_reason() is not None,'later_confirmed_member_withdraws_head_absence')
    fresh=read_head()
    check(short_reason() is None,'prior_addressed_positive_does_not_outlive_newer_head_404')
    check(scalar("""SELECT active_scan_id IS NULL AND full_reconcile_required_token IS NOT DISTINCT FROM full_reconcile_served_token
        AND head_next_due_at>now() FROM public.channel_playlist_state WHERE channel_id=%s AND video_type='short'""",(CHANNEL,)),
        'head_404_retires_full_debt_and_preserves_scheduled_retry')
    key=fresh['transition_id']
    target=" WHERE receipt_scope='addressed_membership' AND transition_id=%s"
    original=scalar('SELECT result::text FROM public.typed_inventory_receipts'+target,(key,))
    for label,changes in [('provider_status',{'provider_http_status':403}),('different_epoch',{'control_epoch':-1}),
                          ('wrong_channel',{'channel_id':'UC-other'}),('wrong_format',{'video_type':'long'}),
                          ('wrong_playlist',{'playlist_id':'UUSH-other'}),('expired_proof',{'source_expired':True}),
                          ('wrong_lease',{'lease_token':str(uuid4())}),('wrong_receipt',{'transition_id':str(uuid4())}),
                          ('ordinary_lookup',{'observation_kind':'membership'})]:
        admin.execute('UPDATE public.typed_inventory_receipts SET result=%s::jsonb || %s::jsonb'+target,
                      (original,json.dumps(changes),key))
        check(short_reason() is not None,'head_404_rejects_'+label)
    admin.execute('UPDATE public.typed_inventory_receipts SET result=%s::jsonb'+target,(original,key))
    source=" WHERE channel_id=%s AND video_type='short'"
    admin.execute("UPDATE public.channel_playlist_state SET source_checked_at=now()-interval '2 days'"+source,(CHANNEL,))
    stamp=scalar('SELECT source_checked_at FROM public.typed_inventory_receipts'+target,(key,))
    set_head_clock="UPDATE public.channel_playlist_state SET head_last_attempt=jsonb_set(head_last_attempt,'{source_checked_at}',to_jsonb(%s::text))"+source
    admin.execute("UPDATE public.channel_playlist_state SET head_pending_request='{}'::jsonb"+source,(CHANNEL,))
    check(short_reason() is not None,'interrupted_pending_head_does_not_reuse_previous_404')
    admin.execute("UPDATE public.channel_playlist_state SET head_pending_request=NULL"+source,(CHANNEL,))
    for label,clock in [('daily_expiry',now()-timedelta(days=1)),('future_receipt',now()+timedelta(hours=1))]:
        admin.execute(set_head_clock,(clock.isoformat(),CHANNEL))
        admin.execute('UPDATE public.typed_inventory_receipts SET source_checked_at=%s'+target,(clock,key))
        check(short_reason() is not None,'head_404_rejects_'+label)
    admin.execute('UPDATE public.typed_inventory_receipts SET source_checked_at=%s'+target,(stamp,key))
    admin.execute(set_head_clock,('malformed',CHANNEL))
    check(short_reason() is not None,'invalid_head_clock_fails_closed')
    admin.execute(set_head_clock,(stamp.isoformat(),CHANNEL))
    check(short_reason() is None,'current_receipt_with_matching_clocks_is_admitted')
    admin.execute('UPDATE public.channel_playlist_state SET source_checked_at=now()'+source,(CHANNEL,))
    check(short_reason() is not None,'newer_scan_supersedes_head_absence')
    read_head()
    check(short_reason() is None,'new_head_can_reestablish_absence')
    read_head(200)
    check(short_reason() is not None,'newer_empty_positive_head_supersedes_head_and_scan_absence')
    read_head()
    read_head(500)
    check(short_reason() is not None,'latest_failed_head_does_not_reuse_prior_404')
    read_head()
    admin.execute("UPDATE public.channel_collection_state SET control_epoch=nextval('public.channel_collection_epoch_seq') WHERE channel_id=%s",(CHANNEL,))
    check(short_reason() is not None,'new_owner_epoch_withdraws_verified_head_absence')
