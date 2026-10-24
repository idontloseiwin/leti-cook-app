"""Bind collection callbacks to supplied process providers and one database scope.

The caller owns configuration, provider resources and database lifetime. These
helpers neither bootstrap runtime state nor open or close any resource.
"""

from collection import initial, video_execution
from youtube_video_profiles import fetch_video_profiles


def bind_reconnect_feeder(database, *, request, require_ready,
                          lease_seconds=180, max_attempts=3, on_settled=None, should_stop=None):
    """Use the existing reconnect owners in the collector's DB/provider scope."""
    from channels.reconnect_source import ReconnectSource
    from collection.channel_metadata import channel_metadata_update_values
    from collection.reconnect_feeder import ReconnectFeeder
    from db.user_work.user_work_backup import UserWorkBackupRepository
    from hot_storage import HotSourceReadExpired, source_is_current, utc_now

    def require_source(row):
        # Never renew an acquisition clock while its response waits for a slot.
        # The binding transaction remains authoritative for shape and its
        # stricter fresh-add clock checks, including individual list evidence.
        if not source_is_current(row.get('channel_metadata_checked_at'), now=utc_now()):
            raise HotSourceReadExpired('reconnect_source_expired')

    return ReconnectFeeder(
        UserWorkBackupRepository(database),
        resolve_channel=ReconnectSource(request, metadata_update_values=channel_metadata_update_values),
        require_ready=require_ready, require_source=require_source,
        lease_seconds=lease_seconds, max_attempts=max_attempts,
        on_settled=on_settled, should_stop=should_stop,
    )


def video_collection_dependencies(
    *, database, settings, api_error, get_video_metadata,
    fetch_video_batch_stats, api_key, attempt_recorder, timeout_seconds,
    counter_parts, build_full_payload, build_counter_payload, is_quota_exhausted,
    get_initial_video_profiles=None,
):
    """Normalize provider/payload calls while retaining their supplied identities."""
    if database is None or settings is None:
        raise TypeError("database and settings are required")
    for name, callback in (
        ("get_video_metadata", get_video_metadata),
        ("fetch_video_batch_stats", fetch_video_batch_stats),
        ("build_full_payload", build_full_payload),
        ("build_counter_payload", build_counter_payload),
        ("is_quota_exhausted", is_quota_exhausted),
    ):
        if not callable(callback):
            raise TypeError(f"{name} must be supplied as a callable")

    if not isinstance(api_error, type) or not issubclass(api_error, Exception):
        raise TypeError("api_error must be supplied as an exception class")

    def fetch_full(video_ids, phase):
        return fetch_video_profiles(
            list(video_ids), phase=phase,
            batch_stats_enabled=settings.youtube_batch_stats_enabled,
            get_video_metadata=get_video_metadata, fetch_video_batch_stats=fetch_video_batch_stats,
            api_key=api_key, attempt_recorder=attempt_recorder,
            timeout_seconds=timeout_seconds, counter_parts=counter_parts, api_error=api_error,
        )

    def fetch_initial_profiles(video_ids, phase):
        if not callable(get_initial_video_profiles):
            raise TypeError("get_initial_video_profiles must be supplied for initial loading")
        return get_initial_video_profiles(list(video_ids), phase=phase)

    def fetch_metadata(video_ids, phase):
        # Unrequested or repeated items fail only themselves, not the batch.
        return get_video_metadata(list(video_ids), phase=phase,
                                  reject_identity_anomalies=False, include_statistics=True)

    def fetch_counters(video_ids, phase):
        return fetch_video_batch_stats(
            list(video_ids),
            api_key=api_key,
            attempt_recorder=attempt_recorder,
            timeout_seconds=timeout_seconds,
            phase=phase,
            parts=counter_parts,
        )

    def build_full(channel_id, video, now, *, batch_stats_enabled):
        return build_full_payload(
            channel_id, video, now=now, batch_stats_enabled=batch_stats_enabled,
        )

    def build_counters(video, now):
        return build_counter_payload(video, now=now)

    return dict(
        database=database, settings=settings,
        fetch_full=fetch_full, fetch_metadata=fetch_metadata,
        fetch_initial_profiles=fetch_initial_profiles,
        fetch_counters=fetch_counters,
        build_full_payload=build_full, build_counter_payload=build_counters,
        is_quota_exhausted=is_quota_exhausted,
    )


