"""Process composition for collection entrypoints, with explicit database ownership.

Importing this module creates no client and reads no environment. Entrypoints
bootstrap core first, freeze their settings, then bind their owned/borrowed scope.
"""

from contextlib import contextmanager
from functools import partial
import os
import socket
import time
from uuid import uuid4

from collection.context import CollectionPolicy, CollectionSources
from collection import commands, video_payloads
from collection.settings import load_collection_settings
from db import ChannelSyncRepository, OperatorJobRepository
from operator_jobs import load_operator_job_settings, operator_jobs_mode, signal_operator_job_worker
from runtime.collection import bind_collection_context, video_collection_dependencies, video_collection_worker_id
from telemetry import emit_event
from typed_playlist import typed_playlist_id
from video_collection_service import YouTubeViewCountAbsent, YouTubeViewCountInvalid
from youtube_batch_stats import BATCH_STATS_COUNTER_PARTS, fetch_video_batch_stats, is_batch_stats_quota_error
from youtube_channels import fetch_channel_metadata_batch
from youtube_client import youtube_api_get
from youtube_errors import YouTubeAPIError, is_youtube_daily_quota_exhausted
from youtube_playlist import fetch_playlist_items_page
from youtube_source_fields import parse_dt
from youtube_video_profiles import fetch_video_metadata, fetch_initial_video_profiles


def load_collection_policy(environ):
    """Freeze the existing process gates after bootstrap has applied environment."""
    def enabled(name):
        return environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
    try:
        stale_hours = int(environ.get("SYNC_RUN_STALE_AFTER_HOURS", "24") or 24)
    except (TypeError, ValueError):
        stale_hours = 24
    # A format is collected when the owner lists it in
    # config.ini [collection] formats (this pass's snapshot) and its fixed
    # internal flag allows it. Long-form has no internal flag.
    from collection_formats import collection_formats
    configured = collection_formats(environ)
    allowed = {"long": True, "short": enabled("SHORT_VIDEO_COLLECTION_ENABLED"),
               "stream": enabled("COMPLETED_STREAM_COLLECTION_ENABLED")}
    formats = tuple(kind for kind in configured if allowed[kind])
    return CollectionPolicy(
        database_channel_discovery_enabled=enabled("DATABASE_CHANNEL_DISCOVERY_ENABLED"),
        short_video_collection_enabled="short" in formats,
        completed_stream_collection_enabled="stream" in formats,
        sync_run_stale_after_hours=max(1, min(stale_hours, 168)),
        video_formats=formats,
    )


def _policy_formats(context):
    """The pass's formats; a policy without the list keeps all."""
    from collection_formats import VIDEO_FORMATS
    return tuple(getattr(getattr(context, 'policy', None), 'video_formats', VIDEO_FORMATS))


def process_collection_configuration():
    # core's optional dotenv application precedes both snapshots.
    import core
    from runtime.collection_schedules import require_schedule_model
    require_schedule_model(core.COLLECTION_SCHEDULES)
    return load_collection_settings(os.environ), load_collection_policy(os.environ)


