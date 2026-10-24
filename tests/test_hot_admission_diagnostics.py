"""Finite HOT denial diagnostics; no network, database or provider payloads."""
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
import hot_runtime as hot

SECRET = 'internal_runtime_secret_0123456789'
CONFIG = hot.HotAdmissionConfig('91f5d9f3-9230-4ee6-b170-155d4c3c1719', SECRET)
URL = 'http://127.0.0.1:8009/internal/hot-runtime'


class HotAdmissionDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.mono = 1000.0, 100.0
        controller = SimpleNamespace(status=lambda: {'status': 'ready', 'epoch': 'a'*32,
            **{name:{'allowed':True,'reason':'ready'} for name in ('api_read','collection','user_work')}})
        self.proof = hot.signed_status(controller, admission=CONFIG, now=self.wall)
        self.emit_patch = patch('telemetry.emit_event')
        self.emit = self.emit_patch.start()
        self.addCleanup(self.emit_patch.stop)

    def remote(self, fetch):
        return hot.RemoteAdmission(URL, admission=CONFIG, fetch=fetch,
            clock=lambda: self.wall, monotonic=lambda: self.mono)

    def signed(self, **changes):
        value = {**self.proof, **changes}
        payload = {key: item for key, item in value.items() if key != 'signature'}
        value['signature'] = hmac.new(SECRET.encode(), b'lic-hot-runtime-v2\0' + hot._canonical(payload), hashlib.sha256).hexdigest()
        return value

    def denied(self, reader, expected):
        with self.assertRaises(hot.HotRuntimeUnavailable) as caught:
            reader.require_ready()
        self.assertEqual(str(caught.exception), 'hot_runtime_not_ready')
        self.assertEqual(self.emit.call_args.kwargs['phase'], expected)
        self.assertNotIn(SECRET, str(self.emit.call_args))
        self.assertNotIn(self.proof['signature'], str(self.emit.call_args))
        self.assertNotIn('detail', self.emit.call_args.kwargs)

    def test_reasons_distinguish_transport_status_identity_signature_and_expiry(self):
        cases = [
            (Mock(side_effect=OSError(SECRET + ' private payload')), 'transport_failed'),
            (lambda: {}, 'proof_shape'),
            (lambda: {**self.proof, 'signature': 'wrong'}, 'proof_signature'),
            (lambda: self.signed(database='foreign'), 'proof_identity'),
            (lambda: self.signed(collection={'allowed':False,'reason':'collection_priority'}), 'proof_blocked'),
            (lambda: self.signed(issued_at=990.0, expires_at=995.0), 'proof_expired'),
            (lambda: self.signed(issued_at=1005.0, expires_at=1010.0), 'proof_clock'),
        ]
        for fetch, reason in cases:
            with self.subTest(reason=reason):
                self.denied(self.remote(fetch), reason)

    def test_repeated_denials_are_bounded_but_never_admitted(self):
        fetch = Mock(side_effect=OSError(SECRET))
        reader = self.remote(fetch)
        self.denied(reader, 'transport_failed')
        for _ in range(10):
            self.denied(reader, 'transport_failed')
        self.assertEqual(fetch.call_count, 11)
        self.assertEqual(self.emit.call_count, 1)
        self.mono += 5
        self.denied(reader, 'transport_failed')
        self.assertEqual(self.emit.call_count, 2)

    def test_success_keeps_cache_and_emits_no_error(self):
        fetch = Mock(return_value=self.proof)
        reader = self.remote(fetch)
        reader.require_ready()
        reader.require_ready()
        fetch.assert_called_once()
        self.emit.assert_not_called()

    def test_diagnostic_failure_cannot_mask_primary_refusal(self):
        self.emit.side_effect = RuntimeError('journal unavailable')
        self.denied(self.remote(Mock(side_effect=OSError(SECRET))), 'transport_failed')

    def test_http_rejection_records_only_numeric_status(self):
        error = hot._AdmissionProofError('http_rejected', http_status=403)
        self.denied(self.remote(Mock(side_effect=error)), 'http_rejected')
        self.assertEqual(self.emit.call_args.kwargs['http_status'], 403)

    def test_local_database_gate_is_distinct_and_status_contains_no_secret(self):
        controller = hot.HotRuntimeController(Mock(), local_cleanup=Mock(), clear_caches=Mock(),
            clock=lambda: self.wall, monotonic=lambda: self.mono)
        controller._database_reason = 'database_unavailable'
        controller._last_tick = self.mono - 21
        controller._last_wall = self.wall - 21
        value = controller.status()
        self.assertEqual(value['reason'], 'database_unavailable')
        denials = [call for call in self.emit.call_args_list if call.args[3] == 'hot_runtime_admission']
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0].kwargs['phase'], 'local_database_unavailable')
        count = self.emit.call_count
        controller.status()
        self.assertEqual(self.emit.call_count, count)


if __name__ == '__main__':
    unittest.main()
