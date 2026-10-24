"""Availability follow-ups using the existing shared video lease/write owner.

Long signals retain their bounded recovery policy. Typed missing is already
scheduled by the common queue; this module adds no typed fanout.
"""
from datetime import timedelta, timezone
from functools import partial

from availability import AVAILABILITY_PRIVATE, AVAILABILITY_UNKNOWN_UNAVAILABLE, is_public_availability_status
from channel_collection_scope import require_control_epoch, validate_channel_scopes
from channel_sync_policy import channel_sync_eligibility
from collection.context import CollectionContext
from collection.provider_steps import run_provider_steps
from collection_outcome import collection_outcome_fields, copy_collection_outcome, copy_collection_stop as _copy_collection_stop, sync_completion_fields
from collection_shutdown import stop_at_boundary
from db import AvailabilityRepository, ChannelDiscoveryRepository
from source_log import source_log_message


INITIAL_LONG_INVENTORY_COMPLETED = "completed"


CHANNEL_DISCOVERY_SKIPPED_BASELINE_INCOMPLETE = "skipped_baseline_incomplete"


AVAILABILITY_RECHECK_LIMIT_PER_CHANNEL = 50


LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT = 250


AVAILABILITY_RECHECK_STATUSES = (
    AVAILABILITY_PRIVATE,
    AVAILABILITY_UNKNOWN_UNAVAILABLE,
)


AVAILABILITY_STALE_RECHECK_AFTER = timedelta(days=1)


def should_run_stale_availability_recheck(discovery_result):
    """A settled head scan with nothing new may retry old unavailable misses."""
    return (
        discovery_result.get("scan_kind") != "full_reconciliation"
        and discovery_result.get("status") == "no_new_videos"
    )


def load_initial_long_inventory_status(channel_id, *, context: CollectionContext):
    row = ChannelDiscoveryRepository(context.database).load_initial_inventory_status(channel_id)
    if not row:
        return None
    if channel_sync_eligibility(row).allow_discovery:
        return INITIAL_LONG_INVENTORY_COMPLETED
    return row.get("initial_long_inventory_status")


def load_channel_discovery_timing(channel_id, *, context: CollectionContext):
    try:
        return ChannelDiscoveryRepository(context.database).load_timing(channel_id)
    except Exception as error:
        print(
            source_log_message('availability_timing_error', lambda: ("[availability-recheck] channel timing unavailable "
            f"channel_id={channel_id} error={error}"), error=error),
            flush=True,
        )
        return {}


def channel_video_count_changed_since_last_discovery(channel_id, *, context: CollectionContext):
    timing = load_channel_discovery_timing(channel_id, context=context)
    changed_at = context.sources.parse_dt(timing.get("channel_video_count_changed_at"))
    if not changed_at:
        return False
    checked_at = context.sources.parse_dt(timing.get("channel_discovery_checked_at"))
    if checked_at and checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    return checked_at is None or changed_at > checked_at


def load_availability_recheck_candidate_ids(channel_id, limit=AVAILABILITY_RECHECK_LIMIT_PER_CHANNEL, recheck_after=None, *, context: CollectionContext):
    if limit <= 0:
        return []
    return AvailabilityRepository(context.database).load_recheck_candidate_ids(
        channel_id,
        AVAILABILITY_RECHECK_STATUSES,
        limit=limit,
        recheck_after=recheck_after,
    )


def should_trigger_availability_recheck(channel_id, discovery_result, *, context: CollectionContext):
    if discovery_result.get("scan_kind") == "full_reconciliation":
        return bool(discovery_result.get("scan_complete")
                    and discovery_result.get("pending_channel_count_change")
                    and discovery_result.get("full_generation_new_count") == 0
                    and int(discovery_result.get("full_generation_items_seen") or 0) > 0)
    if discovery_result.get("status") != "no_new_videos":
        return False
    if not discovery_result.get("latest_uulf_known"):
        return False
    if "pending_channel_count_change" in discovery_result:
        return discovery_result["pending_channel_count_change"] is True
    return channel_video_count_changed_since_last_discovery(channel_id, context=context)


