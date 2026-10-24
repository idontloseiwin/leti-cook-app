"""FastAPI entrypoint: process configuration, resource wiring and route order."""
import os
from pathlib import Path

from fastapi import FastAPI
import core
from core import (
    database, API_KEY, PT, YOUTUBE_QUOTA_ATTEMPT_RECORDER,
    YOUTUBE_API_TIMEOUT_SECONDS,
)
from deployment_profile import selected_deployment_profile
from runtime.process_collection import process_collection_configuration, bind_process_collection
from web.cache import create_read_cache
from web.channels import create_channel_http
from web.explorer import create_explorer_router
from web.frontend import create_frontend_router
from web.home import create_home_router
from web.jobs import create_channel_jobs
from web.library import create_library_http
from web.lifecycle import create_lifespan
from web.middleware import install_runtime_middleware
from web.organization import create_organization_router
from web.system import create_system_router

COLLECTION_SETTINGS, COLLECTION_POLICY = process_collection_configuration()
COLLECTION_CONTEXT = bind_process_collection(
    database=database, settings=COLLECTION_SETTINGS, policy=COLLECTION_POLICY,
)

CHANNEL_BASELINE_ANALYTICS_ENABLED = os.getenv(
    "CHANNEL_BASELINE_ANALYTICS_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
CHANNEL_DERIVED_INSIGHTS_ENABLED = os.getenv(
    "CHANNEL_DERIVED_INSIGHTS_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
CHANNEL_TIMELINE_INSIGHTS_ENABLED = os.getenv(
    "CHANNEL_TIMELINE_INSIGHTS_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
CHANNEL_LAUNCH_ANALYTICS_ENABLED = os.getenv(
    "CHANNEL_LAUNCH_ANALYTICS_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}

APP_DIR = Path(__file__).resolve().parent
DEPLOYMENT_PROFILE = selected_deployment_profile()
FRONTEND_DIST_DIR = APP_DIR / "frontend" / (
    "dist-self-hosted" if DEPLOYMENT_PROFILE == "self-hosted" else "dist"
)

read_cache = create_read_cache()
library_http = create_library_http(cache=read_cache, database=database)
channel_jobs = create_channel_jobs(
    COLLECTION_POLICY=COLLECTION_POLICY, COLLECTION_SETTINGS=COLLECTION_SETTINGS,
    database=database,
)
channel_http = create_channel_http(
    cache=read_cache, jobs=channel_jobs, library=library_http,
    API_KEY=API_KEY, COLLECTION_CONTEXT=COLLECTION_CONTEXT,
    COLLECTION_SETTINGS=COLLECTION_SETTINGS, DEPLOYMENT_PROFILE=DEPLOYMENT_PROFILE,
    YOUTUBE_API_TIMEOUT_SECONDS=YOUTUBE_API_TIMEOUT_SECONDS,
    YOUTUBE_QUOTA_ATTEMPT_RECORDER=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
    core=core, database=database,
)
app = FastAPI(lifespan=create_lifespan(
    core=core, cache=read_cache, YOUTUBE_QUOTA_ATTEMPT_RECORDER=YOUTUBE_QUOTA_ATTEMPT_RECORDER,
))
install_runtime_middleware(app, core=core)

# Private single-operator Saved work. No startup readiness query: an absent
# missing schema support fails explicitly on request without blocking the existing app.
if hasattr(app, "include_router"):
    from db.user_work.saved_views import SavedViewsRepository
    from saved_views import create_saved_views_router
    from db.user_work.saved_collections import SavedCollectionsRepository
    from saved_collections import create_saved_collections_router

    app.include_router(create_saved_views_router(SavedViewsRepository(database)))
    app.include_router(create_saved_collections_router(SavedCollectionsRepository(database)))

    from db.reviews.video_type_reviews import VideoTypeReviewsRepository
    from video_type_reviews import create_video_type_reviews_router

    VIDEO_TYPE_REVIEWS_REPOSITORY = VideoTypeReviewsRepository(database)

    def invalidate_video_type_review_reads(channel_id):
        read_cache.clear_frontend_read_cache(channel_id)
        read_cache.catalogue.invalidate_channel(channel_id)

    app.include_router(create_video_type_reviews_router(
        VIDEO_TYPE_REVIEWS_REPOSITORY, on_decision=invalidate_video_type_review_reads,
    ))

# User-work import and explicit reconnect commands do no provider work here.
# Reconnect is durable; the signal only shortens the worker's next queue poll.
if DEPLOYMENT_PROFILE == "self-hosted" and hasattr(app, "include_router"):
    from db.user_work.user_work_backup import UserWorkBackupRepository
    from user_work_service import create_user_work_router
    from operator_jobs import signal_operator_job_worker

    from db.channel.channel_trash import ChannelTrashRepository
    from channel_trash import create_channel_trash_router

    def invalidate_channel_trash_reads(channel_id):
        read_cache.clear_frontend_read_cache(channel_id)
        read_cache.catalogue.invalidate_channel(channel_id)

    app.include_router(create_channel_trash_router(
        ChannelTrashRepository(database), on_change=invalidate_channel_trash_reads,
        after_restore=channel_http.activate_restored_user_channel,
    ))

    app.include_router(create_user_work_router(
        UserWorkBackupRepository(database), on_reconnect=signal_operator_job_worker,
        on_change=lambda: read_cache.clear_frontend_read_cache(),
    ))

# Optional in-process telemetry routes used by the self-hosted interface.
if hasattr(app, "include_router") and os.getenv(
    "TELEMETRY_LEGACY_HTTP_ENABLED", "true"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    from telemetry_routes import router as telemetry_router

    app.include_router(telemetry_router)


# Keep the public route registration order, including retired 410 endpoints.
app.include_router(channel_http.add_router)
app.include_router(library_http.router)
app.include_router(create_explorer_router(
    cache=read_cache, library=library_http, core=core, database=database,
    CHANNEL_BASELINE_ANALYTICS_ENABLED=CHANNEL_BASELINE_ANALYTICS_ENABLED,
    CHANNEL_DERIVED_INSIGHTS_ENABLED=CHANNEL_DERIVED_INSIGHTS_ENABLED,
    CHANNEL_LAUNCH_ANALYTICS_ENABLED=CHANNEL_LAUNCH_ANALYTICS_ENABLED,
    CHANNEL_TIMELINE_INSIGHTS_ENABLED=CHANNEL_TIMELINE_INSIGHTS_ENABLED,
))
app.include_router(channel_http.backfill_router)
app.include_router(create_home_router(cache=read_cache, library=library_http, core=core, database=database))
app.include_router(create_system_router(
    cache=read_cache, PT=PT, core=core, database=database,
    VIDEO_TYPE_REVIEWS_REPOSITORY=VIDEO_TYPE_REVIEWS_REPOSITORY,
))
app.include_router(channel_http.hydration_router)
app.include_router(create_organization_router(cache=read_cache, library=library_http, database=database))
app.include_router(create_frontend_router(FRONTEND_DIST_DIR=FRONTEND_DIST_DIR))

# Every handler reads its body through this bound; owner access stays outermost.
from request_limits import RequestBodyLimitMiddleware
app.add_middleware(RequestBodyLimitMiddleware)

# Owner access wraps every handler and request-processing middleware.
if selected_deployment_profile() == "self-hosted":
    if os.getenv('LIC_SETUP_DIRECTORY'):
        from self_hosted_onboarding import SetupMiddleware
        app.add_middleware(SetupMiddleware)
    from self_hosted_auth import SelfHostedAuthMiddleware
    app.add_middleware(SelfHostedAuthMiddleware)

# This outer context adds no routes or responses and cannot bypass owner access.
# Setup middleware can answer without reaching the inner runtime middleware.
from web.middleware import HttpReadScopeMiddleware
app.add_middleware(HttpReadScopeMiddleware)
