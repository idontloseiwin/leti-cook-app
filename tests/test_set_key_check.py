"""set-key asks YouTube about a new key and keeps only one it accepts (tester findings T-21, T-25). No provider."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'application'))
import manage  # noqa: E402
from install import InstallError  # noqa: E402
from self_hosted_configuration import ConfigurationStore  # noqa: E402
import self_hosted_onboarding  # noqa: E402

KEY = 'synthetic_test_key_no_requests'
NEW_KEY = 'synthetic_second_key_no_requests'


class Unavailable(Exception):
    pass


class Response:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload

    def json(self):
        return self.payload


def refusal(status, reason):
    return Response(status, {'error': {'code': status, 'errors': [{'reason': reason}]}})


def provider_modules(answer):
    calls = []

    def youtube_api_get(endpoint, **kwargs):
        calls.append((endpoint, kwargs['params'], kwargs['phase'], kwargs['quota_units']))
        if isinstance(answer, Exception):
            raise answer
        return answer
    modules = {'core': types.SimpleNamespace(YOUTUBE_QUOTA_ATTEMPT_RECORDER=None),
               'hot_runtime': types.SimpleNamespace(require_hot_runtime_ready=lambda **kw: None,
                                                    HotRuntimeUnavailable=Unavailable),
               'youtube_client': types.SimpleNamespace(youtube_api_get=youtube_api_get)}
    return modules, calls


class CheckApiKeyTests(unittest.TestCase):
    def check(self, answer):
        modules, calls = provider_modules(answer)
        with patch.dict(sys.modules, modules):
            return self_hosted_onboarding.check_api_key(KEY), calls

    def test_one_request_decides(self):
        code, calls = self.check(Response(200, {'items': [{'snippet': {'hl': 'en', 'name': 'English'}}]}))
        self.assertIsNone(code)
        self.assertEqual(calls, [('i18nLanguages', {'part': 'snippet', 'hl': 'en'}, 'category_metadata', 1)])
        cases = {refusal(400, 'keyInvalid'): 'provider_key_rejected',
                 refusal(403, 'accessNotConfigured'): 'provider_access_denied',
                 refusal(403, 'quotaExceeded'): 'provider_quota_exceeded',
                 Response(503, {}): 'provider_unavailable'}
        for answer, expected in cases.items():
            self.assertEqual(self.check(answer)[0], expected)

    def test_a_network_failure_is_not_a_refusal(self):
        import requests
        self.assertEqual(self.check(requests.ConnectionError('down'))[0], 'provider_unavailable')


@contextlib.contextmanager
def owned_provider_check(check):
    """Observe the CLI resource scope without importing core or opening a pool."""
    lifecycle = []
    @contextlib.contextmanager
    def resources():
        lifecycle.append('open')
        try:
            yield
        finally:
            lifecycle.append('close')
    def check_key(key):
        if lifecycle != ['open']:
            raise AssertionError('provider check must run inside the owned resource scope')
        lifecycle.append('check')
        return check(key)
    with patch.dict(sys.modules, {'core': types.SimpleNamespace(process_database_resources=resources)}), \
            patch.object(self_hosted_onboarding, 'check_api_key', check_key), \
            patch('provider_runtime.start_command', return_value=object()), \
            patch('provider_runtime.stop_runtime'):
        yield lifecycle


class OwnerCommandTests(unittest.TestCase):
    def run_check(self, code):
        from ops import self_hosted_owner
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LIC_SETUP_DIRECTORY': directory}):
            store = ConfigurationStore.initialize(directory)
            store.update(0, lambda v: v.update(api_key=KEY, phase='saved'))
            out = io.StringIO()
            with owned_provider_check(lambda key: code) as lifecycle, \
                    patch.object(sys, 'argv', ['self_hosted_owner.py', '--database', str(Path(directory, 'owner.db')),
                                               'check-api-key']), contextlib.redirect_stdout(out):
                self.assertEqual(self_hosted_owner.main(), 0)
            self.assertEqual(lifecycle, ['open', 'check', 'close'])
            return json.loads(out.getvalue()), ConfigurationStore(directory).read()

    def test_a_refused_key_is_not_kept(self):
        for code in ('provider_key_rejected', 'provider_access_denied'):
            result, stored = self.run_check(code)
            self.assertEqual((result['status'], result['key_kept']), (code, False))
            self.assertEqual((stored['api_key'], stored['phase'], stored['error']), (None, 'needs_key', code))

    def test_an_accepted_or_unchecked_key_stays(self):
        for code in (None, 'provider_quota_exceeded', 'provider_unavailable'):
            result, stored = self.run_check(code)
            self.assertEqual(result['status'], code or 'key_accepted')
            self.assertEqual((stored['api_key'], stored['phase']), (KEY, 'saved'))


class RunningKeyChangeTests(unittest.TestCase):
    """A key change on a prepared installation is not a new setup (T-25)."""

    def owner(self, directory, command, *, stdin=None, check=lambda key: None):
        from ops import self_hosted_owner
        out = io.StringIO()
        argv = ['self_hosted_owner.py', '--database', str(Path(directory, 'owner.db')), command]
        with patch.dict(os.environ, {'LIC_SETUP_DIRECTORY': directory}), \
                owned_provider_check(check) as lifecycle, \
                patch.object(sys, 'argv', argv + (['--stdin'] if stdin else [])), \
                patch.object(sys, 'stdin', io.StringIO(stdin or '')), contextlib.redirect_stdout(out):
            self.assertEqual(self_hosted_owner.main(), 0)
        self.assertEqual(lifecycle, ['open', 'check', 'close'] if command == 'check-api-key' else [])
        return json.loads(out.getvalue()), ConfigurationStore(directory).read()

    def running(self, directory):
        store = ConfigurationStore.initialize(directory)
        store.update(0, lambda v: v.update(api_key=KEY, prepared=True, phase='ready',
                                           workspace={'phase': 'complete', 'channels': {}}))
        self.owner(directory, 'set-api-key', stdin=NEW_KEY + '\n')

    def test_a_new_installation_still_starts_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            ConfigurationStore.initialize(directory)
            _, stored = self.owner(directory, 'set-api-key', stdin=NEW_KEY + '\n')
            self.assertEqual((stored['api_key'], stored['prepared'], stored['phase']), (NEW_KEY, False, 'saved'))
            self.assertNotIn('pending_api_key', stored)

    def test_saving_keeps_the_working_key_and_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            self.running(directory)
            stored = ConfigurationStore(directory).read()
            self.assertEqual((stored['api_key'], stored['pending_api_key']), (KEY, NEW_KEY))
            self.assertEqual((stored['prepared'], stored['phase'], stored['workspace']['phase']), (True, 'ready', 'complete'))

    def test_the_check_asks_about_the_new_key(self):
        asked = []
        with tempfile.TemporaryDirectory() as directory:
            self.running(directory)
            self.owner(directory, 'check-api-key', check=asked.append)
        self.assertEqual(asked, [NEW_KEY])

    def test_a_refused_key_leaves_the_previous_one_in_use(self):
        for code in ('provider_key_rejected', 'provider_access_denied'):
            with tempfile.TemporaryDirectory() as directory:
                self.running(directory)
                result, stored = self.owner(directory, 'check-api-key', check=lambda key: code)
                self.assertEqual((result['key_kept'], result['running'], result['previous_key_in_use']), (False, True, True))
                self.assertEqual((stored['api_key'], stored['prepared'], stored['phase']), (KEY, True, 'ready'))
                self.assertNotIn('pending_api_key', stored)

    def test_an_accepted_or_unchecked_key_replaces_the_old_one(self):
        for code in (None, 'provider_quota_exceeded', 'provider_unavailable'):
            with tempfile.TemporaryDirectory() as directory:
                self.running(directory)
                result, stored = self.owner(directory, 'check-api-key', check=lambda key: code)
                self.assertEqual((result['key_kept'], result['running']), (True, True))
                self.assertEqual((stored['api_key'], stored['prepared'], stored['phase']), (NEW_KEY, True, 'ready'))
                self.assertNotIn('pending_api_key', stored)

    def test_a_waiting_key_needs_a_prepared_installation(self):
        from self_hosted_settings import SettingsError
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigurationStore.initialize(directory)
            store.update(0, lambda v: v.update(api_key=KEY, pending_api_key=NEW_KEY))
            with self.assertRaises(SettingsError):
                store.read()


class ManageSetKeyTests(unittest.TestCase):
    def check(self, output=None, fail=False):
        calls, out, err = [], io.StringIO(), io.StringIO()

        def command(args, **kw):
            calls.append(args[2:])
            if args[2] == 'exec':
                if fail:
                    raise InstallError('command_failed_1')
                return output
            return ''
        with patch.object(manage, 'command', command), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = manage.check_key(Path('.'), ['docker', 'compose'])
        return code, calls, out.getvalue(), err.getvalue()

    def test_the_backend_is_waited_for_then_asked(self):
        code, calls, out, err = self.check(json.dumps({'status': 'key_accepted'}) + '\n')
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in calls], ['up', 'exec'])
        self.assertEqual(calls[1], manage.KEY_CHECK)
        self.assertIn('accepted by YouTube', out)

    def test_a_rejected_key_fails_the_command(self):
        code, calls, out, err = self.check(json.dumps({'status': 'provider_key_rejected'}))
        self.assertEqual(code, 1)
        self.assertIn('was not kept', err)

    def test_a_running_installation_is_told_which_key_is_in_use(self):
        cases = {'key_accepted': (0, 'now in use'), 'provider_key_rejected': (1, 'previous key is still in use'),
                 'provider_access_denied': (1, 'previous key is still in use'),
                 'provider_quota_exceeded': (0, 'now in use'), 'maintenance_not_ready': (0, 'now in use')}
        for status, (expected, text) in cases.items():
            code, calls, out, err = self.check(json.dumps({'status': status, 'running': True}))
            self.assertEqual(code, expected, status)
            self.assertIn(text, err if expected else out, status)
            self.assertNotIn('/setup', out + err, status)

    def test_an_unchecked_key_is_reported_as_saved_but_unchecked(self):
        for kwargs in ({'fail': True}, {'output': 'not json'}, {'output': json.dumps({'status': 'maintenance_not_ready'})}):
            code, calls, out, err = self.check(**kwargs)
            self.assertEqual(code, 0, kwargs)
            self.assertIn('could not be asked', out)


if __name__ == '__main__':
    unittest.main()
