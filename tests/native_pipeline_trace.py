"""Bounded passive-journal proof over real PG17 owners and fake providers only."""
from functools import partial
from pathlib import Path
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch
import argparse
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
import native_unified_availability as base
from native_unified_availability import CHANNEL, IDS, KINDS, seed, require
from schema25_regressions.fixtures import Fixture, admission, dsns
from db.videos.video_collection import VideoCollectionRepository
from collection.initial_video_hydration import run_initial_video_window
from collection.video_payloads import build_video_collection_metadata_payload, build_video_counter_payload
from video_collection_service import VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid
from youtube_source_fields import parse_dt
from telemetry_sink import JsonlTelemetryJournal, TelemetryEmitter
import pipeline_trace as trace
import telemetry


def collector(repository, fetch_full, fetch_counters):
    return VideoCollectionService(repository, fetch_full=fetch_full, fetch_metadata=fetch_full,
        fetch_initial_profiles=fetch_full, fetch_counters=fetch_counters,
        build_full_payload=partial(build_video_collection_metadata_payload, batch_stats_enabled=True,
            parse_dt=parse_dt, view_count_absent_error=YouTubeViewCountAbsent,
            view_count_invalid_error=YouTubeViewCountInvalid),
        build_counter_payload=build_video_counter_payload)


def profile(identity, published, *, counter=True):
    return dict(id=identity, snippet=dict(channelId=CHANNEL, title="Synthetic native profile",
        publishedAt=published.isoformat()), contentDetails=dict(duration="PT5M"),
        status=dict(privacyStatus="public", uploadStatus="processed"),
        statistics=dict(viewCount="23", likeCount="2", commentCount="1") if counter else {})


def _initial_fixture(f):
    """A real frozen initial job hands partial General results to regular work."""
    from psycopg.types.json import Jsonb
    from db.jobs.operator_jobs import OperatorJobRepository
    _, published = seed(f, operation="metadata")
    # A partial positive General response must also clear stale unavailable
    # state from a previous episode, without claiming that counters were read.
    f.rows("""UPDATE public.videos SET availability_status='unknown_unavailable',privacy_status=NULL,
        upload_status=NULL WHERE video_id=%s""", (IDS["short"],))
    f.rows("""UPDATE public.video_collection_state SET typed_collection_status='unavailable',
        availability_first_missing_at=%s,availability_schedule_type='short' WHERE video_id=%s""",
        (f.now - timedelta(days=14), IDS["short"]))
    f.rows("""INSERT INTO public.video_availability_incidents(video_id,channel_id,first_status,latest_status,
        first_seen_at,last_seen_at,source_first_seen,source_latest_seen)
        VALUES(%s,%s,'unknown_unavailable','unknown_unavailable',%s,%s,'typed_video_collection_missing','typed_video_collection_missing')""",
        (IDS["short"], CHANNEL, f.now - timedelta(days=14), f.now - timedelta(hours=1)))
    snapshots_before = f.scalar("SELECT count(*) FROM public.video_snapshots")
    job, token = str(uuid4()), str(uuid4())
    requested, started = f.now - timedelta(minutes=3), f.now - timedelta(minutes=2)
    boundary, checked = f.now - timedelta(minutes=1), f.now - timedelta(seconds=30)
    f.rows("""INSERT INTO public.operator_jobs(id,job_type,channel_id,requested_at,initial_scope_version,
        initial_video_types,max_items,max_pages,max_quota_units,phase,accepted_epoch)
        VALUES(%s,'channel_initial_long_inventory',%s,%s,1,ARRAY['long','short','stream'],
        NULL,NULL,NULL,'inventory',%s)""", (job, CHANNEL, requested, f.epoch(CHANNEL)))
    sources = {}
    for kind in KINDS:
        scan, transition = str(uuid4()), str(uuid4())
        playlist = {"long": "UULF", "short": "UUSH", "stream": "UULV"}[kind] + CHANNEL[2:]
        f.rows("""INSERT INTO public.channel_video_inventory_scans(scan_id,channel_id,video_type,playlist_id,
            scan_kind,status,coverage,scan_boundary_at,started_at,source_checked_at,completed_at,pages_completed)
            VALUES(%s,%s,%s,%s,'initial_inventory','completed','complete',%s,%s,%s,%s,1)""",
            (scan, CHANNEL, kind, playlist, boundary, started, checked, checked))
        f.rows("""INSERT INTO public.typed_inventory_receipts(receipt_scope,transition_id,scan_id,
            transition_kind,source_checked_at,result) VALUES('scan_transition',%s,%s,'page',%s,'{}'::jsonb)""",
            (transition, scan, checked))
        sources[kind] = dict(scan_id=scan, started_at=started.isoformat(), scan_boundary_at=boundary.isoformat(),
            source_checked_at=checked.isoformat(), acked_pages=1, terminal_outcome="completed", last_transition_id=transition)
        f.rows("""INSERT INTO public.channel_playlist_state(channel_id,video_type,playlist_id,runtime_enabled,
            inventory_status,last_completed_scan_id) VALUES(%s,%s,%s,true,'completed',%s)
            ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,inventory_status='completed',
            last_completed_scan_id=excluded.last_completed_scan_id""", (CHANNEL, kind, playlist, scan))
    summary = dict(full_inventory_sources=sources, inventory_results={kind: dict(inventory_ready=True) for kind in KINDS})
    f.rows("""INSERT INTO public.operator_job_targets(job_id,video_id,video_type)
        SELECT %s,video_id,video_type FROM public.video_inventory WHERE channel_id=%s""", (job, CHANNEL))
    f.rows("""UPDATE public.operator_jobs SET status='running',lease_token=%s,lease_owner='unified-availability-proof',
        lease_channel_epoch=accepted_epoch,lease_expires_at=now()+interval '5 minutes',target_count=3,
        max_target_attempts=3,phase='hydrate_videos',targets_frozen_at=%s,summary_json=%s WHERE id=%s""",
        (token, f.now - timedelta(seconds=10), Jsonb(summary), job))
    f.rows("UPDATE public.channel_collection_state SET initial_job_id=%s WHERE channel_id=%s", (job, CHANNEL))
    return job, token, published