def bind_process_collection(*, database, settings, policy, inventory_discovery_admission=None):
    """Borrow the caller's gateway; process quota resources are never its children."""
    import core
    provider = dict(
        youtube_api_get=youtube_api_get, api_key=core.API_KEY,
        attempt_recorder=core.YOUTUBE_QUOTA_ATTEMPT_RECORDER,
        timeout_seconds=core.YOUTUBE_API_TIMEOUT_SECONDS,
        emit_event=emit_event, perf_counter=time.perf_counter,
    )
    quota_exhausted = partial(
        is_youtube_daily_quota_exhausted, is_batch_stats_quota_error=is_batch_stats_quota_error,
    )
    video = video_collection_dependencies(
        database=database, settings=settings, api_error=YouTubeAPIError,
        get_video_metadata=partial(fetch_video_metadata, **provider),
        get_initial_video_profiles=partial(fetch_initial_video_profiles, **provider),
        fetch_video_batch_stats=fetch_video_batch_stats,
        api_key=core.API_KEY, attempt_recorder=core.YOUTUBE_QUOTA_ATTEMPT_RECORDER,
        timeout_seconds=core.YOUTUBE_API_TIMEOUT_SECONDS, counter_parts=BATCH_STATS_COUNTER_PARTS,
        build_full_payload=partial(
            video_payloads.build_video_collection_metadata_payload, parse_dt=parse_dt,
            view_count_absent_error=YouTubeViewCountAbsent,
            view_count_invalid_error=YouTubeViewCountInvalid,
        ),
        build_counter_payload=video_payloads.build_video_counter_payload,
        is_quota_exhausted=quota_exhausted,
    )

    @contextmanager
    def fresh_channel_repository():
        # Each repository operation starts a fresh transaction; a broken
        # connection is discarded by the shared bounded pool.
        yield ChannelSyncRepository(database)

    # Capture configuration in this composition scope; a later environment
    # mutation cannot change the admission policy for its background callback.
    configuration = dict(os.environ)
    load_initial_settings = partial(load_operator_job_settings, environ=configuration)

    def enqueue_initial_inventory(channel_id, *, expected_epoch):
        if operator_jobs_mode(configuration) != "durable":
            raise RuntimeError("operator_jobs_draining")
        result = commands.enqueue_initial_inventory_for_channel(
            channel_id, expected_epoch=expected_epoch,
            channel_repository=ChannelSyncRepository(database),
            repository=OperatorJobRepository(database), load_settings=load_initial_settings,
        )
        if result["status"] in {"queued", "running"}:
            try:
                signal_operator_job_worker(environ=configuration)
            except Exception:
                # The persisted job and the existing timer own recovery. The
                # wake file is only a latency hint and carries no domain state.
                pass
        return result

    return bind_collection_context(
        **video, policy=policy,
        sources=CollectionSources(
            parse_dt=parse_dt,
            fetch_playlist_items_page=partial(fetch_playlist_items_page, **provider),
            fetch_channel_metadata_batch=partial(fetch_channel_metadata_batch, **provider),
            get_longform_playlist_id=partial(typed_playlist_id, video_type="long"),
            is_youtube_daily_quota_exhausted=quota_exhausted,
        ),
        fresh_channel_repository=fresh_channel_repository,
        enqueue_initial_inventory=enqueue_initial_inventory,
        supervised_environ=lambda: configuration,
        video_worker_id=partial(video_collection_worker_id, get_hostname=socket.gethostname, get_pid=os.getpid),
        discovery_worker_id=lambda: f"{socket.gethostname()}:{os.getpid()}:channel-discovery",
        inventory_discovery_admission=inventory_discovery_admission,
    )


@contextmanager
def process_collection_scope(*, settings, policy):
    """Borrow the process pool; transactions end before every provider call."""
    import core
    from runtime.discovery import discovery_budget_for
    from collection.inventory_admission import InventoryDiscoveryAdmission
    # Only collector entrypoints own the supervised interval and automatic
    # inventory admission. Backend route composition has neither authority.
    budget=discovery_budget_for(core.database,os.environ)
    admission=InventoryDiscoveryAdmission(budget) if budget is not None else None
    context=bind_process_collection(database=core.database,settings=settings,policy=policy,
                                   inventory_discovery_admission=admission)
    if budget is None:
        yield context
    else:
        import quota_buckets
        with quota_buckets.controlled(context.database,project_id=budget.project,
                settings=budget.settings,interval_minutes=budget.interval):
            yield context


