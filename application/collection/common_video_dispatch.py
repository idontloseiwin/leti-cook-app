"""Opt-in regular video unit; provider steps never run domain/DB callbacks."""
import time
from datetime import datetime, timezone

from collection.dispatch import Ack, Prepared, Skipped, Work
from collection.initial_video_hydration import _drain_unsent
from collection.provider_steps import ProviderResult, ProviderStep
from collection_shutdown import handoff_requested
from collection_outcome import collection_outcome_fields
from hot_runtime import HotRuntimeUnavailable


class CommonBatchUnit:
    """Keep one credit across metadata, validation, counters and settlement.

    claim_batch must use the existing video queue owner and return its bounded
    row envelope. It runs only in prepare, never when a descriptor is offered.
    The pass owns scan/attempt budgets through on_claimed/on_settled callbacks.
    Unknown claim/commit/progress keeps the credit; no callback is replayed.
    """
    def __init__(self, service, *, key, lane, channel, video_type, claim_batch,
                 require_ready, on_claimed, on_settled, lease_seconds=900,
                 reason='auto_video_collection', clock=time.monotonic, should_stop=lambda: False):
        if (any(not callable(value) for value in (claim_batch, require_ready,
                on_claimed, on_settled, clock, should_stop))
                or type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600):
            raise ValueError('common_dispatch_config_invalid')
        self.service, self.claim_batch = service, claim_batch
        self.require_ready, self.on_claimed, self.on_settled = require_ready, on_claimed, on_settled
        self.lease_seconds, self.reason, self.clock = lease_seconds, reason, clock
        self.should_stop = should_stop
        self.phase = 'candidate'
        self.claims, self.summary, self.claim_info = (), {}, None
        self.steps, self.current_step, self.deadline = None, None, None
        self.work = Work(key, lane, channel, video_type, self.prepare, self.settle)

    def prepare(self):
        if self.phase != 'candidate':
            raise RuntimeError('common_dispatch_prepare_repeated')
        if handoff_requested() or self.should_stop():
            self.phase = 'skipped'
            return Skipped()
        self.require_ready()
        self.phase = 'claiming'
        self.deadline = self.clock() + self.lease_seconds - 1
        self.claim_info = self.claim_batch()
        if not isinstance(self.claim_info, dict) or not isinstance(self.claim_info.get('rows'), (list, tuple)):
            raise ValueError('common_dispatch_claim_invalid')
        self.claims = tuple(self.claim_info['rows'])
        identities = [claim.video_id for claim in self.claims]
        if (len(self.claims) > 50 or len(set(identities)) != len(identities)
                or any(not value for value in identities)):
            raise ValueError('common_dispatch_claim_invalid')
        self.on_claimed(self.claim_info)
        if not self.claims:
            self.phase = 'skipped'
            return Skipped()
        self.steps = self.service.common_claim_steps(self.claims, reason=self.reason, summary=self.summary)
        result = self._advance()
        return Skipped() if isinstance(result, Ack) else result

    def _advance(self, response=None):
        self.phase = 'settling'
        try:
            self.current_step = next(self.steps) if response is None else self.steps.send(response)
        except StopIteration as finished:
            self.summary = self.service.finish_common_claims(self.claims, finished.value)
            if self.summary.get('settlement_unknown'):
                self.phase = 'settlement_unknown'
                raise RuntimeError('common_dispatch_settlement_unknown')
            self.phase = 'progress'
            self.on_settled(self.summary)
            self.phase = 'acknowledged'
            return Ack()
        if not isinstance(self.current_step, ProviderStep):
            raise TypeError('common_dispatch_provider_step_required')
        if self.should_stop():
            return self._drain('collection_stopped')
        try:
            self.require_ready()
        except HotRuntimeUnavailable:
            # This callback performs admission only. Prior claim/writes are
            # acknowledged; an unknown write already stops the iterator.
            return self._drain('hot_runtime_interrupted')
        self.phase = 'prepared'
        return Prepared(self.fetch, self.deadline, len(self.current_step.identities))

    def _drain(self, reason):
        self.phase = 'releasing'
        _drain_unsent(self.service.repository, self.claims)
        self.steps.close()
        self.summary.update(yield_requested=True, stopped_reason=reason,
                            finished_at=datetime.now(timezone.utc).isoformat())
        if reason == 'hot_runtime_interrupted':
            self.summary.update(hot_runtime_interrupted=True, infrastructure_error=True)
        self.summary.update(collection_outcome_fields(self.summary))
        self.phase = 'progress'
        self.on_settled(self.summary)
        self.phase = 'released'
        return Ack()

    def fetch(self, cancelled):
        # The Coordinator handles cancellation/deadline before this callback.
        # This callback contains no generator resume, claim or repository call.
        return self.current_step.execute()

    def settle(self, completion):
        if self.phase != 'prepared':
            raise RuntimeError('common_dispatch_settle_repeated')
        if completion.phase in ('cancelled', 'expired') and not completion.fetch_started:
            # Completion receipts fence already acknowledged rows. Only leases
            # still held by these original tokens can be aborted by this owner.
            return self._drain('scheduled_handoff' if handoff_requested() else completion.phase)
        if completion.phase == 'fetch_error' and isinstance(completion.error, Exception):
            response = ProviderResult(error=completion.error)
        elif completion.phase == 'response' and isinstance(completion.value, ProviderResult):
            response = completion.value
        else:
            raise RuntimeError('common_dispatch_completion_unknown')
        return self._advance(response)
