"""Explicit runtime callback composition; no database or provider requests."""

import ast
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
sys.path.insert(0, str(APPLICATION))
from collection import initial, lifecycle, video_execution
from collection.settings import CollectionSettings
from db import VideoCollectionClaim
from runtime import collection as runtime


class ApiError(RuntimeError):
    pass


class MissingPlaylist(RuntimeError):
    pass


class QuotaExceeded(RuntimeError):
    pass


def provider_inputs():
    return dict(
        database=Mock(name="chunk_gateway"), settings=CollectionSettings(True, True, True),
        api_error=ApiError,
        get_video_metadata=Mock(name="metadata_provider", return_value=object()),
        fetch_video_batch_stats=Mock(name="batch_provider", return_value=object()),
        api_key="offline-test-key", attempt_recorder=Mock(name="process_recorder"),
        timeout_seconds=17, counter_parts=("views", "likes"),
        build_full_payload=Mock(name="full_payload", return_value=object()),
        build_counter_payload=Mock(name="counter_payload", return_value=object()),
        is_quota_exhausted=Mock(name="quota_classifier", return_value=False),
    )


def binder_inputs():
    supplied = runtime.video_collection_dependencies(**provider_inputs())
    supplied.update(
        worker_id_factory=Mock(return_value="worker"), fetch_page=Mock(return_value={"items": []}),
        get_longform_playlist_id=Mock(return_value="playlist"), parse_dt=Mock(return_value=None),
        playlist_not_found_error=MissingPlaylist, daily_quota_exceeded_error=QuotaExceeded,
        copy_collection_stop=Mock(return_value=False),
    )
    return supplied


