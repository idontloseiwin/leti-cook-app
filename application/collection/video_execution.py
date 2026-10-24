"""Compose and dispatch bounded video work with explicitly supplied dependencies.

Queue selection, leases, settlement and HOT admission remain with the existing
repository and video collection service. This module owns no process resources.
"""

from db import VideoCollectionRepository
from video_collection_service import (
    VideoCollectionService,
    run_claimed_common_video_collection,
    run_claimed_initial_video_collection,
    run_claimed_video_collection,
)


def build_video_collection_service(
    *, database, settings, fetch_full, fetch_metadata, fetch_counters,
    build_full_payload, build_counter_payload, is_quota_exhausted,
    fetch_initial_profiles=None,
):
    """Bind a gateway and provider/payload callbacks without process defaults."""
    if database is None or settings is None:
        raise TypeError("database and settings are required")
    for name, callback in (
        ("fetch_full", fetch_full),
        ("fetch_metadata", fetch_metadata),
        ("fetch_counters", fetch_counters),
        ("build_full_payload", build_full_payload),
        ("build_counter_payload", build_counter_payload),
        ("is_quota_exhausted", is_quota_exhausted),
    ):
        if not callable(callback):
            raise TypeError(f"{name} must be supplied as a callable")
    repository = VideoCollectionRepository(database)
    batch_stats_enabled = settings.youtube_batch_stats_enabled

    def counters(video_ids, phase):
        if not batch_stats_enabled:
            raise RuntimeError("videos.batchGetStats is disabled")
        return fetch_counters(video_ids, phase)

    def full_payload(channel_id, video, now):
        return build_full_payload(
            channel_id, video, now=now, batch_stats_enabled=batch_stats_enabled,
        )

    def counter_payload(video, now):
        return build_counter_payload(video, now=now)

    return repository, VideoCollectionService(
        repository,
        fetch_full=fetch_full,
        fetch_metadata=fetch_metadata,
        fetch_initial_profiles=fetch_initial_profiles,
        fetch_counters=counters,
        build_full_payload=full_payload,
        build_counter_payload=counter_payload,
        is_quota_exhausted=is_quota_exhausted,
    )


def run_video_collection_queue_batch(
    limit=50,
    explicit_video_ids=None,
    reason="auto_stats_batch",
    progress_callback=None,
    force_metadata=False,
    common_preflight_checked=False,
    initial_job=None,
    initial_load_concurrency=8,
    channel_scopes=None,
    *,
    database,
    settings,
    fetch_full,
    fetch_metadata,
    fetch_counters,
    build_full_payload,
    build_counter_payload,
    is_quota_exhausted,
    worker_id_factory,
    fetch_initial_profiles=None,
):
    """Route initial, common-pool or explicit work through the existing owner."""
    if database is None or settings is None:
        raise TypeError("database and settings are required")
    if not callable(worker_id_factory):
        raise TypeError("worker_id_factory must be supplied as a callable")
    if not settings.database_video_collection_enabled:
        raise RuntimeError(
            "Database video collection is required; the legacy stats runtime has been "
            "retired. Set DATABASE_VIDEO_COLLECTION_ENABLED=true after verifying "
            "the queue schema."
        )
    repository, service = build_video_collection_service(
        database=database, settings=settings,
        fetch_full=fetch_full, fetch_metadata=fetch_metadata,
        fetch_initial_profiles=fetch_initial_profiles,
        fetch_counters=fetch_counters, build_full_payload=build_full_payload,
        build_counter_payload=build_counter_payload,
        is_quota_exhausted=is_quota_exhausted,
    )
    if initial_job is not None:
        if not force_metadata or explicit_video_ids is None:
            raise ValueError("initial collection requires explicit full-metadata targets")
        if len(explicit_video_ids) > max(int(limit or 0), 0):
            raise ValueError("initial collection targets exceed the requested bound")
        return run_claimed_initial_video_collection(
            service,
            job_id=str(initial_job["id"]),
            channel_id=str(initial_job["channel_id"]),
            video_ids=list(explicit_video_ids),
            worker_id=str(initial_job["lease_owner"]),
            run_id=str(initial_job["lease_token"]),
            reason=reason,
            progress_callback=progress_callback,
            concurrency=initial_load_concurrency,
        )
    if explicit_video_ids is None and not force_metadata:
        if not common_preflight_checked:
            repository.preflight_common_pool_ready()
        return run_claimed_common_video_collection(
            repository,
            service,
            limit=max(int(limit or 0), 0),
            worker_id=worker_id_factory() + ":common",
            reason=reason,
            progress_callback=progress_callback,
        )
    return run_claimed_video_collection(
        repository,
        service,
        limit=max(int(limit or 0), 0),
        worker_id=worker_id_factory(),
        explicit_video_ids=explicit_video_ids,
        reason=reason,
        progress_callback=progress_callback,
        force_metadata=force_metadata,
        channel_scopes=channel_scopes,
    )
