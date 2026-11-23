"""Shared permit and bounded real Response parsing; no network or database."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
import requests
import hot_runtime
import provider_runtime
import youtube_client as client
from provider_admission import AdmissionCancelled, AdmissionUnavailable


class Gate:
    def __init__(self):
        self.active = 0
        self.on_enter = lambda: None
        self.entries = []
        self.outcomes = []
        self.on_outcome = self.outcomes.append

    @contextmanager
    def acquire(self, kind, *, timeout, cancel):
        self.entries.append((kind, timeout))
        self.on_enter()
        if cancel.is_set():
            raise AdmissionCancelled('cancelled')
        self.active += 1
        try:
            yield SimpleNamespace(observe=lambda **value: self.on_outcome(value))
        finally:
            self.active -= 1


class Response(requests.Response):
    def __init__(self, gate, chunks=(b'{"items":[]}',), *, status=200, declared=None):
        super().__init__()
        self.status_code = status
        self.gate, self.chunks = gate, chunks
        self.reads = self.closes = 0
        if declared is not None:
            self.headers['Content-Length'] = declared

    def iter_content(self, chunk_size):
        assert self.gate.active == 1
        assert chunk_size == 65536
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            if callable(chunk):
                chunk = chunk()
            yield chunk

    def close(self):
        assert self.gate.active == 1
        self.closes += 1


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.gate = Gate()
        self.runtime = SimpleNamespace(pid=os.getpid(), stop=threading.Event(), gate=self.gate)
        self.clock = [100.0]
        self.recorder = Mock()
        self.recorder.start_attempt.return_value = 'attempt'
        self.ready = Mock(side_effect=lambda **kw: self.assertEqual(self.gate.active, 1))
        self.response = Response(self.gate)
        self.http = Mock(side_effect=lambda *a, **kw: self.response)
        for context in (
            patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_SETUP_DIRECTORY': ''}),
            patch.object(provider_runtime, '_runtime', self.runtime),
            patch.object(hot_runtime, 'require_hot_runtime_ready', self.ready),
            patch.object(client.time, 'monotonic', side_effect=lambda: self.clock[0]),
            patch.object(client.time_trust, 'request_started', return_value=100),
            patch.object(client.time_trust, 'record_date'),
            patch.object(client.requests, 'get', self.http),
        ):
            context.start()
            self.addCleanup(context.stop)

    def request(self, **kwargs):
        return client.youtube_api_get('channels', phase='add_channel', quota_bucket='general',
            attempt_recorder=self.recorder, params={'id': 'synthetic'}, api_key='synthetic-key', **kwargs)

    def assert_released(self, *, attempts=1):
        self.assertEqual(self.gate.active, 0)
        self.assertEqual(self.recorder.start_attempt.call_count, attempts)
        self.assertEqual(self.recorder.finish_attempt.call_count, attempts)

    def test_permit_covers_stream_close_and_cached_json(self):
        with provider_runtime.request_scope('auto'):
            result = self.request()
        self.assertEqual(result.json(), {'items': []})
        self.assertEqual(self.response.closes, 1)
        self.assertEqual(self.gate.entries, [('auto', 30)])
        self.assertTrue(self.http.call_args.kwargs['stream'])
        self.assertFalse(self.http.call_args.kwargs['allow_redirects'])
        self.assert_released()

    def test_hot_denial_after_wait_sends_nothing(self):
        self.gate.on_enter = lambda: setattr(self.ready, 'side_effect', RuntimeError('hot_unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'hot_unavailable'):
            self.request()
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_deadline_after_wait_sends_nothing(self):
        self.gate.on_enter = lambda: self.clock.__setitem__(0, 106)
        with self.assertRaises(requests.Timeout):
            self.request(deadline=105)
        self.assertEqual(self.gate.entries, [('interactive', 5)])
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_timeout_shrinks_after_admission(self):
        self.gate.on_enter = lambda: self.clock.__setitem__(0, 103)
        self.request(deadline=105)
        self.assertEqual(self.http.call_args.kwargs['timeout'], 2)
        self.assert_released()

    def test_unit_deadline_bounds_calls_without_an_explicit_deadline(self):
        self.gate.on_enter = lambda: self.clock.__setitem__(0, 103)
        with provider_runtime.request_scope('operator', deadline=105):
            self.request()
        self.assertEqual(self.gate.entries, [('operator', 5)])
        self.assertEqual(self.http.call_args.kwargs['timeout'], 2)
        self.assert_released()

    def test_request_cannot_extend_its_unit_deadline(self):
        with provider_runtime.request_scope('auto', deadline=105):
            self.request(deadline=150)
        self.assertEqual(self.http.call_args.kwargs['timeout'], 5)
        self.assert_released()

    def test_expired_unit_does_not_wait_or_start_http(self):
        with provider_runtime.request_scope('auto', deadline=99):
            with self.assertRaises(requests.Timeout):
                self.request()
        self.assertFalse(self.gate.entries)
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_per_call_cancel_during_wait_is_not_an_attempt(self):
        event = threading.Event()
        self.gate.on_enter = event.set
        with self.assertRaises(AdmissionCancelled):
            self.request(cancelled=event)
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_scoped_cancel_during_wait_is_not_an_attempt(self):
        event = threading.Event()
        self.gate.on_enter = event.set
        with provider_runtime.request_scope('operator', event), self.assertRaises(AdmissionCancelled):
            self.request()
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_declared_oversize_refuses_before_read(self):
        self.response = Response(self.gate, declared='11')
        with self.assertRaises(client.YouTubeResponseTooLarge):
            self.request(max_response_bytes=10)
        self.assertEqual((self.response.reads, self.response.closes), (0, 1))
        self.assert_released()

    def test_decoded_chunk_oversize_closes_without_reading_tail(self):
        self.response = Response(self.gate, (b'a' * 8, b'b' * 3, b'tail'), declared='1')
        with self.assertRaises(client.YouTubeResponseTooLarge):
            self.request(max_response_bytes=10)
        self.assertEqual((self.response.reads, self.response.closes), (2, 1))
        self.assert_released()

    def test_stream_failure_is_one_attempt_and_releases(self):
        self.response = Response(self.gate, (b'{', requests.ConnectionError('synthetic')))
        with self.assertRaises(requests.ConnectionError):
            self.request()
        self.assertEqual(self.response.closes, 1)
        self.assert_released()

    def test_deadline_during_stream_closes_and_releases(self):
        def late_chunk():
            self.clock[0] = 121
            return b'{}'
        self.response = Response(self.gate, (late_chunk,))
        with self.assertRaises(requests.Timeout):
            self.request()
        self.assertEqual(self.response.closes, 1)
        self.assert_released()

    def test_cancel_during_stream_closes_and_releases(self):
        event = threading.Event()
        def cancelled_chunk():
            event.set()
            return b'{}'
        self.response = Response(self.gate, (cancelled_chunk,))
        with self.assertRaises(AdmissionCancelled):
            self.request(cancelled=event)
        self.assertEqual(self.response.closes, 1)
        self.assert_released()

    def test_scope_and_backend_stop_are_checked_during_body_read(self):
        for event, scoped in ((threading.Event(), True), (self.runtime.stop, False)):
            self.recorder.reset_mock()
            def cancelled_chunk():
                event.set()
                return b'{}'
            self.response = Response(self.gate, (cancelled_chunk,))
            with provider_runtime.request_scope('operator', event if scoped else None):
                with self.assertRaises(AdmissionCancelled):
                    self.request()
            self.assertEqual(self.response.closes, 1)
            self.assert_released()

    def test_clock_refusal_still_closes_response(self):
        with patch.object(client.time_trust, 'record_date', side_effect=RuntimeError('clock')):
            with self.assertRaisesRegex(RuntimeError, 'clock'):
                self.request()
        self.assertEqual(self.response.closes, 1)
        self.assert_released()

    def test_redirect_closes_without_following(self):
        self.response = Response(self.gate, (), status=302)
        self.response.headers['Location'] = 'https://untrusted.example/path'
        with self.assertRaises(requests.HTTPError):
            self.request()
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.response.closes, 1)
        self.assert_released()

    def test_missing_gate_has_no_private_fallback(self):
        with patch.object(provider_runtime, '_runtime', None):
            with self.assertRaisesRegex(AdmissionUnavailable, 'not_initialized'):
                self.request()
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_other_profile_keeps_event_cancellation_without_gate_feedback(self):
        event = threading.Event()
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"items":[]}'
        response._content_consumed = True
        with patch.object(provider_runtime, '_runtime', None), \
                patch('deployment_profile.selected_deployment_profile', return_value='operator'), \
                patch.object(client, '_send_request', return_value=response) as send:
            result = self.request(cancelled=event)
            self.assertEqual(result.json(), {'items': []})
            cancellation = send.call_args.kwargs['cancelled']
            self.assertFalse(cancellation.is_set())
            event.set()
            self.assertTrue(cancellation.is_set())
            with self.assertRaises(AdmissionCancelled):
                self.request(cancelled=event)
            self.assertEqual(send.call_count, 1)
        self.assertFalse(self.gate.entries)
        self.assertFalse(self.gate.outcomes)

    def test_invalid_limits_refused_before_gate(self):
        for kwargs in ({'max_response_bytes': True}, {'max_response_bytes': 0},
                       {'max_response_bytes': client.MAX_RESPONSE_BYTES+1},
                       {'deadline': float('nan')}, {'timeout_seconds': False},
                       {'timeout_seconds': 0}, {'cancelled': object()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.request(**kwargs)
        self.assertFalse(self.gate.entries)
        self.http.assert_not_called()
        self.assert_released(attempts=0)

    def test_rate_response_notifies_shared_gate_without_retry(self):
        payload = {'error': {'errors': [{'reason': 'rateLimitExceeded'}]}}
        self.response = Response(self.gate, (json.dumps(payload).encode(),), status=403)
        self.response.headers['Retry-After'] = '120'
        response = self.request()
        self.assertEqual(response.json(), payload)
        self.assertEqual(self.gate.outcomes, [{'success': False, 'rate_seconds': 60}])
        self.assertEqual(self.http.call_count, 1)
        self.assert_released()

    def test_explicit_daily_reason_does_not_reduce_shared_rate_limit(self):
        payload = {'error': {'errors': [{'reason': 'quotaExceeded'}]}}
        self.response = Response(self.gate, (json.dumps(payload).encode(),), status=429)
        self.request()
        self.assertEqual(self.gate.outcomes, [{'success': False, 'rate_seconds': None}])
        self.assertEqual(self.http.call_count, 1)
        self.assert_released()

    def test_malformed_429_still_starts_rate_cooldown(self):
        self.response = Response(self.gate, (b'not-json',), status=429)
        self.request()
        self.assertEqual(self.gate.outcomes, [{'success': False, 'rate_seconds': 1}])
        self.assertEqual(self.http.call_count, 1)
        self.assert_released()

    def test_success_notifies_once_after_body_closes(self):
        def observe(value):
            self.assertEqual(self.response.closes, 1)
            self.assertEqual(self.gate.active, 1)
            self.gate.outcomes.append(value)
        self.gate.on_outcome = observe
        self.request()
        self.assertEqual(self.gate.outcomes, [{'success': True, 'rate_seconds': None}])
        self.assert_released()

    def test_uncertain_gate_feedback_never_repeats_http(self):
        self.gate.on_outcome = Mock(side_effect=AdmissionUnavailable('state unknown'))
        with self.assertRaises(AdmissionUnavailable):
            self.request()
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.gate.on_outcome.call_count, 1)
        self.assert_released()


if __name__ == '__main__':
    unittest.main()
