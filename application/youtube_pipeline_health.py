#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from db import (
    ChannelDiscoveryRepository,
    ChannelSyncRepository,
    SyncRunRepository,
    SystemHealthRepository,
    YouTubeQuotaRepository,
)
from channel_sync_policy import channel_sync_eligibility
from db.reads.system_health import CHANNEL_HEALTH_COLUMNS
from db.reads.queue_health_queries import get_all_video_queue_health, get_all_discovery_queue_health
from collection_outcome import sync_run_explanation


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path[:0] = [str(ROOT)]


PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_WINDOW_HOURS = 24
DEFAULT_MAX_SYNC_AGE_MINUTES = 90
DEFAULT_STALE_SYNC_MINUTES = 120
DEFAULT_STALE_ATTEMPT_MINUTES = 10
DEFAULT_QUEUE_SCAN_LIMIT = 10_000
DEFAULT_PAGE_SIZE = 1_000
DEFAULT_GENERAL_QUOTA_LIMIT = 10_000
DEFAULT_BATCH_QUOTA_LIMIT = 10_000
DEFAULT_QUOTA_PROJECT_ID = "primary"


CHANNEL_COLUMNS = CHANNEL_HEALTH_COLUMNS


def utc_now():
    return datetime.now(timezone.utc)


def parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value):
    parsed = parse_datetime(value)
    return parsed.isoformat() if parsed else None


def age_minutes(value, now):
    parsed = parse_datetime(value)
    if not parsed:
        return None
    return round(max((now - parsed).total_seconds(), 0.0) / 60.0, 3)


def number(value, default=0):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def rounded(value, digits=3):
    return round(float(value), digits)


def duration_stats(values):
    values = [float(value) for value in values if value is not None]
    if not values:
        return {"count": 0, "min": None, "avg": None, "median": None, "max": None}
    return {
        "count": len(values),
        "min": rounded(min(values)),
        "avg": rounded(sum(values) / len(values)),
        "median": rounded(statistics.median(values)),
        "max": rounded(max(values)),
    }


def sorted_counts(values):
    return dict(sorted(Counter(str(value) for value in values if value is not None).items()))


def safe_count_map(value):
    if not isinstance(value, dict):
        return {}
    counts = {}
    for key, count in value.items():
        if key is None:
            continue
        counts[str(key)] = max(int(number(count, 0)), 0)
    return dict(sorted(counts.items()))


def streak_bucket(value):
    streak = max(int(number(value, 0)), 0)
    if streak == 0:
        return "0_hot"
    if streak == 1:
        return "1"
    if streak == 2:
        return "2"
    if streak <= 5:
        return "3_5"
    return "6_plus"


def configure_query(query, operations):
    for method_name, args, kwargs in operations:
        query = getattr(query, method_name)(*args, **kwargs)
    return query


def fetch_paginated(
    client,
    table_name,
    columns,
    operations=(),
    page_size=DEFAULT_PAGE_SIZE,
    max_rows=100_000,
):
    return SystemHealthRepository(client).fetch_paginated(
        table_name,
        columns,
        operations=operations,
        page_size=page_size,
        max_rows=max_rows,
    )


def fetch_rpc_rows(client, rpc_name, params=None):
    return SystemHealthRepository(client).fetch_rpc_rows(rpc_name, params)


def quota_clock(now):
    pacific_now = now.astimezone(PACIFIC)
    quota_day = pacific_now.date()
    reset_day = quota_day + timedelta(days=1)
    reset_local = datetime.combine(reset_day, time.min, tzinfo=PACIFIC)
    reset_utc = reset_local.astimezone(timezone.utc)
    return {
        "quota_day": quota_day.isoformat(),
        "timezone": "America/Los_Angeles",
        "next_reset_at": reset_utc.isoformat(),
        "minutes_until_reset": round((reset_utc - now).total_seconds() / 60.0, 3),
    }


