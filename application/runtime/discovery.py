"""Composition of discovery inside the existing collector, never a second writer."""
from functools import partial
from uuid import uuid4
import logging
import threading
import time


class DiscoveryMaintenance:
    '''One backend owner for discovery bookkeeping, with optional hub work on the same thread.'''

    def __init__(self, database, *, ready, websub=None, clock=time.monotonic):
        from db.retention.discovery_operational_retention import clean
        self.cleanup = partial(clean, database)
        self.ready, self.websub, self.clock = ready, websub, clock
        self.next_cleanup = 0.0
        self.last_cleanup_error = None
        self._stop = threading.Event()
        self._thread = None

    def step(self):
        if self.clock() >= self.next_cleanup:
            try:
                self.ready()
            except Exception:
                self.next_cleanup = self.clock() + 60
            else:
                try:
                    self.cleanup()
                except Exception as error:
                    code = type(error).__name__
                    if self.last_cleanup_error != code:
                        logging.getLogger(__name__).warning('Discovery maintenance failed (%s)', code)
                    self.last_cleanup_error = code
                    self.next_cleanup = self.clock() + 60
                else:
                    self.last_cleanup_error = None
                    self.next_cleanup = self.clock() + 3600
        wait = max(0.0, self.next_cleanup - self.clock())
        if self.websub is not None and not self._stop.is_set():
            wait = min(wait, self.websub.step())
        return wait

    def start(self):
        if self._thread is not None:
            raise RuntimeError('discovery_maintenance_already_started')

        def run():
            while not self._stop.is_set():
                self._stop.wait(self.step())

        self._thread = threading.Thread(target=run, name='discovery-maintenance', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=12)


def discovery_budget(context):
    return discovery_budget_for(context.database,context.supervised_environ())


def discovery_budget_for(database,environ):
    from runtime.collection_schedules import load_process_schedules
    from db.discovery_budget import DiscoveryBudgetRepository
    env=dict(environ)
    schedules=load_process_schedules(env,required=False)
    if schedules is None or schedules.discovery is None:return None
    interval=int(env['LIC_COLLECTION_INTERVAL_MINUTES'])
    project=(env.get('YOUTUBE_QUOTA_PROJECT_ID') or 'primary').strip() or 'primary'
    return DiscoveryBudgetRepository(database,project_id=project,
        settings=schedules.discovery,interval_minutes=interval)


def bind_discovery(context, service, window, *, deadline, budget):
    from collection.discovery_feeder import DiscoveryFeeder
    from db.discovery import DiscoveryRepository
    from db.discovery_dispatch import release_claims
    from discovery_service import DiscoveryService
    repository=DiscoveryRepository(context.database)
    if budget is None or not repository.enabled():return None
    collector=DiscoveryService(repository,service,budget,settings=budget.settings,
        fetch_membership=lambda playlist,identity:context.sources.fetch_playlist_items_page(
            playlist,video_id=identity,phase='discovery_membership'),
        worker_id=context.discovery_worker_id(),run_id=str(uuid4()),claim_fill=service.repository.claim_due_fill,
        enabled_types=tuple(context.policy.video_formats),require_ready=window.ready)
    return DiscoveryFeeder(collector,deadline=deadline,require_ready=window.ready,
        should_stop=window.pass_stopping,clock=window.clock,
        on_settled=window.operator_stopped,
        release_claims=partial(release_claims,context.database))


def run_discovery_portion(context, *, deadline, clock=time.monotonic):
    """Only signal sources, owned discovery IDs and manual work; no stats sweep."""
    from runtime.process_collection import bind_process_common_dispatch
    dispatch=bind_process_common_dispatch(context,whole_cycle=True,
                                           discovery_deadline=deadline,clock=clock)
    summary={'errors':0}
    try:
        summary=dispatch.narrow()
        dispatch.finish(summary)
        return summary
    except Exception:
        dispatch.finish(summary,failed=True)
        raise
    finally:dispatch.close()
