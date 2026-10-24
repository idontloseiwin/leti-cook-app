"""The existing bounded automatic pass with an explicit collection scope."""

import time
import uuid
from datetime import datetime, timezone

from collection.context import CollectionContext
from channel_collection_scope import validate_channel_scopes
from collection.channel_metadata import recover_stale_running_statuses, run_channel_metadata_sync_batch
from collection.runs import new_video_count_from_summary, update_sync_run_progress
from collection_shutdown import stop_at_boundary
from collection_outcome import collection_outcome_fields, copy_collection_outcome, copy_collection_stop as _copy_collection_stop, sync_completion_fields
from db import ChannelDiscoveryRepository, TypedInventoryRepository, VideoCollectionRepository
from source_log import source_log_message
from telemetry import emit_phase
from pipeline_trace import event, scope
from collection.inventory import run_typed_inventory_batch
from video_collection_service import run_claimed_typed_video_collection
from youtube_errors import YouTubePlaylistNotFoundError


CHANNEL_DISCOVERY_PAGING_SCHEMA_RPC = "assert_channel_discovery_paging_schema"
VIDEO_COLLECTION_READY_RPC = "assert_video_collection_queue_ready"
VIDEO_COLLECTION_COMMON_POOL_RPC = "assert_video_collection_common_pool_schema"
FORCED_METADATA_CLAIM_RPC = "claim_video_metadata_collection_ids"
CHANNEL_DISCOVERY_READY_RPC = "assert_channel_discovery_queue_ready"
TYPED_INVENTORY_RUNTIME_SCHEMA_RPC = "assert_typed_inventory_runtime_schema"
TYPED_VIDEO_COLLECTION_READY_RPC = "assert_typed_video_collection_schema"


def require_channel_discovery_enabled(*, context: CollectionContext):
    """Keep automatic discovery on the durable queue and its leases."""
    if not context.policy.database_channel_discovery_enabled:
        raise RuntimeError(
            "Database channel discovery is required; the legacy discovery runtime "
            "has been retired. Set DATABASE_CHANNEL_DISCOVERY_ENABLED=true after "
            "verifying the queue schema."
        )


def unique_video_ids(video_ids):
    seen = set()
    unique_ids = []
    for video_id in video_ids:
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        unique_ids.append(video_id)
    return unique_ids


def require_video_collection_enabled(*, context: CollectionContext):
    """Reject the retired runtime before collection or database preflight."""
    enabled = context.settings.database_video_collection_enabled
    if not enabled:
        raise RuntimeError(
            "Database video collection is required; the legacy stats runtime has been "
            "retired. Set DATABASE_VIDEO_COLLECTION_ENABLED=true after verifying "
            "the queue schema."
        )


def validate_required_stats_queue_rpcs(*, context: CollectionContext):
    require_video_collection_enabled(context=context)
    errors = []
    checked = 0
    video_repository = VideoCollectionRepository(context.database)
    discovery_repository = ChannelDiscoveryRepository(context.database)

    def check(label, callback):
        nonlocal checked
        try:
            callback()
        except Exception as error:
            errors.append(f"{label}: {error}")
        checked += 1

    check(VIDEO_COLLECTION_READY_RPC, video_repository.preflight_queue_ready)
    check(
        VIDEO_COLLECTION_COMMON_POOL_RPC,
        video_repository.preflight_common_pool_ready,
    )
    check(
        FORCED_METADATA_CLAIM_RPC,
        lambda: video_repository.assert_forced_metadata_claim_ready(
            worker_id="auto-sync-preflight",
            run_id=str(uuid.uuid4()),
            lease_seconds=300,
        ),
    )

    check(
        CHANNEL_DISCOVERY_PAGING_SCHEMA_RPC,
        discovery_repository.assert_paging_schema,
    )

    if context.policy.database_channel_discovery_enabled:
        check(
            CHANNEL_DISCOVERY_READY_RPC,
            discovery_repository.preflight_queue_ready,
        )

    if context.settings.typed_playlist_inventory_enabled:
        check(
            TYPED_INVENTORY_RUNTIME_SCHEMA_RPC,
            TypedInventoryRepository(context.database).assert_ready,
        )

    # Typed readiness belongs to its isolated runner. An unused/failing typed
    # collector cannot block the healthy long-form phases.

    if errors:
        raise RuntimeError(
            "Required PostgreSQL database contract is unavailable or incompatible; "
            "verify connectivity, migrations, and queue backfill. Underlying errors: "
            + "; ".join(errors)
        )
    return {
        "status": "ok",
        "checked": checked,
        "policy_assertion": None,
        "video_collection_queue": VIDEO_COLLECTION_READY_RPC,
        "video_collection_common_pool": VIDEO_COLLECTION_COMMON_POOL_RPC,
        "forced_metadata_claim_rpc": FORCED_METADATA_CLAIM_RPC,
        "channel_discovery_paging_schema_rpc": CHANNEL_DISCOVERY_PAGING_SCHEMA_RPC,
        "channel_discovery_queue": (
            CHANNEL_DISCOVERY_READY_RPC if context.policy.database_channel_discovery_enabled else None
        ),
        "typed_inventory_runtime_schema_rpc": (
            TYPED_INVENTORY_RUNTIME_SCHEMA_RPC if context.settings.typed_playlist_inventory_enabled else None
        ),
        "typed_video_collection_schema_rpc": (
            TYPED_VIDEO_COLLECTION_READY_RPC
            if context.policy.short_video_collection_enabled or context.policy.completed_stream_collection_enabled
            else None
        ),
        "batch_stats_save_rpc": None,
        "batch_stats_metadata_schema_rpc": None,
    }


