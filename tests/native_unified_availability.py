"""Bounded PG17 acceptance for unified counters and unavailable-video episodes.

Uses the real common feeder, dispatch, service, queue claims, completion, typed
inventory page settlement and retention against a disposable database. Only
provider responses are synthetic; no network request or live installation is
used. Administrator DML creates reachable synthetic fixtures;
all behavior under test runs as the restricted backend role, with guards enabled.
The same proof runs on fresh schema28 and after the reviewed 27 -> 28 upgrade.
"""
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
import argparse
import json
import os
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
from schema25_regressions.fixtures import Fixture, admission, dsns
from db.inventory.typed_inventory import TypedInventoryRepository
from db.videos.video_collection import VideoCollectionRepository
from db.videos.video_typed_reconciliation import reconcile_typed_video_collection_state

ROW_CAP = 1000
WORK_SECONDS = 420
CHANNEL = "UC0000000000000000000081"
KINDS = ("long", "short", "stream")
IDS = {kind: "u" + str(index).zfill(10) for index, kind in enumerate(KINDS, 1)}
UPGRADE_CHANNEL = "UC0000000000000000000099"
# Execute only in the disposable schema27 database before the normal migration.
# No DDL, trigger switch, fabricated fresh API clock or live-installation access.
UPGRADE_SEED_SQL = """
INSERT INTO public.channels(channel_id,title,initial_long_inventory_status,initial_28d_stats_status)
VALUES('UC0000000000000000000099','Synthetic upgrade owner','completed','completed');
INSERT INTO public.channel_collection_state(channel_id,user_work_id,owner_permission,control_epoch)
SELECT channel_id,user_work_id,'accepted',nextval('public.channel_collection_epoch_seq')
FROM public.channels WHERE channel_id='UC0000000000000000000099';
INSERT INTO public.video_inventory(video_id,channel_id,video_type)
SELECT video_id,'UC0000000000000000000099',kind
FROM (VALUES('u0000000901','long'),('u0000000902','short'),
            ('u0000000903','stream'),('u0000000904','long'),('u0000000905','short')) fixture(video_id,kind);
INSERT INTO public.videos(video_id,channel_id,availability_status,availability_checked_at)
SELECT video_id,'UC0000000000000000000099',
 CASE WHEN video_id='u0000000904' THEN 'public' ELSE 'unknown_unavailable' END,now()-interval '3 days'
FROM public.video_inventory WHERE channel_id='UC0000000000000000000099';
INSERT INTO public.video_collection_state(video_id,channel_id,video_type,stats_due_at,
 metadata_due_at,next_due_at,typed_collection_status,typed_collection_checked_at,paused_reason)
SELECT video_id,channel_id,video_type,now()+interval '2 days',now()+interval '4 days',
 CASE WHEN video_id='u0000000905' THEN NULL WHEN video_id='u0000000904' THEN now()+interval '2 days' ELSE now()-interval '1 minute' END,
 CASE WHEN video_id='u0000000902' THEN 'unavailable' END,
 CASE WHEN video_id='u0000000902' THEN now()-interval '7 days' END,
 CASE WHEN video_id='u0000000905' THEN 'channel_hidden' END
FROM public.video_inventory WHERE channel_id='UC0000000000000000000099';
INSERT INTO public.video_availability_incidents(video_id,channel_id,first_status,latest_status,
 first_seen_at,last_seen_at,source_first_seen,source_latest_seen)
VALUES('u0000000901','UC0000000000000000000099','unknown_unavailable','unknown_unavailable',
 now()-interval '30 days',now()-interval '3 days','video_stats_sync','video_stats_sync');
"""


def verify_upgrade_seed(admin):
    """Verify migration source precedence, preserved live deadlines and defaults."""
    rows = admin.execute("""SELECT state.video_id,state.availability_first_missing_at,
        state.availability_schedule_type,state.availability_resume_observed_at,state.video_type,
        state.typed_collection_checked_at,video.availability_checked_at,incident.first_seen_at,
        state.stats_due_at,state.metadata_due_at,state.next_due_at,state.paused_reason
        FROM public.video_collection_state state JOIN public.videos video USING(video_id)
        LEFT JOIN public.video_availability_incidents incident ON incident.video_id=state.video_id
        AND incident.restored_at IS NULL WHERE state.channel_id=%s ORDER BY state.video_id""",
        (UPGRADE_CHANNEL,)).fetchall()
    require(len(rows) == 5, "upgrade_five_seed_states")
    for row in rows:
        expected = (row["first_seen_at"] or row["typed_collection_checked_at"] or row["availability_checked_at"])
        if row["video_id"] == "u0000000904":
            require(row["availability_first_missing_at"] is None and row["availability_schedule_type"] is None,
                    "upgrade_positive_video_no_episode")
            require(row["next_due_at"] == row["stats_due_at"]
                and row["metadata_due_at"] == row["stats_due_at"] + timedelta(days=2),
                    "upgrade_positive_deadlines_unchanged")
        else:
            require(row["availability_first_missing_at"] == expected
                and row["availability_schedule_type"] == row["video_type"], "upgrade_uses_existing_observation")
        require(row["availability_resume_observed_at"] is None, "upgrade_no_invented_return_evidence")
        if row["video_id"] == "u0000000905":
            require(row["paused_reason"] is None and row["next_due_at"] is not None,
                    "upgrade_generic_hold_does_not_strand_episode")
    # The reusable fixture restores its empty application baseline alphabetically.
    # Remove this exact verified seed before it captures that baseline; otherwise
    # the upgrade's dependent rows would need a second fixture restore mechanism.
    for table in ("video_availability_incidents", "videos", "video_inventory", "channels"):
        admin.execute("DELETE FROM public." + table + " WHERE channel_id=%s", (UPGRADE_CHANNEL,))
    require(admin.execute("SELECT count(*) AS n FROM public.video_collection_state WHERE channel_id=%s",
        (UPGRADE_CHANNEL,)).fetchone()["n"] == 0, "upgrade_seed_cleaned_with_guards_enabled")
    return dict(status="passed", checks=13, youtube_requests=0, upgrade_seed_removed=True)


