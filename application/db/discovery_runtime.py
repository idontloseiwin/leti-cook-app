"""Read-only, bounded discovery wake advice; durable owners still admit work.

This probe never claims rows, changes clocks or grants quota. Inactive storage or
a pre-v5 process snapshot leaves the new host off. A lost wake is recoverable
from the same persisted obligations used by the child.
"""
from discovery_policy import DiscoverySettings
from .base.postgres import Operation
from .channel.channel_read_policy import CHANNEL_CONTROL_SQL
from .discovery_budget import _CLOCK

_CONTROL = 'SELECT enabled FROM public.discovery_runtime_control WHERE singleton'
_ELIGIBLE = """coalesce((SELECT p.typed_runtime_eligible FROM (""" + CHANNEL_CONTROL_SQL + """) p
 WHERE p.channel_id=e.channel_id),false)"""


def runtime_enabled(database):
    with database.transaction(Operation('discovery_runtime_read', read_only=True,
            statement_timeout_ms=5000, total_timeout_ms=8000)) as tx:
        row = tx.fetch_one(_CONTROL)
        return bool(row and row['enabled'])


def _event_probe(tx, table, key, *, repair=False):
    extra = """ AND EXISTS(SELECT 1 FROM public.discovery_repair_campaigns campaign
        WHERE campaign.campaign_id=e.campaign_id AND (campaign.state='pending'
          OR (campaign.state='paused' AND e.resume_token IS DISTINCT FROM e.resume_served_token)))""" if repair else ''
    # The two pause arms mirror the two owner indexes. No locks/leases here.
    predicate = """e.status='pending' AND e.work_phase<>'settled'
      AND (e.claim_token IS NULL OR e.claim_expires_at<=clock_timestamp())
      AND (e.paused_at IS NULL OR e.resume_token IS DISTINCT FROM e.resume_served_token)
      AND """ + _ELIGIBLE + extra
    provider = tx.fetch_one(f"""SELECT e.next_try_at FROM public.{table} e
        WHERE {predicate} AND e.work_phase<>'owner_wait'
        ORDER BY e.next_try_at,e.{key} LIMIT 1""")
    owner = tx.fetch_one(f"""SELECT e.next_try_at FROM public.{table} e
        WHERE {predicate} AND e.work_phase='owner_wait'
        ORDER BY e.next_try_at,e.{key} LIMIT 1""")
    return provider, owner


def _hydration_due(tx, video_types):
    from .videos.video_initial_policy import VIDEO_INITIAL_PENDING_STATE_SQL
    row = tx.fetch_one("""SELECT state.video_id FROM public.video_collection_state state
      WHERE state.discovery_hydration_requested_at IS NOT NULL AND state.paused_reason IS NULL
      AND state.next_due_at<=now() AND (state.lease_token IS NULL OR state.lease_expires_at<=now())
      AND (state.video_type IS NULL OR state.video_type=ANY(%s::text[]))
      AND NOT """ + VIDEO_INITIAL_PENDING_STATE_SQL + """
      AND coalesce((SELECT p.typed_runtime_eligible FROM (""" + CHANNEL_CONTROL_SQL + """) p
          WHERE p.channel_id=state.channel_id),false)
      ORDER BY state.discovery_hydration_requested_at,state.video_id LIMIT 1""", (list(video_types),))
    return row is not None


def _source_due(tx, video_types, schedules):
    from .inventory.inventory_claims import (_CANDIDATE_PREDICATE, _EVENT_DUE, _SIGNAL_DUE,
                                             _AUTOMATIC_FULL_REQUIRED_SQL)
    from .inventory.inventory_heads import HEAD_DUE_SQL, HEAD_COUNT_DUE_SQL
    duration = ('CASE d.video_type WHEN \'long\' THEN %(long_seconds)s::integer '
                'WHEN \'short\' THEN %(short_seconds)s::integer ELSE %(stream_seconds)s::integer END')
    full_required = _AUTOMATIC_FULL_REQUIRED_SQL.replace('%(reconciliation_seconds)s', '(' + duration + ')')
    row = tx.fetch_one('SELECT d.channel_id,(' + HEAD_DUE_SQL + ') AS head_due,('
        + HEAD_COUNT_DUE_SQL + ') AS head_signal,(' + _EVENT_DUE + ') AS event_due,(' + full_required + """) AS full_required
      FROM public.channel_playlist_state d JOIN public.channel_collection_state c USING(channel_id)
      WHERE (""" + _CANDIDATE_PREDICATE + ') AND (' + _SIGNAL_DUE + """)
       ORDER BY head_due DESC,head_signal,full_required,d.dispatch_ordinal,d.priority DESC,d.channel_id,d.video_type LIMIT 1""",
      dict(video_types=list(video_types), channel_ids=None, scopes_text=None, include_due=True,
           only_due=True, continuations_text=None, initial_job_id=None, initial_job_lease_token=None,
           **{kind+'_seconds': getattr(schedules,kind).reconciliation_seconds for kind in ('long','short','stream')}))
    return row


