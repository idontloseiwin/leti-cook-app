"""Operational provider refusals; never consult the observe-only journal.

The daily quota row owns both its ban and discovery admission counters.
Writing a ban neither spends admission nor resets the row's policy snapshot.
"""
import time

from ..base.postgres import DatabaseUnavailable, Operation

_READ = """SELECT bucket,blocked_until FROM readable.provider_quota_control
 WHERE project_id=%s AND quota_day=%s ORDER BY bucket LIMIT 2"""
_BAN = """UPDATE public.discovery_budget_days SET blocked_until=%(reset)s
 WHERE project_id=%(project)s AND bucket=%(bucket)s AND quota_day=%(day)s"""


def bounded_control_read(read):
    """Retry one connection-loss read inside the original five-second bound.

    Every attempt opens its own read-only transaction. Pool pressure, unknown
    outcomes, invalid state and server/permission errors are never retried.
    This helper must not wrap admission debits, refusal writes or provider I/O.
    """
    deadline = time.monotonic() + 5
    try:
        return read(5000,deadline)
    except DatabaseUnavailable as error:
        remaining = int((deadline-time.monotonic())*1000)
        if (error.message != 'database_connection_failed' or error.outcome_unknown
                or error.reason not in ('connection_failed', 'unavailable') or remaining < 1):
            raise
        return read(min(5000, remaining),deadline)


class ProviderQuotaControlRepository:
    def __init__(self, database, *, project_id='primary', settings=None, interval_minutes=None):
        if (type(project_id) is not str or not 1 <= len(project_id) <= 128
                or project_id != project_id.strip()):
            raise ValueError('quota_control_project_invalid')
        self.database, self.project = database, project_id
        self.settings, self.interval = settings, interval_minutes

    def snapshot(self):
        """At most two operational rows, also available to HTTP's lic_reader.

        The narrow view exposes current-day bans, never admission counters or
        grants. A read failure propagates as unknown under either role.
        """
        return bounded_control_read(self._snapshot)

    def _snapshot(self, timeout_ms, deadline):
        from ..discovery_budget import _CLOCK
        with self.database.transaction(Operation('provider_quota_control_read', read_only=True,
                                                 total_timeout_ms=timeout_ms,deadline_monotonic=deadline)) as tx:
            clock = tx.fetch_one(_CLOCK)
            rows = tx.fetch_all(_READ, (self.project, clock['day']), max_rows=2)
            banned = {row['bucket']: row['blocked_until'] for row in rows
                      if row['blocked_until'] and row['blocked_until'] > clock['now']}
            return {'quota_day': clock['day'], 'next_reset_at': clock['reset'],
                    'exhausted': tuple(bucket for bucket in ('general', 'stats') if bucket in banned)}

    def note_exhausted(self, bucket, *, quota_day=None):
        """Idempotent actual refusal write, independent of diagnostic delivery."""
        from ..discovery_budget import DiscoveryBudgetRepository, _INSERT, _LOCK
        budget = DiscoveryBudgetRepository(self.database, project_id=self.project,
            settings=self.settings, interval_minutes=self.interval)
        with self.database.transaction(Operation('provider_quota_control_exhausted',
                                                 total_timeout_ms=5000)) as tx:
            args = budget._args(tx, bucket)
            if quota_day is not None and quota_day != args['day']:
                # An old-day pending write may reach storage after midnight.
                # Do not turn yesterday's refusal into today's ban.
                return None
            tx.execute(_INSERT, args)
            tx.fetch_one(_LOCK, args)
            tx.execute(_BAN, args)
            return args['reset']