def run_channel_availability_recheck(channel_id, discovery_result, trigger_allowed=True, *, context: CollectionContext, expected_epoch):
    return run_provider_steps(availability_recheck_steps(
        channel_id, discovery_result, trigger_allowed, context=context,
        expected_epoch=expected_epoch, run_video_steps=partial(_sync_video_steps, context),
    ))


def _sync_video_steps(context, **kwargs):
    """Legacy driver only; dispatched callers supply addressed_video_steps."""
    yield from ()
    return context.video.run_batch(**kwargs)


def availability_recheck_steps(channel_id, discovery_result, trigger_allowed=True, *,
                               context: CollectionContext, expected_epoch, run_video_steps):
    expected_epoch = require_control_epoch(expected_epoch)
    summary = {
        "triggered": False,
        "status": "not_triggered",
        "requested": 0,
        "restored": 0,
        "missing": 0,
        "errors": 0,
        "video_ids": [],
        "error_messages": [],
    }
    if trigger_allowed and should_trigger_availability_recheck(channel_id, discovery_result, context=context):
        summary["trigger"] = "video_count_changed"
        candidate_ids = load_availability_recheck_candidate_ids(channel_id, context=context)
    elif should_run_stale_availability_recheck(discovery_result):
        # Without a count change, only misses old enough to retry; an empty
        # selection is not a triggered recheck.
        candidate_ids = load_availability_recheck_candidate_ids(
            channel_id, recheck_after=AVAILABILITY_STALE_RECHECK_AFTER,
            context=context,
        )
        if not candidate_ids:
            if not trigger_allowed:
                summary["status"] = "not_triggered_no_increase_signal"
            return summary
        summary["trigger"] = "stale_unavailable"
    else:
        if not trigger_allowed:
            summary["status"] = "not_triggered_no_increase_signal"
        return summary

    summary["triggered"] = True
    summary["requested"] = len(candidate_ids)
    if not candidate_ids:
        summary["status"] = "no_candidates"
        print(
            source_log_message('availability_no_candidates', lambda: ("[availability-recheck] no candidates "
            f"channel_id={channel_id} latest_uulf={discovery_result.get('latest_uulf_video_id')}")),
            flush=True,
        )
        return summary

    stats_summary = yield from run_video_steps(
        limit=len(candidate_ids),
        explicit_video_ids=candidate_ids,
        reason="availability_recheck",
        force_metadata=True,
        channel_scopes=[{"channel_id": channel_id, "control_epoch": expected_epoch}],
    )
    stats_summary.update(collection_outcome_fields(stats_summary, explicit_selected=len(candidate_ids)))
    copy_collection_outcome(summary, stats_summary)
    restored_ids = stats_summary.get("video_ids") or []
    summary.update({
        "status": "restored" if restored_ids else "no_restored_videos",
        "restored": len(restored_ids),
        "missing": int(stats_summary.get("missing") or 0),
        "errors": int(stats_summary.get("errors") or 0),
        "video_ids": restored_ids,
        "error_messages": stats_summary.get("error_messages") or [],
    })
    if _copy_collection_stop(summary, stats_summary):
        summary["status"] = "stopped"
    elif summary["errors"]:
        summary["status"] = "error"
    elif summary.get("incomplete"):
        summary["status"] = "partial"
    print(
        source_log_message('availability_completed', lambda: ("[availability-recheck] done "
        f"channel_id={channel_id} requested={summary['requested']} "
        f"restored={summary['restored']} missing={summary['missing']} "
        f"errors={summary['errors']}"), counts=summary),
        flush=True,
    )
    return summary


def load_long_availability_loss_candidate_ids(channel_id, limit=LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT, *, context: CollectionContext):
    if limit <= 0:
        return []
    repository = AvailabilityRepository(context.database)
    inventory_ids = repository.load_long_inventory_candidate_ids(
        channel_id,
        limit=limit,
    )
    if not inventory_ids:
        return []

    public_ids = set()
    for row in repository.load_video_availability_rows(inventory_ids):
        video_id = row.get("video_id")
        if video_id and is_public_availability_status(row.get("availability_status")):
            public_ids.add(video_id)

    return [video_id for video_id in inventory_ids if video_id in public_ids]


