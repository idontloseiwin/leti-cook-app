import argparse
import json
import os
import sys
import traceback
from contextlib import ExitStack
from functools import partial
from datetime import datetime, timezone

from deployment_profile import selected_deployment_profile
from operational_copy_policy import safe_summary_copy
from pipeline_trace import bind as trace_bind, scope as trace_scope


def parse_args(argv=None):
    from self_hosted_settings import preset
    defaults = {name: preset('standard')[name] for name in
                ('metadata_limit', 'inventory_page_budget', 'stats_limit', 'new_video_stats_limit')}
    if selected_deployment_profile() == 'self-hosted':
        from self_hosted_settings import PROFILE, limits_from_environment
        if PROFILE in os.environ:
            configured = limits_from_environment(os.environ)
            defaults.update({key: configured[key] for key in defaults})
    parser = argparse.ArgumentParser(description="Run one YouTube Tracker auto-sync cycle.")
    parser.add_argument("--metadata-limit", type=int, default=defaults["metadata_limit"])
    parser.add_argument("--inventory-page-budget", type=int, default=defaults["inventory_page_budget"],
                        help="Maximum YouTube playlist page attempts in this pass, across all types.")
    parser.add_argument("--stats-limit", type=int, default=defaults["stats_limit"])
    parser.add_argument("--new-video-stats-limit", type=int, default=defaults["new_video_stats_limit"])
    # Compatibility-only flags; the common stats-limit pool owns all types.
    parser.add_argument("--short-video-collection-limit", type=int, default=0)
    parser.add_argument("--completed-stream-collection-limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if selected_deployment_profile() == 'self-hosted':
        from self_hosted_settings import FIELDS, SettingsError
        for name in defaults:
            if not FIELDS[name][1] <= getattr(args, name) <= FIELDS[name][2]:
                raise SettingsError('invalid_' + name)
        if args.new_video_stats_limit > args.stats_limit:
            raise SettingsError('new_stats_exceeds_common_pool')
    return args


def main():
    args = parse_args()
    limits = {
        "metadata_limit": args.metadata_limit,
        "inventory_page_budget": args.inventory_page_budget,
        "stats_limit": args.stats_limit,
        "new_video_stats_limit": args.new_video_stats_limit,
        "short_video_collection_limit": args.short_video_collection_limit,
        "completed_stream_collection_limit": args.completed_stream_collection_limit,
    }
    run_id = None
    run_started_at = datetime.now(timezone.utc).isoformat()
    finish_sync_run = None
    dispatch = None
    finalization_attempted = False

    if args.dry_run:
        print("[auto-sync-entrypoint] dry-run is not implemented yet; no sync was run.")
        return 0

    print(
        "[auto-sync-entrypoint] start "
        f"metadata_limit={args.metadata_limit} "
        f"inventory_page_budget={args.inventory_page_budget} "
        f"stats_limit={args.stats_limit} "
        f"new_video_stats_limit={args.new_video_stats_limit} "
        f"short_video_collection_limit={args.short_video_collection_limit} "
        f"completed_stream_collection_limit={args.completed_stream_collection_limit}",
        flush=True,
    )

    with ExitStack() as scope:
        process_trace = scope.enter_context(trace_scope("auto_worker"))
        try:
            from collection import api as collection_api
            from runtime.process_collection import process_collection_configuration, process_collection_scope

            from core import process_database_resources
            settings, policy = process_collection_configuration()
            scope.enter_context(process_database_resources())
            context = scope.enter_context(process_collection_scope(settings=settings, policy=policy))
            finish_sync_run = partial(collection_api.finish_sync_run, context=context)
            run_id = collection_api.start_sync_run(limits=limits, started_at=run_started_at, context=context)
            scope.enter_context(trace_bind(sync_run_id=run_id))
            common_options = {}
            if selected_deployment_profile() == 'self-hosted' and os.getenv('LIC_COLLECTION_SUPERVISED') == '1':
                from runtime.process_collection import bind_process_common_dispatch
                dispatch = bind_process_common_dispatch(context, whole_cycle=True)
                scope.callback(dispatch.close)
                common_options['common_dispatch'] = dispatch
            summary = collection_api.run_auto_sync_cycle(
                metadata_limit=args.metadata_limit,
                inventory_page_budget=args.inventory_page_budget,
                stats_limit=args.stats_limit,
                new_video_stats_limit=args.new_video_stats_limit,
                short_video_collection_limit=args.short_video_collection_limit,
                completed_stream_collection_limit=args.completed_stream_collection_limit,
                sync_run_id=run_id,
                context=context,
                **common_options,
            )
            if dispatch is not None:dispatch.finish(summary)
            finalization_attempted = True
            receipt_confirmed = finish_sync_run(run_id, summary, started_at=run_started_at) is True
            if not receipt_confirmed:
                # An unconfirmed final receipt is not a clean worker exit.
                summary['errors'] = max(int(summary.get('errors') or 0), 1)
            self_hosted = selected_deployment_profile() == "self-hosted"
            print(json.dumps(safe_summary_copy(summary) if self_hosted else summary, indent=2, sort_keys=True))

            errors = int(summary.get("errors") or 0)
            if errors == 0 and receipt_confirmed and dispatch is not None:
                checkpoint = dispatch.checkpoint()
                if checkpoint is not None:
                    from collection_shutdown import CURSOR_COMPLETE_PREFIX, encode_cursor_checkpoint
                    sys.stderr.write(CURSOR_COMPLETE_PREFIX.decode('ascii') + encode_cursor_checkpoint(checkpoint) + '\n')
                    sys.stderr.flush()
            print(f"[auto-sync-entrypoint] done errors={errors}", flush=True)
            process_trace.finish("error" if errors > 0 else "completed", sync_run_id=run_id)
            return 1 if errors > 0 else 0
        except Exception as error:
            # Imported runtime modules may load .env before this copy boundary.
            self_hosted = selected_deployment_profile() == "self-hosted"
            if self_hosted:
                print(f"[auto-sync-entrypoint] stage=cycle error_type={type(error).__name__}", flush=True)
            else:
                print(f"[auto-sync-entrypoint] error {error}", flush=True)
                traceback.print_exc()
            if dispatch is not None:dispatch.finish({}, failed=True)
            if finish_sync_run and not finalization_attempted:
                now = datetime.now(timezone.utc).isoformat()
                failure = {
                    "started_at": now,
                    "finished_at": now,
                    "errors": 1,
                    "error": type(error).__name__ if self_hosted else str(error),
                    "timings": {"total_seconds": None},
                }
                try:
                    finalization_attempted = True
                    finish_sync_run(run_id, failure, started_at=run_started_at)
                except Exception as finish_error:
                    if not self_hosted:
                        raise
                    print(f"[auto-sync-entrypoint] stage=finish error_type={type(finish_error).__name__}", flush=True)
            process_trace.finish("unknown", sync_run_id=run_id, reason="worker_failed")
            return 1


if __name__ == "__main__":
    sys.exit(main())
