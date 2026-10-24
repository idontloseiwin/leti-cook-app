"""Offline context lifetimes. Optional LIC_COLLECTION_CONTEXT_APPLICATION_DIR supports draft execution."""
import builtins
from contextlib import contextmanager, ExitStack
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = Path(os.environ.get("LIC_COLLECTION_CONTEXT_APPLICATION_DIR") or ROOT / "application")
sys.path.insert(0, str(APPLICATION))
from collection import api, channel_metadata, cycle
from collection.context import CollectionPolicy
from collection_schedule import CollectionSchedules
from collection.settings import CollectionSettings
from runtime import process_collection as process
from youtube_source_fields import parse_dt
import auto_sync

CHANNEL = "UC" + "a" * 22
VIDEO = "video000001"
NOW = datetime(2026, 9, 27, 10, tzinfo=timezone.utc)


class Gateway:
    def __init__(self, name):
        self.name, self.closed = name, False


class CollectionContextTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch("sys.stdout", io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {"LIC_DEPLOYMENT_PROFILE": "self-hosted"}))
        self.core = ModuleType("core")
        self.core.COLLECTION_SCHEDULES = CollectionSchedules()
        self.core.API_KEY, self.core.YOUTUBE_API_TIMEOUT_SECONDS = "fixed-key", 17
        self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER = Mock(name="process_recorder")
        self.core.database = Gateway("process")
        self.core.get_video_metadata = Mock(side_effect=AssertionError("legacy core provider"))
        self.core.get_video_stats = Mock(side_effect=AssertionError("legacy core provider"))
        self.stack.enter_context(patch.dict(sys.modules, {"core": self.core, "sync_service": None}))
        self.settings = CollectionSettings(True, True, True)
        self.policy = CollectionPolicy(True, True, True, 24)
        self.opened, self.closed = [], []

        @contextmanager
        def resources():
            gateway = self.core.database
            self.opened.append(gateway)
            self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.start()
            try:
                yield gateway
            finally:
                self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.close()
                gateway.closed = True
                self.closed.append(gateway)

        self.core.process_database_resources = resources
        self.stack.enter_context(patch.object(process, "emit_event", Mock()))

    def assert_process_alive(self):
        self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.close.assert_not_called()
        self.core.get_video_metadata.assert_not_called()
        self.core.get_video_stats.assert_not_called()

    def scope(self):
        return process.process_collection_scope(settings=self.settings, policy=self.policy)

    def test_configuration_imports_core_before_both_immutable_snapshots(self):
        events = []
        original_import = builtins.__import__
        environment = dict.fromkeys(("DATABASE_VIDEO_COLLECTION_ENABLED", "TYPED_PLAYLIST_INVENTORY_ENABLED",
            "YOUTUBE_BATCH_STATS_ENABLED", "DATABASE_CHANNEL_DISCOVERY_ENABLED", "SHORT_VIDEO_COLLECTION_ENABLED",
            "COMPLETED_STREAM_COLLECTION_ENABLED"), " YeS ") | {"SYNC_RUN_STALE_AFTER_HOURS": "999"}

        def imported(name, *args, **kwargs):
            if name == "core":
                events.append("core")
                os.environ.update(environment)
            return original_import(name, *args, **kwargs)

        original_settings, original_policy = process.load_collection_settings, process.load_collection_policy
        def settings(environ):
            events.append("settings")
            return original_settings(environ)
        def policy(environ):
            events.append("policy")
            return original_policy(environ)

        with patch.dict(os.environ, {}, clear=True), patch("builtins.__import__", side_effect=imported), \
                patch.object(process, "load_collection_settings", side_effect=settings), \
                patch.object(process, "load_collection_policy", side_effect=policy):
            snapshot, frozen_policy = process.process_collection_configuration()
            os.environ.clear()
        self.assertEqual(events, ["core", "settings", "policy"])
        self.assertEqual(snapshot, self.settings)
        self.assertEqual(frozen_policy, CollectionPolicy(True, True, True, 168))
        with self.assertRaises(FrozenInstanceError):
            frozen_policy.sync_run_stale_after_hours = 1
        for value, expected in (("bad", 24), ("", 24), ("0", 1), ("-1", 1), ("169", 168)):
            self.assertEqual(process.load_collection_policy({"SYNC_RUN_STALE_AFTER_HOURS": value}).sync_run_stale_after_hours, expected)

    def test_actual_raw_providers_receive_process_resources_and_common_metadata_policy(self):
        def response(endpoint, **kwargs):
            data = {"items": [{"id": VIDEO, "statistics": {"viewCount": "123"}}]} if endpoint == "videos" else {"items": []}
            return SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)
        transport, metadata = Mock(side_effect=response), Mock(wraps=process.fetch_video_metadata)
        with patch.object(process, "youtube_api_get", transport), patch.object(process, "fetch_video_metadata", metadata), self.scope() as context:
            repository, service = context.video.build_service()
            self.assertIs(repository.database, context.database)
            self.assertEqual(service.fetch_metadata([VIDEO], "context_metadata"), [{"id": VIDEO, "statistics": {"viewCount": "123"}}])
            self.assertIs(metadata.call_args.kwargs["include_statistics"], True)
            self.assertIs(transport.call_args.kwargs["complete_profile"], True)
            self.assertIs(metadata.call_args.kwargs["reject_identity_anomalies"], False)
            context.sources.fetch_channel_metadata_batch([CHANNEL])
            context.sources.fetch_playlist_items_page("UULF" + CHANNEL[2:])
            self.assertIs(context.sources.parse_dt, parse_dt)
            self.assertEqual(context.sources.get_longform_playlist_id(CHANNEL), "UULF" + CHANNEL[2:])
            self.assertEqual([call.args[0] for call in transport.call_args_list], ["videos", "channels", "playlistItems"])
            for call in transport.call_args_list:
                self.assertIs(call.kwargs["attempt_recorder"], self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER)
                self.assertEqual((call.kwargs["api_key"], call.kwargs["timeout_seconds"]), (self.core.API_KEY, 17))
            self.assertFalse(context.database.closed)
        self.assert_process_alive()

    def test_bound_video_service_and_batch_cannot_replace_context_gateway_or_settings(self):
        with self.scope() as context:
            with self.assertRaises(TypeError):
                context.video.build_service(database=object())
            with self.assertRaises(TypeError):
                context.video.build_service(settings=CollectionSettings(False, False, False))
            with self.assertRaises(ValueError):
                context.video.run_batch(database=object())
            with self.assertRaises(ValueError):
                context.video.run_batch(settings=CollectionSettings(False, False, False))
            repository, _ = context.video.build_service()
            self.assertIs(repository.database, context.database)
        self.assert_process_alive()

    def test_nested_scopes_borrow_process_pool_even_when_nested_work_raises(self):
        process_database = self.core.database
        with self.scope() as first:
            with self.assertRaisesRegex(RuntimeError, "nested failure"):
                with self.scope() as second:
                    self.assertIs(first.database, second.database)
                    self.assertIs(first.settings, self.settings)
                    self.assertIs(second.settings, self.settings)
                    raise RuntimeError("nested failure")
            self.assertFalse(second.database.closed)
            self.assertIs(self.core.database, process_database)
        self.assertFalse(first.database.closed)
        self.assertEqual(self.closed, [])
        self.assert_process_alive()

    def test_metadata_unknown_ack_uses_fresh_recovery_read_then_separate_error_write(self):
        repositories = []
        def repository(database):
            result = Mock(database=database)
            result.load_channel_record.side_effect = RuntimeError("read failed")
            def update(*args, **kwargs):
                self.assertEqual(kwargs["expected_epoch"], 7)
                self.assertFalse(database.closed)
            result.update_process_state.side_effect = update
            repositories.append(result)
            return result
        with patch.object(process, "ChannelSyncRepository", side_effect=repository), self.scope() as context:
            result = channel_metadata._reconcile_or_mark_channel_metadata_persistence_error(
                {"channel_id": CHANNEL, "channel_metadata_error_count": 0, "collection_control_epoch": 7}, NOW,
                TimeoutError("unknown write acknowledgement"), context=context)
            self.assertFalse(result)
            self.assertEqual(len(repositories), 2)
            repositories[0].load_channel_record.assert_called_once_with(CHANNEL)
            repositories[1].update_process_state.assert_called_once()
            self.assertIs(repositories[0].database, repositories[1].database)
            self.assertFalse(any(repository.database.closed for repository in repositories))
            self.assertFalse(context.database.closed)
            self.assertIn("channel_metadata_persistence", repositories[1].update_process_state.call_args.args[1]["channel_metadata_error"])
        self.assert_process_alive()

    def test_auto_entrypoint_keeps_scope_open_for_every_phase_and_success_ack(self):
        self.run_auto(fail=False)

    def test_auto_entrypoint_failure_ack_happens_before_scope_closes(self):
        self.run_auto(fail=True)

    def run_auto(self, *, fail):
        contexts, phases, receipts = [], [], []
        args = SimpleNamespace(metadata_limit=1, inventory_page_budget=6, stats_limit=1, new_video_stats_limit=1, short_video_collection_limit=0, completed_stream_collection_limit=0, dry_run=False)
        def accept(context):
            self.assertFalse(context.database.closed)
            self.assertIs(context.settings, self.settings)
            if contexts:
                self.assertIs(context, contexts[0])
            contexts.append(context)
        def start(*, context, **kwargs):
            accept(context)
            return "run"
        def finish(run_id, summary, *, context, **kwargs):
            accept(context)
            receipts.append(summary["errors"])
            return True
        def phase(name):
            def invoke(*args, context, **kwargs):
                accept(context)
                phases.append(name)
                if name == "run_typed_playlist_inventory_batch":
                    self.assertEqual(kwargs["limit"], 6)
                    self.assertTrue(callable(kwargs["on_page"]))
                return {"errors": 0, "results": [], "updated": 0}
            return invoke
        with ExitStack() as patches:
            patches.enter_context(patch.object(auto_sync, "parse_args", return_value=args))
            patches.enter_context(patch.object(auto_sync, "selected_deployment_profile", return_value="self-hosted"))
            patches.enter_context(patch.object(process, "process_collection_configuration", return_value=(self.settings, self.policy)))
            patches.enter_context(patch.object(api, "start_sync_run", side_effect=start))
            patches.enter_context(patch.object(api, "finish_sync_run", side_effect=finish))
            if fail:
                patches.enter_context(patch.object(api, "run_auto_sync_cycle", side_effect=RuntimeError("fixed cycle failure")))
            else:
                for name in ("validate_required_auto_sync_rpcs", "recover_stale_running_statuses", "run_video_stats_sync_batch",
                    "run_channel_metadata_sync_batch", "run_typed_playlist_inventory_batch"):
                    patches.enter_context(patch.object(cycle, name, side_effect=phase(name)))
                patches.enter_context(patch.object(cycle, "update_sync_run_progress", side_effect=lambda *a, context, **k: accept(context)))
                patches.enter_context(patch.object(cycle, "stop_at_boundary", return_value=False))
                patches.enter_context(patch.object(cycle, "emit_phase", Mock()))
            self.assertEqual(auto_sync.main(), 1 if fail else 0)
        self.assertEqual(receipts, [1 if fail else 0])
        self.assertTrue(contexts[0].database.closed)
        self.assertEqual(len(self.opened), 1)
        if not fail:
            self.assertEqual(phases, ["validate_required_auto_sync_rpcs", "recover_stale_running_statuses", "run_video_stats_sync_batch",
                "run_channel_metadata_sync_batch", "run_typed_playlist_inventory_batch"])
        self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.start.assert_called_once_with()
        self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.close.assert_called_once_with()
        self.core.get_video_metadata.assert_not_called()
        self.core.get_video_stats.assert_not_called()

    def test_supervised_auto_injects_shared_common_phase_in_same_context(self):
        args = SimpleNamespace(metadata_limit=1, inventory_page_budget=6, stats_limit=1,
            new_video_stats_limit=1, short_video_collection_limit=0,
            completed_stream_collection_limit=0, dry_run=False)
        runner = Mock()
        runner.checkpoint.return_value = None
        with patch.dict(os.environ, {'LIC_COLLECTION_SUPERVISED': '1'}), \
                patch.object(auto_sync, 'parse_args', return_value=args), \
                patch.object(auto_sync, 'selected_deployment_profile', return_value='self-hosted'), \
                patch.object(process, 'process_collection_configuration', return_value=(self.settings, self.policy)), \
                patch.object(process, 'bind_process_common_dispatch', return_value=runner) as bind, \
                patch.object(api, 'start_sync_run', return_value=42), \
                patch.object(api, 'finish_sync_run', return_value=True), \
                patch.object(api, 'run_auto_sync_cycle', return_value={'errors': 0}) as run:
            self.assertEqual(auto_sync.main(), 0)
        self.assertIs(run.call_args.kwargs['common_dispatch'], runner)
        self.assertIs(bind.call_args.args[0], run.call_args.kwargs['context'])
        self.assertEqual(run.call_args.kwargs['sync_run_id'], 42)
        self.assertEqual(len(self.opened), 1)

    def _finalization_case(self, *, finish_error=None, checkpoint_error=None, cycle_error=None, finish_result=True):
        args=SimpleNamespace(metadata_limit=1,inventory_page_budget=6,stats_limit=1,
            new_video_stats_limit=1,short_video_collection_limit=0,completed_stream_collection_limit=0,dry_run=False)
        runner=Mock();runner.checkpoint.return_value=None
        if checkpoint_error:runner.checkpoint.side_effect=checkpoint_error
        with patch.dict(os.environ,{'LIC_COLLECTION_SUPERVISED':'1'}), \
                patch.object(auto_sync,'parse_args',return_value=args), \
                patch.object(auto_sync,'selected_deployment_profile',return_value='self-hosted'), \
                patch.object(process,'process_collection_configuration',return_value=(self.settings,self.policy)), \
                patch.object(process,'bind_process_common_dispatch',return_value=runner), \
                patch.object(api,'start_sync_run',return_value=42), \
                patch.object(api,'finish_sync_run',return_value=finish_result,side_effect=finish_error) as finish, \
                patch.object(api,'run_auto_sync_cycle',return_value={'errors':0},side_effect=cycle_error):
            self.assertEqual(auto_sync.main(),1)
        finish.assert_called_once()
        return finish.call_args.args[1]

    def test_unknown_or_raising_finish_is_never_repeated(self):
        self._finalization_case(finish_error=OSError('unknown final receipt'))

    def test_unconfirmed_finish_is_never_repeated(self):
        self._finalization_case(finish_result=False)

    def test_checkpoint_failure_after_confirmed_receipt_does_not_finish_again(self):
        self._finalization_case(checkpoint_error=RuntimeError('checkpoint failed'))

    def test_cycle_failure_before_finalization_writes_one_failure_receipt(self):
        failure=self._finalization_case(cycle_error=RuntimeError('cycle failed'))
        self.assertEqual(failure['errors'],1)

    def test_common_binding_borrows_context_and_freezes_existing_operator_limits(self):
        import operator_jobs
        from runtime import collection as runtime_collection
        from db.user_work import reconnect_admission
        context = process.bind_process_collection(database=self.core.database,
            settings=self.settings, policy=self.policy)
        jobs = Mock()
        jobs.schema_status.return_value = dict(ready=True, schema_version=7, initial_quota_limit_enforced=False)
        observed = {}
        def phase(service, **options):
            observed.update(options, service=service)
            options['operator_factory'](coordinator=object(), require_ready=Mock(),
                should_stop=Mock(return_value=False), on_stop=Mock())
            return {'updated': 1}
        with patch.dict(os.environ, {'LIC_YOUTUBE_REQUEST_CONCURRENCY': '2',
                'OPERATOR_JOB_DRAIN_MAX_CHUNKS': '3', 'OPERATOR_JOB_DRAIN_MAX_SECONDS': '4'}):
            bound = process.bind_process_common_dispatch(context)
        with patch.object(process, 'OperatorJobRepository', return_value=jobs), \
                patch.object(process, 'load_operator_job_settings', return_value=object()), \
                patch.object(cycle, 'run_common_operator_phase', side_effect=phase), \
                patch.object(operator_jobs, 'operator_dispatch_turn', return_value=object()) as owner, \
                patch.object(reconnect_admission, 'configured_channel_add_admission', side_effect=AssertionError('manual limiter used by reconnect')), \
                patch.object(runtime_collection, 'bind_reconnect_feeder', return_value=object()) as reconnect:
            self.assertEqual(bound(limit=5, context=context, common_preflight_checked=True), {'updated': 1})
            options = owner.call_args.kwargs
            options['reconnect_factory'](on_settled=Mock(), should_stop=Mock())
            with self.assertRaises(ValueError):
                bound(limit=1, context=object())
        self.assertIs(owner.call_args.args[0], jobs)
        self.assertIs(options['video_service'], observed['service'])
        self.assertEqual((options['concurrency'], options['max_chunks'], options['max_seconds']), (2, 3, 4))
        self.assertIs(reconnect.call_args.args[0], context.database)
        self.assertIs(reconnect.call_args.kwargs['require_ready'], options['require_ready'])
        jobs.schema_status.assert_called_once()

    def test_missing_schedule_refuses_auto_before_pool_or_quota_start(self):
        self.core.COLLECTION_SCHEDULES = None
        args = SimpleNamespace(metadata_limit=1, inventory_page_budget=6, stats_limit=1,
            new_video_stats_limit=1, short_video_collection_limit=0,
            completed_stream_collection_limit=0, dry_run=False)
        with patch.object(auto_sync, 'parse_args', return_value=args), \
                patch.object(auto_sync, 'selected_deployment_profile', return_value='self-hosted'):
            self.assertEqual(auto_sync.main(), 1)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.closed, [])
        self.core.YOUTUBE_QUOTA_ATTEMPT_RECORDER.start.assert_not_called()

    def test_process_composition_import_does_not_import_bootstrap(self):
        script = r'''
import builtins, sys
sys.path.insert(0, APPLICATION)
original=builtins.__import__
def guarded(name,globals=None,locals=None,fromlist=(),level=0):
    if level==0 and name.split('.')[0] in {'core','sync_service','server'}:
        raise AssertionError('bootstrap during composition import: '+name)
    return original(name,globals,locals,fromlist,level)
builtins.__import__=guarded
import runtime.process_collection
'''.replace("APPLICATION", repr(str(APPLICATION)), 1)
        result=subprocess.run([sys.executable,"-I","-B","-c",script],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == "__main__":
    unittest.main()