def validate_required_auto_sync_rpcs(*, context: CollectionContext):
    require_channel_discovery_enabled(context=context)
    return validate_required_stats_queue_rpcs(context=context)


def run_typed_playlist_inventory_batch(*, limit, explicit_channel_ids=None, video_types=('long', 'short', 'stream'), run_id=None, prepare=True, on_page=None, context: CollectionContext):
    """Run the shared UULF/UUSH/UULV pass with an explicit request budget."""

    if not context.settings.typed_playlist_inventory_enabled or int(limit or 0) <= 0:
        return {
            "enabled": context.settings.typed_playlist_inventory_enabled,
            "prepared": 0,
            "claimed": 0,
            "processed": 0,
            "completed": 0,
            "continuations": 0,
            "playlist_not_found": 0,
            "inserted": 0,
            "already_matching": 0,
            "conflicts": 0,
            "errors": 0,
            "results": [],
        }
    return run_typed_inventory_batch(
        repository=TypedInventoryRepository(context.database),
        fetch_page=context.sources.fetch_playlist_items_page,
        limit=limit,
        worker_id=context.video_worker_id() + ":typed-inventory",
        run_id=str(run_id) if run_id is not None else None,
        explicit_channel_ids=explicit_channel_ids,
        video_types=tuple(video_types),
        playlist_not_found_errors=(YouTubePlaylistNotFoundError,),
        prepare=prepare,
        on_page=on_page,
    )


def run_typed_video_collection_queue_batch(*, video_type, limit=50, run_id=None, progress_callback=None, max_duration_seconds=300, context: CollectionContext):
    """Run one separately gated typed current-data phase."""

    normalized_type = str(video_type or "").strip().lower()
    enabled_by_env = {
        "short": context.policy.short_video_collection_enabled,
        "stream": context.policy.completed_stream_collection_enabled,
    }
    if normalized_type not in enabled_by_env:
        raise ValueError("typed video collection requires short or stream")
    if not enabled_by_env[normalized_type] or int(limit or 0) <= 0:
        return {
            "enabled": bool(enabled_by_env[normalized_type]),
            "video_type": normalized_type,
            "requested": 0,
            "claimed": 0,
            "updated": 0,
            "missing": 0,
            "unavailable_marked": 0,
            "errors": 0,
            "quota_exhausted": False,
            "video_ids": [],
        }
    require_video_collection_enabled(context=context)
    repository, service = context.video.build_service()
    repository.preflight_typed_collection_ready()
    return run_claimed_typed_video_collection(
        repository,
        service,
        video_type=normalized_type,
        limit=min(max(int(limit), 0), 100000),
        worker_id=context.video_worker_id() + ":typed:" + normalized_type,
        run_id=str(run_id) if run_id is not None else None,
        progress_callback=progress_callback,
        max_duration_seconds=max_duration_seconds,
    )


def run_video_stats_sync_batch(limit=50, progress_callback=None, common_preflight_checked=False, *, context: CollectionContext):
    return context.video.run_batch(
        limit=limit,
        reason="auto_stats_batch",
        progress_callback=progress_callback,
        common_preflight_checked=common_preflight_checked,
    )



