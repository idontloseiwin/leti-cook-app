"""Scoped collection dependencies through real callbacks and direct SQL recorders.

No core bootstrap, dotenv, provider request, database server, or worker process
is started. The process-owned provider adapters deliberately remain separate
from repositories borrowing the bounded process database pool.
"""

import builtins
from contextlib import ExitStack, nullcontext, contextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
if str(APPLICATION) not in sys.path:
    sys.path.insert(0, str(APPLICATION))

from collection import initial, video_execution, video_payloads
from youtube_errors import YouTubeAPIError, youtube_error_message
from collection_schedule import CollectionSchedules, ScheduleError


CHANNEL = "UC" + "a" * 22
VIDEO = "video000001"
NOW = "2026-09-27T10:00:00+00:00"
ENABLED = {
    "DATABASE_VIDEO_COLLECTION_ENABLED": "true",
    "TYPED_PLAYLIST_INVENTORY_ENABLED": "true",
    "YOUTUBE_BATCH_STATS_ENABLED": "true",
}


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, APPLICATION / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def forbidden_database(*args, **kwargs):
    raise AssertionError("collection escaped to the process database")


def core_stub():
    core = ModuleType("core")
    core.COLLECTION_SCHEDULES = CollectionSchedules()
    core.database = SimpleNamespace(call=forbidden_database, read_rows=forbidden_database,
                                    read_row=forbidden_database, transaction=forbidden_database,
                                    claim_inventory_channels=forbidden_database)
    core.process_database_resources = lambda: nullcontext(core.database)
    core.API_KEY = "local-test-provider-key"
    core.YOUTUBE_QUOTA_ATTEMPT_RECORDER = Mock(name="process_quota_recorder")
    core.parse_dt = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    core.get_video_metadata = Mock(name="process_metadata", return_value=[])
    core.get_video_stats = Mock(name="process_full", return_value=[])
    core.get_longform_playlist_id = lambda channel_id: "UULF" + channel_id[2:]
    core.VIDEO_TYPE_LONG = "long"
    core.YOUTUBE_API_TIMEOUT_SECONDS = 15
    core.YouTubeAPIError = YouTubeAPIError
    core.youtube_error_message = youtube_error_message
    return core


