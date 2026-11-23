"""Explicit composition, request classes and shutdown; no network or DB."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_provider_admission import IDENTITY, GENERATION, BOOT
from provider_admission import AdmissionConfig, AdmissionUnavailable, AdmissionCancelled
import provider_runtime as pr


class Gate:
    def __init__(self):
        self.config = AdmissionConfig(IDENTITY, GENERATION, BOOT, 8)
        self.calls = []

    @contextmanager
    def acquire(self, kind, *, timeout, cancel):
        self.calls.append((kind, timeout, cancel))
        if cancel.is_set():
            raise AdmissionCancelled('provider_admission_cancelled')
        yield

    def _notify(self):
        pass


class ProviderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.gate = Gate()
        self.runtime = pr._install(self.gate)
        self.addCleanup(pr.stop_runtime, self.runtime)

    def test_default_interactive_and_nested_collection_class(self):
        with pr.admit_request():
            pass
        with pr.request_scope('auto'):
            with pr.admit_request(timeout=2):
                pass
            with pr.request_scope('operator'):
                with pr.admit_request():
                    pass
            with pr.admit_request():
                pass
        self.assertEqual([call[0] for call in self.gate.calls], ['interactive', 'auto', 'operator', 'auto'])

    def test_stopped_runtime_cancels_new_admission(self):
        self.runtime.close()
        with self.assertRaises(AdmissionCancelled):
            with pr.admit_request():
                self.fail('closed runtime admitted work')

    def test_action_cancel_is_separate_from_other_channels(self):
        import threading
        action = threading.Event()
        action.set()
        with pr.request_scope('operator', action):
            with self.assertRaises(AdmissionCancelled):
                with pr.admit_request():
                    self.fail('cancelled action admitted')
        with pr.request_scope('operator'):
            with pr.admit_request():
                pass

    def test_no_process_private_fallback_for_self_hosted(self):
        pr.stop_runtime(self.runtime)
        with patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted'}):
            with self.assertRaisesRegex(AdmissionUnavailable, 'not_initialized'):
                with pr.admit_request():
                    self.fail('uninitialized provider admitted')

    def test_forked_registry_and_second_owner_are_refused(self):
        with self.assertRaises(AdmissionUnavailable):
            pr._install(Gate())
        with patch.object(pr.os, 'getpid', return_value=self.runtime.pid + 1):
            with self.assertRaisesRegex(AdmissionUnavailable, 'process_changed'):
                with pr.admit_request():
                    self.fail('inherited provider admitted')

    def test_worker_only_opens_verified_snapshot_never_initializes(self):
        environment = {**self.runtime.environment(), 'LIC_DATABASE_ID': IDENTITY,
            'LIC_AUTH_DATABASE': str(Path(__file__).resolve().parent / 'owner.sqlite3'),
            'LIC_YOUTUBE_REQUEST_CONCURRENCY': '8'}
        pr.stop_runtime(self.runtime)
        gate = SimpleNamespace(snapshot=lambda: None, config=self.gate.config, _notify=lambda: None)
        with patch.object(pr, '_boot_id', return_value=BOOT), patch.object(pr, 'AdmissionGate') as factory:
            factory.return_value = gate
            worker = pr.start_worker(environment)
            self.addCleanup(pr.stop_runtime, worker)
            factory.initialize.assert_not_called()
            factory.for_backend.assert_not_called()
            self.assertEqual(factory.call_args.args[1], self.gate.config)

    def test_worker_rejects_unknown_duplicate_wrong_boot_and_changed_limit(self):
        environment = {**self.runtime.environment(), 'LIC_DATABASE_ID': IDENTITY,
            'LIC_AUTH_DATABASE': str(Path(__file__).resolve().parent / 'owner.sqlite3'),
            'LIC_YOUTUBE_REQUEST_CONCURRENCY': '8'}
        value = json.loads(environment[pr.SNAPSHOT_ENV])
        bad = ('{}', 'x', json.dumps({**value, 'unexpected': 1}),
               json.dumps({**value, 'concurrency': True}),
               json.dumps({**value, 'boot_id': GENERATION}),
               json.dumps({**value, 'concurrency': 16}),
               environment[pr.SNAPSHOT_ENV].replace('{', '{"concurrency":8,', 1))
        with patch.object(pr, '_boot_id', return_value=BOOT), patch.object(pr, 'AdmissionGate') as factory:
            for raw in bad:
                with self.subTest(raw=raw[:20]), self.assertRaises(AdmissionUnavailable):
                    pr.start_worker({**environment, pr.SNAPSHOT_ENV: raw})
            factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
