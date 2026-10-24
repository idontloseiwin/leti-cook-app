import os
import re
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from collection.settings import load_collection_settings

from db import (
    YouTubeQuotaRepository,
    create_database_gateway,
)
from telemetry import emit_event
from youtube_client import youtube_api_get
from youtube_errors import YouTubeAPIError, youtube_error_message
from youtube_batch_stats import BATCH_STATS_COUNTER_PARTS, fetch_video_batch_stats
from youtube_video_profiles import fetch_video_metadata, fetch_video_profiles
from youtube_source_fields import parse_dt
from youtube_quota import YouTubeQuotaAttemptRecorder
from youtube_quota_buffer import quota_journal_config
from typed_playlist import typed_playlist_id
from framework_log import install_self_hosted_framework_logging

try:
    # Development only: the image does not ship python-dotenv, and installations
    # pass configuration through the environment.
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv()
install_self_hosted_framework_logging()

API_KEY = os.getenv("YOUTUBE_API_KEY")
from db.base.client import load_database_config
from hot_runtime import load_hot_admission_config
from runtime.collection_schedules import load_process_schedules

COLLECTION_SCHEDULES = load_process_schedules(os.environ, required=False)

DATABASE_CONFIG = load_database_config()
HOT_ADMISSION_CONFIG = load_hot_admission_config()
database = create_database_gateway(
    DATABASE_CONFIG,
    cadence_snapshot=None if COLLECTION_SCHEDULES is None else COLLECTION_SCHEDULES.cadence_snapshot(),
)

YOUTUBE_QUOTA_PROJECT_ID = (
    os.getenv("YOUTUBE_QUOTA_PROJECT_ID", "primary").strip() or "primary"
)
YOUTUBE_QUOTA_REPOSITORY = YouTubeQuotaRepository(
    database,
    project_id=YOUTUBE_QUOTA_PROJECT_ID,
)
YOUTUBE_QUOTA_JOURNAL_PATH, YOUTUBE_QUOTA_JOURNAL_NAMESPACE = quota_journal_config(
    project_id=YOUTUBE_QUOTA_PROJECT_ID, database_id=HOT_ADMISSION_CONFIG.database_id,
)
YOUTUBE_QUOTA_ATTEMPT_RECORDER = YouTubeQuotaAttemptRecorder(
    database,
    project_id=YOUTUBE_QUOTA_PROJECT_ID,
    journal_path=YOUTUBE_QUOTA_JOURNAL_PATH,
    journal_namespace=YOUTUBE_QUOTA_JOURNAL_NAMESPACE,
)

@contextmanager
def process_database_resources(*, record_quota=True):
    """Standalone command lifetime; nested collection scopes borrow this pool."""
    try:
        database.open()
        if record_quota:
            YOUTUBE_QUOTA_ATTEMPT_RECORDER.start()
        yield database
    finally:
        try:
            if record_quota:
                YOUTUBE_QUOTA_ATTEMPT_RECORDER.close()
        finally:
            database.close()


PT = ZoneInfo("America/Los_Angeles")

VIDEO_TYPE_LONG = "long"
VIDEO_TYPE_SHORT = "short"
VIDEO_TYPE_STREAM = "stream"

YOUTUBE_API_TIMEOUT_SECONDS = 20
YOUTUBE_BATCH_STATS_ENABLED = (
    load_collection_settings(os.environ).youtube_batch_stats_enabled
)


# Authoritative video type comes from typed playlists:
# UULF -> long, UUSH -> short, UULV -> stream.
def get_shorts_playlist_id(channel_id):
    return typed_playlist_id(channel_id, VIDEO_TYPE_SHORT)


def get_streams_playlist_id(channel_id):
    return typed_playlist_id(channel_id, VIDEO_TYPE_STREAM)


def get_longform_playlist_id(channel_id):
    return typed_playlist_id(channel_id, VIDEO_TYPE_LONG)


def parse_duration(duration_str):
    if not duration_str:
        return 0
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    s = int(m.group(3) or 0)
    return h * 3600 + mins * 60 + s


def iter_batches(items, batch_size):
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


def median_value(values):
    if not values:
        return None
    sorted_values = sorted(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2


def int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_video_metadata(video_ids, phase="video_metadata", *, reject_identity_anomalies=True,
                       include_statistics=False):
    """Compatibility metadata fetch using current process provider bindings."""
    return fetch_video_metadata(
        video_ids, phase=phase, reject_identity_anomalies=reject_identity_anomalies,
        include_statistics=include_statistics,
        youtube_api_get=youtube_api_get, api_key=API_KEY,
        attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
        timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
        emit_event=emit_event, perf_counter=time.perf_counter,
    )


def get_video_stats(video_ids, phase="video_stats"):
    """Compatibility full-profile fetch using current process defaults."""
    return fetch_video_profiles(
        video_ids, phase=phase, batch_stats_enabled=YOUTUBE_BATCH_STATS_ENABLED,
        get_video_metadata=get_video_metadata,
        fetch_video_batch_stats=fetch_video_batch_stats,
        api_key=API_KEY, attempt_recorder=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
        timeout_seconds=YOUTUBE_API_TIMEOUT_SECONDS,
        counter_parts=BATCH_STATS_COUNTER_PARTS, api_error=YouTubeAPIError,
    )
