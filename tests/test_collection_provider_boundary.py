"""Offline provider boundary.

All transport, telemetry and clocks are fixed callables; no provider, server,
database, quota recorder or application bootstrap is started.
"""
import copy
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
sys.path.insert(0, str(APPLICATION))
import youtube_errors as errors
import youtube_playlist as playlist
from collection_outcome import copy_collection_stop
from runtime.collection import video_collection_worker_id

PLAYLIST = "UULF" + "a" * 22
PAGE = {"items": [{"contentDetails": {"videoId": "video000001"}}], "nextPageToken": "next"}
RECORDER = object()


def refusal(reason, *, message="Refused", status="PERMISSION_DENIED"):
    return {"error": {"message": message, "status": status, "errors": [{"reason": reason}]}}


def execute(fetch, *, namespace=None, payload=PAGE, transport_error=None, json_error=None,
            page_token="next", max_results=7, phase="fixed_inventory"):
    response = Mock()
    response.status_code = (404 if "playlistNotFound" in repr(payload) else 403) if payload.get("error") else 200
    response.headers = {}
    response.json.side_effect = json_error
    response.json.return_value = copy.deepcopy(payload)
    transport = Mock(side_effect=transport_error, return_value=response)
    emit = Mock()
    clock = Mock(side_effect=[100.0, 100.012])
    supplied = dict(youtube_api_get=transport, api_key="synthetic-key", attempt_recorder=RECORDER,
                    timeout_seconds=17, emit_event=emit, perf_counter=clock)
    if namespace is not None:
        namespace.update(youtube_api_get=transport, API_KEY="synthetic-key", YOUTUBE_QUOTA_ATTEMPT_RECORDER=RECORDER,
                         YOUTUBE_API_TIMEOUT_SECONDS=17, emit_event=emit, time=SimpleNamespace(perf_counter=clock))
        supplied = {}
    try:
        outcome = ("value", fetch(PLAYLIST, page_token, max_results, phase, **supplied))
    except Exception as error:
        outcome = ("error", type(error).__name__, str(error), isinstance(error, RuntimeError))
    return outcome, transport, emit, response


def process_provider(name):
    """Exercise the actual process binder with fixed resources, without owning a DB."""
    from collection.settings import CollectionSettings
    from collection.context import CollectionPolicy
    from runtime import process_collection as process

    def fetch(*args, **kwargs):
        supplied = {key: kwargs.pop(key) for key in (
            "youtube_api_get", "api_key", "attempt_recorder", "timeout_seconds", "emit_event", "perf_counter")}
        core = ModuleType("core")
        core.database = object()
        core.API_KEY, core.YOUTUBE_QUOTA_ATTEMPT_RECORDER = supplied["api_key"], supplied["attempt_recorder"]
        core.YOUTUBE_API_TIMEOUT_SECONDS = supplied["timeout_seconds"]
        with patch.dict(sys.modules, {"core": core, "sync_service": None}), \
                patch.object(process, "youtube_api_get", supplied["youtube_api_get"]), \
                patch.object(process, "emit_event", supplied["emit_event"]), \
                patch.object(process.time, "perf_counter", supplied["perf_counter"]):
            context = process.bind_process_collection(database=object(), settings=CollectionSettings(True, True, True),
                policy=CollectionPolicy(True, True, True, 24))
            return getattr(context.sources, name)(*args, **kwargs)
    return fetch


