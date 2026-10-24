"""Operational status, quota and pipeline HTTP adapters."""
from fastapi import HTTPException, APIRouter
from db import SyncRunRepository, VideoCollectionRepository
from db.reads.queue_health_queries import get_system_queue_health
from collection_outcome import sync_run_explanation
from youtube_pipeline_health import (collect_pipeline_health, env_flag as pipeline_health_env_flag,
                                     read_collection_capacity, video_queue_capacity_warning)
from datetime import datetime, timedelta, timezone
from release_version import read_version
from channel_hydration import int_value
import json
import ui_read_trace
from youtube_quota import load_quota_read_model
from source_log import source_log_message
from .constants import COLLECTION_PAUSED_TEXT, KEY_REFUSAL_TEXT


def create_system_router(*, cache, PT, core, database, VIDEO_TYPE_REVIEWS_REPOSITORY):
    router = APIRouter()
    cached_read = cache.cached_read

    def get_quota_reset_at():
        # The moment itself, so every viewer reads it in their own time zone.
        now_pt = datetime.now(PT)
        next_midnight_pt = datetime(now_pt.year, now_pt.month, now_pt.day, tzinfo=PT) + timedelta(days=1)
        return next_midnight_pt.astimezone(timezone.utc).isoformat()


    def read_quota_summary():
        quota_day = datetime.now(PT).date().isoformat()
        # Snapshot pending evidence first: a concurrent flush may acknowledge it
        # after the following database read has already observed older rows.
        buffer = core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.buffer_status()
        read_model = load_quota_read_model(
            core.YOUTUBE_QUOTA_REPOSITORY,
            quota_day,
        )
        warnings = list(read_model["warnings"])
        units_used = read_model["units_used"]
        if buffer["status"] != "ok" or buffer["pending_events"]:
            if read_model["read_status"] == "ok":
                read_model["read_status"] = "partial"
            warnings.append({
                "code": "quota_journal_unavailable" if buffer["status"] != "ok" else "quota_journal_pending",
                "message": (
                    "Local quota journal needs attention; the ledger may be incomplete."
                    if buffer["status"] != "ok" else
                    "Quota events are waiting to reach the database; the ledger may lag."
                ),
            })

        quota = {
            "units_used": units_used,
            "used": units_used,
            "limit": 10000,
            "remaining": max(0, 10000 - units_used) if read_model["read_status"] == "ok" else None,
            "reset_at": get_quota_reset_at(),
            "quota_day": quota_day,
            "project_id": core.YOUTUBE_QUOTA_PROJECT_ID,
            "bucket_units": read_model["bucket_units"],
            "ledger_attempts": read_model["ledger_attempts"],
            "ledger_truncated": read_model["ledger_truncated"],
            "read_model_source": read_model["read_model_source"],
            "read_status": read_model["read_status"],
            "buffer": buffer,
            "remaining_is_ledger_estimate": (
                read_model["read_status"] == "ok"
            ),
            "remaining_is_google_console_authoritative": False,
        }
        if warnings:
            quota["warnings"] = warnings
        return quota


    def load_quota_summary():
        # Both HTTP consumers share the disposable read cache and journal lag state.
        return cached_read("api-quota", 5, read_quota_summary)


    def compact_run_summary(summary):
        if not summary:
            return {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except Exception:
                return {}
        if not isinstance(summary, dict):
            return {}

        compact = {}
        for key, value in summary.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact[key] = value
            elif isinstance(value, list):
                compact[f"{key}_count"] = len(value)
            elif isinstance(value, dict):
                nested = {
                    nested_key: nested_value
                    for nested_key, nested_value in value.items()
                    if isinstance(nested_value, (str, int, float, bool)) or nested_value is None
                }
                if nested:
                    compact[key] = nested
        return compact


    def compact_sync_run(row):
        compact = dict(row or {})
        compact["explanation"] = sync_run_explanation(row or {})
        compact["summary_json"] = compact_run_summary(compact.get("summary_json"))
        return compact


    def load_recent_sync_runs_state(limit=10):
        """Keep read availability distinct from a successful empty history."""
        try:
            with ui_read_trace.scope("recent_sync_runs"):
                rows = SyncRunRepository(database).list_recent(limit=limit)
                return {"status": "ok", "runs": [compact_sync_run(row) for row in rows]}
        except Exception as error:
            ui_read_trace.failure("recent_sync_runs", error)
            print(
                source_log_message('summary_sync_runs_error', lambda: ("[system-summary] sync_runs skipped error_type=" + error.__class__.__name__), error=error),
                flush=True,
            )
            return {"status": "unavailable", "runs": []}


    def load_recent_sync_runs(limit=10):
        # Compatibility for callers that still require the fail-soft list alone.
        return load_recent_sync_runs_state(limit=limit)["runs"]


    def load_system_queue_health():
        try:
            if pipeline_health_env_flag("DATABASE_VIDEO_COLLECTION_ENABLED"):
                observed = get_system_queue_health(database, discovery_enabled=
                    pipeline_health_env_flag("DATABASE_CHANNEL_DISCOVERY_ENABLED"))
                row = observed["videos"]
                return {
                    **row,
                    "channel_discovery": observed["channel_discovery"],
                    "channel_metadata": observed["channel_metadata"],
                    "mode": "leased_video_collection_queue",
                    "all_due": int_value(row.get("due")),
                    "scope": "all_types",
                    "collection_capacity": read_collection_capacity(),
                }
            return {"mode": "disabled", "all_due": None}
        except Exception as error:
            ui_read_trace.failure("queue_health", error)
            print(
                source_log_message('summary_queue_health_error', lambda: ("[system-summary] queue health skipped: "
                + error.__class__.__name__), error=error),
                flush=True,
            )
            return None


    def system_warning(code, message, count=None):
        warning = {"code": code, "message": message}
        if count is not None:
            warning["count"] = count
        return warning


    def build_system_warnings(queue_health=None, quota=None):
        warnings = []
        if queue_health is None:
            warnings.append(system_warning("queue_health_unavailable", "Current video queue state could not be read."))
        queue_health = queue_health or {}
        if queue_health.get("mode") == "disabled":
            warnings.append(system_warning(
                "database_video_collection_disabled",
                "Database video collection is disabled; the legacy stats runtime has been retired.",
            ))
        elif queue_health.get("mode") == "leased_video_collection_queue":
            warning = video_queue_capacity_warning(
                queue_health, queue_health.get("collection_capacity"),
            )
            if warning:
                warnings.append(warning)
            failed = int_value(queue_health.get("failed"))
            if failed:
                warnings.append(system_warning("video_queue_failures", "Current video queue entries still have a recorded failure.", failed))
            for key, label in (("channel_discovery", "Playlist discovery"), ("channel_metadata", "Channel metadata")):
                observed = queue_health.get(key) or {}
                if observed.get("mode") == "disabled":
                    warnings.append(system_warning("database_channel_discovery_disabled", "Database playlist discovery is disabled."))
                    continue
                failed = int_value(observed.get("failed"))
                if failed:
                    warnings.append(system_warning(key + "_failures", label + ": current queue entries still have a recorded failure.", failed))
        else:
            fresh_due = int_value(queue_health.get("fresh_due_under_7d"))
            if fresh_due > 0:
                warnings.append(system_warning(
                    "fresh_due_overdue",
                    f"{fresh_due} fresh videos are due for stats refresh.",
                    fresh_due,
                ))

        for warning in (quota or {}).get("warnings") or []:
            if isinstance(warning, dict):
                warnings.append(warning)
        return warnings


    def derive_system_status(latest=None, queue_health=None, quota=None):
        warnings = build_system_warnings(queue_health=queue_health, quota=quota)
        if not latest:
            return "unknown", "no_sync_runs", warnings
        if latest.get("status") == "running":
            return "running", "sync_running", warnings
        if int_value(latest.get("errors")) > 0 or latest.get("status") == "error":
            return "error", "latest_run_failed", warnings
        summary = latest.get("summary_json")
        summary = summary if isinstance(summary, dict) else {}
        collection = summary.get("video_collection")
        collection = collection if isinstance(collection, dict) else {}
        # Read recorded evidence only: old completed rows did not prove coverage.
        if latest.get("status") == "partial" or any(
            value.get("incomplete") is True or value.get("completion_outcome") == "partial"
            for value in (latest, summary, collection)
        ):
            warnings.insert(0, system_warning(
                "latest_run_partial",
                "The latest sync left selected collection work unresolved; confirmed changes are saved.",
            ))
            return "warning", "latest_run_partial", warnings
        if warnings:
            return "warning", warnings[0].get("code") or "system_warning", warnings
        return "ok", "latest_run_completed", warnings


    def current_key_refusal():
        from self_hosted_configuration import key_refusal
        return key_refusal()


    def current_collection_paused():
        from self_hosted_configuration import collection_paused
        return collection_paused()


    def load_youtube_pipeline_health():
        report = collect_youtube_pipeline_health()
        refusal = current_key_refusal()
        if refusal:
            report = {**report, "status": "critical", "provider_key": {"status": refusal},
                      "critical": [KEY_REFUSAL_TEXT[refusal], *(report.get("critical") or [])]}
        elif current_collection_paused():
            # Turned off on purpose: failures recorded before the pause are history, not an outage now.
            report = {**report, "status": "paused", "collection": {"state": "paused"}}
        return report


    def collect_youtube_pipeline_health():
        return collect_pipeline_health(
            database,
            quota_buffer_status_reader=core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.buffer_status,
            batch_stats_enabled=pipeline_health_env_flag("YOUTUBE_BATCH_STATS_ENABLED"),
            database_video_collection_enabled=pipeline_health_env_flag(
                "DATABASE_VIDEO_COLLECTION_ENABLED"
            ),
            database_channel_discovery_enabled=pipeline_health_env_flag(
                "DATABASE_CHANNEL_DISCOVERY_ENABLED"
            ),
        )


    @router.get("/system/pipeline-health")
    def system_pipeline_health():
        return cached_read(
            "system-pipeline-health",
            15,
            load_youtube_pipeline_health,
        )


    @router.get("/system-summary")
    def system_summary():
        value = cached_read("system-summary", 3, load_system_summary)
        if value["recent_runs_read_status"] != "ok":
            ui_read_trace.unavailable("recent_sync_runs")
        if value["queue_health"] is None:
            ui_read_trace.unavailable("queue_health")
        if not value["video_type_reviews"]["available"]:
            ui_read_trace.unavailable("video_type_reviews")
        if value["quota"].get("read_status") == "unavailable":
            ui_read_trace.unavailable("quota_summary")
        return value


    @router.get("/system/run-history")
    def system_run_history():
        # Stored run context must not wait for queue/quota/type diagnostics.
        value = cached_read("system-run-history", 3, load_run_history)
        if value["recent_runs_read_status"] != "ok":
            ui_read_trace.unavailable("recent_sync_runs")
        return value


    def load_run_history():
        state = load_recent_sync_runs_state(limit=10)
        runs = state["runs"]
        return {
            "latest_run": runs[0] if runs else None,
            "recent_runs": runs,
            "recent_runs_read_status": state["status"],
            "now": datetime.now(timezone.utc).isoformat(),
        }


    def load_video_type_review_summary():
        # Read-only current-evidence EXISTS; no provider call or write.
        try:
            with ui_read_trace.scope("video_type_reviews"):
                has_open = VIDEO_TYPE_REVIEWS_REPOSITORY.has_open()
                if type(has_open) is not bool:
                    raise ValueError("Invalid review summary")
                return {"available": True, "has_open": has_open}
        except Exception as error:
            # Missing migration/failure remains isolated from the main dashboard.
            ui_read_trace.failure("video_type_reviews", error)
            return {"available": False, "has_open": None}


    def load_system_summary():
        run_state = system_run_history()
        runs = run_state["recent_runs"]
        latest = run_state["latest_run"]
        queue_health = cached_read(
            "system-queue-health",
            15,
            load_system_queue_health,
        )
        quota = load_quota_summary()
        status, status_reason, warnings = derive_system_status(
            latest=latest,
            queue_health=queue_health,
            quota=quota,
        )
        from hot_runtime import current_update_copy_status
        from update_copy_guard import warning_for_status
        with ui_read_trace.scope("update_copy_status"):
            copy_warning = warning_for_status(current_update_copy_status())
        if copy_warning:
            warnings.append(system_warning(copy_warning['code'], copy_warning['message']))
            if status in {'ok', 'unknown'}:
                status, status_reason = 'warning', copy_warning['code']
        review_summary = cached_read("video-type-review-summary", 15, load_video_type_review_summary)
        if review_summary.get("has_open") is True:
            warning = system_warning("video_type_review_required", "Type checks need attention in Health.")
            warnings.insert(0, warning)
            if status in {"ok", "warning", "unknown"}:
                status, status_reason = "warning", warning["code"]
        refusal = current_key_refusal()
        paused = not refusal and current_collection_paused()
        if refusal:
            warnings.insert(0, system_warning(refusal, KEY_REFUSAL_TEXT[refusal]))
            status, status_reason = "error", refusal
        elif paused:
            warnings.insert(0, system_warning("collection_paused", COLLECTION_PAUSED_TEXT))
            status, status_reason = "paused", "collection_paused"

        return {
            "status": status,
            "version": read_version(),
            "status_reason": status_reason,
            "warnings": warnings,
            "provider_key": {"status": refusal} if refusal else None,
            "collection": {"state": "paused"} if paused else None,
            "video_type_reviews": review_summary,
            "latest_run": latest,
            "recent_runs": runs,
            "recent_runs_read_status": run_state["recent_runs_read_status"],
            "queue_health": queue_health,
            "quota": quota,
            "now": datetime.now(timezone.utc).isoformat(),
        }


    @router.post("/run-deep-stats-channel", deprecated=True)
    async def run_deep_stats_channel():
        raise HTTPException(
            status_code=410,
            detail={
                "code": "legacy_channel_stats_retired",
                "message": "Use channel hydration or durable current-stats backfill.",
                "hydration": "/channels/{channel_id}/hydration",
                "current_stats": "/channels/{channel_id}/videos/long/backfill-current-stats",
            },
            headers={"Cache-Control": "no-store"},
        )


    return router