def safe_phase_timings(summary):
    timings = summary.get("timings") if isinstance(summary, dict) else None
    if not isinstance(timings, dict):
        return {}
    return {
        key: rounded(value, 6)
        for key, value in sorted(timings.items())
        if key.endswith("_seconds") and isinstance(value, (int, float))
    }


def safe_regular_stats(summary):
    regular = summary.get("regular_video_stats") if isinstance(summary, dict) else None
    if not isinstance(regular, dict):
        return {}
    allowed_numeric = (
        "requested",
        "updated",
        "batches",
        "errors",
        "missing",
        "unavailable_marked",
        "batch_requests",
        "metadata_requests",
        "fallback_requests",
        "metadata_refresh_requests",
        "metadata_refresh_requested",
        "batch_recurring_requested",
        "presence_fallback_ids",
        "failed_id_fallbacks",
        "degraded_batch_requests",
        "partial_counter_updates",
        "metadata_deferred",
        "source_view_count_absent",
        "source_view_count_invalid",
        "metadata_duplicate_items",
    )
    result = {
        key: int(number(regular.get(key), 0))
        for key in allowed_numeric
    }
    routing = regular.get("routing")
    if isinstance(routing, dict):
        result["routing"] = {
            "mode": routing.get("mode") if isinstance(routing.get("mode"), str) else None,
            "batch_stats_enabled": bool(routing.get("batch_stats_enabled")),
            "database_queue_enabled": bool(routing.get("database_queue_enabled")),
            "hydration_requested": int(number(routing.get("hydration_requested"), 0)),
            "recurring_requested": int(number(routing.get("recurring_requested"), 0)),
        }
    return result


def safe_collection_outcome(summary):
    """Expose recorded coverage only; legacy completed is not proof of it."""
    summary = summary if isinstance(summary, dict) else {}
    outcome = summary.get("completion_outcome")
    return {
        "completion_outcome": (
            outcome if isinstance(outcome, str)
            and outcome in {"completed", "partial", "error"} else None
        ),
        "incomplete": summary.get("incomplete") if isinstance(summary.get("incomplete"), bool) else None,
        "unresolved": int(number(summary.get("unresolved"), 0)) if "unresolved" in summary else None,
    }


def safe_video_collection(summary):
    """Read the common-pool aggregate without replacing legacy Long fields."""
    collection = summary.get("video_collection") if isinstance(summary, dict) else None
    if not isinstance(collection, dict):
        return {}
    result = safe_regular_stats({"regular_video_stats": collection})
    for key in (
        "claimed", "stream_lifecycle_probed", "publication_waiting", "explicit_selected", "explicit_unclaimed",
        "long_requested", "short_requested", "stream_requested",
        "long_updated", "short_updated", "stream_updated",
        "long_missing", "short_missing", "stream_missing",
        "long_unavailable_marked", "short_unavailable_marked", "stream_unavailable_marked",
    ):
        result[key] = int(number(collection.get(key), 0))
    return {**result, **safe_collection_outcome(collection)}


def safe_discovery_summary(summary):
    discovery = summary.get("channel_discovery") if isinstance(summary, dict) else None
    if not isinstance(discovery, dict):
        return {}
    allowed_numeric = (
        "processed",
        "new_videos_found",
        "no_new_videos",
        "catchup_pending",
        "playlist_not_found",
        "errors",
        "availability_recheck_restored",
    )
    result = {
        key: int(number(discovery.get(key), 0))
        for key in allowed_numeric
    }
    safe_results = [row for row in discovery.get("results") or [] if isinstance(row, dict)]
    if isinstance(discovery.get("result_status_counts"), dict):
        result["result_status_counts"] = safe_count_map(
            discovery.get("result_status_counts")
        )
    else:
        result["result_status_counts"] = sorted_counts(
            row.get("status") for row in safe_results
        )
    if isinstance(discovery.get("schedule_reason_counts"), dict):
        result["schedule_reason_counts"] = safe_count_map(
            discovery.get("schedule_reason_counts")
        )
    else:
        result["schedule_reason_counts"] = sorted_counts(
            row.get("schedule_reason") for row in safe_results
        )
    if isinstance(discovery.get("streak_buckets"), dict):
        result["streak_buckets"] = safe_count_map(
            discovery.get("streak_buckets")
        )
    else:
        result["streak_buckets"] = sorted_counts(
            streak_bucket(row.get("no_new_streak")) for row in safe_results
        )
    return result


