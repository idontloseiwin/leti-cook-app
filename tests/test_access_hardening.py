"""Owner access hardening: hashing outside the store lock, lockout recovery, the application CSP
and image hosts. SQLite in a temporary folder; no server, Docker or network."""
import ast
import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
sys.path.insert(0, str(ROOT))
from db.self_hosted_auth import ATTEMPT_LIMIT, AuthStoreError, OwnerAuthRepository
import manage

NOW = 1_800_000_000
SERVER_STACK = all(importlib.util.find_spec(name) for name in ('argon2', 'starlette'))


def make_repository(path):
    repository = OwnerAuthRepository.initialize(path, 'H:dummy', NOW)
    # Every fixture uses the same clock before its first session is issued.
    # Real wall time must not overtake the fixed test timestamp.
    repository.clock = lambda: NOW
    return repository


def constants(path, *names):
    """Evaluate module-level literal assignments without importing the module."""
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    namespace = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), namespace)
    return namespace


class Store(unittest.TestCase):
    """An owner 'owner' whose password hash is 'H:secret'; hashing is simulated by the callbacks."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.repository = make_repository(Path(self.folder.name) / 'auth.sqlite3')
        self.repository.issue_setup('setup-digest', NOW)
        self.assertEqual(self.register(), 'ok')

    def register(self, hash_password=lambda: 'H:secret', session='session-0'):
        return self.repository.register('owner', lambda stored: stored == 'setup-digest', hash_password, session, NOW)

    def login(self, verify=lambda stored: stored == 'H:secret', username='owner', session='session-1', now=NOW):
        return self.repository.login(lambda stored: stored == username, verify, session, now)

    def lock_is_free(self):
        # A write transaction here fails after 0.5 s if the caller still holds the store lock.
        with self.repository.transaction() as db:
            return db.execute('SELECT attempt_count FROM owner').fetchone()[0]


class HashingOutsideTheLockTests(Store):
    def test_password_check_holds_no_lock_and_the_attempt_already_counts(self):
        seen = []
        self.assertEqual(self.login(lambda stored: seen.append(self.lock_is_free()) or stored == 'H:secret'), 'ok')
        self.assertEqual(seen, [1])

    def test_registration_and_setup_hash_hold_no_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = make_repository(Path(folder) / 'auth.sqlite3')
            repository.issue_setup('code', NOW)
            self.assertEqual(repository.claim_setup(lambda stored: stored == 'code', 'setup-session', NOW), 'ok')
            free = []

            def hash_password():
                with repository.transaction():
                    free.append(True)
                return 'H:set'
            self.assertEqual(repository.complete_setup('setup-session', 'password', 'owner', hash_password, 's', NOW), 'ok')
            self.assertEqual(free, [True])
        with tempfile.TemporaryDirectory() as folder:
            repository = make_repository(Path(folder) / 'auth.sqlite3')
            repository.issue_setup('code', NOW)
            free = []

            def hash_password():
                with repository.transaction():
                    free.append(True)
                return 'H:new'
            self.assertEqual(repository.register('owner', lambda stored: stored == 'code', hash_password, 's', NOW), 'ok')
            self.assertEqual(free, [True])

    def test_a_slow_sign_in_keeps_a_browser_that_signed_in_meanwhile(self):
        clock = [NOW]
        self.repository.clock = lambda: clock[0]

        def slow_verify(stored):
            # A second browser starts later and finishes while this hash runs.
            clock[0] = NOW + 1
            self.assertEqual(self.login(session='second', now=NOW + 1), 'ok')
            return stored == 'H:secret'
        self.assertEqual(self.login(slow_verify, session='first'), 'ok')
        self.assertIsNotNone(self.repository.session('second', NOW + 1))
        self.assertIsNotNone(self.repository.session('first', NOW + 1))
        self.assertIsNotNone(self.repository.session('session-0', NOW + 1))

    def test_a_password_reset_during_the_check_wins(self):
        def verify(stored):
            self.repository.reset_password('H:changed')
            return stored == 'H:secret'
        self.assertEqual(self.login(verify), 'invalid_credentials')
        self.assertEqual(self.login(lambda stored: stored == 'H:changed', session='session-2'), 'ok')

    def test_wrong_password_and_wrong_username_take_the_same_path(self):
        opened = []
        original = self.repository.transaction

        @contextlib.contextmanager
        def counted():
            opened.append(1)
            with original() as db:
                yield db
        with patch.object(self.repository, 'transaction', counted):
            for verify, username in ((lambda s: False, 'owner'), (lambda s: True, 'someone'), (lambda s: True, 'owner')):
                opened.clear()
                self.login(verify, username)
                self.assertEqual(len(opened), 2)

    def test_a_setup_code_used_during_hashing_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = make_repository(Path(folder) / 'auth.sqlite3')
            repository.issue_setup('code', NOW)
            inner = []

            def slow_hash():
                inner.append(repository.register('second', lambda s: s == 'code', lambda: 'H:second', 's2', NOW))
                return 'H:first'
            self.assertEqual(repository.register('first', lambda s: s == 'code', slow_hash, 's1', NOW), 'setup_closed')
            self.assertEqual(inner, ['ok'])
            self.assertEqual(repository.login(lambda u: u == 'second', lambda h: h == 'H:second', 's3', NOW), 'ok')

    def test_a_setup_session_closed_during_hashing_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = make_repository(Path(folder) / 'auth.sqlite3')
            repository.issue_setup('code', NOW)
            repository.claim_setup(lambda s: s == 'code', 'setup-session', NOW)

            def slow_hash():
                repository.complete_setup('setup-session', 'local', None, lambda: None, None, NOW)
                return 'H:late'
            self.assertEqual(repository.complete_setup('setup-session', 'password', 'owner', slow_hash, 's', NOW),
                             'setup_closed')
            self.assertEqual(repository.access_state(NOW)['mode'], 'local')

    def test_an_invalid_mode_is_refused_before_hashing(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = make_repository(Path(folder) / 'auth.sqlite3')
            repository.issue_setup('code', NOW)
            repository.claim_setup(lambda s: s == 'code', 'setup-session', NOW)
            with self.assertRaises(AuthStoreError):
                repository.complete_setup('setup-session', 'other', None, lambda: self.fail('hashed'), None, NOW)


class LockoutRecoveryTests(Store):
    def exhaust(self):
        for index in range(ATTEMPT_LIMIT):
            self.assertEqual(self.login(lambda s: False, session=f'bad-{index}'), 'invalid_credentials')
        self.assertEqual(self.login(session='good'), 'rate_limited')

    def test_clearing_the_limit_keeps_password_and_sessions(self):
        self.exhaust()
        self.assertTrue(self.repository.sign_in_limited(NOW))
        self.repository.clear_sign_in_limit()
        self.assertFalse(self.repository.sign_in_limited(NOW))
        self.assertIsNotNone(self.repository.session('session-0', NOW))
        self.assertEqual(self.login(session='good'), 'ok')

    def test_the_limit_expires_on_its_own(self):
        self.exhaust()
        self.assertFalse(self.repository.sign_in_limited(NOW + 60))

    def test_manage_command_runs_without_a_terminal_and_prints_no_secret(self):
        calls, out = [], io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
            with patch.object(manage, '__file__', str(Path(directory, 'manage.py'))), \
                    patch.object(manage, 'safe_path', Path), patch.object(manage, 'check_docker', lambda: None), \
                    patch.object(manage, 'compose_prefix', lambda *a: ['docker', 'compose']), \
                    patch.object(manage, 'command', lambda args, **kw: calls.append((args, kw)) or ''), \
                    patch('ops.updater.management', lambda *a: contextlib.nullcontext((None, False))), \
                    patch.object(sys, 'argv', ['manage.py', 'clear-sign-in-limit']), \
                    patch.object(sys.stdin, 'isatty', lambda: False), contextlib.redirect_stdout(out):
                self.assertEqual(manage.main(), 0)
        self.assertEqual(calls[0][0], ['docker', 'compose', 'exec', '-T', 'backend', 'python',
                                       'ops/self_hosted_owner.py', 'clear-sign-in-limit'])
        self.assertNotIn('interactive', calls[0][1])
        self.assertIn('password and signed-in browsers are unchanged', out.getvalue())

    @unittest.skipUnless(SERVER_STACK, 'argon2 and starlette are installed in CI')
    def test_owner_command_clears_and_reports_the_limit(self):
        from ops import self_hosted_owner
        self.exhaust()
        database = str(self.repository.path)

        def run(command):
            out = io.StringIO()
            with patch.object(sys, 'argv', ['self_hosted_owner.py', '--database', database, command]), \
                    patch('time.time', lambda: NOW), contextlib.redirect_stdout(out):
                self.assertEqual(self_hosted_owner.main(), 0)
            return json.loads(out.getvalue())
        self.assertEqual(run('status'), {'configured': True, 'sign_in_limited': True})
        self.assertEqual(run('clear-sign-in-limit'), {'status': 'sign_in_limit_cleared', 'password_changed': False})
        self.assertEqual(run('status'), {'configured': True, 'sign_in_limited': False})


class ApplicationPolicyTests(unittest.TestCase):
    def setUp(self):
        values = constants('application/self_hosted_auth.py', 'IMAGE_HOSTS', 'APPLICATION_CSP')
        self.hosts, self.policy = values['IMAGE_HOSTS'], values['APPLICATION_CSP'].decode()
        self.directives = dict((part.split()[0], part.split()[1:]) for part in self.policy.split('; '))

    def test_the_policy_allows_only_the_application_and_youtube_images(self):
        self.assertNotIn('unsafe', self.policy)
        for directive in ('default-src', 'script-src', 'style-src', 'font-src', 'connect-src', 'form-action'):
            self.assertEqual(self.directives[directive], ["'self'"], directive)
        self.assertEqual(self.directives['img-src'], ["'self'", 'data:', *('https://' + host for host in self.hosts)])
        for directive in ('object-src', 'base-uri', 'frame-ancestors'):
            self.assertEqual(self.directives[directive], ["'none'"], directive)

    def test_frontend_and_backend_use_the_same_image_hosts(self):
        source = (ROOT / 'application/frontend/src/explorer/imageHosts.ts').read_text(encoding='utf-8')
        listed = re.search(r'IMAGE_HOSTS: readonly string\[\] = \[(.*?)\];', source, re.S).group(1)
        self.assertEqual(tuple(re.findall(r"'([^']+)'", listed)), self.hosts)
        from video_thumbnails import video_thumbnail_url
        self.assertIn(urlsplit(video_thumbnail_url('abcdefghijk')).hostname, self.hosts)

    def test_every_image_goes_through_the_host_check_and_sends_no_referrer(self):
        explorer = ROOT / 'application/frontend/src/explorer'
        for name in ('sidebar.ts', 'videoPresentation.ts'):
            self.assertIn('safeImageUrl(value)', (explorer / name).read_text(encoding='utf-8'), name)
        for path in explorer.glob('*.tsx'):
            for tag in re.findall(r'<img\b[^>]*>', path.read_text(encoding='utf-8'), re.S):
                self.assertIn('referrerPolicy="no-referrer"', tag, path.name)

    def test_the_shipped_page_needs_nothing_the_policy_blocks(self):
        dist = ROOT / 'application/frontend/dist-self-hosted'
        page = (dist / 'explorer-v3.html').read_text(encoding='utf-8')
        self.assertNotRegex(page, r'<script(?![^>]*\bsrc=)|<style|\sstyle=|\son[a-z]+=')
        for reference in re.findall(r'(?:src|href)="([^"]+)"', page):
            self.assertTrue(reference.startswith('/app-explorer-v3/assets/'), reference)
        for script in (dist / 'assets').glob('*.js'):
            self.assertNotRegex(script.read_text(encoding='utf-8'), r'\beval\(|new Function\(', script.name)

    @unittest.skipUnless(SERVER_STACK, 'argon2 and starlette are installed in CI')
    def test_responses_without_a_policy_get_the_application_policy(self):
        from self_hosted_auth import SelfHostedAuthMiddleware

        class Repository:
            def configured(self): return True
            def access_state(self, now, setup_digest=None): return {'mode': 'local'}

        def inner(own_policy):
            async def app(scope, receive, send):
                headers = [(b'content-type', b'text/html')]
                if own_policy:
                    headers.append((b'content-security-policy', b"default-src 'none'"))
                await send({'type': 'http.response.start', 'status': 200, 'headers': headers})
                await send({'type': 'http.response.body', 'body': b'<!doctype html>'})
            return app

        def policies(app, path):
            sent = []

            async def receive(): return {'type': 'http.request', 'body': b''}

            async def send(message): sent.append(message)
            middleware = SelfHostedAuthMiddleware(app, repository=Repository(), origin='http://127.0.0.1:8002', worker_key='k')
            asyncio.run(middleware({'type': 'http', 'method': 'GET', 'path': path, 'raw_path': path.encode(),
                                    'query_string': b'', 'headers': [(b'host', b'127.0.0.1:8002')]}, receive, send))
            start = next(m for m in sent if m['type'] == 'http.response.start')
            return [v.decode() for k, v in start['headers'] if k.lower() == b'content-security-policy']
        self.assertEqual(policies(inner(False), '/app-explorer-v3'), [self.policy])
        self.assertEqual(policies(inner(True), '/setup'), ["default-src 'none'"])


if __name__ == '__main__':
    unittest.main()
