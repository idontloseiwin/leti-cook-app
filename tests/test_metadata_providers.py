"""Fixed-provider metadata contracts.

No application bootstrap, provider request or database is started.
"""
import ast
import copy
import inspect
import itertools
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
sys.path.insert(0, str(APPLICATION))
import youtube_channels
from test_collection_provider_boundary import process_provider
from youtube_errors import YouTubeAPIError, youtube_error_message
from youtube_source_fields import parse_dt
from youtube_video_profiles import fetch_video_metadata

RECORDER = object()
VIDEO = "video000001"
CHANNEL = "UC" + "a" * 22
DEPS = ("youtube_api_get", "api_key", "attempt_recorder", "timeout_seconds", "emit_event", "perf_counter")


def definition(source, name, namespace):
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), name, "exec"), namespace)
    return namespace[name]


def compatibility(kind):
    if kind == "video":
        namespace = {"fetch_video_metadata": fetch_video_metadata}
        definition((APPLICATION / "core.py").read_text(encoding="utf-8"), "get_video_metadata", namespace)
        return namespace
    namespace = {}
    def channel(ids):
        return process_provider("fetch_channel_metadata_batch")(
            ids, youtube_api_get=namespace["youtube_api_get"], api_key=namespace["API_KEY"],
            attempt_recorder=namespace["YOUTUBE_QUOTA_ATTEMPT_RECORDER"],
            timeout_seconds=namespace["YOUTUBE_API_TIMEOUT_SECONDS"], emit_event=namespace["emit_event"],
            perf_counter=namespace["time"].perf_counter, max_results=namespace["CHANNEL_METADATA_MAX_IDS_PER_REQUEST"])
    namespace["fetch_channel_metadata_batch"] = channel
    return namespace


def exercise(kind, ids, bodies=(), *, namespace=None, reject=True, http_error=None,
             transport_error=None, max_results=50):
    trace, pending = [], iter(copy.deepcopy(bodies))
    clock = itertools.count(0, .01)

    class Response:
        def __init__(self, body):
            self.body = body
        def json(self):
            trace.append(("json",))
            if isinstance(self.body, Exception):
                raise self.body
            return self.body
        def raise_for_status(self):
            trace.append(("http_status",))
            if http_error:
                raise http_error

    def transport(endpoint, **kwargs):
        trace.append(("request", endpoint, kwargs))
        if transport_error:
            raise transport_error
        return Response(next(pending))

    def emit(*args, **kwargs):
        trace.append(("event", args, kwargs))

    dependencies = dict(youtube_api_get=transport, api_key="fixed-key", attempt_recorder=RECORDER,
                        timeout_seconds=17, emit_event=emit, perf_counter=lambda: next(clock))
    options = dict(phase="fixed_metadata", reject_identity_anomalies=reject) if kind == "video" else {"max_results": max_results}
    if namespace is None:
        fetch = fetch_video_metadata if kind == "video" else youtube_channels.fetch_channel_metadata_batch
        options.update(dependencies)
    else:
        namespace.update(youtube_api_get=transport, API_KEY="fixed-key", YOUTUBE_QUOTA_ATTEMPT_RECORDER=RECORDER,
                         YOUTUBE_API_TIMEOUT_SECONDS=17, emit_event=emit, time=SimpleNamespace(perf_counter=dependencies["perf_counter"]),
                         CHANNEL_METADATA_MAX_IDS_PER_REQUEST=max_results)
        fetch = namespace["get_video_metadata" if kind == "video" else "fetch_channel_metadata_batch"]
        if kind == "channel":
            options = {}
    try:
        outcome = ("value", fetch(ids, **options))
    except Exception as error:
        outcome = ("error", type(error), str(error))
    return outcome, trace