def safe_latest_sync(row, now):
    if not row:
        return None
    summary = row.get("summary_json") if isinstance(row.get("summary_json"), dict) else {}
    result = {
        "status": row.get("status"),
        "started_at": iso_utc(row.get("started_at")),
        "finished_at": iso_utc(row.get("finished_at")),
        "age_minutes": age_minutes(row.get("finished_at") or row.get("started_at"), now),
        "duration_seconds": rounded(number(row.get("duration_seconds"), 0.0)),
    }
    for key in (
        "errors",
        "metadata_processed",
        "metadata_updated",
        "video_count_changed",
        "discovery_processed",
        "new_videos",
        "new_video_stats_updated",
        "regular_stats_requested",
        "regular_stats_updated",
        "stats_updated",
        "unavailable_marked",
        "missing",
    ):
        result[key] = int(number(row.get(key), 0))
    result["phase_timings"] = safe_phase_timings(summary)
    result["channel_discovery"] = safe_discovery_summary(summary)
    result["regular_video_stats"] = safe_regular_stats(summary)
    result["video_collection"] = safe_video_collection(summary)
    result["explanation"] = sync_run_explanation(row)
    return {**result, **safe_collection_outcome(summary)}


def aggregate_recent_syncs(rows):
    return {
        "count": len(rows),
        "status_counts": sorted_counts(row.get("status") for row in rows),
        "error_total": sum(int(number(row.get("errors"), 0)) for row in rows),
        "duration_seconds": duration_stats(row.get("duration_seconds") for row in rows),
    }


def aggregate_channels(rows, now):
    visible = [row for row in rows if not bool(row.get("is_hidden"))]

    policies = {
        str(row.get("channel_id") or ""): channel_sync_eligibility(row)
        for row in rows
    }

    def schedule_state(prefix):
        if prefix == "channel_discovery":
            eligible = [
                row for row in visible
                if policies[str(row.get("channel_id") or "")].allow_discovery
            ]
        else:
            eligible = [
                row for row in visible
                if policies[str(row.get("channel_id") or "")].allow_channel_metadata
            ]
        status_key = f"{prefix}_status"
        next_key = f"{prefix}_next_after"
        started_key = f"{prefix}_started_at"
        due_rows = []
        null_schedule = 0
        running_rows = []
        for row in eligible:
            status = row.get(status_key)
            if status == "running":
                running_rows.append(row)
            next_after = parse_datetime(row.get(next_key))
            if next_after is None:
                null_schedule += 1
            if status != "running" and (next_after is None or next_after <= now):
                due_rows.append(row)
        overdue_minutes = [
            age_minutes(row.get(next_key), now)
            for row in due_rows
            if row.get(next_key)
        ]
        running_ages = [
            age_minutes(row.get(started_key), now)
            for row in running_rows
            if row.get(started_key)
        ]
        return {
            "due": len(due_rows),
            "failed": sum(row.get(status_key) == "error" for row in eligible),
            "null_schedule": null_schedule,
            "running": len(running_rows),
            "oldest_overdue_minutes": max(overdue_minutes) if overdue_minutes else None,
            "oldest_running_minutes": max(running_ages) if running_ages else None,
            "eligible_channels": len(eligible),
            "policy_paused_channels": len(visible) - len(eligible),
            "status_counts": sorted_counts(row.get(status_key) for row in eligible),
        }

    resume_rows = [
        row for row in visible if row.get("channel_discovery_resume_page_token")
    ]
    resume_ages = [
        age_minutes(row.get("channel_discovery_resume_started_at"), now)
        for row in resume_rows
        if row.get("channel_discovery_resume_started_at")
    ]
    discovery = {
        **schedule_state("channel_discovery"),
        "active_resume_states": len(resume_rows),
        "oldest_resume_age_minutes": max(resume_ages) if resume_ages else None,
        "streak_buckets": sorted_counts(
            streak_bucket(row.get("channel_discovery_no_new_streak"))
            for row in visible
        ),
    }
    return {
        "channels_total": len(rows),
        "visible_channels": len(visible),
        "channel_discovery": discovery,
        "channel_metadata": schedule_state("channel_metadata"),
        "sync_control": {
            "mode_counts": sorted_counts(policy.mode for policy in policies.values()),
            "reason_counts": sorted_counts(policy.reason for policy in policies.values()),
        },
    }


