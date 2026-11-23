"""The /sources routes behind the real middleware stack. No network, no database.

Storage stays off: the only database use is the read-only ledger read of the daily cap, and the
only write is the quota attempt the transport journals. Telemetry, traces and logs never carry a
value, ID, token or parameter of the acquisition.
"""
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import requests  # noqa: E402

import provider_runtime  # noqa: E402
import telemetry  # noqa: E402
import telemetry_contract  # noqa: E402
import youtube_client  # noqa: E402
from db.quota.youtube_quota import YouTubeQuotaRepository  # noqa: E402
from hot_runtime import HotRuntimeUnavailable  # noqa: E402
from hot_runtime_routes import route_access  # noqa: E402
from provider_sources.daily_cap import CAPPED_BUCKET, PHASE  # noqa: E402
from provider_sources.families import policy  # noqa: E402
from provider_sources.results import (AcquisitionResult, Completeness, FieldValue, Row, ValueState,  # noqa: E402
                                      to_wire)
from provider_sources.service import AcquisitionService  # noqa: E402
from request_limits import RequestBodyLimitMiddleware  # noqa: E402
from self_hosted_auth import SelfHostedAuthMiddleware  # noqa: E402
from web.middleware import HttpReadScopeMiddleware, install_runtime_middleware  # noqa: E402
from web.provider_sources import create_provider_sources_router  # noqa: E402
from provider_sources_support import load  # noqa: E402

ORIGIN = 'http://127.0.0.1:8002'
FIXTURE = load('youtube.playlists.list', 'full_two_pages')
MARKERS = ('MARKER-', 'PLsyntheticPlaylist', 'UCAAAAAAAAAAAAAAAAAAAAA1', 'Series 1', 'i.ytimg.com', 'embed/videoseries')


class Owner:
    mode = 'local'

    def configured(self):
        return True

    def access_state(self, now):
        return {'mode': self.mode}

    def session(self, *args, **kwargs):
        return None


class TrapDatabase:
    """Read-only reads succeed; anything else is a storage write and fails the test."""

    def __init__(self, ledger_rows=()):
        self.reads, self.ledger_rows, self.fail = [], list(ledger_rows), False

    def read_rows(self, operation, statement, parameters=(), *, max_rows):
        self.reads.append((operation, tuple(parameters)))
        if self.fail:
            raise RuntimeError('database unavailable')
        return [dict(row) for row in self.ledger_rows][:max_rows]

    def __getattr__(self, name):
        raise AssertionError(f'storage use during a one-off acquisition: {name}')


class Recorder:
    def __init__(self):
        self.started, self.finished = [], []

    def start_attempt(self, **values):
        self.started.append(values)
        return f'00000000-0000-4000-8000-{len(self.started):012d}'

    def finish_attempt(self, attempt_id, **values):
        self.finished.append((attempt_id, values))
        return True


class CapturedEvents:
    def __init__(self):
        self.events = []

    def emit(self, kind, source, target, operation, **optional):
        event = telemetry_contract.build_event(kind, source, target, operation, **optional)
        self.events.append(telemetry_contract.encode_event(event).decode('utf-8'))
        return event


class Response(requests.Response):
    def __init__(self, body):
        super().__init__()
        self.status_code = 200
        self.headers['Date'] = 'Thu, 29 Oct 2026 10:00:00 GMT'
        self._raw = json.dumps(body).encode('utf-8')

    def iter_content(self, chunk_size):
        yield self._raw

    def close(self):
        pass


