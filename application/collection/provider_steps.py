"""Finite provider steps; domain code resumes only on its owning thread."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
import time
from typing import Any, Callable
from pipeline_trace import capture, run as trace_run


@dataclass(frozen=True)
class ProviderResult:
    value: Any = None
    error: Exception | None = None
    elapsed: float = 0.0
    observed_at: datetime | None = None

    def unwrap(self):
        if self.error is not None:
            raise self.error
        return self.value


@dataclass(frozen=True)
class ProviderStep:
    fetch: Callable
    identities: tuple[str, ...]
    reason: str
    materialize: bool = False
    trace_context: Any = field(default_factory=capture, repr=False, compare=False)
    channel: str = 'collection'
    video_type: str = 'channel'
    exclusion_key: str | None = None

    def __post_init__(self):
        if (not callable(self.fetch) or not isinstance(self.identities, tuple)
                or not 1 <= len(self.identities) <= 50
                or any(not isinstance(value, str) or not value for value in self.identities)
                or len(set(self.identities)) != len(self.identities)
                or not isinstance(self.reason, str) or not self.reason
                or type(self.materialize) is not bool
                or not isinstance(self.channel,str) or not self.channel
                or self.video_type not in ('long','short','stream','channel')
                or (self.exclusion_key is not None and not isinstance(self.exclusion_key,str))):
            raise ValueError('provider_step_invalid')

    def execute(self):
        context = dict(self.trace_context)
        # Only the private observer validates/pseudonymizes these identities;
        # domain work and the existing provider request are unchanged.
        context.pop('video_ids', None)
        if all(re.fullmatch(r'[A-Za-z0-9_-]{11}', identity) for identity in self.identities):
            context['video_ids'] = self.identities
        if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', self.channel):
            context['channel_id'] = self.channel
        return trace_run(context, self._execute)

    def _execute(self):
        started = time.perf_counter()
        try:
            value = self.fetch(list(self.identities), self.reason)
            if self.materialize:
                value = list(value)
            return ProviderResult(value=value, elapsed=time.perf_counter() - started,
                                  observed_at=datetime.now(timezone.utc))
        except Exception as error:
            return ProviderResult(error=error, elapsed=time.perf_counter() - started,
                                  observed_at=datetime.now(timezone.utc))


def run_provider_steps(steps):
    """Synchronous compatibility driver for exactly the same domain iterator."""
    try:
        step = next(steps)
        while True:
            if not isinstance(step, ProviderStep):
                raise TypeError('provider_step_required')
            step = steps.send(step.execute())
    except StopIteration as finished:
        return finished.value
    finally:
        steps.close()
