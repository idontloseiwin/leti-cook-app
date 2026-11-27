"""Bounded discovery audit cleanup on the parent's disposable PostgreSQL 17."""
from datetime import timedelta
from hashlib import sha256
import os
from uuid import uuid4

from db.discovery_budget import DiscoveryBudgetRepository
from runtime.discovery import DiscoveryMaintenance
from discovery_policy import DiscoverySettings


def run_checks(admin,database):
    if os.environ.get('LIC_RESEARCH_QUERY_DISPOSABLE')!='1':
        raise RuntimeError('disposable_cluster_required')
    def scalar(sql,args=()):
        row=admin.execute(sql,args).fetchone()
        return next(iter(row.values())) if isinstance(row,dict) else row[0]
    checks=[]
    def check(ok,label):
        if not ok:raise AssertionError(label)
        checks.append(label)
    now=scalar('SELECT clock_timestamp()');old=now-timedelta(days=100)
    prefix='discovery-retention-'+str(uuid4())
    channel='UC'+'fxretention'.ljust(22,'0')

    def day(suffix,at):
        project=prefix+suffix
        quota_day=scalar("SELECT (%s::timestamptz AT TIME ZONE 'America/Los_Angeles')::date",(at,))
        admin.execute('''INSERT INTO public.discovery_budget_days(project_id,bucket,quota_day,event_limit,full_limit,
            event_used,full_used,window_start,window_end,window_event_limit,window_full_limit)
            VALUES(%s,'general',%s,20,20,1,0,%s,%s,10,10)''',(project,quota_day,at,at+timedelta(minutes=10)))
        return project,quota_day,at

    def campaign(suffix,state,completed=None):
        identity=str(uuid4());end=old+timedelta(seconds=suffix)
        admin.execute('''INSERT INTO public.discovery_repair_campaigns(campaign_id,definition_digest,definition_version,
            window_start,window_end,target_limit,general_limit,statistics_limit,state,created_at,completed_at,
            target_count,general_spent,statistics_spent)
            VALUES(%s,%s,1,%s,%s,300,600,50,%s,%s,%s,1,%s,%s)''',
            (identity,sha256(identity.encode()).hexdigest(),end-timedelta(days=7),end,state,old,completed,
             1 if completed else None,0 if completed else None))
        return identity

    def target(campaign,video,status,processed):
        return scalar('''INSERT INTO public.discovery_repair_targets(campaign_id,channel_id,video_id,
            source_event_ids,received_at,status,processed_at,work_phase)
            VALUES(%s,%s,%s,ARRAY[1]::bigint[],%s,%s,%s,%s) RETURNING target_id''',
            (campaign,channel,video,old,status,processed,'metadata_wait' if status=='pending' else 'settled'))

    def grant(day,campaign=None):
        project,quota_day,at=day;token=str(uuid4())
        admin.execute('''INSERT INTO public.discovery_budget_grants(grant_id,project_id,bucket,quota_day,window_start,
            expires_at,event_units,full_units,campaign_id,created_at)
            VALUES(%s,%s,'general',%s,%s,%s,1,0,%s,%s)''',
            (token,project,quota_day,at,at+timedelta(minutes=10),campaign,at))
        return token

    old_closed=day('-closed',old);old_pending=day('-pending',old)
    old_plain=day('-plain',old);current=day('-current',now)
    closed=campaign(1,'complete',now-timedelta(days=91))
    pending=campaign(2,'pending');finishable=campaign(3,'pending')
    cancelled=campaign(4,'cancelled',now-timedelta(days=91))
    terminal=target(closed,'fxr00000001','done',old)
    waiting=target(pending,'fxr00000002','pending',None)
    target(finishable,'fxr00000003','done',now)
    cancelled_wait=target(cancelled,'fxr00000004','pending',None)
    deleted_grant=grant(old_closed,closed);pending_grant=grant(old_pending,pending)
    plain_grant=grant(old_plain);current_grant=grant(current)
    grant(current,finishable)
    # Run the actual backend owner with no configured callback, rather than calling SQL by hand.
    owner = DiscoveryMaintenance(database, ready=lambda: None, clock=lambda: 100)
    counts = {}
    cleanup = owner.cleanup
    def collect_counts():
        counts.update(cleanup())
    owner.cleanup = collect_counts
    check(owner.step() == 3600 and owner.last_cleanup_error is None, 'unconfigured_websub_cleanup_succeeds')
    check(bool(counts) and all(type(value) is int and 0 <= value <= 200 for value in counts.values()),
          'cleanup_each_stage_bounded')
    check(scalar('SELECT count(*) FROM public.discovery_repair_targets WHERE target_id=%s',(terminal,))==0,
          'closed_terminal_target_expires_after_operational_horizon')
    check(scalar('SELECT count(*) FROM public.discovery_repair_targets WHERE target_id=%s',(waiting,))==1,
          'pending_repair_obligation_survives_age_cleanup')
    check(scalar('SELECT count(*) FROM public.discovery_repair_targets WHERE target_id=%s',(cancelled_wait,))==0,
          'cancelled_campaign_cannot_leave_pending_targets_forever')
    check(scalar('SELECT count(*) FROM public.discovery_budget_grants WHERE grant_id=%s',(pending_grant,))==1,
          'pending_campaign_spend_is_never_refilled_by_cleanup')
    check(scalar('SELECT count(*) FROM public.discovery_budget_grants WHERE grant_id=ANY(%s::uuid[])',
                 ([deleted_grant,plain_grant],))==0,'expired_closed_and_unscoped_grants_deleted')
    check(scalar('SELECT count(*) FROM public.discovery_budget_grants WHERE grant_id=%s',(current_grant,))==1,
          'current_window_grant_is_preserved')
    check(scalar('SELECT count(*) FROM public.discovery_budget_days WHERE project_id=%s',(old_pending[0],))==1,
          'day_with_pending_campaign_grant_is_preserved')
    check(scalar('SELECT count(*) FROM public.discovery_budget_days WHERE project_id=ANY(%s::text[])',
                 ([old_closed[0],old_plain[0]],))==0,'old_empty_day_controls_deleted')
    check(scalar('SELECT state FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(finishable,))=='complete',
          'all_terminal_targets_close_campaign_atomically')
    check(scalar('SELECT general_spent FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(finishable,))==1,
          'closed_campaign_keeps_spend_summary')
    check(scalar('SELECT count(*) FROM public.discovery_repair_campaigns WHERE campaign_id=%s',(closed,))==1,
          'compact_campaign_identity_tombstone_is_retained')
    budget=DiscoveryBudgetRepository(database,project_id=old_closed[0],settings=DiscoverySettings(),interval_minutes=10)
    check(not budget.begin_dispatch(deleted_grant),'removed_expired_grant_never_admits_dispatch')
    return checks