class SourcesHttpCase(unittest.TestCase):
    def setUp(self):
        self.owner, self.denied, self.calls = Owner(), False, []
        self.database = TrapDatabase()
        self.recorder = Recorder()
        self.capture = CapturedEvents()
        from telemetry_isolation import telemetry_switched_on
        telemetry_switched_on(self)
        self.pages = [Response(item['body']) for item in FIXTURE['responses']]
        self.environment = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target, value in (('hot_runtime.require_hot_runtime_ready', self.admission),
                              ('telemetry.default_emitter', lambda: self.capture)):
            item = patch(target, value)
            item.start()
            self.addCleanup(item.stop)
        for item in (patch.object(youtube_client.requests, 'get', self.http),
                     patch('provider_runtime.admit_request', side_effect=lambda **kw: contextlib.nullcontext(
                         provider_runtime._UnregulatedRequest(kw.get('cancel')))),
                     patch.object(youtube_client.time_trust, 'record_date')):
            item.start()
            self.addCleanup(item.stop)
        repository = YouTubeQuotaRepository(self.database, project_id='synthetic')
        self.service = AcquisitionService(attempt_recorder=self.recorder, ledger_reader=lambda day, quota_bucket=CAPPED_BUCKET: repository.list_phase_attempts(
            day, phase=PHASE, quota_bucket=quota_bucket))
        self.app = FastAPI()
        install_runtime_middleware(self.app, core=None)
        self.app.include_router(create_provider_sources_router(self.service))
        self.app.add_middleware(RequestBodyLimitMiddleware)
        self.app.add_middleware(SelfHostedAuthMiddleware, repository=self.owner, origin=ORIGIN)
        self.app.add_middleware(HttpReadScopeMiddleware)
        self.client = TestClient(self.app, base_url=ORIGIN, headers={'Origin': ORIGIN})
        self.addCleanup(self.client.close)
        self.logs = io.StringIO()
        handler = logging.StreamHandler(self.logs)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(previous)))

    def admission(self, **kwargs):
        if self.denied:
            raise HotRuntimeUnavailable()

    def http(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.pages.pop(0)

    def preview(self, request=None):
        return self.client.post('/sources/acquisitions/preview', json=request or FIXTURE['request'])

    def execute(self, *, request=None, fingerprint=None, cost=None, headers=None, continuation=None):
        request = request or FIXTURE['request']
        preview = self.preview(request).json()
        body = {'version': 1, 'request': request, 'plan_fingerprint': fingerprint or preview['plan_fingerprint'],
                'confirmed_cost': cost if cost is not None else preview['worst_cost']}
        if continuation:
            body['continuation'] = continuation
        return self.client.post('/sources/acquisitions/execute', json=body,
                                headers={'X-Saved-Work': '1'} if headers is None else headers)


class SourcesHttpTests(SourcesHttpCase):
    def test_routes_have_reviewed_admission(self):
        self.assertEqual({path: route_access(method, path) for method, path in (
            ('GET', '/sources/registry'), ('GET', '/sources/acquisitions/status'),
            ('POST', '/sources/acquisitions/preview'), ('POST', '/sources/acquisitions/execute'))},
            {'/sources/registry': 'user_work', '/sources/acquisitions/status': 'user_work',
             '/sources/acquisitions/preview': 'user_work', '/sources/acquisitions/execute': 'api_read'})

    def test_every_route_needs_the_owner_session(self):
        self.owner.mode = 'password'
        for method, path in (('get', '/sources/registry'), ('get', '/sources/acquisitions/status'),
                             ('post', '/sources/acquisitions/preview'), ('post', '/sources/acquisitions/execute')):
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path, **({'json': {}} if method == 'post' else {})).status_code, 401)
        self.assertEqual(self.calls, [])

    def test_registry_projection_is_safe(self):
        answer = self.client.get('/sources/registry')
        self.assertEqual(answer.status_code, 200)
        value = answer.json()
        self.assertEqual(len(value['methods']), 38)
        executable = {method['id'] for method in value['methods'] if method['executable']}
        self.assertEqual(executable, set(policy.EXECUTABLE))
        self.assertLessEqual({'youtube.channels.list', 'youtube.playlistItems.list', 'youtube.playlists.list',
                              'youtube.videos.batchGetStats', 'youtube.videos.list', 'youtube.search.list',
                              'youtube.commentThreads.list', 'youtube.liveChatMessages.list'}, executable)
        self.assertNotIn('youtube.youtube.v3.liveChat.messages.stream', executable)
        self.assertLessEqual(policy.OWNER_ONLY_FAMILIES, executable) # owner families
        detail = self.client.get('/sources/registry', params={'method': 'youtube.playlists.list'}).json()
        selectable = {field['path'] for field in detail['fields'] if field['selectable']}
        self.assertIn('player.embedHtml', selectable)
        self.assertNotIn('snippet.tags[]', selectable)
        for text in (answer.text, json.dumps(detail)):
            self.assertNotIn('.py::', text)
            self.assertNotIn('ops:', text)
            self.assertNotIn('application/', text)
        self.assertEqual(self.client.get('/sources/registry', params={'method': 'youtube.nothing'}).status_code, 404)
        self.assertEqual(self.client.get('/sources/registry', params={'other': '1'}).status_code, 422)

    def test_preview_spends_nothing_and_shows_cost_and_cap(self):
        answer = self.preview()
        self.assertEqual(answer.status_code, 200, answer.text)
        value = answer.json()
        self.assertEqual((value['worst_cost'], value['network_used'], value['executable']), ({'general': 3}, False, True))
        self.assertEqual((value['daily_cap']['used_units'], value['daily_cap']['remaining_units']), (0, 500))
        self.assertEqual((self.calls, self.recorder.started), ([], []))
        self.assertEqual(answer.headers['cache-control'], 'no-store')
        refused = self.preview({**FIXTURE['request'], 'parameters': {'key': 'AIza-not-real', 'channelId': 'UC' + 'k' * 22}})
        self.assertEqual((refused.status_code, refused.json()), (422, {'code': 'parameter_reserved', 'subject': 'key'}))

    def test_execute_needs_the_write_header_the_previewed_plan_and_the_confirmed_cost(self):
        self.assertEqual(self.execute(headers={}).json()['code'], 'write_guard')
        self.assertEqual(self.execute(fingerprint='0' * 64).json()['code'], 'plan_changed')
        self.assertEqual(self.execute(cost={'general': 2}).json()['code'], 'cost_not_confirmed')
        self.assertEqual((self.calls, self.recorder.started), ([], []))

    def test_execute_end_to_end_stores_nothing_and_logs_no_values(self):
        answer = self.execute()
        self.assertEqual(answer.status_code, 200, answer.text)
        value = answer.json()
        self.assertEqual((value['completeness'], value['cost'], len(value['rows'])), ('complete', {'general': 2}, 3))
        self.assertIn('MARKER-TITLE', answer.text)   # the owner receives the values in the response only
        # Storage trap: the daily-cap ledger read is the only database use.
        self.assertTrue(self.database.reads)
        self.assertEqual({operation for operation, _ in self.database.reads}, {'youtube_quota_phase_attempts'})
        # The quota journal receives labels and units, never IDs, tokens or parameters.
        self.assertEqual(self.recorder.started, [dict(phase='explicit_acquisition', endpoint='playlists',
                                                      quota_bucket='general', quota_units=1,
                                                      retry_of_request_attempt_id=None)] * 2)
        journal = json.dumps([self.recorder.started, self.recorder.finished])
        observed = '\n'.join(self.capture.events) + self.logs.getvalue() + journal
        self.assertTrue(self.capture.events)
        for marker in MARKERS + ('MARKER-TOKEN-PAGE-2',):
            self.assertNotIn(marker, observed, marker)
        self.assertIn('explicit_acquisition', '\n'.join(self.capture.events))

    def test_personal_data_of_comments_and_searches_reaches_only_the_answer(self):
        """Comment texts, authors, search queries and tokens never reach the journal, telemetry or logs."""
        for method, name in (('youtube.commentThreads.list', 'full_two_pages'), ('youtube.search.list', 'mixed_two_pages')):
            with self.subTest(method=method):
                fixture = load(method, name)
                self.pages = [Response(item['body']) for item in fixture['responses']]
                self.recorder.started.clear()
                self.capture.events.clear()
                answer = self.execute(request=fixture['request'])
                self.assertEqual((answer.status_code, answer.json()['completeness']), (200, 'complete'))
                self.assertIn('MARKER-', answer.text)        # the owner receives the values in the response only
                journal = json.dumps([self.recorder.started, self.recorder.finished])
                observed = '\n'.join(self.capture.events) + self.logs.getvalue() + journal
                for marker in ('MARKER-', 'synthetic query', 'UgxSyntheticThread', 'vidxxxxxxx1', 'UCBBBBBBBBBBBBBBBBBBBBB2'):
                    self.assertNotIn(marker, observed, marker)

    def test_search_has_its_own_daily_cap(self):
        def read_rows(operation, statement, parameters=(), *, max_rows):
            self.database.reads.append((operation, tuple(parameters)))
            if parameters[3] != 'search_queries':
                return []
            return [{'request_attempt_id': '00000000-0000-4000-8000-0000000000ab', 'quota_units': 97}]
        self.database.read_rows = read_rows
        search = {'version': 1, 'method': 'youtube.search.list', 'parameters': {'q': 'synthetic'},
                  'selection': {'parts': ['snippet']}}
        preview = self.preview(search).json()
        self.assertEqual((preview['worst_cost'], preview['daily_caps']['search_queries']['remaining_units'],
                          preview['daily_caps']['search_queries']['limit_units']), ({'search_queries': 5}, 3, 100))
        refused = self.execute(request=search)
        self.assertEqual((refused.status_code, refused.json()), (429, {'code': 'daily_cap_reached', 'subject': 'search_queries'}))
        status = self.client.get('/sources/acquisitions/status').json()
        self.assertEqual((status['daily_cap']['used_units'], status['daily_caps']['search_queries']['used_units']), (0, 97))
        self.assertEqual(status['limits']['daily_cap_units'], {'general': 500, 'search_queries': 100})
        self.assertEqual(self.calls, [])

    def test_one_acquisition_at_a_time_and_the_daily_cap(self):
        with self.service._busy:
            self.assertEqual(self.execute().json()['code'], 'acquisition_busy')
        self.database.ledger_rows = [{'request_attempt_id': '00000000-0000-4000-8000-0000000000aa', 'quota_units': 498}]
        refused = self.execute()
        self.assertEqual((refused.status_code, refused.json()['code']), (429, 'daily_cap_reached'))
        self.assertTrue(1 <= int(refused.headers['retry-after']) <= 25 * 3600)
        status = self.client.get('/sources/acquisitions/status').json()
        self.assertEqual((status['daily_cap']['used_units'], status['daily_cap']['remaining_units'], status['active']),
                         (498, 2, False))   # the plan's worst case (3) does not fit
        self.database.fail = True
        refused = self.execute()
        self.assertEqual((refused.status_code, refused.json()['code']), (503, 'daily_cap_unavailable'))
        self.assertEqual(self.client.get('/sources/acquisitions/status').json()['daily_cap']['read_status'], 'unavailable')
        self.assertEqual(self.calls, [])

    def test_owner_context_without_sign_in_and_hot_pause_send_nothing(self):
        owner_request = {**FIXTURE['request'], 'parameters': {'mine': True}, 'context': 'owner'}
        answer = self.client.post('/sources/acquisitions/preview', json=owner_request)
        self.assertEqual((answer.status_code, answer.json()['code']), (409, 'owner_auth_unavailable'))
        preview = self.preview().json()
        self.denied = True
        answer = self.client.post('/sources/acquisitions/execute', headers={'X-Saved-Work': '1'}, json={
            'version': 1, 'request': FIXTURE['request'], 'plan_fingerprint': preview['plan_fingerprint'],
            'confirmed_cost': preview['worst_cost']})
        self.assertEqual(answer.status_code, 503)
        self.assertEqual(self.calls, [])

    def test_answer_is_bounded_to_two_mebibytes(self):
        row = Row('playlist', ('playlist', 'PLx'), {'snippet.description': FieldValue(ValueState.KNOWN, 'x' * 1000)}, 0)
        result = AcquisitionResult('youtube.playlists.list', 'playlist', (row,) * 5000, {}, (), Completeness.COMPLETE,
                                   None, 'provider', {'general': 10})
        value = to_wire(result, max_bytes=2 * 1024 * 1024)
        encoded = json.dumps(value, separators=(',', ':')).encode()
        self.assertLessEqual(len(encoded), 2 * 1024 * 1024)
        self.assertEqual((value['completeness'], value['stop_reason']), ('partial', 'output_budget'))
        self.assertTrue(value['notes'][-1].startswith('rows_trimmed:'))


