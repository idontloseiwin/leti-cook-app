"""Offline full-profile provider contracts and an opt-in frozen-source comparison."""

import ast
from copy import deepcopy
import inspect
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
if str(APPLICATION) not in sys.path:
    sys.path.insert(0, str(APPLICATION))
from youtube_video_profiles import fetch_video_profiles


class APIError(RuntimeError):
    pass


def load_core_fetch(path, dependencies):
    """Execute the exact public function without running core's bootstrap."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "get_video_stats")
    scope = {
        "fetch_video_profiles": fetch_video_profiles,
        "YOUTUBE_BATCH_STATS_ENABLED": dependencies["batch_stats_enabled"],
        "get_video_metadata": dependencies["get_video_metadata"],
        "fetch_video_batch_stats": dependencies["fetch_video_batch_stats"],
        "API_KEY": dependencies["api_key"],
        "YOUTUBE_QUOTA_ATTEMPT_RECORDER": dependencies["attempt_recorder"],
        "YOUTUBE_API_TIMEOUT_SECONDS": dependencies["timeout_seconds"],
        "BATCH_STATS_COUNTER_PARTS": dependencies["counter_parts"],
        "YouTubeAPIError": dependencies["api_error"],
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), scope)
    return scope["get_video_stats"], scope


def dependencies():
    return {
        "batch_stats_enabled": True,
        "get_video_metadata": Mock(return_value=[{"id": "video"}]),
        "fetch_video_batch_stats": Mock(return_value={
            "items": [{"id": "video", "views": 12, "like_count": 0, "comment_count": None}],
            "summary": {"failed_ids": []},
        }),
        "api_key": "offline-test-key",
        "attempt_recorder": Mock(name="process_recorder"),
        "timeout_seconds": 17,
        "counter_parts": ("views", "likes", "comments"),
        "api_error": APIError,
    }


class VideoProfileTests(unittest.TestCase):
    def test_import_is_pure_and_cannot_bootstrap_or_read_environment(self):
        tree = ast.parse((APPLICATION / "youtube_video_profiles.py").read_text(encoding="utf-8"))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imports, {"collections.abc", "youtube_errors", "youtube_response_trace"})
        self.assertFalse(any(isinstance(node, ast.Import) for node in ast.walk(tree)))
        script = """
import builtins, os, socket, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'core', 'sync_service', 'server', 'db', 'dotenv'}:
        raise AssertionError('unexpected bootstrap: ' + name)
    return original(name, *args, **kwargs)
