"""Standalone command integration; local lock simulator, no HTTP/DB/native flock."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
import provider_admission as pa
import provider_runtime as pr
from test_provider_admission import BOOT, GENERATION, IDENTITY, ThreadLockSimulator


class ExistingGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'provider-admission'
        self.locks = ThreadLockSimulator()
        self.config = pa.AdmissionConfig(IDENTITY, GENERATION, BOOT, 8)
        self.backend = pa.AdmissionGate.initialize(self.directory, self.config, _locks=self.locks)

    def join(self, **overrides):
        return pa.AdmissionGate.join_existing(self.directory, **{
            'installation_id': IDENTITY, 'boot_id': BOOT, '_locks': self.locks, **overrides})

    def files(self):
        return {p.name: (p.stat().st_ino, p.read_bytes()) for p in self.directory.iterdir()}

    def test_join_preserves_exact_state_inodes_and_captured_config(self):
        with self.backend.acquire('auto'):
            before = self.files()
            with patch.object(pa.AdmissionGate, 'initialize') as initialize, \
                    patch.object(pa.AdmissionGate, 'for_backend') as startup, \
                    patch.object(pa.AdmissionGate, '_save', side_effect=AssertionError('join wrote state')):
                joined = self.join()
            self.assertEqual(joined.config, self.config)
            self.assertEqual(before, self.files())
            initialize.assert_not_called()
            startup.assert_not_called()
        self.assertEqual(self.backend.snapshot()['tickets'], 0)

    def test_missing_gate_never_creates_directory(self):
        absent = Path(self.temp.name) / 'absent'
        with self.assertRaises(pa.AdmissionUnavailable):
            pa.AdmissionGate.join_existing(absent, installation_id=IDENTITY, boot_id=BOOT, _locks=self.locks)
        self.assertFalse(absent.exists())

    def test_missing_state_or_lock_is_not_repaired(self):
        for name in ('state.json', 'guard.lock', 'slot-7.lock', 'ticket-15.lock'):
            with self.subTest(name=name):
                path = self.directory / name
                saved = self.directory / (name + '.saved')
                path.rename(saved)
                try:
                    before = self.files()
                    with self.assertRaises(pa.AdmissionUnavailable):
                        self.join()
                    self.assertEqual(before, self.files())
                    self.assertFalse(path.exists())
                finally:
                    saved.rename(path)

    def test_corrupt_unbounded_duplicate_or_invalid_full_state_refused_without_write(self):
        path = self.directory / 'state.json'
        original = path.read_bytes()
        parsed = json.loads(original)
        cases = [b'', b'not-json', b'[]', b'x' * (pa.MAX_STATE_BYTES + 1),
                 b'{"concurrency":8,' + original[1:],
                 json.dumps({**parsed, 'concurrency': True}).encode(),
                 json.dumps({**parsed, 'generation': 'invalid'}).encode(),
                 json.dumps({**parsed, 'effective': 9}).encode(),
                 json.dumps({**parsed, 'unexpected': 'private'}).encode()]
        for raw in cases:
            with self.subTest(size=len(raw)):
                path.write_bytes(raw)
                before = self.files()
                with self.assertRaises(pa.AdmissionUnavailable):
                    self.join()
                self.assertEqual(before, self.files())
        path.write_bytes(original)

    def test_wrong_installation_or_boot_cannot_adopt_or_reconfigure(self):
        before = self.files()
        for values in ({'installation_id': GENERATION}, {'boot_id': GENERATION},
                       {'installation_id': None}, {'boot_id': 'bad'}):
            with self.subTest(values=values), self.assertRaises(pa.AdmissionUnavailable):
                self.join(**values)
        self.assertEqual(before, self.files())

    def test_join_shares_occupied_ceiling_without_private_slots(self):
        with contextlib.ExitStack() as stack:
            for _ in range(8):
                stack.enter_context(self.backend.acquire('auto'))
            joined = self.join()
            with self.assertRaises(pa.AdmissionTimeout):
                joined.acquire('interactive', timeout=0.01)
            self.assertEqual(self.backend.snapshot()['active'], 8)
        self.assertEqual(self.backend.snapshot()['tickets'], 0)

    def test_reconfiguration_after_join_fails_at_acquire_without_rejoin(self):
        joined = self.join()
        replacement = pa.AdmissionGate.for_backend(self.directory, installation_id=IDENTITY,
            boot_id=BOOT, concurrency=4, _locks=self.locks)
        before = self.files()
        with self.assertRaises(pa.AdmissionUnavailable):
            joined.acquire('interactive')
        self.assertEqual(before, self.files())
        self.assertEqual(replacement.snapshot()['active'], 0)

    def test_full_validation_under_same_guard_rejects_changed_capture(self):
        original = pa.AdmissionGate._load
        def changed(gate, guard):
            # Simulate a noncooperating state replacement before full validation.
            # A legitimate backend is excluded here by the held existing guard.
            path = self.directory / 'state.json'
            state = json.loads(path.read_bytes())
            state['generation'] = BOOT
            path.write_text(json.dumps(state), encoding='utf-8')
            return original(gate, guard)
        with patch.object(pa.AdmissionGate, '_load', changed), self.assertRaises(pa.AdmissionUnavailable):
            self.join()


class CommandRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNone(pr._runtime)
        self.addCleanup(lambda: pr.stop_runtime(pr._runtime))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / 'owner.db'
        self.locks = ThreadLockSimulator()
        self.backend = pa.AdmissionGate.initialize(self.root / 'provider-admission',
            pa.AdmissionConfig(IDENTITY, GENERATION, BOOT, 8), _locks=self.locks)
        self.environment = {'LIC_AUTH_DATABASE': str(self.auth), 'LIC_DATABASE_ID': IDENTITY,
                            'LIC_SETUP_DIRECTORY': str(self.root),
                            'LIC_DEPLOYMENT_PROFILE': 'self-hosted',
                            'LIC_YOUTUBE_REQUEST_CONCURRENCY': '16'}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(pa, '_Flock', return_value=self.locks))
        self.stack.enter_context(patch.object(pr, '_boot_id', return_value=BOOT))

    def test_command_uses_existing_c_without_snapshot_or_environment_setting(self):
        with patch.object(pa.AdmissionGate, 'initialize') as initialize, \
                patch.object(pa.AdmissionGate, 'for_backend') as startup:
            runtime = pr.start_command(self.environment)
        self.assertEqual(runtime.gate.config.concurrency, 8)
        self.assertEqual(runtime.environment()['LIC_YOUTUBE_REQUEST_CONCURRENCY'], '8')
        initialize.assert_not_called()
        startup.assert_not_called()
        with pr.admit_request():
            self.assertEqual(self.backend.snapshot()['active'], 1)
        pr.stop_runtime(runtime)
        self.assertTrue(runtime.stop.is_set())
        self.assertIsNone(pr._runtime)
        self.assertEqual(self.backend.snapshot()['tickets'], 0)

    def test_command_start_denial_installs_no_runtime_or_fallback(self):
        for reason in ('missing', 'corrupt', 'wrong-boot'):
            path = self.root / 'provider-admission' / 'state.json'
            original = path.read_bytes()
            if reason == 'missing':
                path.rename(path.with_suffix('.saved'))
            elif reason == 'corrupt':
                path.write_bytes(b'{private-invalid')
            else:
                state = json.loads(original)
                state['boot_id'] = GENERATION
                path.write_text(json.dumps(state), encoding='utf-8')
            try:
                with self.subTest(reason=reason), self.assertRaises(pa.AdmissionUnavailable):
                    pr.start_command(self.environment)
                self.assertIsNone(pr._runtime)
            finally:
                if reason == 'missing':
                    path.with_suffix('.saved').rename(path)
                else:
                    path.write_bytes(original)

    def owner(self, *, failure=None, command='check-api-key', denied=False):
        from ops import self_hosted_owner as owner
        import self_hosted_configuration as configuration
        import self_hosted_onboarding as onboarding
        store = Mock()
        store.read.return_value = {'api_key': 'synthetic-key-no-http', 'revision': 0,
            'error': None, 'settings': {'youtube_request_concurrency': 16}}
        events, runtimes = [], []
        @contextlib.contextmanager
        def resources():
            events.append('db-open')
            try:
                yield
            finally:
                events.append('db-close')
        actual_start, actual_stop = pr.start_command, pr.stop_runtime
        def start(environment):
            events.append('start')
            self.assertEqual(environment['LIC_AUTH_DATABASE'], str(self.auth))
            if denied:
                raise pa.AdmissionUnavailable('synthetic-source-free-denial')
            runtime = actual_start(environment)
            runtimes.append(runtime)
            return runtime
        def stop(runtime):
            events.append('stop')
            actual_stop(runtime)
        def check(key):
            self.assertEqual(key, 'synthetic-key-no-http')
            self.assertEqual(pr._scope.get()[0], 'interactive')
            with pr.admit_request():
                events.append('request')
                self.assertEqual(pr._runtime.gate.config.concurrency, 8)
                self.assertEqual(self.backend.snapshot()['active'], 1)
                if failure:
                    raise failure
            return None
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {**self.environment, 'LIC_AUTH_DATABASE': str(self.root / 'wrong' / 'owner.db')}), \
                patch.object(sys, 'argv', ['owner', '--database', str(self.auth), command]), \
                patch.object(owner, 'OwnerAuthRepository', return_value=Mock(configured=lambda: True,
                    sign_in_limited=lambda now: False)), \
                patch.object(configuration, 'ConfigurationStore', return_value=store), \
                patch.object(onboarding, 'check_api_key', check), \
                patch.dict(sys.modules, {'core': types.SimpleNamespace(process_database_resources=resources)}), \
                patch.object(pr, 'start_command', start), patch.object(pr, 'stop_runtime', stop), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if failure:
                with self.assertRaises(type(failure)):
                    owner.main()
                code = None
            else:
                code = owner.main()
        self.assertIsNone(pr._runtime)
        self.assertTrue(all(runtime.stop.is_set() for runtime in runtimes))
        self.assertEqual(self.backend.snapshot()['tickets'], 0)
        return code, events, out.getvalue(), err.getvalue(), store

    def test_owner_one_request_start_stop_preserves_key_response(self):
        code, events, output, error, store = self.owner()
        self.assertEqual(code, 0)
        self.assertEqual(events, ['start', 'db-open', 'request', 'db-close', 'stop'])
        self.assertEqual(json.loads(output)['provider_requests'], 1)
        self.assertEqual(json.loads(output)['status'], 'key_accepted')
        self.assertEqual(error, '')
        store.update.assert_not_called()

    def test_owner_request_failure_closes_runtime_and_releases_permit(self):
        code, events, output, error, store = self.owner(failure=RuntimeError('synthetic'))
        self.assertEqual(events, ['start', 'db-open', 'request', 'db-close', 'stop'])
        self.assertEqual(output, '')
        store.update.assert_not_called()

    def test_owner_start_denied_has_no_provider_db_or_key_mutation(self):
        code, events, output, error, store = self.owner(denied=True)
        self.assertEqual(code, 1)
        self.assertEqual(events, ['start'])
        self.assertEqual(output, '')
        self.assertIn('Owner operation refused', error)
        self.assertNotIn('synthetic', error)
        store.update.assert_not_called()

    def test_db_only_status_does_not_join_provider_runtime(self):
        code, events, output, error, store = self.owner(command='status')
        self.assertEqual(code, 0)
        self.assertEqual(events, [])
        self.assertTrue(json.loads(output)['configured'])
        store.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
