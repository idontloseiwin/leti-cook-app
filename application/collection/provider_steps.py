"""Finite provider steps; domain code resumes only on its owning thread."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    def __post_init__(self):
        if (not callable(self.fetch) or not isinstance(self.identities, tuple)
                or not 1 <= len(self.identities) <= 50
                or any(not isinstance(value, str) or not value for value in self.identities)
                or len(set(self.identities)) != len(self.identities)
                or not isinstance(self.reason, str) or not self.reason
                or type(self.materialize) is not bool):
            raise ValueError('provider_step_invalid')

    def execute(self):
        return trace_run(self.trace_context, self._execute)

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