def initial_actual_owner(f):
    job, token, published = _initial_fixture(f)
    from db.jobs.operator_jobs import OperatorJobRepository
    prepared = OperatorJobRepository(f.database).prepare_initial_hydration_chunk(job, token, limit=50)
    f.check(set(prepared["video_ids"]) == set(IDS.values()), "initial_real_owner_prepared_targets")
    f.check(f.scalar("SELECT count(*) FROM public.operator_job_targets WHERE job_id=%s AND job_attempts=1",
                     (job,)) == 3, "initial_claim_requires_prepared_attempts")
    repository = VideoCollectionRepository(f.database)
    def general(identities, reason):
        trace.event("native_provider", "received", quota_bucket="general", video_ids=list(identities))
        return [profile(identity, published, counter=False) for identity in identities if identity != IDS["stream"]]
    def counters(identities, reason):
        trace.event("native_provider", "received", quota_bucket="stats", video_ids=list(identities))
        return dict(items=[], summary=dict(failed_ids=list(identities)))
    result = run_initial_video_window(collector(repository, general, counters), job_id=job,
        channel_id=CHANNEL, video_ids=list(IDS.values()), worker_id="unified-availability-proof",
        run_id=token)
    f.check(not result["settlement_unknown"] and result["metadata_deferred"] == 2
            and result["unavailable_marked"] == 1, "initial_real_owner_partial_handoff")
    for kind in ("long", "short"):
        row = base.state(f, IDS[kind])
        f.check(row["last_error_type"] == "youtube_view_count_absent" and row["lease_token"] is None,
                "initial_counterless_committed_before_trace_ack")
    f.check(base.state(f, IDS["stream"])["availability_first_missing_at"] is not None,
            "initial_missing_committed_before_trace_ack")