class SourcesFaultHttpTests(SourcesHttpCase):
    """Faults at the HTTP edge (disconnect, revoked owner, oversize body) and secrets on every failure path."""

    def body(self, request=None):
        request = request or FIXTURE['request']
        preview = self.preview(request).json()
        return {'version': 1, 'request': request, 'plan_fingerprint': preview['plan_fingerprint'],
                'confirmed_cost': preview['worst_cost']}

    def test_a_dropped_connection_cancels_the_running_request(self):
        import asyncio
        import threading
        import time
        started, reads, outcomes = threading.Event(), [], []

        class Slow(Response):
            def iter_content(self, chunk_size):
                started.set()
                for _ in range(200):
                    time.sleep(0.01)
                    reads.append(1)
                    yield b' '
                yield self._raw

        self.pages = [Slow(item['body']) for item in FIXTURE['responses']]
        execute = self.service.execute

        def observed(raw, *, cancelled):
            outcomes.append(execute(raw, cancelled=cancelled))
            return outcomes[-1]
        self.service.execute = observed
        raw = json.dumps(self.body()).encode()
        messages, sent = [{'type': 'http.request', 'body': raw, 'more_body': False}], []

        async def receive():
            # Like uvicorn: after the body, receive() waits until the client is gone, then answers at once.
            if messages:
                return messages.pop(0)
            while not started.is_set():
                await asyncio.sleep(0.01)
            return {'type': 'http.disconnect'}

        async def send(message):
            sent.append(message)
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
                 'path': '/sources/acquisitions/execute', 'raw_path': b'/sources/acquisitions/execute', 'query_string': b'',
                 'root_path': '', 'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 8002),
                 'headers': [(b'host', b'127.0.0.1:8002'), (b'origin', ORIGIN.encode()), (b'x-saved-work', b'1'),
                             (b'content-type', b'application/json'), (b'content-length', str(len(raw)).encode())]}
        asyncio.run(self.app(scope, receive, send))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual((outcomes[0]['completeness'], outcomes[0]['stop_reason'], outcomes[0]['requests_sent'],
                          outcomes[0]['rows']), ('partial', 'cancelled', 1, []))
        self.assertEqual(len(self.calls), 1)            # the second page is never asked for
        self.assertLess(len(reads), 200)                # the body read stopped early
        self.assertEqual(self.recorder.finished[0][1]['status'], 'transport_error')

    def test_owner_access_revoked_during_the_acquisition_hands_out_no_values(self):
        def http(url, **kwargs):
            self.calls.append((url, kwargs))
            self.owner.mode = 'password'               # the owner signs out while the request runs
            return self.pages.pop(0)
        body = self.body()
        with patch.object(youtube_client.requests, 'get', http):
            answer = self.client.post('/sources/acquisitions/execute', json=body, headers={'X-Saved-Work': '1'})
        self.assertEqual((answer.status_code, answer.json()), (403, {'code': 'policy_denied'}))
        self.assertNotIn('MARKER-', answer.text)
        self.assertEqual(len(self.recorder.started), 2)   # the spent quota stays journaled

    def http(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.pages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def test_oversized_or_deeply_nested_documents_are_refused_unsent(self):
        big = self.client.post('/sources/acquisitions/preview', content=b'{"x":"' + b'a' * (64 * 1024) + b'"}',
                               headers={'Content-Type': 'application/json'})
        self.assertEqual((big.status_code, big.json()), (413, {'code': 'request_too_large'}))
        deep = self.client.post('/sources/acquisitions/preview', content=b'[' * 30000 + b']' * 30000,
                                headers={'Content-Type': 'application/json'})
        self.assertEqual((deep.status_code, deep.json()), (422, {'code': 'request_invalid'}))
        self.assertEqual((self.calls, self.recorder.started), ([], []))

    def test_no_key_token_or_provider_text_on_any_failure_path(self):
        import tempfile
        from provider_credentials import OwnerBearer
        from provider_sources.credentials import OwnerBinding
        from self_hosted_configuration import ConfigurationStore
        key, token, secret = 'AIza' + '0' * 35, 'synthetic-owner-token-' + 'w' * 30, 'PROVIDER-TEXT-7'
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {'LIC_SETUP_DIRECTORY': directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        store = ConfigurationStore.initialize(directory.name)
        store.update(store.read()['revision'], lambda v: v.update(
            api_key=key, prepared=True, phase='ready', workspace={'phase': 'complete', 'channels': {}}))

        class Raw(Response):
            def __init__(self, status, raw, headers=None):
                super().__init__({})
                self.status_code, self._raw = status, raw
                self.headers.update(headers or {})
        echo = json.dumps({'error': {'code': 500, 'message': f'{secret} key={key}', 'errors': [{'reason': 'backendError'}]}})
        failures = (
            requests.ConnectionError(f'HTTPSConnectionPool url: /youtube/v3/playlists?pageToken={secret} {key}'),
            requests.ReadTimeout(f'{secret} {key}'),
            Raw(500, echo.encode(), {'Retry-After': '3'}),
            Raw(429, echo.replace('backendError', 'rateLimitExceeded').encode()),
            Raw(200, ('{"kind":"youtube#playlistListResponse","items":[{"id":"' + secret + key).encode()),
            Raw(200, json.dumps({'kind': 'youtube#playlistListResponse', 'items': [], 'zzUnknown': secret + key}).encode()),
            Raw(200, b'x' * (3 * 1024 * 1024)),
            Raw(403, json.dumps({'error': {'code': 403, 'message': key, 'errors': [{'reason': 'accessNotConfigured'}]}}).encode()),
        )
        answers = []
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                self.pages = [failure]
                answer = self.execute()
                self.assertEqual(answer.status_code, 200, answer.text)
                answers.append(answer.text)
                self.assertEqual(self.calls[-1][1]['headers'], {'X-Goog-Api-Key': key})
                self.assertNotIn(key, self.calls[-1][0])     # never in the URL
        self.assertEqual([json.loads(text)['stop_reason'] for text in answers],
                         ['transient', 'transient', 'transient', 'rate_limited', 'response_invalid', None,
                          'response_too_large', 'credential_access_denied'])

        class Provider:
            def binding(self):
                return OwnerBinding('binding-9', 'UC' + 'b' * 22, frozenset({'https://www.googleapis.com/auth/youtube.readonly'}),
                                    'synthetic-project')

            def access_token(self, *, scopes, deadline):
                return OwnerBearer(token)
        self.service.owner_provider = Provider()
        self.pages = [Raw(401, json.dumps({'error': {'code': 401, 'message': token}}).encode())]
        owner = self.execute(request={**FIXTURE['request'], 'parameters': {'mine': True}, 'context': 'owner'})
        answers.append(owner.text)
        self.assertEqual((owner.status_code, owner.json()['stop_reason']), (200, 'owner_auth_expired'))
        self.assertEqual(self.calls[-1][1]['headers'], {'Authorization': 'Bearer ' + token})
        journal = json.dumps([self.recorder.started, self.recorder.finished])
        observed = '\n'.join(self.capture.events) + self.logs.getvalue() + journal + '\n'.join(answers)
        for marker in (key, token, secret, 'key='):
            self.assertNotIn(marker, observed, marker)
        self.assertEqual(ConfigurationStore(directory.name).read()['error'], 'provider_access_denied')  # only the key's own 403


if __name__ == '__main__':
    unittest.main()