def aggregate_channel_discovery_queue(queue_health, now):
    base = dict(queue_health or {})
    oldest_overdue_at = parse_datetime(base.get("oldest_overdue_at"))
    oldest_lease_at = parse_datetime(base.get("oldest_lease_expires_at"))
    return {
        "mode": "leased_channel_discovery_queue",
        "due": int(number(base.get("due"), 0)),
        "null_schedule": 0,
        "running": int(number(base.get("leased"), 0)),
        "ready": int(number(base.get("ready"), 0)),
        "paused": int(number(base.get("paused"), 0)),
        "failed": int(number(base.get("failed"), 0)),
        "playlist_not_found": int(number(base.get("playlist_not_found"), 0)),
        "expired_leases": int(number(base.get("expired_leases"), 0)),
        "active_resume_states": int(
            number(base.get("active_resume_states"), 0)
        ),
        "oldest_overdue_minutes": age_minutes(oldest_overdue_at, now),
        "oldest_running_minutes": age_minutes(oldest_lease_at, now),
        "oldest_resume_age_minutes": None,
        "reconciliation_required": bool(
            base.get("reconciliation_required", True)
        ),
        "streak_buckets": {
            "0_hot": int(number(base.get("no_new_streak_0"), 0)),
            "1": int(number(base.get("no_new_streak_1"), 0)),
            "2": int(number(base.get("no_new_streak_2"), 0)),
            "3_5": int(number(base.get("no_new_streak_3_5"), 0)),
            "6_plus": int(number(base.get("no_new_streak_6_plus"), 0)),
        },
    }


def aggregate_video_collection_queue(queue_health_rows, now):
    base = dict(queue_health_rows[0]) if queue_health_rows else {}
    oldest_overdue_at = parse_datetime(base.get("oldest_overdue_at"))
    due = int(number(base.get("due"), 0))
    return {
        "mode": "leased_video_collection_queue",
        "all_due": due,
        "total": int(number(base.get("total"), 0)),
        "leased": int(number(base.get("leased"), 0)),
        "expired_leases": int(number(base.get("expired_leases"), 0)),
        "paused": int(number(base.get("paused"), 0)),
        "policy_paused": int(number(base.get("policy_paused"), 0)),
        "hydration_due": int(number(base.get("hydration_due"), 0)),
        "metadata_due": int(number(base.get("metadata_due"), 0)),
        "counter_due": int(number(base.get("counter_due"), 0)),
        "failed": int(number(base.get("failed"), 0)),
        "oldest_overdue_at": (
            oldest_overdue_at.isoformat() if oldest_overdue_at else None
        ),
        "oldest_overdue_minutes": age_minutes(oldest_overdue_at, now),
        "due_rows_scanned": 0,
        "scan_truncated": False,
    }


def present_all_type_queue(row, now, *, discovery=False):
    """Presentation only; the DB reader validates totals and all type buckets."""
    if row is None:
        return {"mode": "unavailable", "scope": "all_types", "all_due": None, "due": None}
    result = dict(row)
    result["mode"] = "leased_channel_discovery_queue" if discovery else "leased_video_collection_queue"
    result["all_due"] = row["due"]
    result["running"] = row["leased"]
    result["oldest_overdue_minutes"] = age_minutes(row.get("oldest_overdue_at"), now)
    return result