def bind_process_common_dispatch(context, *, whole_cycle=False, discovery_deadline=None, clock=time.monotonic):
    """Bind ordered automatic phases to one existing operator service window.

    The compatibility callable closes after its common phase. The supervised
    whole-cycle binding closes only after all ordered phases or a global stop.
    """
    from collection.auto_pass import CommonCandidateCursor
    configuration = dict(os.environ)
    cursor = CommonCandidateCursor()
    from collection.inventory_feeder import AutoInventoryCursor
    from collection.metadata_feeder import AutoMetadataCursor
    inventory_cursor,metadata_cursor=AutoInventoryCursor(),AutoMetadataCursor()
    cursors=(cursor,metadata_cursor,inventory_cursor)
    if whole_cycle:
        from collection_shutdown import CURSOR_SNAPSHOT_ENV,decode_cursor_checkpoint
        encoded=configuration.get(CURSOR_SNAPSHOT_ENV)
        if encoded is not None:_restore_collection_cursors(cursors,decode_cursor_checkpoint(encoded))
    cached={}
    from runtime.discovery import discovery_budget,bind_discovery
    budget=discovery_budget(context)
    bound_context = context
    from pathlib import Path
    from operator_jobs import OPERATOR_JOB_WAKE_PATH_ENV, DEFAULT_OPERATOR_JOB_WAKE_PATH
    wake_path = Path(configuration.get(OPERATOR_JOB_WAKE_PATH_ENV) or DEFAULT_OPERATOR_JOB_WAKE_PATH)
    if not wake_path.is_absolute():
        raise ValueError('operator_job_wake_path_invalid')
    def wake_revision():
        try:
            return wake_path.stat().st_mtime_ns
        except OSError:
            return None

    def build(*, common_preflight_checked=False):
        import core
        from collection import initial
        from collection.cycle import run_common_operator_phase
        from collection_shutdown import handoff_requested
        from collection_outcome import copy_collection_stop
        from db import TypedInventoryRepository
        from hot_runtime import require_hot_runtime_ready
        from operator_jobs import operator_dispatch_turn, run_one_operator_job_chunk
        from provider_runtime import request_scope
        from runtime.collection import bind_reconnect_feeder
        from youtube_errors import YouTubePlaylistNotFoundError, YouTubeDailyQuotaExceededError

        operator_jobs_mode(configuration)
        video_repository, service = context.video.build_service()
        if not common_preflight_checked:
            video_repository.preflight_common_pool_ready()
        repository = OperatorJobRepository(context.database)
        schema = repository.schema_status()
        if (int(schema.get('schema_version') or 0) < 7 or not schema.get('ready')
                or schema.get('initial_quota_limit_enforced') is not False):
            raise RuntimeError('operator_jobs_schema_unavailable')
        settings = load_operator_job_settings(environ=configuration, require_quota_cap=False)
        concurrency = int(configuration.get('LIC_YOUTUBE_REQUEST_CONCURRENCY', '8'))
        ready = partial(require_hot_runtime_ready, operation='collection')
        initial_inventory = partial(initial.collect_initial_inventory_chunk,
            database=context.database, settings=context.settings,
            worker_id=context.video_worker_id() + ':typed-inventory',
            fetch_page=context.sources.fetch_playlist_items_page,
            get_longform_playlist_id=context.sources.get_longform_playlist_id,
            parse_dt=context.sources.parse_dt, playlist_not_found_error=YouTubePlaylistNotFoundError,
            daily_quota_exceeded_error=YouTubeDailyQuotaExceededError)
        initial_stats = partial(initial.initial_28d_stats_import, database=context.database,
            settings=context.settings, run_video_collection_queue_batch=context.video.run_batch,
            copy_collection_stop=copy_collection_stop)
        @contextmanager
        def fresh_repository():
            yield OperatorJobRepository(context.database)
        def operator_factory(**options):
            leaf_ready = options['require_ready']
            return operator_dispatch_turn(repository, inventory_repository=TypedInventoryRepository(context.database),
                video_service=service, fetch_page=context.sources.fetch_playlist_items_page,
                settings=settings, concurrency=concurrency,
                max_chunks=int(configuration.get('OPERATOR_JOB_DRAIN_MAX_CHUNKS') or 128),
                max_seconds=int(configuration.get('OPERATOR_JOB_DRAIN_MAX_SECONDS') or 900),
                legacy_chunk=lambda claimed: run_one_operator_job_chunk(repository, context.video.run_batch,
                    settings=settings, claimed_job=claimed, run_initial_inventory_chunk=initial_inventory,
                    run_initial_stats=initial_stats, fresh_repository_factory=fresh_repository),
                reconnect_factory=lambda **callbacks: bind_reconnect_feeder(context.database,
                    request=partial(youtube_api_get, api_key=core.API_KEY,
                        attempt_recorder=core.YOUTUBE_QUOTA_ATTEMPT_RECORDER),
                    require_ready=leaf_ready, **callbacks),
                **options)
        stopping=lambda:handoff_requested() or (discovery_deadline is not None and clock()>=discovery_deadline)
        resources=dict(service=service,concurrency=concurrency,require_ready=ready,
            clock=clock,should_stop=stopping,operator_factory=operator_factory,
            fetch_scope=request_scope,wake_revision=wake_revision)
        if whole_cycle:
            from collection.cycle import AutomaticDispatchWindow
            resources['window']=AutomaticDispatchWindow(**{key:value for key,value in resources.items() if key!='service'})
            from collection.inventory_admission import InventoryDiscoveryAdmission
            resources['window'].inventory_admission=InventoryDiscoveryAdmission(budget) if budget is not None else None
            from ops.run_discovery_worker import collection_deadline
            deadline=discovery_deadline if discovery_deadline is not None else collection_deadline(configuration,clock=clock)
            if deadline is not None:
                resources['window'].discovery=bind_discovery(context,service,resources['window'],deadline=deadline,budget=budget)
        return resources

    def ensure(common_preflight_checked=False):
        if not whole_cycle:return build(common_preflight_checked=common_preflight_checked)
        if not cached:cached.update(build(common_preflight_checked=common_preflight_checked))
        return cached

    def _run(*, limit, progress_callback=None, common_preflight_checked=False, context=context):
        from collection.cycle import run_common_operator_phase
        if context is not bound_context:raise ValueError('collection dependencies differ from their bound scope')
        resources=ensure(common_preflight_checked)
        return run_common_operator_phase(limit=limit,worker_id=context.video_worker_id()+':common',
            cursor=cursor,progress_callback=progress_callback,formats=_policy_formats(context),**resources)

    def window():
        if not whole_cycle:raise RuntimeError('collection_whole_cycle_required')
        return ensure(True)['window']

    def _metadata(*,limit,context=context):
        from collection.metadata_feeder import AutoMetadataFeeder
        from collection.channel_metadata import CHANNEL_METADATA_MAX_IDS_PER_REQUEST
        if context is not bound_context:raise ValueError('collection dependencies differ from their bound scope')
        limit=max(0,min(int(limit),100000));active=window()
        feeder=AutoMetadataFeeder(ChannelSyncRepository(context.database),context,
            channel_limit=limit,candidate_scan_limit=limit*CHANNEL_METADATA_MAX_IDS_PER_REQUEST,
            cursor=metadata_cursor,require_ready=active.ready,should_stop=active.stopping,on_settled=active.stop_all)
        return active.run_phase(feeder)

    def _inventory(*,limit,run_id,on_page,context=context,signals_only=False):
        from collection.budgets import PageBudget
        from collection.inventory import MAX_CLAIM_WINDOW
        from collection.inventory_feeder import AutoInventoryFeeder
        from collection.cycle import run_typed_playlist_inventory_batch
        from db import TypedInventoryRepository
        from youtube_errors import YouTubePlaylistNotFoundError
        from uuid import uuid4
        if context is not bound_context:raise ValueError('collection dependencies differ from their bound scope')
        if not context.settings.typed_playlist_inventory_enabled or limit<=0:
            return run_typed_playlist_inventory_batch(limit=0,context=context)
        from self_hosted_settings import FIELDS
        if type(limit) is not int or not FIELDS['inventory_page_budget'][1]<=limit<=FIELDS['inventory_page_budget'][2]:
            raise ValueError('inventory_page_budget_invalid')
        active=window();repository=TypedInventoryRepository(context.database)
        active.ready()
        prepared={} if signals_only else repository.prepare_runtime(limit=min(limit,1000),enable=True)
        feeder=AutoInventoryFeeder(repository,page_budget=PageBudget(limit),cursor=inventory_cursor,
            candidate_scan_limit=limit*MAX_CLAIM_WINDOW,run_id=str(run_id or uuid4()),
            worker_id=context.video_worker_id()+':typed-inventory',fetch_page=context.sources.fetch_playlist_items_page,
            require_ready=active.ready,concurrency=active.concurrency,should_stop=active.stopping,
            playlist_not_found_errors=(YouTubePlaylistNotFoundError,),on_page=on_page,on_settled=active.stop_all,
            formats=_policy_formats(context),discovery_admission=active.inventory_admission,signals_only=signals_only)
        feeder.summary['prepared']=int((prepared or {}).get('selected_channels') or
            (prepared or {}).get('states_inserted') or (prepared or {}).get('prepared') or 0)
        return active.run_phase(feeder)

    def observed_phase(phase, callback, **options):
        from pipeline_trace import observe_collection_phase
        return observe_collection_phase(phase, callback, **options)

    def run(*, limit, progress_callback=None, common_preflight_checked=False, context=context):
        return observed_phase("video_collection", _run, limit=limit, progress_callback=progress_callback,
            common_preflight_checked=common_preflight_checked, context=context)

    def metadata(*,limit,context=context):
        return observed_phase("channel_metadata", _metadata, limit=limit, context=context)

    def inventory(*,limit,run_id,on_page,context=context):
        return observed_phase("typed_inventory", _inventory, limit=limit, run_id=run_id, on_page=on_page, context=context)

    def discovery(*,wait_operator=False):
        active=window()
        if active.discovery is None:return dict(errors=0,enabled=False)
        return active.run_phase(active.discovery,wait_operator=wait_operator)

    def narrow():
        if budget is None:raise RuntimeError('discovery_settings_required')
        active=window()
        if active.discovery is None:raise RuntimeError('discovery_runtime_not_active')
        from self_hosted_settings import limits_from_environment
        limit=limits_from_environment(configuration)['inventory_page_budget']
        inventory_errors=0;inventory={}
        while not active.pass_stopping():
            progress_fields=('claimed','settled','provider_steps','hydration_claimed')
            before=tuple(active.discovery.summary.get(key,0) for key in progress_fields)
            inventory=_inventory(limit=limit,run_id=str(uuid4()),on_page=None,signals_only=True)
            inventory_errors+=int(inventory.get('errors') or 0)
            result=discovery(wait_operator=True)
            if active.pass_stopping() or active.discovery.summary.get('stopped_reason'):break
            from db.discovery import DiscoveryRepository
            status=DiscoveryRepository(context.database).runtime_status(project_id=budget.project,
                settings=budget.settings,interval_minutes=budget.interval,video_types=_policy_formats(context))
            progressed=(bool(inventory.get('processed')) or
                before!=tuple(active.discovery.summary.get(key,0) for key in progress_fields))
            if status['runnable'] and progressed:
                active.discovery.resume_idle()
                continue
            # Reuse this admitted child for late signals. The existing wake
            # poll and DB fallback stay bounded by its original fixed deadline;
            # no wake can replenish persisted budgets or start an ordinary sweep.
            if not active.wait_for_wake(active.discovery.deadline):break
        return dict(errors=inventory_errors+int(active.discovery.summary.get('errors') or 0),
                    typed_inventory=inventory,discovery=dict(active.discovery.summary),operator=active.operator.summary)

    def close():
        if cached:cached['window'].close()

    def finish(summary, *, failed=False):
        from collection_outcome import (BUCKET_STOP_FLAGS,PHASE_STOP_FLAGS,collection_stop_reason,
            copy_collection_stop,count_stop_as_error,exhausted_buckets)
        if cached:
            active=cached['window']
            if failed:active.stop_all(dict(settlement_unknown=True,infrastructure_error=True))
            else:active.stop_all(summary)
            active.close()
            if active.stop_reason:copy_collection_stop(summary,active.stop_flags)
            else:
                # A step stop, an exhausted bucket or a one-off job's stop
                # leaves the pass running: the run records it without a pass
                # stop reason.
                for name in (*PHASE_STOP_FLAGS,*BUCKET_STOP_FLAGS):
                    summary[name]=bool(summary.get(name) or active.stop_flags.get(name))
                if exhausted_buckets(summary):summary['quota_exhausted']=True
            summary['operator']=active.operator.summary
            if active.discovery is not None:summary['discovery']=dict(active.discovery.summary)
            # A failure is an error; waiting for an exhausted bucket is not.
            # An error added for a quota or 429 stop alone is explained.
            if collection_stop_reason(active.stop_flags,'pass') or collection_stop_reason(active.stop_flags,'phase'):
                count_stop_as_error(summary,active.stop_flags)

    def checkpoint():
        if not cached or not cached['window'].checkpoint_ready:return None
        return _capture_collection_cursors(cursors)

    if whole_cycle:
        run.metadata,run.inventory=metadata,inventory
        run.discovery,run.narrow=discovery,narrow
        run.close,run.checkpoint,run.finish=close,checkpoint,finish
    return run


