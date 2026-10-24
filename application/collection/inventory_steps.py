"""Immutable one-page provider messages; no database or shared budget access."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from pipeline_trace import run as trace_run, scope


@dataclass(frozen=True)
class InventoryPageResponse:
    value: Any = None
    error: BaseException | None = None
    source_checked_at: datetime | None = None
    attempted: bool = True

    def unwrap(self):
        if self.error is not None:
            raise self.error
        return self.value


@dataclass(frozen=True)
class InventoryPageStep:
    fetch: Callable
    playlist_id: str
    page_token: str | None
    phase: str
    now: Callable
    before_fetch: datetime
    trace_context: Any = None

    def execute(self):
        return trace_run(self.trace_context, self._observed_execute)

    def _observed_execute(self):
        with scope("inventory_provider", phase=self.phase) as trace:
            result = self._execute()
            trace.finish("error" if result.error is not None else "received",
                         observed_at=result.source_checked_at)
            return result

    def _execute(self):
        # No scanner/domain callbacks here. The prepared source clock is a
        # conservative fallback if the post-response clock callback fails.
        value, error = None, None
        try:
            value = self.fetch(self.playlist_id, page_token=self.page_token,
                               max_results=50, phase=self.phase)
        except BaseException as caught:
            error = caught
        try:
            checked_at = self.now()
        except Exception:
            checked_at = self.before_fetch
        return InventoryPageResponse(value=value, error=error, source_checked_at=checked_at)


@dataclass(frozen=True)
class PreparedInventoryPage:
    claim: Any
    identity: dict
    scan_id: str
    expected_page_token: str | None
    scan_boundary_at: datetime
    step: InventoryPageStep
    playlist_not_found_errors: tuple
    require_observed_video_ids: bool