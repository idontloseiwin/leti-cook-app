"""Paused repair debt, unchanged allowance and bounded hot queues; PG17 only."""
from datetime import timedelta
from hashlib import sha256
import json
import os
from uuid import uuid4

from db.channel.channel_sync import ChannelSyncRepository
from db.discovery import DiscoveryRepository
from db.discovery_budget import DiscoveryBudgetRepository
from db.inventory.typed_inventory import TypedInventoryRepository
from db.retention.discovery_operational_retention import clean
from discovery_policy import DiscoverySettings


CHANNEL='UC'+'campaigncoverage'.ljust(22,'0')


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

    def now():return scalar('SELECT clock_timestamp()')

    def campaign(*,general=2):
        identity=str(uuid4());end=now()-timedelta(days=100)
        admin.execute('''INSERT INTO public.discovery_repair_campaigns(campaign_id,definition_digest,
            definition_version,window_start,window_end,target_limit,general_limit,statistics_limit,created_at)
            VALUES(%s,%s,1,%s,%s,300,%s,1,%s)''',
            (identity,sha256(identity.encode()).hexdigest(),end-timedelta(days=7),end,general,end))
        return identity

    def target(campaign_id,identity,*,paused=False):
        target_id=scalar('''INSERT INTO public.discovery_repair_targets(campaign_id,channel_id,video_id,
            source_event_ids,received_at,next_try_at,paused_at,pause_reason)
            VALUES(%s,%s,%s,ARRAY[1]::bigint[],%s,%s,%s,%s) RETURNING target_id''',
            (campaign_id,CHANNEL,identity,now()-timedelta(days=31) if paused else now(),
             now()-timedelta(days=1),now() if paused else None,'long_wait' if paused else None))
        admin.execute('UPDATE public.discovery_repair_campaigns SET target_count=target_count+1 WHERE campaign_id=%s',(campaign_id,))
        return target_id

    def take(target_id):
        # Do not consume work from other helpers sharing this disposable fixture.
        admin.execute("UPDATE public.discovery_repair_targets SET next_try_at=now()+interval '2 days' WHERE status='pending' AND target_id<>%s",(target_id,))
        admin.execute('UPDATE public.discovery_repair_targets SET next_try_at=clock_timestamp() WHERE target_id=%s',(target_id,))
        return [row for row in repo.claim_event_batch(50,origin='repair') if row['work_id']==str(target_id)]

    ChannelSyncRepository(database).insert_channel(dict(channel_id=CHANNEL,title='Discovery campaign synthetic',
        user_input='https://www.youtube.com/channel/'+CHANNEL,initial_long_inventory_status='completed',
        initial_28d_stats_status='completed'),permission='accepted')
    TypedInventoryRepository(database).prepare_runtime(limit=1000,enable=True)
    # Fixture setup only. Production activation is covered by its own gate suite.
    admin.execute('UPDATE public.discovery_runtime_control SET enabled=true,first_activated_at=coalesce(first_activated_at,clock_timestamp())')
    repo=DiscoveryRepository(database)
    budget=DiscoveryBudgetRepository(database,project_id='Discovery campaign '+str(uuid4()),
        settings=DiscoverySettings(),interval_minutes=10)

    campaign_id=campaign();target_id=target(campaign_id,'fxcmp000001',paused=True)
    received=scalar('SELECT received_at FROM public.discovery_repair_targets WHERE target_id=%s',(target_id,))
    token=str(uuid4())
    check(budget.reserve(token,bucket='general',campaign_id=campaign_id)['allowed'],'campaign_first_credit_reserved')
    admin.execute("UPDATE public.discovery_budget_grants SET created_at=now()-interval '101 days',expires_at=now()-interval '100 days' WHERE grant_id=%s",(token,))
    result=clean(database)
    check(result['campaigns_paused']>=1 and scalar('SELECT state FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(campaign_id,))=='paused',
          'all_paused_remainder_has_explicit_campaign_state')
    check(scalar('SELECT completed_at IS NULL FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(campaign_id,)),
          'paused_campaign_is_not_falsely_complete')
    check(scalar("SELECT status='pending' AND received_at=%s FROM public.discovery_repair_targets WHERE target_id=%s",(received,target_id)),
          'cleanup_keeps_paused_intent_and_original_age')
    check(scalar('SELECT count(*) FROM public.discovery_budget_grants WHERE grant_id=%s',(token,))==1,
          'old_paused_campaign_debit_is_not_cleaned_or_refilled')
    check(not budget.reserve(str(uuid4()),bucket='general',campaign_id=campaign_id)['allowed'],
          'paused_campaign_cannot_dispatch_new_work')
    check(take(target_id)==[],'dormant_paused_target_is_not_claimed')

    check(repo.resume(CHANNEL,['fxcmp000001'])==1,'explicit_resume_preserves_campaign_target')
    selected=take(target_id)
    check(len(selected)==1 and scalar('SELECT state FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(campaign_id,))=='pending',
          'repair_claim_reopens_paused_campaign_before_state_filter')
    check(scalar('SELECT received_at FROM public.discovery_repair_targets WHERE target_id=%s',(target_id,))==received,
          'campaign_resume_does_not_reset_age')
    check(budget.reserve(str(uuid4()),bucket='general',campaign_id=campaign_id)['allowed'],
          'resumed_campaign_uses_only_remaining_credit')
    rejected=budget.reserve(str(uuid4()),bucket='general',campaign_id=campaign_id)
    check(not rejected['allowed'] and rejected['reason']=='campaign_budget','resumed_campaign_cannot_refill_spent_allowance')
    check(not budget.begin_dispatch(token),'old_expired_grant_stays_invalid_after_resume')
    request=str(uuid4());at=now();repo.start_request(selected,request,at);repo.complete_attempt(selected,request,now())
    repo.defer(selected,'metadata_not_returned',now(),request_id=request)
    clean(database)
    check(scalar('SELECT state FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(campaign_id,))=='paused',
          'unresolved_resumed_attempt_returns_to_paused_campaign')
    check(scalar('SELECT count(*) FROM public.discovery_repair_targets WHERE target_id=%s',(target_id,))==1,
          'repeated_maintenance_never_drops_paused_debt')

    mixed=campaign();target(mixed,'fxcmp000002',paused=True)
    future_id=target(mixed,'fxcmp000003')
    admin.execute("UPDATE public.discovery_repair_targets SET next_try_at=now()+interval '2 days' WHERE target_id=%s",(future_id,))
    clean(database)
    check(scalar('SELECT state FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(mixed,))=='pending',
          'future_due_remainder_does_not_mean_campaign_paused')
    epoch=scalar('SELECT control_epoch FROM public.channel_collection_state WHERE channel_id=%s',(CHANNEL,))
    admin.execute("UPDATE public.discovery_repair_targets SET paused_at=now(),pause_reason='long_wait',claim_token=%s,claim_expires_at=now()+interval '5 minutes',claim_channel_epoch=%s WHERE target_id=%s",(str(uuid4()),epoch,future_id))
    clean(database)
    check(scalar('SELECT state FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(mixed,))=='pending',
          'live_claim_is_not_misreported_as_all_paused')
    admin.execute('UPDATE public.discovery_repair_targets SET claim_token=NULL,claim_expires_at=NULL WHERE target_id=%s',(future_id,))
    clean(database)
    check(scalar('SELECT state FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(mixed,))=='paused',
          'campaign_pauses_only_after_last_active_claim_finishes')

    duplicate_campaign=campaign();duplicate_target=target(duplicate_campaign,'fxcmp000004')
    event_id=scalar('''INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind,
        received_at,paused_at,pause_reason) VALUES(%s,%s,'fxcmp000004','entry',%s,clock_timestamp(),'long_wait') RETURNING event_id''',
        (sha256(str(uuid4()).encode()).hexdigest(),CHANNEL,now()-timedelta(days=31)))
    selected=take(duplicate_target)
    check(len(selected)==1,'truly_sleeping_websub_does_not_block_repair_duplicate')
    repo.defer(selected,'budget',now())
    admin.execute('UPDATE public.websub_events SET resume_token=%s WHERE event_id=%s',(str(uuid4()),event_id))
    check(take(duplicate_target)==[],'outstanding_websub_resume_still_owns_duplicate_work')
    admin.execute("UPDATE public.websub_events SET resume_served_token=resume_token,claim_token=%s,claim_expires_at=now()+interval '5 minutes',claim_channel_epoch=%s WHERE event_id=%s",(str(uuid4()),epoch,event_id))
    check(take(duplicate_target)==[],'live_websub_claim_still_blocks_duplicate_work')
    admin.execute('UPDATE public.websub_events SET claim_token=NULL,claim_expires_at=NULL WHERE event_id=%s',(event_id,))
    check(len(take(duplicate_target))==1,'repair_proceeds_when_duplicate_is_really_sleeping_again')

    # Dormant rows remain retained, but neither active nor resume hot index can
    # include them. Actual claims above exercise the two CTEs and lock paths.
    prefix=sha256(str(uuid4()).encode()).hexdigest()[:56]
    admin.execute('''INSERT INTO public.websub_events(dedupe_key,channel_id,video_id,kind,
        received_at,next_try_at,paused_at,pause_reason)
        SELECT %s||lpad(i::text,8,'0'),%s,'fxpq'||lpad(i::text,7,'0'),'entry',
            now()-interval '31 days',now()-interval '30 days',now(),'long_wait'
        FROM generate_series(1,200) i''',(prefix,CHANNEL))
    for table,key,active_index,resume_index in (
            ('websub_events','event_id','websub_events_discovery_due','websub_events_discovery_resume_due'),
            ('discovery_repair_targets','target_id','discovery_repair_targets_due','discovery_repair_targets_resume_due')):
        for index,predicate in ((active_index,'paused_at IS NULL'),
                (resume_index,'paused_at IS NOT NULL AND resume_token IS DISTINCT FROM resume_served_token')):
            actual=scalar('SELECT pg_get_expr(indpred,indrelid) FROM pg_index WHERE indexrelid=%s::regclass',('public.'+index,))
            check(('paused_at IS NULL' if index==active_index else 'resume_token IS DISTINCT FROM resume_served_token') in actual,
                  index+'_excludes_dormant_rows')
            with admin.transaction():
                admin.execute('SET LOCAL enable_seqscan=off')
                plan=scalar('EXPLAIN (FORMAT JSON,COSTS OFF) SELECT '+key+' FROM public.'+table+
                    " WHERE status='pending' AND work_phase<>'settled' AND "+predicate+
                    ' AND next_try_at<=clock_timestamp() ORDER BY next_try_at,'+key+' LIMIT 5')
                check(index in json.dumps(plan),index+'_serves_matching_hot_selection')
    return checks
