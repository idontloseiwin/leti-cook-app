"""Bound the regular-video producer to the shared coordinator's free capacity."""
from dataclasses import replace
from datetime import datetime, timezone
from threading import get_ident
from pipeline_trace import event

from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.common_video_dispatch import CommonBatchUnit
from collection.dispatch import Coordinator, Skipped
from video_collection_service import _common_empty_summary, _absorb_common_summary


class CommonAutoFeeder:
    """One preflighted pass; no executor, lease owner or private ready queue.

    Queue preparation/preflight happens once in the outer automatic phase.
    Candidate reads and all callbacks below run on that phase's owner thread.
    Only CommonBatchUnit.fetch executes in the coordinator's provider pool.
    """
    def __init__(self, service, *, state, cursor, worker_id, require_ready, lease_seconds=900,
                 reason='auto_video_collection', should_stop=lambda: False, on_settled=None):
        if (not isinstance(state, AutoPassState) or not isinstance(cursor, CommonCandidateCursor)
                or not isinstance(worker_id,str)
                or not worker_id.strip() or not callable(require_ready) or not callable(should_stop)
                or (on_settled is not None and not callable(on_settled))
                or type(lease_seconds) is not int or not 30<=lease_seconds<=3600
                or not callable(getattr(service.repository,'due_candidate_page',None))
                or not callable(getattr(service.repository,'claim_due_batch',None))):
            raise ValueError('common_feeder_config_invalid')
        self.service, self.state = service, state
        self.worker_id, self.require_ready = worker_id, require_ready
        self.lease_seconds, self.reason = lease_seconds, reason
        self.should_stop, self.on_settled = should_stop, on_settled
        self.summary = _common_empty_summary(reason)
        self.summary['started_at'] = datetime.now(timezone.utc).isoformat()
        self.summary['scan_limit'] = state.scan_limit
        self.pass_finished = False
        self.pass_stop_reason = None
        self._owner = get_ident()
        self._sequence = 0
        self._coordinator = None
        self._offered = set()
        # At most 50 advisory scopes for one not-yet-offered descriptor.
        # This holds no leases, provider work or future-due videos.
        self._pending_scopes = []
        self._pending_operation = None
        self._pending_ready_count = 0
        self._pending_wrapped = False
        self._closed = False
        self.cursor = cursor
        state.bind_candidate_cursor(cursor)

    @property
    def exhausted(self):
        """Compatibility spelling for per-pass finish, not physical sweep EOF."""
        return self.pass_finished

    def pause(self, reason):
        self.state.pause(reason)
        self.summary['stopped_reason'] = reason
        event("common_feeder", "paused", reason=reason)

    def _prune(self, coordinator):
        self._offered = {key for key in self._offered if coordinator.contains(key)}

    def _finish_budget_if_drained(self):
        # Own acknowledged accounting can finish even when unrelated operator
        # descriptors fill the shared ready queue. No candidate read is needed.
        if self._offered or self.state.held or self.state.paused:
            return False
        reason = ('stats_limit' if not self.state.remaining else
            'raw_scan_budget' if self.state.inspected >= self.state.scan_limit else
            'candidate_scan_budget' if self.state.candidate_inspected >= self.state.candidate_scan_limit else None)
        if reason is None or (reason == 'candidate_scan_budget' and self._pending_scopes):
            return False
        self.pass_finished, self.pass_stop_reason = True, reason
        return True

    def finish_pass(self, coordinator):
        """Release positions for the next pass only after known descriptor drain."""
        if get_ident() != self._owner:
            raise RuntimeError('common_feeder_owner_thread_required')
        if self._closed or coordinator is not self._coordinator:
            raise RuntimeError('common_feeder_pass_not_active')
        self._prune(coordinator)
        self._finish_budget_if_drained()
        if (self._offered or self.state.held or coordinator.snapshot().blocked
                or not (self.pass_finished or self.state.paused)):
            raise RuntimeError('common_feeder_pass_release_unconfirmed')
        self.cursor._release(self.state)
        self._pending_scopes.clear()
        self._closed = True
        event("common_pass", "finished", operation_id=self.state.run_id, reason=self.pass_stop_reason or self.state.stop_reason,
              requested_count=self.state.claimed, received_count=self.summary.get("updated", 0))

    def _sync_counters(self):
        self.summary.update(claimed=self.state.claimed, inspected=self.state.inspected,
                            paused=self.state.paused_rows)

    def _work(self, scopes, operation, selection_reason):
        # The leading scope is a scheduling key, never the owner of other rows.
        scope = scopes[0]
        allowed = {(item["channel_id"], item["video_type"]) for item in scopes}
        self._sequence += 1
        key = f'auto:{self.state.run_id}:{self._sequence}'
        def claim():
            options = self.state.begin_claim(key)
            if options is None:
                raise RuntimeError('common_feeder_claim_without_budget')
            try:
                batch = self.service.repository.claim_due_batch(**options,
                    worker_id=self.worker_id,lease_seconds=self.lease_seconds,scopes=scopes,operation=operation)
                if len(batch.get('rows', ())) < options['limit']:
                    batch['selection_reason'] = selection_reason
                event("common_claim", "confirmed", operation_id=key, reason=selection_reason,
                      phase=operation, requested_count=options["limit"], received_count=len(batch.get("rows", ())))
                return batch
            except Exception:
                self.state.claim_failed(key)
                raise
        def claimed(batch):
            rows = batch.get('rows',())
            if any((row.channel_id, row.video_type) not in allowed for row in rows):
                self.state.claim_failed(key)
                raise RuntimeError('common_feeder_claim_scope_mismatch')
            self.state.on_claimed(key,batch)
            # A scope may still contain the other provider operation. The
            # claim reports nonexhausted and its acknowledged settlement keeps
            # the existing fair-sweep revisit; advancing here is not queue EOF.
            for item in scopes:
                self.state.candidate_claimed(item, operation=operation)
            self._sync_counters()
        def settled(summary):
            self.state.on_settled(key,summary)
            _absorb_common_summary(self.summary,summary)
            self._sync_counters()
            if self.on_settled is not None:
                self.on_settled(summary)
        unit = CommonBatchUnit(self.service,key=key,lane='auto',channel=scope['channel_id'],
            video_type='channel' if scope['video_type']=='unknown' else scope['video_type'],
            claim_batch=claim,require_ready=self.require_ready,on_claimed=claimed,
            on_settled=settled,lease_seconds=self.lease_seconds,reason=self.reason,
            should_stop=lambda: self.state.paused or self.should_stop())
        def prepare():
            if (self.state.paused or self.should_stop() or not self.state.remaining
                    or self.state.inspected>=self.state.scan_limit):
                event("common_claim", "skipped", operation_id=key, reason="pass_stopped_or_budget_exhausted")
                return Skipped()
            return unit.prepare()
        return replace(unit.work,prepare=prepare)

    def offer_to(self, coordinator, *, limit=None):
        """Read at most four pages per turn; assemble one bounded scope window."""
        if limit is not None and (type(limit) is not int or not 0 <= limit <= 100):
            raise ValueError('common_feeder_offer_limit_invalid')
        if get_ident()!=self._owner:
            raise RuntimeError('common_feeder_owner_thread_required')
        if self._closed:
            raise RuntimeError('common_feeder_pass_closed')
        if (not isinstance(coordinator,Coordinator) or coordinator.concurrency!=self.state.concurrency
                or (self._coordinator is not None and self._coordinator is not coordinator)):
            raise ValueError('common_feeder_coordinator_mismatch')
        self._coordinator=coordinator
        if limit == 0:
            return 0
        self._prune(coordinator)
        current=coordinator.snapshot()
        capacity=2*coordinator.concurrency-current.ready
        if limit is not None:
            capacity=min(capacity,limit)
        if current.blocked or current.paused or current.stopped:
            return 0
        if self._finish_budget_if_drained() or not capacity:
            return 0
        scopes = self._pending_scopes
        flush = False
        selection_reason = None
        # Both advisory head peeks and filtered physical pages count toward
        # the same four-read turn limit and the pass's candidate-scan budget.
        try:
            for _ in range(4):
                allowance = min(50, self.state.remaining)
                if self._pending_operation is None:
                    request = self.state.seed_request(capacity)
                    if request is None:
                        break
                    page = self.service.repository.due_candidate_page(**request)
                    self._pending_operation = self.state.accept_seed_page(page)
                    continue
                if self._pending_ready_count >= allowance or len(scopes) >= allowance:
                    flush, selection_reason = True, 'ready_limit'
                    break
                operation = self._pending_operation
                if self.state.operation_window_exhausted(operation):
                    if scopes and not self._pending_wrapped:
                        if not self.state.wrap_candidate_window(operation, scopes=scopes):
                            flush, selection_reason = True, 'frontier_busy'
                            break
                        self._pending_wrapped = True
                    elif scopes:
                        flush, selection_reason = True, 'operation_eof'
                        break
                    else:
                        self._pending_operation = None
                        self._pending_wrapped = False
                        continue
                request = self.state.candidate_request(capacity,
                    scope_limit=allowance-len(scopes),
                    extend_types={item['video_type'] for item in scopes}, operation=operation)
                if request is None:
                    flush, selection_reason = bool(scopes), 'candidate_scan_budget' if (
                        self.state.candidate_inspected >= self.state.candidate_scan_limit) else 'frontier_busy'
                    break
                page = self.service.repository.due_candidate_page(**request)
                rows = self.state.accept_candidate_page(page)
                scopes.extend(dict(channel_id=row['channel_id'], video_type=row['video_type']) for row in rows)
                self._pending_ready_count += sum(row['ready_count'] for row in rows)
        except Exception:
            self.state.pause('candidate_read_failed')
            raise
        self.summary['candidate_inspected'] = self.state.candidate_inspected
        if self.state.paused or not self.state.remaining or self.state.inspected >= self.state.scan_limit:
            scopes.clear()  # Unclaimed candidates do not advance the checkpoint.
        if not scopes:
            self.pass_finished = (not self.state.held and not self._offered and (
                self.state.paused or not self.state.remaining
                or self.state.inspected >= self.state.scan_limit
                or self.state.candidate_inspected >= self.state.candidate_scan_limit
                or (self.cursor.sweep_exhausted and not self.cursor._revisit
                    and self.state.claimed == self.state._sweep_claimed)))
            self.pass_stop_reason = (self.state.stop_reason if self.state.paused else
                'stats_limit' if not self.state.remaining else
                'raw_scan_budget' if self.state.inspected >= self.state.scan_limit else
                'candidate_scan_budget' if self.state.candidate_inspected >= self.state.candidate_scan_limit else
                'sweep_exhausted' if self.cursor.sweep_exhausted else None)
            return 0
        self.pass_finished = False
        self.pass_stop_reason = None
        if self._pending_ready_count >= min(50, self.state.remaining) or len(scopes) >= min(50, self.state.remaining):
            flush, selection_reason = True, 'ready_limit'
        elif self.state.candidate_inspected >= self.state.candidate_scan_limit:
            flush, selection_reason = True, 'candidate_scan_budget'
        if not flush:
            return 0
        try:
            work = self._work(tuple(scopes), self._pending_operation, selection_reason)
            coordinator.offer(work)
            self._offered.add(work.key)
            scopes.clear()
            self._pending_operation = None
            self._pending_ready_count = 0
            self._pending_wrapped = False
        except Exception:
            self.state.pause('candidate_offer_failed')
            raise
        if len(self._offered) > 3 * coordinator.concurrency:
            raise AssertionError('common_feeder_descriptor_bound')
        return 1
