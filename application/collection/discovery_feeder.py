"""Bounded discovery continuations in the existing owner-thread coordinator.

One batched iterator at a time; every HTTP step gives its credit back before
the next step competes with ordinary/manual work. Domain writes and settlement
stay on the owner thread; provider admission may read bounded control state.
"""
from collections import deque
from uuid import uuid4
import time

from collection.dispatch import Ack, Prepared, Skipped, Work, DuplicateWork, QueueFull
from collection.provider_steps import ProviderResult, ProviderStep
from pipeline_trace import event
import support_journal


class DiscoveryFeeder:
    quota_buckets=('general','stats')

    def __init__(self, service, *, deadline, require_ready, should_stop=lambda:False,
                 clock=time.monotonic, release_claims=lambda rows:None,on_settled=lambda result:None):
        self.service,self.deadline,self.require_ready=service,deadline,require_ready
        self.should_stop,self.clock,self.release_claims=should_stop,clock,release_claims
        self.on_settled=on_settled
        self.summary=dict(claimed=0,settled=0,discovered=0,deferred=0,errors=0,
                          provider_steps=0,hydration_claimed=0,units=0)
        self._order=deque(('websub','hydration','repair'))
        self._empty=set();self._steps=None;self._step=None;self._key=None
        self._origin=None;self._stopped=False;self._broken=False
        self._after=None;self._owner=None

    @property
    def exhausted(self):
        return self._steps is None and (self._stopped or len(self._empty)==3)

    def pause(self, reason):
        if not self._stopped:
            support_journal.event('worker', 'stopped', reason=reason)
        self._stopped=True
        self.summary['stopped_reason']=reason

    def resume_idle(self):
        # A wake may retry advisory EOF, never reopen a closed time allowance.
        if not self._stopped and self.clock()<self.deadline:
            self._empty.clear();self._after=None

    def _halted(self):
        return self._stopped or self.should_stop() or self.clock()>=self.deadline

    def _advance(self, response=None):
        try:
            self._step=next(self._steps) if response is None else self._steps.send(response)
        except StopIteration as done:
            result=done.value or {}
            self.summary['units']+=1
            if self._origin=='hydration':
                self.summary['hydration_claimed']+=result.get('claimed',0)
                self._after=result.get('next_after')
                if not self._after:self._empty.add('hydration')
            else:
                for name in ('claimed','settled','discovered','deferred'):
                    self.summary[name]+=result.get(name,0)
                if not result.get('claimed'):self._empty.add(self._origin)
            event('discovery_unit','settled',requested_count=result.get('claimed',0),
                  received_count=result.get('settled',0),reason=self._origin)
            self._steps=self._step=None
            return
        if not isinstance(self._step,ProviderStep):
            raise TypeError('discovery_provider_step_required')

    def _start(self):
        # Three source kinds is a finite no-provider probe, not a busy polling
        # loop. New WebSub first, then targeted initial debt, then old repair.
        for _ in range(3):
            origin=self._order[0];self._order.rotate(-1)
            if origin in self._empty:continue
            self.require_ready()
            self._origin=origin
            self.service.active_claims=()
            self._steps=(self.service.hydration_steps(after=self._after,limit=50)
                         if origin=='hydration' else self.service.steps(origin=origin,limit=50))
            self._advance()
            if self._steps is not None:return

    def offer_to(self, coordinator, *, limit):
        if self._owner is not None and self._owner is not coordinator:
            raise RuntimeError('discovery_coordinator_changed')
        self._owner=coordinator
        if limit<=0 or self._key is not None or self._broken:return
        if self._halted():
            self.pause('scheduled_handoff');self._close();return
        if self._steps is None:self._start()
        if self._steps is None:return
        step=self._step
        key='discovery:'+str(uuid4())
        self._key=key

        def prepare():
            if self._halted():
                self.pause('scheduled_handoff');self._close();self._key=None
                return Skipped()
            self.require_ready()
            return Prepared(lambda _cancel:step.execute(),self.deadline,len(step.identities))

        def settle(completion):
            if completion.phase in ('cancelled','expired') and not completion.fetch_started:
                self.pause('scheduled_handoff');self._close()
            else:
                if completion.phase=='response' and isinstance(completion.value,ProviderResult):
                    response=completion.value
                elif completion.phase=='fetch_error' and isinstance(completion.error,Exception):
                    response=ProviderResult(error=completion.error)
                else:
                    self._broken=True
                    raise RuntimeError('discovery_dispatch_outcome_unknown')
                try:
                    self.summary['provider_steps']+=int(completion.fetch_started)
                    self._advance(response)
                    from youtube_errors import youtube_rate_limit_retry_after
                    if response.error is not None and youtube_rate_limit_retry_after(response.error) is not None:
                        self.summary['rate_limited']=True
                        self.pause('provider_rate_limited')
                    elif getattr(response.error,'reason',None)=='quota_state_unavailable':
                        self.summary['quota_control_unknown']=True
                        self.summary['errors']+=1
                        self.pause('quota_state_unavailable')
                    self.on_settled(dict(self.summary))
                except Exception:
                    self._broken=True
                    support_journal.rows('writer', 'unknown', self.service.active_claims)
                    self.summary.update(errors=self.summary['errors']+1,settlement_unknown=True)
                    raise
            self._key=None
            return Ack()

        try:
            coordinator.offer(Work(key,'auto',step.channel,step.video_type,prepare,settle,
                                   exclusion_key=step.exclusion_key))
        except (DuplicateWork,QueueFull):
            self._key=None  # Retain the same prepared request, never debit again.

    def _close(self):
        if self._broken:return  # Leave uncertain owners fenced for reconciliation.
        if self._steps is not None:self._steps.close()
        self._steps=self._step=None
        self.release_claims(self.service.active_claims)
        self.service.active_claims=()

    def finish_pass(self, coordinator):
        if self._key is not None and self._key in coordinator.keys():
            raise RuntimeError('discovery_finish_before_drain')
        if self._halted():self._close()

    def close(self):
        self.pause('phase_complete');self._close()
