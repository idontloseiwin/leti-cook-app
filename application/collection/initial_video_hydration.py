"""One initial-job owner: parallel provider I/O, serial atomic batch commits.

Only the provider callback runs in threads. Claims, planning, persistence and
progress stay on the coordinator thread; no connection/transaction crosses it.
"""
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
import uuid

from pipeline_trace import bind, capture, event, run as trace_run
from collection.video_trace import claims_selected, write_events, write_failed

from collection_outcome import collection_outcome_fields
from collection_shutdown import handoff_requested

MAX_INITIAL_CONCURRENCY = 8
INITIAL_BATCH_SIZE = 50
MAX_INITIAL_WINDOW = MAX_INITIAL_CONCURRENCY * INITIAL_BATCH_SIZE


@dataclass(frozen=True)
class InitialResponse:
    items: tuple = ()
    error: Exception | None = None
    elapsed: float = 0.0
    dispatched: bool = True
    counter_ids: tuple = ()
    counter_error: Exception | None = None
    batch_requests: int = 0
    metadata_observed_at: datetime | None = None
    counters_observed_at: datetime | None = None
    counter_observed_ids: tuple = ()

    def items_or_raise(self):
        if self.error is not None:
            raise self.error
        return self.items


def _counter_candidates(service, claims, items, observed_at):
    """Only absent required counts on valid, completed profiles need Batch."""
    by_id = {claim.video_id: claim for claim in claims}
    identities = [item.get("id") if isinstance(item, Mapping) else None for item in items]
    if (any(not isinstance(value, str) or value not in by_id for value in identities)
            or len(set(identities)) != len(identities)):
        return []  # The ordinary validator reports malformed/foreign identities.
    selected = []
    for item in items:
        try:
            _, lifecycle_only = service._validate_common_metadata(
                by_id[item["id"]], item, observed_at)
        except (ValueError, TypeError, KeyError):
            continue
        statistics = item.get("statistics")
        if statistics is None:
            statistics = {}
        if (not lifecycle_only and isinstance(statistics, Mapping)
                and statistics.get("viewCount") in (None, "")):
            selected.append(item["id"])
    return selected


def _with_initial_counters(items, selected, result):
    """Validate the normalized Batch identity contract before enriching copies."""
    from youtube_errors import YouTubeAPIError
    if (not isinstance(result, Mapping) or not isinstance(result.get("items"), list)
            or not isinstance(result.get("summary"), Mapping)):
        raise YouTubeAPIError("initial_batch_counters_invalid")
    counters = result["items"]
    failed = result["summary"].get("failed_ids")
    if (not isinstance(failed, list) or any(not isinstance(v, str) for v in failed)
            or any(not isinstance(v, Mapping) or not isinstance(v.get("id"), str) for v in counters)):
        raise YouTubeAPIError("initial_batch_counter_identities_invalid")
    ids = [value["id"] for value in counters]
    if (len(set(ids)) != len(ids) or len(set(failed)) != len(failed)
            or set(ids) & set(failed) or set(ids) | set(failed) != set(selected)):
        raise YouTubeAPIError("initial_batch_counter_identities_invalid")
    by_id = {value["id"]: value for value in counters}
    enriched = []
    for item in items:
        counter = by_id.get(item["id"])
        if counter is None:
            enriched.append(item)
            continue
        copy = dict(item)
        copy["statistics"] = {
            "viewCount": counter.get("views"), "likeCount": counter.get("like_count"),
            "commentCount": counter.get("comment_count"),
        }
        enriched.append(copy)
    return tuple(enriched)


class _BatchPlan:
    """Collect normalized writes without opening a database transaction."""
    _trace_staging = True

    def __init__(self):
        self.commands = []

    def _record(self, operation, rows):
        rows = [dict(row) for row in rows]
        if rows:
            self.commands.append({"operation": operation, "rows": rows})
        return len(rows)

    def complete_metadata(self, rows):
        return self._record("complete_metadata", rows)

    def complete_typed_metadata(self, rows):
        return self._record("complete_typed_metadata", rows)

    def complete_stream_lifecycle(self, rows):
        return self._record("complete_stream_lifecycle", rows)

    def complete_identity_refresh(self, rows):
        return self._record("complete_identity_refresh", rows)

    def complete_missing(self, rows):
        return self._record("complete_missing", rows)

    def fail_batch(self, rows):
        return self._record("fail_batch", rows)