class AutomaticDispatchWindow:
    """Existing ordered auto phases borrow one finite operator service window.

    One Coordinator/executor survives phase boundaries; operator limits never
    reset there. Unknown owner callbacks are latched and never replayed, while
    independently known completions can drain. This owns no durable work queue.
    """
    def __init__(self, *, concurrency, require_ready, should_stop, operator_factory,
                 executor=None, clock=time.monotonic, fetch_scope=None, wake_revision=lambda:None):
        from concurrent.futures import ThreadPoolExecutor
        from collection.dispatch import Coordinator
        self.concurrency,self.require_ready,self.should_stop=concurrency,require_ready,should_stop
        self.clock,self.wake_revision=clock,wake_revision
        self.stop_flags,self.stop_reason={},None
        self._advance_failed=self._drain_failed=self._closed=False
        self._feeder=None;self._auto_first=True
        self._last_wake=wake_revision()
        self._next_wake,self._next_poll=clock()+3,clock()+60
        self._own_executor=executor is None
        self.executor=executor or ThreadPoolExecutor(max_workers=concurrency,thread_name_prefix='collection-provider')
        self.coordinator=Coordinator(self.executor,concurrency,clock=clock,
            **({'fetch_scope':fetch_scope} if fetch_scope is not None else {}))
        try:self.operator=operator_factory(coordinator=self.coordinator,on_stop=self.stop_all,
                should_stop=self.stopping,require_ready=self.ready)
        except BaseException:
            if self._own_executor:self.executor.shutdown(wait=True,cancel_futures=True)
            raise

    def stop_all(self, summary):
        from operator_jobs import COLLECTION_STOP_FLAGS,operator_collection_stop_reason
        reason=operator_collection_stop_reason(summary)
        if summary.get('yield_requested') and not reason:reason='scheduled_handoff'
        if reason:
            self.stop_reason=self.stop_reason or reason
            if self._feeder is not None:self._feeder.pause(self.stop_reason)
            for name in (*COLLECTION_STOP_FLAGS,'yield_requested','hot_runtime_interrupted'):
                self.stop_flags[name]=bool(self.stop_flags.get(name) or summary.get(name))

    def stopping(self):
        return bool(self.stop_reason) or self.should_stop()

    def ready(self):
        from hot_runtime import HotRuntimeUnavailable
        try:self.require_ready()
        except HotRuntimeUnavailable:
            self.stop_all(dict(hot_runtime_interrupted=True,infrastructure_error=True,
                               stopped_reason='hot_runtime_interrupted'))
            raise

    def _advance(self, turn=None, *, ready_limit=0):
        if self._advance_failed:return
        try:self.operator.advance(turn,ready_limit=ready_limit)
        except Exception:
            self._advance_failed=True
            raise

    def _drain(self):
        if self._drain_failed:return True
        try:return self._drain_known()
        except Exception:
            self._drain_failed=True
            raise

    def _drain_known(self):
        owner=self.coordinator
        owner.pause()
        self._advance(ready_limit=0)
        failed={failure.key for failure in owner.failures()}
        known=tuple(key for key in owner.keys() if key not in failed)
        for key in known:owner.cancel(key)
        # cancel removes never-prepared ready descriptors immediately. Only
        # the remaining held keys are valid for the confirmed drain boundary.
        known=tuple(key for key in owner.keys() if key not in failed)
        turn=owner.drain_known(known) if known else None
        self._advance(turn,ready_limit=0)
        return not any(key not in failed for key in owner.keys())

    def _wake(self):
        if self.clock()<self._next_wake:return
        self._next_wake=self.clock()+3
        revision=self.wake_revision()
        if revision!=self._last_wake or self.clock()>=self._next_poll:
            self.operator.resume_idle()
            self._next_poll=self.clock()+60
        self._last_wake=revision

    def run_phase(self, feeder, *, progress_callback=None):
        if self._closed:raise RuntimeError('collection_window_closed')
        owner=self.coordinator;self._feeder=feeder
        auto_keys=set();last_progress=None;progress_failed=False;refill_turns=0
        feeder.offer_to(owner,limit=0)
        def offer():
            before=set(owner.keys())
            feeder.offer_to(owner,limit=self.concurrency)
            auto_keys.update(set(owner.keys())-before)
        try:
            while True:
                submitted=0
                candidate_before=feeder.summary.get('candidate_inspected',0)
                raw_before=feeder.summary.get('inspected',0)
                if self.should_stop():self.stop_all(dict(yield_requested=True))
                draining=bool(self.stop_reason)
                try:
                    if self.stop_reason:
                        feeder.pause(self.stop_reason)
                        self.operator.stop(self.stop_reason,**self.stop_flags)
                        finished=self._drain()
                    else:
                        self._wake()
                        if self._auto_first:offer()
                        self._advance(ready_limit=self.concurrency)
                        if not self._auto_first:offer()
                        self._auto_first=not self._auto_first
                        turn=owner.step()
                        submitted+=turn.submitted
                        self._advance(turn,ready_limit=0)
                        if turn.skipped and not self.stop_reason:
                            # A DB-only parent admission uses a fair turn but
                            # creates provider children only on advance. Give
                            # those children one bounded turn before refilling
                            # automatic work; all credits and gates stay shared.
                            self._advance(ready_limit=self.concurrency)
                            if not self.stop_reason:
                                followup=owner.step()
                                submitted+=followup.submitted
                                self._advance(followup,ready_limit=0)
                        auto_keys.intersection_update(owner.keys())
                        finished=feeder.exhausted and not auto_keys
                    progress=tuple(feeder.summary.get(name) for name in
                        ('claimed','processed','inspected','updated','errors','candidate_inspected'))
                    if progress!=last_progress and progress_callback is not None and not progress_failed:
                        last_progress=progress
                        try:progress_callback(dict(feeder.summary))
                        except Exception:
                            progress_failed=True
                            raise
                except Exception:
                    self.stop_all(dict(settlement_unknown=True,infrastructure_error=True))
                    self.operator.stop(self.stop_reason,**self.stop_flags)
                    if draining:break
                    continue
                if finished:break
                snapshot=owner.snapshot()
                # A productive turn can expose more feeder work that wait()
                # cannot see yet. Re-enter the complete fair turn, bounded by C.
                # A filtered advisory page can make finite progress without
                # offering work yet. Empty/locked claims still do not count.
                candidate_progress=(feeder.summary.get('candidate_inspected',0)>candidate_before
                    and feeder.summary.get('inspected',0)==raw_before)
                if (snapshot.inflight and (submitted or candidate_progress) and snapshot.held<self.concurrency
                        and not self.stop_reason and not (snapshot.paused or snapshot.stopped or snapshot.blocked)
                        and refill_turns<self.concurrency-1):
                    refill_turns+=1
                    continue
                refill_turns=0
                if snapshot.inflight:owner.wait(.05)
            result=feeder.summary
            result.update(self.stop_flags)
            if self.stop_reason:
                result['stopped_reason']=self.stop_reason
                if result.get('infrastructure_error'):result['errors']=max(1,result.get('errors',0))
            if not owner.snapshot().blocked and not result.get('settlement_unknown'):
                feeder.finish_pass(owner)
            if progress_callback is not None and not progress_failed:
                try:progress_callback(dict(result))
                except Exception:
                    progress_failed=True
                    self.stop_all(dict(settlement_unknown=True,infrastructure_error=True))
                    result.update(self.stop_flags)
                    raise
            return result
        finally:self._feeder=None

    def close(self):
        if self._closed:return
        self._closed=True
        try:
            self.operator.stop(self.stop_reason or 'phase_complete',**self.stop_flags)
            while True:
                try:
                    if self._drain():break
                except Exception:
                    self.stop_all(dict(settlement_unknown=True,infrastructure_error=True))
                    break
                if self.coordinator.snapshot().inflight:self.coordinator.wait(.05)
        finally:
            if self._own_executor:self.executor.shutdown(wait=True,cancel_futures=True)

    @property
    def checkpoint_ready(self):
        return (self._closed and not self._advance_failed and not self._drain_failed and not self.coordinator.keys()
                and not self.coordinator.snapshot().blocked and not self.stop_flags.get('settlement_unknown'))


