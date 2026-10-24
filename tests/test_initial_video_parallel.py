"""Deterministic initial hydration threads and atomic planning; no network/DB."""
from concurrent.futures import ThreadPoolExecutor as RealExecutor
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from threading import Barrier, Event, Lock, get_ident
from types import SimpleNamespace
import copy
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
from collection import initial_video_hydration as initial
from collection.video_payloads import build_video_collection_metadata_payload
import collection_shutdown as shutdown
from db import VideoCollectionClaim
from operator_jobs import compact_initial_inventory_summary, operator_collection_stop_reason
from video_collection_service import VideoCollectionService, YouTubeViewCountAbsent, YouTubeViewCountInvalid
from youtube_errors import YouTubeDailyQuotaExceededError, is_youtube_daily_quota_exhausted
from youtube_source_fields import parse_dt
from youtube_video_profiles import fetch_initial_video_profiles

CHANNEL = "UC" + "c" * 22
IN_TRANSACTION = ContextVar("initial_test_transaction", default=False)
STAMP = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
END = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()


def video(video_id, *, ended=False, observed_at=None):
    published = STAMP if observed_at is None else (observed_at - timedelta(days=10)).isoformat()
    end = END if observed_at is None else (observed_at - timedelta(days=9)).isoformat()
    item = {"id": video_id,
        "snippet": {"channelId": CHANNEL, "publishedAt": published, "title": "Real normalized title",
                    "tags": ["tag"], "categoryId": "1", "defaultAudioLanguage": "en", "defaultLanguage": "en"},
        "contentDetails": {"duration": "PT10M", "caption": "true"},
        "status": {"privacyStatus": "public", "uploadStatus": "processed"},
        "statistics": {"viewCount": "123", "likeCount": "7", "commentCount": "2"},
        "topicDetails": {"topicCategories": ["https://en.wikipedia.org/wiki/Film"]}}
    if ended:
        item["liveStreamingDetails"] = {"actualEndTime": end}
    return item


class AtomicRepository:
    def __init__(self, kinds=None):
        self.owner = get_ident()
        self.database = object()
        self.kinds = kinds or {}
        self.claims, self.attempts, self.commits, self.released = [], [], [], []
        self.claim_hook = self.commit_hook = None

    def claim_initial_video_ids(self, ids, **scope):
        assert get_ident() == self.owner and not IN_TRANSACTION.get()
        assert 1 <= len(ids) <= 50
        token = IN_TRANSACTION.set(True)
        try:
            claims = [VideoCollectionClaim(
                video_id=value, channel_id=CHANNEL, video_type=self.kinds.get(value, ("long", "hydrate"))[0],
                collection_mode=self.kinds.get(value, ("long", "hydrate"))[1],
                lease_token=str(uuid5(NAMESPACE_URL, value)), lease_expires_at=STAMP, next_due_at=STAMP)
                for value in ids]
            self.claims.append((list(ids), dict(scope)))
        finally:
            IN_TRANSACTION.reset(token)
        if self.claim_hook:
            self.claim_hook(len(self.claims))
        return claims

    def complete_initial_batch(self, commands):
        assert get_ident() == self.owner and not IN_TRANSACTION.get()
        token = IN_TRANSACTION.set(True)
        try:
            rows = [row for command in commands for row in command["rows"]]
            assert 1 <= len(rows) <= 50
            assert len({row["video_id"] for row in rows}) == len(rows)
            self.attempts.append(copy.deepcopy(commands))
            if self.commit_hook:
                self.commit_hook(len(self.attempts), commands)
            self.commits.append(copy.deepcopy(commands))
            return len(rows)
        finally:
            IN_TRANSACTION.reset(token)

    def release(self, repository, claims):
        assert repository is self and get_ident() == self.owner and not IN_TRANSACTION.get()
        self.released.extend(claim.video_id for claim in claims)

    def successful_ids(self):
        return {row["video_id"] for commands in self.commits for command in commands
                if command["operation"] in {"complete_metadata", "complete_typed_metadata"}
                for row in command["rows"]}


def service(repository, fetch):
    forbidden = Mock(side_effect=AssertionError("recurring provider must not run"))
    def build(channel_id, item, now):
        assert get_ident() == repository.owner and not IN_TRANSACTION.get()
        return build_video_collection_metadata_payload(channel_id, item, now,
            batch_stats_enabled=True, parse_dt=parse_dt,
            view_count_absent_error=YouTubeViewCountAbsent, view_count_invalid_error=YouTubeViewCountInvalid)
    result = VideoCollectionService(repository, fetch_full=forbidden, fetch_metadata=forbidden,
        fetch_counters=forbidden, build_full_payload=build, build_counter_payload=forbidden,
        fetch_initial_profiles=fetch, is_quota_exhausted=partial(is_youtube_daily_quota_exhausted,
                                                              is_batch_stats_quota_error=lambda error: False))
    result.forbidden = forbidden
    return result