def _clear_uncommitted(summary):
    for key in ("updated", "unavailable_marked", "stream_lifecycle_probed", "publication_waiting", "completed_streams",
                "long_updated", "short_updated", "stream_updated",
                "long_unavailable_marked", "short_unavailable_marked", "stream_unavailable_marked"):
        summary[key] = 0
    summary["video_ids"] = []


def settle_initial_response(service, claims, response, *, reason):
    """Validate/plan before one transaction; acknowledge progress only after it."""
    from video_collection_service import VideoCollectionService, _source_observation
    if response.error is None:
        _source_observation(response.metadata_observed_at)
        selected, observed = tuple(response.counter_ids), tuple(response.counter_observed_ids)
        if (len(set(selected)) != len(selected) or len(set(observed)) != len(observed)
                or not set(selected) <= {claim.video_id for claim in claims}
                or not set(observed) <= set(selected)
                or (observed and (response.batch_requests != 1 or response.counter_error is not None))):
            raise ValueError("initial_counter_observation_invalid")
        if observed:
            _source_observation(response.counters_observed_at)
    plan = _BatchPlan()
    staging = VideoCollectionService(
        plan, fetch_full=service.fetch_full, fetch_metadata=service.fetch_metadata,
        fetch_counters=service.fetch_counters, build_full_payload=service.build_full_payload,
        build_counter_payload=service.build_counter_payload, is_quota_exhausted=service.is_quota_exhausted,
    )
    summary = staging._process_common_claims(claims, reason=reason, initial_response=response)
    summary["routing"].update(mode="initial_combined_profiles", counter_request_per_claim_batch=response.batch_requests)
    summary["timings"]["youtube_seconds"] = response.elapsed
    summary["timings"]["db_save_seconds"] = 0.0
    rows = [row for command in plan.commands for row in command["rows"]]
    expected = {(claim.video_id, claim.lease_token) for claim in claims}
    actual = [(row.get("video_id"), row.get("lease_token")) for row in rows]
    if (len(actual) != len(expected) or set(actual) != expected
            or summary.get("settlement_unknown") or summary.get("infrastructure_error")):
        _clear_uncommitted(summary)
        raise RuntimeError("initial_batch_plan_incomplete")
    started = time.perf_counter()
    for command in plan.commands:
        write_events(command["operation"], command["rows"], "started")
    try:
        settled = service.repository.complete_initial_batch(plan.commands)
        if type(settled) is not int or settled != len(claims):
            raise RuntimeError("initial_batch_ack_invalid")
    except Exception as error:
        for command in plan.commands:
            write_failed(command["operation"], command["rows"], error)
        _clear_uncommitted(summary)
        VideoCollectionService._stop_on_unknown_settlement(summary, "initial atomic batch", error)
    else:
        for command in plan.commands:
            write_events(command["operation"], command["rows"], "confirmed")
    finally:
        summary["timings"]["db_save_seconds"] = time.perf_counter() - started
    if response.error is not None and not (summary["quota_exhausted"] or summary.get("rate_limited")):
        # A key/network/provider refusal ends this window. Previously launched
        # independent responses may still settle, but no new request is sent.
        summary["infrastructure_error"] = True
        summary["stopped_reason"] = summary["stopped_reason"] or "initial_provider_failed"
    summary.update(collection_outcome_fields(summary))
    event("video_batch", "unknown" if summary.get("settlement_unknown")
          else summary.get("completion_outcome") or "settled",
          requested_count=len(claims), received_count=summary.get("updated"),
          reason=summary.get("stopped_reason"))
    return summary


def _release_unsent(repository, claims):
    from db.hot.hot_collection_control import HotCollectionControlRepository
    return HotCollectionControlRepository(repository.database).abort(videos=[
        {"video_id": claim.video_id, "video_type": claim.video_type, "lease_token": claim.lease_token}
        for claim in claims
    ])


def _drain_unsent(repository, claims):
    """An ACK needs proof that no original token remains, not a skipped count."""
    from db.hot.hot_collection_control import HotCollectionControlRepository
    return HotCollectionControlRepository(repository.database).drain(videos=[
        {"video_id": claim.video_id, "video_type": claim.video_type, "lease_token": claim.lease_token}
        for claim in claims
    ])


