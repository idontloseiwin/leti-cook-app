"""Durable admission, not an estimate of Google's remaining quota.

One grant admits one provider HTTP attempt. A signal-caused full page consumes
both admission limits while the normal quota ledger records the request once.
Uncertain commits are resolved with the same token; a dispatched grant is never
reused. Counters are deliberately not refunded after a crash or cancellation.
"""
from uuid import UUID

from .base.postgres import Operation

_CLOCK = """WITH clock AS MATERIALIZED (SELECT clock_timestamp() AS now) SELECT now,
 (now AT TIME ZONE 'America/Los_Angeles')::date AS day,
 date_trunc('day',now AT TIME ZONE 'America/Los_Angeles')
 AT TIME ZONE 'America/Los_Angeles' AS day_start,
 (date_trunc('day',now AT TIME ZONE 'America/Los_Angeles')+interval '1 day')
 AT TIME ZONE 'America/Los_Angeles' AS reset FROM clock"""
_INSERT = """INSERT INTO public.discovery_budget_days
 (project_id,bucket,quota_day,event_limit,full_limit,window_start,window_end,window_event_limit,window_full_limit)
 VALUES(%(project)s,%(bucket)s,%(day)s,%(event_limit)s,%(full_limit)s,%(start)s,%(end)s,%(window_event_limit)s,%(window_full_limit)s)
 ON CONFLICT DO NOTHING"""
_LOCK = """SELECT * FROM public.discovery_budget_days
 WHERE project_id=%(project)s AND bucket=%(bucket)s AND quota_day=%(day)s FOR UPDATE"""
_GRANT = "SELECT * FROM public.discovery_budget_grants WHERE grant_id=%s::uuid"
_CAMPAIGN = "SELECT * FROM public.discovery_repair_campaigns WHERE campaign_id=%s::uuid FOR UPDATE"
_CAMPAIGN_USED = """SELECT count(*) AS used FROM public.discovery_budget_grants
 WHERE campaign_id=%s::uuid AND bucket=%s"""
_ROLL = """UPDATE public.discovery_budget_days SET window_start=%(start)s,window_end=%(end)s,
 window_event_limit=%(window_event_limit)s,window_full_limit=%(window_full_limit)s,
 window_event_used=0,window_full_used=0
 WHERE project_id=%(project)s AND bucket=%(bucket)s AND quota_day=%(day)s RETURNING *"""
_DEBIT = """UPDATE public.discovery_budget_days SET event_used=event_used+%(event)s,
 full_used=full_used+%(full)s,window_event_used=window_event_used+%(event)s,
 window_full_used=window_full_used+%(full)s
 WHERE project_id=%(project)s AND bucket=%(bucket)s AND quota_day=%(day)s"""
_SAVE = """INSERT INTO public.discovery_budget_grants
 (grant_id,project_id,bucket,quota_day,window_start,expires_at,event_units,full_units,campaign_id)
 VALUES(%(token)s::uuid,%(project)s,%(bucket)s,%(day)s,%(start)s,%(end)s,%(event)s,%(full)s,%(campaign)s::uuid)
 RETURNING *"""
_BEGIN = """UPDATE public.discovery_budget_grants SET dispatch_started_at=clock_timestamp()
 WHERE grant_id=%s::uuid AND dispatch_started_at IS NULL AND expires_at>clock_timestamp()
 RETURNING grant_id"""


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('discovery_grant_id_invalid')
    return value


