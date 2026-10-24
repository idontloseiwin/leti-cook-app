"""Legacy initial entrypoints now enqueue admitted work, with no provider calls."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch
import inspect
import os
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from collection import api, commands
from collection.context import CollectionContext, CollectionPolicy
from collection.settings import CollectionSettings
from db.jobs.operator_jobs import OperatorJobRepository
from operator_jobs import OperatorJobSettings
from runtime import process_collection as process
from runtime.collection import bind_collection_context

CHANNEL = "UC" + "a" * 22
SETTINGS = OperatorJobSettings(max_quota_units=None, max_target_attempts=3, chunk_size=50, lease_seconds=1200)
SCHEMA = dict(ready=True, schema_version=9, initial_quota_limit_enforced=False,
              initial_inventory_scan_bridge=True, initial_chunk_pages=1,
              full_initial_scope_version=1, initial_total_limits_enforced=False)


def repositories():
    channel, jobs = Mock(), Mock()
    channel.load_channel_record.return_value = dict(channel_id=CHANNEL, collection_control_epoch=7)
    jobs.schema_status.return_value = dict(SCHEMA)
    jobs.estimate_initial_inventory.return_value = dict(items=9, pages=3, quota_units=5,
        scope="all", video_types=["long", "short", "stream"], counts={"long": 4, "short": 3, "stream": 2},
        statuses={"long": "ok", "short": "ok", "stream": "ok"})
    jobs.enqueue_initial_inventory.return_value = dict(created=True,
        job=dict(id="job", channel_id=CHANNEL, accepted_epoch=7, status="queued"))
    return channel, jobs


class InitialEnqueueTests(unittest.TestCase):
    def setUp(self):
        self.channel, self.jobs = repositories()
        self.load = Mock(return_value=SETTINGS)
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start(); self.addCleanup(network.stop)

    def enqueue(self, expected_epoch=7):
        return commands.enqueue_initial_inventory_for_channel(CHANNEL, expected_epoch=expected_epoch,
            channel_repository=self.channel, repository=self.jobs, load_settings=self.load)

    def test_new_full_scope_estimate_and_confirmation_do_not_create_total_caps(self):
        self.assertEqual(self.enqueue(), dict(status="queued", channel_id=CHANNEL, job_id="job", created=True))
        self.channel.load_channel_record.assert_called_once_with(CHANNEL)
        self.load.assert_called_once_with(require_quota_cap=False)
        self.jobs.enqueue_initial_inventory.assert_called_once_with(CHANNEL,
            expected_epoch=7, expected_items=9, expected_pages=3, expected_quota_units=5,
            max_items=None, max_pages=None, max_quota_units=None, max_attempts=3, confirmed=False)

    def test_new_generation_is_never_adopted(self):
        self.channel.load_channel_record.return_value["collection_control_epoch"] = 9
        with self.assertRaisesRegex(RuntimeError, "scope_lost"): self.enqueue()
        self.assertEqual(self.jobs.mock_calls, []); self.load.assert_not_called()

    def test_missing_or_foreign_channel_never_enqueues(self):
        for row in (None, {"channel_id": "foreign", "collection_control_epoch": 7}):
            self.channel.load_channel_record.return_value = row
            with self.assertRaisesRegex(RuntimeError, "scope_lost"): self.enqueue()
        self.assertEqual(self.jobs.mock_calls, [])

    def test_invalid_scope_fails_before_repository(self):
        with self.assertRaises(ValueError): self.enqueue(expected_epoch=True)
        self.channel.load_channel_record.assert_not_called(); self.assertEqual(self.jobs.mock_calls, [])

    def test_invalid_settings_fail_before_enqueue(self):
        self.load.side_effect = ValueError("invalid chunk size")
        with self.assertRaises(ValueError): self.enqueue()
        self.jobs.enqueue_initial_inventory.assert_not_called()

    def test_old_environment_item_cap_does_not_truncate_full_scope(self):
        with patch.dict(os.environ, {"OPERATOR_JOB_MAX_ITEMS": "1"}):
            self.assertEqual(self.enqueue()["status"], "queued")
        self.assertIsNone(self.jobs.enqueue_initial_inventory.call_args.kwargs["max_items"])

    def test_unknown_enqueue_ack_is_not_retried_or_overwritten(self):
        self.jobs.enqueue_initial_inventory.side_effect = TimeoutError("ack lost")
        with self.assertRaises(TimeoutError): self.enqueue()
        self.jobs.enqueue_initial_inventory.assert_called_once()
        self.jobs.fail_initial_inventory.assert_not_called()

    def test_returned_job_cannot_adopt_replacement_epoch(self):
        self.jobs.enqueue_initial_inventory.return_value["job"]["accepted_epoch"] = 9
        with self.assertRaisesRegex(RuntimeError, "enqueue_identity_invalid"): self.enqueue()

    def test_public_api_is_only_explicit_context_dispatch(self):
        callback = Mock(return_value={"status": "queued"})
        provider = Mock(side_effect=AssertionError("provider forbidden"))
        context = SimpleNamespace(enqueue_initial_inventory=callback,
                                  sources=SimpleNamespace(fetch_playlist_items_page=provider))
        self.assertEqual(api.initial_inventory_for_channel(CHANNEL, context=context, expected_epoch=7),
                         {"status": "queued"})
        callback.assert_called_once_with(CHANNEL, expected_epoch=7); provider.assert_not_called()
        with self.assertRaises(ValueError): api.initial_inventory_for_channel(CHANNEL, context=context, expected_epoch=True)
        self.assertEqual(callback.call_count, 1)


class EnqueueCompositionTests(unittest.TestCase):
    def setUp(self):
        core = ModuleType("core")
        core.API_KEY, core.YOUTUBE_API_TIMEOUT_SECONDS = "offline", 17
        core.YOUTUBE_QUOTA_ATTEMPT_RECORDER = Mock()
        core.database = object()
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"core": core}).start()
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")).start()
        patch.dict(os.environ, {"OPERATOR_JOB_CHUNK_SIZE": "50"}, clear=True).start()
        self.channel, self.jobs = repositories()
        self.gateway = object()
        self.channel_factory = patch.object(process, "ChannelSyncRepository", return_value=self.channel).start()
        self.jobs_factory = patch.object(process, "OperatorJobRepository", return_value=self.jobs).start()
        self.wake = patch.object(process, "signal_operator_job_worker").start()
        self.provider = patch.object(process, "youtube_api_get", side_effect=AssertionError("provider forbidden")).start()

    def bind(self):
        return process.bind_process_collection(database=self.gateway,
            settings=CollectionSettings(True, True, True), policy=CollectionPolicy(True, True, True, 24))

    def test_environ_snapshot_and_gateway_are_bound_before_enqueue(self):
        context = self.bind()
        os.environ["OPERATOR_JOB_CHUNK_SIZE"] = "1"
        self.assertEqual(context.enqueue_initial_inventory(CHANNEL, expected_epoch=7)["status"], "queued")
        self.channel_factory.assert_called_once_with(self.gateway)
        self.jobs_factory.assert_called_once_with(self.gateway)
        self.assertEqual(self.wake.call_args.kwargs["environ"]["OPERATOR_JOB_CHUNK_SIZE"], "50")
        self.provider.assert_not_called()

    def test_wake_failure_does_not_undo_committed_job(self):
        self.wake.side_effect = OSError("wake unavailable")
        self.assertEqual(self.bind().enqueue_initial_inventory(CHANNEL, expected_epoch=7)["status"], "queued")
        self.jobs.enqueue_initial_inventory.assert_called_once(); self.jobs.fail_initial_inventory.assert_not_called()
        self.provider.assert_not_called()

    def test_unknown_enqueue_ack_does_not_wake_or_retry(self):
        self.jobs.enqueue_initial_inventory.side_effect = TimeoutError("ack lost")
        with self.assertRaises(TimeoutError): self.bind().enqueue_initial_inventory(CHANNEL, expected_epoch=7)
        self.wake.assert_not_called(); self.jobs.enqueue_initial_inventory.assert_called_once()
        self.provider.assert_not_called()

    def test_invalid_captured_configuration_cannot_fall_back_to_later_environment(self):
        os.environ["OPERATOR_JOB_CHUNK_SIZE"] = "0"
        context = self.bind()
        os.environ["OPERATOR_JOB_CHUNK_SIZE"] = "50"
        with self.assertRaises(ValueError): context.enqueue_initial_inventory(CHANNEL, expected_epoch=7)
        self.jobs.enqueue_initial_inventory.assert_not_called(); self.wake.assert_not_called()

    def test_captured_drain_rejects_before_repositories_and_wake(self):
        os.environ["OPERATOR_JOBS_MODE"] = "drain"
        context = self.bind()
        os.environ["OPERATOR_JOBS_MODE"] = "durable"
        with self.assertRaisesRegex(RuntimeError, "operator_jobs_draining"):
            context.enqueue_initial_inventory(CHANNEL, expected_epoch=7)
        self.channel_factory.assert_not_called(); self.jobs_factory.assert_not_called()
        self.wake.assert_not_called(); self.provider.assert_not_called()

    def test_captured_invalid_mode_cannot_adopt_later_valid_environment(self):
        os.environ["OPERATOR_JOBS_MODE"] = "invalid"
        context = self.bind()
        os.environ["OPERATOR_JOBS_MODE"] = "durable"
        with self.assertRaises(ValueError): context.enqueue_initial_inventory(CHANNEL, expected_epoch=7)
        self.channel_factory.assert_not_called(); self.jobs_factory.assert_not_called()
        self.wake.assert_not_called(); self.provider.assert_not_called()

    def test_original_durable_admission_survives_later_ambient_mode_change(self):
        os.environ["OPERATOR_JOBS_MODE"] = "durable"
        context = self.bind()
        os.environ["OPERATOR_JOBS_MODE"] = "drain"
        self.assertEqual(context.enqueue_initial_inventory(CHANNEL, expected_epoch=7)["status"], "queued")
        self.jobs.enqueue_initial_inventory.assert_called_once()
        self.wake.assert_called_once(); self.provider.assert_not_called()

    def test_legacy_alias_enqueues_using_same_durable_path(self):
        import io
        import operator_jobs
        os.environ["OPERATOR_JOBS_MODE"] = "legacy"
        with patch.object(operator_jobs, "_LEGACY_MODE_WARNED", False), patch("sys.stderr", new_callable=io.StringIO) as error:
            self.assertEqual(self.bind().enqueue_initial_inventory(CHANNEL, expected_epoch=7)["status"], "queued")
        self.assertIn("using durable jobs", error.getvalue())
        self.jobs.enqueue_initial_inventory.assert_called_once()
        self.wake.assert_called_once(); self.provider.assert_not_called()

    def test_context_and_composition_require_the_callback(self):
        for target in (CollectionContext, bind_collection_context):
            self.assertIs(inspect.signature(target).parameters["enqueue_initial_inventory"].default, inspect.Parameter.empty)


class InitialBridgeReadinessTests(unittest.TestCase):
    def test_old_or_incomplete_schema_fails_preflight(self):
        repository = OperatorJobRepository(object())
        for field, value in (("schema_version", 8), ("schema_version", True),
                             ("initial_inventory_scan_bridge", False), ("initial_inventory_scan_bridge", 1),
                             ("full_initial_scope_version", 0), ("initial_total_limits_enforced", True),
                             ("initial_chunk_pages", 5), ("initial_chunk_pages", True)):
            with self.subTest(field=field, value=value):
                with patch("db.jobs.operator_jobs.capabilities.assert_operator_jobs_ready", return_value={**SCHEMA, field: value}) as check:
                    with self.assertRaisesRegex(RuntimeError, "bridge_schema_required"): repository.schema_status()
                check.assert_called_once_with(repository.database)

    def test_valid_preflight_preserves_other_readiness_fields(self):
        repository = OperatorJobRepository(object())
        with patch("db.jobs.operator_jobs.capabilities.assert_operator_jobs_ready", return_value=SCHEMA) as check:
            self.assertEqual(repository.schema_status(), SCHEMA)
        check.assert_called_once_with(repository.database)


if __name__ == "__main__": unittest.main()