def load_video_availability_status_counts(video_ids, *, context: CollectionContext):
    counts = {}
    rows = AvailabilityRepository(context.database).load_video_availability_rows(video_ids)
    for row in rows:
        status = row.get("availability_status") or "<null>"
        counts[status] = counts.get(status, 0) + 1
    return counts


def run_channel_long_availability_loss_check(channel_id, *, context: CollectionContext, expected_epoch):
    return run_provider_steps(availability_loss_steps(
        channel_id, context=context, expected_epoch=expected_epoch,
        run_video_steps=partial(_sync_video_steps, context),
    ))


def availability_loss_steps(channel_id, *, context: CollectionContext, expected_epoch, run_video_steps):
    expected_epoch = require_control_epoch(expected_epoch)
    summary = {
        "triggered": True,
        "status": "not_started",
        "channel_id": channel_id,
        "candidate_limit": LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT,
        "candidates": 0,
        "requested": 0,
        "updated": 0,
        "missing": 0,
        "unavailable_marked": 0,
        "unlisted": 0,
        "public": 0,
        "errors": 0,
        "video_ids": [],
        "missing_video_ids": [],
        "availability_status_counts": {},
        "error_messages": [],
    }

    initial_status = load_initial_long_inventory_status(channel_id, context=context)
    if initial_status != INITIAL_LONG_INVENTORY_COMPLETED:
        summary["status"] = CHANNEL_DISCOVERY_SKIPPED_BASELINE_INCOMPLETE
        return summary

    candidate_ids = load_long_availability_loss_candidate_ids(
        channel_id,
        limit=LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT,
        context=context,
    )
    summary["candidates"] = len(candidate_ids)
    summary["requested"] = len(candidate_ids)
    if not candidate_ids:
        summary["status"] = "no_candidates"
        print(
            source_log_message('long_loss_no_candidates', lambda: ("[channel-long-availability-loss] no candidates "
            f"channel_id={channel_id}")),
            flush=True,
        )
        return summary

    stats_summary = yield from run_video_steps(
        limit=len(candidate_ids),
        explicit_video_ids=candidate_ids,
        reason="long_availability_loss",
        force_metadata=True,
        channel_scopes=[{"channel_id": channel_id, "control_epoch": expected_epoch}],
    )
    stats_summary.update(collection_outcome_fields(stats_summary, explicit_selected=len(candidate_ids)))
    copy_collection_outcome(summary, stats_summary)
    returned_ids = stats_summary.get("video_ids") or []
    stopped = _copy_collection_stop(summary, stats_summary)
    status_counts = (
        {} if stopped else load_video_availability_status_counts(returned_ids, context=context)
    )

    summary.update({
        "updated": int(stats_summary.get("updated") or 0),
        "missing": int(stats_summary.get("missing") or 0),
        "unavailable_marked": int(stats_summary.get("unavailable_marked") or 0),
        "unlisted": int(status_counts.get("unlisted") or 0),
        "public": int(status_counts.get("public") or 0),
        "errors": int(stats_summary.get("errors") or 0),
        "video_ids": returned_ids,
        "missing_video_ids": stats_summary.get("missing_video_ids") or [],
        "availability_status_counts": status_counts,
        "error_messages": stats_summary.get("error_messages") or [],
    })
    if stopped:
        summary["status"] = "stopped"
    elif summary["errors"]:
        summary["status"] = "error"
    elif summary.get("incomplete"):
        summary["status"] = "partial"
    elif summary["unavailable_marked"] or summary["unlisted"]:
        summary["status"] = "detected"
    else:
        summary["status"] = "no_long_loss_detected"

    print(
        source_log_message('long_loss_completed', lambda: ("[channel-long-availability-loss] done "
        f"channel_id={channel_id} status={summary['status']} "
        f"candidates={summary['candidates']} candidate_limit={summary['candidate_limit']} "
        f"updated={summary['updated']} "
        f"missing={summary['missing']} unavailable_marked={summary['unavailable_marked']} "
        f"unlisted={summary['unlisted']} errors={summary['errors']}"), counts=summary),
        flush=True,
    )
    return summary


