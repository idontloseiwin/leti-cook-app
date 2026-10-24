#!/usr/bin/env python3
"""Selected real-repository smoke cases on an empty disposable PostgreSQL 17 DB.

Uses the two LIC_DIRECT_PG_PROBE_* DSNs and the baseline's exact loopback,
disposable-name, role, advisory-lock, empty-table and fixed cleanup safeguards.
No database is bootstrapped, no provider is called, and no case runs by default.
The runtime is the real bounded PostgresDatabase with authenticator credentials;
its admission callback is synthetic, so this is not production admission proof.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
# Older than the 29-day source window (hot_storage.HOT_DATA_WINDOW).
BEYOND_RETENTION = timedelta(days=30)
# Importing the harness itself does not import the application or open a pool.
sys.path.insert(0, str(ROOT / "tools"))
import check_direct_pg_baseline as safety

CHANNEL = "UC0000000000000000000001"
OTHER_CHANNEL = "UC0000000000000000000002"
FOLDER = "f0000000-0000-4000-8000-000000000001"
VIEW = "e0000000-0000-4000-8000-000000000001"
COLLECTION = "c0000000-0000-4000-8000-000000000001"
REQUEST = "d0000000-0000-4000-8000-000000000001"
LABEL = "custom:a0000000-0000-4000-8000-000000000001"
TITLE = "Probe O'Brien; SELECT 'ÐšÐ¸Ñ—Ð²'"
VIDEOS = ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd", "eeeeeeeeeee")
BIGINT = 9007199254740993
CASES = {
    "user_work": "Folders and labels; Saved view JSON and collection receipts; export conversion, trash restore and pending import.",
    "quota_sync": "Bound JSON quota journal replay, nullable terminal fields, paginated ledger, sync INSERT RETURNING and summary projection.",
    "reads": "Typed current versus canonical counts, NULL ordering, bound ID filters, bigint/array values and source clocks.",
    "timeline": "Real bounded timeline returns the extra 501st pagination row without transport truncation.",
    "stale_sync": "Direct stale-run recovery on stale/fresh/completed runs; boundary, replay, summary reports, NULL finish clocks and atomic report conflict.",
    "transaction_steps": "Real savepoint rollback, whole-operation deadline and repeatable-read export snapshot under concurrent change.",
}
LIMITATIONS = [
    "Selected repository smoke only; no full coverage claim.",
    "Admission is an explicit no-op callback; production admission and lifecycle acceptance are separate.",
    "No provider requests, concurrency, physical network failures, 1000-row boundary or performance percentiles are tested.",
    "At most two database connections: one admin and a runtime pool capped at one connection.",
    "Repository method calls are counted, not internal SQL executions or network round trips.",
    "Admin execute calls include setup, observations and cleanup, excluding transaction-manager BEGIN/COMMIT.",
]


class ProbeFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure("repository_probe_" + code)


FAILURE_STAGES = frozenset({"case_setup", "saved_conditions", "user_work_mutations", "backup_snapshot", "backup_build", "backup_payload",
                            "backup_projection", "backup_reset", "backup_import", "backup_pending",
                            "backup_collection"})
# Only these owned public codes can leave a process. Never serialize an arbitrary
# exception name, message, diagnostic field, object key, or application payload.
PUBLIC_FAILURE_CODES = frozenset({
    "invalid_user_work_backup", "invalid_user_work_snapshot", "invalid_request",
    "user_work_limit_exceeded", "unsupported_user_work_backup", "duplicate_user_work_identity",
    "missing_user_work_reference", "preset_label_in_backup", "cyclic_channel_relationship",
    "review_has_no_user_decision", "inconsistent_user_decision", "duplicate_json_key",
    "database_operation_failed", "database_result_limit_exceeded", "database_result_shape_invalid",
    "database_result_missing", "database_unexpected_result_set", "database_function_parameters_invalid",
    "database_connection_failed", "database_pool_unavailable", "database_pool_closed",
    "database_pool_not_open", "database_transaction_aborted", "database_runtime_role_invalid",
    "database_role_check_failed", "user_work_limit_reached",
})


def safe_failure(error, *, stage=None):
    # Type names and arbitrary exception messages are not published.
    result = {"failure_type": "probe" if isinstance(error, ProbeFailure) else
              "preflight" if isinstance(error, safety.BaselineFailure) else "operation"}
    if isinstance(error, ProbeFailure) and len(error.args) == 1:
        token = error.args[0]
        if isinstance(token, str) and re.fullmatch(r"repository_probe_[a-z0-9_]{1,90}", token):
            result["failure_code"] = token
    if isinstance(error, safety.BaselineFailure):
        result.update({k: v for k, v in safety.safe_failure(error).items() if k != "failure_type"})
    # The real driver retains SQLSTATE as code after scrubbing server messages.
    for value in (getattr(error, "sqlstate", None), getattr(error, "code", None)):
        if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{5}", value):
            result["sqlstate"] = value
            break
    if stage in FAILURE_STAGES:
        result["failure_stage"] = stage
    for module_name, class_name, category in (
        ("user_work_backup", "UserWorkError", "user_work_validation"),
        ("saved_work", "SavedWorkError", "saved_work_validation"),
        ("db.base.postgres", "DatabaseServerError", "database_server"),
        ("db.base.postgres", "DatabaseUnavailable", "database_unavailable"),
        ("db.base.postgres", "DatabaseError", "database"),
    ):
        error_class = getattr(sys.modules.get(module_name), class_name, None)
        if isinstance(error_class, type) and isinstance(error, error_class):
            result["failure_category"] = category
            if type(getattr(error, "outcome_unknown", None)) is bool:
                result["outcome_unknown"] = error.outcome_unknown
            break
    if "failure_category" not in result:
        for error_class, category in ((KeyError, "key_error"), (TypeError, "type_error"),
                                      (ValueError, "value_error")):
            if isinstance(error, error_class):
                result["failure_category"] = category
                break
    for token in (getattr(error, "code", None), getattr(error, "message", None)):
        if isinstance(token, str) and token in PUBLIC_FAILURE_CODES:
            result["failure_code"] = token
            break
    return result


def saved_conditions():
    """Seed the same canonical condition object the real service accepts."""
    from saved_work import FILTER_KEYS, normalize_conditions
    filters = dict.fromkeys(FILTER_KEYS, "")
    filters.update(sort_by="published_at", sort_order="desc")
    return normalize_conditions({"version": 1, "filters": filters, "facets": {"version": 1}})


@contextmanager
def self_hosted_profile():
    previous = os.environ.get("LIC_DEPLOYMENT_PROFILE")
    os.environ["LIC_DEPLOYMENT_PROFILE"] = "self-hosted"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LIC_DEPLOYMENT_PROFILE", None)
        else:
            os.environ["LIC_DEPLOYMENT_PROFILE"] = previous


def open_database(dsn):
    sys.path.insert(0, str(ROOT / "application"))
    from db.base.postgres import PostgresConfig, PostgresDatabase
    return PostgresDatabase(PostgresConfig(dsn, min_size=0, max_size=1, max_waiting=1,
                                          acquire_timeout=3, connect_timeout=5), admission=lambda name: None)


class Probe:
    def __init__(self, admin, database, snapshots, before_counts):
        self.admin, self.database = admin, database
        self.snapshots, self.before_counts = snapshots, before_counts
        self.checks = []
        self.method_calls = 0
        self.method_seconds = 0.0
        self.peak_rows = sum(before_counts.values())
        self.resets = 0
        self.stage = "case_setup"
        self.now = admin.scalar("SELECT clock_timestamp()")

    def call(self, method, *args, **kwargs):
        self.method_calls += 1
        started = time.monotonic()
        try:
            return method(*args, **kwargs)
        finally:
            self.method_seconds += time.monotonic() - started

    def check(self, name, condition):
        require(re.fullmatch(r"[a-z][a-z0-9_]{1,90}", name), "invalid_check_name")
        require(name not in self.checks, "duplicate_check_name")
        require(condition, name)
        self.checks.append(name)

    def observe(self):
        counts = safety.bounded_counts(self.admin)
        total = sum(counts.values())
        require(total <= safety.MAX_ROWS, "synthetic_row_budget")
        self.peak_rows = max(self.peak_rows, total)
        return counts

    def reset(self):
        # Once preflight authorizes this fixture, cleanup must run even when a
        # preceding row-budget assertion or result observation has failed.
        safety.reset_fixture(self.admin, self.snapshots, self.before_counts)
        self.resets += 1

    def report(self):
        return {"checks": [{"check_id": name, "status": "passed"} for name in self.checks],
                "passed_checks": len(self.checks), "repository_method_calls": self.method_calls,
                "repository_method_seconds": round(self.method_seconds, 6),
                "peak_observed_total_rows": self.peak_rows, "completed_resets": self.resets}


def seed_channel(probe, channel_id=CHANNEL):
    # The current Python channel owner creates the channel, as in check_initial_video_batch.
    from db.channel.channel_control import create_channel_for_collection
    create_channel_for_collection(probe.database, {
        "channel_id": channel_id, "title": TITLE, "initial_long_inventory_status": "completed",
        "initial_28d_stats_status": "completed"}, "accepted")
    with probe.admin.transaction():
        probe.admin.execute("UPDATE public.channels SET user_input=%s,channel_metadata_status='ok',"
                            "channel_metadata_checked_at=%s WHERE channel_id=%s",
                            ("https://www.youtube.com/channel/" + channel_id,
                             probe.now - timedelta(minutes=1), channel_id))
    probe.observe()


def user_work(probe):
    from db.user_work.channel_organization import ChannelOrganizationRepository
    from db.user_work.channel_labels import ChannelLabelRepository
    from db.user_work.saved_views import SavedViewsRepository
    from db.user_work.saved_collections import SavedCollectionsRepository
    from db.channel.channel_trash import ChannelTrashRepository
    from db.user_work.user_work_backup import UserWorkBackupRepository
    from db.reads.frontend import FrontendRepository
    from user_work_backup import build_backup, import_payload
    db, call = probe.database, probe.call
    folders, labels = ChannelOrganizationRepository(db), ChannelLabelRepository(db)
    views, collections = SavedViewsRepository(db), SavedCollectionsRepository(db)
    trash, backup = ChannelTrashRepository(db), UserWorkBackupRepository(db)
    front = FrontendRepository(db, read_clock=lambda: probe.now)
    seed_channel(probe)
    call(folders.create_folder, {"id": FOLDER, "name": TITLE})
    call(folders.assign_folder, CHANNEL, FOLDER)
    probe.check("folder_assignment", call(front.load_channel, CHANNEL)[0]["folder_id"] == FOLDER)
    call(folders.assign_folder, CHANNEL, None)
    probe.check("folder_explicit_null", call(front.load_channel, CHANNEL)[0]["folder_id"] is None)
    call(folders.assign_folder, CHANNEL, FOLDER)
    label = call(labels.create_label, {"label_key": LABEL, "name": TITLE, "label_type": "custom"})
    probe.check("label_returning_defaults", label["label_key"] == LABEL and label["name"] == TITLE and isinstance(label["created_at"], str))
    call(labels.assign_label, CHANNEL, LABEL)
    call(labels.assign_label, CHANNEL, LABEL)
    probe.check("label_assignment_idempotent", call(labels.load_assignments) == [{"channel_id": CHANNEL, "label_key": LABEL}])
    probe.stage = "saved_conditions"
    conditions = saved_conditions()
    probe.stage = "user_work_mutations"
    view = call(views.create_object, VIEW, TITLE, "0" * 64, channel_id=CHANNEL, conditions=conditions)
    probe.check("saved_view_json", view.get("outcome") == "ready" and view["object"]["conditions"] == conditions)
    probe.check("saved_view_kind_and_uuid", call(views.get_object, VIEW)["id"] == VIEW and call(collections.get_object, VIEW) is None)
    created = call(collections.create_object, COLLECTION, TITLE, "1" * 64)
    probe.check("collection_explicit_nulls", created.get("outcome") == "ready" and created["object"]["channel_id"] is None and created["object"]["conditions"] is None)
    change = call(collections.change_members, COLLECTION, REQUEST, "add", list(VIDEOS[:2]), 1, "2" * 64)
    probe.check("collection_command_receipt", change.get("outcome") == "ready" and change["mutation"]["changed_count"] == 2 and change["collection"]["revision"] == 2)
    replay = call(collections.change_members, COLLECTION, REQUEST, "add", list(VIDEOS[:2]), 1, "2" * 64)
    probe.check("collection_receipt_replay", replay["mutation"]["replayed"] is True and replay["collection"]["member_count"] == 2)
    conflict = call(collections.change_members, COLLECTION, REQUEST, "add", list(VIDEOS[:2]), 1, "3" * 64)
    probe.check("collection_receipt_conflict", conflict.get("outcome") == "conflict")
    members = call(collections.list_members, COLLECTION, 1)
    second = call(collections.list_members, COLLECTION, 1, after=(members[0]["added_at"], members[0]["video_id"]))
    probe.check("collection_member_cursor", len(members) == len(second) == 1 and {members[0]["video_id"], second[0]["video_id"]} == set(VIDEOS[:2]))
    removed = call(trash.remove, CHANNEL)
    probe.check("channel_trash", removed.get("outcome") == "removed" and len(call(trash.list_removed)) == 1 and call(views.load_available_channels, [CHANNEL]) == set())
    restored = call(trash.restore, removed["id"], removed["removed_at"])
    probe.check("channel_trash_restore", restored.get("outcome") == "restored" and restored["removed_at"] is None and call(trash.list_removed) == [] and call(views.load_available_channels, [CHANNEL]) == {CHANNEL})
    probe.stage = "backup_snapshot"
    snapshot = call(backup.snapshot)
    probe.check("backup_snapshot_shape", set(snapshot) == {"folders", "labels", "channels", "assignments", "saved_objects", "members", "pending", "reviews"})
    probe.stage = "backup_build"
    exported = build_backup(snapshot, now=(probe.now + timedelta(seconds=1)).isoformat())
    probe.stage = "backup_payload"
    document = import_payload(exported["backup"], normalized=True)
    probe.stage = "backup_projection"
    identity = document["channels"][0]["id"]
    probe.check("backup_service_projection", document["schema_version"] == 2 and len(document["channels"]) == len(document["collections"]) == 1 and len(document["collections"][0]["members"]) == 2)
    probe.observe()
    probe.stage = "backup_reset"
    probe.reset()  # Import is only valid into a fresh user-work installation.
    probe.stage = "backup_import"
    imported = call(backup.import_document, document)
    probe.check("backup_import", imported.get("outcome") == "ready" and imported["pending_channels"] == 1 and imported["saved_objects"] == 2 and imported["members"] == 2)
    probe.stage = "backup_pending"
    pending = call(backup.pending_item, identity)
    probe.check("backup_pending_arrays_json", pending["id"] == identity and pending["folder_id"] == FOLDER and pending["label_keys"] == [LABEL] and pending["saved_views"][0]["conditions"] == conditions)
    probe.stage = "backup_collection"
    probe.check("backup_restored_collection", call(collections.get_object, COLLECTION)["revision"] == 2 and len(call(collections.list_members, COLLECTION, 3)) == 2)


def quota_sync(probe):
    from db.quota.youtube_quota import YouTubeQuotaRepository
    from db.runs.sync_runs import SyncRunRepository
    call, db = probe.call, probe.database
    quota, runs = YouTubeQuotaRepository(db), SyncRunRepository(db)
    # Ask the server for its Pacific day so the probe needs no local tzdata.
    day = probe.admin.scalar("SELECT (%s::timestamptz AT TIME ZONE 'America/Los_Angeles')::date", (probe.now,)).isoformat()
    stamp = probe.now.isoformat()
    start = {"request_attempt_id": REQUEST, "project_id": "primary", "quota_bucket": "general", "quota_day": day,
             "phase": "video_stats", "endpoint": "videos", "quota_units": 1, "status": "started", "started_at": stamp}
    finish = {"status": "success", "finished_at": stamp, "duration_ms": 7, "http_status": 200, "error_type": None}
    events = [{"kind": "start", "request_attempt_id": REQUEST, "project_id": "primary", "values": start},
              {"kind": "finish", "request_attempt_id": REQUEST, "project_id": "primary", "values": finish}]
    probe.check("quota_bound_json_batch", call(quota.persist_events, events) is True)
    probe.check("quota_bound_json_replay", call(quota.persist_events, events) is True)
    second_id = "d0000000-0000-4000-8000-000000000002"
    call(quota.start_attempt, {**start, "request_attempt_id": second_id, "quota_units": 2, "retry_of_request_attempt_id": REQUEST})
    call(quota.finish_attempt, second_id, {**finish, "duration_ms": 9})
    rows, truncated = call(quota.list_attempts_for_day, day, page_size=1, max_rows=3)
    probe.check("quota_uuid_date_null_paging", len(rows) == 2 and not truncated and rows[0]["request_attempt_id"] == REQUEST and rows[0]["quota_day"] == day and rows[0]["error_type"] is None and rows[1]["retry_of_request_attempt_id"] == REQUEST)
    limited, limited_truncated = call(quota.list_attempts_for_day, day, page_size=1, max_rows=1)
    probe.check("quota_read_cap", len(limited) == 1 and limited_truncated)
    summary = call(quota.summarize_day, day, now=probe.now, max_rows=3)
    probe.check("quota_summary_numeric", summary["attempts"] == 2 and summary["quota_units"] == 3 and summary["status_counts"] == {"success": 2} and summary["duration_ms"]["avg"] == 8.0)
    run_id = call(runs.start, {"run_type": "auto_sync", "status": "running", "summary_json": {"routing": {"enabled": False}, "errors": 0}})
    probe.check("sync_insert_returning_id", type(run_id) is int and run_id > 0)
    initial = call(runs.latest)
    probe.check("sync_server_defaults", initial["id"] == run_id and initial["finished_at"] is None and isinstance(initial["started_at"], str) and initial["summary_json"]["routing"]["enabled"] is False)
    call(runs.update_progress, run_id, {"metadata_processed": 1, "summary_json": {"channel_metadata": {"processed": 1}, "routing": {"enabled": False}}})
    call(runs.finish, run_id, {"status": "completed", "started_at": (probe.now - timedelta(days=5)).isoformat(),
         "finished_at": (probe.now + timedelta(seconds=1)).isoformat(), "duration_seconds": 1.25,
         "metadata_updated": 1, "errors": 0, "summary_json": {"channel_metadata": {"processed": 1, "updated": 1}, "routing": {"enabled": False}, "errors": 0}})
    completed = call(runs.latest_completed)
    probe.check("sync_finish_preserves_start", completed["id"] == run_id and completed["started_at"] == initial["started_at"] and completed["duration_seconds"] == 1.25)
    probe.check("sync_summary_projection", completed["summary_json"]["channel_metadata"] == {"processed": 1, "updated": 1} and completed["summary_json"]["routing"]["enabled"] is False)
    probe.check("sync_filtered_reads", call(runs.latest_completed_with_changes)["id"] == run_id and len(call(runs.list_since, (probe.now - timedelta(seconds=1)).isoformat(), limit=2)) == 1 and call(runs.list_since, (probe.now + timedelta(days=1)).isoformat(), limit=2) == [])


def seed_reads(probe):
    seed_channel(probe)
    seed_channel(probe, OTHER_CHANNEL)
    fresh, stale = probe.now - timedelta(minutes=1), probe.now - BEYOND_RETENTION
    published = probe.now - timedelta(days=2)
    with probe.admin.transaction():
        for index, video in enumerate(VIDEOS):
            kind = "short" if index == 4 else "long"
            publication = None if index == 2 else published
            proof = stale if index == 3 else fresh
            probe.admin.execute("INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at) VALUES(%s,%s,%s,%s)", (video, CHANNEL, kind, publication))
            probe.admin.execute("INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_at,last_seen_at,last_present_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                                (video, CHANNEL, kind, ("UUSH" if kind == "short" else "UULF") + CHANNEL[2:], publication, proof, proof, proof))
            probe.admin.execute("INSERT INTO public.videos(video_id,channel_id,title,views,checked_at,published_at,metadata_checked_at,availability_status,availability_checked_at,tags,like_count,caption_available) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                (video, CHANNEL, TITLE, BIGINT, proof, publication, stale if index == 1 else fresh, "public", fresh, ["O'Brien", "ÐšÐ¸Ñ—Ð²"], None, False))
    probe.observe()


def reads(probe):
    from db.reads.channel_explorer import ChannelExplorerRepository
    from db.reads.frontend import FrontendRepository
    from db.user_work.saved_collections import SavedCollectionsRepository
    from db.reads.system_health import SystemHealthRepository, CHANNEL_HEALTH_COLUMNS, CHANNEL_HEALTH_ORDER
    from hot_storage import HotSourceReadExpired
    seed_reads(probe)
    call, db = probe.call, probe.database
    explorer = ChannelExplorerRepository(db)
    front = FrontendRepository(db, read_clock=lambda: probe.now)
    collections = SavedCollectionsRepository(db, read_clock=lambda: probe.now)
    health = SystemHealthRepository(db)
    probe.check("canonical_count_includes_stale", call(explorer.count_canonical_inventory, CHANNEL, video_type="long") == 4)
    probe.check("typed_current_counts", call(front.count_inventory_by_type, [CHANNEL, OTHER_CHANNEL, CHANNEL], ["long", "short", "stream"], channel_batch_size=1) == {"long": 3, "short": 1, "stream": 0})
    current = call(explorer.load_inventory, CHANNEL, video_type="long", limit=10)
    probe.check("inventory_null_order_and_types", [r["video_id"] for r in current] == list(VIDEOS[:3]) and current[-1]["published_at"] is None and all(isinstance(r["source_checked_at"], str) for r in current))
    probe.check("inventory_empty_filters", call(explorer.load_inventory, OTHER_CHANNEL, video_type="long", limit=2) == [] and call(explorer.load_inventory, CHANNEL + "' OR true --", video_type="long", limit=2) == [])
    page, raw_count = call(front.load_channel_inventory_page, CHANNEL, video_type="long", offset=1, page_size=1)
    probe.check("inventory_offset_raw_count", raw_count == len(page) == 1 and page[0]["video_id"] == VIDEOS[1])
    probe.check("canonical_page_includes_stale", len(call(front.load_channel_backfill_page, CHANNEL, video_type="long", offset=0, page_size=10)) == 4)
    probe.check("membership_source_clock", call(front.load_channel_id_for_video, VIDEOS[0]) == CHANNEL and call(front.load_channel_id_for_video, VIDEOS[3]) is None)
    values = call(explorer.load_videos_by_ids, [VIDEOS[0], VIDEOS[0], "x' OR true --"], columns="video_id,title,views,tags,like_count,caption_available")
    probe.check("bound_array_bigint_null_boolean", len(values) == 1 and values[0]["views"] == BIGINT and values[0]["title"] == TITLE and values[0]["tags"] == ["O'Brien", "ÐšÐ¸Ñ—Ð²"] and values[0]["like_count"] is None and values[0]["caption_available"] is False)
    admitted = call(front.load_videos_by_ids, list(VIDEOS), columns="video_id,title,views")
    probe.check("current_video_source_filter", {r["video_id"] for r in admitted} == {VIDEOS[0], VIDEOS[2], VIDEOS[4]})
    sources = call(collections.load_sources, [VIDEOS[1], "zzzzzzzzzzz", VIDEOS[0]], as_of=probe.now)
    probe.check("source_function_json_and_coverage", [r["video_id"] for r in sources] == [VIDEOS[1], "zzzzzzzzzzz", VIDEOS[0]] and [r["source_reason"] for r in sources] == ["source_stale", "inventory_missing", None] and sources[0]["video_row"] is None and sources[2]["video_row"]["views"] == BIGINT)
    resolutions = call(explorer.load_type_resolutions, list(VIDEOS), video_type="long")
    probe.check("type_resolution_arrays", {r["video_id"] for r in resolutions} == set(VIDEOS[:3]) and all(r["present_membership_types"] == ["long"] and r["resolution_status"] == "resolved" for r in resolutions))
    probe.check("expired_type_evidence_is_withheld", call(explorer.load_type_resolutions, [VIDEOS[3]], video_type="long") == [])
    expired = FrontendRepository(db, read_clock=lambda: probe.now + BEYOND_RETENTION)
    expired_page, expired_raw = call(expired.load_channel_inventory_page, CHANNEL, video_type="long", offset=0, page_size=5)
    probe.check("expired_in_transit_preserves_raw_count", expired_page == [] and expired_raw == 3)
    try:
        call(expired.count_inventory_by_type, [CHANNEL], ["long"])
    except HotSourceReadExpired:
        did_expire = True
    else:
        did_expire = False
    probe.check("expired_count_fails_closed", did_expire)
    health_rows, truncated = call(health.fetch_paginated, "channels", CHANNEL_HEALTH_COLUMNS, operations=CHANNEL_HEALTH_ORDER, page_size=1, max_rows=3)
    probe.check("health_finite_paged_scenario", [r["channel_id"] for r in health_rows] == [CHANNEL, OTHER_CHANNEL] and not truncated)


def timeline(probe):
    from db.reads.channel_timeline import ChannelTimelineRepository
    seed_channel(probe)
    with probe.admin.transaction():
        probe.admin.execute("INSERT INTO public.channel_snapshots(channel_id,recorded_at,views,subscribers,source) "
                            "SELECT %s, date_trunc('hour',%s::timestamptz)-g*interval '5 minutes', "
                            "1000000-g, 10000-g, 'youtube_channel_metadata_refresh' FROM generate_series(0,500) AS g", (CHANNEL, probe.now))
    probe.observe()
    repo = ChannelTimelineRepository(probe.database, read_clock=lambda: probe.now)
    rows, truncated, count = probe.call(repo.load_snapshot_window, CHANNEL, metric='views',
        recorded_from=(probe.now-timedelta(days=4)).isoformat(), recorded_through=probe.now.isoformat(),
        bucket_seconds=300, scan_limit=1000, limit=500)
    probe.check('timeline_retains_501st_sentinel', len(rows) == 501)
    probe.check('timeline_scan_count', count == 501 and truncated is False)
    probe.check('timeline_chronological_values', rows[0]['views'] == 999500 and rows[-1]['views'] == 1000000)


class _RecoveryClockDatabase:
    """Observe now() inside the unchanged transaction requested by the repository."""
    def __init__(self, database):
        self.database = database
        self.now = None

    @contextmanager
    def transaction(self, operation):
        with self.database.transaction(operation) as tx:
            self.now = tx.fetch_one('SELECT now() AS value')['value']
            yield tx


def _stale_sync_expected_rows(now, recovered_at):
    """Independent expected stored rows for the six fixed synthetic inputs.

    This is a fixture, not the recovery codec: in particular the persisted
    vocabulary drops timestamp reports, dotted metadata reports and reason text.
    """
    cutoff = now - timedelta(hours=1)
    finished = now - timedelta(minutes=1)
    return [
        dict(status='completed', started_at=cutoff-timedelta(hours=3), finished_at=finished,
             errors=0, summary_json={'errors': 0}),
        dict(status='error', started_at=cutoff-timedelta(hours=2), finished_at=recovered_at,
             errors=1, summary_json={'summary_schema_version': 3, 'column_fields': 9,
                                    'reported_values': {'errors': 0}, 'stale_run_recovered': True}),
        dict(status='error', started_at=cutoff-timedelta(hours=1), finished_at=finished-timedelta(minutes=1),
             errors=2, summary_json={'summary_schema_version': 3, 'column_fields': 5,
                                    'routing': {'enabled': False}, 'stale_run_recovered': True}),
        dict(status='error', started_at=cutoff-timedelta(seconds=1), finished_at=recovered_at,
             errors=1, summary_json={'summary_schema_version': 3, 'column_fields': 5,
                                    'reported_values': {'errors': 0}, 'stale_run_recovered': True}),
        dict(status='running', started_at=cutoff, finished_at=None, errors=0, summary_json={}),
        dict(status='running', started_at=cutoff+timedelta(seconds=1), finished_at=None, errors=0, summary_json={}),
    ]


def stale_sync(probe):
    from db.runs.sync_runs import SyncRunRepository
    from psycopg.types.json import Jsonb
    cutoff = probe.now - timedelta(hours=1)
    finished = probe.now - timedelta(minutes=1)
    specs = [
        ("running", cutoff-timedelta(seconds=1), None, 0, {"errors": 0, "finished_at": None}),
        ("running", cutoff-timedelta(hours=1), finished-timedelta(minutes=1), 2,
         {"errors": 2, "finished_at": (finished-timedelta(minutes=1)).isoformat(), "routing": {"enabled": False}}),
        ("running", cutoff-timedelta(hours=2), None, 0, {"errors": 0, "channel_metadata": {"processed": 9}}),
        ("running", cutoff, None, 0, {}),
        ("running", cutoff+timedelta(seconds=1), None, 0, {}),
        ("completed", cutoff-timedelta(hours=3), finished, 0, {"errors": 0}),
    ]
    def seed():
        with probe.admin.transaction():
            for status, started, done, errors, summary in specs:
                probe.admin.execute("INSERT INTO public.sync_runs(run_type,status,started_at,finished_at,errors,summary_json) "
                    "VALUES('auto_sync',%s,%s,%s,%s,%s)", (status, started, done, errors, Jsonb(summary)))
        probe.observe()
    def rows():
        return probe.admin.execute("SELECT status,started_at,finished_at,errors,summary_json FROM public.sync_runs ORDER BY started_at")
    seed()
    count = probe.call(SyncRunRepository(probe.database).recover_stale_running,
                       cutoff.isoformat(), {'finished_at':finished.isoformat()})
    recovered = rows()
    probe.check('stale_recovery_exact_rows',
                count == 3 and recovered == _stale_sync_expected_rows(probe.now, finished))
    probe.check('stale_recovery_report_preserved', any(row['summary_json'].get('reported_values', {}).get('errors') == 0 for row in recovered))
    probe.check('stale_recovery_boundary_excluded', sum(row['status']=='running' for row in recovered)==2)
    probe.check('stale_recovery_replay_no_changes', probe.call(SyncRunRepository(probe.database).recover_stale_running,
                cutoff.isoformat(), {'finished_at':finished.isoformat()}) == 0 and rows()==recovered)
    # NULL finish means the transaction's native now(). Compare to its own
    # captured start rather than accept a fuzzy moving-clock tolerance.
    probe.reset()
    seed()
    clocked = _RecoveryClockDatabase(probe.database)
    count = probe.call(SyncRunRepository(clocked).recover_stale_running, cutoff.isoformat(), {})
    actual = rows()
    probe.check('stale_recovery_null_clock',
                count == 3 and actual == _stale_sync_expected_rows(probe.now, clocked.now))
    probe.reset()
    seed()
    with probe.admin.transaction():
        probe.admin.execute("UPDATE public.sync_runs SET summary_json=%s WHERE started_at=%s",
                            # The current schema has no summary-normalization trigger.
                            # A saved reported value conflicts with the next recovery's
                            # original errors=0, exercising the current Python invariant.
                            (Jsonb({'summary_schema_version': 3, 'column_fields': 1,
                                    'reported_values': {'errors': 7}}), cutoff-timedelta(hours=2)))
    before_rows = rows()
    try:
        probe.call(SyncRunRepository(probe.database).recover_stale_running,
                   cutoff.isoformat(), {'finished_at':finished.isoformat()})
    except Exception as error:
        rejected = getattr(error, 'code', None)
    else:
        rejected = None
    probe.check('stale_report_conflict_atomic', rows()==before_rows)
    probe.check('stale_report_conflict_rejected', rejected=='P0001')


def transaction_steps(probe):
    from db.base.postgres import Operation, DatabaseError, DatabaseServerError
    db = probe.database
    ids = [f'f0000000-0000-4000-8000-{number:012d}' for number in range(1,8)]
    def add(tx, identity):
        tx.execute('INSERT INTO public.folders(id,name) VALUES(%s::uuid,%s)', (identity,'synthetic-transaction'))
    def exists(identity):
        return probe.admin.scalar('SELECT EXISTS(SELECT 1 FROM public.folders WHERE id=%s::uuid)',(identity,))
    with db.transaction(Operation('probe_savepoint',total_timeout_ms=5000)) as tx:
        add(tx,ids[0])
        try:
            with tx.savepoint():
                add(tx,ids[1])
                add(tx,ids[0])
        except DatabaseServerError as error:
            probe.check('savepoint_known_unique_error',error.code=='23505')
        else:
            probe.check('savepoint_requires_error',False)
        add(tx,ids[2])
    probe.check('savepoint_partial_rollback',exists(ids[0]) and not exists(ids[1]) and exists(ids[2]))
    try:
        with db.transaction(Operation('probe_no_savepoint')) as tx:
            add(tx,ids[3])
            try:add(tx,ids[0])
            except DatabaseServerError:pass
    except DatabaseError as error:
        probe.check('swallowed_error_poisoned_parent',error.message=='database_transaction_aborted')
    else:
        probe.check('poisoning_required',False)
    probe.check('poisoning_rolled_back_prior_write',not exists(ids[3]))
    with db.transaction(Operation('probe_export_snapshot',read_only=True,repeatable_read=True)) as tx:
        first = tx.fetch_one('SELECT count(*) AS n FROM public.folders')['n']
        with probe.admin.transaction():
            probe.admin.execute('INSERT INTO public.folders(id,name) VALUES(%s::uuid,%s)',(ids[4],'concurrent-synthetic'))
        second = tx.fetch_one('SELECT count(*) AS n FROM public.folders')['n']
    probe.check('repeatable_read_same_snapshot',first==second==2)
    probe.check('repeatable_read_new_snapshot_sees_commit',db.read_row('probe_after_snapshot','SELECT count(*) AS n FROM public.folders')['n']==3)
    started = time.monotonic()
    try:
        with db.transaction(Operation('probe_budget',statement_timeout_ms=2000,total_timeout_ms=350)) as tx:
            add(tx,ids[5])
            tx.fetch_one('SELECT pg_sleep(0.25) AS pause')
            tx.fetch_one('SELECT pg_sleep(0.25) AS pause')
    except DatabaseServerError as error:
        probe.check('total_budget_not_per_statement',error.code=='57014')
    else:
        probe.check('budget_timeout_required',False)
    probe.check('budget_rolls_back_prior_write',not exists(ids[5]))
    probe.check('budget_wall_time_bounded',time.monotonic()-started<3)
    try:
        with db.transaction(Operation('probe_borrowed_failure')) as tx:
            add(tx,ids[6])
            try:
                with tx.borrow():
                    raise DatabaseServerError('channel_collection_scope_lost',code='P0001')
            except DatabaseServerError:
                pass
    except DatabaseError as error:
        probe.check('borrowed_domain_failure_poisoned_parent',error.message=='database_transaction_aborted')
    else:
        probe.check('borrowed_poisoning_required',False)
    probe.check('borrowed_failure_rolls_back_prior_write',not exists(ids[6]))
    with db.transaction(Operation('probe_borrowed_savepoint')) as tx:
        try:
            with tx.savepoint():
                with tx.borrow():
                    add(tx,ids[6])
                    raise DatabaseServerError('channel_collection_scope_lost',code='P0001')
        except DatabaseServerError:
            pass
        add(tx,ids[5])
    probe.check('borrowed_savepoint_recovers_after_rollback',not exists(ids[6]) and exists(ids[5]))

    probe.observe()


RUNNERS = {"user_work": user_work, "quota_sync": quota_sync, "reads": reads, "timeline": timeline, "stale_sync": stale_sync, "transaction_steps": transaction_steps}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument("--case", choices=sorted(CASES), action="append", default=[])
    parser.add_argument("--report", type=Path, help="Optional NEW JSON report; never overwrites an existing report.")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_cases:
        print(json.dumps({"cases": [{"case_id": name, "proves": detail} for name, detail in CASES.items()],
                          "limitations": LIMITATIONS}, indent=2))
        return 0
    if not args.case or len(args.case) != len(set(args.case)):
        parser.error("Select distinct --case names explicitly; no automatic full-suite selection.")
    if args.report and args.report.exists():
        parser.error("Report already exists; choose a new path.")
    report = {"format_version": 1, "scope": "selected_real_direct_postgres_repositories", "status": "running",
              "youtube_requests": 0, "seed_row_limit": safety.MAX_ROWS,
              "selected_cases": args.case, "cases": [], "fixture_cleanup_incomplete": False,
              "limitations": LIMITATIONS, "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    metrics = {"client_execute_calls": 0, "client_execute_seconds": 0.0}
    admin, dirty = None, False
    started = time.monotonic()
    try:
        dsns = safety.validate_configuration(os.environ.get("LIC_DIRECT_PG_PROBE_ADMIN_DSN", ""), os.environ.get("LIC_DIRECT_PG_PROBE_DSN", ""))
        admin = safety.connect(dsns[0], metrics)
        snapshots, before_counts = safety.preflight(admin)
        report["postgres_major"] = 17
        with self_hosted_profile(), open_database(dsns[1]) as database:
            identity = database.read_row("repository_probe_identity", "SELECT session_user AS login,current_user AS role")
            require(identity == {"login": "authenticator", "role": "service_role"}, "runtime_identity")
            report["runtime_identity_verified"] = True
            for case_id in args.case:
                item = {"case_id": case_id, "status": "running", "proves": CASES[case_id]}
                report["cases"].append(item)
                probe = Probe(admin, database, snapshots, before_counts)
                case_started = time.monotonic()
                dirty = True
                try:
                    RUNNERS[case_id](probe)
                    counts = probe.observe()
                    item["result_table_counts"] = {key: value for key, value in counts.items() if value}
                    require(probe.checks, "empty_case")
                    item["status"] = "passed"
                except BaseException as error:
                    item.update(safe_failure(error, stage=probe.stage))
                    item["status"] = "failed"
                    raise
                finally:
                    try:
                        probe.reset()
                        dirty = False
                    except BaseException as error:
                        item["status"] = "failed"
                        item["cleanup_failure"] = safe_failure(error)
                        raise
                    finally:
                        item.update(probe.report())
                        item["elapsed_seconds"] = round(time.monotonic() - case_started, 6)
        report["status"] = "passed"
    except BaseException as error:
        report["status"] = "failed"
        report.update(safe_failure(error))
        report["fixture_cleanup_incomplete"] = dirty
    finally:
        if admin is not None:
            admin.connection.close()
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    report["admin_metrics"] = {**metrics, "client_execute_seconds": round(metrics["client_execute_seconds"], 6)}
    report["passed_checks"] = sum(item.get("passed_checks", 0) for item in report["cases"])
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        with args.report.open("x", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
    print(encoded, flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