def require(ok, code):
    if not ok:
        raise AssertionError(code)


def instant(f):
    return f.scalar("SELECT clock_timestamp()")


def state(f, video):
    return f.rows("SELECT * FROM public.video_collection_state WHERE video_id=%s", (video,))[0]


def unavailable_read_proof(f, kind):
    proof = f.database.read_row("unified_native_read_proof",
        "SELECT unavailable_observed FROM readable.video_hydration_proof WHERE video_id=%s", (IDS[kind],))
    health = f.database.read_row("unified_native_health",
        "SELECT (value->'video_types'->%s::text->>'unavailable')::integer AS count "
        "FROM readable.typed_video_collection_health", (kind,))
    return proof["unavailable_observed"], health["count"]


def seed(f, *, operation="counters", episode_days=None):
    """Three fresh source profiles and typed proofs; no expired API fixture."""
    f.channel(CHANNEL)
    f.rows("UPDATE public.typed_video_collection_runtime SET runtime_enabled=true WHERE video_type IN('short','stream')")
    acquired = f.now - timedelta(hours=1)
    published = f.now - timedelta(days=200)
    due, future = f.now - timedelta(minutes=1), f.now + timedelta(days=2)
    for kind, video in IDS.items():
        end = published + timedelta(hours=1) if kind == "stream" else None
        f.rows("""INSERT INTO public.video_inventory
            (video_id,channel_id,video_type,published_at,published_acquired_at)
            VALUES(%s,%s,%s,%s,%s)""", (video, CHANNEL, kind, published, acquired))
        f.rows("""INSERT INTO public.videos(video_id,channel_id,title,published_at,duration,
            metadata_checked_at,checked_at,availability_checked_at,availability_status,
            privacy_status,upload_status,views,like_count,comment_count,stream_actual_end_at)
            VALUES(%s,%s,'Synthetic native video',%s,'PT5M',%s,%s,%s,%s,%s,%s,1,1,0,%s)""",
            (video, CHANNEL, published, acquired, acquired, acquired,
             "public" if episode_days is None else "unknown_unavailable",
             "public" if episode_days is None else None, "processed" if episode_days is None else None, end))
        f.rows("""INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
            hydration_needed,profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,
            stats_source_checked_at,metadata_source_checked_at,typed_collection_status,
            typed_collection_checked_at,typed_source_published_at,typed_source_published_acquired_at,
            typed_stream_actual_end_at,typed_source_stream_end_acquired_at,
            availability_first_missing_at,availability_schedule_type)
            VALUES(%s,%s,%s,false,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (video, CHANNEL, kind, due if operation == "counters" else future,
             due if operation == "metadata" else future, due, acquired, acquired,
             None if kind == "long" else "unavailable" if episode_days is not None else "hydrated",
             None if kind == "long" else acquired, None if kind == "long" else published,
             None if kind == "long" else acquired, end, acquired if end else None,
             f.now - timedelta(days=episode_days) if episode_days is not None else None,
             kind if episode_days is not None else None))
        if kind != "long":
            scan = str(uuid4())
            playlist = ("UUSH" if kind == "short" else "UULV") + CHANNEL[2:]
            f.rows("""INSERT INTO public.channel_video_inventory_scans
                (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,
                 scan_boundary_at,started_at,completed_at,source_checked_at,pages_completed,items_seen)
                VALUES(%s,%s,%s,%s,'full_reconciliation','completed','complete',%s,%s,%s,%s,1,1)""",
                (scan, CHANNEL, kind, playlist, acquired, acquired, acquired, acquired))
            f.rows("""INSERT INTO public.channel_playlist_state
                (channel_id,video_type,playlist_id,runtime_enabled,status,next_due_at,
                 inventory_status,last_completed_scan_id,inventory_completed_at)
                VALUES(%s,%s,%s,true,'ready',now(),'completed',%s,%s)
                ON CONFLICT(channel_id,video_type) DO UPDATE SET runtime_enabled=true,status='ready',
                next_due_at=now(),inventory_status='completed',last_completed_scan_id=excluded.last_completed_scan_id,
                inventory_completed_at=excluded.inventory_completed_at""", (CHANNEL, kind, playlist, scan, acquired))
            f.rows("""INSERT INTO public.video_inventory_memberships
                (video_id,channel_id,video_type,playlist_id,source_published_at,first_seen_scan_id,
                 last_seen_scan_id,membership_status,first_seen_at,last_seen_at,last_present_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'present',%s,%s,%s)""",
                (video, CHANNEL, kind, playlist, published, scan, scan, acquired, acquired, acquired))
    f.observe()
    return acquired, published


def claim(f, *, kinds=KINDS, operation=None):
    return VideoCollectionRepository(f.database).claim_due_batch(
        limit=50, worker_id="unified-availability-proof", run_id=str(uuid4()), lease_seconds=90,
        scopes=[dict(channel_id=CHANNEL, video_type=kind) for kind in kinds], operation=operation)


def payload(owned, observed, **values):
    return dict(video_id=owned.video_id, channel_id=owned.channel_id, video_type=owned.video_type,
        lease_token=str(owned.lease_token), collection_attempt_id=str(uuid4()),
        recorded_at=observed.isoformat(), **values)


def missing_payload(owned, observed):
    return payload(owned, observed, availability_status="unknown_unavailable",
                   availability_source="video_stats_sync")


def counters_all_types(f):
    acquired, _ = seed(f)
    before = {kind: state(f, video) for kind, video in IDS.items()}
    result = claim(f, operation="counters")
    f.check({row.video_type for row in result["rows"]} == set(KINDS), "three_counter_types_claimed")
    f.check(all(row.collection_mode == "counters" for row in result["rows"]), "all_claims_are_counters")
    observed = instant(f)
    rows = [payload(row, observed, views=12, like_count=2, comment_count=1) for row in result["rows"]]
    repo = VideoCollectionRepository(f.database)
    f.check(repo.complete_counters(rows) == 3, "three_counter_types_completed")
    for kind, video in IDS.items():
        after = state(f, video)
        f.check(after["metadata_due_at"] == before[kind]["metadata_due_at"], "stats_preserve_metadata_deadline")
        f.check(after["metadata_source_checked_at"] == acquired, "stats_preserve_metadata_clock")
        f.check(after["stats_source_checked_at"] == observed, "stats_have_own_clock")
        f.check(after["stats_due_at"] == observed + timedelta(days=1), "same_initial_stats_defaults")
        profile = f.rows("SELECT checked_at,metadata_checked_at FROM public.videos WHERE video_id=%s", (video,))[0]
        f.check(profile == dict(checked_at=observed, metadata_checked_at=acquired), "source_clocks_separate")
    settled = {video: state(f, video) for video in IDS.values()}
    f.check(repo.complete_counters(rows) == 3, "counter_receipt_replay")
    f.check(settled == {video: state(f, video) for video in IDS.values()}, "counter_replay_no_reschedule")


def common_feeder_all_types(f):
    """Prove typed Stats through the actual producer and service, with real SQL."""
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial
    from threading import get_ident
    from collection.auto_pass import AutoPassState, CommonCandidateCursor
    from collection.common_feeder import CommonAutoFeeder
    from collection.dispatch import Coordinator
    from collection.video_payloads import build_video_collection_metadata_payload, build_video_counter_payload
    from video_collection_service import VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid
    from youtube_source_fields import parse_dt

    acquired, _ = seed(f)
    before = {kind: state(f, video) for kind, video in IDS.items()}
    owner = get_ident()
    calls = []

    def unexpected_general(*args, **kwargs):
        raise AssertionError("counter_only_fixture_must_not_request_general")

    def fetch_counters(video_ids, reason):
        require(get_ident() != owner, "stats_provider_uses_coordinator_worker")
        calls.append(tuple(video_ids))
        return dict(items=[dict(id=video, views=123, like_count=7, comment_count=2)
                           for video in video_ids], summary=dict(failed_ids=[]))

    repository = VideoCollectionRepository(f.database)
    repository.preflight_common_pool_ready()
    service = VideoCollectionService(repository, fetch_full=unexpected_general,
        fetch_metadata=unexpected_general, fetch_counters=fetch_counters,
        build_full_payload=partial(build_video_collection_metadata_payload, batch_stats_enabled=True,
            parse_dt=parse_dt, view_count_absent_error=YouTubeViewCountAbsent,
            view_count_invalid_error=YouTubeViewCountInvalid),
        build_counter_payload=build_video_counter_payload)
    feeder = CommonAutoFeeder(service, state=AutoPassState(10, concurrency=2),
        cursor=CommonCandidateCursor(), worker_id="unified-native-common-feeder",
        require_ready=f.gate.require_collection_ready, lease_seconds=90)
    with ThreadPoolExecutor(max_workers=2) as executor:
        coordinator = Coordinator(executor, 2)
        deadline = min(time.monotonic() + 20, f.deadline)
        while time.monotonic() < deadline:
            feeder.offer_to(coordinator)
            coordinator.step()
            current = coordinator.snapshot()
            if feeder.exhausted and not current.ready and not current.held:
                break
            coordinator.wait(.001)
        else:
            raise AssertionError("native_common_feeder_did_not_finish")
        feeder.finish_pass(coordinator)

    requested = [video for batch in calls for video in batch]
    f.check(len(requested) == 3 and set(requested) == set(IDS.values()), "common_feeder_stats_all_three_once")
    f.check(feeder.summary["updated"] == 3 and feeder.summary["errors"] == 0,
            "common_feeder_three_updates_without_errors")
    f.check(all(feeder.summary[kind + "_updated"] == 1 for kind in KINDS), "common_feeder_keeps_typed_totals")
    f.check(f.scalar("SELECT count(*) FROM public.video_snapshots WHERE video_id=ANY(%s::text[])",
        (list(IDS.values()),)) == 3, "common_feeder_writes_three_snapshots")
    for kind, video in IDS.items():
        after = state(f, video)
        f.check(after["lease_token"] is None and after["stats_source_checked_at"] > acquired
            and after["stats_due_at"] > after["stats_source_checked_at"], "common_feeder_settles_and_reschedules_stats")
        f.check(after["metadata_source_checked_at"] == acquired
            and after["metadata_due_at"] == before[kind]["metadata_due_at"], "common_feeder_preserves_metadata_schedule")
        f.check(f.scalar("SELECT views FROM public.videos WHERE video_id=%s", (video,)) == 123,
                "common_feeder_commits_provider_counters")
    proof = f.database.read_rows("unified_native_profile_proof",
        "SELECT video_id,profile_observed FROM readable.video_hydration_proof WHERE video_id=ANY(%s::text[])",
        ([IDS["short"], IDS["stream"]],), max_rows=2)
    f.check(len(proof) == 2 and all(row["profile_observed"] for row in proof), "common_stats_keep_typed_profile_readable")


def missing_calendar(f):
    for age, interval in ((None, 1), (7, 7), (28, None), (29, None)):
        f.reset()
        seed(f, operation="metadata", episode_days=age)
        before = {video: state(f, video) for video in IDS.values()}
        result = claim(f, operation="metadata")
        repo = VideoCollectionRepository(f.database)
        if age == 29:
            f.check(result["rows"] == [] and result["paused_count"] == 3, "day29_stops_before_provider_lease")
            for video in IDS.values():
                after = state(f, video)
                f.check(after["paused_reason"] == "availability_retry_expired" and after["next_due_at"] is None,
                        "day29_local_stop")
                f.check(after["attempt_count"] == before[video]["attempt_count"], "day29_no_new_provider_attempt")
            continue
        f.check(len(result["rows"]) == 3, "missing_all_types_claimed")
        observed = instant(f)
        rows = [missing_payload(row, observed) for row in result["rows"]]
        f.check(repo.complete_missing(rows) == 3, "missing_completion_all_types")
        for video in IDS.values():
            after = state(f, video)
            first = observed if age is None else before[video]["availability_first_missing_at"]
            expected = observed + timedelta(days=interval) if interval else first + timedelta(days=29)
            f.check(after["availability_first_missing_at"] == first, "first_miss_not_renewed")
            f.check(after["next_due_at"] == expected and after["metadata_due_at"] == expected, "missing_due_daily_weekly_or_stop")
            f.check(after["stats_due_at"] is None, "missing_suspends_stats")
        settled = {video: state(f, video) for video in IDS.values()}
        f.check(repo.complete_missing(rows) == 3, "missing_exact_receipt_replay")
        f.check(settled == {video: state(f, video) for video in IDS.values()}, "missing_replay_preserves_episode")
        for kind in ("short", "stream"):
            f.check(unavailable_read_proof(f, kind) == (True, 1), "typed_missing_visible_without_fresh_metadata")


def positive_general(f):
    _, published = seed(f, operation="metadata", episode_days=14)
    result = claim(f, operation="metadata")
    f.check(len(result["rows"]) == 3, "positive_general_claims_all_types")
    repo = VideoCollectionRepository(f.database)
    observed = instant(f)
    for owned in result["rows"]:
        row = payload(owned, observed, title="Synthetic restored video", views=14, like_count=2,
            comment_count=0, published_at=published.isoformat(), duration="PT5M", tags=[],
            metadata_observed_at=observed.isoformat(), availability_checked_at=observed.isoformat(),
            availability_status="public", privacy_status="public", upload_status="processed",
            youtube_topic_categories=[], stream_actual_end_at=(published + timedelta(hours=1)).isoformat()
                if owned.video_type == "stream" else None)
        completed = repo.complete_metadata([row]) if owned.video_type == "long" else repo.complete_typed_metadata([row])
        f.check(completed == 1, "positive_general_completion")
        after = state(f, owned.video_id)
        f.check(after["availability_first_missing_at"] is None and after["availability_schedule_type"] is None
            and not after["availability_resume_observed_at"] and after["paused_reason"] is None,
            "positive_general_closes_missing_episode")
        f.check(after["stats_due_at"] > observed and after["metadata_due_at"] > observed, "normal_collection_resumes")


def initial_counterless_and_missing_handoff(f):
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
    jobs, videos = OperatorJobRepository(f.database), VideoCollectionRepository(f.database)
    prepared = jobs.prepare_initial_hydration_chunk(job, token, limit=50)
    f.check(set(prepared["video_ids"]) == set(IDS.values()), "initial_three_targets_prepared")
    claims = videos.claim_initial_video_ids(prepared["video_ids"], job_id=job, channel_id=CHANNEL,
        worker_id="unified-availability-proof", run_id=token)
    f.check(len(claims) == 3, "initial_three_video_leases")
    observed = instant(f)
    counterless = [payload(owned, observed, missing=False, counterless_profile=True,
        title="Synthetic counterless profile", published_at=published.isoformat(), duration="PT5M",
        tags=[], youtube_topic_categories=[], availability_status="public", privacy_status="public",
        upload_status="processed") for owned in claims if owned.video_type != "stream"]
    missing = [missing_payload(owned, observed) for owned in claims if owned.video_type == "stream"]
    f.check(videos.complete_initial_batch([
        dict(operation="complete_identity_refresh", rows=counterless),
        dict(operation="complete_missing", rows=missing)]) == 3, "initial_atomic_counterless_and_missing")
    for kind in ("long", "short"):
        after = state(f, IDS[kind])
        f.check(after["availability_first_missing_at"] is None and after["stats_due_at"] is None
            and after["metadata_source_checked_at"] == observed and after["last_error_type"] == "youtube_view_count_absent",
            "counterless_is_present_and_metadata_only")
        f.check(after["next_due_at"] == observed + timedelta(days=1), "counterless_general_retry_uses_counter_interval")
    f.check(state(f, IDS["short"])["typed_collection_status"] == "pending"
        and unavailable_read_proof(f, "short") == (False, 0), "counterless_positive_clears_unavailable_health")
    f.check(f.scalar("SELECT count(*) FROM public.video_snapshots") == snapshots_before,
            "counterless_initial_does_not_invent_stats_snapshot")
    stream = state(f, IDS["stream"])
    f.check(stream["availability_first_missing_at"] == observed and stream["metadata_source_checked_at"] < requested,
            "initial_missing_does_not_invent_metadata_observation")
    f.check(unavailable_read_proof(f, "stream") == (True, 1), "initial_typed_missing_has_readable_proof")
    settled = jobs.settle_initial_hydration_chunk(job, token, observed_quota_units=1, error_code=None, summary={})
    targets = f.rows("SELECT video_type,status,result_code FROM public.operator_job_targets WHERE job_id=%s ORDER BY video_type", (job,))
    f.check(targets == [dict(video_type="long", status="checked", result_code="counters_pending"),
        dict(video_type="short", status="checked", result_code="counters_pending"),
        dict(video_type="stream", status="unavailable", result_code="unavailable_confirmed")], "initial_targets_handed_to_regular")
    f.check(settled["status"] == "queued" and settled["phase"] == "verify", "initial_job_can_verify_without_stats")
    claimed = jobs.claim_initial_hydration(CHANNEL, "unified-availability-proof")
    f.check(claimed is not None and claimed["phase"] == "verify", "initial_verification_claim")
    verified = jobs.verify_initial_hydration(job, claimed["lease_token"])
    f.check(verified["status"] == "completed", "initial_counterless_handoff_finished")


def seed_absence_generation(f, kind):
    """Represent a completed later empty scan, retaining all intrinsic guards."""
    scan, observed = str(uuid4()), instant(f)
    playlist = {"long": "UULF", "short": "UUSH", "stream": "UULV"}[kind] + CHANNEL[2:]
    f.rows("""INSERT INTO public.channel_video_inventory_scans
        (scan_id,channel_id,video_type,playlist_id,scan_kind,status,coverage,
         scan_boundary_at,started_at,completed_at,source_checked_at,pages_completed,items_seen)
        VALUES(%s,%s,%s,%s,'full_reconciliation','completed','complete',%s,%s,%s,%s,1,0)""",
        (scan, CHANNEL, kind, playlist, observed, observed, observed, observed))
    f.rows("""UPDATE public.video_inventory_memberships SET membership_status='absent',absent_since=%s,
        absence_confirmed_by_scan_id=%s,last_seen_at=%s WHERE video_id=%s""", (observed, scan, observed, IDS[kind]))
    f.rows("""UPDATE public.channel_playlist_state SET last_completed_scan_id=%s,
        inventory_completed_at=%s,source_checked_at=%s WHERE channel_id=%s AND video_type=%s""",
        (scan, observed, observed, CHANNEL, kind))


def no_type_proof(f):
    seed(f)
    video = IDS["short"]
    seed_absence_generation(f, "short")
    reconcile_typed_video_collection_state(f.database, "short", 10)
    result = claim(f, kinds=("short",))
    f.check(len(result["rows"]) == 1 and result["rows"][0].collection_mode == "identity_refresh",
            "lost_type_proof_uses_general_identity_not_stats")
    owned = result["rows"][0]
    observed = instant(f)
    row = payload(owned, observed, missing=True)
    repo = VideoCollectionRepository(f.database)
    f.check(repo.complete_identity_refresh([row]) == 1, "identity_missing_completes")
    after = state(f, video)
    f.check(after["availability_first_missing_at"] == observed and after["next_due_at"] == observed + timedelta(days=1),
            "identity_missing_uses_shared_daily_policy")
    f.check(repo.complete_identity_refresh([row]) == 1 and state(f, video) == after, "identity_replay_stable")


def unfinished_stream_returns(f):
    seed(f, operation="metadata", episode_days=14)
    video = IDS["stream"]
    f.rows("UPDATE public.videos SET stream_actual_end_at=NULL WHERE video_id=%s", (video,))
    f.rows("""UPDATE public.video_collection_state SET typed_stream_actual_end_at=NULL,
        typed_source_stream_end_acquired_at=NULL WHERE video_id=%s""", (video,))
    f.rows("""INSERT INTO public.video_availability_incidents(video_id,channel_id,first_status,latest_status,
        first_seen_at,last_seen_at,source_first_seen,source_latest_seen)
        VALUES(%s,%s,'unknown_unavailable','unknown_unavailable',%s,%s,'typed_video_collection_missing','typed_video_collection_missing')""",
        (video, CHANNEL, f.now - timedelta(days=14), f.now - timedelta(hours=1)))
    result = claim(f, kinds=("stream",), operation="metadata")
    f.check(len(result["rows"]) == 1 and result["rows"][0].collection_mode == "stream_lifecycle", "unfinished_stream_lifecycle_claim")
    observed = instant(f)
    row = payload(result["rows"][0], observed, stream_actual_end_at=None,
        availability_status="public", privacy_status="public", upload_status="processed",
        title="Waiting stream",published_at=(f.now-timedelta(days=200)).isoformat(),
        duration=None,publication_status="live",scheduled_start_at=None)
    repo = VideoCollectionRepository(f.database)
    f.check(repo.complete_stream_lifecycle([row]) == 1, "unfinished_stream_positive_general")
    after = state(f, video)
    f.check(after["availability_first_missing_at"] is None and after["availability_schedule_type"] is None
        and after["stats_due_at"] is None and after["typed_collection_status"] == "awaiting_completion",
        "unfinished_return_clears_episode_without_stats")
    f.check(f.scalar("SELECT restored_at FROM public.video_availability_incidents WHERE video_id=%s", (video,)) == observed,
            "unfinished_return_restores_incident")
    f.check(f.scalar("SELECT count(*) FROM public.video_availability_events WHERE video_id=%s AND event_type='restored'", (video,)) == 1,
            "unfinished_return_has_one_event")
    f.check(repo.complete_stream_lifecycle([row]) == 1 and state(f, video) == after, "unfinished_return_replay_stable")
    f.check(f.scalar("SELECT count(*) FROM public.video_availability_events WHERE video_id=%s AND event_type='restored'", (video,)) == 1,
            "unfinished_replay_does_not_duplicate_event")


def prepare_page(f, kind):
    repo = TypedInventoryRepository(f.database)
    claims = repo.claim_channels([CHANNEL], video_types=[kind], worker_id="unified-availability-proof",
        run_id=str(uuid4()), lease_seconds=90)
    f.check(len(claims) == 1, "inventory_scan_claim")
    owned = claims[0]
    scan = str(uuid4())
    repo.begin_scan(owned, scan_id=scan, scan_boundary_at=instant(f))
    observed = instant(f)
    def settle():
        answer = repo.settle_page(owned, scan_id=scan, transition_id=str(uuid4()), expected_page_token=None,
            next_page_token=None, page_status="complete",
            items=[dict(video_id=IDS[kind], published_at=(f.now - timedelta(days=200)).isoformat())],
            source_checked_at=observed)
        f.check(answer["status"] == "completed", "inventory_page_settled")
    return observed, settle


def page(f, kind):
    observed, settle = prepare_page(f, kind)
    settle()
    return observed


def return_evidence(f):
    seed(f, operation="metadata", episode_days=29)
    claim(f, operation="metadata")
    video = IDS["short"]
    stopped = state(f, video)
    page(f, "short")
    repeated = state(f, video)
    f.check(repeated["paused_reason"] == "availability_retry_expired"
        and not repeated["availability_resume_observed_at"], "repeated_present_id_does_not_resume")
    seed_absence_generation(f, "short")
    page(f, "short")
    returned = state(f, video)
    f.check(returned["availability_resume_observed_at"] and returned["paused_reason"] is None,
            "genuine_membership_return_requests_general")
    f.check(returned["availability_first_missing_at"] == stopped["availability_first_missing_at"],
            "membership_return_does_not_reset_episode")
    first = claim(f, kinds=("short",), operation="metadata")
    f.check(len(first["rows"]) == 1 and first["rows"][0].collection_mode != "counters", "returned_one_general_lease")
    f.check(claim(f, kinds=("short",), operation="metadata")["rows"] == [], "return_cannot_duplicate_live_lease")
    observed = instant(f)
    f.check(VideoCollectionRepository(f.database).complete_missing([missing_payload(first["rows"][0], observed)]) == 1,
            "still_missing_return_settled")
    after = state(f, video)
    f.check(after["paused_reason"] == "availability_retry_expired" and not after["availability_resume_observed_at"],
            "failed_return_goes_back_to_terminal_stop")

    # Real page settlement and real identity General completion in both commit
    # orders. Their source clocks, not the transaction order, decide coverage.
    for missing in (False, True):
        for completion_first in (False, True):
            f.reset()
            _, published = seed(f, operation="metadata", episode_days=14)
            seed_absence_generation(f, "short")
            owned = claim(f, kinds=("short",), operation="metadata")["rows"][0]
            f.check(owned.collection_mode == "identity_refresh", "return_race_owns_identity_lease")
            general_at = instant(f)
            evidence_at, settle = prepare_page(f, "short")
            row = payload(owned, general_at, missing=missing)
            if not missing:
                row.update(title="Synthetic returned ID", published_at=published.isoformat(), duration="PT5M",
                    tags=[], youtube_topic_categories=[], availability_status="public", privacy_status="public",
                    upload_status="processed")
            repo = VideoCollectionRepository(f.database)
            if completion_first:
                f.check(repo.complete_identity_refresh([row]) == 1, "return_race_general_first")
                settle()
            else:
                settle()
                during = state(f, video)
                f.check(str(during["lease_token"]) == str(owned.lease_token)
                    and during["availability_resume_observed_at"] == evidence_at,
                    "return_signal_keeps_live_lease")
                f.check(repo.complete_identity_refresh([row]) == 1, "return_race_page_first")
            pending = state(f, video)
            f.check(pending["availability_resume_observed_at"] == evidence_at
                and pending["next_due_at"] <= evidence_at and pending["paused_reason"] is None,
                "newer_return_survives_older_general_both_orders")
            f.check(repo.complete_identity_refresh([row]) == 1 and state(f, video) == pending,
                    "older_general_receipt_replay_cannot_consume_return")
            next_owned = claim(f, kinds=("short",), operation="metadata")["rows"][0]
            f.check(next_owned.collection_mode != "counters", "pending_return_is_general")
            newer = instant(f)
            if next_owned.collection_mode == "identity_refresh":
                completed = repo.complete_identity_refresh([payload(next_owned, newer, missing=True)])
            else:
                completed = repo.complete_missing([missing_payload(next_owned, newer)])
            f.check(completed == 1 and state(f, video)["availability_resume_observed_at"] is None,
                    "newer_general_consumes_return")
            page(f, "short")
            f.check(state(f, video)["availability_resume_observed_at"] is None,
                    "continued_presence_does_not_repeat_return")

    # A delayed playlist response observed before a successful General must not
    # undo that General's schedule, whether it found the ID or confirmed a miss.
    for missing in (False, True):
        f.reset()
        _, published = seed(f, operation="metadata", episode_days=14)
        seed_absence_generation(f, "short")
        owned = claim(f, kinds=("short",), operation="metadata")["rows"][0]
        _, settle = prepare_page(f, "short")
        general_at = instant(f)
        row = payload(owned, general_at, missing=missing)
        if not missing:
            row.update(title="Synthetic already returned", published_at=published.isoformat(), duration="PT5M",
                tags=[], youtube_topic_categories=[], availability_status="public", privacy_status="public",
                upload_status="processed")
        f.check(VideoCollectionRepository(f.database).complete_identity_refresh([row]) == 1,
                "later_general_before_delayed_page")
        before = state(f, video)
        settle()
        after = state(f, video)
        f.check(after["availability_resume_observed_at"] is None
            and after["next_due_at"] == before["next_due_at"], "stale_playlist_does_not_request_covered_general")


def retained_id_cleanup(f):
    seed(f, operation="metadata", episode_days=29)
    claim(f, operation="metadata")
    # Represent an installation after more than 29 days offline. The retained
    # source timestamps are real historical fixture instants, not renewed dates.
    old = f.now - timedelta(days=30)
    f.rows("""UPDATE public.videos SET checked_at=%s,metadata_checked_at=%s,availability_checked_at=%s""", (old, old, old))
    f.rows("""UPDATE public.video_inventory SET published_acquired_at=%s""", (old,))
    f.rows("""UPDATE public.video_inventory_memberships SET first_seen_at=%s,last_seen_at=%s,last_present_at=%s""", (old, old, old))
    f.rows("""UPDATE public.video_collection_state SET typed_collection_checked_at=%s,
        typed_source_published_acquired_at=CASE WHEN typed_source_published_at IS NOT NULL THEN %s END,
        typed_source_stream_end_acquired_at=CASE WHEN typed_stream_actual_end_at IS NOT NULL THEN %s END""", (old, old, old))
    first = {video: state(f, video)["availability_first_missing_at"] for video in IDS.values()}
    for target in ("video_counters", "video_metadata", "video_availability", "memberships", "inventory"):
        answer = f.cleanup(target, limit=100)
        f.check(answer.deferred == 0, "cleanup_not_deferred")
    for video in IDS.values():
        after = state(f, video)
        f.check(after["availability_first_missing_at"] == first[video]
            and after["paused_reason"] == "availability_retry_expired" and after["next_due_at"] is None,
            "retention_keeps_terminal_episode")
        f.check(f.scalar("SELECT count(*) FROM public.video_inventory WHERE video_id=%s", (video,)) == 1,
                "retained_identity_survives_api_cleanup")
    f.check(claim(f)["rows"] == [], "retention_does_not_resume_provider_work")


def rebuilt_episode_while_channel_held(f):
    from db.channel.channel_trash import ChannelTrashRepository
    from db.videos.video_queue_ensure import ensure_video_collection_state
    seed(f, operation="metadata")
    video = IDS["long"]
    first = f.now - timedelta(days=7)
    f.rows("""UPDATE public.videos SET availability_status='unknown_unavailable',privacy_status=NULL,
        upload_status=NULL WHERE video_id=%s""", (video,))
    f.rows("""INSERT INTO public.video_availability_incidents(video_id,channel_id,first_status,latest_status,
        first_seen_at,last_seen_at,source_first_seen,source_latest_seen)
        VALUES(%s,%s,'unknown_unavailable','unknown_unavailable',%s,%s,'video_stats_sync','video_stats_sync')""",
        (video, CHANNEL, first, f.now - timedelta(hours=1)))
    f.rows("DELETE FROM public.video_collection_state WHERE video_id=%s", (video,))
    f.update_channel({"initial_long_inventory_status": "skipped_no_long_playlist"}, CHANNEL)
    scopes = [dict(channel_id=CHANNEL, control_epoch=f.epoch(CHANNEL))]
    f.check(ensure_video_collection_state(f.database, [video], scopes) == 1, "rebuild_episode_after_terminal_empty_long")
    rebuilt = state(f, video)
    f.check(rebuilt["availability_first_missing_at"] == first and rebuilt["paused_reason"] is None
        and rebuilt["next_due_at"] is not None, "rebuilt_episode_keeps_calendar")
    f.update_channel({"initial_long_inventory_status": "completed"}, CHANNEL)
    trash = ChannelTrashRepository(f.database)
    hidden = trash.remove(CHANNEL)
    f.check(hidden["outcome"] == "removed", "channel_hidden_by_real_owner")
    # Make only the operational retry eligible; the API observations remain
    # unchanged. This simulates the future retry while the channel is still held.
    f.rows("UPDATE public.video_collection_state SET next_due_at=now()-interval '1 second' WHERE video_id=%s", (video,))
    due = state(f, video)["next_due_at"]
    f.check(claim(f, kinds=("long",), operation="metadata")["rows"] == [], "held_channel_no_provider_lease")
    held = state(f, video)
    f.check(held["next_due_at"] == due and held["paused_reason"] is None, "held_claim_preserves_episode_calendar")
    f.check(trash.restore(hidden["id"], hidden["removed_at"])["outcome"] == "restored", "channel_unhidden_by_real_owner")
    f.check(len(claim(f, kinds=("long",), operation="metadata")["rows"]) == 1, "released_channel_uses_existing_episode")


def legacy_empty_long_channel_mode(f):
    """Terminal empty Long is not a channel-wide hold; real holds still fence work."""
    from db.channel.channel_read_policy import CHANNEL_CONTROL_SQL

    for scenario, expected in (("unlinked", "active"), ("completed_long", "active"),
                               ("unfinished_all", "initial_inventory"), ("blocked_all", "operator_hold"),
                               ("manual", "operator_hold"), ("hidden", "hidden")):
        f.reset()
        seed(f)
        f.update_channel({"initial_long_inventory_status": "skipped_no_long_playlist"}, CHANNEL)
        if scenario in ("completed_long", "unfinished_all", "blocked_all"):
            job = str(uuid4())
            if scenario == "completed_long":
                # A successful old empty playlist may retain a previous error count.
                f.rows("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,phase,
                    initial_scope_version,initial_video_types,max_items,max_pages,max_quota_units,
                    accepted_epoch,error_count) VALUES(%s,'channel_initial_long_inventory',%s,
                    'completed','inventory',0,ARRAY['long'],2500,50,100,%s,1)""",
                    (job, CHANNEL, f.epoch(CHANNEL)))
            else:
                f.rows("""INSERT INTO public.operator_jobs(id,job_type,channel_id,status,phase,
                    initial_scope_version,initial_video_types,max_items,max_pages,max_quota_units,
                    accepted_epoch) VALUES(%s,'channel_initial_long_inventory',%s,%s,'inventory',
                    1,ARRAY['long','short','stream'],NULL,NULL,NULL,%s)""",
                    (job, CHANNEL, "blocked" if scenario == "blocked_all" else "queued", f.epoch(CHANNEL)))
            f.rows("UPDATE public.channel_collection_state SET initial_job_id=%s WHERE channel_id=%s", (job, CHANNEL))
        elif scenario == "manual":
            f.rows("UPDATE public.channel_collection_state SET owner_permission='manual_required' WHERE channel_id=%s", (CHANNEL,))
        elif scenario == "hidden":
            f.rows("UPDATE public.channel_collection_state SET is_hidden=true,hidden_at=now() WHERE channel_id=%s", (CHANNEL,))
        generated = f.rows("SELECT collection_mode FROM (" + CHANNEL_CONTROL_SQL + ") policy WHERE channel_id=%s", (CHANNEL,))[0]
        retained = f.rows("SELECT collection_mode FROM public.channel_collection_control WHERE channel_id=%s", (CHANNEL,))[0]
        f.check(generated["collection_mode"] == retained["collection_mode"] == expected,
                "empty_long_policy_sql_agrees_" + scenario)
        rows = claim(f, operation="counters")["rows"]
        f.check({row.video_type for row in rows} == (set(KINDS) if expected == "active" else set()),
                "empty_long_common_queue_admission_" + scenario)