def add_quota_estimates(ledger, general_limit, batch_limit):
    limits = {
        "general": max(int(general_limit), 0),
        "video_batch_get_stats": max(int(batch_limit), 0),
    }
    used = ledger.get("bucket_units") or {}
    read_status = ledger.get("read_status", "ok")
    return {
        **ledger,
        "read_status": read_status,
        "configured_bucket_limits": limits,
        "estimated_bucket_remaining": {
            bucket: max(limit - int(number(used.get(bucket), 0)), 0)
            if read_status == "ok" else None
            for bucket, limit in limits.items()
        },
        "remaining_is_ledger_estimate": read_status == "ok",
        "remaining_is_google_console_authoritative": False,
    }


def component_error(errors, component, error):
    errors.append({"component": component, "error_type": error.__class__.__name__})


def read_collection_capacity():
    """Current nonsecret configuration; health never falls back to a shipped limit."""
    from self_hosted_settings import read_configured_limits
    try:
        limit = read_configured_limits(os.environ)['stats_limit']
    except Exception:
        return {"status": "unavailable", "stats_limit": None}
    return {"status": "ok", "stats_limit": limit}


def video_queue_capacity_warning(stats_queue, capacity):
    """Compare a declared queue scope with the shared configured allowance."""
    if stats_queue.get("mode") == "disabled":
        return None
    capacity = capacity or {}
    limit = capacity.get("stats_limit")
    if capacity.get("status") != "ok" or type(limit) is not int or not 1 <= limit <= 100000:
        return {"code": "collection_limit_unavailable", "message":
                "The configured video limit could not be read; queue capacity is unknown."}
    due = int(stats_queue.get("all_due") or 0)
    if due > limit:
        return {"code": "stats_queue_backlog", "message":
                f"{due} {'videos' if stats_queue.get('scope') == 'all_types' else 'Long videos'} are due, exceeding the configured {limit}-video "
                "allowance shared by all types in one pass.", "count": due}
    return None


def classify_health(
    latest_sync,
    last_successful_sync,
    recent_syncs,
    channels,
    stats_queue,
    ledger,
    collection_errors,
    batch_stats_enabled,
    max_sync_age_minutes,
    stale_sync_minutes,
    collection_capacity=None,
):
    critical = []
    warnings = []
    if collection_errors:
        critical.append("one or more health-report components could not be collected")
    if not latest_sync:
        critical.append("no sync run is available")
    else:
        status = latest_sync.get("status")
        age = latest_sync.get("age_minutes")
        if status == "error" or int(latest_sync.get("errors") or 0) > 0:
            critical.append("latest sync run failed")
        elif status == "running" and age is not None and age >= stale_sync_minutes:
            critical.append("latest sync run is stale in running state")
    if not last_successful_sync:
        critical.append("no successful sync run is available")
    elif (
        last_successful_sync.get("age_minutes") is not None
        and last_successful_sync["age_minutes"] >= max_sync_age_minutes
    ):
        warnings.append("latest successful sync is older than expected")
    # A failed request remains historical evidence. Current stored queue failures
    # remain actionable even when a later unrelated sync completed successfully.
    for label, queue in (
        ("Video collection", stats_queue),
        ("Playlist discovery", channels.get("channel_discovery") or {}),
        ("Channel metadata", channels.get("channel_metadata") or {}),
    ):
        failed = int(queue.get("failed") or 0)
        if failed:
            warnings.append(f"{label}: {failed} current queue entries still have a recorded failure")
    if ledger.get("truncated"):
        warnings.append("YouTube request ledger read reached its safety bound")
    buffer = ledger.get("buffer") or {}
    if buffer.get("status") == "error":
        warnings.append("Local quota journal needs attention; the ledger may be incomplete")
    elif buffer.get("pending_events"):
        warnings.append("Quota events are waiting to reach the database; the ledger may lag")
    unfinished = ledger.get("unfinished") or {}
    if int(unfinished.get("stale_count") or 0) > 0:
        warnings.append("YouTube request ledger contains stale unfinished attempts")
    discovery = channels.get("channel_discovery") or {}
    if number(discovery.get("oldest_resume_age_minutes"), 0) >= 24 * 60:
        warnings.append("a channel discovery resume state is older than 24 hours")
    capacity_warning = video_queue_capacity_warning(stats_queue, collection_capacity)
    if capacity_warning:
        warnings.append(capacity_warning["message"])
    latest = latest_sync or {}
    regular = latest.get("video_collection") or latest.get("regular_video_stats") or {}
    if (
        latest.get("status") == "partial"
        or latest.get("incomplete") is True
        or regular.get("incomplete") is True
        or int(regular.get("metadata_deferred") or 0) > 0
    ):
        warnings.append("latest recorded sync left admitted work unresolved; see its recorded reasons")
    if int(regular.get("source_view_count_absent") or 0) > 0:
        warnings.append(
            "YouTube omitted view counts for one or more returned videos"
        )
    if (
        batch_stats_enabled
        and int(regular.get("batch_recurring_requested") or 0) > 0
        and int(regular.get("batch_requests") or 0) == 0
    ):
        warnings.append(
            "batch routing was enabled but recurring batch work made no batch request"
        )
    status = "critical" if critical else "warning" if warnings else "ok"
    return status, critical, warnings


