"""Bounded retention of discovery quota and repair records.

No YouTube payload or source clock is renewed here. Pending obligations and
their campaign debits remain. A compact closed campaign tombstone prevents a
second campaign for the same cutover from receiving a fresh allowance.
"""
from ..base.postgres import Operation
from ..discovery_repair import refresh_campaign_states


def clean_in_transaction(tx, *, limit=200, days=90):
    if type(limit) is not int or not 1<=limit<=1000 or type(days) is not int or days<1:
        raise ValueError('discovery_retention_bounds_invalid')
    clock=tx.fetch_one('''SELECT clock_timestamp() AS at,
        clock_timestamp()-make_interval(days=>%s) AS cutoff,
        (clock_timestamp() AT TIME ZONE 'America/Los_Angeles')::date-%s::integer AS quota_cutoff''',(days,days))
    result=dict(campaigns_closed=0,targets_deleted=0,grants_deleted=0,days_deleted=0)
    result.update(refresh_campaign_states(tx,limit=limit))
    # Budget reserve/dispatch use the same campaign-first lock order. Once
    # closed, no new grant is possible; its exact spent total is now stable.
    campaigns=tx.fetch_all('''SELECT campaign_id FROM public.discovery_repair_campaigns c
        WHERE c.completed_at IS NULL AND (c.state='cancelled' OR (c.state IN ('pending','paused')
            AND NOT EXISTS(SELECT 1 FROM public.discovery_repair_targets t
                WHERE t.campaign_id=c.campaign_id AND t.status='pending')))
        ORDER BY created_at,campaign_id LIMIT %s FOR UPDATE SKIP LOCKED''',(limit,),max_rows=limit)
    for campaign in campaigns:
        result['campaigns_closed']+=tx.execute('''UPDATE public.discovery_repair_campaigns c SET
            state=CASE WHEN state IN ('pending','paused') THEN 'complete' ELSE state END,completed_at=%s,
            general_spent=(SELECT coalesce(sum(event_units) FILTER(WHERE bucket='general'),0)
                FROM public.discovery_budget_grants WHERE campaign_id=c.campaign_id),
            statistics_spent=(SELECT coalesce(sum(event_units) FILTER(WHERE bucket='stats'),0)
                FROM public.discovery_budget_grants WHERE campaign_id=c.campaign_id)
            WHERE c.campaign_id=%s::uuid AND c.completed_at IS NULL''',(clock['at'],campaign['campaign_id']))
    result['targets_deleted']=tx.execute('''DELETE FROM public.discovery_repair_targets WHERE target_id IN (
        SELECT t.target_id FROM public.discovery_repair_targets t
        JOIN public.discovery_repair_campaigns c USING(campaign_id)
        WHERE c.state IN ('complete','cancelled') AND c.completed_at<=%s
          AND (t.status<>'pending' OR c.state='cancelled')
        ORDER BY c.completed_at,t.target_id LIMIT %s FOR UPDATE OF t SKIP LOCKED)''',
        (clock['cutoff'],limit))
    result['grants_deleted']=tx.execute('''DELETE FROM public.discovery_budget_grants WHERE grant_id IN (
        SELECT g.grant_id FROM public.discovery_budget_grants g
        LEFT JOIN public.discovery_repair_campaigns c USING(campaign_id)
        WHERE g.created_at<=%s AND g.expires_at<=%s
          AND (g.campaign_id IS NULL OR (c.state IN ('complete','cancelled') AND c.completed_at<=%s))
        ORDER BY g.created_at,g.grant_id LIMIT %s FOR UPDATE OF g SKIP LOCKED)''',
        (clock['cutoff'],clock['at'],clock['cutoff'],limit))
    result['days_deleted']=tx.execute('''DELETE FROM public.discovery_budget_days WHERE (project_id,bucket,quota_day) IN (
        SELECT d.project_id,d.bucket,d.quota_day FROM public.discovery_budget_days d
        WHERE d.quota_day<%s AND d.window_end<=%s AND (d.blocked_until IS NULL OR d.blocked_until<=%s)
          AND NOT EXISTS(SELECT 1 FROM public.discovery_budget_grants g
            WHERE g.project_id=d.project_id AND g.bucket=d.bucket AND g.quota_day=d.quota_day)
        ORDER BY d.quota_day,d.project_id,d.bucket LIMIT %s FOR UPDATE OF d SKIP LOCKED)''',
        (clock['quota_cutoff'],clock['at'],clock['at'],limit))
    return result


def clean(database, *, limit=200, days=90):
    with database.transaction(Operation('discovery_operational_cleanup',statement_timeout_ms=5000,
            lock_timeout_ms=1000,total_timeout_ms=10000)) as tx:
        return clean_in_transaction(tx,limit=limit,days=days)
