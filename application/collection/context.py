"""Required dependencies for collection scenarios; composition owns their lifetime.

The database is borrowed for one collection scope. Provider callbacks retain the
separate process transport/recorder lifetime. Recovery always opens a fresh
repository through its own context manager; this record opens/closes nothing.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ContextManager

from collection.settings import CollectionSettings


@dataclass(frozen=True)
class CollectionPolicy:
    database_channel_discovery_enabled: bool
    short_video_collection_enabled: bool
    completed_stream_collection_enabled: bool
    sync_run_stale_after_hours: int


@dataclass(frozen=True)
class CollectionSources:
    parse_dt: Callable[[Any], Any]
    fetch_playlist_items_page: Callable[..., dict]
    fetch_channel_metadata_batch: Callable[[list], list]
    get_longform_playlist_id: Callable[[str], str | None]
    is_youtube_daily_quota_exhausted: Callable[[Any], bool]


@dataclass(frozen=True)
class CollectionVideo:
    """Only the two existing video-execution boundaries, bound to this scope.

    run_batch retains the queue runner's named arguments, including its identity
    checked database/settings keywords for the legacy initial-statistics call.
    build_service constructs a service on demand at the old typed-runner point.
    Both callbacks must bind the same database/settings as CollectionContext.
    """

    run_batch: Callable[..., dict]
    build_service: Callable[[], Any]


@dataclass(frozen=True)
class CollectionContext:
    database: Any
    settings: CollectionSettings
    policy: CollectionPolicy
    sources: CollectionSources
    video: CollectionVideo
    fresh_channel_repository: Callable[[], ContextManager[Any]]
    supervised_environ: Callable[[], Mapping[str, str]]
    video_worker_id: Callable[[], str]
    discovery_worker_id: Callable[[], str]
    enqueue_initial_inventory: Callable[..., dict]