class MetadataProviderTests(unittest.TestCase):
    def test_explicit_dependencies_empty_and_invalid_ids_do_not_send_requests(self):
        for fetch in (fetch_video_metadata, youtube_channels.fetch_channel_metadata_batch):
            parameters = inspect.signature(fetch).parameters
            for dependency in DEPS:
                self.assertIs(parameters[dependency].kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameters[dependency].default, inspect.Parameter.empty)
            self.assertEqual(fetch([], **dict.fromkeys(DEPS)), [])
            with self.assertRaises(TypeError):
                fetch([])
        for ids in (["bad "], [""], [123]):
            outcome, trace = exercise("video", ids)
            self.assertEqual(outcome, ("error", YouTubeAPIError, "Video request identities are invalid"))
            self.assertEqual(trace, [])

    def test_metadata_deduplicates_in_order_splits_at_fifty_and_discards_general_counters(self):
        ids = [f"video{i:06d}" for i in range(51)]
        bodies = [{"items": [{"id": value, "statistics": {"viewCount": "999"}, "snippet": {"title": value}}
                             for value in ids[:50]]}, {"items": [{"id": ids[50]}]}]
        original = copy.deepcopy(bodies)
        outcome, trace = exercise("video", ids + ids[:2], bodies)
        self.assertEqual(outcome[0], "value")
        self.assertEqual([item["id"] for item in outcome[1]], ids)
        self.assertTrue(all("statistics" not in item for item in outcome[1]))
        self.assertEqual(bodies, original)
        requests = [entry for entry in trace if entry[0] == "request"]
        self.assertEqual([entry[2]["params"]["id"] for entry in requests], [",".join(ids[:50]), ids[50]])
        self.assertTrue(all(entry[2]["attempt_recorder"] is RECORDER for entry in requests))
        self.assertEqual(requests[0][2], dict(phase="fixed_metadata", quota_bucket="general", attempt_recorder=RECORDER,
            params={"part": "snippet,contentDetails,status,topicDetails,liveStreamingDetails", "id": ",".join(ids[:50])},
            api_key="fixed-key", timeout_seconds=17, quota_units=1))

    def test_strict_and_per_item_modes_keep_anomalies_and_error_identity(self):
        body = {"items": [{"id": VIDEO, "statistics": {}}, {"id": VIDEO}, {"id": "other"}, "junk"]}
        strict, _ = exercise("video", [VIDEO], [body])
        self.assertEqual(strict, ("error", YouTubeAPIError, "Video metadata response identities are invalid"))
        permissive, _ = exercise("video", [VIDEO], [body], reject=False)
        self.assertEqual(permissive, ("value", [{"id": VIDEO}, {"id": VIDEO}, {"id": "other"}, "junk"]))

    def test_channel_fetch_keeps_single_request_fields_ids_and_limit_binding(self):
        body = {"items": [{"id": CHANNEL, "statistics": {"viewCount": "2"}}]}
        outcome, trace = exercise("channel", [CHANNEL, CHANNEL], [body], max_results=17)
        self.assertEqual(outcome, ("value", body["items"]))
        request = [entry for entry in trace if entry[0] == "request"]
        self.assertEqual(len(request), 1)
        self.assertEqual(request[0][1], "channels")
        self.assertEqual(request[0][2]["params"], dict(part="snippet,statistics,contentDetails,brandingSettings",
                                                        id=CHANNEL + "," + CHANNEL, maxResults=17))
        self.assertEqual(request[0][2]["phase"], "channel_metadata")
        self.assertEqual(youtube_channels.CHANNEL_METADATA_MAX_IDS_PER_REQUEST, 50)

    def test_core_wrapper_and_process_channel_binding_capture_supplied_resources(self):
        for kind, name in (("video", "get_video_metadata"), ("channel", "fetch_channel_metadata_batch")):
            namespace = compatibility(kind)
            for index in range(2):
                recorder = object()
                transport = Mock(return_value=SimpleNamespace(json=lambda: {"items": []}, raise_for_status=Mock()))
                namespace.update(youtube_api_get=transport, API_KEY=f"fixed-key-{index}",
                    YOUTUBE_QUOTA_ATTEMPT_RECORDER=recorder, YOUTUBE_API_TIMEOUT_SECONDS=11+index,
                    emit_event=Mock(), time=SimpleNamespace(perf_counter=lambda: 0), CHANNEL_METADATA_MAX_IDS_PER_REQUEST=17+index)
                namespace[name]([VIDEO if kind == "video" else CHANNEL])
                self.assertEqual(transport.call_count, 1)
                self.assertIs(transport.call_args.kwargs["attempt_recorder"], recorder)
                self.assertEqual(transport.call_args.kwargs["api_key"], f"fixed-key-{index}")
                self.assertEqual(transport.call_args.kwargs["timeout_seconds"], 11+index)
                if kind == "channel":
                    self.assertEqual(transport.call_args.kwargs["params"]["maxResults"], 17+index)

    def test_parser_keeps_offsets_fraction_precision_naive_values_and_failure_contract(self):
        for value in (None, "", " ", 0, "nonsense", "2026-99-99"):
            self.assertIsNone(parse_dt(value))
        for suffix in ("+03", "+0300", "+03:00"):
            result = parse_dt("2026-09-27T10:00:00.123456789" + suffix)
            self.assertEqual(result.isoformat(), "2026-09-27T10:00:00.123456+03:00")
        self.assertEqual(parse_dt("2026-09-27T10:00:00.1Z").isoformat(), "2026-09-27T10:00:00.100000+00:00")
        self.assertIsNone(parse_dt("2026-09-27T10:00:00").tzinfo)
        tree = ast.parse((APPLICATION / "core.py").read_text(encoding="utf-8"))
        imports = [alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                   and node.module == "youtube_source_fields" for alias in node.names]
        self.assertIn("parse_dt", imports)
        self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "parse_dt" for node in tree.body))

    def test_all_provider_and_parser_imports_are_independent_of_bootstrap_and_environment(self):
        script = r'''
import builtins, os, re, datetime, typing, sys
sys.path.insert(0, APPLICATION)
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'core','sync_service','server','db','dotenv','requests','httpx','telemetry'}:
        raise AssertionError('provider bootstrap: '+name)
    return original(name,*args,**kwargs)
def forbidden(*args,**kwargs):
    raise AssertionError('environment read')
class Environment(dict):
    get=__getitem__=forbidden
os.environ=Environment()
os.getenv=forbidden
builtins.__import__=guarded
from youtube_video_profiles import fetch_video_metadata
from youtube_channels import fetch_channel_metadata_batch
from youtube_source_fields import parse_dt
'''.replace("APPLICATION", repr(str(APPLICATION)), 1)
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