class PlaylistBoundaryTests(unittest.TestCase):
    def test_one_request_uses_explicit_resources_and_preserves_page_and_telemetry(self):
        outcome, transport, emit, response = execute(playlist.fetch_playlist_items_page)
        self.assertIs(outcome[1], response.json.return_value)
        transport.assert_called_once_with("playlistItems", phase="fixed_inventory", quota_bucket="general",
            attempt_recorder=RECORDER, params={"part": "contentDetails", "playlistId": PLAYLIST,
            "maxResults": 7, "pageToken": "next"}, api_key="synthetic-key", timeout_seconds=17, quota_units=1)
        response.json.assert_called_once_with()
        self.assertEqual(emit.call_args_list[0].args, ("api", "sync_engine", "youtube_api", "playlistItems.list"))
        self.assertEqual(emit.call_args_list[0].kwargs,
                         dict(direction="request", status="started", count=7, quota=1))
        self.assertEqual(emit.call_args_list[1].args, ("api", "youtube_api", "sync_engine", "playlistItems.list"))
        self.assertEqual(emit.call_args_list[1].kwargs,
                         dict(direction="response", status="ok", count=1, quota=1, duration_ms=12))
        _, transport, _, _ = execute(playlist.fetch_playlist_items_page, page_token="")
        self.assertNotIn("pageToken", transport.call_args.kwargs["params"])

    def test_dependencies_have_no_implicit_process_defaults(self):
        with self.assertRaises(TypeError):
            playlist.fetch_playlist_items_page(PLAYLIST)
        with self.assertRaises(TypeError):
            errors.is_youtube_daily_quota_exhausted(RuntimeError("ordinary"))
        with self.assertRaises(TypeError):
            video_collection_worker_id()

    def test_daily_rate_and_absence_remain_separate(self):
        for reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"):
            expected = ("YouTubeDailyQuotaExceededError" if reason in ("quotaExceeded", "dailyLimitExceeded")
                        else "YouTubeRateLimitError")
            outcome, transport, emit, _ = execute(playlist.fetch_playlist_items_page, payload=refusal(reason))
            self.assertEqual(outcome[:2], ("error", expected))
            transport.assert_called_once()
            self.assertEqual(emit.call_args.kwargs["detail"], expected)
        self.assertEqual(execute(playlist.fetch_playlist_items_page, payload=refusal("playlistNotFound"))[0][:2],
                         ("error", "YouTubePlaylistNotFoundError"))
        # Explicit daily evidence wins over misleading message text.
        self.assertEqual(execute(playlist.fetch_playlist_items_page,
                         payload=refusal("quotaExceeded", message="The playlist cannot be found"))[0][:2],
                         ("error", "YouTubeDailyQuotaExceededError"))
        self.assertEqual(execute(playlist.fetch_playlist_items_page, payload=refusal("accessNotConfigured"))[0][:2],
                         ("error", "YouTubeAPIError"))
        self.assertTrue(issubclass(errors.YouTubeRateLimitError, errors.YouTubeAPIError))
        self.assertTrue(issubclass(errors.YouTubeDailyQuotaExceededError, errors.YouTubeAPIError))
        self.assertFalse(issubclass(errors.YouTubePlaylistNotFoundError, errors.YouTubeAPIError))

    def test_transport_and_json_failures_propagate_once_with_error_telemetry(self):
        for option, error in (("transport_error", TimeoutError("fixed timeout")),
                              ("json_error", ValueError("fixed invalid json"))):
            outcome, transport, emit, response = execute(playlist.fetch_playlist_items_page, **{option: error})
            self.assertEqual(outcome[:3], ("error", type(error).__name__, str(error)))
            transport.assert_called_once()
            self.assertEqual(response.json.call_count, int(option == "json_error"))
            self.assertEqual(emit.call_args.kwargs,
                             dict(direction="response", status="error", count=0, quota=1,
                                  duration_ms=12, detail=type(error).__name__))

    def test_quota_classifier_keeps_batch_predicate_and_reason_semantics(self):
        batch = Mock(return_value=False)
        for error in (RuntimeError("reason=quotaExceeded"), RuntimeError("reason=dailyLimitExceeded"),
                      errors.YouTubeDailyQuotaExceededError("without provider text")):
            self.assertTrue(errors.is_youtube_daily_quota_exhausted(error, is_batch_stats_quota_error=batch))
        batch.assert_not_called()
        marker = RuntimeError("fixed batch refusal")
        batch.return_value = True
        self.assertTrue(errors.is_youtube_daily_quota_exhausted(marker, is_batch_stats_quota_error=batch))
        batch.assert_called_once_with(marker)
        self.assertEqual(errors.youtube_error_message(refusal("accessNotConfigured")),
                         "Refused | status=PERMISSION_DENIED | reason=accessNotConfigured")

    def test_stop_merge_preserves_flags_priority_bound_and_excludes_raw_error(self):
        target = {"updated": 3, "settlement_unknown": True}
        source = {"batch_quota_exhausted": True, "error": "raw provider error"}
        self.assertTrue(copy_collection_stop(target, source))
        self.assertEqual(target["stopped_reason"], "settlement_unknown")
        self.assertTrue(target["settlement_unknown"])
        self.assertTrue(target["batch_quota_exhausted"])
        self.assertEqual(target["updated"], 3)
        self.assertNotIn("error", target)
        self.assertNotIn("stopped_reason", source)
        self.assertTrue(copy_collection_stop(target, {"infrastructure_error": True, "stopped_reason": "x" * 100}))
        self.assertEqual(target["stopped_reason"], "x" * 80)
        yielded = {}
        self.assertTrue(copy_collection_stop(yielded, {"yield_requested": True}))
        self.assertEqual(yielded, dict(yield_requested=True, stopped_reason="scheduled_handoff"))
        self.assertFalse(copy_collection_stop({}, {"errors": 1}))

    def test_worker_identity_uses_only_supplied_process_readers(self):
        for hostname, expected in ((" machine ", "machine:123"), ("", "worker:123"), ("  ", "worker:123")):
            get_hostname, get_pid = Mock(return_value=hostname), Mock(return_value=123)
            self.assertEqual(video_collection_worker_id(get_hostname=get_hostname, get_pid=get_pid), expected)
            get_hostname.assert_called_once_with()
            get_pid.assert_called_once_with()

    def test_provider_import_needs_no_bootstrap_database_or_environment(self):
        code = r'''
import builtins, os, sys
sys.path.insert(0, APPLICATION)
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'core', 'sync_service', 'server', 'db', 'dotenv', 'requests', 'httpx', 'telemetry'}:
        raise AssertionError('forbidden provider import: ' + name)
    return original(name, *args, **kwargs)
def forbidden(*args, **kwargs):
    raise AssertionError('provider read process environment')
class Environment(dict):
    get = __getitem__ = forbidden
os.environ = Environment()
os.getenv = forbidden
builtins.__import__ = guard
import youtube_errors, youtube_playlist
assert youtube_playlist.YouTubeAPIError is youtube_errors.YouTubeAPIError
'''.replace("APPLICATION", repr(str(APPLICATION)), 1)
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
