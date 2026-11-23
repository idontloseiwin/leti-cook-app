"""Explicit snapshots of the feature gates used by an operator chunk."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionSettings:
    database_video_collection_enabled: bool
    typed_playlist_inventory_enabled: bool
    youtube_batch_stats_enabled: bool


def load_collection_settings(environ: Mapping[str, str]) -> CollectionSettings:
    """Read the supplied configuration after the worker environment is applied."""
    def enabled(name: str) -> bool:
        return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

    return CollectionSettings(
        database_video_collection_enabled=enabled("DATABASE_VIDEO_COLLECTION_ENABLED"),
        typed_playlist_inventory_enabled=enabled("TYPED_PLAYLIST_INVENTORY_ENABLED"),
        youtube_batch_stats_enabled=enabled("YOUTUBE_BATCH_STATS_ENABLED"),
    )