def fetch_initial_response(service, claims, *, reason):
    """Fetch one bounded initial unit outside the database-owning thread."""
    at = time.perf_counter()
    event("video_provider_selection", "selected", quota_bucket="general",
          video_ids=[claim.video_id for claim in claims], reason=reason,
          requested_count=len(claims))
    try:
        items = tuple(service.fetch_initial_profiles([claim.video_id for claim in claims], reason))
        metadata_observed_at = datetime.now(timezone.utc)
    except Exception as error:
        return InitialResponse(error=error, elapsed=time.perf_counter() - at)
    event("video_general_observation", "received", quota_bucket="general",
          requested_count=len(claims), received_count=len(items),
          observed_at=metadata_observed_at.isoformat())
    counter_ids = _counter_candidates(service, claims, items, metadata_observed_at)
    if not counter_ids:
        return InitialResponse(items=items, elapsed=time.perf_counter() - at,
                               metadata_observed_at=metadata_observed_at)
    event("video_provider_selection", "selected", quota_bucket="stats",
          video_ids=list(counter_ids), requested_count=len(counter_ids),
          reason="required_counter_fallback")
    try:
        counters = service.fetch_counters(counter_ids, reason)
        counters_observed_at = datetime.now(timezone.utc)
        items = _with_initial_counters(items, counter_ids, counters)
        event("video_stats_observation", "received", quota_bucket="stats",
              video_ids=list(counter_ids), requested_count=len(counter_ids),
              received_count=len(counters["items"]), observed_at=counters_observed_at.isoformat())
        return InitialResponse(items=items, counter_ids=tuple(counter_ids), batch_requests=1,
                               elapsed=time.perf_counter() - at, metadata_observed_at=metadata_observed_at,
                               counters_observed_at=counters_observed_at,
                               counter_observed_ids=tuple(item["id"] for item in counters["items"]))
    except Exception as error:
        return InitialResponse(items=items, counter_ids=tuple(counter_ids), counter_error=error,
                               batch_requests=1, elapsed=time.perf_counter() - at,
                               metadata_observed_at=metadata_observed_at)


def _claim_initial(repository, video_ids, **claim_scope):
    """Observe claim admission even when no lease/manifest can be returned."""
    event("video_claim", "started", video_ids=list(video_ids), requested_count=len(video_ids))
    try:
        claims = repository.claim_initial_video_ids(video_ids, **claim_scope)
    except Exception as error:
        code = getattr(error, "code", None)
        if not (isinstance(code, str) and len(code) == 5 and code.isascii() and code.isalnum()):
            code = None
        event("video_claim", "unknown", video_ids=list(video_ids),
              error_code=code, error_type=type(error).__name__)
        raise
    event("video_claim", "claimed" if claims else "not_claimed",
          video_ids=[claim.video_id for claim in claims], received_count=len(claims),
          requested_count=len(video_ids))
    return claims