class MemoryDatabase:
    """Finite direct SQL/RPC responses, without an HTTP client or query builder."""
    def __init__(self, name, *, recent_rows=()):
        self.name, self.closed = name, False
        self.requests, self.recent_rows = [], list(recent_rows)
        self.gateway = self

    def close(self):
        self.closed = True

    def claim_inventory_channels(self, *, channel_ids, video_types, worker_id, run_id,
                                 lease_seconds, expected_channel_scopes, only_due=False):
        assert not self.closed
        assert only_due is False
        assert channel_ids == [CHANNEL]
        assert expected_channel_scopes == [{'channel_id': CHANNEL, 'control_epoch': 7}]
        assert video_types in (['long'], ['short'], ['stream'])
        values = dict(channel_ids=channel_ids, video_types=video_types, worker_id=worker_id,
                      run_id=run_id, lease_seconds=lease_seconds,
                      expected_channel_scopes=expected_channel_scopes)
        self.requests.append(SimpleNamespace(operation='transaction/claim_typed_inventory_channels', parameters=values))
        return ([{'channel_id': CHANNEL, 'video_type': 'long', 'playlist_id': 'UULF' + CHANNEL[2:],
                  'lease_token': '00000000-0000-0000-0000-000000000001', 'lease_channel_epoch': 7,
                  'lease_expires_at': NOW, 'scan_kind': 'initial_inventory', 'no_new_streak': 0}]
                if video_types == ['long'] else [])

    def begin_inventory_scan(self, **values):
        assert not self.closed
        assert values['channel_id'] == CHANNEL and values['video_type'] == 'long'
        self.requests.append(SimpleNamespace(operation='transaction/begin_typed_inventory_scan', parameters=values))
        return {'status': 'running', 'scan_id': values['scan_id'], 'resume_page_token': None, 'scan_boundary_at': NOW}

    def settle_inventory_page(self, **values):
        assert not self.closed
        assert values['channel_id'] == CHANNEL and values['video_type'] == 'long'
        self.requests.append(SimpleNamespace(operation='transaction/settle_typed_inventory_page', parameters=values))
        ids = [row['video_id'] for row in values['items']]
        return {'channel_id': CHANNEL, 'video_type': 'long', 'lease_token': values['lease_token'],
                'scan_id': values['scan_id'], 'transition_id': values['transition_id'],
                'status': 'completed', 'coverage': 'complete', 'page_items': len(ids),
                'inserted': len(ids), 'already_matching': 0, 'conflicts': 0, 'items_seen': len(ids),
                'canonical_video_ids': ids, 'new_video_ids': ids, 'resume_page_token': None}

    def ensure_addressed(self, **values):
        assert not self.closed
        assert values['channel_scopes'] == [{'channel_id': CHANNEL, 'control_epoch': 7}]
        self.requests.append(SimpleNamespace(operation='transaction/ensure_video_collection_state', parameters=values))
        return len(values['video_ids'])

    def claim_addressed(self, name, **values):
        assert not self.closed
        assert values['video_ids'] == [VIDEO]
        assert values['channel_scopes'] == [{'channel_id': CHANNEL, 'control_epoch': 7}]
        self.requests.append(SimpleNamespace(operation='transaction/' + name, parameters=values))
        return []

    def read_rows(self, operation, statement, parameters=(), *, max_rows):
        assert not self.closed
        if operation == 'collection_addressed_video_channels':
            result = [{'video_id': VIDEO, 'channel_id': CHANNEL}]
            relation = 'video_inventory'
        elif operation == 'channel_sync_recent_long_inventory':
            result, relation = self.recent_rows, 'video_inventory'
        elif operation == 'collection_addressed_channel_scopes':
            result = [{'channel_id': CHANNEL, 'collection_control_epoch': 7,
                       'collection_permission': 'accepted', 'is_hidden': False}]
            relation = 'channels_with_control'
        else:
            raise AssertionError('unexpected direct read: ' + operation)
        assert len(result) <= max_rows
        assert relation in statement
        self.requests.append(SimpleNamespace(operation=relation, parameters=parameters))
        return result

    @contextmanager
    def transaction(self, operation):
        assert not self.closed
        assert operation.name == 'update_channel_collection_fields'
        database = self
        class Transaction:
            def borrow(self): return nullcontext(self)
            def execute(self, statement, parameters=()):
                assert 'UPDATE public.channels' in statement.as_string()
                assert parameters[1] == CHANNEL
                database.requests.append(SimpleNamespace(
                    operation='transaction/update_channel_collection_fields', parameters=parameters))
                return 1
            def fetch_all(self, statement, parameters=(), *, max_rows):
                assert 'FOR SHARE' in statement and parameters == ([CHANNEL],) and max_rows == 1
                return [{'channel_id':CHANNEL, 'owner_permission':'accepted', 'is_hidden':False, 'control_epoch':7}]
        yield Transaction()

    def call(self, name, parameters=None, *, max_rows=1000):
        assert not self.closed
        body = parameters or {}
        self.requests.append(SimpleNamespace(operation='call/' + name, parameters=body))
        if name in {'claim_video_collection_ids', 'claim_video_metadata_collection_ids',
                    'claim_initial_video_collection_batch'}:
            return []
        if name == 'assert_operator_jobs_ready':
            return {'ready': True, 'schema_version': 9, 'initial_quota_limit_enforced': False,
                    'initial_inventory_scan_bridge': True, 'initial_chunk_pages': 1,
                    'full_initial_scope_version': 1, 'initial_total_limits_enforced': False}
        raise AssertionError('unexpected direct call: ' + name)

    def paths(self):
        return [request.operation for request in self.requests]


def playlist_page():
    return {"items": [{"contentDetails": {"videoId": VIDEO, "videoPublishedAt": NOW}}]}


def metadata_video():
    return {
        "id": VIDEO,
        "snippet": {"title": "Example", "publishedAt": NOW},
        "statistics": {"viewCount": "42"},
        "contentDetails": {"duration": "PT3M"},
    }


