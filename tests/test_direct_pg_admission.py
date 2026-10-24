"""Direct-PG admission primitives; no network, database, YouTube or server."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import hot_runtime as runtime  # noqa: E402
from self_hosted_auth_protocol import HEADER, valid_worker_proof  # noqa: E402


DATABASE_ID = '91f5d9f3-9230-4ee6-b170-155d4c3c1719'
OTHER_DATABASE_ID = 'a8d5ca52-8a14-4b7d-bfe0-c2aa0c6d9192'
SECRET = 'internal_runtime_secret_0123456789'
ROTATED_SECRET = 'rotated_runtime_secret_9876543210'
URL = 'http://127.0.0.1:8005/internal/hot-runtime'
GATEWAY = 'http://gateway:8080'
LEGACY_KEY = 'legacy.database.service.key'


class Controller:
    def __init__(self, status='ready'):
        self.current = status

    def status(self):
        return {'status': self.current, 'epoch': 'a' * 32,
                **{name:{'allowed': self.current=='ready', 'reason': 'ready' if self.current=='ready' else 'validation_pending'}
                   for name in ('api_read','collection','user_work')}}


class DirectAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.config = runtime.HotAdmissionConfig(DATABASE_ID, SECRET)
        self.wall = 1_000_000.0
        self.mono = 100.0
        self.controller = Controller()
        environment = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_TRANSPORT': 'postgres',
                                             'LIC_DATABASE_ID': DATABASE_ID, 'LIC_HOT_RUNTIME_SECRET': SECRET}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def proof(self, config=None, **kwargs):
        return runtime.signed_status(self.controller, admission=config or self.config, now=self.wall, **kwargs)

    def remote(self, config=None, fetch=None):
        return runtime.RemoteAdmission(
            URL, admission=config or self.config,
            fetch=fetch or self.proof, clock=lambda: self.wall, monotonic=lambda: self.mono,
        )

    def assertDenied(self, action, code='hot_runtime_not_configured'):
        with self.assertRaises(runtime.HotRuntimeUnavailable) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_identity_is_stable_and_independent_of_secret(self):
        rotated = runtime.HotAdmissionConfig(DATABASE_ID, ROTATED_SECRET)
        other = runtime.HotAdmissionConfig(OTHER_DATABASE_ID, SECRET)
        self.assertEqual(self.config.identity, rotated.identity)
        self.assertNotEqual(self.config.identity, other.identity)
        self.assertNotEqual(self.config.identity, runtime.database_identity(GATEWAY))
        self.assertEqual(len(self.config.identity), 64)

    def test_secret_is_never_rendered_in_config(self):
        self.assertNotIn(SECRET, repr(self.config))
        self.assertIn(DATABASE_ID, repr(self.config))
        with self.assertRaises(AttributeError):
            self.config.secret = ROTATED_SECRET

    def test_uuid_must_be_canonical_non_nil(self):
        for value in (None, '', DATABASE_ID.upper(), DATABASE_ID.replace('-', ''),
                      ' ' + DATABASE_ID, '00000000-0000-0000-0000-000000000000',
                      'postgresql://login:secret@db/app', uuid_object()):
            with self.subTest(value_type=type(value).__name__):
                self.assertDenied(lambda: runtime.HotAdmissionConfig(value, SECRET))

    def test_secret_must_be_bounded_url_safe_token(self):
        for value in (None, b'x' * 32, '', 'a' * 31, 'a' * 129, 'a' * 31 + ' ',
                      'a' * 31 + '\n', 'a' * 31 + '/', 'a' * 31 + 'é'):
            with self.subTest(value_type=type(value).__name__):
                self.assertDenied(lambda: runtime.HotAdmissionConfig(DATABASE_ID, value))
        for value in ('a' * 32, 'Z_9-' * 32):
            self.assertEqual(runtime.HotAdmissionConfig(DATABASE_ID, value).secret, value)

    def test_missing_and_legacy_configuration_are_rejected(self):
        for values in ({}, {'LIC_DATABASE_TRANSPORT': 'postgrest',
                           'SUPABASE_URL': GATEWAY, 'SUPABASE_KEY': LEGACY_KEY}):
            self.assertDenied(lambda: runtime.load_hot_admission_config(values))

    def test_explicit_config_loads_without_gateway_or_database_credentials(self):
        loaded = runtime.load_hot_admission_config({
            'LIC_DATABASE_TRANSPORT': 'postgres',
            'LIC_DATABASE_ID': DATABASE_ID, 'LIC_HOT_RUNTIME_SECRET': SECRET,
        })
        self.assertEqual(loaded, self.config)

    def test_explicit_config_never_falls_back_to_gateway_credentials(self):
        legacy = {'SUPABASE_URL': GATEWAY, 'SUPABASE_KEY': LEGACY_KEY}
        variants = (
            {'LIC_DATABASE_TRANSPORT': 'postgres'},
            {'LIC_DATABASE_ID': DATABASE_ID},
            {'LIC_HOT_RUNTIME_SECRET': SECRET},
            {'LIC_DATABASE_ID': '', 'LIC_HOT_RUNTIME_SECRET': SECRET},
            {'LIC_DATABASE_ID': DATABASE_ID, 'LIC_HOT_RUNTIME_SECRET': ''},
        )
        for values in variants:
            with self.subTest(fields=sorted(values)):
                self.assertDenied(lambda: runtime.load_hot_admission_config({**legacy, **values}))

    def test_unknown_transport_is_not_silently_defaulted(self):
        for value in ('', 'direct', 'postgres ', 'POSTGRES', None):
            self.assertDenied(lambda: runtime.load_hot_admission_config({'LIC_DATABASE_TRANSPORT': value}))

    def test_proof_round_trip_uses_explicit_identity(self):
        proof = self.proof()
        self.assertEqual(proof['database'], self.config.identity)
        self.assertNotIn(SECRET, json.dumps(proof))
        self.assertNotIn(DATABASE_ID, json.dumps(proof))
        self.remote(fetch=lambda: proof).require_ready()

    def test_v2_proof_contains_separate_signed_switches(self):
        proof = runtime.signed_status(self.controller, GATEWAY + '/', LEGACY_KEY, now=self.wall)
        expected = {
            'version': 2, 'profile': 'self-hosted',
            'database': hashlib.sha256(GATEWAY.encode()).hexdigest(),
            'status': 'ready', 'epoch': 'a' * 32,
            'issued_at': self.wall, 'expires_at': self.wall + 5,
            **{name:{'allowed':True,'reason':'ready'} for name in ('api_read','collection','user_work')},
        }
        signature = hmac.new(LEGACY_KEY.encode(), b'lic-hot-runtime-v2\0' +
                             json.dumps(expected, sort_keys=True, separators=(',', ':')).encode(),
                             hashlib.sha256).hexdigest()
        self.assertEqual(proof, {**expected, 'signature': signature})
        runtime.RemoteAdmission(URL, GATEWAY, LEGACY_KEY, fetch=lambda: proof,
                                clock=lambda: self.wall, monotonic=lambda: self.mono).require_ready()

    def test_explicit_and_legacy_proofs_cannot_mix_even_with_the_same_key(self):
        legacy = runtime.signed_status(self.controller, GATEWAY, SECRET, now=self.wall)
        explicit = self.proof()
        self.assertDenied(self.remote(fetch=lambda: legacy).require_ready, 'hot_runtime_not_ready')
        reader = runtime.RemoteAdmission(URL, GATEWAY, SECRET, fetch=lambda: explicit,
                                         clock=lambda: self.wall, monotonic=lambda: self.mono)
        self.assertDenied(reader.require_ready, 'hot_runtime_not_ready')

    def test_config_and_legacy_arguments_cannot_be_combined(self):
        self.assertDenied(lambda: runtime.signed_status(
            self.controller, GATEWAY, LEGACY_KEY, admission=self.config))
        self.assertDenied(lambda: runtime.signed_status(self.controller, key=LEGACY_KEY, admission=self.config))
        self.assertDenied(lambda: runtime.RemoteAdmission(URL, GATEWAY, LEGACY_KEY, admission=self.config))
        self.assertDenied(lambda: runtime.RuntimeOwnerLock(GATEWAY, admission=self.config))
        self.assertDenied(lambda: runtime.signed_status(self.controller, admission=object()))

    def test_a_different_database_cannot_reuse_a_valid_proof(self):
        other = runtime.HotAdmissionConfig(OTHER_DATABASE_ID, SECRET)
        self.assertDenied(self.remote(other, fetch=self.proof).require_ready, 'hot_runtime_not_ready')

    def test_rotated_secret_rejects_old_proofs_and_accepts_new_proofs(self):
        rotated = runtime.HotAdmissionConfig(DATABASE_ID, ROTATED_SECRET)
        old_proof = self.proof()
        reader = self.remote(rotated, fetch=lambda: old_proof)
        self.assertDenied(reader.require_ready, 'hot_runtime_not_ready')
        reader.fetch = lambda: self.proof(rotated)
        reader.require_ready()

    def test_worker_request_uses_internal_secret_with_unchanged_auth_protocol(self):
        headers = self.remote()._request_headers()
        self.assertEqual(set(headers), {HEADER})
        self.assertTrue(valid_worker_proof(headers[HEADER], SECRET, now=self.wall))
        self.assertFalse(valid_worker_proof(headers[HEADER], LEGACY_KEY, now=self.wall))

    def test_proof_expires_at_the_deadline(self):
        proof = self.proof()
        reader = self.remote(fetch=lambda: proof)
        self.wall += 4.9
        self.mono += 4.9
        reader.require_ready()
        self.wall += 0.1
        self.mono += 0.1
        self.assertDenied(reader.require_ready, 'hot_runtime_not_ready')

    def test_clock_drift_invalidates_cached_readiness(self):
        fetch = Mock(side_effect=[self.proof(), OSError('private response must not be displayed')])
        reader = self.remote(fetch=fetch)
        reader.require_ready()
        self.wall -= 3
        self.mono += 0.1
        self.assertDenied(reader.require_ready, 'hot_runtime_not_ready')
        self.assertEqual(fetch.call_count, 2)

    def test_cache_is_bounded_and_failed_refresh_does_not_reuse_it(self):
        fetch = Mock(side_effect=[self.proof(), OSError(SECRET), OSError(SECRET)])
        reader = self.remote(fetch=fetch)
        reader.require_ready()
        self.wall += 0.9
        self.mono += 0.9
        reader.require_ready()
        self.assertEqual(fetch.call_count, 1)
        self.wall += 0.2
        self.mono += 0.2
        self.assertDenied(reader.require_ready, 'hot_runtime_not_ready')
        self.assertDenied(reader.require_ready, 'hot_runtime_not_ready')
        self.assertEqual(fetch.call_count, 3)

    def test_blocked_and_tampered_proofs_are_rejected(self):
        self.controller.current = 'blocked'
        self.assertDenied(self.remote().require_ready, 'hot_runtime_not_ready')
        proof = self.proof()
        proof['status'] = 'ready'
        self.assertDenied(self.remote(fetch=lambda: proof).require_ready, 'hot_runtime_not_ready')

    def test_invalid_remote_locations_are_rejected(self):
        for url in ('https://127.0.0.1/internal/hot-runtime', 'http://example.com/internal/hot-runtime',
                    URL + '?x=1', URL + '#x', URL + '/', 'http://user:pass@127.0.0.1/internal/hot-runtime'):
            self.assertDenied(lambda: runtime.RemoteAdmission(url, admission=self.config))

    def test_explicit_configuration_obeys_local_readiness(self):
        controller = Mock()
        with patch.object(runtime, '_local_controller', controller), patch.dict(os.environ, {
            'LIC_DATABASE_TRANSPORT': 'postgres',
            'LIC_DATABASE_ID': DATABASE_ID, 'LIC_HOT_RUNTIME_SECRET': SECRET,
        }):
            runtime.require_hot_runtime_ready()
            controller.require_collection_ready.side_effect = runtime.HotRuntimeUnavailable('hot_runtime_not_ready')
            self.assertDenied(runtime.require_hot_runtime_ready, 'hot_runtime_not_ready')
        self.assertEqual(controller.require_collection_ready.call_count, 2)

    def test_partial_configuration_cannot_reuse_remote_or_local_readiness(self):
        for local in (None, Mock()):
            with self.subTest(local=local is not None), patch.object(runtime, '_local_controller', local), \
                    patch.object(runtime, '_remote', Mock()) as cached, \
                    patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_DATABASE_ID': DATABASE_ID}, clear=True):
                self.assertDenied(runtime.require_hot_runtime_ready)
                cached.require_ready.assert_not_called()

    def test_valid_explicit_pair_cannot_mix_with_legacy_transport(self):
        with patch.dict(os.environ, {
            'LIC_DATABASE_TRANSPORT': 'postgrest',
            'SUPABASE_URL': GATEWAY, 'SUPABASE_KEY': LEGACY_KEY,
            'LIC_DATABASE_ID': DATABASE_ID, 'LIC_HOT_RUNTIME_SECRET': SECRET,
        }):
            self.assertDenied(runtime.require_hot_runtime_ready)

    def test_explicit_configuration_rotation_replaces_cached_remote(self):
        with patch.object(runtime, '_local_controller', None), patch.object(runtime, '_remote', None), \
                patch.object(runtime, '_remote_configuration', None), \
                patch.object(runtime, 'RemoteAdmission') as factory, patch.dict(os.environ, {
                    'LIC_HOT_RUNTIME_URL': URL, 'SUPABASE_URL': GATEWAY, 'SUPABASE_KEY': LEGACY_KEY,
                }):
            runtime.require_hot_runtime_ready()
            runtime.require_hot_runtime_ready()
            self.assertEqual(factory.call_count, 1)
            os.environ['LIC_HOT_RUNTIME_SECRET'] = ROTATED_SECRET
            runtime.require_hot_runtime_ready()
            self.assertEqual(factory.call_count, 2)
            os.environ['LIC_DATABASE_ID'] = OTHER_DATABASE_ID
            runtime.require_hot_runtime_ready()
            self.assertEqual(factory.call_count, 3)

    def test_maintenance_and_control_capabilities_are_not_broadened(self):
        with patch.object(runtime, '_local_controller', None), patch.object(runtime, 'RemoteAdmission') as factory:
            with runtime.maintenance_access():
                runtime.require_hot_runtime_ready(rpc_name='cleanup_hot_evidence')
                self.assertDenied(lambda: runtime.require_hot_runtime_ready(rpc_name='claim_operator_job'),
                                  'hot_runtime_maintenance_scope_denied')
                self.assertDenied(lambda: runtime.require_hot_runtime_ready(operation='http'),
                                  'hot_runtime_maintenance_scope_denied')
                with runtime.collection_control_access():
                    runtime.require_hot_runtime_ready(rpc_name='abort_hot_collection_claims')
                    self.assertDenied(lambda: runtime.require_hot_runtime_ready(rpc_name='cleanup_hot_evidence'),
                                      'hot_runtime_control_scope_denied')
            factory.assert_not_called()

    def test_secret_rotation_does_not_create_a_second_maintenance_owner(self):
        rotated = runtime.HotAdmissionConfig(DATABASE_ID, ROTATED_SECRET)
        with tempfile.TemporaryDirectory() as directory:
            first = runtime.RuntimeOwnerLock(admission=self.config, directory=directory)
            second = runtime.RuntimeOwnerLock(admission=rotated, directory=directory)
            self.assertEqual(first.name, second.name)
            with first:
                self.assertDenied(second.__enter__, 'hot_runtime_lock_unavailable')
            with second:
                self.assertIsNotNone(second.stream)

    def test_distinct_database_identities_have_distinct_owner_locks(self):
        other = runtime.HotAdmissionConfig(OTHER_DATABASE_ID, SECRET)
        with tempfile.TemporaryDirectory() as directory:
            with runtime.RuntimeOwnerLock(admission=self.config, directory=directory) as first:
                with runtime.RuntimeOwnerLock(admission=other, directory=directory) as second:
                    self.assertNotEqual(first.name, second.name)
        legacy = runtime.RuntimeOwnerLock(GATEWAY, directory=ROOT)
        self.assertEqual(legacy.name, 'hot-runtime-' + runtime.database_identity(GATEWAY)[:24] + '.lock')


def uuid_object():
    import uuid
    return uuid.UUID(DATABASE_ID)


if __name__ == '__main__':
    unittest.main()