with patch('builtins.__import__', side_effect=guarded), \
     patch('os.getenv', side_effect=AssertionError('environment read')), \
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network')):
    from youtube_video_profiles import fetch_video_profiles
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(APPLICATION)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_input_returns_before_gate_and_unused_dependency_validation(self):
        supplied = dict.fromkeys(dependencies())
        self.assertEqual(fetch_video_profiles(iter(()), **supplied), [])
        supplied = dependencies()
        supplied["batch_stats_enabled"] = False
        legacy, _ = load_core_fetch(APPLICATION / "core.py", supplied)
        self.assertEqual(legacy([]), [])
        supplied["get_video_metadata"].assert_not_called()
        supplied["fetch_video_batch_stats"].assert_not_called()

    def test_all_dependencies_are_mandatory_keyword_arguments(self):
        supplied = dependencies()
        signature = inspect.signature(fetch_video_profiles)
        for name in supplied:
            with self.subTest(name=name):
                parameter = signature.parameters[name]
                self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameter.default, inspect.Parameter.empty)
                missing = {key: value for key, value in supplied.items() if key != name}
                with self.assertRaises(TypeError):
                    fetch_video_profiles(["video"], **missing)
        supplied["get_video_metadata"].assert_not_called()
        supplied["fetch_video_batch_stats"].assert_not_called()

    def test_invalid_boundary_dependencies_fail_before_any_provider_action(self):
        supplied = dependencies()
        invalid = {
            "batch_stats_enabled": (None, 1, "true"),
            "get_video_metadata": (None, object()),
            "fetch_video_batch_stats": (None, object()),
            "api_error": (None, APIError("instance"), object, BaseException),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(TypeError):
                    fetch_video_profiles(["video"], **{**supplied, name: value})
        supplied["get_video_metadata"].assert_not_called()
        supplied["fetch_video_batch_stats"].assert_not_called()

    def test_disabled_gate_precedes_request_identity_validation(self):
        supplied = dependencies()
        supplied["batch_stats_enabled"] = False
        with self.assertRaisesRegex(APIError, "^Batch video statistics is disabled$"):
            fetch_video_profiles([None], **supplied)
        supplied["get_video_metadata"].assert_not_called()
        supplied["fetch_video_batch_stats"].assert_not_called()

    def test_invalid_request_identities_fail_without_provider_calls(self):
        supplied = dependencies()
        for video_id in (None, "", " video", "video ", 7):
            with self.subTest(video_id=video_id):
                with self.assertRaisesRegex(APIError, "^Video request identities are invalid$"):
                    fetch_video_profiles([video_id], **supplied)
        supplied["get_video_metadata"].assert_not_called()
        supplied["fetch_video_batch_stats"].assert_not_called()

    def test_deduplication_batches_fifty_in_order_and_preserves_provider_resources(self):
        supplied = dependencies()
        events = []
        ids = [f"video-{index}" for index in range(103)]

        def metadata(video_ids, **kwargs):
            self.assertEqual(kwargs, {"phase": "full-phase"})
            events.append(("metadata", list(video_ids)))
            return [{"id": video_id} for video_id in video_ids]

        def counters(video_ids, **kwargs):
            self.assertEqual(kwargs, {
                "api_key": supplied["api_key"], "attempt_recorder": supplied["attempt_recorder"],
                "timeout_seconds": 17, "phase": "full-phase", "parts": supplied["counter_parts"],
            })
            self.assertIs(kwargs["attempt_recorder"], supplied["attempt_recorder"])
            self.assertIs(kwargs["parts"], supplied["counter_parts"])
            events.append(("counters", list(video_ids)))
            return {"items": [{"id": video_id, "views": 0} for video_id in video_ids],
                    "summary": {"failed_ids": []}}

        supplied.update(get_video_metadata=metadata, fetch_video_batch_stats=counters)
        result = fetch_video_profiles(iter(ids[:2] + ids + ids[-1:]), phase="full-phase", **supplied)
        self.assertEqual([video["id"] for video in result], ids)
        self.assertEqual(events, [(kind, ids[start:start + 50])
                                  for start in (0, 50, 100) for kind in ("metadata", "counters")])
        self.assertTrue(all(video["statistics"] == {"viewCount": 0, "likeCount": None, "commentCount": None}
                            for video in result))
        supplied["attempt_recorder"].close.assert_not_called()

    def test_empty_metadata_skips_batch_without_inventing_missing_videos(self):
        supplied = dependencies()
        supplied["get_video_metadata"].return_value = []
        self.assertEqual(fetch_video_profiles(["video"], **supplied), [])
        supplied["get_video_metadata"].assert_called_once_with(["video"], phase="video_stats")
        supplied["fetch_video_batch_stats"].assert_not_called()

    def test_failed_or_missing_counters_never_use_unsolicited_general_statistics(self):
        supplied = dependencies()
        metadata = [{"id": video_id, "statistics": {"viewCount": 999}}
                    for video_id in ("good", "failed", "missing")]
        original = deepcopy(metadata)
        supplied["get_video_metadata"].return_value = metadata
        supplied["fetch_video_batch_stats"].return_value = {
            "items": [{"id": "good", "views": 0, "like_count": 2, "comment_count": None},
                      {"id": "failed", "views": 1000}],
            "summary": {"failed_ids": ["failed"]},
        }
        result = fetch_video_profiles(["good", "failed", "missing"], **supplied)
        self.assertEqual([item["id"] for item in result], ["good", "failed", "missing"])
        self.assertEqual([item["statistics"] for item in result], [
            {"viewCount": 0, "likeCount": 2, "commentCount": None}, {}, {},
        ])
        self.assertEqual(metadata, original)
        self.assertIsNot(result[0], metadata[0])

    def test_metadata_exception_identity_is_preserved_without_batch_or_retry(self):
        supplied = dependencies()
        error = APIError("strict metadata identity refusal")
        supplied["get_video_metadata"].side_effect = error
        with self.assertRaises(APIError) as caught:
            fetch_video_profiles(["video"], **supplied)
        self.assertIs(caught.exception, error)
        self.assertEqual(supplied["get_video_metadata"].call_count, 1)
        supplied["fetch_video_batch_stats"].assert_not_called()

    def test_batch_exception_identity_is_preserved_without_retry_or_general_fallback(self):
        supplied = dependencies()
        error = APIError("Batch unavailable")
        supplied["fetch_video_batch_stats"].side_effect = error
        supplied["get_video_metadata"].return_value = [{"id": "video", "statistics": {"viewCount": 999}}]
        with self.assertRaises(APIError) as caught:
            fetch_video_profiles(["video"], **supplied)
        self.assertIs(caught.exception, error)
        self.assertEqual(supplied["get_video_metadata"].call_count, 1)
        self.assertEqual(supplied["fetch_video_batch_stats"].call_count, 1)
        supplied["attempt_recorder"].close.assert_not_called()

    def test_legacy_public_wrapper_keeps_signature_and_reads_current_process_bindings(self):
        supplied = dependencies()
        legacy, scope = load_core_fetch(APPLICATION / "core.py", supplied)
        self.assertEqual(str(inspect.signature(legacy)), "(video_ids, phase='video_stats')")
        scope["YOUTUBE_BATCH_STATS_ENABLED"] = False
        with self.assertRaisesRegex(APIError, "Batch video statistics is disabled"):
            legacy(["video"])
        supplied["get_video_metadata"].assert_not_called()
        scope["YOUTUBE_BATCH_STATS_ENABLED"] = True
        replacement = Mock(return_value=[{"id": "video"}])
        recorder = Mock(name="replacement_process_recorder")
        scope.update(get_video_metadata=replacement, YOUTUBE_QUOTA_ATTEMPT_RECORDER=recorder)
        result = legacy(["video"], phase="legacy-phase")
        self.assertEqual(result[0]["statistics"]["viewCount"], 12)
        replacement.assert_called_once_with(["video"], phase="legacy-phase")
        supplied["get_video_metadata"].assert_not_called()
        supplied["fetch_video_batch_stats"].assert_called_once_with(
            ["video"], api_key=supplied["api_key"], attempt_recorder=recorder,
            timeout_seconds=17, phase="legacy-phase", parts=supplied["counter_parts"],
        )
        recorder.close.assert_not_called()



if __name__ == "__main__":
    unittest.main()