CASES = (counters_all_types, common_feeder_all_types, missing_calendar, positive_general, initial_counterless_and_missing_handoff, no_type_proof,
         unfinished_stream_returns, return_evidence,
         retained_id_cleanup, rebuilt_episode_while_channel_held, legacy_empty_long_channel_mode)


def error_details(error):
    return dict(error_class=type(error).__name__,
        error_code=str(error) if isinstance(error, AssertionError) else None,
        database_code=getattr(error, "sqlstate", None) or getattr(error, "code", None),
        trace=[dict(file=Path(row.filename).name, line=row.lineno, function=row.name)
            for row in traceback.extract_tb(error.__traceback__)[-8:]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run-proof", action="store_true")
    modes.add_argument("--prepare-upgrade-seed", action="store_true")
    modes.add_argument("--verify-upgrade-seed", action="store_true")
    parser.add_argument("--installation", choices=("fresh", "upgrade27"), default="fresh")
    args = parser.parse_args()
    if args.prepare_upgrade_seed or args.verify_upgrade_seed:
        import psycopg
        from psycopg.rows import dict_row
        require(os.getenv("LIC_SCHEMA_CI_MAX_ROWS") == str(ROW_CAP), "explicit_row_budget")
        with psycopg.connect(dsns()[0], row_factory=dict_row, connect_timeout=5) as admin:
            admin.execute("SET LOCAL statement_timeout='20s'; SET LOCAL lock_timeout='250ms'")
            if args.prepare_upgrade_seed:
                found = admin.execute("SELECT count(*) AS n FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='video_collection_state' "
                    "AND column_name='availability_first_missing_at'").fetchone()
                require(found["n"] == 0, "prepare_only_before_schema28")
                admin.execute(UPGRADE_SEED_SQL)
                print(json.dumps(dict(status="seeded", channels=1, videos=5, youtube_requests=0)))
            else:
                print(json.dumps(verify_upgrade_seed(admin)))
        return 0
    if not args.run_proof:
        print(json.dumps(dict(native_executed=False, cases=[case.__name__ for case in CASES],
            max_rows=ROW_CAP, work_seconds=WORK_SECONDS, youtube_requests=0, installation=args.installation)))
        return 0
    require(os.getenv("LIC_SCHEMA_CI_MAX_ROWS") == str(ROW_CAP), "explicit_row_budget")
    out = Path("/out")
    require(out.is_dir(), "owned_output_required")
    report = dict(status="failed", installation=args.installation, fixture_restored=False, cases=[],
        youtube_requests=0, limitation="Native common feeder/service and DB proof; synthetic provider responses, no real network.")
    started = time.monotonic()
    try:
        with admission() as gate:
            f = Fixture(*dsns(), gate)
            try:
                f.check(f.scalar("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name='video_collection_state' AND column_name IN "
                    "('availability_first_missing_at','availability_schedule_type','availability_resume_observed_at')") == 3,
                    "schema28_episode_fields")
                for case in CASES:
                    before = f.checks
                    result = dict(name=case.__name__, status="failed")
                    try:
                        case(f)
                        result.update(status="passed", rows=f.observe())
                    except Exception as error:
                        result.update(error_details(error))
                    result["checks"] = f.checks - before
                    report["cases"].append(result)
                    # A failed case cannot contaminate the next one. Any reset
                    # failure stops the packet; the original global budget stays.
                    f.reset()
                    require(time.monotonic() < f.deadline, "suite_deadline")
            finally:
                f.close()
                report["fixture_restored"] = True
        report["status"] = "passed" if all(case["status"] == "passed" for case in report["cases"]) else "failed"
    except Exception as error:
        report.update(error_details(error))
    report["seconds"] = round(time.monotonic() - started, 6)
    (out / "RESULT.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(dict(status=report["status"], cases=len(report["cases"]), seconds=report["seconds"],
        error_class=report.get("error_class"), error_code=report.get("error_code"))))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