def run(collector, ids, *, concurrency=8, progress=None):
    return initial.run_initial_video_window(collector, job_id="job", channel_id=CHANNEL,
        video_ids=ids, worker_id="worker", run_id="run", concurrency=concurrency,
        progress_callback=progress)


def gated_executor(gate, immediate):
    class Executor(RealExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.submitted = 0
        def submit(self, callback, *args, **kwargs):
            self.submitted += 1
            index = self.submitted
            def gated():
                if index > immediate:
                    assert gate.wait(10), "dispatch gate timed out"
                return callback(*args, **kwargs)
            return super().submit(gated)
        def __exit__(self, *args):
            gate.set()
            return super().__exit__(*args)
    return Executor


class InitialVideoParallelTests(unittest.TestCase):
    def setUp(self):
        self.stop_patch = patch.object(shutdown, "_requested", Event())
        self.stop_patch.start()
        self.addCleanup(self.stop_patch.stop)

    def test_eight_real_parallel_callbacks_fifty_ids_and_400_bound(self):
        repository = AtomicRepository()
        barrier, lock = Barrier(8), Lock()
        active = peak = 0
        threads, calls, progress = set(), [], []
        def fetch(ids, phase):
            nonlocal active, peak
            self.assertFalse(IN_TRANSACTION.get())
            self.assertNotEqual(get_ident(), repository.owner)
            with lock:
                threads.add(get_ident())
                active += 1
                peak = max(peak, active)
                calls.append((list(ids), phase))
            barrier.wait(timeout=10)
            result = [video(value) for value in ids]
            with lock:
                active -= 1
            return result
        def progressed(summary):
            self.assertEqual(get_ident(), repository.owner)
            self.assertFalse(IN_TRANSACTION.get())
            self.assertEqual(set(summary["video_ids"]), repository.successful_ids())
            self.assertEqual(summary["updated"], len(repository.successful_ids()))
            progress.append(summary["updated"])
        collector = service(repository, fetch)
        result = run(collector, [f"video{i:06d}" for i in range(400)], progress=progressed)
        self.assertEqual((peak, len(threads), len(calls)), (8, 8, 8))
        self.assertEqual([len(ids) for ids, _ in calls], [50] * 8)
        self.assertEqual((len(repository.commits), result["updated"], result["claimed"]), (8, 400, 400))
        self.assertEqual((result["metadata_requests"], result["batch_requests"]), (8, 0))
        self.assertEqual(result["unstarted_video_ids"], [])
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(result["completion_outcome"], "completed")
        collector.forbidden.assert_not_called()

    def test_invalid_bounds_fail_before_claim_or_provider(self):
        repository = AtomicRepository()
        fetch = Mock()
        collector = service(repository, fetch)
        for count, concurrency in ((401, 8), (51, 1), (1, 0), (1, 9), (1, True)):
            with self.subTest(count=count, concurrency=concurrency), self.assertRaises(ValueError):
                run(collector, [f"video{i:06d}" for i in range(count)], concurrency=concurrency)
        self.assertEqual(repository.claims, [])
        fetch.assert_not_called()

    def test_actual_combined_provider_uses_single_general_request_and_normalizes(self):
        repository = AtomicRepository()
        transport = Mock(return_value=SimpleNamespace(status_code=200,
            json=lambda: {"items": [video("video000001")]}, raise_for_status=lambda: None))
        fetch = partial(fetch_initial_video_profiles, youtube_api_get=transport,
            api_key="synthetic", attempt_recorder=object(), timeout_seconds=17,
            emit_event=lambda *a, **kw: None, perf_counter=lambda: 1.0)
        collector = service(repository, fetch)
        result = run(collector, ["video000001"])
        transport.assert_called_once()
        request = transport.call_args.kwargs
        self.assertEqual((transport.call_args.args, request["quota_bucket"], request["initial_profile"]),
                         (("videos",), "general", True))
        self.assertIn("statistics", request["params"]["part"].split(","))
        row = repository.commits[0][0]["rows"][0]
        self.assertEqual((row["views"], row["like_count"], row["comment_count"]), (123, 7, 2))
        self.assertEqual((row["caption_available"], row["category_id"], row["tags"]), (True, "1", ["tag"]))
        self.assertEqual((result["updated"], result["batch_requests"]), (1, 0))
        collector.forbidden.assert_not_called()

    def mixed(self):
        kinds = {"short": ("short", "hydrate"), "ended": ("stream", "hydrate"),
                 "live": ("stream", "stream_lifecycle"), "missing-short": ("short", "hydrate"),
                 "missing-stream": ("stream", "hydrate"), "bad-short": ("short", "hydrate"),
                 "bad-stream": ("stream", "hydrate")}
        ids = ["long", "short", "ended", "live", "missing-long", "missing-short", "missing-stream",
               "absent", "invalid", "bad-short", "bad-stream"]
        def fetch(selected, phase):
            items = []
            for value in selected:
                if value.startswith("missing"):
                    continue
                item = video(value, ended=value in {"ended", "bad-stream"})
                if value in {"absent", "bad-short"}:
                    item["statistics"].pop("viewCount")
                if value in {"invalid", "bad-stream"}:
                    item["statistics"]["viewCount"] = "not-a-counter"
                items.append(item)
            return items
        return kinds, ids, fetch

    def test_mixed_types_missing_lifecycle_and_invalid_counters_share_one_atomic_commit(self):
        kinds, ids, fetch = self.mixed()
        repository = AtomicRepository(kinds)
        collector = service(repository, fetch)
        collector.fetch_counters = lambda ids, phase: {"items": [], "summary": {"failed_ids": list(ids)}}
        result = run(collector, ids)
        self.assertEqual(len(repository.commits), 1)
        commands = repository.commits[0]
        self.assertEqual({command["operation"] for command in commands},
            {"complete_metadata", "complete_typed_metadata", "complete_stream_lifecycle", "complete_missing", "complete_identity_refresh", "fail_batch"})
        rows = {row["video_id"]: (command["operation"], row) for command in commands for row in command["rows"]}
        self.assertEqual(set(rows), set(ids))
        self.assertEqual(rows["live"][0], "complete_stream_lifecycle")
        self.assertEqual(rows["ended"][1]["stream_actual_end_at"], END)
        self.assertEqual(rows["absent"][0], "complete_identity_refresh")
        self.assertTrue(rows["absent"][1]["counterless_profile"])
        self.assertFalse(rows["absent"][1]["missing"])
        self.assertEqual(rows["invalid"][1]["error_type"], "youtube_view_count_invalid")
        self.assertTrue(all(rows[value][0] == "complete_missing" for value in ids if value.startswith("missing")))
        self.assertEqual((result["updated"], result["unavailable_marked"], result["stream_lifecycle_probed"]), (3, 3, 1))
        self.assertEqual(result["unresolved"], 4)

    def test_concurrency_one_keeps_identical_normalized_outcomes(self):
        kinds, ids, fetch = self.mixed()
        results = []
        for concurrency in (1, 8):
            repository = AtomicRepository(kinds)
            collector = service(repository, fetch)
            collector.fetch_counters = lambda ids, phase: {"items": [], "summary": {"failed_ids": list(ids)}}
            result = run(collector, ids, concurrency=concurrency)
            outcomes = sorted((command["operation"], row["video_id"], row.get("views"), row.get("error_type"))
                for commands in repository.commits for command in commands for row in command["rows"])
            results.append((outcomes, {key: result[key] for key in ("updated", "unavailable_marked", "errors",
                            "source_view_count_absent", "source_view_count_invalid", "unresolved", "completion_outcome")}))
        self.assertEqual(results[0], results[1])

    def test_quota_refusal_suppresses_queued_dispatch_and_reports_exact_refund_ids(self):
        repository = AtomicRepository()
        claimed, gate = Event(), Event()
        repository.claim_hook = lambda count: claimed.set() if count == 8 else None
        calls = []
        def fetch(ids, phase):
            calls.append(list(ids))
            self.assertTrue(claimed.wait(10))
            raise YouTubeDailyQuotaExceededError("quota")
        ids = [f"video{i:06d}" for i in range(400)]
        with patch.object(initial, "ThreadPoolExecutor", gated_executor(gate, 1)), \
             patch.object(initial, "_release_unsent", side_effect=repository.release):
            result = run(service(repository, fetch), ids, progress=lambda summary: gate.set())
        self.assertEqual(len(calls), 1)
        self.assertEqual((result["metadata_requests"], result["batch_requests"]), (1, 0))
        self.assertEqual(result["unstarted_video_ids"], ids[50:])
        self.assertEqual(set(repository.released), set(ids[50:]))
        compact = compact_initial_inventory_summary({"hydration_unstarted_video_ids": result["unstarted_video_ids"]})
        self.assertEqual(compact["hydration_unstarted_video_ids"], ids[50:])
        self.assertEqual(operator_collection_stop_reason(result), "quota_exhausted")
        failures = [row for command in repository.commits[0] for row in command["rows"]]
        self.assertEqual(len(failures), 50)
        self.assertTrue(all(row["error_type"] == "youtube_quota_exhausted" for row in failures))

    def test_unknown_commit_stops_queued_dispatch_but_drains_independent_response(self):
        repository = AtomicRepository()
        claimed, second_started, gate = Event(), Event(), Event()
        repository.claim_hook = lambda count: claimed.set() if count == 8 else None
        def commit(index, commands):
            if index == 1:
                raise RuntimeError("lost commit acknowledgement")
        repository.commit_hook = commit
        calls, snapshots = [], []
        def fetch(ids, phase):
            calls.append(list(ids))
            if ids[0] == "video000000":
                self.assertTrue(second_started.wait(10))
                self.assertTrue(claimed.wait(10))
            else:
                second_started.set()
                self.assertTrue(gate.wait(10))
            return [video(value) for value in ids]
        def progressed(summary):
            self.assertEqual(set(summary["video_ids"]), repository.successful_ids())
            snapshots.append(copy.deepcopy(summary))
            gate.set()
        ids = [f"video{i:06d}" for i in range(400)]
        with patch.object(initial, "ThreadPoolExecutor", gated_executor(gate, 2)), \
             patch.object(initial, "_release_unsent", side_effect=repository.release):
            result = run(service(repository, fetch), ids, progress=progressed)
        self.assertEqual((len(calls), len(repository.attempts), len(repository.commits)), (2, 2, 1))
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["updated"], 50)
        self.assertEqual(result["unstarted_video_ids"], ids[100:])
        self.assertEqual(set(repository.released), set(ids[100:]))
        self.assertEqual(snapshots[0]["updated"], 0)
        self.assertTrue(snapshots[0]["settlement_unknown"])

    def test_raw_handoff_stops_new_dispatch_even_under_parent_context_deferral(self):
        repository = AtomicRepository()
        calls = []
        def fetch(ids, phase):
            calls.append(list(ids))
            self.assertTrue(shutdown._deferred.get())
            shutdown._requested.set()
            self.assertFalse(shutdown.yield_requested())
            return [video(value) for value in ids]
        # The second claim waits until the first callback has set raw handoff.
        requested = Event()
        original_fetch = fetch
        def fetch_and_signal(ids, phase):
            result = original_fetch(ids, phase)
            requested.set()
            return result
        repository.claim_hook = lambda count: requested.wait(10) if count == 2 else None
        ids = [f"video{i:06d}" for i in range(100)]
        with shutdown.finish_current_chunk(), patch.object(initial, "_release_unsent", side_effect=repository.release):
            result = run(service(repository, fetch_and_signal), ids)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["updated"], 50)
        self.assertEqual(result["unstarted_video_ids"], ids[50:])
        self.assertTrue(result["yield_requested"])
        self.assertEqual(result["stopped_reason"], "scheduled_handoff")

    def test_unknown_claim_ids_are_not_released_or_refunded(self):
        repository = AtomicRepository()
        def claim_failed(count):
            raise RuntimeError("claim acknowledgement lost")
        repository.claim_hook = claim_failed
        fetch = Mock()
        ids = [f"video{i:06d}" for i in range(400)]
        with patch.object(initial, "_release_unsent", side_effect=repository.release):
            result = run(service(repository, fetch), ids)
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(result["unstarted_video_ids"], ids[50:])
        self.assertEqual(result["metadata_requests"], 0)
        self.assertEqual(repository.released, [])
        self.assertEqual(repository.commits, [])
        fetch.assert_not_called()

    def test_quota_latch_during_next_claim_prevents_dispatch_of_that_claim(self):
        repository = AtomicRepository()
        second_claim, refused = Event(), Event()
        def claimed(count):
            if count == 2:
                second_claim.set()
                self.assertTrue(refused.wait(10))
        repository.claim_hook = claimed
        calls = []
        def fetch(ids, phase):
            calls.append(list(ids))
            self.assertTrue(second_claim.wait(10))
            # Set only after the worker's fetch wrapper has caught the error.
            raise YouTubeDailyQuotaExceededError("quota")
        real_executor = RealExecutor
        class ObservedExecutor(real_executor):
            def submit(self, callback, *args, **kwargs):
                def observe():
                    result = callback(*args, **kwargs)
                    if result.error is not None:
                        refused.set()
                    return result
                return super().submit(observe)
        ids = [f"video{i:06d}" for i in range(400)]
        with patch.object(initial, "ThreadPoolExecutor", ObservedExecutor), \
             patch.object(initial, "_release_unsent", side_effect=repository.release):
            result = run(service(repository, fetch), ids)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(repository.claims), 2)
        self.assertEqual(result["unstarted_video_ids"], ids[50:])
        self.assertEqual(set(repository.released), set(ids[50:100]))
        self.assertEqual(result["metadata_requests"], 1)
        self.assertTrue(result["quota_exhausted"])

    def test_atomic_count_mismatch_never_reports_success(self):
        repository = AtomicRepository()
        repository.complete_initial_batch = Mock(return_value=0)
        result = run(service(repository, lambda ids, phase: [video(value) for value in ids]), ["video000001"])
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["video_ids"], [])
        self.assertTrue(result["settlement_unknown"])
        self.assertEqual(repository.complete_initial_batch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
