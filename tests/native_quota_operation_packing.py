"""Focused QUOTA-03 native proof using the real queue, service and coordinator.

Only disposable PostgreSQL and deterministic fake provider callbacks. The same
fixture runs against baseline and candidate source trees. A callback is one
physical provider request; this does not claim real YouTube network performance.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from uuid import uuid4
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import argparse
import hashlib
import json
import os
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "application"), str(ROOT / "tests")]
from schema25_regressions.fixtures import Fixture, admission, dsns
from collection.auto_pass import AutoPassState, CommonCandidateCursor
from collection.common_feeder import CommonAutoFeeder
from collection.cycle import AutomaticDispatchWindow
from collection.context import CollectionPolicy, CollectionSources
from collection.settings import CollectionSettings
from collection.inventory_feeder import AutoInventoryCursor
from collection.metadata_feeder import AutoMetadataCursor
from collection.video_payloads import build_video_counter_payload, build_video_collection_metadata_payload
from collection_shutdown import encode_cursor_checkpoint, decode_cursor_checkpoint
from runtime.process_collection import _capture_collection_cursors, _restore_collection_cursors
from db.videos.video_collection import VideoCollectionRepository
from video_collection_service import YouTubeViewCountAbsent, YouTubeViewCountInvalid
from runtime.collection import bind_collection_context, video_collection_dependencies
from youtube_video_profiles import fetch_video_metadata
from youtube_errors import YouTubeAPIError

CONCURRENCY = 8
ROW_CAP = 810000
WORK_SECONDS = 1500
TOTAL = 199987


def require(ok, code):
    if not ok:
        raise AssertionError(code)


def emit(**row):
    print(json.dumps(row, sort_keys=True), flush=True)


def channel(index):
    return "UC" + str(index).zfill(22)


def vid(index):
    return "q" + str(index).zfill(10)


def number(video):
    require(isinstance(video, str) and len(video) == 11 and video[0] == "q", "video_shape")
    return int(video[1:])


def operation(index, mode):
    if mode == "general":
        return "general"
    if mode == "stats":
        return "stats"
    # 14% General mixed throughout the physical queue, approximating observed 7/50.
    return "general" if index % 50 < 7 else "stats"


def seed(f, total, channels, mode):
    require(1 <= channels <= 500 and 1 <= total <= TOTAL, "fixture_bound")
    for index in range(1, channels + 1):
        f.channel(channel(index))
    old, published = f.now - timedelta(days=2), f.now - timedelta(days=365)
    due, future = f.now - timedelta(hours=2), f.now + timedelta(days=2)
    # Source dates are anchored to this fixture, not wall-clock synthetic years.
    cte = """WITH input AS (
      SELECT n, 'q'||lpad(n::text,10,'0') AS video_id,
        'UC'||lpad((1+(n-1)%%%(channels)s)::text,22,'0') AS channel_id,
        %(due)s::timestamptz + make_interval(secs => n%%1800) AS due,
        CASE WHEN %(mode)s='general' OR (%(mode)s='mixed' AND n%%50<7)
             THEN %(due)s::timestamptz + make_interval(secs => n%%1800)
             ELSE %(future)s::timestamptz END AS metadata_due
      FROM generate_series(%(start)s::integer,%(end)s::integer) n) """
    for start in range(1, total + 1, 10000):
        args = dict(start=start, end=min(start + 9999, total), channels=channels,
                    mode=mode, due=due, future=future, old=old, published=published)
        with f.admin.transaction():
            # Schema 27: a publication date always comes with when it was acquired.
            f.rows(cte + """INSERT INTO public.video_inventory(video_id,channel_id,video_type,published_at,published_acquired_at)
                SELECT video_id,channel_id,'long',%(published)s,%(old)s FROM input""", args)
            f.rows(cte + """INSERT INTO public.videos(video_id,channel_id,published_at,checked_at,metadata_checked_at,
                availability_checked_at,availability_status,privacy_status,upload_status,views,like_count,comment_count)
                SELECT video_id,channel_id,%(published)s,%(old)s,%(old)s,%(old)s,
                  'public','public','processed',1,1,0 FROM input""", args)
            f.rows(cte + """INSERT INTO public.video_collection_state(video_id,channel_id,video_type,
                hydration_needed,profile_metadata_version,stats_due_at,metadata_due_at,next_due_at,
                stats_source_checked_at,metadata_source_checked_at)
                SELECT video_id,channel_id,'long',false,1,due,metadata_due,due,%(old)s,%(old)s FROM input""", args)
        require(time.monotonic() < f.deadline, "seed_deadline")
        if total > 1000 and (args["end"] % 50000 == 0 or args["end"] == total):
            emit(stage="seed", videos=args["end"], total=total)
    for table in ("channels", "channel_collection_state", "video_inventory", "videos", "video_collection_state"):
        f.rows("ANALYZE public." + table)
    require(f.scalar("SELECT count(*) FROM public.video_collection_state") == total, "seed_count")
    return dict(rows=f.observe(), old=old.isoformat(), future=future.isoformat(), published=published.isoformat())


def occupancy(rows, begin, end):
    if end <= begin:
        return dict(seconds=0, average_active=0, peak=0, occupancy_seconds=[0] * 9)
    events = sorted([(max(begin, row["start"]), 1) for row in rows
                     if row["start"] < end and row["end"] > begin] +
                    [(min(end, row["end"]), -1) for row in rows
                     if row["start"] < end and row["end"] > begin])
    active = peak = 0
    previous = begin
    bins = [0.0] * 9
    for at, change in events:
        require(0 <= active <= CONCURRENCY, "occupancy_bound")
        bins[active] += at - previous
        active += change
        peak = max(peak, active)
        previous = at
    bins[active] += end - previous
    require(active == 0, "provider_drained")
    return dict(seconds=round(end - begin, 6), average_active=round(
        sum(index * seconds for index, seconds in enumerate(bins)) / (end - begin), 6),
        peak=peak, occupancy_seconds=[round(value, 6) for value in bins])


class Metrics:
    def __init__(self, total, channels, mode, variant, published, *, scale=False, missing=()):
        self.total, self.channels, self.mode, self.variant = total, channels, mode, variant
        self.published, self.scale, self.missing = published, scale, set(missing)
        self.lock = Lock()
        # The trace clock: on Windows monotonic() ticks every 15.6 ms, longer than a
        # 5 ms provider call, and a zero-length interval breaks occupancy().
        self.started = time.perf_counter()
        self.active = self.peak = 0
        self.claimed = bytearray(total + 1)
        self.completed = bytearray(total + 1)
        self.requested = {"general": bytearray(total + 1), "stats": bytearray(total + 1)}
        self.claimed_count = self.completed_count = 0
        self.trace, self.claim_trace, self.pass_trace = [], [], []
        self.db_seconds = Counter()
        self.db_calls = Counter()
        self.max_ready = self.max_held = self.max_raw = 0

    def provider(self, route, ids, reason):
        ids = tuple(ids)
        require(1 <= len(ids) <= 50 and len(ids) == len(set(ids)), "request_size")
        with self.lock:
            for video in ids:
                index = number(video)
                require(1 <= index <= self.total and self.claimed[index] == 1, "request_has_lease")
                require(not self.requested[route][index], "duplicate_physical_route")
                self.requested[route][index] = 1
                if route == "general":
                    require(operation(index, self.mode) == "general", "future_metadata_requested")
            seq = len(self.trace) + 1
            # Fixed, disclosed sequence: use the same profile for each version.
            delay = (1.5 if seq <= 8 else (.1, .3, .6)[(seq - 9) % 3]) if self.scale else .005
            row = dict(sequence=seq, route=route, start=time.perf_counter() - self.started,
                       end=None, size=len(ids), ids=list(ids), delay=delay,
                       channels=len({1 + (number(video) - 1) % self.channels for video in ids}))
            self.trace.append(row)
            self.active += 1
            self.peak = max(self.peak, self.active)
            require(self.active <= CONCURRENCY, "shared_provider_cap")
        try:
            time.sleep(delay)
            if route == "stats":
                return dict(items=[dict(id=video, views=1000000000 + number(video),
                    like_count=number(video) % 1000, comment_count=number(video) % 17) for video in ids],
                    summary=dict(failed_ids=[]))
            result = []
            for video in ids:
                index = number(video)
                item = dict(id=video, snippet=dict(channelId=channel(1 + (index - 1) % self.channels),
                    title="Synthetic video " + str(index), publishedAt=self.published,
                    liveBroadcastContent="none", tags=["synthetic"], categoryId="22"),
                    contentDetails=dict(duration="PT10M", caption="false"),
                    status=dict(privacyStatus="public", uploadStatus="processed"))
                if self.variant == "candidate":
                    item["statistics"] = dict(likeCount=str(index % 1000), commentCount=str(index % 17))
                    if index not in self.missing:
                        item["statistics"]["viewCount"] = str(1000000000 + index)
                result.append(item)
            return result
        finally:
            with self.lock:
                row["end"] = time.perf_counter() - self.started
                self.active -= 1

    def aggregate(self):
        elapsed = self.collection_end
        require(all(row["end"] is not None for row in self.trace), "unfinished_request")
        route_metrics = {}
        for route in ("general", "stats"):
            rows = [row for row in self.trace if row["route"] == route]
            count, items = len(rows), sum(row["size"] for row in rows)
            route_metrics[route] = dict(requests=count, ids=items,
                full_requests=sum(row["size"] == 50 for row in rows),
                fill_percent=round(100 * items / (50 * count), 5) if count else None,
                batch_size_histogram=dict(Counter(row["size"] for row in rows)))
        first = min((row["start"] for row in self.trace), default=0)
        last = max((row["end"] for row in self.trace), default=0)
        # Remove the artificial first-eight warm-up and final eight requests.
        steady_start = max(row["end"] for row in self.trace[:8]) if len(self.trace) > 24 else first
        steady_end = self.trace[-8]["start"] if len(self.trace) > 24 else last
        return dict(physical_requests=len(self.trace), routes=route_metrics,
            whole_pass_window=occupancy(self.trace, 0, elapsed),
            provider_window=occupancy(self.trace, first, last),
            steady_window=occupancy(self.trace, steady_start, steady_end),
            db_seconds={key:round(value,6) for key,value in self.db_seconds.items()},
            db_calls=dict(self.db_calls), max_ready=self.max_ready, max_held=self.max_held,
            raw_window_max=self.max_raw, claimed=self.claimed_count, completed=self.completed_count,
            target_average_reached=occupancy(self.trace, steady_start, steady_end)["average_active"] >= 6)


class Repository(VideoCollectionRepository):
    def __init__(self, database, metrics):
        super().__init__(database)
        self.m = metrics

    def _timed(self, label, action, *args, **kwargs):
        begin = time.monotonic()
        try:
            return action(*args, **kwargs)
        finally:
            self.m.db_seconds[label] += time.monotonic() - begin
            self.m.db_calls[label] += 1

    def due_candidate_page(self, **kwargs):
        return self._timed("candidate", super().due_candidate_page, **kwargs)

    def claim_due_batch(self, **kwargs):
        result = self._timed("claim", super().claim_due_batch, **kwargs)
        self.m.max_raw = max(self.m.max_raw, result["inspected_count"])
        require(result["inspected_count"] <= 1000, "raw_window_bound")
        row_ids = []
        for row in result["rows"]:
            index = number(row.video_id)
            expected = operation(index, self.m.mode)
            require(row.channel_id == channel(1 + (index - 1) % self.m.channels), "claim_owner")
            require(row.collection_mode == ("metadata" if expected == "general" else "counters"), "claim_operation")
            require(self.m.claimed[index] == 0, "duplicate_claim")
            self.m.claimed[index] = 1
            self.m.claimed_count += 1
            row_ids.append(index)
        if row_ids:
            operations = {operation(index, self.m.mode) for index in row_ids}
            if self.m.variant == "candidate":
                require(len(operations) == 1, "candidate_mixed_operation_claim")
            self.m.claim_trace.append(dict(ids=row_ids, operations=sorted(operations),
                first_due=min(index % 1800 for index in row_ids), inspected=result["inspected_count"]))
        return result

    def _completed(self, rows):
        for row in rows:
            index = number(row["video_id"])
            require(not self.m.completed[index], "duplicate_completion")
            require(self.m.requested["stats"][index] or self.m.requested["general"][index], "complete_unfetched")
            self.m.completed[index] = 1
            self.m.completed_count += 1

    def complete_counters(self, rows):
        result = self._timed("complete_counters", super().complete_counters, rows)
        self._completed(rows)
        return result

    def complete_metadata(self, rows):
        result = self._timed("complete_metadata", super().complete_metadata, rows)
        self._completed(rows)
        return result


def cursors():
    return CommonCandidateCursor(), AutoMetadataCursor(), AutoInventoryCursor()


def build_service(f, m):
    repo = Repository(f.database, m)
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    builder = partial(build_video_collection_metadata_payload, parse_dt=parse,
        view_count_absent_error=YouTubeViewCountAbsent, view_count_invalid_error=YouTubeViewCountInvalid)
    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated_phase_called")
    def general_http(endpoint, **kwargs):
        require(endpoint == "videos" and kwargs["quota_bucket"] == "general", "general_wire_route")
        parts = set(kwargs["params"]["part"].split(","))
        combined = m.variant == "candidate"
        require(("statistics" in parts) == combined, "real_runtime_statistics_part")
        require(bool(kwargs.get("complete_profile")) == combined, "real_runtime_complete_profile")
        items = m.provider("general", kwargs["params"]["id"].split(","), kwargs["phase"])
        return SimpleNamespace(status_code=200, headers={}, json=lambda:dict(items=items),
                               raise_for_status=lambda:None)
    def stats_http(ids, **kwargs):
        return m.provider("stats", ids, kwargs["phase"])
    dependencies = video_collection_dependencies(
        database=f.database, settings=CollectionSettings(True, True, True), api_error=YouTubeAPIError,
        get_video_metadata=partial(fetch_video_metadata, youtube_api_get=general_http,
            api_key="synthetic-only-no-real-key", attempt_recorder=None, timeout_seconds=5,
            emit_event=lambda *args, **kwargs:None, perf_counter=time.perf_counter),
        fetch_video_batch_stats=stats_http, api_key="synthetic-only-no-real-key",
        attempt_recorder=None, timeout_seconds=5, counter_parts=("viewCount", "likeCount", "commentCount"),
        build_full_payload=builder, build_counter_payload=build_video_counter_payload,
        is_quota_exhausted=lambda error:False)
    context = bind_collection_context(**dependencies,
        policy=CollectionPolicy(False, False, False, 24),
        sources=CollectionSources(parse, forbidden, forbidden, forbidden, lambda error:False),
        fresh_channel_repository=forbidden, supervised_environ=lambda:{},
        video_worker_id=lambda:"quota03_native", discovery_worker_id=lambda:"quota03_unused",
        enqueue_initial_inventory=forbidden)
    original_repo, service = context.video.build_service()
    require(original_repo.database is f.database, "context_database_identity")
    # Observation subclass calls the same repository owners; only records timings.
    service.repository = repo
    return service


def drive(f, m, *, pass_limit):
    service = build_service(f, m)
    positions, empty_passes = cursors(), 0
    class EmptyOperator:
        # There are no operator jobs in this fixture. The actual scheduler still
        # gives this producer its normal turns and wake/resume calls.
        def __init__(self, **kwargs):
            pass
        def advance(self, turn=None, *, ready_limit=0):
            pass
        def resume_idle(self):
            pass
        def stop(self, reason, **flags):
            pass
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        for pass_index in range(100):
            if m.completed_count == m.total:
                break
            require(time.monotonic() < f.deadline, "work_deadline")
            target = min(pass_limit, m.total - m.completed_count)
            state = AutoPassState(target, concurrency=CONCURRENCY)
            window = AutomaticDispatchWindow(concurrency=CONCURRENCY,
                require_ready=f.gate.require_ready, should_stop=lambda:time.monotonic()>=f.deadline,
                operator_factory=EmptyOperator, executor=executor)
            feeder = CommonAutoFeeder(service, state=state, cursor=positions[0],
                worker_id="quota03_native", require_ready=window.ready, lease_seconds=300,
                should_stop=window.stopping, on_settled=window.stop_all)
            before, begin = m.completed_count, time.monotonic()
            def progress(summary):
                snapshot = window.coordinator.snapshot()
                m.max_ready, m.max_held = max(m.max_ready, snapshot.ready), max(m.max_held, snapshot.held)
                require(snapshot.ready <= 16 and snapshot.held <= 8, "coordinator_bounds")
                require(state.inspected < state.scan_limit + 1000, "scan_bound")
            try:
                window.run_phase(feeder, progress_callback=progress)
            finally:
                window.close()
            require(window.checkpoint_ready and not window.stop_reason, "actual_window_drained")
            updated = m.completed_count - before
            empty_passes = empty_passes + 1 if not updated else 0
            require(empty_passes < 3, "three_empty_passes")
            require(feeder.summary["updated"] == updated and updated <= target, "ack_accounting")
            require(not feeder.summary["infrastructure_error"] and not feeder.summary["settlement_unknown"], "known_settlement")
            require(not feeder.summary["errors"], "no_provider_or_build_errors")
            captured = _capture_collection_cursors(positions)
            encoded = encode_cursor_checkpoint(captured)
            positions = cursors()
            _restore_collection_cursors(positions, decode_cursor_checkpoint(encoded))
            require(_capture_collection_cursors(positions) == captured, "cursor_roundtrip")
            report = dict(pass_index=pass_index + 1, updated=updated, total=m.completed_count,
                requests=len(m.trace), seconds=round(time.monotonic() - begin, 6),
                checkpoint_bytes=len(encoded), raw_inspected=state.inspected,
                summary={key:feeder.summary[key] for key in ("metadata_requests", "batch_requests", "metadata_refresh_requested", "updated")})
            m.pass_trace.append(report)
            emit(stage="pass", **report)
        else:
            raise AssertionError("too_many_passes")
    m.collection_end = time.perf_counter() - m.started
    require(sum(m.claimed) == sum(m.completed) == m.total, "all_ids_once")
    require(m.active == 0, "provider_not_drained")
    expected_general = sum(operation(index, m.mode) == "general" for index in range(1, m.total + 1))
    require(sum(m.requested["general"]) == expected_general, "general_id_count")
    expected_stats = (m.total if m.variant == "baseline" else m.total - expected_general + len(m.missing))
    require(sum(m.requested["stats"]) == expected_stats, "stats_id_count")
    require(f.scalar("SELECT count(*) FROM public.video_snapshots") == m.total, "snapshot_count")
    require(f.scalar("""SELECT count(*) FROM public.video_collection_state WHERE lease_token IS NULL
        AND attempt_count=1 AND next_due_at>clock_timestamp() AND stats_source_checked_at IS NOT NULL""") == m.total, "released_scheduled_once")
    require(f.scalar("""SELECT count(*) FROM public.videos WHERE views=1000000000+substring(video_id FROM 2)::bigint
        AND like_count=substring(video_id FROM 2)::bigint%1000 AND comment_count=substring(video_id FROM 2)::bigint%17""") == m.total, "same_source_values")
    require(f.scalar("""SELECT count(*) FROM public.video_collection_state
        WHERE next_due_at=least(stats_due_at,metadata_due_at)""") == m.total, "next_due_exact_minimum")
    counter_ids = [vid(index) for index in range(1,m.total+1) if operation(index,m.mode)=="stats"]
    if counter_ids:
        require(f.scalar("""SELECT count(*) FROM public.video_collection_state state
            JOIN public.videos video USING(video_id) WHERE state.video_id=ANY(%s::text[])
            AND state.metadata_source_checked_at=%s::timestamptz AND state.metadata_due_at=%s::timestamptz
            AND video.metadata_checked_at=%s::timestamptz""",
            (counter_ids,m.seed_old,m.seed_future,m.seed_old)) == len(counter_ids), "counter_only_metadata_clocks_untouched")
    # Compare effective schedule intervals, never timestamps from different wall-clock runs.
    rows = f.rows("""SELECT state.video_id,state.channel_id,state.video_type,
        video.views,video.like_count,video.comment_count,video.title,video.duration,
        state.attempt_count,state.hydration_needed,state.profile_metadata_version,
        EXTRACT(EPOCH FROM state.stats_due_at-state.stats_source_checked_at)::bigint AS stats_interval,
        CASE WHEN state.metadata_source_checked_at>%(old)s::timestamptz
             THEN EXTRACT(EPOCH FROM state.metadata_due_at-state.metadata_source_checked_at)::bigint ELSE NULL END AS metadata_interval,
        state.lease_token IS NULL AS lease_released,
        state.next_due_at=least(state.stats_due_at,state.metadata_due_at) AS next_due_exact,
        CASE WHEN state.metadata_source_checked_at=%(old)s::timestamptz THEN 'unchanged'
             ELSE 'refreshed' END AS metadata_clock_kind
      FROM public.video_collection_state state JOIN public.videos video USING(video_id)
      ORDER BY state.video_id""", dict(old=m.seed_old))
    normalized = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    result = dict(videos=m.total, general_ids=expected_general, stats_ids=expected_stats,
        state_sha256=hashlib.sha256(normalized.encode()).hexdigest(), rows=f.observe(),
        schedule_histogram=dict(Counter(str((row["stats_interval"], row["metadata_interval"])) for row in rows)),
        all_claims_single_operation=all(len(row["operations"]) == 1 for row in m.claim_trace),
        claim_sizes=dict(Counter(len(row["ids"]) for row in m.claim_trace)))
    return result


def run_case(f, out, variant, name, total, channels, mode, *, scale=False, missing=(), pass_limit=10000):
    seed_started = time.monotonic()
    seeded = seed(f, total, channels, mode)
    seed_seconds = time.monotonic() - seed_started
    m = Metrics(total, channels, mode, variant, seeded["published"], scale=scale, missing=missing)
    m.seed_old, m.seed_future = seeded["old"], seeded["future"]
    try:
        result = drive(f, m, pass_limit=pass_limit)
    finally:
        # Keep bounded synthetic evidence even if a native assertion fails.
        (out / (name + "-trace.jsonl")).write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in m.trace))
        (out / (name + "-claims.jsonl")).write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in m.claim_trace))
    metrics = m.aggregate()
    if scale:
        require(len(m.trace) >= 4000, "representative_request_floor")
    if variant == "candidate" and mode in ("general", "stats") and channels == 1:
        expected = Counter([50] * (total // 50) + ([total % 50] if total % 50 else []))
        require(Counter(row["size"] for row in m.trace if row["route"] == mode) == expected, "single_channel_exact_remainder")
    if variant == "candidate" and missing:
        actual = {number(video) for row in m.trace if row["route"] == "stats" for video in row["ids"]}
        require(actual == set(missing), "fallback_exact_missing_ids")
    if variant == "candidate":
        general_indices = [index for index in range(1, total + 1) if operation(index, mode) == "general"]
        healthy = [vid(index) for index in general_indices if index not in set(missing)]
        if healthy:
            require(f.scalar("""SELECT count(*) FROM public.video_collection_state
                WHERE video_id=ANY(%s::text[]) AND stats_source_checked_at=metadata_source_checked_at""",
                (healthy,)) == len(healthy), "general_source_clock_shared")
        if missing:
            require(f.scalar("""SELECT count(*) FROM public.video_collection_state
                WHERE video_id=ANY(%s::text[]) AND stats_source_checked_at>metadata_source_checked_at""",
                ([vid(index) for index in missing],)) == len(missing), "fallback_source_clock_distinct")
    result.update(name=name, status="passed", seed_seconds=round(seed_seconds, 6), metrics=metrics,
                  passes=m.pass_trace, pass_limit=pass_limit)
    (out / (name + ".json")).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    emit(stage="case", name=name, status="passed", videos=total, requests=len(m.trace),
         peak=metrics["provider_window"]["peak"], average=metrics["provider_window"]["average_active"])
    f.reset()
    return result



def native_guards(f):
    """Reuse acknowledged QUOTA-02 negative tests, then operation-specific cases."""
    from native_quota_batch_packing import locked_row, claim_race, ack_loss, fair_refill
    results = []
    for name, action in (
        ("locked_row", locked_row), ("claim_race", claim_race), ("small_large_fairness", fair_refill),
        ("claim_ack_loss", lambda db:ack_loss(db, "claim")),
        ("completion_ack_loss", lambda db:ack_loss(db, "completion")),
    ):
        f.reset()
        details = action(f)
        results.append(dict(name=name, status="passed", **details))
    f.reset()
    seed(f, 4, 1, "general")
    f.rows("""UPDATE public.video_collection_state SET metadata_due_at=clock_timestamp()+interval '2 days'
              WHERE video_id=ANY(%s::text[])""", ([vid(1), vid(3)],))
    repo = VideoCollectionRepository(f.database)
    scope = [dict(channel_id=channel(1), video_type="long")]
    before = f.rows("""SELECT video_id,attempt_count,next_due_at,metadata_due_at,stats_due_at
        FROM public.video_collection_state WHERE video_id=ANY(%s::text[]) ORDER BY video_id""", ([vid(2),vid(4)],))
    def claim():
        return repo.claim_due_batch(limit=50,worker_id="quota03_guard",run_id=str(uuid4()),
                                    lease_seconds=90,scopes=scope)
    first = claim()
    require({row.video_id for row in first["rows"]} == {vid(1),vid(3)}, "oldest_stats_anchor")
    require(not first["scan_exhausted"], "other_operation_not_exhausted")
    after = f.rows("""SELECT video_id,attempt_count,next_due_at,metadata_due_at,stats_due_at
        FROM public.video_collection_state WHERE video_id=ANY(%s::text[]) ORDER BY video_id""", ([vid(2),vid(4)],))
    require(after == before, "opposite_operation_untouched")
    second = claim()
    require({row.video_id for row in second["rows"]} == {vid(2),vid(4)}, "next_general_anchor")
    results.append(dict(name="oldest_operation_and_untouched_opposite",status="passed",provider_requests=0))
    f.reset()
    seed(f, 4, 1, "general")
    f.rows("""UPDATE public.video_collection_state SET metadata_due_at=clock_timestamp()+interval '2 days'
              WHERE video_id=ANY(%s::text[])""", ([vid(2),vid(3)],))
    with f.admin.transaction():
        f.rows("SELECT video_id FROM public.video_collection_state WHERE video_id=%s FOR UPDATE",(vid(1),))
        result = claim()
        require({row.video_id for row in result["rows"]} == {vid(2),vid(3)}, "locked_oldest_uses_next_eligible")
    results.append(dict(name="locked_oldest_operation",status="passed",provider_requests=0))
    f.reset()
    seed(f, 2, 1, "general")
    f.rows("""UPDATE public.video_collection_state SET stats_due_at=clock_timestamp()+interval '2 days',
        metadata_due_at=clock_timestamp()+interval '2 days',next_due_at=clock_timestamp()+interval '2 days'
        WHERE video_id=%s""",(vid(2),))
    result=claim()
    require([row.video_id for row in result["rows"]]==[vid(1)],"future_work_not_used_for_fill")
    results.append(dict(name="future_work_not_used_for_fill",status="passed",provider_requests=0))
    f.reset()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-proof", action="store_true")
    parser.add_argument("--size", choices=("small", "scale"), default="small")
    parser.add_argument("--variant", choices=("baseline", "candidate"), required=True)
    args = parser.parse_args()
    if not args.run_proof:
        emit(native_executed=False, size=args.size, variant=args.variant,
             videos=TOTAL if args.size == "scale" else 300, max_rows=ROW_CAP,
             youtube_requests=0, work_seconds=WORK_SECONDS)
        return 0
    require(os.getenv("LIC_SCHEMA_CI_MAX_ROWS") == str(ROW_CAP), "explicit_row_budget")
    out = Path("/out")
    require(out.is_dir(), "output_required")
    report = dict(status="failed", variant=args.variant, size=args.size, youtube_requests=0,
        fixture_restored=False, cases=[], limitation="Synthetic provider latency, real context/queue/PG17/service/AutomaticDispatchWindow; empty operator source, not full supervisor or live YouTube.")
    started = time.monotonic()
    try:
        with admission() as gate:
            f = Fixture(*dsns(), gate)
            f.budget, f.deadline = ROW_CAP, time.monotonic() + WORK_SECONDS
            try:
                cases = ([("scale", TOTAL, 500, "mixed", dict(scale=True))] if args.size == "scale" else [
                    ("single_stats", 1, 1, "stats", {}),
                    ("single_general", 1, 1, "general", {}),
                    ("remainder_stats", 83, 1, "stats", {}),
                    ("remainder_general", 83, 1, "general", {}),
                    ("mixed_channels", 300, 50, "mixed", {}),
                    ("cursor_continuation", 151, 17, "mixed", dict(pass_limit=75)),
                ])
                if args.variant == "candidate" and args.size == "small":
                    cases.append(("missing_view_fallback", 83, 1, "general", dict(missing=(2, 17, 81))))
                if args.variant == "candidate" and args.size == "small":
                    report["guards"] = native_guards(f)
                    emit(stage="guards", status="passed", count=len(report["guards"]))
                for name, total, channels, mode, options in cases:
                    report["cases"].append(run_case(f, out, args.variant, name, total, channels, mode, **options))
            finally:
                f.close()
                report["fixture_restored"] = True
        report["status"] = "passed"
    except Exception as error:
        report["error_class"] = type(error).__name__
        report["error_code"] = str(error) if isinstance(error, AssertionError) else None
        report["trace"] = [dict(file=Path(row.filename).name, line=row.lineno, function=row.name)
                           for row in traceback.extract_tb(error.__traceback__)[-8:]]
    report["seconds"] = round(time.monotonic() - started, 6)
    (out / "RESULT.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    emit(stage="result", status=report["status"], cases=len(report["cases"]),
         seconds=report["seconds"], error_class=report.get("error_class"), error_code=report.get("error_code"))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

