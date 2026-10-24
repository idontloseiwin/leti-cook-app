"""Explicit video composition and routing; no server, provider or database."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / "application"
sys.path.insert(0, str(APPLICATION))
from collection.settings import CollectionSettings
from collection import video_execution as execution


class VideoExecutionTests(unittest.TestCase):
    def dependencies(self):
        return dict(
            database=object(), settings=CollectionSettings(True, True, True),
            fetch_full=Mock(return_value=[]), fetch_metadata=Mock(return_value=[]),
            fetch_counters=Mock(return_value={}), build_full_payload=Mock(return_value={}),
            build_counter_payload=Mock(return_value={}), is_quota_exhausted=Mock(return_value=False),
        )

    def test_actual_owner_imports_without_bootstrap_or_network(self):
        source = APPLICATION / "collection/video_execution.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([node.module] if isinstance(node, ast.ImportFrom)
                     else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            self.assertFalse({name.split(".")[0] for name in names if name}
                             & {"core", "sync_service", "server", "os", "dotenv"})
        script = """
import builtins, runpy, socket, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
real_import = builtins.__import__
def checked_import(name, *args, **kwargs):
    if name.split('.')[0] in {'core', 'sync_service', 'server', 'dotenv'}:
        raise AssertionError('bootstrap import: ' + name)
    return real_import(name, *args, **kwargs)
