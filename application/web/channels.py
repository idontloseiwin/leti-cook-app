"""Channel addition, setup and durable manual job HTTP adapters."""
from db.channel.channel_trash import ChannelTrashRepository
from db.user_work.reconnect_admission import configured_channel_add_admission, ChannelAddAdmissionUnavailable
from fastapi import BackgroundTasks, HTTPException, APIRouter
from quota_guards import (
    CHANNEL_ADD_WINDOWS,
    ChannelLookupCache,
    RateLimiter,
    backfill_cooldown_seconds,
    wait_text,
)
from db.channel.channel_sync import ChannelCommandRepository
from db.reads.frontend import FrontendRepository
from db.jobs.operator_job_read_queries import get_operator_job_by_id
from hot_storage import capture_read_lifetime, register_read_deadline
from hot_runtime import require_hot_runtime_ready, runtime_collection_access
from youtube_errors import KEY_REFUSALS
from starlette.responses import Response
from self_hosted_configuration import STARTER_CHANNEL_URLS as _STARTER_CHANNEL_URLS
from collection.api import initial_hydration_payload as _initial_hydration_payload
from channel_collection_scope import channel_scope
from channels import service as channel_service, sources as channel_sources
from channel_sync_policy import channel_sync_eligibility
from collection import api as collection_api
from datetime import datetime, timezone
from operator_jobs import load_operator_job_settings, operator_job_snapshot, operator_jobs_mode
from starlette.concurrency import run_in_threadpool
import threading
from youtube_client import youtube_api_get
from .models import ChannelRequest, InitialHydrationRequest
from .constants import OPERATOR_JOBS_MODE_INVALID, VISIBLE_CHANNEL_FIELDS
from typing import Callable
from dataclasses import dataclass

# Only the durable command receipt's local identity/control/progress is retained
# if the independent reader cannot load its source-bearing display projection.
_JOB_RECEIPT_FIELDS = frozenset((
    "id", "channel_id", "job_type", "status", "phase", "requested_at",
    "started_at", "updated_at", "finished_at", "accepted_epoch",
    "initial_video_types", "initial_scope_version", "targets_frozen_at",
    "inventory_video_type", "target_count", "processed_count", "updated_count",
    "unavailable_count", "skipped_count", "error_count", "quota_units_estimated",
    "quota_units_reserved", "quota_units_observed_estimate", "max_items", "max_pages",
    "max_quota_units", "pages_processed", "items_processed", "inventory_inserted_count",
    "inventory_updated_count",
))


def _source_free_job_receipt(row):
    result = {key: row[key] for key in _JOB_RECEIPT_FIELDS if key in row}
    result.update(summary_json={}, resume_page_token=None, last_error_code=None,
                  last_error_summary=None)
    return result


def _read_committed_job(database, receipt):
    fallback = _source_free_job_receipt(receipt)
    with capture_read_lifetime() as lifetime:
        try:
            current = get_operator_job_by_id(
                database, receipt["id"], channel_id=receipt["channel_id"], job_type=receipt["job_type"],
            )
        except Exception:
            return fallback
        if (current is None or current.get("id") != receipt.get("id")
                or current.get("channel_id") != receipt.get("channel_id")
                or current.get("job_type") != receipt.get("job_type")):
            return fallback
    if lifetime.expires_at is not None:
        register_read_deadline(lifetime.expires_at)
    return current


@dataclass(frozen=True)
class ChannelHttp:
    add_router: APIRouter
    backfill_router: APIRouter
    hydration_router: APIRouter
    resolve_user_work_channel: Callable
    activate_restored_user_channel: Callable

def _require_collection_ready():
    require_hot_runtime_ready(operation="collection")