def _budget_wait(row, clock, *, full=False, event=True):
    """The existing ledger owns current-day limits and window expiration."""
    if row is None:
        return None
    if row['blocked_until'] is not None and row['blocked_until'] > clock['now']:
        return row['blocked_until']
    if (event and row['event_used'] >= row['event_limit']) or (full and row['full_used'] >= row['full_limit']):
        return clock['reset']
    if row['window_end'] > clock['now'] and (
            (event and row['window_event_used'] >= row['window_event_limit'])
            or (full and row['window_full_used'] >= row['window_full_limit'])):
        return row['window_end']
    return None


def runtime_status(database, *, project_id, settings, interval_minutes, video_types=('long', 'short')):
    if settings is None:
        return dict(enabled=False, runnable=False, next_retry=None, budget_wait=False)
    if (type(settings) is not DiscoverySettings or type(project_id) is not str or not project_id
            or type(interval_minutes) is not int or not 1 <= interval_minutes <= 1440
            or not isinstance(video_types, (tuple, list)) or not video_types
            or set(video_types) - {'long', 'short', 'stream'}):
        raise ValueError('discovery_runtime_snapshot_invalid')
    with database.transaction(Operation('discovery_runtime_probe', read_only=True,
            statement_timeout_ms=5000, total_timeout_ms=8000)) as tx:
        control = tx.fetch_one(_CONTROL)
        if not control or control['enabled'] is not True:
            return dict(enabled=False, runnable=False, next_retry=None, budget_wait=False)
        clock = tx.fetch_one(_CLOCK)
        events, event_owner = _event_probe(tx, 'websub_events', 'event_id')
        repair, repair_owner = _event_probe(tx, 'discovery_repair_targets', 'target_id', repair=True)
        owners = [row['next_try_at'] for row in (event_owner, repair_owner) if row]
        providers = [row['next_try_at'] for row in (events, repair) if row]
        hydration = _hydration_due(tx, video_types)
        from .videos.collection_policy import require_cadence_snapshot
        source = _source_due(tx, video_types, require_cadence_snapshot(database).inventory_by_type)
        row = tx.fetch_one("""SELECT * FROM public.discovery_budget_days
          WHERE project_id=%s AND bucket='general' AND quota_day=%s""", (project_id, clock['day']))
        provider_wait = _budget_wait(row, clock)
        head_due=bool(source and source.get('head_due'))
        source_wait = _budget_wait(row, clock, full=bool(source and source['full_required'] and not head_due),
                                  event=bool(source and (source.get('head_signal') if head_due else source['event_due'])))
        owner_due = any(at <= clock['now'] for at in owners)
        provider_due = hydration or any(at <= clock['now'] for at in providers)
        runnable = owner_due or (provider_due and provider_wait is None) or (source is not None and source_wait is None)
        waits = [at for at in owners + providers if at > clock['now']]
        blocked = []
        if provider_due and provider_wait is not None:
            blocked.append(provider_wait)
        if source is not None and source_wait is not None:
            blocked.append(source_wait)
        # Sleeping demand must not delay new signals. Budget-only suppression is
        # safe only when every provider-backed path shares the same spent gate.
        all_budget_blocked = not owner_due and provider_wait is not None
        waits.extend(blocked)
        return dict(enabled=True, runnable=bool(runnable), next_retry=min(waits) if waits else None,
                    budget_wait=bool(not runnable and all_budget_blocked))
