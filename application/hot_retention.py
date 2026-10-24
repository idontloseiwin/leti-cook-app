"""A finite cleanup pass. Scheduling and installation activation are separate."""

from collections import deque
import time

from db.hot.hot_retention import HotRetentionRepository, RetentionError, TARGET_FUNCTIONS, MAX_PRIORITY_REFERENCES


def run_cleanup(repository: HotRetentionRepository, *, targets=None, limit=100,
                max_batches=len(TARGET_FUNCTIONS), max_seconds=30, clock=time.monotonic) -> dict:
    selected = tuple(TARGET_FUNCTIONS if targets is None else targets)
    if not selected or len(set(selected)) != len(selected) or any(t not in TARGET_FUNCTIONS for t in selected):
        raise ValueError("invalid_cleanup_targets")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid_cleanup_limit")
    if type(max_batches) is not int or not 1 <= max_batches <= 100:
        raise ValueError("invalid_cleanup_batch_budget")
    if type(max_seconds) is not int or not 10 <= max_seconds <= 60:
        raise ValueError("invalid_cleanup_time_budget")
    started = clock()
    pending = deque((target, None) for target in selected)
    pages, visited, exhausted = [], set(), set()
    stop_reason = "pass_finished"
    while pending:
        if len(pages) >= max_batches:
            stop_reason = "batch_budget"
            break
        # Local transport takes at most 8 seconds; reserve that time before entry.
        if max_seconds - (clock() - started) < 8:
            stop_reason = "time_budget"
            break
        target, cursor = pending.popleft()
        try:
            page = repository.cleanup_page(target, after_id=cursor, limit=limit)
        except RetentionError as error:
            return {"status": "error", "error_code": error.code, "failed_target": target,
                    "batches": len(pages), "pages": pages,
                    "outcome_may_be_committed": error.code in ("cleanup_outcome_unknown", "invalid_cleanup_result"),
                    "automatic_retry": False}
        visited.add(target)
        pages.append(page.summary())
        if page.complete:
            exhausted.add(target)
        elif page.next_cursor is not None and page.next_cursor != cursor:
            pending.append((target, page.next_cursor))
        elif page.next_cursor is None and page.deleted > 0:
            # Immutable oldest-first history does not need a retained cursor.
            pending.append((target, None))
        else:
            # A locked/deferred page is revisited by a later explicit pass.
            # Never spin on an unchanged cursor or claim that it is clean.
            stop_reason = "deferred_or_no_progress"
    oldest = [p["oldest_remaining_expired_source_at"] for p in pages if p["oldest_remaining_expired_source_at"]]
    return {"status": "bounded_pass", "stop_reason": stop_reason, "batches": len(pages),
            "counts": {key: sum(p[key] for p in pages) for key in ("scanned", "deleted", "scrubbed", "deferred")},
            "targets_visited": [t for t in selected if t in visited],
            "targets_scan_exhausted": [t for t in selected if t in exhausted],
            "targets_not_visited": [t for t in selected if t not in visited],
            "oldest_remaining_expired_source_at": min(oldest) if oldest else None,
            "telemetry_scope": "scanned_pages_only", "whole_database_clean_verified": False,
            "pages": pages}