def _restore_collection_cursors(cursors, value):
    from collections import deque
    from collection.auto_pass import COUNTERS_EMPTY_FRONTIERS
    from collection_shutdown import validate_cursor_checkpoint
    from db.channel.channel_sync import MetadataCandidateCursor
    from db.inventory.inventory_candidates import InventoryCandidateCursor
    value=validate_cursor_checkpoint(value)
    common,metadata,inventory=cursors
    common._after={(operation,kind):after for operation,positions in value['common']['after'].items()
                   for kind,after in positions.items()}
    common._formats=deque(value['common']['formats'])
    common._exhausted={(operation,kind) for operation,kinds in value['common']['exhausted'].items()
                      for kind in kinds} | COUNTERS_EMPTY_FRONTIERS
    common._revisit=value['common']['revisit']
    state=value['metadata'];metadata.after=MetadataCandidateCursor(**state['after']) if state['after'] is not None else None
    metadata.due_ceiling=state['due_ceiling'];metadata.sweep_exhausted=state['exhausted']
    state=value['inventory'];inventory._after=InventoryCandidateCursor(**state['after']) if state['after'] is not None else None
    inventory._ceiling=state['ceiling'];inventory._exhausted=state['exhausted']


def _capture_collection_cursors(cursors):
    from dataclasses import asdict
    from collection_shutdown import validate_cursor_checkpoint
    common,metadata,inventory=cursors
    if any(cursor._active is not None for cursor in cursors):
        raise RuntimeError('collection_cursor_drain_unconfirmed')
    after={operation:{} for operation in ('metadata','counters')}
    exhausted={operation:[] for operation in after}
    for (operation,kind),position in common._after.items():
        after[operation][kind]=position
    for operation,kind in sorted(common._exhausted):
        exhausted[operation].append(kind)
    return validate_cursor_checkpoint(dict(version=3,
        common=dict(after=after,formats=list(common._formats),
                    exhausted=exhausted,revisit=common._revisit),
        metadata=dict(after=asdict(metadata.after) if metadata.after is not None else None,
                      due_ceiling=metadata.due_ceiling,exhausted=metadata.sweep_exhausted),
        inventory=dict(after=asdict(inventory._after) if inventory._after is not None else None,
                       ceiling=inventory._ceiling,exhausted=inventory._exhausted)))
