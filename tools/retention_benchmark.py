"""Retention benchmark on a disposable database: one large channel, one full cleanup pass.

Seeds a fresh schema with synthetic channels, videos, upload-list memberships,
video and channel snapshots, statistics jobs with a target per video, collection
runs and API request records, then runs
the application's own cleanup cycle (HotCleanupCycle through the maintenance
repository) until one whole pass is done, twice. The first pass removes what the
scenario left expired; the second measures a pass over clean data.

Scenarios:
  steady    every source is within its refresh interval; nothing has expired.
  downtime  the server was off for --downtime-days: every clock is that much older,
            so the oldest snapshots, runs and some current values have expired.

Run it through tools/retention_benchmark.sh: disposable containers, no network.
It never calls YouTube and accepts only a disposable lic_direct_pg_probe_* database
(the same guard as tools/check_direct_pg_baseline.py). Output is one JSON report.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import check_direct_pg_baseline as safety  # noqa: E402

BIG_CHANNEL = "UC" + "B" * 22
SMALL_CHANNELS = tuple("UC" + letter * 22 for letter in "CDEF")
REFRESH = timedelta(days=21)          # the longest refresh interval (collection_schedule.MAX_INTERVAL_SECONDS)
SNAPSHOT_SPAN = timedelta(days=28)    # counters are refreshed often; history spans the window
HISTORY_DAYS = 35                     # channel snapshots, runs and API records: one per day/hour back this far
JOB_SCOPE = timedelta(hours=696)      # the operator job guards refuse new jobs older than this
JOB_GUARDS = (("public.operator_jobs", "hot_operator_source_scope"),
              ("public.operator_job_targets", "hot_operator_target_scope"))
STEP_PAUSE_SECONDS = 0.1              # the runtime loop waits this long between due cleanup steps
STEP_LIMIT = 50_000

# Every value is a bound parameter; each statement seeds one channel.
SEED_SQL = {
    # A publication date always comes with the time it was acquired (schema step 27).
    "inventory": """INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at,
          discovered_at,updated_at)
        SELECT %(prefix)s||lpad(g::text,9,'0'),%(channel)s,'long',%(at)s::timestamptz-g*interval '1 hour',
          %(at)s::timestamptz-%(offset)s::interval,%(at)s,%(at)s
        FROM generate_series(1,%(videos)s) g""",
    "memberships": """INSERT INTO public.video_inventory_memberships(video_id,channel_id,video_type,playlist_id,source_published_at,
        first_seen_at,last_seen_at,last_present_at)
        SELECT i.video_id,i.channel_id,'long','UULF'||substr(i.channel_id,3),i.published_at,s.seen,s.seen,s.seen
        FROM public.video_inventory i CROSS JOIN LATERAL (SELECT %(at)s::timestamptz-%(offset)s::interval
          -(abs(hashtext(i.video_id||'m'))%%%(refresh)s)*interval '1 second' AS seen) s
        WHERE i.channel_id=%(channel)s""",
    "videos": """INSERT INTO public.videos(video_id,channel_id,title,views,like_count,comment_count,published_at,duration,
        checked_at,metadata_checked_at,availability_status,availability_checked_at)
        SELECT i.video_id,i.channel_id,'Video '||i.video_id,1000+abs(hashtext(i.video_id))%%100000,10,1,i.published_at,'PT10M',
          s.counters,s.metadata,'public',s.counters
        FROM public.video_inventory i CROSS JOIN LATERAL (SELECT
          %(at)s::timestamptz-%(offset)s::interval-(abs(hashtext(i.video_id||'c'))%%%(refresh)s)*interval '1 second' AS counters,
          %(at)s::timestamptz-%(offset)s::interval-(abs(hashtext(i.video_id||'d'))%%%(refresh)s)*interval '1 second' AS metadata) s
        WHERE i.channel_id=%(channel)s""",
    # The collector's queue row per video, due again one refresh interval after its last check.
    "queue": """INSERT INTO public.video_collection_state(video_id,channel_id,video_type,profile_metadata_version,
          stats_source_checked_at,metadata_source_checked_at,last_success_at,stats_due_at,metadata_due_at,next_due_at)
        SELECT v.video_id,v.channel_id,'long',1,v.checked_at,v.metadata_checked_at,v.checked_at,
          v.checked_at+%(refresh)s*interval '1 second',v.metadata_checked_at+%(refresh)s*interval '1 second',
          least(v.checked_at,v.metadata_checked_at)+%(refresh)s*interval '1 second'
        FROM public.videos v WHERE v.channel_id=%(channel)s""",
    "video_snapshots": """INSERT INTO public.video_snapshots(video_id,views,like_count,comment_count,recorded_at)
        SELECT i.video_id,1000+k,10,1,%(at)s::timestamptz-%(offset)s::interval-k*(%(span)s::interval/%(snapshots)s)
        FROM public.video_inventory i CROSS JOIN generate_series(0,%(snapshots)s-1) k
        WHERE i.channel_id=%(channel)s""",
    # One finished statistics job per channel, with a target per video (20,000 for the large one).
    "operator_jobs": """WITH job AS (INSERT INTO public.operator_jobs(job_type,channel_id,status,requested_at,started_at,finished_at,
          max_items,max_quota_units,target_count,processed_count,updated_count,initial_scope_version,phase)
        VALUES ('channel_current_stats_backfill',%(channel)s,'completed',%(at)s::timestamptz-%(offset)s::interval-interval '1 hour',
          %(at)s::timestamptz-%(offset)s::interval-interval '1 hour',%(at)s::timestamptz-%(offset)s::interval,
          %(videos)s,%(videos)s,%(videos)s,%(videos)s,%(videos)s,0,'video_targets') RETURNING id)
        INSERT INTO public.operator_job_targets(job_id,video_id,status,job_attempts,completed_at,video_type)
        SELECT job.id,i.video_id,'updated',1,%(at)s::timestamptz-%(offset)s::interval,'long'
        FROM job CROSS JOIN public.video_inventory i WHERE i.channel_id=%(channel)s""",
    "channel_snapshots": """INSERT INTO public.channel_snapshots(channel_id,recorded_at,views,subscribers,video_count,source)
        SELECT %(channel)s,%(at)s::timestamptz-%(offset)s::interval-d*interval '1 day',1000000,1000,%(videos)s,
          'youtube_channel_metadata_refresh'
        FROM generate_series(0,%(history)s-1) d""",
}
SHARED_SQL = {
    "sync_runs": """INSERT INTO public.sync_runs(status,started_at,finished_at,duration_seconds)
        SELECT 'completed',s.at,s.at+interval '1 minute',60
        FROM generate_series(0,%(history)s*24-1) h
        CROSS JOIN LATERAL (SELECT %(at)s::timestamptz-%(offset)s::interval-h*interval '1 hour' AS at) s""",
    "quota_attempts": """INSERT INTO public.youtube_api_request_attempts(project_id,quota_bucket,quota_day,phase,endpoint,
        quota_units,status,started_at,finished_at,duration_ms,http_status)
        SELECT 'primary',%(bucket)s,(s.at AT TIME ZONE 'America/Los_Angeles')::date,%(phase)s,%(endpoint)s,1,'success',
          s.at,s.at+interval '1 second',1000,200
        FROM generate_series(0,%(history)s*100-1) n
        CROSS JOIN LATERAL (SELECT %(at)s::timestamptz-%(offset)s::interval-n*interval '864 seconds' AS at) s""",
}
# Labels the collector really records (operational_copy_policy.QUOTA_LABELS); others are normalized by cleanup.
QUOTA_SEED = {"bucket": "general", "phase": "video_collection", "endpoint": "videos"}
COUNTED = ("public.channels", "public.video_inventory", "public.video_inventory_memberships", "public.videos",
           "public.video_collection_state",
           "public.video_snapshots", "public.channel_snapshots", "public.sync_runs", "public.youtube_api_request_attempts",
           "public.operator_jobs", "public.operator_job_targets", "public.youtube_category_reference")


def category_reference_parameters(at, offset):
    """One reference refreshed at the longest supported interval before downtime."""
    received = at - offset - REFRESH
    document = {"schema_version": 1, "clock_origin": "acquisition_started_at",
                "retrieved_at": received.isoformat(),
                "source": {"languages_url": "https://www.googleapis.com/youtube/v3/i18nLanguages",
                           "categories_url": "https://www.googleapis.com/youtube/v3/videoCategories"},
                "languages": {"en": "English"}, "categories": {"en": {"10": "Music"}}}
    return {"received": received, "document": json.dumps(document)}


def channels(big_videos, small_videos):
    return [(BIG_CHANNEL, "B" + "B", big_videos)] + [(channel, "B" + channel[2], small_videos) for channel in SMALL_CHANNELS]


def seed_parameters(channel, prefix, videos, *, at, offset, snapshots):
    return {"channel": channel, "prefix": prefix, "videos": videos, "at": at, "offset": offset,
            "refresh": int(REFRESH.total_seconds()), "span": SNAPSHOT_SPAN, "snapshots": snapshots,
            "history": HISTORY_DAYS}


def seed(admin, database, *, big_videos, small_videos, snapshots, offset):
    from db.channel.channel_control import create_channel_for_collection
    admin.execute("SET statement_timeout='300s'")  # bulk seeding of a disposable database
    at = admin.scalar("SELECT clock_timestamp()")
    # Jobs of a downtime longer than the job scope were written before it. Only
    # their two age guards are off while the channels are seeded.
    guards = JOB_GUARDS if offset + timedelta(hours=1) > JOB_SCOPE else ()
    for table, trigger in guards:
        admin.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    try:
        for channel, prefix, videos in channels(big_videos, small_videos):
            create_channel_for_collection(database, {
                "channel_id": channel, "title": "Benchmark " + channel[2], "initial_long_inventory_status": "completed",
                "initial_28d_stats_status": "completed"}, "accepted")
            with admin.transaction():
                admin.execute("UPDATE public.channels SET user_input=%s,channel_metadata_status='ok',channel_metadata_checked_at=%s"
                              " WHERE channel_id=%s", ("https://www.youtube.com/channel/" + channel, at - offset, channel))
                values = seed_parameters(channel, prefix, videos, at=at, offset=offset, snapshots=snapshots)
                for statement in SEED_SQL.values():
                    admin.execute(statement, values)
    finally:
        for table, trigger in guards:
            admin.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
    with admin.transaction():
        for statement in SHARED_SQL.values():
            admin.execute(statement, {"at": at, "offset": offset, "history": HISTORY_DAYS, **QUOTA_SEED})
        admin.execute("INSERT INTO public.youtube_category_reference(retrieved_at,document) "
                      "VALUES (%(received)s,%(document)s::jsonb)", category_reference_parameters(at, offset))
    admin.execute("ANALYZE")
    return {table: admin.scalar(f"SELECT count(*) FROM {table}") for table in COUNTED}


def rows_read(admin):
    # Cumulative rows PostgreSQL returned to scans of each user table: sequential plus index fetches.
    admin.execute("SELECT pg_stat_clear_snapshot()")
    rows = admin.execute("SELECT relname AS name,(seq_tup_read+coalesce(idx_tup_fetch,0))::bigint AS value FROM pg_stat_user_tables")
    return {row["name"]: row["value"] for row in rows}


def read_delta(before, after):
    delta = {name: after[name] - before.get(name, 0) for name in after if after[name] - before.get(name, 0)}
    return sum(delta.values()), dict(sorted(delta.items(), key=lambda item: -item[1]))


class TimedRepository:
    """The maintenance repository, timing each cleanup page by its target."""

    def __init__(self, repository):
        self.repository, self.seconds = repository, {}

    def cleanup_page(self, target, **kwargs):
        started = time.perf_counter()
        try:
            return self.repository.cleanup_page(target, **kwargs)
        finally:
            self.seconds[target] = self.seconds.get(target, 0.0) + time.perf_counter() - started

    def __getattr__(self, name):
        return getattr(self.repository, name)


def full_pass(repository, *, cycle_factory=None, limit=STEP_LIMIT):
    """One whole cleanup pass, one page per step, as the runtime controller runs it."""
    if cycle_factory is None:
        from hot_retention import HotCleanupCycle as cycle_factory
    timed = TimedRepository(repository)
    cycle = cycle_factory(timed)
    targets, steps, slowest = {}, 0, 0.0
    started = time.perf_counter()
    while steps < limit:
        step_started = time.perf_counter()
        summary = cycle.run_slice(max_batches=1, max_seconds=10)
        slowest = max(slowest, time.perf_counter() - step_started)
        steps += 1
        if summary["status"] == "error":
            raise RuntimeError("cleanup_failed:" + str(summary.get("failed_target")))
        for page in summary["pages"]:
            totals = targets.setdefault(page["target"], {"pages": 0, "scanned": 0, "deleted": 0, "scrubbed": 0, "deferred": 0})
            totals["pages"] += 1
            for key in ("scanned", "deleted", "scrubbed", "deferred"):
                totals[key] += page[key]
        if summary["status"] == "scan_exhausted":
            break
    else:
        raise RuntimeError("cleanup_pass_too_long")
    seconds = time.perf_counter() - started
    return {"steps": steps, "seconds": round(seconds, 3), "slowest_step_seconds": round(slowest, 3),
            # The runtime loop pauses between due steps; this is the wait a restart would see.
            "runtime_estimate_seconds": round(seconds + steps * STEP_PAUSE_SECONDS, 1),
            # Where the time of a slow pass goes: the seconds of each target's pages.
            "targets": {name: dict(totals, seconds=round(timed.seconds.get(name, 0.0), 3))
                        for name, totals in sorted(targets.items())}}


def run(args, environ=os.environ):
    admin_dsn, runtime_dsn = safety.validate_configuration(environ.get("LIC_DIRECT_PG_PROBE_ADMIN_DSN", ""),
                                                           environ.get("LIC_DIRECT_PG_PROBE_DSN", ""))
    sys.path.insert(0, str(ROOT / "application"))
    import check_direct_pg_repositories as fixtures
    from db.hot.hot_runtime import MaintenanceRepository
    offset = timedelta(days=args.downtime_days if args.scenario == "downtime" else 0)
    admin = safety.connect(admin_dsn, {"client_execute_calls": 0, "client_execute_seconds": 0.0})
    try:
        with fixtures.self_hosted_profile(), fixtures.open_database(runtime_dsn) as database:
            seeded_started = time.perf_counter()
            counts = seed(admin, database, big_videos=args.videos, small_videos=args.small_videos,
                          snapshots=args.snapshots, offset=offset)
            seeding = round(time.perf_counter() - seeded_started, 1)
            repository = MaintenanceRepository(database)
            passes = []
            for _ in range(2):
                before = rows_read(admin)
                result = full_pass(repository)
                time.sleep(11)  # an idle backend flushes its table statistics within 10 seconds
                result["rows_read"], result["rows_read_by_table"] = read_delta(before, rows_read(admin))
                passes.append(result)
    finally:
        admin.connection.close()
    return {"scenario": args.scenario, "downtime_days": offset.days, "seed_seconds": seeding,
            "rows": counts, "first_pass": passes[0], "clean_pass": passes[1]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=("steady", "downtime"), default="steady")
    parser.add_argument("--downtime-days", type=int, default=10)
    parser.add_argument("--videos", type=int, default=20_000, help="videos of the large channel")
    parser.add_argument("--small-videos", type=int, default=800, help="videos of each of four small channels")
    parser.add_argument("--snapshots", type=int, default=6, help="counter snapshots per video")
    args = parser.parse_args(argv)
    if not (1 <= args.videos <= 100_000 and 0 <= args.small_videos <= 20_000 and 1 <= args.snapshots <= 50
            and 1 <= args.downtime_days <= 60):
        parser.error("sizes out of range")
    print(json.dumps(run(args), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