class CollectionRuntimeTests(unittest.TestCase):
    def test_module_imports_without_bootstrap_environment_or_network(self):
        tree = ast.parse((APPLICATION / "runtime/collection.py").read_text(encoding="utf-8"))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
        self.assertFalse(imports & {"core", "sync_service", "server", "os", "dotenv"})
        script = """
import builtins, socket, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split('.')[0] in {'core', 'sync_service', 'server', 'dotenv'}:
        raise AssertionError('bootstrap import: ' + name)
    return original(name, *args, **kwargs)
with patch('builtins.__import__', side_effect=checked_import), \
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network during import')):
    from runtime.collection import bind_collection_callbacks, video_collection_dependencies
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(APPLICATION)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_provider_normalization_preserves_phase_options_order_and_resources(self):
        supplied = provider_inputs()
        dependencies = runtime.video_collection_dependencies(**supplied)
        self.assertEqual(set(dependencies), {"database", "settings", "fetch_full", "fetch_metadata", "fetch_counters", "fetch_initial_profiles",
                                             "build_full_payload", "build_counter_payload", "is_quota_exhausted"})
        self.assertIs(dependencies["database"], supplied["database"])
        self.assertIs(dependencies["settings"], supplied["settings"])
        self.assertIs(dependencies["is_quota_exhausted"], supplied["is_quota_exhausted"])
        ids = ("video-b", "video-a", "video-b")
        self.assertIs(dependencies["fetch_metadata"](iter(ids), "metadata-phase"), supplied["get_video_metadata"].return_value)
        self.assertIs(dependencies["fetch_counters"](iter(ids), "counter-phase"), supplied["fetch_video_batch_stats"].return_value)
        supplied["get_video_metadata"].assert_called_once_with(list(ids), phase="metadata-phase", reject_identity_anomalies=False, include_statistics=True)
        supplied["fetch_video_batch_stats"].assert_called_once_with(
            list(ids), api_key=supplied["api_key"], attempt_recorder=supplied["attempt_recorder"],
            timeout_seconds=17, phase="counter-phase", parts=supplied["counter_parts"])
        self.assertIs(supplied["fetch_video_batch_stats"].call_args.kwargs["attempt_recorder"], supplied["attempt_recorder"])
        self.assertIs(supplied["fetch_video_batch_stats"].call_args.kwargs["parts"], supplied["counter_parts"])
        supplied["database"].close.assert_not_called()
        supplied["attempt_recorder"].close.assert_not_called()

    def test_full_profiles_use_scoped_gate_and_raw_strict_metadata_with_process_resources(self):
        video_id = "video000001"
        for scoped_enabled in (True, False):
            with self.subTest(scoped_enabled=scoped_enabled):
                supplied = provider_inputs()
                supplied["settings"] = CollectionSettings(True, True, scoped_enabled)
                calls = []
                def metadata(ids, *, phase, reject_identity_anomalies=True, include_statistics=False):
                    calls.append((list(ids), phase, reject_identity_anomalies, include_statistics))
                    return [{"id": video_id, "snippet": {"title": "Synthetic"}}]
                supplied["get_video_metadata"] = metadata
                supplied["fetch_video_batch_stats"].return_value = {
                    "items": [{"id": video_id, "views": 42, "like_count": 7, "comment_count": 2}],
                    "summary": {"failed_ids": []},
                }
                conflicting_core = SimpleNamespace(
                    YOUTUBE_BATCH_STATS_ENABLED=not scoped_enabled,
                    get_video_stats=Mock(side_effect=AssertionError("process full provider must not run")),
                )
                with patch.dict(sys.modules, {"core": conflicting_core}), \
                     patch.dict(os.environ, {"YOUTUBE_BATCH_STATS_ENABLED": str(not scoped_enabled)}):
                    dependencies = runtime.video_collection_dependencies(**supplied)
                    if scoped_enabled:
                        result = dependencies["fetch_full"]([video_id], "full-phase")
                        self.assertEqual(result[0]["statistics"], {"viewCount": 42, "likeCount": 7, "commentCount": 2})
                        self.assertEqual(calls, [([video_id], "full-phase", True, False)])
                        supplied["fetch_video_batch_stats"].assert_called_once_with(
                            [video_id], api_key=supplied["api_key"], attempt_recorder=supplied["attempt_recorder"],
                            timeout_seconds=17, phase="full-phase", parts=supplied["counter_parts"])
                        arguments = supplied["fetch_video_batch_stats"].call_args.kwargs
                        self.assertIs(arguments["attempt_recorder"], supplied["attempt_recorder"])
                        self.assertIs(arguments["parts"], supplied["counter_parts"])
                        dependencies["fetch_metadata"]([video_id], "common-phase")
                        self.assertEqual(calls[-1], ([video_id], "common-phase", False, True))
                    else:
                        with self.assertRaises(ApiError):
                            dependencies["fetch_full"]([video_id], "full-phase")
                        self.assertEqual(calls, [])
                        supplied["fetch_video_batch_stats"].assert_not_called()
                conflicting_core.get_video_stats.assert_not_called()
                supplied["database"].close.assert_not_called()
                supplied["attempt_recorder"].close.assert_not_called()

    def test_full_profile_errors_keep_identity_and_do_not_retry_either_provider(self):
        for failed_provider in ("get_video_metadata", "fetch_video_batch_stats"):
            with self.subTest(failed_provider=failed_provider):
                supplied = provider_inputs()
                supplied["get_video_metadata"].return_value = [{"id": "video000001"}]
                error = QuotaExceeded("synthetic refusal")
                supplied[failed_provider].side_effect = error
                dependencies = runtime.video_collection_dependencies(**supplied)
                with self.assertRaises(QuotaExceeded) as caught:
                    dependencies["fetch_full"](["video000001"], "full-error")
                self.assertIs(caught.exception, error)
                supplied["get_video_metadata"].assert_called_once_with(["video000001"], phase="full-error")
                self.assertEqual(supplied["fetch_video_batch_stats"].call_count,
                                 int(failed_provider == "fetch_video_batch_stats"))
                supplied["attempt_recorder"].close.assert_not_called()

    def test_retained_typed_service_settles_full_profiles_using_the_scoped_gate(self):
        video_id, channel = "video000001", "UC" + "s" * 22
        claim = VideoCollectionClaim(video_id=video_id, channel_id=channel, video_type="short",
            collection_mode="hydrate", lease_token="owned-short-lease",
            lease_expires_at="2026-09-30T12:00:00Z", next_due_at="2026-09-27T12:00:00Z")
        for scoped_enabled in (True, False):
            with self.subTest(scoped_enabled=scoped_enabled):
                supplied = provider_inputs()
                supplied["settings"] = CollectionSettings(True, True, scoped_enabled)
                supplied["get_video_metadata"].return_value = [{
                    "id": video_id, "snippet": {"channelId": channel, "publishedAt": "2026-09-01T12:00:00Z",
                                                "title": "Synthetic Short"},
                    "contentDetails": {"duration": "PT1M"},
                    "status": {"privacyStatus": "public", "uploadStatus": "processed"},
                }]
                supplied["fetch_video_batch_stats"].return_value = {
                    "items": [{"id": video_id, "views": 42, "like_count": 7, "comment_count": 2}],
                    "summary": {"failed_ids": []},
                }
                supplied["build_full_payload"].return_value = {
                    "row": {"channel_id": channel, "title": "Synthetic Short"},
                    "snapshot": {"views": 42, "like_count": 7, "comment_count": 2},
                }
                repository = Mock(spec=["complete_typed_metadata", "fail_typed", "complete_missing",
                                        "complete_stream_lifecycle"])
                core = SimpleNamespace(YOUTUBE_BATCH_STATS_ENABLED=not scoped_enabled,
                    get_video_stats=Mock(side_effect=AssertionError("process full provider must not run")))
                with patch.dict(sys.modules, {"core": core}), \
                     patch.object(video_execution, "VideoCollectionRepository", return_value=repository) as make_repository:
                    bound, service = video_execution.build_video_collection_service(
                        **runtime.video_collection_dependencies(**supplied))
                    self.assertIs(bound, repository)
                    make_repository.assert_called_once_with(supplied["database"])
                    summary = service.process_typed_claims([claim], video_type="short", reason="typed-scope")
                self.assertEqual(summary["claimed"], 1)
                if scoped_enabled:
                    self.assertEqual((summary["updated"], summary["errors"]), (1, 0))
                    repository.complete_typed_metadata.assert_called_once()
                    row = repository.complete_typed_metadata.call_args.args[0][0]
                    self.assertEqual((row["video_id"], row["video_type"], row["lease_token"], row["views"]),
                                     (video_id, "short", claim.lease_token, 42))
                    supplied["get_video_metadata"].assert_called_once_with([video_id], phase="typed-scope")
                    supplied["fetch_video_batch_stats"].assert_called_once()
                    payload_call = supplied["build_full_payload"].call_args
                    self.assertEqual(payload_call.args[1]["statistics"]["viewCount"], 42)
                    self.assertIs(payload_call.kwargs["batch_stats_enabled"], True)
                    repository.fail_typed.assert_not_called()
                else:
                    self.assertEqual((summary["updated"], summary["errors"], summary["stopped_reason"]),
                                     (0, 1, "provider_request_failed"))
                    repository.complete_typed_metadata.assert_not_called()
                    repository.fail_typed.assert_called_once()
                    failed_type, failures = repository.fail_typed.call_args.args
                    self.assertEqual((failed_type, failures[0]["video_id"], failures[0]["lease_token"],
                                      failures[0]["error_type"]),
                                     ("short", video_id, claim.lease_token, "youtube_full_request_error"))
                    self.assertIsInstance(supplied["is_quota_exhausted"].call_args.args[0], ApiError)
                    supplied["get_video_metadata"].assert_not_called()
                    supplied["fetch_video_batch_stats"].assert_not_called()
                    supplied["build_full_payload"].assert_not_called()
                core.get_video_stats.assert_not_called()
                supplied["attempt_recorder"].close.assert_not_called()
                supplied["database"].close.assert_not_called()

    def test_worker_composition_binds_raw_metadata_and_the_shared_error_class(self):
        tree = ast.parse((APPLICATION / "scripts/run_operator_job_worker.py").read_text(encoding="utf-8"))
        core_imports = {alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                        and node.module == "core" for alias in node.names}
        self.assertNotIn("get_video_stats", core_imports)
        self.assertNotIn("YouTubeAPIError", core_imports)
        shared_errors = {alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                         and node.module == "youtube_errors" for alias in node.names}
        self.assertIn("YouTubeAPIError", shared_errors)
        core_tree = ast.parse((APPLICATION / "core.py").read_text(encoding="utf-8"))
        core_errors = {alias.name for node in core_tree.body if isinstance(node, ast.ImportFrom)
                       and node.module == "youtube_errors" for alias in node.names}
        self.assertIn("YouTubeAPIError", core_errors)
        factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_collection_callbacks")
        calls = [node for node in ast.walk(factory) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "video_collection_dependencies"]
        self.assertEqual(len(calls), 1)
        arguments = {keyword.arg: keyword.value for keyword in calls[0].keywords}
        self.assertNotIn("get_video_stats", arguments)
        self.assertNotIn("get_video_metadata", core_imports)
        self.assertIsInstance(arguments["api_error"], ast.Name)
        self.assertEqual(arguments["api_error"].id, "YouTubeAPIError")
        metadata = arguments["get_video_metadata"]
        self.assertIsInstance(metadata, ast.Call)
        self.assertEqual(ast.unparse(metadata.func), "partial")
        self.assertEqual([ast.unparse(argument) for argument in metadata.args], ["fetch_video_metadata"])
        self.assertEqual({keyword.arg: ast.unparse(keyword.value) for keyword in metadata.keywords}, {
            "youtube_api_get": "youtube_api_get", "api_key": "API_KEY",
            "attempt_recorder": "YOUTUBE_QUOTA_ATTEMPT_RECORDER",
            "timeout_seconds": "YOUTUBE_API_TIMEOUT_SECONDS", "emit_event": "emit_event",
            "perf_counter": "time.perf_counter",
        })

    def test_payload_callbacks_receive_now_and_explicit_flag_and_bind_at_construction(self):
        supplied = provider_inputs()
        dependencies = runtime.video_collection_dependencies(**supplied)
        full, counters = supplied["build_full_payload"], supplied["build_counter_payload"]
        supplied["build_full_payload"] = Mock(side_effect=AssertionError("replacement must not run"))
        supplied["build_counter_payload"] = Mock(side_effect=AssertionError("replacement must not run"))
        video = {"id": "video"}
        self.assertIs(dependencies["build_full_payload"]("channel", video, "now", batch_stats_enabled=False), full.return_value)
        self.assertIs(dependencies["build_counter_payload"](video, "now"), counters.return_value)
        full.assert_called_once_with("channel", video, now="now", batch_stats_enabled=False)
        counters.assert_called_once_with(video, now="now")

    def test_provider_exception_is_not_reclassified_or_retried(self):
        supplied = provider_inputs()
        error = QuotaExceeded("offline refusal")
        supplied["fetch_video_batch_stats"].side_effect = error
        dependencies = runtime.video_collection_dependencies(**supplied)
        with self.assertRaises(QuotaExceeded) as caught:
            dependencies["fetch_counters"](["video"], "phase")
        self.assertIs(caught.exception, error)
        self.assertEqual(supplied["fetch_video_batch_stats"].call_count, 1)
        supplied["get_video_metadata"].assert_not_called()

    def test_provider_factory_requires_explicit_arguments_and_valid_callbacks(self):
        supplied = provider_inputs()
        for name in supplied:
            with self.subTest(missing=name):
                missing = dict(supplied)
                del missing[name]
                with self.assertRaises(TypeError):
                    runtime.video_collection_dependencies(**missing)
        for name in ("database", "settings", "api_error", "get_video_metadata", "fetch_video_batch_stats",
                     "build_full_payload", "build_counter_payload", "is_quota_exhausted"):
            with self.subTest(null=name), self.assertRaises(TypeError):
                runtime.video_collection_dependencies(**{**supplied, name: None})
        for invalid in (RuntimeError("instance"), object, "RuntimeError"):
            with self.subTest(api_error=invalid), self.assertRaises(TypeError):
                runtime.video_collection_dependencies(**{**supplied, "api_error": invalid})
        for name in ("get_video_metadata", "fetch_video_batch_stats", "build_full_payload", "build_counter_payload"):
            supplied[name].assert_not_called()

    def test_binder_requires_dependencies_without_creating_resources_or_calling_owners(self):
        supplied = binder_inputs()
        with patch.object(video_execution, "run_video_collection_queue_batch") as video, \
             patch.object(initial, "collect_initial_inventory_chunk") as inventory, \
             patch.object(initial, "initial_28d_stats_import") as stats:
            for name in supplied:
                # Legacy manual binders may omit the initial-only provider;
                # production composition always supplies it.
                if name == "fetch_initial_profiles":
                    continue
                with self.subTest(name=name):
                    missing = dict(supplied)
                    del missing[name]
                    with self.assertRaises(TypeError):
                        runtime.bind_collection_callbacks(**missing)
                    with self.assertRaises(TypeError):
                        runtime.bind_collection_callbacks(**{**supplied, name: None})
            callbacks = runtime.bind_collection_callbacks(**supplied)
            self.assertEqual(set(callbacks), {"run_stats", "run_initial_inventory_chunk", "run_initial_stats"})
            video.assert_not_called()
            inventory.assert_not_called()
            stats.assert_not_called()
        supplied["database"].assert_not_called()
        supplied["database"].close.assert_not_called()
        supplied["worker_id_factory"].assert_not_called()
        supplied["fetch_page"].assert_not_called()

    def test_bound_callbacks_reject_scope_and_dependency_overrides_before_owner(self):
        supplied = binder_inputs()
        with patch.object(video_execution, "run_video_collection_queue_batch") as video, \
             patch.object(initial, "collect_initial_inventory_chunk") as inventory, \
             patch.object(initial, "initial_28d_stats_import") as stats:
            callbacks = runtime.bind_collection_callbacks(**supplied)
            for name, args in (("run_stats", ()), ("run_initial_inventory_chunk", ("channel",)), ("run_initial_stats", ("channel",))):
                callback = callbacks[name]
                scope = {} if name == "run_stats" else {"expected_epoch": 7}
                for override in ({"database": object()}, {"database": None},
                                 {"settings": CollectionSettings(True, True, True)}, {"settings": None}):
                    with self.subTest(callback=name, override=tuple(override)), self.assertRaises(ValueError):
                        callback(*args, **scope, **override)
                for dependency in set(supplied) - {"database", "settings"}:
                    with self.subTest(callback=name, dependency=dependency), self.assertRaises(TypeError):
                        callback(*args, **scope, **{dependency: Mock()})
            video.assert_not_called()
            inventory.assert_not_called()
            stats.assert_not_called()
        supplied["worker_id_factory"].assert_not_called()
        supplied["fetch_page"].assert_not_called()

    def test_callbacks_reach_actual_owners_and_keep_public_operational_arguments(self):
        supplied = binder_inputs()
        repository = Mock(load_recent_long_inventory=Mock(return_value=[]), update_process_state=Mock(return_value=True))
        with patch.object(video_execution, "run_video_collection_queue_batch", wraps=video_execution.run_video_collection_queue_batch) as video, \
             patch.object(initial, "collect_initial_inventory_chunk", wraps=initial.collect_initial_inventory_chunk) as inventory, \
             patch.object(initial, "initial_28d_stats_import", wraps=initial.initial_28d_stats_import) as stats, \
             patch.object(initial, "ChannelSyncRepository", return_value=repository) as repository_factory, \
             patch.object(lifecycle, "ChannelSyncRepository", new=repository_factory), \
             patch.object(initial, "source_log_message", return_value="offline"), patch("sys.stdout", new=io.StringIO()):
            callbacks = runtime.bind_collection_callbacks(**supplied)
            repository_factory.assert_not_called()
            result = callbacks["run_stats"](limit=0, explicit_video_ids=[], force_metadata=True, reason="bounded")
            self.assertEqual(result["requested"], 0)
            self.assertEqual(callbacks["run_initial_inventory_chunk"]("channel", video_type="short", max_pages=0, expected_epoch=7),
                             {"pages": 0, "items": 0, "outcome": "stored_inventory"})
            self.assertEqual(callbacks["run_initial_stats"]("channel", expected_epoch=7)["status"], "skipped_no_recent_videos")
        self.assertEqual((video.call_count, inventory.call_count, stats.call_count), (1, 1, 1))
        for owner in (video, inventory, stats):
            self.assertIs(owner.call_args.kwargs["database"], supplied["database"])
            self.assertIs(owner.call_args.kwargs["settings"], supplied["settings"])
        self.assertIs(video.call_args.kwargs["fetch_metadata"], supplied["fetch_metadata"])
        self.assertIs(inventory.call_args.kwargs["playlist_not_found_error"], MissingPlaylist)
        self.assertEqual(inventory.call_args.kwargs["worker_id"], "worker:typed-inventory")
        self.assertEqual(supplied["worker_id_factory"].call_count, 2)
        supplied["database"].close.assert_not_called()
        supplied["fetch_page"].assert_not_called()

    def test_legacy_statistics_uses_same_guarded_video_callback(self):
        supplied = binder_inputs()
        captured = {}
        outcome = {"settlement_unknown": True}
        def initial_stats(channel, **arguments):
            captured.update(arguments)
            return arguments["run_video_collection_queue_batch"](
                limit=1, explicit_video_ids=["video"], reason="initial_28d_import", force_metadata=True,
                database=arguments["database"], settings=arguments["settings"])
        with patch.object(video_execution, "run_video_collection_queue_batch", return_value=outcome) as video, \
             patch.object(initial, "initial_28d_stats_import", side_effect=initial_stats):
            callbacks = runtime.bind_collection_callbacks(**supplied)
            self.assertIs(callbacks["run_initial_stats"]("channel", expected_epoch=7), outcome)
            self.assertIs(captured["run_video_collection_queue_batch"], callbacks["run_stats"])
            self.assertEqual(video.call_args.kwargs["reason"], "initial_28d_import")
            self.assertIs(video.call_args.kwargs["database"], supplied["database"])
            video.reset_mock()
            with self.assertRaises(ValueError):
                captured["run_video_collection_queue_batch"](database=object(), settings=supplied["settings"])
            video.assert_not_called()

    def test_two_bound_scopes_keep_their_own_gateway_and_accept_only_identical_explicit_scope(self):
        first, second = binder_inputs(), binder_inputs()
        with patch.object(video_execution, "run_video_collection_queue_batch", return_value={}) as video:
            callbacks_a, callbacks_b = runtime.bind_collection_callbacks(**first), runtime.bind_collection_callbacks(**second)
            callbacks_b["run_stats"](limit=3, database=second["database"], settings=second["settings"])
            callbacks_a["run_stats"](limit=2)
        self.assertIs(video.call_args_list[0].kwargs["database"], second["database"])
        self.assertIs(video.call_args_list[1].kwargs["database"], first["database"])
        self.assertIs(video.call_args_list[0].kwargs["settings"], second["settings"])
        self.assertIs(video.call_args_list[1].kwargs["settings"], first["settings"])


if __name__ == "__main__":
    unittest.main()
