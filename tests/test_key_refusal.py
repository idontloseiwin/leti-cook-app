"""A key YouTube refuses is said plainly and stops requests until set-key (tester finding T-26). No provider."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'application'))
import manage  # noqa: E402
from channels import service as channel_service  # noqa: E402
from collection_schedule import CollectionSchedules
from self_hosted_collection import CollectionSupervisor  # noqa: E402
from self_hosted_configuration import ConfigurationStore, key_refusal, provider_key, record_key_refusal  # noqa: E402
import self_hosted_onboarding  # noqa: E402
from self_hosted_settings import SettingsError, preset  # noqa: E402
import youtube_client  # noqa: E402
from youtube_errors import key_refusal_code, provider_refusal_code  # noqa: E402

KEY = 'synthetic_test_key_no_requests'
NEW_KEY = 'synthetic_second_key_no_requests'
# YouTube's answer to an invalid key, as observed on 28.09.2026: the generic reason is badRequest.
INVALID_KEY = {'error': {'code': 400, 'message': 'API key not valid. Please pass a valid API key.',
                         'errors': [{'message': 'API key not valid. Please pass a valid API key.', 'domain': 'global',
                                     'reason': 'badRequest'}],
                         'status': 'INVALID_ARGUMENT',
                         'details': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': 'API_KEY_INVALID',
                                      'domain': 'googleapis.com'}]}}


def refusal(status, reason):
    return {'error': {'code': status, 'errors': [{'reason': reason}]}}


class Response:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload

    def json(self):
        return self.payload

    headers = {}
    def iter_content(self, chunk_size): return iter((json.dumps(self.payload).encode(),))
    def close(self): pass


class ClassificationTests(unittest.TestCase):
    def test_refusals_of_the_key_are_told_apart_from_others(self):
        cases = [
            (400, INVALID_KEY, 'provider_key_rejected', 'provider_key_rejected'),
            (400, refusal(400, 'keyInvalid'), 'provider_key_rejected', 'provider_key_rejected'),
            (403, refusal(403, 'accessNotConfigured'), 'provider_access_denied', 'provider_access_denied'),
            (403, refusal(403, 'ipRefererBlocked'), 'provider_access_denied', 'provider_access_denied'),
            (403, refusal(403, 'quotaExceeded'), None, 'provider_quota_exceeded'),
            (403, refusal(403, 'rateLimitExceeded'), None, 'provider_unavailable'),
            (429, {}, None, 'provider_unavailable'),
            (503, {}, None, 'provider_unavailable'),
            (403, refusal(403, 'playlistItemsNotAccessible'), None, None),
            (404, refusal(404, 'playlistNotFound'), None, None),
            (400, refusal(400, 'invalidParameter'), None, None),
        ]
        for status, payload, key_code, code in cases:
            self.assertEqual(key_refusal_code(status, payload), key_code, (status, payload))
            self.assertEqual(provider_refusal_code(status, payload), code, (status, payload))


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        environment = patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_SETUP_DIRECTORY': self.directory})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = ConfigurationStore.initialize(self.directory)

    def running(self, **fields):
        self.store.update(self.store.read()['revision'], lambda v: v.update(
            api_key=KEY, prepared=True, phase='ready', workspace={'phase': 'complete', 'channels': {}}, **fields))

    def stored(self):
        return ConfigurationStore(self.directory).read()


class MarkTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        gate = patch('provider_runtime.admit_request', side_effect=lambda **kw: contextlib.nullcontext(kw.get('cancel')))
        gate.start()
        self.addCleanup(gate.stop)

    def test_a_refusal_of_the_stored_key_stops_requests(self):
        self.running()
        self.assertTrue(record_key_refusal(KEY, 'provider_key_rejected'))
        self.assertEqual(key_refusal(), 'provider_key_rejected')
        with self.assertRaises(SettingsError) as raised:
            provider_key(None)
        self.assertEqual(str(raised.exception), 'provider_key_rejected')
        # The installation stays open: stored data remains readable.
        self.assertEqual((self.stored()['prepared'], self.stored()['phase']), (True, 'ready'))

    def test_only_the_stored_key_of_a_prepared_installation_is_marked(self):
        self.store.update(0, lambda v: v.update(api_key=KEY, phase='saved'))
        self.assertFalse(record_key_refusal(KEY, 'provider_key_rejected'))    # first setup handles its own errors
        self.running()
        self.assertFalse(record_key_refusal(NEW_KEY, 'provider_key_rejected'))  # a key being checked or replaced
        self.assertFalse(record_key_refusal(KEY, 'provider_quota_exceeded'))    # waiting fixes the quota
        self.assertIsNone(self.stored()['error'])
        self.assertEqual(provider_key(None), KEY)

    def test_the_client_marks_the_key_it_sent_and_then_sends_nothing(self):
        self.running()
        sent = []

        def get(url, **kwargs):
            sent.append(kwargs['headers']['X-Goog-Api-Key'])
            return Response(400, INVALID_KEY)
        with patch.object(youtube_client.requests, 'get', get), \
                patch.dict(sys.modules, {'hot_runtime': types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None)}):
            response = youtube_client.youtube_api_get('channels', phase='add_channel', quota_bucket='general',
                                                      attempt_recorder=None, params={'id': 'x'})
            self.assertEqual(response.status_code, 400)
            with self.assertRaises(SettingsError):
                youtube_client.youtube_api_get('channels', phase='add_channel', quota_bucket='general',
                                               attempt_recorder=None, params={'id': 'x'})
        self.assertEqual(sent, [KEY])
        self.assertEqual(self.stored()['error'], 'provider_key_rejected')

    def test_a_refusal_about_one_resource_marks_nothing(self):
        self.running()
        with patch.object(youtube_client.requests, 'get', lambda url, **kw: Response(403, refusal(403, 'forbidden'))), \
                patch.dict(sys.modules, {'hot_runtime': types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None)}):
            youtube_client.youtube_api_get('playlistItems', phase='playlist_inventory', quota_bucket='general',
                                           attempt_recorder=None, params={'playlistId': 'x'})
        self.assertIsNone(self.stored()['error'])


class ResumeTests(StoreTestCase):
    def check(self, code):
        from ops import self_hosted_owner
        out = io.StringIO()
        argv = ['self_hosted_owner.py', '--database', str(Path(self.directory, 'owner.db')), 'check-api-key']
        lifecycle = []
        @contextlib.contextmanager
        def resources():
            lifecycle.append('open')
            try:
                yield
            finally:
                lifecycle.append('close')
        def check_key(key):
            self.assertEqual(lifecycle, ['open'])
            lifecycle.append('check')
            return code
        with patch.dict(sys.modules, {'core': types.SimpleNamespace(process_database_resources=resources)}), \
                patch.object(self_hosted_onboarding, 'check_api_key', check_key), \
                patch('provider_runtime.start_command', return_value=object()), \
                patch('provider_runtime.stop_runtime'), \
                patch.object(sys, 'argv', argv), contextlib.redirect_stdout(out):
            self.assertEqual(self_hosted_owner.main(), 0)
        self.assertEqual(lifecycle, ['open', 'check', 'close'])
        return json.loads(out.getvalue())

    def test_a_key_youtube_accepts_resumes_collection(self):
        self.running(error='provider_key_rejected', pending_api_key=NEW_KEY)
        self.check(None)
        self.assertEqual((self.stored()['api_key'], self.stored()['error']), (NEW_KEY, None))
        self.assertIsNone(key_refusal())
        self.assertEqual(provider_key(None), NEW_KEY)

    def test_a_second_refused_key_keeps_collection_stopped(self):
        self.running(error='provider_access_denied', pending_api_key=NEW_KEY)
        result = self.check('provider_key_rejected')
        self.assertTrue(result['previous_key_refused'])
        self.assertEqual((self.stored()['api_key'], self.stored()['error']), (KEY, 'provider_access_denied'))
        code, message = manage.BOTH_KEYS_REFUSED
        self.assertEqual(code, 1)
        self.assertIn('stays stopped', message)


class SupervisorTests(unittest.TestCase):
    def test_no_pass_starts_while_the_key_is_refused_and_one_starts_after(self):
        settings = dict(preset='standard', limits=preset('standard'), automatic_collection=True, interval_minutes=30,
                        manual_stats_quota_units=200, handoff_grace_seconds=120)
        value = {'prepared': True, 'phase': 'ready', 'error': 'provider_key_rejected', 'settings': settings}
        starts = []

        def spawn(args, **kwargs):
            starts.append(args[-1])
            return types.SimpleNamespace(stdin=io.BytesIO(), stderr=None, poll=lambda: None)
        manager = types.SimpleNamespace(lock=threading.RLock(), store=types.SimpleNamespace(read=lambda: value))
        supervisor = CollectionSupervisor(manager, ready=lambda: None, schedules=CollectionSchedules(), spawn=spawn)
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'OPERATOR_JOB_WAKE_PATH': directory + '/wake'}):
            supervisor.tick()
            self.assertEqual((supervisor.status()['state'], starts), ('key_refused', []))
            value['error'] = None  # set-key stored a key YouTube accepts
            supervisor.tick()
        self.assertEqual(len(starts), 1)
        self.assertEqual(supervisor.status()['state'], 'running')


class AddChannelTests(unittest.TestCase):
    def add(self, response):
        repository = types.SimpleNamespace(load_channel_record=lambda channel_id: None)
        cache = types.SimpleNamespace(get=lambda *locator: (False, None), put=lambda *a: None)
        return channel_service.add_channel(
            'UCabcdefghijklmnopqrstuv', deployment_profile='self-hosted', repository=repository,
            trash_repository=None, lookup_cache=cache, channel_lock=lambda channel_id: contextlib.nullcontext(),
            parse_legacy_input=None, get_channel_id_from_video=None, lookup_channel_response=lambda params: response,
            estimate_initial_inventory=None, metadata_update_values=None, prepare_hydration=None, on_change=None,
            clock=lambda: None)

    def test_the_answer_names_the_refusal(self):
        cases = {Response(400, INVALID_KEY): ('provider_key_rejected', 'set-key'),
                 Response(403, refusal(403, 'accessNotConfigured')): ('provider_access_denied', 'YouTube Data API v3'),
                 Response(403, refusal(403, 'quotaExceeded')): ('provider_quota_exceeded', 'daily reset'),
                 Response(503, {}): ('channel_lookup_unavailable', 'Try again later')}
        for response, (code, text) in cases.items():
            result = self.add(response)
            self.assertEqual(result['code'], code)
            self.assertIn(text, result['error'])


from http_test_support import channel_http, endpoint, system_http
from web.models import ChannelRequest


class ReportTests(unittest.TestCase):
    def test_home_and_health_lead_with_the_refusal(self):
        for refusal in ('provider_key_rejected', 'provider_access_denied'):
            with patch('self_hosted_configuration.key_refusal', return_value=refusal), \
                 patch('web.system.collect_pipeline_health', return_value={'status': 'ok', 'critical': ['latest sync run failed']}):
                report = endpoint(system_http(), 'system_pipeline_health')()
            self.assertEqual((report['status'], report['provider_key']), ('critical', {'status': refusal}))
            self.assertIn('set-key', report['critical'][0])
            self.assertEqual(report['critical'][1], 'latest sync run failed')

    def test_a_working_key_changes_no_report(self):
        with patch('self_hosted_configuration.key_refusal', return_value=None), \
             patch('self_hosted_configuration.collection_paused', return_value=False), \
             patch('web.system.collect_pipeline_health', return_value={'status': 'ok', 'critical': []}):
            self.assertEqual(endpoint(system_http(), 'system_pipeline_health')(), {'status': 'ok', 'critical': []})

    def test_an_add_that_meets_a_marked_key_says_why(self):
        def refused(*args, **kwargs):
            raise SettingsError('provider_key_rejected')
        import asyncio
        with patch('web.channels.ChannelCommandRepository'), patch('web.channels.ChannelTrashRepository'), \
             patch('web.channels.channel_service.add_channel', side_effect=refused):
            result = asyncio.run(endpoint(channel_http().add_router, 'add_channel')(ChannelRequest(url='@test'), None))
        self.assertEqual(result['code'], 'provider_key_rejected')
        self.assertIn('set-key', result['error'])


if __name__ == '__main__':
    unittest.main()