class HotCleanupCycle:
    """A single-owner, RAM-only cursor cycle across bounded cleanup slices.

    This is not a scheduler, admission certificate or durable checkpoint. A
    process restart starts a fresh scan. Current SQL contracts use each RPC's
    database clock, not one shared snapshot/cutoff. Writers and time can change
    already visited rows, so even an exhausted scan never proves the DB clean.
    The supplied repository must independently bound each call to eight seconds.
    """

    def __init__(self, repository: HotRetentionRepository, *, targets=None):
        selected = tuple(TARGET_FUNCTIONS if targets is None else targets)
        if (not selected or len(set(selected)) != len(selected)
                or any(target not in TARGET_FUNCTIONS for target in selected)):
            raise ValueError("invalid_cleanup_targets")
        self.repository = repository
        self.targets = selected
        self.reset()

    def reset(self):
        """Explicitly abandon any prior cycle and scan again from the beginning."""
        self._pending = deque((target, None) for target in self.targets)
        self._visited = set()
        self._exhausted = set()
        self._observed_deferred = set()
        self._counts = dict.fromkeys(("scanned", "deleted", "scrubbed", "deferred"), 0)
        self._batches = 0
        self._invalid_clocks_observed = 0
        self._unknown_expiry_observed = 0
        self._confirmed_expired_observed = 0
        self._oldest_observed = None
        self._first_page_at = None
        self._last_page_at = None
        self._failure = None

    def retry_failed(self):
        """Permit one explicit retry after the caller reviewed an uncertain result.

        A failed RPC may have committed. Its cursor is retained unchanged so a
        retry re-reads that page; counts cover acknowledged pages only and must
        not be treated as an exact mutation ledger. There is no automatic retry.
        """
        if self._failure is None:
            raise ValueError("cleanup_cycle_has_no_failed_page")
        self._failure = None

    def summary(self):
        """Bounded diagnostics only: neither API locators nor opaque cursors."""
        result = {
            "status": ("error" if self._failure is not None else
                       "scan_exhausted" if not self._pending else "in_progress"),
            "batches": self._batches,
            "counts": dict(self._counts),
            "targets_visited": [target for target in self.targets if target in self._visited],
            "targets_scan_exhausted": [target for target in self.targets if target in self._exhausted],
            "targets_not_visited": [target for target in self.targets if target not in self._visited],
            "targets_pending": [target for target in self.targets
                                if target not in self._exhausted],
            "targets_with_observed_deferrals": [target for target in self.targets
                                                if target in self._observed_deferred],
            "invalid_clocks_observed": self._invalid_clocks_observed,
            "unknown_expiry_observed": self._unknown_expiry_observed,
            "confirmed_expired_observed": self._confirmed_expired_observed,
            "oldest_observed_expired_source_at": self._oldest_observed,
            "first_page_as_of": self._first_page_at,
            "last_page_as_of": self._last_page_at,
            "scan_clock_scope": "per_rpc_database_time",
            "telemetry_scope": "acknowledged_pages_only",
            "whole_database_clean_verified": False,
        }
        if self._failure is not None:
            result.update(self._failure)
        return result

    def run_slice(self, *, limit=100, max_batches=len(TARGET_FUNCTIONS),
                  max_seconds=30, clock=time.monotonic, on_page=None):
        """Advance pending targets fairly without restarting their PK scans."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_cleanup_limit")
        if type(max_batches) is not int or not 1 <= max_batches <= 100:
            raise ValueError("invalid_cleanup_batch_budget")
        if type(max_seconds) is not int or not 10 <= max_seconds <= 60:
            raise ValueError("invalid_cleanup_time_budget")
        started = clock()
        pages = []
        stalled = set()
        stop_reason = "scan_exhausted"
        while self._pending:
            if self._failure is not None:
                stop_reason = "explicit_retry_required"
                break
            if len(pages) >= max_batches:
                stop_reason = "batch_budget"
                break
            if max_seconds - (clock() - started) < 8:
                stop_reason = "time_budget"
                break
            if all(target in stalled for target, _ in self._pending):
                stop_reason = "deferred_or_no_progress"
                break
            target, cursor = self._pending[0]
            if target in stalled:
                self._pending.rotate(-1)
                continue
            try:
                page = self.repository.cleanup_page(target, after_id=cursor, limit=limit)
            except RetentionError as error:
                # Leave the failed page at the head. An ambiguous commit cannot
                # advance the cursor or disappear from subsequent slices.
                known_code = error.code in ("cleanup_outcome_unknown", "invalid_cleanup_result")
                self._failure = {
                    "error_code": error.code if known_code else "cleanup_failed",
                    "failed_target": target,
                    "outcome_may_be_committed": known_code,
                    "automatic_retry": False,
                }
                stop_reason = "explicit_retry_required"
                break
            if on_page is not None:
                on_page(page)
            self._pending.popleft()
            self._visited.add(target)
            self._batches += 1
            pages.append(page.summary())
            for key in self._counts:
                self._counts[key] += getattr(page, key)
            self._invalid_clocks_observed += page.invalid_clock_remaining
            self._unknown_expiry_observed += page.unknown_expiry_remaining
            self._confirmed_expired_observed += page.confirmed_expired_remaining
            if page.deferred:
                self._observed_deferred.add(target)
            if page.oldest_remaining_expired_source_at is not None:
                self._oldest_observed = min(filter(None, (
                    self._oldest_observed, page.oldest_remaining_expired_source_at)))
            if self._first_page_at is None:
                self._first_page_at = page.as_of
            self._last_page_at = page.as_of
            if page.complete:
                self._exhausted.add(target)
            elif page.next_cursor is not None and page.next_cursor != cursor:
                self._pending.append((target, page.next_cursor))
            elif page.next_cursor is None and page.deleted > 0:
                self._pending.append((target, None))
            else:
                # Retain this work for the next bounded slice, but don't spin on
                # the same lease/unchanged page inside this slice.
                self._pending.append((target, cursor))
                stalled.add(target)
        result = self.summary()
        result.update(stop_reason=stop_reason, slice_batches=len(pages), pages=pages,
                      targets_stalled=[target for target in self.targets if target in stalled],
                      priority_observation_complete=on_page is not None)
        return result


class PriorityRetrySet:
    """Bounded private causes; historical scan diagnostics are not live proof.

    Complete acknowledged exact-key retries can resolve retained causes. Missing
    coverage and overflow require the existing complete clean scan fallback.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.references = {}
        self.incomplete = False
        self.generation = getattr(self, "generation", 0) + 1
        self.next_retry = 0
        self.fallback_oldest = None

    @property
    def unresolved(self):
        return bool(self.references or self.incomplete)

    @property
    def oldest(self):
        values = [value for value in (*self.references.values(), self.fallback_oldest) if value]
        return min(values) if values else None

    def invalidate(self, oldest=None):
        self.incomplete = True
        self.generation += 1
        if oldest:
            self.fallback_oldest = min(filter(None, (oldest, self.fallback_oldest)))

    def observe(self, page, now):
        # Lock contention does not establish age. Keep a previously observed
        # unsafe cause until its acknowledged retry clears it, but do not add
        # a fresh writer merely because the cleaner could not read its clock.
        retention = bool(page.oldest_remaining_expired_source_at or page.invalid_clock_remaining
                         or page.confirmed_expired_remaining)
        if not retention:
            return
        oldest = page.oldest_remaining_expired_source_at
        if not page.priority_complete:
            self.invalidate(oldest)
        was_empty = not self.references
        for ref in page.priority_references:
            if ref not in self.references and len(self.references) >= MAX_PRIORITY_REFERENCES:
                self.invalidate(oldest)
                continue
            previous = self.references.get(ref)
            self.references[ref] = min(filter(None, (previous, oldest)), default=None)
            self.generation += 1
        if was_empty and self.references:
            self.next_retry = now + 5

    def due(self, now):
        return bool(self.references) and now >= self.next_retry

    def request(self):
        return self.generation, tuple(self.references)

    def acknowledge(self, generation, requested, result, now):
        self.next_retry = now + 5
        # A partial proof or a changed registry cannot certify its unseen set.
        if (not result.complete or generation != self.generation
                or set(result.checked) != set(requested)
                or not set(result.remaining) <= set(requested)):
            return False
        for ref in set(requested) - set(result.remaining):
            self.references.pop(ref, None)
        self.generation += 1
        return not self.unresolved
