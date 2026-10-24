"""Resource-bounded full catalogue reads; no database queries or persisted state."""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from typing import Any


# Sized for the largest list YouTube returns, 20,000 long videos: such a full
# catalogue accounts about 52 MiB here (tests/test_large_catalogue.py).
DEFAULT_CATALOGUE_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_CATALOGUE_TIMEOUT_SECONDS = 30
CATALOGUE_READ_BATCH_SIZE = 500
CATALOGUE_VIDEO_TYPES = frozenset({"long", "short", "stream"})


class CatalogueScopeError(RuntimeError):
    """A full read could not establish its complete scope within its budget."""


class CatalogueReadBudget:
    """Conservative retained-value accounting, not a process RSS measurement."""

    def __init__(
        self,
        max_bytes: int = DEFAULT_CATALOGUE_MAX_BYTES,
        timeout_seconds: float = DEFAULT_CATALOGUE_TIMEOUT_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("catalogue max_bytes must be positive")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("catalogue timeout_seconds must be positive")
        self.max_bytes = max_bytes
        self.retained_bytes = 0
        self._clock = clock or time.monotonic
        self._deadline = self._clock() + timeout_seconds

    def check(self) -> None:
        if self._clock() >= self._deadline:
            raise CatalogueScopeError("catalogue_read_deadline_exceeded")

    def account(self, value: Any) -> None:
        self.check()
        pending = [value]
        seen: set[int] = set()
        while pending:
            item = pending.pop()
            identity = id(item)
            if identity in seen:
                continue
            seen.add(identity)
            self.retained_bytes += sys.getsizeof(item)
            if self.retained_bytes > self.max_bytes:
                raise CatalogueScopeError("catalogue_read_byte_budget_exceeded")
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple, set, frozenset)):
                pending.extend(item)
            if len(seen) % 128 == 0:
                self.check()
        self.check()

    def read(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.check()
        result = method(*args, **kwargs)
        self.account(result)
        return result


class CatalogueScopeRepository:
    """Budget wrapper around the caller's repository, including recording fakes."""

    def __init__(self, repository: Any, budget: CatalogueReadBudget | None = None) -> None:
        self.repository = repository
        self.budget = budget if budget is not None else CatalogueReadBudget()

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.repository, name)
        if name.startswith("load_") and callable(method):
            return lambda *args, **kwargs: self.budget.read(method, *args, **kwargs)
        return method

    def load_full_inventory(
        self, channel_id: str, *, video_type: str = "long",
    ) -> list[dict[str, Any]]:
        if not isinstance(video_type, str) or video_type not in CATALOGUE_VIDEO_TYPES:
            raise ValueError("unsupported video type")
        self.budget.check()
        # Preserve explicitly long-only wrappers and recording fakes. Typed
        # requests must use the typed iterator; never fall back to a Long list.
        iterator = iter(self.repository.iter_long_inventory_pages(channel_id)
                        if video_type == "long" else
                        self.repository.iter_inventory_pages(channel_id, video_type=video_type))
        inventory: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            while True:
                page = self.budget.read(next, iterator, None)
                if page is None:
                    return inventory
                if not isinstance(page, list) or not page:
                    raise CatalogueScopeError("catalogue_inventory_page_invalid")
                for row in page:
                    if not isinstance(row, dict):
                        raise CatalogueScopeError("catalogue_inventory_row_invalid")
                    video_id = row.get("video_id")
                    if not isinstance(video_id, str) or not video_id or video_id in seen_ids:
                        raise CatalogueScopeError("catalogue_inventory_identity_invalid")
                    seen_ids.add(video_id)
                    inventory.append(row)
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    def load_full_long_inventory(self, channel_id: str) -> list[dict[str, Any]]:
        """Compatibility alias for the original Long catalogue contract."""
        return self.load_full_inventory(channel_id)

    def load_video_view_growth(
        self, channel_id: str, video_ids: list[str], *, as_of: Any, recorded_from: Any = None,
        video_type: str = "long",
    ) -> list[dict[str, Any]]:
        if not isinstance(video_type, str) or video_type not in CATALOGUE_VIDEO_TYPES:
            raise ValueError("unsupported video type")
        rows: list[dict[str, Any]] = []
        for start in range(0, len(video_ids), CATALOGUE_READ_BATCH_SIZE):
            rows.extend(self.budget.read(
                self.repository.load_video_view_growth,
                channel_id,
                video_ids[start:start + CATALOGUE_READ_BATCH_SIZE],
                as_of=as_of,
                **({"recorded_from": recorded_from} if recorded_from is not None else {}),
                **({"video_type": video_type} if video_type != "long" else {}),
            ))
        return rows

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        self.budget.account(result)
        return result