class SettingsTests(unittest.TestCase):
    def test_settings_import_needs_no_bootstrap_or_environment(self):
        real_import = builtins.__import__

        def pure_import(name, *args, **kwargs):
            if name.split(".")[0] in {"core", "dotenv", "db", "httpx", "requests"}:
                raise AssertionError("settings imported a runtime dependency: " + name)
            return real_import(name, *args, **kwargs)

        class UnreadableEnvironment(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("settings read process environment during import")

            def __getitem__(self, key):
                raise AssertionError("settings read process environment during import")

        with patch.dict(sys.modules), patch("builtins.__import__", side_effect=pure_import), \
                patch.object(os, "getenv", side_effect=AssertionError("import-time getenv")), \
                patch.object(os, "environ", UnreadableEnvironment()):
            module = load_module("_collection_pure_settings", "collection/settings.py")
        self.assertEqual(module.load_collection_settings({}), module.CollectionSettings(False, False, False))

    def test_settings_are_an_immutable_explicit_snapshot_with_existing_truthy_rules(self):
        from collection.settings import CollectionSettings, load_collection_settings

        for value in ("1", "true", "yes", "on", " TrUe "):
            with self.subTest(value=value):
                self.assertEqual(load_collection_settings(dict.fromkeys(ENABLED, value)),
                                 CollectionSettings(True, True, True))
        for value in ("", "0", "false", "no", "off", "unexpected"):
            with self.subTest(value=value):
                self.assertEqual(load_collection_settings(dict.fromkeys(ENABLED, value)),
                                 CollectionSettings(False, False, False))
        environ = dict(ENABLED)
        snapshot = load_collection_settings(environ)
        environ.clear()
        with patch.dict(os.environ, {key: "false" for key in ENABLED}):
            self.assertTrue(snapshot.youtube_batch_stats_enabled)
            self.assertEqual(load_collection_settings(environ), CollectionSettings(False, False, False))
        with self.assertRaises(FrozenInstanceError):
            snapshot.youtube_batch_stats_enabled = False


class ScopedCollectionTests(unittest.TestCase):
    def setUp(self):
        from collection.settings import CollectionSettings

        self.settings = CollectionSettings(True, True, True)
        self.disabled = CollectionSettings(False, False, False)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"}, clear=True))
        self.stack.enter_context(patch.object(Path, "home", return_value=ROOT))
        self.stack.enter_context(patch("hot_runtime.require_hot_runtime_ready", return_value=None))
        self.stack.enter_context(patch("db.inventory.inventory_claims.claim_typed_inventory_channels", autospec=True,
            side_effect=lambda database, **values: database.claim_inventory_channels(**values)))
        self.stack.enter_context(patch("db.inventory.typed_inventory.inventory_scans.begin_typed_inventory_scan",
            side_effect=lambda database, **values: database.begin_inventory_scan(**values)))
        self.stack.enter_context(patch("db.inventory.inventory_scan_pages.settle_typed_inventory_page",
            side_effect=lambda database, **values: database.settle_inventory_page(**values)))
        self.stack.enter_context(patch("db.videos.video_queue_ensure.ensure_video_collection_state",
            side_effect=lambda database, **values: database.ensure_addressed(**values)))
        self.stack.enter_context(patch("db.videos.video_claims.claim_video_collection_ids",
            side_effect=lambda database, **values: database.claim_addressed('claim_video_collection_ids', **values)))
        self.stack.enter_context(patch("db.videos.video_claims.claim_video_metadata_collection_ids",
            side_effect=lambda database, **values: database.claim_addressed('claim_video_metadata_collection_ids', **values)))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch("sys.stdout", new=io.StringIO()))
        self.core = core_stub()
        self.stack.enter_context(patch.dict(sys.modules, {"core": self.core, "dotenv": None}))
        from collection import api
        from runtime import process_collection
        from youtube_errors import YouTubeDailyQuotaExceededError, YouTubePlaylistNotFoundError
        from collection_outcome import copy_collection_stop
        self.api, self.compose = api, process_collection
        self.policy = self.compose.load_collection_policy(os.environ)
        # A poisoned old facade proves neither context nor worker imports it.
        self.sync = SimpleNamespace(database=self.core.database,
            YouTubeDailyQuotaExceededError=YouTubeDailyQuotaExceededError,
            YouTubePlaylistNotFoundError=YouTubePlaylistNotFoundError,
            _copy_collection_stop=copy_collection_stop,
            **{name: Mock(side_effect=AssertionError("old facade used")) for name in (
                "run_video_collection_queue_batch", "collect_initial_inventory_chunk", "initial_28d_stats_import",
                "build_video_stat_payload", "build_video_collection_metadata_payload", "build_video_counter_payload",
                "_legacy_video_collection_dependencies", "_legacy_inventory_dependencies",
                "_legacy_channel_process_repository", "_legacy_video_payload_dependencies")})
        self.stack.enter_context(patch.dict(sys.modules, {"sync_service": self.sync}))
        def metadata(ids, phase="video_metadata", *, reject_identity_anomalies=True, **resources):
            self.assertIs(resources["attempt_recorder"], self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER)
            self.assertEqual(resources["api_key"], self.core.API_KEY)
            if reject_identity_anomalies:
                return self.core.get_video_metadata(ids, phase=phase)
            return self.core.get_video_metadata(ids, phase=phase, reject_identity_anomalies=False)
        self.stack.enter_context(patch.object(self.compose, "fetch_video_metadata", side_effect=metadata))
        self.worker = load_module("_collection_scoped_worker", "scripts/run_operator_job_worker.py")

    def context(self, database, settings):
        return self.compose.bind_process_collection(database=database, settings=settings, policy=self.policy)

    def database(self, name, **kwargs):
        database = MemoryDatabase(name, **kwargs)
        self.addCleanup(database.close)
        return database

    def assert_process_untouched(self):
        self.assertIs(self.sync.database, self.core.database)
        self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.close.assert_not_called()

    def test_long_inventory_nested_callbacks_keep_their_own_gateway(self):
        first, second = self.database("first"), self.database("second")
        nested_results = []

        def fetch_page(playlist_id, page_token=None, **resources):
            self.assert_process_untouched()
            if not nested_results:
                nested_results.append(None)
                nested_results[0] = self.api.collect_initial_inventory_chunk(
                    CHANNEL, video_type="long", context=self.context(second.gateway, self.settings),
                 expected_epoch=7)
            return playlist_page()

        with patch.object(self.compose, "fetch_playlist_items_page", side_effect=fetch_page):
            result = self.api.collect_initial_inventory_chunk(
                CHANNEL, video_type="long", context=self.context(first.gateway, self.settings),
             expected_epoch=7)
        for database, outcome in ((first, result), (second, nested_results[0])):
            self.assertEqual(outcome["inserted"], 1)
            self.assertEqual(database.paths(), ["transaction/claim_typed_inventory_channels", "transaction/begin_typed_inventory_scan", "transaction/settle_typed_inventory_page"])
            self.assertEqual(database.requests[0].parameters['channel_ids'], [CHANNEL])
            self.assertEqual(database.requests[0].parameters['expected_channel_scopes'],
                             [{'channel_id': CHANNEL, 'control_epoch': 7}])
            self.assertFalse(database.closed)
        self.assert_process_untouched()

    def test_typed_inventory_uses_explicit_gate_and_repository(self):
        database = self.database("typed")
        self.sync.TYPED_PLAYLIST_INVENTORY_ENABLED = False
        for video_type in ("short", "stream"):
            with self.subTest(video_type=video_type):
                result = self.api.collect_initial_inventory_chunk(
                    CHANNEL, video_type=video_type, context=self.context(database.gateway, self.settings),
                 expected_epoch=7)
                self.assertEqual(result["outcome"], "deferred")
                self.assertEqual(database.requests[-1].parameters["video_types"], [video_type])
        self.sync.TYPED_PLAYLIST_INVENTORY_ENABLED = True
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            self.api.collect_initial_inventory_chunk(
                CHANNEL, video_type="short", context=self.context(database.gateway, self.disabled),
             expected_epoch=7)
        self.assertEqual(database.paths(), ["transaction/claim_typed_inventory_channels"] * 2)
        self.assert_process_untouched()

    def test_initial_stats_recent_read_and_empty_lifecycle_write_share_gateway(self):
        database = self.database("empty")
        result = self.api.initial_28d_stats_import(CHANNEL, context=self.context(database.gateway, self.settings), expected_epoch=7)
        self.assertEqual(result["status"], "skipped_no_recent_videos")
        self.assertEqual(database.paths(), ["video_inventory", "transaction/update_channel_collection_fields"])
        self.assertEqual(database.requests[-1].parameters[0].obj["initial_28d_stats_status"],
                         "skipped_no_recent_videos")
        self.assert_process_untouched()

    def test_initial_stats_propagates_settings_and_gateway_into_real_queue(self):
        database = self.database("recent", recent_rows=[{"video_id": VIDEO}])
        self.sync.DATABASE_VIDEO_COLLECTION_ENABLED = False
        result = self.api.initial_28d_stats_import(CHANNEL, context=self.context(database.gateway, self.settings), expected_epoch=7)
        self.assertIn("sync_summary", result)
        self.assertEqual(result["sync_summary"]["errors"], 0)
        self.assertIn("transaction/ensure_video_collection_state", database.paths())
        self.assertIn("transaction/claim_video_metadata_collection_ids", database.paths())
        self.assertEqual(database.requests[-1].parameters[0].obj["initial_28d_stats_status"], "error")
        self.assert_process_untouched()

    def test_initial_job_callback_claims_through_explicit_gateway(self):
        database = self.database("initial-job")
        # Exercise the actual callback and public repository, recording the new
        # Python transaction owner rather than the retired stored-call transport.
        with patch("db.videos.video_initial_claims.claim_initial_video_collection_batch", return_value=[]) as claim:
            result = self.api.run_video_collection_queue_batch(
                limit=1, explicit_video_ids=[VIDEO], force_metadata=True,
                initial_job={"id": "job", "channel_id": CHANNEL, "lease_owner": "worker", "lease_token": "token"},
                context=self.context(database.gateway, self.settings),
            )
            self.assertEqual(result["errors"], 0)
            claim.assert_called_once()
            self.assertIs(claim.call_args.args[0], database.gateway)
            self.assertEqual(claim.call_args.kwargs["job_id"], "job")
            self.assertEqual(claim.call_args.kwargs["video_ids"], [VIDEO])
            self.assertEqual(claim.call_args.kwargs["worker_id"], "worker")
            self.assertEqual(database.paths(), [])
            self.sync.DATABASE_VIDEO_COLLECTION_ENABLED = True
            with self.assertRaisesRegex(RuntimeError, "Database video collection is required"):
                self.api.run_video_collection_queue_batch(context=self.context(database.gateway, self.disabled))
            self.assertEqual(claim.call_count, 1)
        self.assert_process_untouched()

    def test_service_closures_capture_settings_but_keep_process_provider_ownership(self):
        first, second = self.database("enabled"), self.database("disabled")
        batch = Mock(return_value=[])
        # Provider callbacks are deliberately bound when composition happens.
        with patch.object(self.compose, "fetch_video_batch_stats", batch):
            repository_a, enabled = self.context(first.gateway, self.settings).video.build_service()
            repository_b, disabled = self.context(second.gateway, self.disabled).video.build_service()
            self.assertIs(repository_a.database, first.gateway)
            self.assertIs(repository_b.database, second.gateway)
            self.sync.YOUTUBE_BATCH_STATS_ENABLED = False
            enabled.fetch_counters([VIDEO], "scoped_counters")
            self.assertIs(batch.call_args.kwargs["attempt_recorder"], self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER)
            self.assertEqual(batch.call_args.kwargs["api_key"], self.core.API_KEY)
            self.assertEqual(batch.call_args.kwargs["phase"], "scoped_counters")
            self.assertEqual(enabled.build_full_payload(CHANNEL, metadata_video(), NOW)["row"]["metadata_checked_at"], NOW)
            self.sync.YOUTUBE_BATCH_STATS_ENABLED = True
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                disabled.fetch_counters([VIDEO], "scoped_counters")
            self.assertNotIn("metadata_checked_at", disabled.build_full_payload(CHANNEL, metadata_video(), NOW)["row"])
        enabled.fetch_metadata([VIDEO], "scoped_metadata")
        self.core.get_video_metadata.assert_called_once_with([VIDEO], phase="scoped_metadata", reject_identity_anomalies=False)
        self.core.get_video_metadata.reset_mock()
        self.core.YOUTUBE_BATCH_STATS_ENABLED = False
        self.assertEqual(enabled.fetch_full([VIDEO], "scoped_full"), [])
        self.core.get_video_metadata.assert_called_once_with([VIDEO], phase="scoped_full")
        self.core.get_video_metadata.reset_mock()
        self.core.YOUTUBE_BATCH_STATS_ENABLED = True
        with self.assertRaises(self.core.YouTubeAPIError):
            disabled.fetch_full([VIDEO], "disabled_full")
        self.core.get_video_metadata.assert_not_called()
        self.core.get_video_stats.assert_not_called()
        batch.assert_called_once()
        self.assert_process_untouched()

    def test_worker_payloads_use_new_owner_with_legacy_builders_poisoned_before_import(self):
        database = self.database("worker-payloads")
        captured = []

        with ExitStack() as wiring:
            for name in ("build_video_stat_payload", "build_video_collection_metadata_payload",
                         "build_video_counter_payload", "_legacy_video_payload_dependencies"):
                wiring.enter_context(patch.object(self.sync, name,
                    side_effect=AssertionError("worker used legacy payload builder")))
            worker = load_module("_collection_payload_worker", "scripts/run_operator_job_worker.py")
            metadata = wiring.enter_context(patch.object(video_payloads, "build_video_collection_metadata_payload",
                                                        wraps=video_payloads.build_video_collection_metadata_payload))
            counters = wiring.enter_context(patch.object(video_payloads, "build_video_counter_payload",
                                                        wraps=video_payloads.build_video_counter_payload))
            compose = worker.video_collection_dependencies

            def capture(**dependencies):
                resolved = compose(**dependencies)
                captured.append(resolved)
                return resolved

            with patch.object(worker, "video_collection_dependencies", side_effect=capture):
                callbacks = worker._collection_callbacks(database.gateway, self.settings)
            self.assertEqual(set(callbacks), {"run_stats", "run_initial_inventory_chunk", "run_initial_stats",
                                              "video_service", "inventory_repository", "fetch_page"})
            dependencies = captured[0]
            self.assertIs(dependencies["database"], database.gateway)
            self.assertIs(dependencies["settings"], self.settings)
            built = dependencies["build_full_payload"](CHANNEL, metadata_video(), NOW, batch_stats_enabled=True)
            self.assertEqual(built["row"]["metadata_checked_at"], NOW)
            self.assertEqual(built["snapshot"]["views"], 42)
            count = dependencies["build_counter_payload"]({"id": VIDEO, "views": 43}, NOW)
            self.assertEqual(count["snapshot"]["views"], 43)
            metadata.assert_called_once()
            self.assertIs(metadata.call_args.kwargs["parse_dt"], worker.parse_dt)
            self.assertIs(metadata.call_args.kwargs["view_count_absent_error"], worker.YouTubeViewCountAbsent)
            self.assertIs(metadata.call_args.kwargs["view_count_invalid_error"], worker.YouTubeViewCountInvalid)
            counters.assert_called_once()
        self.assertEqual(database.requests, [])
        self.assert_process_untouched()

    def test_worker_import_and_real_inventory_binding_do_not_need_sync_service(self):
        database = self.database("worker-without-sync")
        original_import = builtins.__import__

        def forbid_sync(name, *args, **kwargs):
            if name.split(".")[0] == "sync_service":
                raise AssertionError("operator worker imported sync_service")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=forbid_sync), patch.dict(sys.modules, {"sync_service": None}):
            worker = load_module("_collection_no_sync_worker", "scripts/run_operator_job_worker.py")
            transport = Mock(return_value=SimpleNamespace(json=lambda: playlist_page()))
            with patch.object(worker, "youtube_api_get", transport):
                callbacks = worker._collection_callbacks(database.gateway, self.settings)
                result = callbacks["run_initial_inventory_chunk"](CHANNEL, video_type="long", expected_epoch=7)
            self.assertEqual(result["inserted"], 1)
            transport.assert_called_once()
            self.assertIs(transport.call_args.kwargs["attempt_recorder"], self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER)
            self.assertEqual(transport.call_args.kwargs["api_key"], self.core.API_KEY)
            self.assertEqual(transport.call_args.kwargs["params"]["playlistId"], "UULF" + CHANNEL[2:])
            self.assertIs(worker.YouTubeAPIError, YouTubeAPIError)
            self.assertIs(worker.YouTubeDailyQuotaExceededError, self.sync.YouTubeDailyQuotaExceededError)
            self.assertIs(worker.YouTubePlaylistNotFoundError, self.sync.YouTubePlaylistNotFoundError)
            self.assertIs(worker._copy_collection_stop, self.sync._copy_collection_stop)
        self.assertEqual(database.paths(), ["transaction/claim_typed_inventory_channels", "transaction/begin_typed_inventory_scan", "transaction/settle_typed_inventory_page"])
        self.assert_process_untouched()


    def test_worker_nested_scope_and_exception_leave_borrowed_process_pool_open(self):
        database = self.database('process')
        with patch.object(self.worker, 'process_database', database), patch(
                'db.jobs.operator_jobs.capabilities.assert_operator_jobs_ready', return_value={
                    'ready': True, 'schema_version': 9, 'initial_inventory_scan_bridge': True,
                    'initial_chunk_pages': 1, 'full_initial_scope_version': 1,
                    'initial_total_limits_enforced': False, 'initial_quota_limit_enforced': False}) as check:
            with self.worker.fresh_operator_job_repository() as repository:
                self.assertTrue(repository.schema_status()['ready'])
                with self.assertRaisesRegex(RuntimeError, 'recovery failed'):
                    with self.worker.fresh_operator_job_repository() as replacement:
                        self.assertIsNot(replacement, repository)
                        self.assertIs(replacement.database, repository.database)
                        self.assertTrue(replacement.schema_status()['ready'])
                        raise RuntimeError('recovery failed')
                self.assertFalse(database.closed)
                self.assertTrue(repository.schema_status()['ready'])
        self.assertFalse(database.closed)
        self.assertEqual(database.paths(), [])
        self.assertEqual(check.call_count, 3)
        self.assertTrue(all(call.args == (database,) for call in check.call_args_list))
        self.assert_process_untouched()

    def test_worker_binds_real_callbacks_to_each_chunk_and_one_settings_snapshot(self):
        first = self.database("chunk-a", recent_rows=[{"video_id": VIDEO}])
        recovery = second = first  # every chunk and recovery borrows the same process pool
        observed, legacy_calls = [], []
        lock_handles = [Mock(), Mock()]

        def legacy_used(*args, **kwargs):
            legacy_calls.append(True)
            raise AssertionError("legacy adapter used by operator worker")

        def consume(repository, run_stats, *, run_initial_inventory_chunk, run_initial_stats,
                    fresh_repository_factory, **kwargs):
            database = (first, second)[len(observed)]
            observed.append(repository.database)
            self.assert_process_untouched()
            # Later configuration changes must not replace the one snapshot.
            os.environ.update({key: "false" for key in ENABLED})
            if len(observed) == 1:
                with self.assertRaisesRegex(RuntimeError, "recovery failed"):
                    with fresh_repository_factory() as replacement:
                        self.assertTrue(replacement.schema_status()["ready"])
                        self.assertIs(replacement.database, repository.database)
                        self.assert_process_untouched()
                        raise RuntimeError("recovery failed")
                self.assertFalse(recovery.closed)
            self.assertFalse(database.closed)
            self.assertEqual(run_stats(limit=1, explicit_video_ids=[VIDEO], force_metadata=True)["errors"], 0)
            self.assertEqual(run_initial_inventory_chunk(CHANNEL, video_type="long", expected_epoch=7)["inserted"], 1)
            self.assertEqual(run_initial_inventory_chunk(CHANNEL, video_type="short", expected_epoch=7)["outcome"], "deferred")
            # A real recent-row path invokes the bound video owner again. Empty
            # claims intentionally produce an incomplete result, not a fake success.
            imported = run_initial_stats(CHANNEL, expected_epoch=7)
            self.assertEqual(imported["status"], "error")
            self.assertEqual(imported["sync_summary"]["errors"], 0)
            self.assertTrue(imported["incomplete"])
            self.assertEqual(legacy_calls, [])
            return {"status": "completed", "processed_video_ids": 0, "processed_pages": 1}

        with ExitStack() as wiring:
            wiring.enter_context(patch('db.jobs.operator_jobs.capabilities.assert_operator_jobs_ready', return_value={
                'ready': True, 'schema_version': 9, 'initial_inventory_scan_bridge': True,
                'initial_chunk_pages': 1, 'full_initial_scope_version': 1,
                'initial_total_limits_enforced': False, 'initial_quota_limit_enforced': False}))
            # Poison before worker import so stale from-import aliases cannot
            # accidentally preserve access to the compatibility entrypoints.
            for name in ("run_video_collection_queue_batch", "collect_initial_inventory_chunk",
                         "initial_28d_stats_import", "_legacy_video_collection_dependencies",
                         "_legacy_inventory_dependencies", "_legacy_channel_process_repository"):
                wiring.enter_context(patch.object(self.sync, name, side_effect=legacy_used))
            self.worker = load_module("_collection_scoped_worker_explicit", "scripts/run_operator_job_worker.py")
            video_owner = wiring.enter_context(patch.object(video_execution, "run_video_collection_queue_batch",
                                                           wraps=video_execution.run_video_collection_queue_batch))
            inventory_owner = wiring.enter_context(patch.object(initial, "collect_initial_inventory_chunk",
                                                               wraps=initial.collect_initial_inventory_chunk))
            initial_owner = wiring.enter_context(patch.object(initial, "initial_28d_stats_import",
                                                             wraps=initial.initial_28d_stats_import))
            snapshot_loader = wiring.enter_context(patch.object(self.worker, "load_collection_settings",
                                                               wraps=self.worker.load_collection_settings))
            with patch.dict(os.environ, ENABLED), \
                    patch.object(self.worker, "operator_jobs_mode", return_value="durable"), \
                    patch.object(self.worker, "load_operator_job_settings", return_value=object()), \
                    patch.object(self.worker, "acquire_shared_sync_lock", side_effect=lock_handles), \
                    patch.object(self.worker, "fcntl", SimpleNamespace(flock=Mock(), LOCK_UN=8)), \
                    patch.object(self.worker, "process_database", first), \
                    patch.object(self.worker, "run_one_operator_job_chunk", side_effect=consume), \
                    patch.object(self.worker, "fetch_playlist_items_page", return_value=playlist_page(), create=True):
                with patch.object(self.worker, 'in_auto_sync_reserved_window', return_value=False):
                    result = self.worker.main(drain=True, supervised=False, max_chunks=2, max_seconds=10)
            self.assertEqual(result, 0)
            snapshot_loader.assert_called_once()
            self.assertEqual(len(observed), 2)
            self.assertIs(observed[0], observed[1])
            snapshot = video_owner.call_args_list[0].kwargs["settings"]
            self.assertEqual(snapshot, self.settings)
            for owner, per_chunk in ((video_owner, 2), (inventory_owner, 2), (initial_owner, 1)):
                self.assertEqual(owner.call_count, 2 * per_chunk)
                for index, call in enumerate(owner.call_args_list):
                    self.assertIs(call.kwargs["database"], observed[index // per_chunk])
                    self.assertIs(call.kwargs["settings"], snapshot)
        for database in (first,):
            self.assertFalse(database.closed)
            self.assertEqual(database.paths().count("transaction/claim_video_metadata_collection_ids"), 4)
            self.assertIn("transaction/settle_typed_inventory_page", database.paths())
            self.assertIn("transaction/claim_typed_inventory_channels", database.paths())
            self.assertIn("transaction/update_channel_collection_fields", database.paths())
        for handle in lock_handles:
            handle.close.assert_called_once()
        self.assertEqual(legacy_calls, [])
        self.assert_process_untouched()


class RequiredWorkerScheduleTests(unittest.TestCase):
    def test_missing_schedule_blocks_both_operator_entries_before_resources(self):
        core = core_stub()
        core.COLLECTION_SCHEDULES = None
        with patch.dict(sys.modules, {'core': core}):
            worker = load_module('_collection_worker_missing_schedule', 'scripts/run_operator_job_worker.py')
        with patch.dict(os.environ, ENABLED), \
                patch.object(worker, 'operator_jobs_mode', return_value='durable'), \
                patch.object(worker, 'process_database_resources') as resources, \
                patch.object(worker, 'run_one_operator_job_chunk') as run_chunk, \
                patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(ScheduleError, '^schedule_snapshot_required$'):
                worker.main()
            self.assertEqual(worker.cli([]), 1)
            resources.assert_not_called()
            run_chunk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
