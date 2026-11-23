"""Bounded advisory reconnect pages for the shared collection coordinator."""
from threading import get_ident

from collection.dispatch import Coordinator
from collection.reconnect import ReconnectUnit
from db.user_work.user_work_reconnect import ReconnectCandidateCursor
from user_work_backup import uid


class ReconnectFeeder:
    """A sweep cursor only: the Coordinator owns every ready/held descriptor.

    The lifetime owner starts subsequent sweeps on its wake/poll policy, never
    by resetting a partly scanned prefix. Repository candidates carry no source
    payload and obtain no lease; the existing exact unit claims on prepare.
    """
    def __init__(self, repository, *, resolve_channel, require_ready,
                 require_source, lease_seconds=180, max_attempts=3,
                 on_settled=None, should_stop=None):
        if (not callable(getattr(repository, 'reconnect_candidate_page', None))
                or not all(callable(v) for v in (resolve_channel, require_ready, require_source))
                or type(lease_seconds) is not int or not 30 <= lease_seconds <= 600
                or type(max_attempts) is not int or not 1 <= max_attempts <= 20
                or on_settled is not None and not callable(on_settled)
                or should_stop is not None and not callable(should_stop)):
            raise ValueError('reconnect_feeder_config_invalid')
        self.repository = repository
        self.options = dict(resolve_channel=resolve_channel, require_ready=require_ready,
                            require_source=require_source, lease_seconds=lease_seconds,
                            max_attempts=max_attempts, on_settled=on_settled, should_stop=should_stop)
        self.after = self.ceiling = None
        self.exhausted = False
        self.inspected = 0
        self._owner, self._coordinator = get_ident(), None

    def _check(self):
        if get_ident() != self._owner:
            raise RuntimeError('reconnect_feeder_owner_thread_required')

    def begin_sweep(self):
        self._check()
        if not self.exhausted:
            raise RuntimeError('reconnect_sweep_not_exhausted')
        if self._coordinator is not None:
            status = self._coordinator.snapshot()
            if status.blocked or status.paused or status.stopped:
                raise RuntimeError('reconnect_sweep_not_admitted')
        self.after = self.ceiling = None
        self.exhausted = False
        self.inspected = 0

    def offer_to(self, coordinator, *, limit=None):
        self._check()
        if limit is not None and (type(limit) is not int or not 0 <= limit <= 100):
            raise ValueError('reconnect_feeder_limit_invalid')
        if (not isinstance(coordinator, Coordinator)
                or self._coordinator is not None and self._coordinator is not coordinator):
            raise ValueError('reconnect_feeder_coordinator_mismatch')
        self._coordinator = coordinator
        status = coordinator.snapshot()
        capacity = min(100, 2 * coordinator.concurrency - status.ready)
        if limit is not None:
            capacity = min(capacity, limit)
        if self.exhausted or not capacity or status.blocked or status.paused or status.stopped:
            return 0
        page = self.repository.reconnect_candidate_page(
            limit=capacity, after=self.after, ceiling=self.ceiling)
        candidates = page.get('candidates')
        inspected, after, ceiling, exhausted = (page.get(k) for k in
            ('inspected', 'next_after', 'ceiling', 'exhausted'))
        if (not isinstance(candidates, list) or type(inspected) is not int
                or not 0 <= len(candidates) <= inspected <= capacity
                or type(exhausted) is not bool or exhausted != (inspected < capacity)
                or not isinstance(ceiling, str)
                or self.ceiling is not None and ceiling != self.ceiling
                or inspected == 0 and after != self.after
                or inspected > 0 and (not isinstance(after, ReconnectCandidateCursor)
                    or self.after is not None and after.order_key() <= self.after.order_key())):
            raise RuntimeError('reconnect_feeder_page_invalid')
        identities = []
        for row in candidates:
            if not isinstance(row, dict) or set(row) != {'id'}:
                raise RuntimeError('reconnect_feeder_page_invalid')
            identity = uid(row['id'])
            if identity != row['id'] or identity in identities:
                raise RuntimeError('reconnect_feeder_page_invalid')
            identities.append(identity)
        offered = 0
        for identity in identities:
            if coordinator.contains('reconnect:' + identity):
                continue
            unit = ReconnectUnit(self.repository, identity=identity, **self.options)
            coordinator.offer(unit.work)
            offered += 1
        self.after, self.ceiling, self.exhausted = after, ceiling, exhausted
        self.inspected += inspected
        return offered
