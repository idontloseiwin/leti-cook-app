"""Application startup and shutdown; resources are provided by composition."""
from contextlib import asynccontextmanager
import os
from starlette.concurrency import run_in_threadpool
from deployment_profile import selected_deployment_profile


def create_lifespan(*, core, cache, YOUTUBE_QUOTA_ATTEMPT_RECORDER):
    clear_read_cache = cache.clear_read_cache
    CATALOGUE_BROWSE_CACHE = cache.catalogue

    @asynccontextmanager
    async def app_lifespan(app):
        runtime = owner_lock = onboarding = maintenance = reference_store = provider = projection = None
        try:
            from update_copy_guard import UpdateCopyGuard
            from update_hold import UpdateHoldGuard
            update_hold_guard = UpdateHoldGuard.from_environment()
            await run_in_threadpool(update_hold_guard.check)
            update_copy_guard = UpdateCopyGuard.from_environment()
            await run_in_threadpool(update_copy_guard.check, force=True)
            await run_in_threadpool(core.database.open)
            if selected_deployment_profile() == "self-hosted":
                from hot_runtime import HotRuntimeController, RuntimeOwnerLock, install_local_runtime
                from db.hot.hot_runtime import maintenance_repository
                from db.category_reference import CategoryReferenceRepository
                from youtube_category_reference import (
                    CategoryReferenceStore, install_managed_reference_store,
                )
                owner_lock = RuntimeOwnerLock(admission=core.HOT_ADMISSION_CONFIG)
                owner_lock.__enter__()
                from provider_runtime import start_backend
                from self_hosted_settings import YOUTUBE_REQUEST_CONCURRENCY_ENV
                provider = await run_in_threadpool(start_backend, os.environ,
                    concurrency=int(os.environ.get(YOUTUBE_REQUEST_CONCURRENCY_ENV, '8')))
                reference_store = CategoryReferenceStore(CategoryReferenceRepository(core.database))
                install_managed_reference_store(reference_store)
                await run_in_threadpool(reference_store.purge_expired)
                def clear_runtime_caches():
                    clear_read_cache()
                    CATALOGUE_BROWSE_CACHE.invalidate_all()
                maintenance = await run_in_threadpool(maintenance_repository, core.DATABASE_CONFIG,
                    cadence_snapshot=core.database.cadence_snapshot)
                runtime = HotRuntimeController(
                    maintenance,
                    local_cleanup=reference_store.purge_expired,
                    clear_caches=clear_runtime_caches,
                    on_ready=YOUTUBE_QUOTA_ATTEMPT_RECORDER.start,
                    update_copy_guard=update_copy_guard,
                    update_hold_guard=update_hold_guard,
                )
                install_local_runtime(runtime)
                app.state.hot_runtime = runtime
                # Parent-only, asynchronous: slow database checks must not delay
                # the first clock probe. Importing workers or reading status is quiet.
                runtime.time_trust.probe_if_due()
                await run_in_threadpool(runtime.initialize)
                runtime.start()
                # Readers fall back to the protected views whenever it lags.
                from db.inventory.search_projection import SearchProjectionRepository
                from research_projection_refresher import SearchProjectionRefresher
                projection = SearchProjectionRefresher(SearchProjectionRepository(core.database),
                                                       ready=runtime.require_collection_ready)
                app.state.research_projection = projection
                projection.start()
                if os.getenv('LIC_SETUP_DIRECTORY'):
                    from self_hosted_configuration import ConfigurationStore
                    from self_hosted_onboarding import SetupManager, install_manager
                    from self_hosted_collection import CollectionSupervisor
                    onboarding = SetupManager(ConfigurationStore(os.environ['LIC_SETUP_DIRECTORY']))
                    onboarding.collection = CollectionSupervisor(onboarding, ready=runtime.require_collection_ready, schedules=core.COLLECTION_SCHEDULES, update_hold_guard=update_hold_guard,
                        provider_snapshot=provider.environment())
                    install_manager(onboarding)
                    app.state.setup_manager = onboarding
                    onboarding.collection.start()
            else:
                YOUTUBE_QUOTA_ATTEMPT_RECORDER.start()
            yield
        finally:
            try:
                if onboarding is not None:
                    try:
                        onboarding.close()
                    finally:
                        from self_hosted_onboarding import install_manager
                        install_manager(None)
            finally:
                try:
                    try:
                        from provider_runtime import stop_runtime
                        await run_in_threadpool(stop_runtime, provider)
                    finally:
                        await run_in_threadpool(YOUTUBE_QUOTA_ATTEMPT_RECORDER.close)
                finally:
                    try:
                        if runtime is not None:
                            from hot_runtime import remove_local_runtime
                            try:
                                if projection is not None:
                                    await run_in_threadpool(projection.close)
                                    app.state.research_projection = None
                            finally:
                                try:
                                    await run_in_threadpool(runtime.close)
                                finally:
                                    remove_local_runtime(runtime)
                    finally:
                        try:
                            if maintenance is not None:
                                await run_in_threadpool(maintenance.close)
                        finally:
                            try:
                                if reference_store is not None:
                                    from youtube_category_reference import remove_managed_reference_store
                                    await run_in_threadpool(remove_managed_reference_store, reference_store)
                            finally:
                                try:
                                    await run_in_threadpool(core.database.close)
                                finally:
                                    if owner_lock is not None:
                                        owner_lock.__exit__(None, None, None)

    return app_lifespan