with patch('builtins.__import__', side_effect=checked_import), \
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network during import')):
    module = runpy.run_module('collection.video_execution')
    assert callable(module['run_video_collection_queue_batch'])
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(APPLICATION)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(execution.run_video_collection_queue_batch.__module__,
                         "collection.video_execution")

    def test_database_settings_and_callbacks_are_mandatory(self):
        for entry in (execution.build_video_collection_service,
                      execution.run_video_collection_queue_batch):
            dependencies = self.dependencies()
            if entry is execution.run_video_collection_queue_batch:
                dependencies["worker_id_factory"] = Mock(return_value="worker")
            for name in dependencies:
                with self.subTest(entry=entry.__name__, missing=name):
                    incomplete = dict(dependencies)
                    del incomplete[name]
                    with self.assertRaises(TypeError):
                        entry(**incomplete)
                with self.subTest(entry=entry.__name__, null=name):
                    with self.assertRaises(TypeError):
                        entry(**{**dependencies, name: None})

    def test_disabled_gate_precedes_repository_or_provider_use(self):
        dependencies = self.dependencies()
        dependencies["settings"] = CollectionSettings(False, True, True)
        with patch.object(execution, "VideoCollectionRepository") as repository:
            with self.assertRaisesRegex(RuntimeError, "Database video collection is required"):
                execution.run_video_collection_queue_batch(
                    **dependencies, worker_id_factory=Mock(return_value="worker"))
        repository.assert_not_called()
        for name in ("fetch_full", "fetch_metadata", "fetch_counters"):
            dependencies[name].assert_not_called()

    def test_factory_binds_gateway_and_settings_without_environment_fallback(self):
        dependencies = self.dependencies()
        repository, service = execution.build_video_collection_service(**dependencies)
        self.assertIs(repository.database, dependencies["database"])
        self.assertIs(service.repository, repository)
        self.assertIs(service.fetch_metadata, dependencies["fetch_metadata"])
        self.assertIs(service.fetch_full, dependencies["fetch_full"])
        self.assertIs(service.is_quota_exhausted, dependencies["is_quota_exhausted"])
        with patch.dict(os.environ, {"YOUTUBE_BATCH_STATS_ENABLED": "false"}):
            service.fetch_counters(["video"], "counter_phase")
            service.build_full_payload("channel", {"id": "video"}, "now")
            service.build_counter_payload({"id": "video"}, "now")
        dependencies["fetch_counters"].assert_called_once_with(["video"], "counter_phase")
        dependencies["build_full_payload"].assert_called_once_with(
            "channel", {"id": "video"}, now="now", batch_stats_enabled=True)
        dependencies["build_counter_payload"].assert_called_once_with({"id": "video"}, now="now")
        disabled = self.dependencies()
        disabled["settings"] = CollectionSettings(True, True, False)
        _, service = execution.build_video_collection_service(**disabled)
        with self.assertRaisesRegex(RuntimeError, "videos.batchGetStats is disabled"):
            service.fetch_counters(["video"], "counter_phase")
        disabled["fetch_counters"].assert_not_called()

    def test_common_preflight_and_worker_identity_precede_claim_once(self):
        for preflight_checked in (False, True):
            with self.subTest(preflight_checked=preflight_checked):
                events = []
                repository = Mock()
                repository.preflight_common_pool_ready.side_effect = lambda: events.append("preflight")
                worker_id = Mock(side_effect=lambda: events.append("worker_id") or "worker")
                outcome = {"settlement_unknown": True, "hot_blocked": True}
                with patch.object(execution, "VideoCollectionRepository", return_value=repository), \
                     patch.object(execution, "run_claimed_common_video_collection",
                                  side_effect=lambda *a, **kw: events.append("claim") or outcome) as run:
                    actual = execution.run_video_collection_queue_batch(
                        **self.dependencies(), worker_id_factory=worker_id, limit=-2,
                        common_preflight_checked=preflight_checked)
                self.assertIs(actual, outcome)
                self.assertEqual(events, ([] if preflight_checked else ["preflight"]) + ["worker_id", "claim"])
                self.assertEqual(run.call_args.kwargs["worker_id"], "worker:common")
                self.assertEqual(run.call_args.kwargs["limit"], 0)

    def test_explicit_route_preserves_targets_metadata_and_progress(self):
        for scopes in (None, [{"channel_id": "channel", "control_epoch": 7}]):
            with self.subTest(channel_scopes=scopes):
                repository, progress = Mock(), Mock()
                ids, outcome = ["video", "video"], {"settlement_unknown": True}
                with patch.object(execution, "VideoCollectionRepository", return_value=repository), \
                     patch.object(execution, "run_claimed_video_collection", return_value=outcome) as run:
                    actual = execution.run_video_collection_queue_batch(
                        **self.dependencies(), worker_id_factory=lambda: "worker", limit=9,
                        explicit_video_ids=ids, force_metadata=True, reason="reason",
                        progress_callback=progress, channel_scopes=scopes)
                self.assertIs(actual, outcome)
                repository.preflight_common_pool_ready.assert_not_called()
                self.assertEqual(run.call_args.kwargs, dict(
                    limit=9, worker_id="worker", explicit_video_ids=ids, force_metadata=True,
                    reason="reason", progress_callback=progress, channel_scopes=scopes))
                self.assertIs(run.call_args.kwargs["channel_scopes"], scopes)

    def test_initial_route_uses_job_lease_and_preserves_outcome(self):
        repository, worker_id, progress = Mock(), Mock(), Mock()
        job = dict(id=1, channel_id="channel", lease_owner="job-owner", lease_token="job-token")
        outcome = {"settlement_unknown": True, "hot_blocked": True}
        with patch.object(execution, "VideoCollectionRepository", return_value=repository), \
             patch.object(execution, "run_claimed_initial_video_collection", return_value=outcome) as run:
            actual = execution.run_video_collection_queue_batch(
                **self.dependencies(), worker_id_factory=worker_id, limit=1,
                explicit_video_ids=["video"], force_metadata=True, initial_job=job,
                progress_callback=progress)
        self.assertIs(actual, outcome)
        worker_id.assert_not_called()
        repository.preflight_common_pool_ready.assert_not_called()
        self.assertEqual(run.call_args.kwargs, dict(
            job_id="1", channel_id="channel", video_ids=["video"], worker_id="job-owner",
            run_id="job-token", reason="auto_stats_batch", progress_callback=progress, concurrency=8))

    def test_initial_targets_fail_before_claim_when_not_explicit_or_over_bound(self):
        with patch.object(execution, "run_claimed_initial_video_collection") as run:
            for arguments in ({"force_metadata": False, "explicit_video_ids": ["video"]},
                              {"force_metadata": True, "explicit_video_ids": None},
                              {"force_metadata": True, "explicit_video_ids": ["video", "extra"]}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    execution.run_video_collection_queue_batch(
                        **self.dependencies(), worker_id_factory=Mock(), initial_job={}, limit=1, **arguments)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