def run_channel_long_availability_loss_batch(channel_ids, *, context: CollectionContext, channel_scopes):
    return run_provider_steps(availability_loss_batch_steps(
        channel_ids, context=context, channel_scopes=channel_scopes,
        run_video_steps=partial(_sync_video_steps, context),
    ))


def availability_loss_batch_steps(channel_ids, *, context: CollectionContext, channel_scopes, run_video_steps):
    unique_channel_ids = []
    seen = set()
    for channel_id in channel_ids or []:
        if not channel_id or channel_id in seen:
            continue
        seen.add(channel_id)
        unique_channel_ids.append(channel_id)
    captured = validate_channel_scopes(channel_scopes, channel_ids=unique_channel_ids)
    epochs = {scope["channel_id"]: scope["control_epoch"] for scope in captured}

    summary = {
        "triggered": len(unique_channel_ids),
        "processed": 0,
        "detected": 0,
        "no_long_loss_detected": 0,
        "skipped": 0,
        "candidate_limit": LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT,
        "candidates": 0,
        "requested": 0,
        "updated": 0,
        "missing": 0,
        "unavailable_marked": 0,
        "unlisted": 0,
        "errors": 0,
        "results": [],
        "error_messages": [],
    }
    if not unique_channel_ids:
        return summary

    for channel_id in unique_channel_ids:
        if stop_at_boundary(summary):
            break
        try:
            result = yield from availability_loss_steps(channel_id, context=context,
                expected_epoch=epochs[channel_id], run_video_steps=run_video_steps)
        except Exception as error:
            result = {
                "triggered": True,
                "status": "error",
                "channel_id": channel_id,
                "candidates": 0,
                "requested": 0,
                "updated": 0,
                "missing": 0,
                "unavailable_marked": 0,
                "unlisted": 0,
                "errors": 1,
                "video_ids": [],
                "missing_video_ids": [],
                "error_messages": [str(error)],
            }
            print(
                source_log_message('long_loss_error', lambda: ("[channel-long-availability-loss] error "
                f"channel_id={channel_id} error={error}"), error=error),
                flush=True,
            )

        summary["processed"] += 1
        status = result.get("status")
        if status == "detected":
            summary["detected"] += 1
        elif status == "no_long_loss_detected":
            summary["no_long_loss_detected"] += 1
        elif status in {CHANNEL_DISCOVERY_SKIPPED_BASELINE_INCOMPLETE, "no_candidates"}:
            summary["skipped"] += 1

        for key in (
            "candidates",
            "requested",
            "updated",
            "missing",
            "unavailable_marked",
            "unlisted",
            "errors",
        ):
            summary[key] += int(result.get(key) or 0)
        summary["error_messages"].extend(result.get("error_messages") or [])
        summary["results"].append(result)
        if _copy_collection_stop(summary, result):
            break

    summary.update(sync_completion_fields(summary))
    return summary



