"""Explicit per-request discovery admission; no process-global switch."""
from uuid import uuid4

from youtube_errors import YouTubeDailyQuotaExceededError


class DiscoveryAdmissionDenied(RuntimeError):
    lic_http_attempted = False

    def __init__(self, decision):
        self.reason = decision['reason']
        self.next_try_at = decision.get('next_try_at')
        super().__init__('discovery_admission_denied')


class DiscoveryAdmissionUnsafe(RuntimeError):
    """The durable admission/ban outcome is unknown; no further request is safe."""

    def __init__(self, message, *, http_attempted=False):
        self.lic_http_attempted = http_attempted
        super().__init__(message)


class DiscoveryAdmission:
    def __init__(self, budget, *, campaign_id=None):
        self.budget, self.campaign_id = budget, campaign_id
        self.last_denial = None
        self.failure = None
        self.observed_at = None

    def require_safe(self):
        if self.failure is not None:
            raise self.failure

    def _unsafe(self, error, *, http_attempted=False):
        self.failure = DiscoveryAdmissionUnsafe('discovery_admission_outcome_unknown',http_attempted=http_attempted)
        raise self.failure from error

    def prepare(self, bucket, *, event=True, full=False, before_dispatch=None):
        """Owner-thread debit and exact binding, before one provider step."""
        self.require_safe()
        token = str(uuid4())
        try:
            decision = self.budget.reserve(token, bucket=bucket, event=event,
                                           full=full, campaign_id=self.campaign_id)
            allowed = decision['allowed'] and self.budget.begin_dispatch(token)
        except Exception as error:
            self._unsafe(error)
        if not allowed:
            self.last_denial = decision if not decision['allowed'] else dict(reason='grant_not_dispatchable')
            raise DiscoveryAdmissionDenied(self.last_denial)
        if before_dispatch is not None:
            try:
                before_dispatch()
            except Exception as error:
                self._unsafe(error)
        return token
    def observe_error(self, bucket, error):
        if isinstance(error,DiscoveryAdmissionDenied):
            self.last_denial=dict(reason=error.reason,next_try_at=error.next_try_at)
        if isinstance(error, YouTubeDailyQuotaExceededError):
            # This write is safety-critical, before optional telemetry. Failure
            # propagates and stops the unit; it never opens another request.
            try:
                import quota_buckets
                reset=None if quota_buckets.control_active() else self.budget.note_exhausted(bucket)
                self.last_denial=dict(reason='provider_quota',next_try_at=reset)
            except Exception as error:
                self._unsafe(error,http_attempted=True)
        raise error

    def call(self, bucket, callback, *args, event=True, full=False,
             before_dispatch=None, **kwargs):
        """Synchronous compatibility path with the same admission owner."""
        token=self.prepare(bucket,event=event,full=full,before_dispatch=before_dispatch)
        try:
            import quota_buckets
            with quota_buckets.discovery_grant(self.budget,token):
                return callback(*args, **kwargs)
        except Exception as error:
            self.observe_error(bucket,error)

    def steps(self, bucket, callback, *args, identities, reason,
              before_dispatch=None, descriptor=None, **kwargs):
        """Only HTTP executes in the pool; admission and refusal persist here."""
        from collection.provider_steps import ProviderStep
        token=self.prepare(bucket,before_dispatch=before_dispatch)
        def fetch(_ids,_reason):
            import quota_buckets
            with quota_buckets.discovery_grant(self.budget,token):
                return callback(*args,**kwargs)
        response = yield ProviderStep(fetch,
                                      tuple(identities),reason,**(descriptor or {}))
        self.observed_at=response.observed_at
        if response.error is not None:
            self.observe_error(bucket,response.error)
        return response.unwrap()

    def wrap(self, bucket, callback):
        def admitted(*args, **kwargs):
            return self.call(bucket, callback, *args, **kwargs)
        return admitted