def bind_collection_callbacks(
    *, database, settings, fetch_full, fetch_metadata, fetch_counters,
    build_full_payload, build_counter_payload, is_quota_exhausted,
    worker_id_factory, fetch_page, get_longform_playlist_id, parse_dt,
    playlist_not_found_error, daily_quota_exceeded_error, copy_collection_stop,
    fetch_initial_profiles=None,
):
    """Bind the three operator callbacks directly to their collection owners."""
    if database is None or settings is None:
        raise TypeError("database and settings are required")
    for name, callback in (
        ("fetch_full", fetch_full),
        ("fetch_metadata", fetch_metadata),
        ("fetch_counters", fetch_counters),
        ("build_full_payload", build_full_payload),
        ("build_counter_payload", build_counter_payload),
        ("is_quota_exhausted", is_quota_exhausted),
        ("worker_id_factory", worker_id_factory),
        ("fetch_page", fetch_page),
        ("get_longform_playlist_id", get_longform_playlist_id),
        ("parse_dt", parse_dt),
        ("copy_collection_stop", copy_collection_stop),
    ):
        if not callable(callback):
            raise TypeError(f"{name} must be supplied as a callable")
    for name, error_type in (
        ("playlist_not_found_error", playlist_not_found_error),
        ("daily_quota_exceeded_error", daily_quota_exceeded_error),
    ):
        if not isinstance(error_type, type) or not issubclass(error_type, BaseException):
            raise TypeError(f"{name} must be supplied as an exception class")

    bound_database, bound_settings = database, settings
    video_runner = video_execution.run_video_collection_queue_batch
    inventory_runner = initial.collect_initial_inventory_chunk
    initial_stats_runner = initial.initial_28d_stats_import

    def require_bound_scope(database, settings):
        if database is not bound_database or settings is not bound_settings:
            raise ValueError("collection dependencies differ from their bound scope")

    def run_stats(
        limit=50, explicit_video_ids=None, reason="auto_stats_batch",
        progress_callback=None, force_metadata=False, common_preflight_checked=False,
        initial_job=None, channel_scopes=None, *, initial_load_concurrency=8,
        database=database, settings=settings,
    ):
        require_bound_scope(database, settings)
        return video_runner(
            limit=limit, explicit_video_ids=explicit_video_ids, reason=reason,
            progress_callback=progress_callback, force_metadata=force_metadata,
            common_preflight_checked=common_preflight_checked, initial_job=initial_job, channel_scopes=channel_scopes,
            initial_load_concurrency=initial_load_concurrency,
            database=bound_database, settings=bound_settings,
            fetch_full=fetch_full, fetch_metadata=fetch_metadata,
            fetch_counters=fetch_counters,
            fetch_initial_profiles=fetch_initial_profiles,
            build_full_payload=build_full_payload, build_counter_payload=build_counter_payload,
            is_quota_exhausted=is_quota_exhausted, worker_id_factory=worker_id_factory,
        )

    def run_initial_inventory_chunk(
        channel_id, *, max_pages=1, page_token=None, video_type=None, expected_epoch,
        transition_id=None, before_dispatch=None, database=database, settings=settings,
        initial_job_id=None, initial_job_lease_token=None,
    ):
        require_bound_scope(database, settings)
        job_options = {} if initial_job_id is None and initial_job_lease_token is None else dict(
            initial_job_id=initial_job_id, initial_job_lease_token=initial_job_lease_token)
        return inventory_runner(
            channel_id, max_pages=max_pages, page_token=page_token, video_type=video_type,
            transition_id=transition_id, before_dispatch=before_dispatch,
            database=bound_database, settings=bound_settings,
            worker_id=worker_id_factory() + ":typed-inventory",
            fetch_page=fetch_page, get_longform_playlist_id=get_longform_playlist_id,
            parse_dt=parse_dt, playlist_not_found_error=playlist_not_found_error,
            daily_quota_exceeded_error=daily_quota_exceeded_error,
            expected_epoch=expected_epoch, **job_options,
        )

    def run_initial_stats(channel_id, *, expected_epoch, database=database, settings=settings):
        require_bound_scope(database, settings)
        return initial_stats_runner(
            channel_id, database=bound_database, settings=bound_settings,
            run_video_collection_queue_batch=run_stats,
            copy_collection_stop=copy_collection_stop,
            expected_epoch=expected_epoch,
        )

    return dict(
        run_stats=run_stats,
        run_initial_inventory_chunk=run_initial_inventory_chunk,
        run_initial_stats=run_initial_stats,
    )


def video_collection_worker_id(*, get_hostname, get_pid):
    hostname = get_hostname().strip() or "worker"
    return f"{hostname}:{get_pid()}"


def bind_collection_context(
    *, database, settings, fetch_full, fetch_metadata, fetch_counters,
    build_full_payload, build_counter_payload, is_quota_exhausted,
    policy, sources, fresh_channel_repository, supervised_environ,
    video_worker_id, discovery_worker_id, enqueue_initial_inventory,
    fetch_initial_profiles=None,
):
    """Bind scenarios once; nested work cannot switch the borrowed gateway."""
    from collection.context import CollectionContext, CollectionVideo
    from collection_outcome import copy_collection_stop
    from youtube_errors import YouTubePlaylistNotFoundError, YouTubeDailyQuotaExceededError

    for name, callback in (
        ("fresh_channel_repository", fresh_channel_repository),
        ("supervised_environ", supervised_environ),
        ("video_worker_id", video_worker_id),
        ("discovery_worker_id", discovery_worker_id),
        ("enqueue_initial_inventory", enqueue_initial_inventory),
    ):
        if not callable(callback):
            raise TypeError(f"{name} must be supplied as a callable")
    video_dependencies = dict(
        database=database, settings=settings, fetch_full=fetch_full,
        fetch_metadata=fetch_metadata, fetch_counters=fetch_counters,
        fetch_initial_profiles=fetch_initial_profiles,
        build_full_payload=build_full_payload, build_counter_payload=build_counter_payload,
        is_quota_exhausted=is_quota_exhausted,
    )
    callbacks = bind_collection_callbacks(
        **video_dependencies, worker_id_factory=video_worker_id,
        fetch_page=sources.fetch_playlist_items_page,
        get_longform_playlist_id=sources.get_longform_playlist_id, parse_dt=sources.parse_dt,
        playlist_not_found_error=YouTubePlaylistNotFoundError,
        daily_quota_exceeded_error=YouTubeDailyQuotaExceededError,
        copy_collection_stop=copy_collection_stop,
    )
    def build_service():
        return video_execution.build_video_collection_service(**video_dependencies)

    return CollectionContext(
        database=database, settings=settings, policy=policy, sources=sources,
        video=CollectionVideo(
            run_batch=callbacks["run_stats"],
            build_service=build_service,
        ),
        fresh_channel_repository=fresh_channel_repository, supervised_environ=supervised_environ,
        video_worker_id=video_worker_id, discovery_worker_id=discovery_worker_id,
        enqueue_initial_inventory=enqueue_initial_inventory,
    )
