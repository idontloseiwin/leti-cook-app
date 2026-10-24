"""Opt-in pending reconnect adapter; no scheduler, SQL or runtime composition.

The injected resolver must do provider reads only, enforce the supplied deadline
and bound response bytes. The existing HTTP prepare_only resolver is unsuitable:
it also reads repositories. Readiness/source guards run on the coordinator.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time

from channel_reconnect_outcome import ReconnectResponse, _BLOCK
from collection_policy import next_pacific_midnight
from collection_shutdown import handoff_requested
from saved_work import channel_id, revision
from provider_admission import AdmissionUnavailable
from hot_runtime import HotDatabaseAdmissionDenied, HotRuntimeUnavailable
from user_work_backup import uid
from youtube_errors import YouTubeAPIError, YouTubeDailyQuotaExceededError, YouTubeRateLimitError
from .dispatch import Ack, Prepared, Skipped, Work



class ReconnectUnit:
    """A candidate holds no lease until exact prepare; uncertain writes never repeat.

    Ack after lease_lost means this token owns no work, not channel completion.
    The caller must reconcile authoritative state after an unknown ACK; deleting
    a pending row alone does not prove a particular bind/merge result.
    """
    def __init__(self, repository, *, identity, resolve_channel,
                 require_ready, require_source, lease_seconds=180, max_attempts=3,
                 clock=time.monotonic, utc_clock=lambda: datetime.now(timezone.utc),
                 on_settled=None, should_stop=None):
        self.identity = uid(identity)
        if (type(lease_seconds) is not int or not 30 <= lease_seconds <= 600
                or type(max_attempts) is not int or not 1 <= max_attempts <= 20
                or not all(callable(value) for value in (resolve_channel, require_ready,
                    require_source, clock, utc_clock,
                    getattr(repository, 'claim_reconnect', None),
                    getattr(repository, 'defer_reconnect', None),
                    getattr(repository, 'bind_reconnect', None)))):
            raise ValueError('reconnect_unit_config_invalid')
        if ((on_settled is not None and not callable(on_settled))
                or (should_stop is not None and not callable(should_stop))):
            raise ValueError('reconnect_unit_config_invalid')
        self.repository = repository
        self.resolve_channel, self.require_ready, self.require_source = resolve_channel, require_ready, require_source
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.clock, self.utc_clock = clock, utc_clock
        self.phase, self.claim, self.result, self.deadline = 'candidate', None, None, None
        self._fetch_started = False
        self._provider_code = None
        self.on_settled = on_settled
        self.should_stop = should_stop or (lambda: False)
        key = 'reconnect:' + self.identity
        self.work = Work(key=key, lane='operator', channel=self.identity, format='channel',
                         exclusion_key=key, prepare=self.prepare, settle=self.settle)

    def prepare(self):
        if self.phase != 'candidate':
            raise RuntimeError('reconnect_prepare_not_repeatable')
        self.phase = 'checking'
        if handoff_requested() or self.should_stop():
            self.phase = 'skipped'
            return Skipped()
        self.require_ready()
        started = self.clock()
        if type(started) not in (int, float) or not math.isfinite(started) or started < 0:
            raise ValueError('reconnect_clock_invalid')
        self.phase = 'claiming'
        claims = self.repository.claim_reconnect(identity=self.identity, limit=1,
                                                  lease_seconds=self.lease_seconds)
        if not claims:
            self.phase = 'skipped'
            return Skipped()
        if len(claims) != 1 or not isinstance(claims[0], dict) or claims[0].get('id') != self.identity:
            raise RuntimeError('reconnect_claim_invalid')
        self.claim = dict(claims[0])
        revision(self.claim.get('revision'))
        uid(self.claim.get('lease_token'))
        locator = self.claim.get('user_input')
        if (not isinstance(locator, str) or not 1 <= len(locator) <= 2048
                or any(ord(char) < 32 or ord(char) == 127 for char in locator)):
            raise RuntimeError('reconnect_claim_invalid')
        self.deadline = started + self.lease_seconds - 1
        self.phase = 'prepared'
        return Prepared(self.fetch, deadline=self.deadline)

    def fetch(self, cancelled):
        if self.phase != 'prepared' or self._fetch_started:
            raise RuntimeError('reconnect_fetch_not_repeatable')
        self._fetch_started = True
        if cancelled.is_set() or handoff_requested() or self.should_stop() or self.clock() >= self.deadline:
            return ReconnectResponse(dispatched=False)
        return self.resolve_channel(self.claim['user_input'], cancelled=cancelled, deadline=self.deadline)

    def _ack(self, reason=None):
        self.phase = 'acknowledged'
        if self.on_settled is not None:
            self.on_settled(dict(outcome=self.result['outcome'], reason=reason,
                quota_exhausted=self._provider_code == 'provider_quota_exceeded',
                rate_limited=self._provider_code == 'provider_rate_limited'))
        return Ack()

    def _defer(self, reason, retry_seconds=None):
        self.phase = 'deferring'
        try:
            self.result = self.repository.defer_reconnect(self.identity, self.claim['revision'],
                self.claim['lease_token'], reason=reason, retry_seconds=retry_seconds)
        except HotDatabaseAdmissionDenied as error:
            if getattr(error, 'outcome_may_be_committed', False):
                raise
            return self._abort()
        if self.result.get('outcome') not in ('ready', 'lease_lost'):
            raise RuntimeError('reconnect_defer_unconfirmed')
        return self._ack(reason)

    def _abort(self):
        # Called only for definite admission refusal after an acknowledged
        # claim. Unknown claim/bind/defer results never enter this branch.
        self.phase = 'aborting'
        self.result = self.repository.abort_reconnect(
            self.identity, self.claim['revision'], self.claim['lease_token'])
        if type(self.result) is not dict or set(self.result) != {'outcome'} or self.result['outcome'] not in ('ready', 'lease_lost'):
            raise RuntimeError('reconnect_abort_unconfirmed')
        return self._ack('hot_runtime_interrupted')

    def _daily_delay(self):
        now = self.utc_clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('reconnect_utc_clock_invalid')
        now = now.astimezone(timezone.utc)
        seconds = math.ceil((next_pacific_midnight(now) - now).total_seconds())
        # A Pacific DST fallback day is 25h. No local quota estimate is consulted.
        if not 1 <= seconds <= 90000:
            raise ValueError('reconnect_quota_reset_invalid')
        return seconds

    def settle(self, completion):
        if self.phase != 'prepared':
            raise RuntimeError('reconnect_settle_not_repeatable')
        self.phase = 'settling'
        if completion.phase in ('cancelled', 'expired'):
            response = ReconnectResponse(dispatched=False)
        elif completion.phase == 'response' and isinstance(completion.value, ReconnectResponse):
            response = completion.value
        elif completion.phase == 'fetch_error':
            if isinstance(completion.error, YouTubeDailyQuotaExceededError):
                response = ReconnectResponse(code='provider_quota_exceeded')
            elif isinstance(completion.error, YouTubeRateLimitError):
                response = ReconnectResponse(code='provider_rate_limited',
                                             retry_seconds=completion.error.retry_after_seconds)
            elif isinstance(completion.error, AdmissionUnavailable):
                # The resolver only reads provider data and never writes domain
                # state. Admission can fail before HTTP or while observing its
                # response: keep this an attempted failure, never claim unsent.
                # The original token is released only after durable deferral;
                # an unknown database acknowledgement still blocks below.
                response = ReconnectResponse(code='provider_unavailable', retry_seconds=60)
            elif isinstance(completion.error, (YouTubeAPIError, TimeoutError, ConnectionError)):
                response = ReconnectResponse(code='provider_unavailable')
            else:
                raise RuntimeError('reconnect_fetch_requires_reconciliation') from completion.error
        else:
            raise RuntimeError('reconnect_completion_invalid')
        self._provider_code = response.code if response.dispatched else None
        if not response.dispatched:
            return self._defer('busy', 1)
        try:
            self.require_ready()
        except HotRuntimeUnavailable:
            return self._abort()
        if response.code:
            delay = (self._daily_delay() if response.code == 'provider_quota_exceeded'
                     else None if response.code in _BLOCK else response.retry_seconds or 60)
            return self._defer(response.code, delay)
        self.require_source(response.channel)
        self.phase = 'binding'
        self.result = self.repository.bind_reconnect(self.identity, self.claim['revision'],
            self.claim['lease_token'], response.channel, max_attempts=self.max_attempts)
        outcome = self.result.get('outcome')
        if outcome in ('ready', 'lease_lost'):
            return self._ack()
        if outcome in ('busy', 'conflict', 'parent_pending', 'channel_unavailable'):
            reason = {'conflict': 'busy', 'channel_unavailable': 'channel_lookup_unavailable'}.get(outcome, outcome)
            return self._defer(reason, 60)
        if outcome == 'needs_input':
            return self._defer('needs_input')
        raise RuntimeError('reconnect_bind_unconfirmed')