class DiscoveryBudgetRepository:
    def __init__(self, database, *, project_id, settings, interval_minutes):
        if (type(project_id) is not str or not 1 <= len(project_id) <= 128
                or project_id != project_id.strip() or type(interval_minutes) is not int
                or not 1 <= interval_minutes <= 1440):
            raise ValueError('discovery_budget_config_invalid')
        self.database, self.project = database, project_id
        self.settings, self.interval = settings, interval_minutes

    def _args(self, tx, bucket):
        from datetime import timedelta
        if bucket not in ('general', 'stats'):
            raise ValueError('discovery_bucket_invalid')
        clock = tx.fetch_one(_CLOCK)
        duration = timedelta(minutes=self.interval)
        start = clock['day_start'] + duration * ((clock['now']-clock['day_start']) // duration)
        s = self.settings
        return dict(project=self.project, bucket=bucket, day=clock['day'], now=clock['now'], reset=clock['reset'],
                    start=start, end=min(start+duration, clock['reset']),
                    event_limit=getattr(s, f'events_{bucket}_daily_limit'),
                    full_limit=s.full_general_daily_limit if bucket=='general' else 0,
                    window_event_limit=getattr(s, f'events_{bucket}_window_limit'),
                    window_full_limit=s.full_general_window_limit if bucket=='general' else 0)

    def reserve(self, token, *, bucket, event=True, full=False, campaign_id=None):
        """Idempotent reservation; a caller must also win begin_dispatch()."""
        _uuid(token)
        if (type(event) is not bool or type(full) is not bool or not(event or full)
                or (full and bucket!='general') or (campaign_id is not None and not event)):
            raise ValueError('discovery_budget_request_invalid')
        if campaign_id is not None: _uuid(campaign_id)
        with self.database.transaction(Operation('discovery_budget_reserve', total_timeout_ms=5000)) as tx:
            # One global lock order: optional campaign, then project/bucket/day.
            campaign = tx.fetch_one(_CAMPAIGN, (campaign_id,)) if campaign_id else None
            if campaign_id and (not campaign or campaign['state']!='pending'):
                return dict(allowed=False, reason='campaign_closed')
            args=self._args(tx,bucket)
            tx.execute(_INSERT,args)
            row=tx.fetch_one(_LOCK,args)
            existing=tx.fetch_one(_GRANT,(token,))
            if existing:
                if (existing['project_id']!=self.project or existing['bucket']!=bucket
                        or existing['event_units']!=int(event) or existing['full_units']!=int(full)
                        or (str(existing['campaign_id']) if existing['campaign_id'] else None)!=campaign_id):
                    raise ValueError('discovery_grant_replay_mismatch')
                return dict(allowed=existing['dispatch_started_at'] is None and existing['expires_at']>args['now']
                            and existing['quota_day']==args['day'] and not(row['blocked_until'] and row['blocked_until']>args['now']),
                            reason='existing_grant', grant=existing)
            if row['blocked_until'] and row['blocked_until']>args['now']:
                return dict(allowed=False,reason='provider_quota',next_try_at=row['blocked_until'])
            if row['window_end']<=args['now']:
                from datetime import timedelta
                period=timedelta(minutes=self.interval)
                args['start']=row['window_end']+period*((args['now']-row['window_end'])//period)
                args['end']=min(args['start']+period,args['reset'])
                row=tx.returning_one(_ROLL,args)
            if campaign:
                used=tx.fetch_one(_CAMPAIGN_USED,(campaign_id,bucket))['used']
                limit=campaign['general_limit' if bucket=='general' else 'statistics_limit']
                if used>=limit:return dict(allowed=False,reason='campaign_budget')
            if row['event_used']+int(event)>row['event_limit'] or row['full_used']+int(full)>row['full_limit']:
                return dict(allowed=False,reason='daily_budget',next_try_at=args['reset'])
            if (row['window_event_used']+int(event)>row['window_event_limit']
                    or row['window_full_used']+int(full)>row['window_full_limit']):
                return dict(allowed=False,reason='window_budget',next_try_at=row['window_end'])
            args.update(token=token,event=int(event),full=int(full),campaign=campaign_id,
                        start=row['window_start'],end=min(row['window_end'],args['reset']))
            tx.execute(_DEBIT,args)
            return dict(allowed=True,reason='reserved',grant=tx.returning_one(_SAVE,args))

    def begin_dispatch(self, token):
        """One-shot provider admission. Database failure never means permission."""
        _uuid(token)
        with self.database.transaction(Operation('discovery_budget_dispatch',total_timeout_ms=5000)) as tx:
            grant=tx.fetch_one(_GRANT,(token,))
            if not grant or grant['project_id']!=self.project:return False
            if grant['campaign_id']:
                campaign=tx.fetch_one(_CAMPAIGN,(grant['campaign_id'],))
                if not campaign or campaign['state']!='pending':return False
            args=self._args(tx,grant['bucket'])
            if grant['quota_day']!=args['day']:return False
            row=tx.fetch_one(_LOCK,args)
            if not row or (row['blocked_until'] and row['blocked_until']>args['now']):return False
            return tx.returning_one(_BEGIN,(token,)) is not None

    def note_exhausted(self, bucket):
        """Call only for a real quotaExceeded; never for 429, 5xx or timeout."""
        from .quota.provider_quota_control import ProviderQuotaControlRepository
        return ProviderQuotaControlRepository(self.database, project_id=self.project,
            settings=self.settings, interval_minutes=self.interval).note_exhausted(bucket)

    def validate_dispatch(self, token, *, bucket=None):
        """Read-only final check after provider waiting; never debit or replay.

        begin_dispatch belongs to the coordinator. Its one issued HTTP attempt
        still needs a current day, unexpired window, open campaign and no ban
        when the transport is about to send it.
        """
        _uuid(token)
        if bucket is not None and bucket not in ('general', 'stats'):
            raise ValueError('discovery_bucket_invalid')
        from .quota.provider_quota_control import bounded_control_read
        return bounded_control_read(lambda timeout,deadline: self._validate_dispatch(token,bucket,timeout,deadline))

    def _validate_dispatch(self, token, bucket, timeout_ms, deadline):
        with self.database.transaction(Operation('discovery_budget_validate_dispatch',
                read_only=True,total_timeout_ms=timeout_ms,deadline_monotonic=deadline)) as tx:
            grant=tx.fetch_one(_GRANT,(token,))
            if (not grant or grant['project_id']!=self.project
                    or (bucket is not None and grant['bucket']!=bucket)):
                return dict(allowed=False,reason='grant_mismatch')
            clock=tx.fetch_one(_CLOCK)
            if grant['dispatch_started_at'] is None:
                return dict(allowed=False,reason='grant_not_dispatchable')
            if grant['quota_day']!=clock['day'] or grant['expires_at']<=clock['now']:
                return dict(allowed=False,reason='grant_expired')
            if grant['campaign_id']:
                campaign=tx.fetch_one(_CAMPAIGN.replace(' FOR UPDATE',''),(grant['campaign_id'],))
                if not campaign or campaign['state']!='pending':
                    return dict(allowed=False,reason='campaign_closed')
            row=tx.fetch_one(_LOCK.replace(' FOR UPDATE',''),
                dict(project=self.project,bucket=grant['bucket'],day=clock['day']))
            if not row:
                return dict(allowed=False,reason='grant_not_dispatchable')
            if row['blocked_until'] and row['blocked_until']>clock['now']:
                return dict(allowed=False,reason='provider_quota',next_try_at=row['blocked_until'])
            return dict(allowed=True,reason='dispatchable')

    def blocked(self, bucket):
        with self.database.transaction(Operation('discovery_budget_read',read_only=True,total_timeout_ms=5000)) as tx:
            args=self._args(tx,bucket)
            row=tx.fetch_one(_LOCK.replace(' FOR UPDATE',''),args)
            return bool(row and row['blocked_until'] and row['blocked_until']>args['now'])