def lost_ack_after_real_commit(f):
    _, published = seed(f, operation="metadata")
    rows = base.claim(f, kinds=("long",), operation="metadata")["rows"]
    f.check(len(rows) == 1, "lost_ack_single_owned_claim")
    repository = VideoCollectionRepository(f.database)
    original = repository.complete_metadata
    def uncertain(payload):
        original(payload)
        raise OSError("synthetic lost acknowledgement")
    repository.complete_metadata = uncertain
    service = collector(repository,
        lambda ids, reason: [profile(identity, published) for identity in ids],
        lambda *args: (_ for _ in ()).throw(AssertionError("no_stats_fallback_needed")))
    result = service.process_common_claims(rows)
    f.check(result["settlement_unknown"] and result["updated"] == 0, "lost_ack_not_reported_as_success")
    f.check(f.scalar("SELECT views FROM public.videos WHERE video_id=%s", (IDS["long"],)) == 23,
            "unknown_ack_can_hide_real_commit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-proof", action="store_true")
    parser.add_argument("--installation", choices=("fresh",), default="fresh")
    cases = (base.common_feeder_all_types, initial_actual_owner, lost_ack_after_real_commit)
    parser.add_argument("--case", choices=tuple(case.__name__ for case in cases), action="append")
    args = parser.parse_args()
    selected = set(args.case or [case.__name__ for case in cases])
    if not args.run_proof:
        print(json.dumps(dict(native_executed=False, cases=[c.__name__ for c in cases if c.__name__ in selected], max_rows=1000,
                              work_seconds=420, youtube_requests=0)))
        return 0
    require(os.getenv("LIC_SCHEMA_CI_MAX_ROWS") == "1000", "explicit_row_budget")
    out = Path("/out")
    require(out.is_dir(), "owned_output_required")
    journal_path = out / "pipeline.jsonl"
    require(not journal_path.exists(), "fresh_journal_required")
    emitter = TelemetryEmitter(JsonlTelemetryJournal(journal_path, max_bytes=8 * 1024 * 1024),
                               queue_size=4096, batch_size=64)
    report = dict(status="failed", cases=[], fixture_restored=False, youtube_requests=0,
                  limitation="Synthetic provider; fresh temporary PG17, real passive sink and collection owners.")
    started = time.monotonic()
    try:
        with patch.object(telemetry, "default_emitter", return_value=emitter), admission() as gate:
            f = Fixture(*dsns(), gate)
            try:
                for index, case in enumerate(cases, 1):
                    if case.__name__ not in selected:
                        continue
                    before = f.checks
                    item = dict(name=case.__name__, status="failed")
                    try:
                        with trace.scope("native_case", sync_run_id=index) as span:
                            case(f)
                            span.finish("completed")
                        require(emitter.flush(3), "journal_flush")
                        events = [json.loads(line) for line in journal_path.read_text().splitlines()]
                        events = [event for event in events if event.get("sync_run_id") == index]
                        writes = [event for event in events if event["operation"] == "video_write"]
                        confirmed = [event for event in writes if event["status"] == "confirmed"]
                        if index < 3:
                            require({event["video_id"] for event in confirmed} == set(IDS.values()),
                                    "all_actual_commits_traced")
                            selected = {event["video_id"]: event for event in events
                                        if event["operation"] == "video_selected"}
                            require(set(selected) == set(IDS.values()), "all_actual_claims_traced")
                            for event in confirmed:
                                require(event["batch_id"] == selected[event["video_id"]]["batch_id"],
                                        "claim_commit_batch_join")
                                require("due_at" not in event, "no_invented_next_due")
                            require(all(len(event.get("video_ids", [])) <= 50 for event in events),
                                    "bounded_batch_manifest")
                        else:
                            require(not confirmed and any(event["status"] == "unknown" for event in writes),
                                    "lost_ack_is_unknown_in_journal")
                        metrics = emitter.metrics()
                        require(all(metrics[key] == 0 for key in ("invalid", "dropped", "sink_errors")),
                                "journal_has_no_rejected_or_lost_records")
                        item.update(status="passed", events=len(events), rows=f.observe())
                    except Exception as error:
                        item.update(base.error_details(error))
                    item["checks"] = f.checks - before
                    report["cases"].append(item)
                    f.reset()
                    require(time.monotonic() < f.deadline, "suite_deadline")
            finally:
                f.close()
                report["fixture_restored"] = True
        report["status"] = "passed" if all(item["status"] == "passed" for item in report["cases"]) else "failed"
    except Exception as error:
        report.update(base.error_details(error))
    finally:
        emitter.close(3)
        report["journal_metrics"] = emitter.metrics()
    report["seconds"] = round(time.monotonic() - started, 6)
    (out / "RESULT.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(dict(status=report["status"], cases=len(report["cases"]), seconds=report["seconds"])))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
