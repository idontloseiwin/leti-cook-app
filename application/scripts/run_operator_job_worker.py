#!/usr/bin/env python3
"""Run durable operator-job chunks outside the API process."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.base.postgres import DatabaseUnavailable
from collection.settings import load_collection_settings
from collection import video_payloads
from video_collection_service import YouTubeViewCountAbsent, YouTubeViewCountInvalid
from core import (
    database as process_database, process_database_resources, API_KEY, YOUTUBE_QUOTA_ATTEMPT_RECORDER,
    YOUTUBE_API_TIMEOUT_SECONDS, COLLECTION_SCHEDULES,
)
from runtime.collection_schedules import require_schedule_model
from runtime.collection import (
    bind_collection_callbacks, video_collection_dependencies, video_collection_worker_id,
)
from youtube_batch_stats import BATCH_STATS_COUNTER_PARTS, fetch_video_batch_stats, is_batch_stats_quota_error
from youtube_client import youtube_api_get
from youtube_errors import (
    YouTubeAPIError, YouTubePlaylistNotFoundError, YouTubeDailyQuotaExceededError,
    is_youtube_daily_quota_exhausted,
)
from youtube_playlist import fetch_playlist_items_page
from youtube_video_profiles import fetch_initial_video_profiles, fetch_video_metadata
from youtube_source_fields import parse_dt
from typed_playlist import typed_playlist_id
from collection_outcome import copy_collection_stop as _copy_collection_stop
from telemetry import emit_event
from db import OperatorJobRepository
from operator_jobs import (
    COLLECTION_STOP_FLAGS,
    load_operator_job_settings,
    operator_collection_stop_reason,
    operator_jobs_mode,
    run_one_operator_job_chunk,
    run_operator_jobs_dispatch,
)

try:
    import fcntl
except Exception:
    fcntl = None


REQUIRED_OPERATOR_JOBS_SCHEMA_VERSION = 7
DEFAULT_DRAIN_MAX_CHUNKS = 128
DEFAULT_DRAIN_MAX_SECONDS = 900
DRAIN_MAX_CHUNKS_ENV = "OPERATOR_JOB_DRAIN_MAX_CHUNKS"
DRAIN_MAX_SECONDS_ENV = "OPERATOR_JOB_DRAIN_MAX_SECONDS"
DEFAULT_AUTO_SYNC_INTERVAL_MINUTES = 10
SUPPORTED_AUTO_SYNC_INTERVAL_MINUTES = (6, 10, 15, 20, 30)


@contextmanager
def fresh_operator_job_repository():
    """Borrow the process pool; every receipt lookup owns a new transaction."""
    yield OperatorJobRepository(process_database)



def _collection_callbacks(database, settings):
    """Bind existing process providers to one explicitly owned chunk gateway."""
    video = video_collection_dependencies(
        database=database, settings=settings,
        api_error=YouTubeAPIError, get_video_metadata=partial(
            fetch_video_metadata, youtube_api_get=youtube_api_get,
            api_key=API_KEY, attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
            timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
            emit_event=emit_event, perf_counter=time.perf_counter,
        ),
        get_initial_video_profiles=partial(
            fetch_initial_video_profiles, youtube_api_get=youtube_api_get,
            api_key=API_KEY, attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
            timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
            emit_event=emit_event, perf_counter=time.perf_counter,
        ),
        fetch_video_batch_stats=fetch_video_batch_stats,
        api_key=API_KEY, attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
        timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS, counter_parts=BATCH_STATS_COUNTER_PARTS,
        build_full_payload=partial(
            video_payloads.build_video_collection_metadata_payload, parse_dt=parse_dt,
            view_count_absent_error=YouTubeViewCountAbsent,
            view_count_invalid_error=YouTubeViewCountInvalid,
        ),
        build_counter_payload=video_payloads.build_video_counter_payload,
        is_quota_exhausted=partial(
            is_youtube_daily_quota_exhausted, is_batch_stats_quota_error=is_batch_stats_quota_error,
        ),
    )
    callbacks = bind_collection_callbacks(
        **video, worker_id_factory=partial(
            video_collection_worker_id, get_hostname=socket.gethostname, get_pid=os.getpid,
        ),
        fetch_page=partial(
            fetch_playlist_items_page, youtube_api_get=youtube_api_get,
            api_key=API_KEY, attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
            timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
            emit_event=emit_event, perf_counter=time.perf_counter,
        ),
        get_longform_playlist_id=partial(typed_playlist_id, video_type="long"), parse_dt=parse_dt,
        playlist_not_found_error=YouTubePlaylistNotFoundError,
        daily_quota_exceeded_error=YouTubeDailyQuotaExceededError,
        copy_collection_stop=_copy_collection_stop,
    )

    from collection.video_execution import build_video_collection_service
    from db.inventory.typed_inventory import TypedInventoryRepository
    callbacks.update(
        video_service=build_video_collection_service(**video)[1],
        inventory_repository=TypedInventoryRepository(database),
        fetch_page=partial(fetch_playlist_items_page, youtube_api_get=youtube_api_get,
            api_key=API_KEY, attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
            timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
            emit_event=emit_event, perf_counter=time.perf_counter),
    )
    return callbacks


def _run_dispatch_slice(repository, callbacks, settings, *, max_chunks, max_seconds, clock):
    from collection_shutdown import handoff_requested
    from hot_runtime import require_hot_runtime_ready
    from provider_runtime import request_scope
    from runtime.collection import bind_reconnect_feeder
    ready = partial(require_hot_runtime_ready, operation='collection')
    return run_operator_jobs_dispatch(
        repository, inventory_repository=callbacks['inventory_repository'],
        video_service=callbacks['video_service'], fetch_page=callbacks['fetch_page'],
        settings=settings, concurrency=int(os.environ.get('LIC_YOUTUBE_REQUEST_CONCURRENCY', '8')),
        max_chunks=max_chunks, max_seconds=max_seconds, require_ready=ready,
        should_stop=handoff_requested, clock=clock, fetch_scope=request_scope,
        legacy_chunk=lambda claimed: run_one_operator_job_chunk(
            repository, callbacks['run_stats'], settings=settings, claimed_job=claimed,
            run_initial_inventory_chunk=callbacks['run_initial_inventory_chunk'],
            run_initial_stats=callbacks['run_initial_stats'],
            fresh_repository_factory=fresh_operator_job_repository),
        reconnect_factory=lambda **options: bind_reconnect_feeder(repository.database,
            request=partial(youtube_api_get, api_key=API_KEY,
                attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER),
            require_ready=ready, **options),
    )


def _operator_jobs_schema_version(schema: dict[str, object]) -> int:
    try:
        return int(schema.get("schema_version") or 0)
    except (TypeError, ValueError):
        return 0


def _bounded_positive_int(
    value: int | str | None,
    *,
    default: int,
    maximum: int,
    name: str,
) -> int:
    parsed = int(value) if value not in (None, "") else int(default)
    if parsed <= 0 or parsed > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return parsed


def _auto_sync_interval_minutes(value: int | str | None) -> int:
    parsed = (
        DEFAULT_AUTO_SYNC_INTERVAL_MINUTES
        if value in (None, "")
        else int(value)
    )
    if parsed not in SUPPORTED_AUTO_SYNC_INTERVAL_MINUTES:
        raise ValueError(
            "auto-sync interval must be one of "
            + ", ".join(str(item) for item in SUPPORTED_AUTO_SYNC_INTERVAL_MINUTES)
        )
    return parsed


def in_auto_sync_reserved_window(
    now: datetime | None = None,
    *,
    interval_minutes: int = DEFAULT_AUTO_SYNC_INTERVAL_MINUTES,
) -> bool:
    current = now or datetime.now(timezone.utc)
    interval = _auto_sync_interval_minutes(interval_minutes)
    phase = current.minute % interval
    if interval == 6:
        # Reserve two minutes from each anchor and one minute before it.
        return phase < 2 or phase >= 5
    if interval == 10:
        # Protect the timer handoff without starving operator jobs: two
        # minutes before and three minutes from the anchor.
        return phase < 3 or phase >= 8
    return phase < 5 or phase >= interval - 5


def acquire_shared_sync_lock():
    if fcntl is None:
        raise RuntimeError("operator job worker requires fcntl on this platform")
    path = os.getenv("AUTO_SYNC_LOCK_PATH", "/tmp/youtube-tracker-auto-sync.lock")
    handle = open(path, "a", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except Exception:
        handle.close()
        raise
    return handle


def _public_result(
    result: dict[str, object],
    *,
    chunks_processed: int,
    stop_reason: str,
) -> dict[str, object]:
    return {
        "status": result.get("status"),
        "processed_video_ids": int(result.get("processed_video_ids") or 0),
        "processed_pages": int(result.get("processed_pages") or 0),
        "deferred": bool(result.get("deferred")),
        "chunks_processed": chunks_processed,
        "stop_reason": stop_reason,
        **{key: bool(result.get(key)) for key in COLLECTION_STOP_FLAGS},
    }


def main(
    *,
    channel_id: str | None = None,
    drain: bool = False,
    max_chunks: int | None = None,
    max_seconds: int | None = None,
    auto_sync_interval_minutes: int = DEFAULT_AUTO_SYNC_INTERVAL_MINUTES,
    monotonic: Callable[[], float] | None = None,
    now_factory: Callable[[], datetime] | None = None,
    supervised: bool = False,
) -> int:
    from collection_shutdown import yield_requested, finish_current_chunk
    mode = operator_jobs_mode()
    if mode not in {"durable", "drain"}:
        print(json.dumps({"status": "disabled", "mode": mode}, sort_keys=True))
        return 0
    collection_settings = load_collection_settings(os.environ)
    if not collection_settings.database_video_collection_enabled:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "database_video_collection_disabled",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    require_schedule_model(COLLECTION_SCHEDULES)
    clock = monotonic or time.monotonic
    current_time = now_factory or (lambda: datetime.now(timezone.utc))
    auto_sync_interval = _auto_sync_interval_minutes(
        auto_sync_interval_minutes
    )
    chunk_limit = _bounded_positive_int(
        max_chunks if max_chunks is not None else os.getenv(DRAIN_MAX_CHUNKS_ENV),
        default=DEFAULT_DRAIN_MAX_CHUNKS,
        maximum=512,
        name=DRAIN_MAX_CHUNKS_ENV,
    )
    second_limit = _bounded_positive_int(
        max_seconds if max_seconds is not None else os.getenv(DRAIN_MAX_SECONDS_ENV),
        default=DEFAULT_DRAIN_MAX_SECONDS,
        maximum=1200,
        name=DRAIN_MAX_SECONDS_ENV,
    )

    started_at = clock()
    chunks_processed = 0
    total_video_ids = 0
    total_pages = 0
    schema_checked = False
    last_result: dict[str, object] = {
        "status": "idle",
        "processed_video_ids": 0,
        "processed_pages": 0,
        "deferred": False,
    }
    stop_reason = "single_chunk"

    while True:
        if yield_requested():
            stop_reason = 'scheduled_handoff'
            break
        # A supervised drain gets its budget from the supervisor; handoff can
        # wait for the current chunk. Unsupervised drains keep the clock window.
        if drain and not supervised and in_auto_sync_reserved_window(
            current_time(),
            interval_minutes=auto_sync_interval,
        ):
            if chunks_processed == 0:
                last_result = {
                    "status": "deferred",
                    "processed_video_ids": 0,
                    "processed_pages": 0,
                    "deferred": True,
                }
            stop_reason = "auto_sync_window"
            break
        if drain and chunks_processed >= chunk_limit:
            stop_reason = "max_chunks"
            break
        if drain and clock() - started_at >= second_limit:
            stop_reason = "time_budget"
            break

        lock_handle = acquire_shared_sync_lock()
        if lock_handle is None:
            last_result = {
                "status": "deferred",
                "processed_video_ids": 0,
                "processed_pages": 0,
                "deferred": True,
            }
            stop_reason = "auto_sync_lock_busy"
            break

        try:
            try:
                with fresh_operator_job_repository() as repository:
                    if not schema_checked:
                        schema = repository.schema_status()
                        if (
                            not schema.get("ready")
                            or _operator_jobs_schema_version(schema)
                            < REQUIRED_OPERATOR_JOBS_SCHEMA_VERSION
                            or schema.get("initial_quota_limit_enforced") is not False
                        ):
                            print(
                                json.dumps(
                                    {
                                        "status": "blocked",
                                        "reason": "schema_not_ready",
                                    },
                                    sort_keys=True,
                                ),
                                file=sys.stderr,
                            )
                            return 2
                        schema_checked = True

                    settings = load_operator_job_settings(require_quota_cap=False)
                    # Schema/config preparation can be the first operation to
                    # observe update hold. Do not accept a new chunk after it.
                    if yield_requested():
                        stop_reason = 'scheduled_handoff'
                        break
                    with finish_current_chunk():
                        callbacks = _collection_callbacks(repository.database, collection_settings)
                        if supervised and drain and channel_id is None:
                            shared = _run_dispatch_slice(repository, callbacks, settings,
                                max_chunks=chunk_limit, max_seconds=second_limit, clock=clock)
                            print(json.dumps(_public_result(shared,
                                chunks_processed=shared['chunks_processed'],
                                stop_reason=shared['stop_reason']), sort_keys=True))
                            return 1 if operator_collection_stop_reason(shared) else 0
                        last_result = run_one_operator_job_chunk(
                            repository,
                            callbacks["run_stats"],
                            settings=settings,
                            run_initial_inventory_chunk=callbacks["run_initial_inventory_chunk"],
                            run_initial_stats=callbacks["run_initial_stats"],
                            target_channel_id=channel_id,
                            fresh_repository_factory=fresh_operator_job_repository,
                        )
            except DatabaseUnavailable as error:
                print(
                    json.dumps(
                        {
                            "status": "error",
                            "reason": "database_transport_error",
                            "error_type": error.__class__.__name__,
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
                return 1
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()

        status = str(last_result.get("status") or "unknown")
        if status == "idle":
            stop_reason = "idle"
            break

        chunks_processed += 1
        total_video_ids += int(last_result.get("processed_video_ids") or 0)
        total_pages += int(last_result.get("processed_pages") or 0)
        collection_stop = operator_collection_stop_reason(last_result)
        if collection_stop:
            stop_reason = collection_stop
            break
        if bool(last_result.get("deferred")):
            stop_reason = "deferred"
            break
        if not drain:
            stop_reason = "single_chunk"
            break

    public = _public_result(
        {
            **last_result,
            "processed_video_ids": total_video_ids,
            "processed_pages": total_pages,
        },
        chunks_processed=chunks_processed,
        stop_reason=stop_reason,
    )
    print(json.dumps(public, sort_keys=True))
    return 1 if operator_collection_stop_reason(last_result) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run durable operator-job chunks outside the API process."
    )
    parser.add_argument("--channel-id")
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--max-chunks", type=int)
    parser.add_argument("--max-seconds", type=int)
    parser.add_argument(
        "--supervised",
        action="store_true",
        help="The collection supervisor granted this drain its time slice.",
    )
    parser.add_argument(
        "--auto-sync-interval-minutes",
        type=int,
        choices=SUPPORTED_AUTO_SYNC_INTERVAL_MINUTES,
        default=DEFAULT_AUTO_SYNC_INTERVAL_MINUTES,
    )
    return parser


def cli(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        require_schedule_model(COLLECTION_SCHEDULES)
        with process_database_resources():
            return main(
                channel_id=arguments.channel_id,
                drain=bool(arguments.drain),
                max_chunks=arguments.max_chunks,
                max_seconds=arguments.max_seconds,
                auto_sync_interval_minutes=arguments.auto_sync_interval_minutes,
                supervised=bool(arguments.supervised),
            )
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "reason": "worker_unhandled_error",
                    "error_type": error.__class__.__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
