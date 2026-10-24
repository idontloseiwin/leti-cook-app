"""Initial-load provider and eight concurrent journal producers; no network/DB."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from types import SimpleNamespace
import copy
import json
import sys
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))
import youtube_client
from runtime import collection as runtime
from youtube_errors import YouTubeAPIError, is_youtube_daily_quota_exhausted
from youtube_quota import YouTubeQuotaAttemptRecorder
from db.quota.youtube_quota_journal import QuotaJournalStorageError
from youtube_video_profiles import fetch_initial_video_profiles
from test_collection_runtime import provider_inputs, binder_inputs

PARTS = "snippet,contentDetails,status,topicDetails,liveStreamingDetails,statistics"


def dependencies(body=None):
    response = SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=Mock())
    return dict(youtube_api_get=Mock(return_value=response), api_key="synthetic-key",
                attempt_recorder=object(), timeout_seconds=17, emit_event=Mock(),
                perf_counter=time.perf_counter)


class InitialVideoProviderTests(unittest.TestCase):
    def test_one_general_request_keeps_all_parts_and_same_recorder(self):
        body = {"items": [{"id": "video000001", "statistics": {"viewCount": "12"},
                            "topicDetails": {"topicCategories": []}}]}
        original = copy.deepcopy(body)
        deps = dependencies(body)
        result = fetch_initial_video_profiles(["video000001", "video000001"], **deps)
        self.assertEqual(result, body["items"])
        self.assertEqual(body, original)
        deps["youtube_api_get"].assert_called_once_with(
            "videos", phase="initial_hydration", quota_bucket="general",
            attempt_recorder=deps["attempt_recorder"], params={"part": PARTS, "id": "video000001"},
            api_key="synthetic-key", timeout_seconds=17, quota_units=1, initial_profile=True)

    def test_invalid_or_oversized_input_never_dispatches(self):
        for ids in (["bad "], [1], [""], [f"video{i:06d}" for i in range(51)]):
            deps = dependencies()
            with self.assertRaises((ValueError, YouTubeAPIError)):
                fetch_initial_video_profiles(ids, **deps)
            deps["youtube_api_get"].assert_not_called()
        self.assertEqual(fetch_initial_video_profiles([], **dependencies()), [])

    def test_missing_ids_are_allowed_but_all_response_identities_are_strict(self):
        self.assertEqual(fetch_initial_video_profiles(["requested"], **dependencies({"items": []})), [])
        for body in ({}, {"items": None}, {"items": [{"id": "other"}]},
                     {"items": [{"id": "requested"}, {"id": "requested"}]},
                     {"items": [None]}, {"items": [{"id": 2}]}):
            with self.subTest(body=body), self.assertRaises(YouTubeAPIError):
                fetch_initial_video_profiles(["requested"], **dependencies(body))

    def test_transport_requires_explicit_complete_initial_general_path(self):
        validate = youtube_client._validate_video_request_boundary
        for kwargs in ({}, {"initial_profile": False}):
            with self.assertRaises(ValueError):
                validate("videos", "general", {"part": PARTS}, **kwargs)
        validate("videos", "general", {"part": PARTS}, initial_profile=True)
        for endpoint, bucket, parts in (("videos", "video_batch_get_stats", PARTS),
                                      ("videos:batchGetStats", "general", PARTS),
                                      ("videos", "general", "statistics")):
            with self.assertRaises(ValueError):
                validate(endpoint, bucket, {"part": parts}, initial_profile=True)

    def test_refusals_are_fixed_safe_errors_and_stop_quota_work(self):
        for status, reason in ((403, "quotaExceeded"), (403, "rateLimitExceeded"), (429, None)):
            deps = dependencies({"error": {"message": "private-provider-text", "errors": [{"reason": reason}]}})
            deps["youtube_api_get"].return_value.status_code = status
            with self.assertRaises(YouTubeAPIError) as caught:
                fetch_initial_video_profiles(["requested"], **deps)
            self.assertEqual(is_youtube_daily_quota_exhausted(caught.exception, is_batch_stats_quota_error=lambda error: False), reason == "quotaExceeded")
            self.assertNotIn("private-provider-text", str(caught.exception))
        deps = dependencies({"error": {"message": "private-provider-text", "errors": [{"reason": "keyInvalid"}]}})
        deps["youtube_api_get"].return_value.status_code = 400
        with self.assertRaises(YouTubeAPIError) as caught:
            fetch_initial_video_profiles(["requested"], **deps)
        self.assertEqual(caught.exception.code, "provider_key_rejected")
        self.assertEqual(str(caught.exception), "provider_key_rejected")

    def test_runtime_passes_initial_callback_without_using_batch_or_metadata(self):
        supplied = provider_inputs()
        initial = Mock(return_value=object())
        supplied["get_initial_video_profiles"] = initial
        deps = runtime.video_collection_dependencies(**supplied)
        self.assertIs(deps["fetch_initial_profiles"](iter(["video"]), "initial_hydration"), initial.return_value)
        initial.assert_called_once_with(["video"], phase="initial_hydration")
        supplied["get_video_metadata"].assert_not_called()
        supplied["fetch_video_batch_stats"].assert_not_called()
        supplied = binder_inputs()
        supplied["fetch_initial_profiles"] = deps["fetch_initial_profiles"]
        with patch.object(runtime.video_execution, "run_video_collection_queue_batch") as run:
            runtime.bind_collection_callbacks(**supplied)["run_stats"](initial_job={"id": "job"})
        self.assertIs(run.call_args.kwargs["fetch_initial_profiles"], deps["fetch_initial_profiles"])

    def test_actual_operator_worker_assembly_runs_combined_initial_provider(self):
        import importlib.util
        from threading import Event
        import collection_shutdown
        from collection.settings import CollectionSettings
        from collection import api as collection_api
        from test_initial_video_parallel import AtomicRepository, CHANNEL, video

        core = SimpleNamespace(database=object(), process_database_resources=Mock(),
            API_KEY="synthetic-key", YOUTUBE_QUOTA_ATTEMPT_RECORDER=object(),
            YOUTUBE_API_TIMEOUT_SECONDS=17, COLLECTION_SCHEDULES=object())
        spec = importlib.util.spec_from_file_location("initial_worker_composition_test",
            ROOT / "application/scripts/run_operator_job_worker.py")
        worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"core": core}):
            spec.loader.exec_module(worker)
        ids = [f"video{i:06d}" for i in range(4)]
        def response(endpoint, **arguments):
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                json=lambda: {"items": [video(value) for value in arguments["params"]["id"].split(",")]})
        for concurrency in (1, 3, 8):
            with self.subTest(concurrency=concurrency):
                repository = AtomicRepository()
                with patch.object(collection_shutdown, "_requested", Event()), \
                     patch.object(runtime.video_execution, "VideoCollectionRepository", return_value=repository), \
                     patch.object(worker, "youtube_api_get", side_effect=response) as transport, \
                     patch.object(worker, "fetch_video_metadata", side_effect=AssertionError("split metadata called")) as metadata, \
                     patch.object(worker, "fetch_video_batch_stats", side_effect=AssertionError("Batch called")) as batch, \
                     patch.object(worker, "emit_event"):
                    callbacks = worker._collection_callbacks(repository.database, CollectionSettings(True, True, True))
                    runner = callbacks["run_stats"]
                    if concurrency == 3:
                        runner = partial(collection_api.run_video_collection_queue_batch,
                            context=SimpleNamespace(video=SimpleNamespace(run_batch=runner)))
                    result = runner(limit=len(ids), explicit_video_ids=ids,
                        reason="initial_channel_hydration", force_metadata=True,
                        initial_job={"id": "job", "channel_id": CHANNEL,
                                     "lease_owner": "worker", "lease_token": "run"},
                        initial_load_concurrency=concurrency)
                self.assertEqual((result["updated"], result["metadata_requests"], result["batch_requests"]), (4, 1, 0))
                self.assertEqual(result["initial_load_concurrency"], concurrency)
                self.assertEqual(len(repository.commits), 1)
                self.assertEqual(repository.successful_ids(), set(ids))
                transport.assert_called_once()
                self.assertEqual(transport.call_args.args, ("videos",))
                self.assertEqual(transport.call_args.kwargs["params"], {"part": PARTS, "id": ",".join(ids)})
                self.assertIs(transport.call_args.kwargs["initial_profile"], True)
                self.assertIs(transport.call_args.kwargs["attempt_recorder"], core.YOUTUBE_QUOTA_ATTEMPT_RECORDER)
                self.assertEqual(transport.call_args.kwargs["quota_bucket"], "general")
                metadata.assert_not_called()
                batch.assert_not_called()
        core.process_database_resources.assert_not_called()

    def test_eight_transport_threads_record_exact_start_and_finish_pairs(self):
        for force_fallback in (False, True):
            with self.subTest(force_fallback=force_fallback), TemporaryDirectory(prefix="initial-quota-") as directory:
                barrier, sink_lock = Barrier(8), Lock()
                persisted = {}
                recorder = YouTubeQuotaAttemptRecorder(Mock(), journal_path=Path(directory) / "journal.sqlite3",
                                                        background=False)
                self.addCleanup(recorder.close)
                journal = recorder.buffer._get_journal()
                append = journal.append
                forced = []

                def persist(events):
                    # Match the real idempotent DB acknowledgement. A busy local
                    # journal can use this path without losing the HTTP lifecycle.
                    with sink_lock:
                        for event in events:
                            key = (event["kind"], event["request_attempt_id"])
                            if key in persisted:
                                self.assertEqual(persisted[key], event)
                            persisted[key] = copy.deepcopy(event)
                    return True

                def journal_append(event):
                    with sink_lock:
                        fail = force_fallback and event["kind"] == "finish" and not forced
                        if fail:
                            forced.append(event["request_attempt_id"])
                    if fail:
                        raise QuotaJournalStorageError("synthetic_storage_failure")
                    return append(event)

                def response(url, **kwargs):
                    barrier.wait(timeout=10)
                    self.assertEqual(kwargs["headers"], {"X-Goog-Api-Key": "synthetic-key"})
                    self.assertEqual(kwargs["params"]["part"], PARTS)
                    video_id = kwargs["params"]["id"]
                    return SimpleNamespace(status_code=200, is_redirect=False,
                        json=lambda: {"items": [{"id": video_id, "statistics": {"viewCount": "1"}}]},
                        headers={}, close=lambda: None,
                        iter_content=lambda **kw: iter((json.dumps({'items': [{'id': video_id,
                            'statistics': {'viewCount': '1'}}]}).encode(),)),
                        raise_for_status=lambda: None)
                fetch = partial(fetch_initial_video_profiles, youtube_api_get=youtube_client.youtube_api_get,
                                api_key="synthetic-key", attempt_recorder=recorder, timeout_seconds=17,
                                emit_event=lambda *args, **kwargs: None, perf_counter=time.perf_counter)
                with patch("hot_runtime.require_hot_runtime_ready"), \
                     patch('provider_runtime.admit_request', side_effect=lambda **kw: nullcontext(kw.get('cancel'))), \
                     patch.object(youtube_client.os, "getenv", return_value=None), \
                     patch.object(youtube_client.requests, "get", side_effect=response), \
                     patch.object(journal, "append", side_effect=journal_append), \
                     patch.object(recorder.repository, "persist_events", side_effect=persist):
                    with ThreadPoolExecutor(max_workers=8) as executor:
                        results = list(executor.map(lambda index: fetch([f"video{index:06d}"]), range(8)))
                self.assertEqual(len(results), 8)
                self.assertEqual(recorder.buffer_status()["unrecorded_events"], 0)
                self.assertEqual(recorder._active_starts, {})
                events = dict(persisted)
                for _, event in journal.read_batch(100):
                    key = (event["kind"], event["request_attempt_id"])
                    if key in events:
                        self.assertEqual(events[key], event)
                    events[key] = event
                self.assertEqual(len(events), 16)
                starts = {attempt: event for (kind, attempt), event in events.items() if kind == "start"}
                finishes = {attempt: event for (kind, attempt), event in events.items() if kind == "finish"}
                self.assertEqual(len(starts), 8)
                self.assertEqual(set(starts), set(finishes))
                self.assertTrue(all(event["values"]["quota_bucket"] == "general" for event in starts.values()))
                self.assertEqual(sum(event["values"]["quota_units"] for event in starts.values()), 8)
                self.assertTrue(all(event["values"]["status"] == "success" for event in finishes.values()))
                if force_fallback:
                    self.assertEqual(len(forced), 1)
                    self.assertIn(("start", forced[0]), persisted)
                    self.assertIn(("finish", forced[0]), persisted)
                recorder.close()


if __name__ == "__main__":
    unittest.main()
