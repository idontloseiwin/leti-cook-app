"""Real HTTP factories with explicit inert dependencies for offline tests."""
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from web.channels import create_channel_http


def endpoint(router, name):
    handlers = [route.endpoint for route in router.routes if route.name == name]
    if not handlers or any(handler is not handlers[0] for handler in handlers):
        raise AssertionError(f'Expected one endpoint named {name}')
    return handlers[0]


def unused(*args, **kwargs):
    raise AssertionError('Unexpected dependency use during an offline HTTP test')


def cache_stub():
    return SimpleNamespace(cached_read=lambda key, ttl, load: load(),
                           clear_frontend_read_cache=lambda *args: None,
                           catalogue=SimpleNamespace())


def library_stub(**overrides):
    return SimpleNamespace(**dict(dict(load_folders=lambda: [], load_visible_channel_ids=lambda: set(),
        load_visible_channel_or_404=unused, require_visible_channel_user_work=unused), **overrides))


def jobs_stub(**overrides):
    return SimpleNamespace(**dict(dict(_load_initial_hydration_state=unused,
        _prepare_channel_add_hydration=unused, _prepare_initial_typed_inventory=unused,
        _request_operator_job_worker_wake=unused, _with_initial_format_readiness=unused,
        operator_job_repository=unused), **overrides))


def channel_http(**overrides):
    dependencies = dict(cache=cache_stub(), jobs=jobs_stub(), library=library_stub(),
        API_KEY='synthetic_test_key', COLLECTION_CONTEXT=SimpleNamespace(
            sources=SimpleNamespace(fetch_playlist_items_page=unused)),
        COLLECTION_SETTINGS=SimpleNamespace(database_video_collection_enabled=True, typed_playlist_inventory_enabled=False),
        DEPLOYMENT_PROFILE='self-hosted', YOUTUBE_API_TIMEOUT_SECONDS=1,
        YOUTUBE_QUOTA_ATTEMPT_RECORDER=None, core=SimpleNamespace(VIDEO_TYPE_LONG='long'),
        database=object(), require_collection_ready=lambda: None, channel_add_admission=SimpleNamespace(acquire=lambda: 0))
    dependencies.update(overrides)
    return create_channel_http(**dependencies)


def system_http(*, cache=None):
    from zoneinfo import ZoneInfo
    from web.system import create_system_router
    recorder = SimpleNamespace(buffer_status=lambda: {'status': 'ok', 'pending_events': 0})
    return create_system_router(cache=cache or cache_stub(), PT=ZoneInfo('America/Los_Angeles'),
        core=SimpleNamespace(YOUTUBE_QUOTA_ATTEMPT_RECORDER=recorder,
                             YOUTUBE_QUOTA_REPOSITORY=object(), YOUTUBE_QUOTA_PROJECT_ID='test'),
        database=object(), VIDEO_TYPE_REVIEWS_REPOSITORY=SimpleNamespace(has_open=lambda: False))