def run_common_operator_phase(service, *, limit, concurrency, worker_id, require_ready,
        should_stop, operator_factory, cursor, progress_callback=None, executor=None,
        clock=time.monotonic, fetch_scope=None, wake_revision=lambda: None, window=None):
    """Compatibility wrapper; production borrows its whole cycle's window."""
    from collection.auto_pass import AutoPassState
    from collection.common_feeder import CommonAutoFeeder
    from video_collection_service import TYPED_RECONCILE_LIMIT,DEFAULT_LEASE_SECONDS
    state=AutoPassState(max(int(limit or 0),0),concurrency=concurrency)
    own_window=window is None
    window=window or AutomaticDispatchWindow(concurrency=concurrency,require_ready=require_ready,
        should_stop=should_stop,operator_factory=operator_factory,executor=executor,clock=clock,
        fetch_scope=fetch_scope,wake_revision=wake_revision)
    feeder=CommonAutoFeeder(service,state=state,cursor=cursor,worker_id=worker_id,
        require_ready=window.ready,lease_seconds=DEFAULT_LEASE_SECONDS,reason='auto_stats_batch',
        should_stop=window.stopping,on_settled=window.stop_all)
    try:
        if state.remaining and not window.stopping():
            for kind in ('short','stream'):
                try:
                    prepared=service.repository.reconcile_typed(kind,limit=TYPED_RECONCILE_LIMIT)
                    feeder.summary[kind+'_queue_prepared']=int((prepared or {}).get('affected_count') or 0)
                except Exception as error:
                    feeder.summary['queue_prepare_errors']+=1
                    feeder.summary['errors']+=1
                    feeder.summary['warnings'].append(kind+' queue prepare failed: '+type(error).__name__)
        result=window.run_phase(feeder,progress_callback=progress_callback)
    finally:
        if own_window:window.close()
    result.update(window.stop_flags)
    if not window.stop_reason:
        result['stopped_reason']=('limit_reached' if not state.remaining else
                                 'scan_budget' if not cursor.sweep_exhausted else 'no_claimable_work')
    result.update(collection_outcome_fields(result))
    result.update(finished_at=datetime.now(timezone.utc).isoformat(),claim_chunk_size=50,
                  lease_seconds=DEFAULT_LEASE_SECONDS,single_attempt_per_run=True)
    result['operator']=window.operator.summary
    return result