class AddressedAvailabilityUnit:
    """One bounded Long follow-up on the existing addressed video owner.

    A unit retains one Coordinator credit across at most five <=50-video
    batches (loss policy250, recheck50). Only its current known claims can be
    drained. An unknown queue/claim/write/observer never acknowledges or retries.
    """
    def __init__(self, context, service, *, key, channel_id, expected_epoch, kind,
                 require_ready, on_settled, should_stop=lambda: False,
                 discovery_result=None, trigger_allowed=True, worker_id='availability',
                 clock=None):
        from collection.dispatch import Work
        import time
        from threading import get_ident
        if (kind not in ('recheck','loss') or any(not callable(value) for value in
                (require_ready,on_settled,should_stop))):
            raise ValueError('availability_dispatch_config_invalid')
        self.scope=validate_channel_scopes([dict(channel_id=channel_id,control_epoch=expected_epoch)],channel_ids=[channel_id])[0]
        self.context,self.service,self.kind=context,service,kind
        self.require_ready,self.on_settled,self.should_stop=require_ready,on_settled,should_stop
        self.discovery_result,self.trigger_allowed=discovery_result,trigger_allowed
        self.worker_id,self.clock=worker_id,clock or time.monotonic
        self.claims=();self.steps=self.current_step=self.deadline=None
        self.summary={};self.partial_summary={};self.selected_count=0
        self.phase='candidate';self._thread=get_ident()
        self.work=Work(key,'auto',channel_id,'long',self.prepare,self.settle,
                       exclusion_key='availability:'+channel_id)

    def _owner(self):
        from threading import get_ident
        if get_ident()!=self._thread:raise RuntimeError('availability_dispatch_owner_required')

    def _claimed(self, claims):
        self._owner()
        if self.claims or len(claims)>50:
            raise RuntimeError('availability_dispatch_claim_overlap')
        self.claims=tuple(claims)

    def _batch_settled(self, summary):
        self._owner()
        if summary.get('settlement_unknown'):
            raise RuntimeError('availability_dispatch_settlement_unknown')
        self.claims=()

    def _video_steps(self, **options):
        from video_collection_service import addressed_video_steps
        if (not 0<=options['limit']<=LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT
                or options.get('channel_scopes') != [self.scope]):
            raise ValueError('availability_dispatch_scope_invalid')
        self.selected_count=options['limit']
        return addressed_video_steps(self.service.repository,self.service,worker_id=self.worker_id,
            on_claimed=self._claimed,on_settled=self._batch_settled,
            should_stop=self.should_stop,summary=self.partial_summary,**options)

    def prepare(self):
        from collection.dispatch import Ack,Skipped
        from collection_shutdown import handoff_requested
        from video_collection_service import DEFAULT_LEASE_SECONDS
        self._owner()
        if self.phase!='candidate':raise RuntimeError('availability_dispatch_prepare_repeated')
        if self.should_stop() or handoff_requested():return Skipped()
        from hot_runtime import HotRuntimeUnavailable
        try:self.require_ready()
        except HotRuntimeUnavailable:
            self._ack(dict(errors=0,yield_requested=True,incomplete=True,
                hot_runtime_interrupted=True,stopped_reason='hot_runtime_interrupted'))
            return Skipped()
        self.deadline=self.clock()+DEFAULT_LEASE_SECONDS-1
        options=dict(context=self.context,expected_epoch=self.scope['control_epoch'],run_video_steps=self._video_steps)
        self.steps=(availability_recheck_steps(self.scope['channel_id'],self.discovery_result,
                    self.trigger_allowed,**options) if self.kind=='recheck'
                    else availability_loss_steps(self.scope['channel_id'],**options))
        result=self._advance()
        return Skipped() if isinstance(result,Ack) else result

    def _ack(self, summary):
        from collection.dispatch import Ack
        if self.claims or summary.get('settlement_unknown'):
            self.phase='settlement_unknown'
            raise RuntimeError('availability_dispatch_settlement_unknown')
        self.summary=summary;self.phase='progress'
        self.on_settled(summary)
        self.phase='acknowledged'
        return Ack()

    def _advance(self, response=None):
        from collection.dispatch import Prepared
        from collection.provider_steps import ProviderStep
        from hot_runtime import HotRuntimeUnavailable
        self.phase='settling'
        try:self.current_step=next(self.steps) if response is None else self.steps.send(response)
        except StopIteration as done:return self._ack(done.value)
        if not isinstance(self.current_step,ProviderStep):
            raise TypeError('availability_dispatch_provider_step_required')
        if self.should_stop():return self._drain('collection_stopped')
        try:self.require_ready()
        except HotRuntimeUnavailable:return self._drain('hot_runtime_interrupted')
        self.phase='prepared'
        return Prepared(self.fetch,self.deadline,len(self.current_step.identities))

    def _drain(self, reason):
        from collection.initial_video_hydration import _drain_unsent
        self.phase='releasing'
        if self.claims:_drain_unsent(self.service.repository,self.claims)
        self.claims=()
        self.steps.close()
        # Generator close only copies its current partial batch into this shared
        # accumulator; it never completes/fails rows or repeats a known ACK.
        summary=dict(self.partial_summary)
        summary.update(triggered=True,status='stopped',requested=self.selected_count,
            yield_requested=True,stopped_reason=reason,
            hot_runtime_interrupted=reason=='hot_runtime_interrupted')
        if self.kind=='recheck':summary['restored']=len(summary.get('video_ids') or [])
        else:summary.update(channel_id=self.scope['channel_id'],candidates=self.selected_count,
            candidate_limit=LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT,availability_status_counts={})
        summary.update(collection_outcome_fields(summary,explicit_selected=self.selected_count))
        return self._ack(summary)

    def fetch(self, cancelled):
        return self.current_step.execute()

    def settle(self, completion):
        from collection.provider_steps import ProviderResult
        self._owner()
        if self.phase!='prepared':raise RuntimeError('availability_dispatch_settle_repeated')
        if completion.phase in ('cancelled','expired') and not completion.fetch_started:
            return self._drain(completion.phase)
        if completion.phase!='response' or not isinstance(completion.value,ProviderResult):
            raise RuntimeError('availability_dispatch_response_unknown')
        return self._advance(completion.value)