class InitialBatchUnit:
    """Opt-in shared-dispatch adapter; offering IDs never acquires their leases.

    The coordinator owns prepare/settle. An uncertain claim, release or commit
    blocks its credit until the caller reconciles durable state; this object
    never repeats one of those operations. Existing entrypoints are unchanged.
    """
    def __init__(self, service, *, key, job_id, channel_id, video_ids, worker_id,
                 run_id, require_ready, on_settled, lease_seconds=900,
                 reason='initial_channel_hydration', clock=time.monotonic):
        from .dispatch import Work
        ids = tuple(video_ids)
        if (not 1 <= len(ids) <= INITIAL_BATCH_SIZE
                or any(not isinstance(value, str) or not value or value != value.strip()
                       or len(value) > 128 for value in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('initial_batch_identities_invalid')
        if (not all(isinstance(value, str) and value.strip() for value in
                    (job_id, channel_id, worker_id, run_id, reason))
                or type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600
                or not all(callable(value) for value in
                           (require_ready, on_settled, clock, service.fetch_initial_profiles))):
            raise ValueError('initial_batch_scope_invalid')
        self.service, self.ids = service, ids
        self.scope = dict(job_id=job_id, channel_id=channel_id, worker_id=worker_id,
                          run_id=run_id, lease_seconds=lease_seconds)
        self.require_ready, self.on_settled, self.clock = require_ready, on_settled, clock
        self.reason, self.phase = reason, 'candidate'
        self.claims, self.summary = (), None
        self.trace_context = capture()
        self.work = Work(key=key, lane='operator', channel=channel_id, format='channel',
                         prepare=self.prepare, settle=self.settle)

    def prepare(self):
        from .dispatch import Prepared, Skipped
        if self.phase != 'candidate':
            raise RuntimeError('initial_batch_prepare_not_repeatable')
        if handoff_requested():
            self.phase = 'skipped'
            return Skipped()
        self.require_ready()
        started = self.clock()
        self.phase = 'claiming'
        with bind(batch_id=str(uuid.uuid4()), job_id=self.scope['job_id']):
            self.trace_context = capture()
        self.claims = tuple(trace_run(self.trace_context, _claim_initial,
                                    self.service.repository, self.ids, **self.scope))
        if not self.claims:
            self.phase = 'skipped'
            return Skipped()
        if (len(self.claims) > len(self.ids)
                or len({row.video_id for row in self.claims}) != len(self.claims)
                or any(row.video_id not in self.ids or row.channel_id != self.scope['channel_id']
                       for row in self.claims)):
            raise RuntimeError('initial_batch_claim_invalid')
        trace_run(self.trace_context, claims_selected, self.claims, self.reason)
        self.phase = 'prepared'
        # Count claim/DB time within the lease, leaving a margin for settlement.
        return Prepared(self.fetch, deadline=started + self.scope['lease_seconds'] - 1,
                        batch_size=len(self.claims))

    def fetch(self, cancelled):
        return trace_run(self.trace_context, self._fetch, cancelled)

    def _fetch(self, cancelled):
        if cancelled.is_set() or handoff_requested():
            return InitialResponse(dispatched=False)
        return fetch_initial_response(self.service, self.claims, reason=self.reason)

    def settle(self, completion):
        return trace_run(self.trace_context, self._settle, completion)

    def _settle(self, completion):
        from .dispatch import Ack
        if self.phase != 'prepared':
            raise RuntimeError('initial_batch_settle_not_repeatable')
        if completion.phase in ('cancelled', 'expired'):
            response = InitialResponse(dispatched=False)
        elif completion.phase == 'response' and isinstance(completion.value, InitialResponse):
            response = completion.value
        elif completion.phase == 'fetch_error' and isinstance(completion.error, Exception):
            response = InitialResponse(error=completion.error)
        else:
            raise RuntimeError('initial_batch_completion_invalid')
        if not response.dispatched:
            self.phase = 'releasing'
            _drain_unsent(self.service.repository, self.claims)
            event("video_batch", "cancelled", video_ids=[claim.video_id for claim in self.claims])
            self.phase = 'released'
            return Ack()
        self.phase = 'settling'
        self.summary = settle_initial_response(self.service, self.claims, response, reason=self.reason)
        if self.summary.get('settlement_unknown'):
            self.phase = 'settlement_unknown'
            raise RuntimeError('initial_batch_settlement_unknown')
        self.phase = 'progress'
        self.on_settled(dict(self.summary))
        self.phase = 'acknowledged'
        return Ack()


def run_initial_video_window(service, *, job_id, channel_id, video_ids, worker_id,
                             run_id, lease_seconds=900, reason="initial_channel_hydration",
                             progress_callback=None, concurrency=8):
    from video_collection_service import (
        VideoCollectionService, _common_empty_summary, _absorb_common_summary,
    )
    if type(concurrency) is not int or not 1 <= concurrency <= MAX_INITIAL_CONCURRENCY:
        raise ValueError("initial_load_concurrency must be 1..8")
    if not callable(service.fetch_initial_profiles):
        raise TypeError("initial profiles provider is required")
    selected = list(video_ids)
    if (len(selected) > concurrency * INITIAL_BATCH_SIZE
            or len(set(selected)) != len(selected)
            or any(not isinstance(value, str) or not value or value != value.strip()
                   or len(value) > 128 for value in selected)):
        raise ValueError("initial video identities are invalid")
    if not all(isinstance(value, str) and value.strip()
               for value in (job_id, channel_id, worker_id, run_id)):
        raise ValueError("initial video collection requires job/channel/worker/run")
    if type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
        raise ValueError("initial video lease must be 30..3600 seconds")
    combined = _common_empty_summary(reason)
    combined.update(claim_chunk_size=INITIAL_BATCH_SIZE, lease_seconds=lease_seconds,
                    single_attempt_per_run=True, initial_load_concurrency=concurrency)
    combined["routing"].update(mode="leased_parallel_initial_targets", counter_request_per_claim_batch=1)
    stopped = threading.Event()
    dispatch_lock = threading.Lock()
    dispatched = set()
    unknown_claim_ids = set()
    request_count = 0
    batches = [selected[i:i + INITIAL_BATCH_SIZE] for i in range(0, len(selected), INITIAL_BATCH_SIZE)]
    next_batch = 0
    started = time.perf_counter()

    def fetch(claims):
        nonlocal request_count
        ids = [claim.video_id for claim in claims]
        with dispatch_lock:
            if stopped.is_set() or handoff_requested():
                return InitialResponse(dispatched=False)
            dispatched.update(ids)
            request_count += 1
        response = fetch_initial_response(service, claims, reason=reason)
        if response.error is not None or response.counter_error is not None:
            stopped.set()
        return response

    def fail(label, error):
        stopped.set()
        VideoCollectionService._stop_on_unknown_settlement(combined, label, error)

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="initial-profile") as executor:
        pending = {}
        while next_batch < len(batches) or pending:
            while (next_batch < len(batches) and len(pending) < concurrency
                   and not stopped.is_set() and not handoff_requested()):
                batch = batches[next_batch]
                next_batch += 1
                with bind(batch_id=str(uuid.uuid4()), job_id=job_id):
                    trace_context = capture()
                try:
                    claims = trace_run(trace_context, _claim_initial, service.repository,
                        batch, job_id=job_id, channel_id=channel_id, worker_id=worker_id,
                        run_id=run_id, lease_seconds=lease_seconds,
                    )
                except Exception as error:
                    unknown_claim_ids.update(batch)
                    fail("initial claim", error)
                    break
                combined["claimed"] += len(claims)
                if not claims:
                    continue
                trace_run(trace_context, claims_selected, claims, reason)
                # Copy only context, never a connection. This preserves the
                # parent handoff deferral through provider admission wrappers.
                try:
                    pending[executor.submit(copy_context().run, trace_run, trace_context,
                                            fetch, claims)] = (claims, trace_context)
                except Exception as error:
                    fail("initial dispatch", error)
                    try:
                        _release_unsent(service.repository, claims)
                    except Exception as release_error:
                        fail("initial unsent release", release_error)
                    break
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                claims, trace_context = pending.pop(future)
                try:
                    response = future.result()
                    if not response.dispatched:
                        _release_unsent(service.repository, claims)
                        trace_run(trace_context, event, "video_batch", "cancelled",
                                  video_ids=[claim.video_id for claim in claims])
                        continue
                    summary = trace_run(trace_context, settle_initial_response,
                                        service, claims, response, reason=reason)
                    _absorb_common_summary(combined, summary, preserve_video_ids=True)
                    if any(summary.get(key) for key in (
                            "quota_exhausted", "rate_limited", "infrastructure_error", "settlement_unknown")):
                        stopped.set()
                except Exception as error:
                    fail("initial batch", error)
                if progress_callback:
                    try:
                        progress_callback(dict(combined))
                    except Exception as error:
                        fail("initial progress", error)
    combined["requested"] = len(selected)
    combined["metadata_requests"] = request_count
    combined["unstarted_video_ids"] = [value for value in selected
                                       if value not in dispatched and value not in unknown_claim_ids]
    if handoff_requested():
        combined["yield_requested"] = True
        combined["stopped_reason"] = combined["stopped_reason"] or "scheduled_handoff"
    combined["stopped_reason"] = combined["stopped_reason"] or (
        "targets_attempted" if combined["claimed"] else "no_claimable_targets")
    combined.update(collection_outcome_fields(combined, explicit_selected=len(selected)))
    combined["finished_at"] = datetime.now(timezone.utc).isoformat()
    combined["timings"]["total_batch_seconds"] = time.perf_counter() - started
    for key in combined["timings"]:
        combined["timings"][key] = round(combined["timings"][key], 6)
    if progress_callback:
        try:
            progress_callback(dict(combined))
        except Exception as error:
            fail("initial progress", error)
            combined.update(collection_outcome_fields(combined, explicit_selected=len(selected)))
    return combined