def collect_pipeline_health(
    client,
    now=None,
    window_hours=DEFAULT_WINDOW_HOURS,
    max_sync_age_minutes=DEFAULT_MAX_SYNC_AGE_MINUTES,
    stale_sync_minutes=DEFAULT_STALE_SYNC_MINUTES,
    stale_attempt_minutes=DEFAULT_STALE_ATTEMPT_MINUTES,
    queue_scan_limit=DEFAULT_QUEUE_SCAN_LIMIT,  # Compatibility only; no due-row scan.
    page_size=DEFAULT_PAGE_SIZE,
    batch_stats_enabled=False,
    database_video_collection_enabled=False,
    database_channel_discovery_enabled=False,
    general_quota_limit=DEFAULT_GENERAL_QUOTA_LIMIT,
    batch_quota_limit=DEFAULT_BATCH_QUOTA_LIMIT,
    quota_buffer_status_reader=None,
):
    now = parse_datetime(now) or utc_now()
    since = now - timedelta(hours=max(int(window_hours), 1))
    clock = quota_clock(now)
    collection_errors = []
    collection_capacity = read_collection_capacity()

    latest_rows = []
    latest_success_rows = []
    recent_rows = []
    try:
        sync_runs = SyncRunRepository(client)
        latest = sync_runs.latest()
        latest_rows = [latest] if latest else []
        recent_rows = sync_runs.list_since(since.isoformat(), limit=10_000)
        latest_success = sync_runs.latest_completed()
        latest_success_rows = [latest_success] if latest_success else []
    except Exception as error:
        component_error(collection_errors, "sync_runs", error)

    channel_rows = []
    try:
        channel_rows, _ = fetch_paginated(
            client,
            "channels",
            CHANNEL_COLUMNS,
            operations=(("order", ("channel_id",), {"desc": False}),),
            page_size=page_size,
            max_rows=100_000,
        )
    except Exception as error:
        component_error(collection_errors, "channels", error)

    discovery_queue_health = None
    if database_channel_discovery_enabled:
        try:
            discovery_queue_health = get_all_discovery_queue_health(client)
        except Exception as error:
            component_error(
                collection_errors,
                "channel_discovery_queue",
                error,
            )

    if not database_channel_discovery_enabled:
        collection_errors.append({
            "component": "channel_discovery_queue",
            "error_type": "DatabaseChannelDiscoveryDisabled",
        })

    queue_health_rows = None
    if database_video_collection_enabled:
        try:
            queue_health_rows = get_all_video_queue_health(client)
        except Exception as error:
            component_error(collection_errors, "stats_queue", error)
    else:
        collection_errors.append({
            "component": "stats_queue",
            "error_type": "DatabaseVideoCollectionDisabled",
        })

    channel_sync_control_health = {}
    try:
        channel_sync_control_health = ChannelSyncRepository(client).health()
    except Exception as error:
        component_error(collection_errors, "channel_sync_control", error)
        channel_sync_control_health = {"ready": False, "error_type": error.__class__.__name__}

    if quota_buffer_status_reader is None:
        from youtube_quota_buffer import read_quota_buffer_status

        quota_buffer_status_reader = read_quota_buffer_status
    # Snapshot pending evidence before the ledger read; never add its units to
    # the database sum because committed-but-unacknowledged events can overlap.
    quota_buffer = quota_buffer_status_reader()
    ledger = {"attempts": 0, "quota_units": 0,
        **{key: {} for key in ("bucket_attempt_counts", "bucket_units", "phase_attempt_counts",
                              "phase_units", "endpoint_attempt_counts", "status_counts",
                              "http_status_counts", "error_type_counts")},
        "duration_ms": {"count": 0, "min": None, "avg": None, "median": None, "max": None},
        "unfinished": {"count": 0, "stale_count": 0, "oldest_age_minutes": None,
                       "stale_after_minutes": stale_attempt_minutes}}
    ledger_truncated = False
    ledger_read_status = "ok"
    try:
        ledger = YouTubeQuotaRepository(
            client,
            project_id=(
                os.getenv("YOUTUBE_QUOTA_PROJECT_ID", DEFAULT_QUOTA_PROJECT_ID).strip()
                or DEFAULT_QUOTA_PROJECT_ID
            ),
        ).summarize_day(
            clock["quota_day"], now=now,
            stale_attempt_minutes=stale_attempt_minutes, max_rows=100_000,
        )
        ledger_truncated = ledger.pop("truncated")
        if ledger_truncated:
            ledger_read_status = "partial"
    except Exception as error:
        ledger_read_status = "unavailable"
        component_error(collection_errors, "youtube_api_request_attempts", error)
    if (
        ledger_read_status == "ok"
        and (quota_buffer["status"] != "ok" or quota_buffer["pending_events"])
    ):
        ledger_read_status = "partial"

    latest_sync = safe_latest_sync(latest_rows[0] if latest_rows else None, now)
    last_successful_sync = safe_latest_sync(
        latest_success_rows[0] if latest_success_rows else None,
        now,
    )
    recent_syncs = aggregate_recent_syncs(recent_rows)
    channels = aggregate_channels(channel_rows, now)
    if any(item["component"] == "channels" for item in collection_errors):
        channels.update(channels_total=None, visible_channels=None,
            channel_metadata={"mode": "unavailable", "due": None, "running": None, "failed": None})
    if database_channel_discovery_enabled:
        channels["channel_discovery"] = present_all_type_queue(
            discovery_queue_health,
            now, discovery=True,
        )
    else:
        channels["channel_discovery"] = {"mode": "disabled"}
    if database_video_collection_enabled:
        stats_queue = present_all_type_queue(queue_health_rows, now)
    else:
        stats_queue = {
            "mode": "disabled",
            "all_due": None,
            "due_rows_scanned": 0,
            "scan_truncated": False,
        }
    ledger["read_status"] = ledger_read_status
    ledger["truncated"] = ledger_truncated
    ledger["buffer"] = quota_buffer
    if ledger_read_status == "unavailable":
        ledger["attempts"] = None
        ledger["quota_units"] = None
    ledger = add_quota_estimates(ledger, general_quota_limit, batch_quota_limit)
    status, critical, warnings = classify_health(
        latest_sync,
        last_successful_sync,
        recent_syncs,
        channels,
        stats_queue,
        ledger,
        collection_errors,
        bool(batch_stats_enabled),
        max_sync_age_minutes,
        stale_sync_minutes,
        collection_capacity=collection_capacity,
    )
    if int(channel_sync_control_health.get("collection_rows_policy_drift") or 0):
        critical.append("Channel sync control has queue eligibility drift")
        status = "critical"
    return {
        "status": status,
        "generated_at": now.isoformat(),
        "window_hours": max(int(window_hours), 1),
        "runtime": {
            "stats_limit": collection_capacity["stats_limit"],
            "stats_limit_read_status": collection_capacity["status"],
            "stats_queue_scope": "all_types",
            "stats_limit_scope": "all_types_general_and_stats",
            "batch_stats_enabled": bool(batch_stats_enabled),
            "database_video_collection_enabled": bool(
                database_video_collection_enabled
            ),
            "database_channel_discovery_enabled": bool(
                database_channel_discovery_enabled
            ),
        },
        "latest_sync": latest_sync,
        "last_successful_sync": last_successful_sync,
        "recent_syncs": recent_syncs,
        "history": {
            "scope": "recorded_windows",
            "sync_errors": recent_syncs["error_total"] if not any(
                item["component"] == "sync_runs" for item in collection_errors) else None,
            "transport_errors": int((ledger.get("status_counts") or {}).get("transport_error") or 0)
            if ledger_read_status != "unavailable" else None,
            "transport_read_status": ledger_read_status,
            "quota_day": clock["quota_day"],
            "recovery": "not_correlated",
        },
        "queues": {
            "stats": stats_queue,
            "channel_discovery": channels.get("channel_discovery") or {},
            "channel_metadata": channels.get("channel_metadata") or {},
        },
        "channels": {
            "total": channels.get("channels_total", 0),
            "visible": channels.get("visible_channels", 0),
            "sync_control": {
                **(channels.get("sync_control") or {}),
                "database": channel_sync_control_health,
            },
        },
        "youtube_quota": {**clock, **ledger},
        "critical": critical,
        "warnings": warnings,
        "collection_errors": collection_errors,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print an aggregate-only, read-only YouTube pipeline health report."
    )
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument(
        "--max-sync-age-minutes",
        type=int,
        default=DEFAULT_MAX_SYNC_AGE_MINUTES,
    )
    parser.add_argument(
        "--stale-sync-minutes",
        type=int,
        default=DEFAULT_STALE_SYNC_MINUTES,
    )
    parser.add_argument(
        "--stale-attempt-minutes",
        type=int,
        default=DEFAULT_STALE_ATTEMPT_MINUTES,
    )
    parser.add_argument(
        "--queue-scan-limit",
        type=int,
        default=DEFAULT_QUEUE_SCAN_LIMIT,
    )
    parser.add_argument(
        "--general-quota-limit",
        type=int,
        default=DEFAULT_GENERAL_QUOTA_LIMIT,
        help="Observed Google Console daily general bucket limit; reporting only.",
    )
    parser.add_argument(
        "--batch-quota-limit",
        type=int,
        default=DEFAULT_BATCH_QUOTA_LIMIT,
        help="Observed Google Console daily batchGetStats bucket limit; reporting only.",
    )
    parser.add_argument("--compact", action="store_true")
    parser.add_argument(
        "--fail-on-critical",
        action="store_true",
        help="Return exit code 2 when the aggregate report status is critical.",
    )
    return parser


def env_flag(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main(argv=None):
    args = build_parser().parse_args(argv)
    from core import database, process_database_resources

    with process_database_resources(record_quota=False):
        report = collect_pipeline_health(
            database,
            window_hours=args.window_hours,
            max_sync_age_minutes=args.max_sync_age_minutes,
            stale_sync_minutes=args.stale_sync_minutes,
            stale_attempt_minutes=args.stale_attempt_minutes,
            queue_scan_limit=args.queue_scan_limit,
            batch_stats_enabled=env_flag("YOUTUBE_BATCH_STATS_ENABLED"),
            database_video_collection_enabled=env_flag(
                "DATABASE_VIDEO_COLLECTION_ENABLED"
            ),
            database_channel_discovery_enabled=env_flag(
                "DATABASE_CHANNEL_DISCOVERY_ENABLED"
            ),
            general_quota_limit=args.general_quota_limit,
            batch_quota_limit=args.batch_quota_limit,
        )
    if args.compact:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.fail_on_critical and report["status"] == "critical":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
