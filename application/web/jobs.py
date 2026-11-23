"""Channel job orchestration and background-process callbacks."""
from db import OperatorJobRepository, TypedInventoryRepository
from channels import reads as channel_reads, service as channel_service
from collection import api as collection_api
from operator_jobs import load_operator_job_settings, operator_jobs_mode, signal_operator_job_worker
from runtime.process_collection import process_collection_scope
from db.base.read_context import collection_scope
from hot_runtime import runtime_collection_access
from source_log import source_log_message
from typing import Callable
from dataclasses import dataclass

@dataclass(frozen=True)
class ChannelJobs:
    _load_initial_hydration_state: Callable
    _prepare_channel_add_hydration: Callable
    _prepare_initial_typed_inventory: Callable
    _request_operator_job_worker_wake: Callable
    _with_initial_format_readiness: Callable
    operator_job_repository: Callable

def create_channel_jobs(*, COLLECTION_POLICY, COLLECTION_SETTINGS, database):
    def operator_job_repository():
        return OperatorJobRepository(database)


    def _initial_hydration_error_payload(channel, *, estimate=None):
        return collection_api.initial_hydration_error_payload(
            channel, estimate=estimate,
            typed_inventory_enabled=COLLECTION_SETTINGS.typed_playlist_inventory_enabled,
        )


    def _load_initial_hydration_state(channel, *, repository=None):
        mode = operator_jobs_mode()
        if mode in {"durable", "drain"}:
            repository = repository or operator_job_repository()
        return channel_reads.load_initial_hydration_state(
            channel, repository=repository, get_typed_repository=lambda: TypedInventoryRepository(database),
            mode=mode, typed_inventory_enabled=COLLECTION_SETTINGS.typed_playlist_inventory_enabled,
        )


    def _with_initial_format_readiness(channel, payload):
        return collection_api.with_initial_format_readiness(
            channel, payload, get_typed_repository=lambda: TypedInventoryRepository(database),
            typed_inventory_enabled=COLLECTION_SETTINGS.typed_playlist_inventory_enabled,
        )


    def _prepare_initial_typed_inventory(channel_id):
        return collection_api.prepare_initial_typed_inventory(
            channel_id, get_typed_repository=lambda: TypedInventoryRepository(database),
            typed_inventory_enabled=COLLECTION_SETTINGS.typed_playlist_inventory_enabled,
        )


    def _enqueue_initial_hydration(channel, *, repository, expected=None, confirmed=False):
        return collection_api.enqueue_initial_hydration(
            channel, repository=repository, expected=expected, confirmed=confirmed,
            load_settings=load_operator_job_settings,
        )


    def _request_operator_job_worker_wake(channel_id):
        """Best-effort signal only; the API process never executes job chunks."""
        try:
            signal_operator_job_worker()
        except Exception as error:
            print(
                source_log_message('operator_job_wake_error', lambda: ("[operator-job-wake] "
                f"channel={channel_id} error_type={error.__class__.__name__}"), error=error),
                flush=True,
            )


    def _run_channel_initial_inventory_task(channel_id, expected_epoch):
        # Background work begins its own database scope when it actually executes.
        with collection_scope(), runtime_collection_access():
            with process_collection_scope(settings=COLLECTION_SETTINGS, policy=COLLECTION_POLICY) as context:
                return collection_api.initial_inventory_for_channel(channel_id, context=context, expected_epoch=expected_epoch)


    def _prepare_channel_add_hydration(channel, background_tasks, *, ensure_auto):
        return channel_service.prepare_channel_add_hydration(
            channel, ensure_auto=ensure_auto, get_mode=operator_jobs_mode,
            get_repository=operator_job_repository, get_typed_repository=lambda: TypedInventoryRepository(database),
            typed_inventory_enabled=COLLECTION_SETTINGS.typed_playlist_inventory_enabled,
            load_settings=load_operator_job_settings,
            schedule_initial_inventory=lambda channel_id, *, expected_epoch: background_tasks.add_task(
                _run_channel_initial_inventory_task, channel_id, expected_epoch,
            ),
            wake_worker=_request_operator_job_worker_wake,
        )


    return ChannelJobs(_load_initial_hydration_state, _prepare_channel_add_hydration, _prepare_initial_typed_inventory, _request_operator_job_worker_wake, _with_initial_format_readiness, operator_job_repository)
