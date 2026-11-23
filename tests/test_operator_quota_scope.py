"""Standalone CLI/main reaches the real provider gate without real I/O."""
from contextlib import contextmanager, nullcontext
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from collection_schedule import CollectionSchedules, discovery_schedules
import quota_buckets as quota
import youtube_client


class OperatorQuotaScopeTests(unittest.TestCase):
    def setUp(self):
        quota.end_pass()
        self.addCleanup(quota.end_pass)
        self.events, self.failures = [], []
        self.database = object()
        self.storage = Mock()
        self.storage.snapshot.side_effect = lambda: dict(quota_day=quota.quota_day(), exhausted=())
        self.recorder = Mock()
        self.recorder.start_attempt.return_value = 'synthetic-attempt'
        self.handle = Mock()
        self.jobs = Mock(database=self.database)
        self.jobs.schema_status.return_value = dict(ready=True, schema_version=49,
                                                   initial_quota_limit_enforced=False)

        @contextmanager
        def resources():
            self.assertFalse(quota.control_active())
            self.events.append('database.open')
            try:
                yield
            finally:
                self.assertFalse(quota.control_active())
                self.events.append('database.close')

        self.core = SimpleNamespace(database=self.database, process_database_resources=resources,
            API_KEY=None, YOUTUBE_QUOTA_ATTEMPT_RECORDER=self.recorder,
            YOUTUBE_API_TIMEOUT_SECONDS=10, COLLECTION_SCHEDULES=discovery_schedules(),
            YOUTUBE_QUOTA_PROJECT_ID='standalone-project')
        spec = importlib.util.spec_from_file_location('operator_quota_worker',
            ROOT / 'application/scripts/run_operator_job_worker.py')
        self.worker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'core': self.core}):
            spec.loader.exec_module(self.worker)
        self.response = Mock(status_code=200, headers={}, is_redirect=False)
        self.response.iter_content.return_value = [b'{}']
        self.response.json.return_value = {}

    def run_cli(self, bucket='general', *, model=None, transport_error=None, argv=None):
        self.worker.COLLECTION_SCHEDULES = model or discovery_schedules()
        expected_control = self.worker.COLLECTION_SCHEDULES.discovery is not None

        def callback():
            self.assertEqual(quota.control_active(), expected_control)
            self.events.append('callback')
            try:
                return youtube_client.youtube_api_get(
                    'videos' if bucket == 'general' else 'videos:batchGetStats',
                    phase='operator_test', quota_bucket=bucket, attempt_recorder=self.recorder,
                    params={'id': 'synthetic01', **({'part': 'snippet'} if bucket == 'general' else {})})
            except Exception as error:
                self.failures.append(error)
                raise

        def chunk(repository, run_stats, **kwargs):
            self.assertIs(repository, self.jobs)
            run_stats()
            return dict(status='completed', processed_video_ids=1)

        with patch.dict(sys.modules, {'core': self.core}), \
                patch.dict(os.environ, {'DATABASE_VIDEO_COLLECTION_ENABLED': 'true',
                    'OPERATOR_JOBS_MODE': 'durable', 'LIC_COLLECTION_INTERVAL_MINUTES': 'invalid-parent-value',
                    'LIC_SETUP_DIRECTORY': ''}), \
                patch.object(self.worker, 'acquire_shared_sync_lock', return_value=self.handle), \
                patch.object(self.worker, 'fcntl', SimpleNamespace(flock=Mock(), LOCK_UN=8)), \
                patch.object(self.worker, 'fresh_operator_job_repository',
                             side_effect=lambda: nullcontext(self.jobs)), \
                patch.object(self.worker, 'load_operator_job_settings', return_value=object()), \
                patch.object(self.worker, '_collection_callbacks', return_value=dict(run_stats=callback,
                    run_initial_inventory_chunk=Mock(), run_initial_stats=Mock())), \
                patch.object(self.worker, 'run_one_operator_job_chunk', side_effect=chunk), \
                patch('collection_shutdown.yield_requested', return_value=False), \
                patch('collection_shutdown.finish_current_chunk', side_effect=lambda: nullcontext()), \
                patch('db.quota.provider_quota_control.ProviderQuotaControlRepository',
                      return_value=self.storage) as repository, \
                patch('db.discovery_budget.DiscoveryBudgetRepository.reserve',
                      side_effect=AssertionError('manual_allowance_debit')) as reserve, \
                patch('provider_runtime.admit_request', side_effect=lambda **kw: nullcontext()), \
                patch('hot_runtime.require_hot_runtime_ready'), \
                patch('youtube_client.requests.get', return_value=self.response,
                      side_effect=transport_error) as http, \
                patch('socket.socket.connect', side_effect=AssertionError('network_forbidden')), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            result = self.worker.cli(argv or ['--auto-sync-interval-minutes', '30'])
        reserve.assert_not_called()
        self.assertFalse(quota.control_active())
        if expected_control:
            repository.assert_called_once_with(self.database, project_id='standalone-project',
                settings=self.worker.COLLECTION_SCHEDULES.discovery, interval_minutes=30)
        else:
            repository.assert_not_called()
        return result, http

    def test_manual_cli_uses_explicit_interval_and_never_debits_discovery(self):
        result, http = self.run_cli()
        self.assertEqual(result, 0)
        http.assert_called_once()
        self.assertEqual(self.events, ['database.open', 'callback', 'database.close'])
        self.storage.snapshot.assert_called_once()
        self.storage.note_exhausted.assert_not_called()

    def test_existing_ban_blocks_only_its_bucket_before_attempt_or_transport(self):
        for blocked, refused, permitted in (
                ('general', 'general', 'video_batch_get_stats'),
                ('stats', 'video_batch_get_stats', 'general')):
            with self.subTest(blocked=blocked):
                self.storage.snapshot.side_effect = lambda: dict(quota_day=quota.quota_day(), exhausted=(blocked,))
                self.recorder.reset_mock()
                result, http = self.run_cli(refused)
                self.assertEqual(result, 1)
                http.assert_not_called()
                self.recorder.start_attempt.assert_not_called()
                self.assertFalse(self.failures[-1].lic_http_attempted)
                self.assertEqual(self.failures[-1].lic_quota_remembered, blocked)
                result, http = self.run_cli(permitted)
                self.assertEqual(result, 0)
                http.assert_called_once()

    def test_unknown_control_fails_closed_then_next_scope_can_recover(self):
        self.storage.snapshot.side_effect = OSError('synthetic-read-failure')
        result, http = self.run_cli()
        self.assertEqual(result, 1)
        http.assert_not_called()
        self.recorder.start_attempt.assert_not_called()
        self.assertIsInstance(self.failures[-1], quota.QuotaStateUnavailable)
        self.assertEqual(self.failures[-1].reason, 'quota_state_unavailable')
        self.storage.note_exhausted.assert_not_called()
        self.storage.snapshot.side_effect = lambda: dict(quota_day=quota.quota_day(), exhausted=())
        result, http = self.run_cli()
        self.assertEqual(result, 0)
        http.assert_called_once()
        self.assertEqual(self.events.count('database.close'), 2)

    def test_transport_exception_unwinds_control_before_database_close(self):
        result, http = self.run_cli(transport_error=TimeoutError('synthetic-timeout'))
        self.assertEqual(result, 1)
        http.assert_called_once()
        self.assertEqual(self.events[-1], 'database.close')
        self.handle.close.assert_called_once()
        self.storage.note_exhausted.assert_not_called()

    def test_v4_remains_unbound_even_if_operational_storage_is_unavailable(self):
        self.storage.snapshot.side_effect = AssertionError('v4_control_read')
        result, http = self.run_cli(model=CollectionSchedules())
        self.assertEqual(result, 0)
        http.assert_called_once()
        self.storage.snapshot.assert_not_called()

    def test_invalid_cli_interval_rejected_before_resources(self):
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            self.worker.cli(['--auto-sync-interval-minutes', '17'])
        self.assertEqual(self.events, [])
        self.assertFalse(quota.control_active())


if __name__ == '__main__':
    unittest.main()