def create_channel_http(*, cache, jobs, library, API_KEY, COLLECTION_CONTEXT, COLLECTION_SETTINGS, DEPLOYMENT_PROFILE, YOUTUBE_API_TIMEOUT_SECONDS, YOUTUBE_QUOTA_ATTEMPT_RECORDER, core, database, require_collection_ready=_require_collection_ready, channel_add_admission=None):
    add_router = APIRouter()
    backfill_router = APIRouter()
    hydration_router = APIRouter()
    _load_initial_hydration_state = jobs._load_initial_hydration_state
    _prepare_channel_add_hydration = jobs._prepare_channel_add_hydration
    _prepare_initial_typed_inventory = jobs._prepare_initial_typed_inventory
    _request_operator_job_worker_wake = jobs._request_operator_job_worker_wake
    _with_initial_format_readiness = jobs._with_initial_format_readiness
    clear_frontend_read_cache = cache.clear_frontend_read_cache
    load_visible_channel_or_404 = library.load_visible_channel_or_404
    operator_job_repository = jobs.operator_job_repository
    _CHANNEL_ADD_SLOTS = threading.BoundedSemaphore(2)

    _STARTER_ADD_SLOTS = threading.BoundedSemaphore(len(_STARTER_CHANNEL_URLS))

    _CHANNEL_ADD_LOCKS = tuple(threading.RLock() for _ in range(64))

    CHANNEL_LOOKUPS = ChannelLookupCache()

    CHANNEL_ADD_LIMIT = channel_add_admission
    if CHANNEL_ADD_LIMIT is None and DEPLOYMENT_PROFILE != 'self-hosted':
        CHANNEL_ADD_LIMIT = RateLimiter(CHANNEL_ADD_WINDOWS)

    def acquire_channel_add():
        owner = CHANNEL_ADD_LIMIT if CHANNEL_ADD_LIMIT is not None else configured_channel_add_admission()
        return owner.acquire()

    def get_channel_id_from_video(video_id):
        return channel_sources.get_channel_id_from_video(
            video_id, youtube_api_get=youtube_api_get, api_key=API_KEY,
            attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
            timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
        )


    def get_initial_inventory_estimate(channel_id):
        return channel_sources.estimate_initial_inventory(
            channel_id, fetch_playlist_items_page=COLLECTION_CONTEXT.sources.fetch_playlist_items_page,
            clock=lambda: datetime.now(timezone.utc),
        )


    def parse_channel_input(raw):
        return channel_sources.parse_channel_input(
            raw, get_channel_id_from_video=get_channel_id_from_video,
        )


    @add_router.post("/add-channel")
    async def add_channel(req: ChannelRequest, background_tasks: BackgroundTasks):
        try:
            # SQLite wait is bounded and outside the event loop/provider permits.
            wait = await run_in_threadpool(acquire_channel_add)
        except ChannelAddAdmissionUnavailable:
            raise HTTPException(status_code=503, detail={'code':'channel_add_admission_unavailable',
                'message':'Channel addition is temporarily unavailable. Check installation storage before retrying.'}) from None
        if wait:
            raise HTTPException(status_code=429, headers={"Retry-After": str(wait)}, detail={
                "message": f"Too many channel additions. Try again in {wait_text(wait)}.",
                "code": "channel_add_rate_limited",
            })
        return await _add_channel(req, background_tasks)


    @add_router.post("/starter-channels/add")
    async def add_starter_channel(req: ChannelRequest, background_tasks: BackgroundTasks):
        if req.url not in _STARTER_CHANNEL_URLS:
            raise HTTPException(status_code=400, detail={"code": "unsupported_starter_channel"})
        from self_hosted_onboarding import get_manager
        await run_in_threadpool(require_collection_ready)
        manager = get_manager()
        await run_in_threadpool(manager.begin_starter, req.url)
        result = await _add_channel(req, background_tasks, starter=True)
        await run_in_threadpool(manager.record_starter, req.url, result)
        return result


    @add_router.post("/starter-channels/finish")
    def finish_starter_setup():
        from hot_runtime import require_hot_runtime_ready
        from self_hosted_onboarding import get_manager, initial_loading_complete
        from self_hosted_settings import SettingsError
        manager = get_manager()
        value = manager.public()
        workspace = value['workspace']
        if workspace['phase'] == 'complete':
            return {'ready': True}
        if workspace['phase'] != 'loading':
            raise HTTPException(status_code=409, detail={'code': 'workspace_not_started'})
        for channel_id in workspace['channels'].values():
            if channel_id is None:
                return {'ready': False}
            channel = load_visible_channel_or_404(channel_id)
            if not initial_loading_complete(_load_initial_hydration_state(channel)):
                return {'ready': False}
        require_hot_runtime_ready(operation='http')
        try:
            return manager.complete_workspace(value['revision'])
        except SettingsError as error:
            raise HTTPException(status_code=409, detail={'code': str(error)}) from None


    def _channel_add_lock(channel_id):
        return _CHANNEL_ADD_LOCKS[hash(channel_id) % len(_CHANNEL_ADD_LOCKS)]


    def _public_add_channel_result(result):
        """Return the sidebar fields only: never stored error text, page tokens or internal state."""
        channel = result.get("channel") if isinstance(result, dict) else None
        if not isinstance(channel, dict):
            return result
        # A mutation row is an acknowledgement, never a provider read source.
        # Re-read in the same worker thread through the admitted read boundary.
        with capture_read_lifetime() as channel_lifetime:
            try:
                rows = FrontendRepository(database).load_channel(channel["channel_id"])
                admitted = rows[0] if rows else {"channel_id": channel["channel_id"]}
            except Exception:
                rows = []
                admitted = {"channel_id": channel["channel_id"]}
        if rows and channel_lifetime.expires_at is not None:
            register_read_deadline(channel_lifetime.expires_at)
        with capture_read_lifetime() as hydration_lifetime:
            try:
                hydration = _load_initial_hydration_state(admitted)
            except Exception:
                hydration = None
        if hydration is None:
            hydration = collection_api.initial_hydration_error_payload(
                admitted, typed_inventory_enabled=COLLECTION_SETTINGS.typed_playlist_inventory_enabled,
            )
        elif hydration_lifetime.expires_at is not None:
            register_read_deadline(hydration_lifetime.expires_at)
        return {**result, "title": admitted.get("title"), "hydration": hydration,
                "channel": {field: admitted.get(field) for field in VISIBLE_CHANNEL_FIELDS}}


    async def _add_channel(req: ChannelRequest, background_tasks: BackgroundTasks, *, prepare_only=False, starter=False):
        result = await run_in_threadpool(_run_add_channel, req, background_tasks, prepare_only=prepare_only, starter=starter)
        return result


    def _run_add_channel(req, background_tasks, *, prepare_only=False, starter=False):
        from self_hosted_settings import SettingsError
        with (_STARTER_ADD_SLOTS if starter else _CHANNEL_ADD_SLOTS):
            require_collection_ready()
            try:
                with runtime_collection_access():
                    result = _add_channel_sync(req, background_tasks, prepare_only=prepare_only)
                # Restores prepare internal rows without an HTTP projection.
                return result if prepare_only else _public_add_channel_result(result)
            except SettingsError as error:
                # The stored key is marked refused: nothing was sent, and the owner learns why.
                if str(error) in KEY_REFUSALS:
                    return channel_sources.lookup_refusal(str(error))
                raise


    def _add_channel_sync(req: ChannelRequest, background_tasks: BackgroundTasks, *, prepare_only=False):
        return channel_service.add_channel(
            req.url, prepare_only=prepare_only, deployment_profile=DEPLOYMENT_PROFILE,
            repository=ChannelCommandRepository(database),
            trash_repository=ChannelTrashRepository(database) if DEPLOYMENT_PROFILE == "self-hosted" else None,
            lookup_cache=CHANNEL_LOOKUPS, channel_lock=_channel_add_lock,
            parse_legacy_input=parse_channel_input, get_channel_id_from_video=get_channel_id_from_video,
            lookup_channel_response=lambda params: channel_sources.lookup_channel_response(
                params, youtube_api_get=youtube_api_get, api_key=API_KEY,
                attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
                timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
            ),
            estimate_initial_inventory=get_initial_inventory_estimate,
            metadata_update_values=collection_api.channel_metadata_update_values,
            prepare_hydration=lambda channel, *, ensure_auto: _prepare_channel_add_hydration(
                channel, background_tasks, ensure_auto=ensure_auto,
            ),
            on_change=clear_frontend_read_cache, clock=lambda: datetime.now(timezone.utc),
        )


    @add_router.post("/refresh-all")
    async def refresh_all():
        raise HTTPException(
            status_code=410,
            detail="Legacy refresh-all is disabled. Use auto_sync.py/systemd auto-sync.",
        )


    @backfill_router.get("/channels/{channel_id}/videos/long/backfill-current-stats")
    @backfill_router.get("/channels/{channel_id}/backfill-current-stats")
    def channel_backfill_current_stats_status(channel_id: str):
        load_visible_channel_or_404(channel_id)
        try:
            mode = operator_jobs_mode()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=OPERATOR_JOBS_MODE_INVALID) from error
        if mode in {"durable", "drain"}:
            try:
                row = operator_job_repository().get_channel_job(channel_id)
            except Exception as error:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "Durable operator job status is temporarily unavailable."},
                ) from error
            return {
                "video_type": core.VIDEO_TYPE_LONG,
                "job": operator_job_snapshot(row, channel_id=channel_id),
            }


    @backfill_router.post("/channels/{channel_id}/videos/long/backfill-current-stats")
    @backfill_router.post("/channels/{channel_id}/backfill-current-stats")
    def start_channel_backfill_current_stats(channel_id: str, background_tasks: BackgroundTasks):
        require_collection_ready()
        channel = load_visible_channel_or_404(channel_id)
        eligibility = channel_sync_eligibility(channel)
        if not eligibility.allow_current_stats_job:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Current-stats backfill requires completed initial inventory.",
                    "channel_sync_mode": eligibility.mode,
                    "reason": eligibility.reason,
                },
            )
        try:
            mode = operator_jobs_mode()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=OPERATOR_JOBS_MODE_INVALID) from error

        if mode == "drain":
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Durable operator jobs are draining; new manual backfills are disabled."
                },
            )

        if mode == "durable":
            if not COLLECTION_SETTINGS.database_video_collection_enabled:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "Durable manual backfill requires the database video collection queue."
                    },
                )
            try:
                settings = load_operator_job_settings()
            except (TypeError, ValueError) as error:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "Durable operator job configuration is incomplete.",
                        "code": "operator_job_settings_invalid",
                    },
                ) from error
            # A completed backfill spent quota on every long video; repeats wait.
            try:
                wait = backfill_cooldown_seconds(operator_job_repository().get_channel_job(channel_id))
            except Exception as error:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "Durable operator job status is temporarily unavailable."},
                ) from error
            if wait:
                raise HTTPException(status_code=429, headers={"Retry-After": str(wait)}, detail={
                    "message": f"Current stats for this channel were backfilled recently. Try again in {wait_text(wait)}.",
                    "code": "current_stats_backfill_cooldown",
                })
            try:
                with runtime_collection_access():
                    result = operator_job_repository().enqueue_current_stats(
                        channel_id,
                        expected_epoch=channel_scope(channel)["control_epoch"],
                        max_items=None,
                        max_quota_units=settings.max_quota_units,
                        max_target_attempts=settings.max_target_attempts,
                    )
            except Exception as error:
                error_text = str(error)
                if "channel_collection_scope_lost" in error_text:
                    message = "Channel state changed. Refresh before trying again."
                    status_code = 409
                elif "operator_job_max_items_exceeded" in error_text:
                    message = "Channel scope exceeds the configured operator-job item cap."
                    status_code = 409
                elif "operator_job_quota_cap_exceeded" in error_text:
                    message = "Channel scope exceeds the configured operator-job quota cap."
                    status_code = 409
                elif "operator_job_channel_sync_not_ready" in error_text:
                    message = "Current-stats backfill requires completed initial inventory."
                    status_code = 409
                else:
                    message = "Durable operator job could not be queued."
                    status_code = 503
                raise HTTPException(status_code=status_code, detail={"message": message}) from error

            receipt = result["job"]
            job = operator_job_snapshot(_read_committed_job(database, receipt), channel_id=channel_id)
            if str(receipt.get("status") or "") in {"queued", "running"}:
                # Rewriting the hint is safe and recovers a lost first wake.
                _request_operator_job_worker_wake(channel_id)

            if result["created"]:
                status = "completed" if receipt["status"] == "completed" else "queued"
            else:
                status = "already_running"
            return {
                "video_type": core.VIDEO_TYPE_LONG,
                "status": status,
                "job": job,
            }


    def _initial_inventory_job_http_error(error: Exception) -> HTTPException:
        code = str(error)
        if "channel_collection_scope_lost" in code:
            return HTTPException(status_code=409, detail={
                "message": "Channel state changed. Refresh before trying again.",
                "code": "channel_collection_scope_lost",
            })
        if "operator_job_channel_not_visible" in code:
            return HTTPException(status_code=404, detail={"message": "Channel not found."})
        if "operator_jobs_full_hydration_schema_not_ready" in code:
            return HTTPException(
                status_code=503,
                detail={
                    "message": "Full channel hydration is awaiting its database migration.",
                    "code": "full_hydration_schema_not_ready",
                },
            )
        if "operator_job_initial_inventory_estimate_unavailable" in code:
            return HTTPException(
                status_code=409,
                detail={
                    "message": "Initial inventory estimate is unavailable; verify the channel count first.",
                    "code": "initial_inventory_estimate_unavailable",
                },
            )
        known_conflicts = {
            "operator_job_confirmation_required": "Explicit confirmation is required.",
            "operator_job_estimate_changed": "The channel estimate changed; refresh and confirm again.",
            "operator_job_initial_inventory_status_not_eligible": "Channel state does not permit an initial-inventory job.",
            "operator_job_channel_has_active_job": "Another operator job is already active for this channel.",
            "operator_job_max_items_exceeded": "Confirmed item cap is below the current estimate.",
            "operator_job_max_pages_exceeded": "Confirmed page cap is below the current estimate.",
            "operator_job_quota_cap_exceeded": "Confirmed quota cap is below the current estimate.",
            "operator_job_source_scope_expired": "The original source scope expired. Recheck status and explicitly confirm a fresh job; the old job cannot resume.",
            "operator_job_retry_not_available": "This job is not in a retryable state.",
            "operator_job_retry_cap_below_observed": "Retry caps cannot be lower than already observed progress.",
            "operator_job_retry_cap_invalid": "Retry caps are invalid.",
            "operator_job_retry_request_id_required": "Retry requires a fresh idempotency key.",
            "operator_job_retry_request_conflict": "This retry key was already used with different confirmed limits.",
            "initial_hydration_workset_evidence_missing": "Verified hydration workset evidence is unavailable; automatic repair is not safe.",
            "initial_hydration_workset_changed_after_verify": "The verified hydration workset changed; automatic repair is not safe.",
            "initial_hydration_workset_repair_incomplete": "The hydration workset could not be repaired completely; operator review is required.",
        }
        for marker, message in known_conflicts.items():
            if marker in code:
                return HTTPException(status_code=409, detail={"message": message, "code": marker})
        return HTTPException(
            status_code=503,
            detail={"message": "Durable initial-inventory control is temporarily unavailable."},
        )


    @hydration_router.get("/channels/{channel_id}/hydration")
    def channel_hydration_status(channel_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        channel = load_visible_channel_or_404(channel_id)
        try:
            return _load_initial_hydration_state(channel)
        except Exception as error:
            raise _initial_inventory_job_http_error(error) from error


    @hydration_router.post("/channels/{channel_id}/hydration")
    def enqueue_channel_hydration(
        channel_id: str,
        request: InitialHydrationRequest,
        background_tasks: BackgroundTasks,
        response: Response,
    ):
        require_collection_ready()
        response.headers["Cache-Control"] = "no-store"
        channel = load_visible_channel_or_404(channel_id)
        if request.confirmed is not True:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Explicit confirmation is required.",
                    "code": "operator_job_confirmation_required",
                },
            )
        try:
            mode = operator_jobs_mode()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=OPERATOR_JOBS_MODE_INVALID) from error
        if mode == "legacy":
            raise HTTPException(
                status_code=409,
                detail={"message": "Durable full channel hydration is not enabled."},
            )
        if mode == "drain":
            raise HTTPException(
                status_code=503,
                detail={"message": "Durable operator jobs are draining."},
            )
        try:
            with runtime_collection_access():
                estimate, row = collection_api.confirm_initial_hydration(
                    channel, request, repository=operator_job_repository(),
                    load_settings=load_operator_job_settings,
                )
        except Exception as error:
            raise _initial_inventory_job_http_error(error) from error

        if (
            estimate
            and str(row.get("status") or "") in {"queued", "running"}
        ):
            # The wake file is an idempotent hint; replay repairs a lost first wake.
            _request_operator_job_worker_wake(channel_id)
        with runtime_collection_access():
            _prepare_initial_typed_inventory(channel_id)
        # Admission estimates returned by the writer are not display evidence.
        # The committed job identity remains fixed even if another job appears.
        row = _read_committed_job(database, row)
        with capture_read_lifetime() as estimate_lifetime:
            try:
                estimate = operator_job_repository().estimate_initial_inventory(channel_id)
            except Exception:
                estimate = None
        if estimate is not None and estimate_lifetime.expires_at is not None:
            register_read_deadline(estimate_lifetime.expires_at)
        return _with_initial_format_readiness(channel, _initial_hydration_payload(
            channel,
            row=row,
            estimate=estimate,
            mode=mode,
        ))


    async def resolve_user_work_channel(locator, background_tasks):
        return await _add_channel(ChannelRequest(url=locator), background_tasks, prepare_only=True)

    def activate_restored_user_channel(channel_id, background_tasks):
        require_collection_ready()
        with runtime_collection_access():
            return channel_service.activate_restored_channel(
                channel_id, repository=ChannelCommandRepository(database),
                prepare_hydration=lambda channel, *, ensure_auto: _prepare_channel_add_hydration(
                    channel, background_tasks, ensure_auto=ensure_auto,
                ),
            )

    return ChannelHttp(add_router, backfill_router, hydration_router, resolve_user_work_channel, activate_restored_user_channel)
