"""Source-layer transport failures, credential separation, and telemetry. No network or provider."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import requests  # noqa: E402
import provider_runtime  # noqa: E402
import telemetry_contract  # noqa: E402
import youtube_client  # noqa: E402
from operational_copy_policy import QUOTA_LABELS, quota_label  # noqa: E402
from provider_admission import AdmissionUnavailable  # noqa: E402
from provider_boundary import provider_forbidden, stored_data_only  # noqa: E402
from provider_credentials import OwnerBearer  # noqa: E402
from self_hosted_configuration import ConfigurationStore  # noqa: E402
from youtube_errors import OUTCOME_BUCKETS, classify_youtube_outcome  # noqa: E402
from youtube_source_fields import youtube_counter_integer  # noqa: E402

KEY = 'synthetic_installation_key_no_requests'
TOKEN = 'synthetic-owner-token-' + 'x' * 24
REAL_ADMIT = provider_runtime.admit_request  # the cases below patch the module attribute
NEW_BUCKETS = ('search_queries', 'youtube_analytics', 'youtube_reporting')


class Response:
    def __init__(self, status, payload, headers=None):
        self.status_code, self.payload = status, payload
        self.headers = dict(headers or {})

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        return iter((json.dumps(self.payload).encode(),))

    def close(self):
        pass


class Recorder:
    def __init__(self):
        self.started, self.finished = [], []

    def start_attempt(self, **values):
        self.started.append(values)
        return f'attempt-{len(self.started)}'

    def finish_attempt(self, attempt_id, **values):
        self.finished.append((attempt_id, values))
        return True


def refusal(status, reason):
    return {'error': {'code': status, 'errors': [{'reason': reason}]}}


class TransportCase(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.answer = Response(200, {'kind': 'youtube#playlistListResponse', 'items': []})
        self.recorder = Recorder()
        hot = types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None)
        for item in (patch.dict(sys.modules, {'hot_runtime': hot}),
                     patch('provider_runtime.admit_request',
                           side_effect=lambda **kw: contextlib.nullcontext(kw.get('cancel'))),
                     patch.object(youtube_client.requests, 'get', self.http)):
            item.start()
            self.addCleanup(item.stop)

    def http(self, url, **kwargs):
        self.sent.append((url, kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def get(self, endpoint='playlists', bucket='general', **kwargs):
        options = dict(phase='explicit_acquisition', quota_bucket=bucket, attempt_recorder=self.recorder,
                       params={'part': 'id', 'channelId': 'UC' + 'a' * 22})
        options.update(kwargs)
        return youtube_client.youtube_api_get(endpoint, **options)


class G3NewBucketTests(TransportCase):
    def test_every_new_bucket_classifies_daily_rate_and_missing(self):
        self.assertLessEqual(set(NEW_BUCKETS), OUTCOME_BUCKETS)
        for bucket in OUTCOME_BUCKETS:
            with self.subTest(bucket=bucket):
                daily = classify_youtube_outcome(403, refusal(403, 'quotaExceeded'), quota_bucket=bucket)
                self.assertEqual((daily.kind, daily.quota_bucket), ('daily_quota', bucket))
                self.assertEqual(classify_youtube_outcome(429, {}, quota_bucket=bucket).kind, 'rate_limit')
                self.assertEqual(classify_youtube_outcome(404, {}, quota_bucket=bucket).kind, 'resource_missing')

    def test_refusal_in_a_new_bucket_is_returned_not_a_value_error(self):
        # A refusal must retain its status and quota bucket.
        self.answer = Response(403, refusal(403, 'quotaExceeded'))
        response = self.get('search', bucket='search_queries', params={'part': 'snippet', 'q': 'x'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.recorder.started[0]['quota_bucket'], 'search_queries')
        self.assertEqual(quota_label('quota_bucket', 'search_queries'), 'search_queries')

    def test_unknown_bucket_is_refused_before_admission_attempt_or_http(self):
        with patch('provider_runtime.admit_request', side_effect=AssertionError('admitted')):
            with self.assertRaisesRegex(ValueError, 'quota_bucket is not reviewed'):
                self.get(bucket='raw_bucket')
        self.assertEqual((self.sent, self.recorder.started), ([], []))

    def test_unclassifiable_status_still_returns_the_response(self):
        self.answer = Response(999, {})
        observed = []
        gate = types.SimpleNamespace(is_set=lambda: False, observe=lambda **value: observed.append(value))
        with patch('provider_runtime.admit_request', side_effect=lambda **kw: contextlib.nullcontext(gate)):
            self.assertEqual(self.get().status_code, 999)
        self.assertEqual(observed, [{'success': False, 'rate_seconds': None}])


class G7OwnerCredentialTests(TransportCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted',
                                              'LIC_SETUP_DIRECTORY': directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = ConfigurationStore.initialize(directory.name)
        self.store.update(self.store.read()['revision'], lambda v: v.update(
            api_key=KEY, prepared=True, phase='ready', workspace={'phase': 'complete', 'channels': {}}))

    def stored_error(self):
        return ConfigurationStore(os.environ['LIC_SETUP_DIRECTORY']).read()['error']

    def test_owner_401_is_returned_and_never_marks_the_installation_key(self):
        self.answer = Response(401, {'error': {'code': 401, 'status': 'UNAUTHENTICATED'}})
        with patch('self_hosted_configuration.provider_key', side_effect=AssertionError('key read')):
            response = self.get(credential=OwnerBearer(TOKEN), params={'part': 'id', 'mine': 'true'})
        self.assertEqual(response.status_code, 401)
        headers = self.sent[0][1]['headers']
        self.assertEqual(headers['Authorization'], 'Bearer ' + TOKEN)
        self.assertNotIn('X-Goog-Api-Key', headers)
        self.assertIsNone(self.stored_error())
        # The installation key keeps working for the next request.
        self.answer = Response(200, {'items': []})
        self.get()
        self.assertEqual(self.sent[-1][1]['headers'], {'X-Goog-Api-Key': KEY})

    def test_installation_key_401_still_marks_the_key(self):
        self.answer = Response(401, {'error': {'code': 401}})
        self.assertEqual(self.get().status_code, 401)
        self.assertEqual(self.sent[0][1]['headers'], {'X-Goog-Api-Key': KEY})
        self.assertEqual(self.stored_error(), 'provider_key_rejected')

    def test_owner_request_is_independent_of_a_refused_installation_key(self):
        self.store.update(self.store.read()['revision'], lambda v: v.update(error='provider_key_rejected'))
        self.get(credential=OwnerBearer(TOKEN))
        self.assertEqual(self.sent[0][1]['headers'], {'Authorization': 'Bearer ' + TOKEN})

    def test_owner_token_is_never_combined_or_disguised(self):
        for options in ({'credential': OwnerBearer(TOKEN), 'api_key': KEY}, {'credential': 'Bearer ' + TOKEN},
                        {'credential': object()}):
            with self.subTest(options=tuple(options)), self.assertRaisesRegex(ValueError, 'credential_invalid'):
                self.get(**options)
        self.assertEqual(self.sent, [])
        for value in ('', 'short', TOKEN + ' extra', TOKEN + '\r\nX: 1', 'x' * 4097, None, 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OwnerBearer(value)

    def test_token_value_never_leaves_through_repr_pickle_or_errors(self):
        bearer = OwnerBearer(TOKEN)
        self.assertNotIn(TOKEN, repr(bearer) + str(bearer) + repr([bearer]))
        with self.assertRaises(TypeError):
            __import__('pickle').dumps(bearer)
        self.answer = requests.ConnectionError('Max retries exceeded with url: /youtube/v3/playlists?part=id')
        with self.assertRaises(requests.ConnectionError) as raised:
            self.get(credential=bearer)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertEqual(self.recorder.finished[0][1]['status'], 'transport_error')


class G8LabelTests(TransportCase):
    def test_unknown_endpoint_label_is_refused_before_anything_is_counted(self):
        with self.assertRaisesRegex(ValueError, 'endpoint label is not reviewed'):
            self.get('newMethod')
        with self.assertRaisesRegex(ValueError, 'endpoint label is not reviewed'):
            self.get('liveChat/messages', endpoint_label='liveChat/messages')
        self.assertEqual((self.sent, self.recorder.started), ([], []))

    def test_label_differs_from_path_for_nested_resources(self):
        self.get('liveChat/messages', endpoint_label='liveChatMessages', params={'liveChatId': 'x', 'part': 'id'})
        self.assertTrue(self.sent[0][0].endswith('/youtube/v3/liveChat/messages'))
        self.assertEqual(self.recorder.started[0]['endpoint'], 'liveChatMessages')

    def test_trace_keeps_an_event_with_an_unreviewed_label(self):
        event = telemetry_contract.build_event('pipeline', 'collector', 'collector', 'provider_request',
                                               status='started', endpoint='brandNewMethod', quota_bucket='brand_new')
        self.assertEqual((event['endpoint'], event['quota_bucket']), ('other', 'other'))
        for label in QUOTA_LABELS['endpoint']:
            with self.subTest(label=label):
                built = telemetry_contract.build_event('pipeline', 'collector', 'collector', 'x', endpoint=label)
                self.assertEqual(built['endpoint'], label)
        for bucket in NEW_BUCKETS:
            self.assertEqual(telemetry_contract.build_event('pipeline', 'c', 'c', 'x', quota_bucket=bucket)['quota_bucket'],
                             bucket)

    def test_new_phase_labels_are_kept(self):
        self.assertEqual(quota_label('phase', 'explicit_acquisition'), 'explicit_acquisition')


class C5AttemptIdentityTests(TransportCase):
    def test_response_and_late_failure_carry_the_attempt(self):
        response = self.get()
        self.assertEqual(response.lic_request_attempt_id, 'attempt-1')
        self.answer = requests.ReadTimeout('synthetic')
        with self.assertRaises(requests.ReadTimeout) as raised:
            self.get()
        self.assertEqual((raised.exception.lic_request_attempt_id, raised.exception.lic_http_attempted),
                         ('attempt-2', True))

    def test_refusal_before_http_carries_no_attempt(self):
        with patch.dict(sys.modules, {'hot_runtime': types.SimpleNamespace(
                require_hot_runtime_ready=lambda **kw: (_ for _ in ()).throw(RuntimeError('paused')))}):
            with self.assertRaises(RuntimeError) as raised:
                self.get()
        self.assertFalse(getattr(raised.exception, 'lic_http_attempted', False))
        self.assertEqual(self.recorder.started, [])


class ModeARuntimeTests(TransportCase):
    def test_forbidden_scope_refuses_transport_and_gate(self):
        with provider_forbidden():
            with self.assertRaisesRegex(AdmissionUnavailable, 'provider_forbidden'):
                self.get()
        self.assertEqual((self.sent, self.recorder.started), ([], []))
        with patch.object(provider_runtime, '_runtime', None), provider_forbidden():
            with self.assertRaisesRegex(AdmissionUnavailable, 'provider_forbidden'):
                with REAL_ADMIT():
                    pass
        self.get()  # outside the scope the same request proceeds
        self.assertEqual(len(self.sent), 1)

    def test_query_service_entry_points_are_stored_data_only(self):
        from research_query.service import QueryService
        seen = []

        def probe(*args, **kwargs):
            try:
                self.get()
            except AdmissionUnavailable as error:
                seen.append(str(error))
            raise LookupError('stop')
        service = QueryService(types.SimpleNamespace(), lambda: None)
        for name in ('execute', 'execute_graph', 'execute_operation'):
            with self.subTest(name=name), patch('research_query.service.validate', probe), \
                    patch('research_query.graph_service.execute_graph', probe), \
                    patch('research_query.aggregate_service.execute_operation', probe):
                with self.assertRaises(LookupError):
                    getattr(service, name)(b'{}')
        self.assertEqual(seen, ['provider_forbidden'] * 3)
        self.assertEqual(self.sent, [])

    def test_select_all_matching_and_frozen_reads_are_stored_data_only(self):
        # Select all matching (frozen_ids) and the dependent frozen
        # reads run in the same scope as a page; the probe would otherwise send.
        from research_query.service import QueryService
        seen = []

        def probe(*args, **kwargs):
            try:
                self.get()
            except AdmissionUnavailable as error:
                seen.append(str(error))
            raise LookupError('stop')
        service = QueryService(types.SimpleNamespace(), lambda: None)
        calls = (('frozen_ids', lambda: service.frozen_ids(b'{}'), probe),
                 ('frozen_reference', lambda: service.frozen_reference('token'), probe),
                 # member() runs after frozen_reference() has returned
                 ('frozen_member', lambda: service.frozen_member('token', 'video'),
                  lambda *args, **kwargs: types.SimpleNamespace(member=probe)))
        for name, call, reference in calls:
            with self.subTest(name=name), patch('research_query.frozen_service.frozen_ids', probe), \
                    patch('research_query.frozen_service.frozen_reference', reference):
                with self.assertRaises(LookupError):
                    call()
        self.assertEqual(seen, ['provider_forbidden'] * 3)
        self.assertEqual(self.sent, [])

    def test_decorator_keeps_identity_and_threads_inherit_the_scope(self):
        import contextvars
        import threading
        self.assertEqual(stored_data_only(len).__name__, 'len')
        result = []

        def worker():
            try:
                self.get()
            except AdmissionUnavailable as error:
                result.append(str(error))
        with provider_forbidden():
            context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(worker,))
        thread.start()
        thread.join()
        self.assertEqual(result, ['provider_forbidden'])


class C7CounterBoundTests(unittest.TestCase):
    def test_default_bound_is_unchanged_and_uint64_is_opt_in(self):
        self.assertEqual(youtube_counter_integer(str(2**63 - 1)), 2**63 - 1)
        self.assertIsNone(youtube_counter_integer(str(2**63)))
        self.assertEqual(youtube_counter_integer(str(2**64 - 1), maximum=2**64 - 1), 2**64 - 1)
        self.assertIsNone(youtube_counter_integer(str(2**64), maximum=2**64 - 1))
        for bad in (-1, True, 1.0, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                youtube_counter_integer('1', maximum=bad)


if __name__ == '__main__':
    unittest.main()