class AvailabilityLossFeeder:
    """Offer the already bounded metadata decrease handoff without preclaiming."""
    def __init__(self, context, service, *, channel_scopes, require_ready, should_stop,
                 on_settled, worker_id):
        from uuid import uuid4
        self.scopes=validate_channel_scopes(channel_scopes,channel_ids=[row['channel_id'] for row in channel_scopes])
        self.context,self.service=context,service
        self.require_ready,self.should_stop,self.on_settled=require_ready,should_stop,on_settled
        self.worker_id=worker_id;self._identity=str(uuid4());self._position=0;self._keys=set()
        self.paused=self.exhausted=False
        self.summary=dict(triggered=len(self.scopes),processed=0,detected=0,no_long_loss_detected=0,
            skipped=0,candidate_limit=LONG_AVAILABILITY_LOSS_CANDIDATE_LIMIT,candidates=0,requested=0,
            updated=0,missing=0,unavailable_marked=0,unlisted=0,errors=0,results=[],error_messages=[])

    def pause(self, reason):
        self.paused=True;self.summary['stopped_reason']=reason

    def _settled(self, result):
        self.summary['processed']+=1
        status=result.get('status')
        if status in ('detected','no_long_loss_detected'):self.summary[status]+=1
        elif status in (CHANNEL_DISCOVERY_SKIPPED_BASELINE_INCOMPLETE,'no_candidates'):self.summary['skipped']+=1
        for name in ('candidates','requested','updated','missing','unavailable_marked','unlisted','errors'):
            self.summary[name]+=int(result.get(name) or 0)
        self.summary['results'].append(result)
        self.summary['error_messages'].extend((result.get('error_messages') or [])[:max(0,50-len(self.summary['error_messages']))])
        _copy_collection_stop(self.summary,result)
        self.on_settled(result)

    def offer_to(self, coordinator, *, limit=None):
        if limit is not None and (type(limit) is not int or not 0<=limit<=100):
            raise ValueError('availability_feeder_offer_limit_invalid')
        if limit==0:return 0
        self._keys.intersection_update(coordinator.keys())
        self.exhausted=self._position==len(self.scopes) and not self._keys
        status=coordinator.snapshot()
        if self.paused or self.should_stop() or status.paused or status.stopped or status.blocked:return 0
        allowance=min(2*coordinator.concurrency-status.ready,100 if limit is None else limit)
        offered=0
        while offered<allowance and self._position<len(self.scopes):
            scope=self.scopes[self._position]
            key=f'availability-loss:{self._identity}:{self._position}'
            unit=AddressedAvailabilityUnit(self.context,self.service,key=key,kind='loss',
                channel_id=scope['channel_id'],expected_epoch=scope['control_epoch'],
                require_ready=self.require_ready,should_stop=self.should_stop,on_settled=self._settled,
                worker_id=self.worker_id)
            coordinator.offer(unit.work);self._keys.add(key);self._position+=1;offered+=1
        return offered

    def finish_pass(self, coordinator):
        self._keys.intersection_update(coordinator.keys())
        if self._keys or coordinator.snapshot().blocked or not (self.exhausted or self.paused):
            raise RuntimeError('availability_feeder_release_unconfirmed')
        self.summary.update(sync_completion_fields(self.summary))