def _run_auto_sync_cycle(stats_limit=50, new_video_stats_limit=50, metadata_limit=150, short_video_collection_limit=0, completed_stream_collection_limit=0, sync_run_id=None, *, inventory_page_budget, context: CollectionContext, common_dispatch=None):
    dispatched = common_dispatch is not None and hasattr(common_dispatch, 'metadata')
    # Retained CLI arguments are compatibility-only. Current-data work for all
    # three formats is owned by the single common stats_limit pool below.
    short_video_collection_limit = 0
    completed_stream_collection_limit = 0
    cycle_start = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    summary = {
        "started_at": started_at,
        "finished_at": None,
        "channel_discovery": None,
        "channel_metadata": None,
        "typed_inventory": None,
        "long_availability_loss": None,
        "lifecycle_recovery": None,
        "preflight": None,
        "new_video_stats": None,
        "video_collection": None,
        "regular_video_stats": None,
        "short_video_collection": None,
        "completed_stream_collection": None,
        "new_video_ids": [],
        "new_video_stats_video_ids": [],
        "new_video_stats_deferred": 0,
        "new_video_ids_omitted": 0,
        "errors": 0,
        "timings": {
            "preflight_seconds": None,
            "lifecycle_recovery_seconds": None,
            "channel_metadata_seconds": None,
            "typed_inventory_seconds": None,
            "long_availability_loss_seconds": None,
            "channel_discovery_seconds": None,
            "collect_new_video_ids_seconds": None,
            "new_video_stats_seconds": None,
            "video_collection_seconds": None,
            "regular_video_stats_seconds": None,
            "short_video_collection_seconds": None,
            "completed_stream_collection_seconds": None,
            "total_seconds": None,
        },
    }

    def mark_progress(phase, state="running"):
        summary.update(sync_completion_fields(summary))
        summary["current_phase"] = phase
        summary["phase_state"] = state
        summary["progress_updated_at"] = datetime.now(timezone.utc).isoformat()
        summary["timings"]["elapsed_seconds"] = time.perf_counter() - cycle_start
        emit_phase(phase, state)
        event("cycle_phase", state, phase=phase, count=summary.get("errors"))
        update_sync_run_progress(sync_run_id, summary, context=context)

    def stop_after_collection(phase, result):
        if not _copy_collection_stop(summary, result):
            return False
        if not result.get("yield_requested") or any(result.get(key) for key in (
            "errors", "infrastructure_error", "settlement_unknown", "quota_exhausted",
            "general_quota_exhausted", "batch_quota_exhausted", "batch_unavailable",
        )):
            summary["errors"] = max(int(summary["errors"]), 1)
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["timings"]["total_seconds"] = time.perf_counter() - cycle_start
        mark_progress(phase, "error" if summary["errors"] else "paused")
        return True

    def stop_for_handoff():
        if not stop_at_boundary(summary):
            return False
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["timings"]["total_seconds"] = time.perf_counter() - cycle_start
        mark_progress(summary.get("current_phase", "preflight"), "paused")
        return True

    print(
        "[auto-sync] start "
        f"metadata_limit={metadata_limit} "
        f"inventory_page_budget={inventory_page_budget} "
        f"short_video_collection_limit={short_video_collection_limit} "
        f"completed_stream_collection_limit={completed_stream_collection_limit} "
        f"stats_limit={stats_limit}",
        flush=True,
    )

    if stop_for_handoff():
        return summary
    mark_progress("preflight")
    phase_start = time.perf_counter()
    try:
        summary["preflight"] = validate_required_auto_sync_rpcs(context=context)
        summary["timings"]["preflight_seconds"] = time.perf_counter() - phase_start
        mark_progress("preflight", "completed")
    except Exception as error:
        summary["preflight"] = {"status": "error", "error": str(error)}
        summary["errors"] += 1
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["timings"]["preflight_seconds"] = time.perf_counter() - phase_start
        summary["timings"]["total_seconds"] = time.perf_counter() - cycle_start
        mark_progress("preflight", "error")
        print(source_log_message('auto_preflight_error', lambda: (f"[auto-sync] error preflight failed: {error}"), error=error), flush=True)
        return summary

    if stop_for_handoff():
        return summary
    mark_progress("lifecycle_recovery")
    phase_start = time.perf_counter()
    lifecycle_recovery = recover_stale_running_statuses(context=context)
    summary["lifecycle_recovery"] = lifecycle_recovery
    summary["errors"] += int(lifecycle_recovery.get("errors") or 0)
    summary["timings"]["lifecycle_recovery_seconds"] = time.perf_counter() - phase_start
    mark_progress("lifecycle_recovery", "completed")

    # Drain due current-data work before the serial channel phases. Newly
    # discovered IDs later in this cycle are only enqueued and join the next
    # normal common-pool run.
    if stop_for_handoff():
        return summary
    mark_progress("video_collection")
    phase_start = time.perf_counter()

    def long_collection_projection(collection_summary):
        return {
            "reason": collection_summary.get("reason"),
            "started_at": collection_summary.get("started_at"),
            "finished_at": collection_summary.get("finished_at"),
            "requested": int(
                collection_summary.get("long_requested")
                if collection_summary.get("long_requested") is not None
                else collection_summary.get("requested") or 0
            ),
            "updated": int(
                collection_summary.get("long_updated")
                if collection_summary.get("long_updated") is not None
                else collection_summary.get("updated") or 0
            ),
            "missing": int(collection_summary.get("long_missing") or 0),
            "unavailable_marked": int(
                collection_summary.get("long_unavailable_marked") or 0
            ),
            "errors": int(collection_summary.get("errors") or 0),
            "routing": {"mode": "common_pool_long_compatibility_projection"},
        }

    def video_collection_progress(collection_summary):
        summary["video_collection"] = collection_summary
        summary["regular_video_stats"] = long_collection_projection(
            collection_summary
        )
        elapsed = time.perf_counter() - phase_start
        summary["timings"]["video_collection_seconds"] = elapsed
        summary["timings"]["regular_video_stats_seconds"] = elapsed
        mark_progress("video_collection")

    try:
        video_collection = (common_dispatch or run_video_stats_sync_batch)(
            limit=stats_limit,
            progress_callback=video_collection_progress,
            common_preflight_checked=True,
            context=context,
        )
    except Exception as error:
        video_collection = {
            "reason": "auto_video_collection",
            "started_at": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "requested": 0,
            "updated": 0,
            "batches": 0,
            "errors": 1,
            "video_ids": [],
            "error_messages": [str(error)],
        }
        if dispatched:video_collection.update(settlement_unknown=True,infrastructure_error=True)
        print(source_log_message('auto_video_collection_error', lambda: (f"[auto-sync] error video_collection failed: {error}"), error=error), flush=True)
    summary["timings"]["video_collection_seconds"] = (
        time.perf_counter() - phase_start
    )
    summary["timings"]["regular_video_stats_seconds"] = (
        summary["timings"]["video_collection_seconds"]
    )
    regular_video_stats = long_collection_projection(video_collection)
    summary["video_collection"] = video_collection
    summary["regular_video_stats"] = regular_video_stats
    summary["errors"] += int(video_collection.get("errors") or 0)
    print(
        "[auto-sync] phase video_collection done "
        f"seconds={summary['timings']['video_collection_seconds']:.3f}",
        flush=True,
    )
    mark_progress("video_collection", "completed")
    if stop_after_collection("video_collection", video_collection):
        return summary

    if stop_for_handoff():
        return summary
    mark_progress("channel_metadata")
    phase_start = time.perf_counter()
    try:
        channel_metadata = (common_dispatch.metadata if dispatched else run_channel_metadata_sync_batch)(limit=metadata_limit, context=context)
    except Exception as error:
        channel_metadata = {
            "requested": 0,
            "processed": 0,
            "updated": 0,
            "video_count_changed": 0,
            "video_count_increased": 0,
            "video_count_decreased": 0,
            "video_count_increased_channel_ids": [],
            "video_count_decreased_channel_ids": [],
            "channel_discovery_prioritized": 0,
            "errors": 1,
            "error_messages": [str(error)],
        }
        if dispatched:channel_metadata.update(settlement_unknown=True,infrastructure_error=True)
        print(source_log_message('auto_metadata_error', lambda: (f"[auto-sync] error channel_metadata failed: {error}"), error=error), flush=True)
    summary["timings"]["channel_metadata_seconds"] = time.perf_counter() - phase_start
    print(
        "[auto-sync] phase channel_metadata done "
        f"seconds={summary['timings']['channel_metadata_seconds']:.3f}",
        flush=True,
    )

    summary["channel_metadata"] = channel_metadata
    summary["errors"] += channel_metadata.get("errors", 0)
    mark_progress("channel_metadata", "completed")
    if stop_after_collection("channel_metadata", channel_metadata):
        return summary

    if stop_for_handoff():
        return summary
    mark_progress("typed_inventory")
    phase_start = time.perf_counter()
    # Queue initialization belongs to the atomic page settlement for every type.
    # Observe its confirmed identities without a second priority write: that
    # write could otherwise reset a deadline assigned after the page committed.
    # The legacy limit now bounds only the diagnostic ID sample, not collection.
    discovery_results = []
    observed_new_ids = []
    selected_new_ids = set()
    observed_new_count = 0
    discovery_totals = dict(processed=0, new_videos_found=0, no_new_videos=0,
        availability_recheck_triggered=0, availability_recheck_restored=0, errors=0)

    def after_inventory_page(result):
        nonlocal observed_new_count
        scope = validate_channel_scopes([result.get("channel_scope")],
                                       channel_ids=[result["channel_id"]])[0]
        new_ids = result.get("new_video_ids") or []
        observed_new_count += len(new_ids)
        saved = []
        for video_id in new_ids:
            if len(selected_new_ids) >= new_video_stats_limit:
                break
            if video_id not in selected_new_ids:
                selected_new_ids.add(video_id)
                observed_new_ids.append(video_id)
                saved.append(video_id)
        # Keep the historical Long-only discovery projection explicit. The
        # authoritative all-type insert totals are in typed_inventory.
        if result.get("video_type") != "long":
            return
        discovery_totals["processed"] += 1
        discovery_totals["new_videos_found"] += len(new_ids)
        if saved:
            discovery_results.append(dict(channel_scope=scope, new_video_ids=saved))
        if result.get("status") != "completed":
            return
        # The committed scan counter includes earlier pages and earlier passes;
        # an empty terminal page alone cannot prove that nothing new was found.
        no_new_videos = result.get("full_generation_new_count") == 0
        if no_new_videos:
            discovery_totals["no_new_videos"] += 1
        # Inventory owns membership evidence, not an extra availability loop.
        # General/Stats scheduling owns all video refresh and missing retries.

    try:
        typed_inventory = (common_dispatch.inventory if dispatched else run_typed_playlist_inventory_batch)(
            limit=inventory_page_budget,
            run_id=sync_run_id,
            on_page=after_inventory_page,
            context=context,
        )
    except Exception as error:
        typed_inventory = {
            "enabled": context.settings.typed_playlist_inventory_enabled,
            "prepared": 0,
            "claimed": 0,
            "processed": 0,
            "completed": 0,
            "continuations": 0,
            "playlist_not_found": 0,
            "inserted": 0,
            "already_matching": 0,
            "conflicts": 0,
            "errors": 1,
            "results": [],
            "error_type": error.__class__.__name__,
        }
        if dispatched:typed_inventory.update(settlement_unknown=True,infrastructure_error=True)
        print(
            source_log_message('auto_typed_inventory_error', lambda: ("[auto-sync] error typed_inventory failed: "
            f"{error.__class__.__name__}"), error=error),
            flush=True,
        )
    summary["typed_inventory"] = typed_inventory
    channel_discovery = {**discovery_totals, "results": discovery_results,
                         "routing": {"mode": "shared_inventory_long_projection"}}
    summary["channel_discovery"] = channel_discovery
    summary["errors"] += int(typed_inventory.get("errors") or 0) + discovery_totals["errors"]
    summary["timings"]["typed_inventory_seconds"] = (
        time.perf_counter() - phase_start
    )
    mark_progress("typed_inventory", "completed")
    if stop_after_collection("typed_inventory", typed_inventory):
        return summary

    if stop_for_handoff():
        return summary
    # Retain historical summary fields for run readers, without an executable
    # loss phase. A lower channel count must never force video requests.
    long_availability_loss = {"triggered": 0, "processed": 0, "updated": 0,
        "unavailable_marked": 0, "errors": 0, "results": []}
    summary["timings"]["long_availability_loss_seconds"] = 0.0
    summary["long_availability_loss"] = long_availability_loss
    # Compatibility progress field only: the Long work was committed by the
    # shared pass above. There is no second discovery claim or provider loop.
    summary["timings"]["channel_discovery_seconds"] = 0.0
    mark_progress("channel_discovery", "completed")

    if stop_for_handoff():
        return summary
    mark_progress("collect_new_video_ids")
    phase_start = time.perf_counter()
    summary["new_video_ids"] = observed_new_ids
    summary["new_video_ids_omitted"] = max(observed_new_count - len(observed_new_ids), 0)
    summary["timings"]["collect_new_video_ids_seconds"] = time.perf_counter() - phase_start
    print(
        "[auto-sync] phase collect_new_video_ids done "
        f"seconds={summary['timings']['collect_new_video_ids_seconds']:.3f}",
        flush=True,
    )
    mark_progress("collect_new_video_ids", "completed")

    if stop_for_handoff():
        return summary
    mark_progress("new_video_stats")
    phase_start = time.perf_counter()
    # Compatibility phase only. Inventory has already initialized eligible IDs
    # inside its receipt transaction; conflicts and held channels keep their
    # existing policy. Never re-enqueue them or overwrite later retry deadlines.
    summary["new_video_stats"] = {
        "reason": "inventory_queue_owned",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "requested": 0, "enqueued": 0, "updated": 0, "batches": 0, "errors": 0,
        "video_ids": [], "error_messages": [],
        "routing": {"mode": "inventory_owned_no_followup", "scope": "all_types"},
    }
    summary["timings"]["new_video_stats_seconds"] = time.perf_counter() - phase_start
    print(
        "[auto-sync] phase new_video_stats done "
        f"seconds={summary['timings']['new_video_stats_seconds']:.3f}",
        flush=True,
    )
    mark_progress("new_video_stats", "completed")

    typed_inventory_quota_exhausted = bool(
        (summary.get("typed_inventory") or {}).get("quota_exhausted")
    )

    if stop_for_handoff():
        return summary
    mark_progress("short_video_collection")
    phase_start = time.perf_counter()
    if short_video_collection_limit <= 0:
        short_video_collection = {
            "enabled": False,
            "video_type": "short",
            "requested": 0,
            "claimed": 0,
            "updated": 0,
            "missing": 0,
            "unavailable_marked": 0,
            "errors": 0,
            "quota_exhausted": False,
            "skipped_reason": "owned_by_common_pool",
            "video_ids": [],
        }
    elif typed_inventory_quota_exhausted:
        short_video_collection = {
            "enabled": context.policy.short_video_collection_enabled,
            "video_type": "short",
            "requested": 0,
            "claimed": 0,
            "updated": 0,
            "missing": 0,
            "unavailable_marked": 0,
            "errors": 0,
            "quota_exhausted": True,
            "skipped_reason": "quota_exhausted_previous_phase",
            "video_ids": [],
        }
    else:
        try:
            short_video_collection = run_typed_video_collection_queue_batch(
                video_type="short",
                limit=short_video_collection_limit,
                run_id=sync_run_id,
                context=context,
            )
        except Exception as error:
            short_video_collection = {
                "enabled": context.policy.short_video_collection_enabled,
                "video_type": "short",
                "requested": 0,
                "claimed": 0,
                "updated": 0,
                "missing": 0,
                "unavailable_marked": 0,
                "errors": 1,
                "quota_exhausted": False,
                "video_ids": [],
                "error_messages": [str(error)],
            }
            print(
                source_log_message('auto_short_collection_error', lambda: (f"[auto-sync] error short_video_collection failed: {error}"), error=error),
                flush=True,
            )
    summary["short_video_collection"] = short_video_collection
    summary["errors"] += int(short_video_collection.get("errors") or 0)
    summary["timings"]["short_video_collection_seconds"] = (
        time.perf_counter() - phase_start
    )
    mark_progress("short_video_collection", "completed")

    typed_quota_exhausted = bool(
        typed_inventory_quota_exhausted
        or short_video_collection.get("quota_exhausted")
    )
    if stop_for_handoff():
        return summary
    mark_progress("completed_stream_collection")
    phase_start = time.perf_counter()
    if completed_stream_collection_limit <= 0:
        completed_stream_collection = {
            "enabled": False,
            "video_type": "stream",
            "requested": 0,
            "claimed": 0,
            "updated": 0,
            "missing": 0,
            "unavailable_marked": 0,
            "errors": 0,
            "quota_exhausted": False,
            "skipped_reason": "owned_by_common_pool",
            "video_ids": [],
        }
    elif typed_quota_exhausted:
        completed_stream_collection = {
            "enabled": context.policy.completed_stream_collection_enabled,
            "video_type": "stream",
            "requested": 0,
            "claimed": 0,
            "updated": 0,
            "missing": 0,
            "unavailable_marked": 0,
            "errors": 0,
            "quota_exhausted": True,
            "skipped_reason": "quota_exhausted_previous_phase",
            "video_ids": [],
        }
    else:
        try:
            completed_stream_collection = run_typed_video_collection_queue_batch(
                video_type="stream",
                limit=completed_stream_collection_limit,
                run_id=sync_run_id,
                context=context,
            )
        except Exception as error:
            completed_stream_collection = {
                "enabled": context.policy.completed_stream_collection_enabled,
                "video_type": "stream",
                "requested": 0,
                "claimed": 0,
                "updated": 0,
                "missing": 0,
                "unavailable_marked": 0,
                "errors": 1,
                "quota_exhausted": False,
                "video_ids": [],
                "error_messages": [str(error)],
            }
            print(
                source_log_message('auto_stream_collection_error', lambda: (f"[auto-sync] error completed_stream_collection failed: {error}"), error=error),
                flush=True,
            )
    summary["completed_stream_collection"] = completed_stream_collection
    summary["errors"] += int(completed_stream_collection.get("errors") or 0)
    summary["timings"]["completed_stream_collection_seconds"] = (
        time.perf_counter() - phase_start
    )
    mark_progress("completed_stream_collection", "completed")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["timings"]["total_seconds"] = time.perf_counter() - cycle_start
    mark_progress("completed", "completed")

    availability_recheck_restored = int(
        (summary["channel_discovery"] or {}).get("availability_recheck_restored") or 0
    )
    stats_updated = (
        (summary["new_video_stats"] or {}).get("updated", 0)
        + (summary["long_availability_loss"] or {}).get("updated", 0)
        + video_collection.get("updated", 0)
        + availability_recheck_restored
    )
    print(
        "[auto-sync] done "
        f"metadata_updated={channel_metadata.get('updated', 0)} "
        f"video_count_changed={channel_metadata.get('video_count_changed', 0)} "
        f"typed_inserted={typed_inventory.get('inserted', 0)} "
        f"typed_conflicts={typed_inventory.get('conflicts', 0)} "
        f"video_count_decreased={channel_metadata.get('video_count_decreased', 0)} "
        f"long_loss_marked={long_availability_loss.get('unavailable_marked', 0)} "
        f"channels_processed={channel_discovery.get('processed', 0)} "
        f"new_videos={new_video_count_from_summary(summary)} "
        f"shorts_updated={video_collection.get('short_updated', 0)} "
        f"completed_streams_updated={video_collection.get('stream_updated', 0)} "
        f"availability_recheck_restored={availability_recheck_restored} "
        f"stats_updated={stats_updated} "
        f"errors={summary['errors']} "
        f"seconds_total={summary['timings']['total_seconds']:.3f}",
        flush=True,
    )
    return summary




def run_auto_sync_cycle(stats_limit=50, new_video_stats_limit=50, metadata_limit=150, short_video_collection_limit=0, completed_stream_collection_limit=0, sync_run_id=None, *, inventory_page_budget, context: CollectionContext, common_dispatch=None):
    """Observe the existing whole pass without adding an execution owner."""
    with scope("auto_sync", sync_run_id=sync_run_id, reason="scheduled") as trace:
        result = _run_auto_sync_cycle(stats_limit=stats_limit,
            new_video_stats_limit=new_video_stats_limit, metadata_limit=metadata_limit,
            short_video_collection_limit=short_video_collection_limit,
            completed_stream_collection_limit=completed_stream_collection_limit,
            sync_run_id=sync_run_id, inventory_page_budget=inventory_page_budget,
            context=context, common_dispatch=common_dispatch)
        outcome = result.get("completion_outcome", "unknown")
        trace.finish(outcome, reason=result.get("stopped_reason"), count=result.get("errors"))
        return result
