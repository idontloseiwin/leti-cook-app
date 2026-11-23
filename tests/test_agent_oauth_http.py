"""MCP sign-in with the owner's Allow (OAuth 2.1) through the real middleware stack.

Synthetic owner store and research database; no PostgreSQL, provider or network.
Covers discovery, registration, consent, PKCE code exchange, refresh rotation,
reuse detection, revocation, the token's use on /mcp and the code, access-token
and connection lifetimes (time injected, no sleeps).
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from test_agent_mcp_http import ACCEPT, FIELDS, ORIGIN, TYPES, Installation, rpc
from agent_access import issue
import agent_oauth

VERIFIER = 'v' * 20 + 'erifier-' + 'x' * 30
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b'=').decode()
FORM = {'Content-Type': 'application/x-www-form-urlencoded'}


class OAuthCase(unittest.IsolatedAsyncioTestCase):
    """A local-mode installation and the steps of one sign-in."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        preferences = {'revision': 1, 'effective': {kind: {field: 'off' for field in FIELDS} for kind in TYPES}}
        for active in (patch.dict(os.environ, {'LIC_PUBLIC_ORIGIN': ORIGIN,
                                               'LIC_AUTH_DATABASE': str(Path(directory.name) / 'owner.sqlite3')}),
                       patch('web.research.ResearchPreferencesRepository.read', return_value=preferences)):
            active.start()
            self.addCleanup(active.stop)
        self.site = Installation(directory.name, 'local')

    async def register(self, client, uris=('http://localhost:54321/callback',), name='Claude Code'):
        answer = await client.post('/oauth/register', json={'client_name': name, 'redirect_uris': list(uris),
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'token_endpoint_auth_method': 'none'})
        return answer

    def authorize_query(self, client_id, redirect='http://localhost:60123/callback', **extra):
        query = {'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect,
                 'code_challenge': CHALLENGE, 'code_challenge_method': 'S256', 'state': 'xyz',
                 'resource': ORIGIN + '/mcp', 'scope': 'research.read', **extra}
        return '/oauth/authorize?' + urlencode({k: v for k, v in query.items() if v is not None})

    async def allow(self, browser, client_id, redirect='http://localhost:60123/callback', days='7', decision='allow'):
        page = await browser.get(self.authorize_query(client_id, redirect))
        self.assertEqual(page.status_code, 200, page.text)
        request = re.search(r'name="request" value="([0-9a-f]{32})"', page.text).group(1)
        answer = await browser.post('/oauth/authorize', content=urlencode({'request': request, 'decision': decision,
                                    'days': days}), headers=FORM)
        self.assertEqual(answer.status_code, 303, answer.text)
        return page, answer.headers['location']

    async def exchange(self, program, client_id, code, verifier=VERIFIER, redirect='http://localhost:60123/callback'):
        return await program.post('/oauth/token', content=urlencode({'grant_type': 'authorization_code', 'code': code,
            'client_id': client_id, 'code_verifier': verifier, 'redirect_uri': redirect,
            'resource': ORIGIN + '/mcp'}), headers=FORM)

class OAuthFlowTests(OAuthCase):
    async def test_discovery_and_registration(self):
        async with self.site.client(Accept=ACCEPT) as program:
            refused = await program.post('/mcp', json=rpc('ping'))
            self.assertEqual(refused.status_code, 401)
            challenge = refused.headers['www-authenticate']
            self.assertEqual(challenge, 'Bearer resource_metadata="' + ORIGIN
                             + '/.well-known/oauth-protected-resource/mcp", scope="research.read"')
            for path in ('/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-protected-resource'):
                meta = (await program.get(path)).json()
                self.assertEqual(meta['resource'], ORIGIN + '/mcp')
                self.assertEqual(meta['authorization_servers'], [ORIGIN])
            server = (await program.get('/.well-known/oauth-authorization-server')).json()
            self.assertEqual((server['issuer'], server['token_endpoint'], server['registration_endpoint']),
                             (ORIGIN, ORIGIN + '/oauth/token', ORIGIN + '/oauth/register'))
            self.assertEqual(server['code_challenge_methods_supported'], ['S256'])
            self.assertEqual(server['token_endpoint_auth_methods_supported'], ['none'])
            self.assertNotIn('client_id_metadata_document_supported', server)
            mixed = await self.register(program, ('http://localhost:54321/callback', 'https://vscode.dev/redirect',
                                                  'http://127.0.0.1:33418/', 'cursor://anysphere.cursor-retrieval/oauth/cb'))
            self.assertEqual(mixed.status_code, 201, mixed.text)
            body = mixed.json()
            self.assertRegex(body['client_id'], r'^lic-client-[0-9a-f]{32}$')
            self.assertEqual(body['redirect_uris'], ['http://localhost:54321/callback', 'http://127.0.0.1:33418/',
                                                     'cursor://anysphere.cursor-retrieval/oauth/cb'])
            self.assertEqual(body['token_endpoint_auth_method'], 'none')
            for uris in (['https://claude.ai/api/mcp/auth_callback'], ['javascript:alert(1)'], ['http://evil.test/cb'],
                         ['myapp:/cb'], ['http://localhost:1/cb#x'], []):
                answer = await self.register(program, uris)
                self.assertEqual(answer.status_code, 400, uris)
            self.assertEqual((await program.post('/oauth/register', content=b'{', headers={'Content-Type': 'application/json'})).status_code, 400)
            self.assertEqual((await program.post('/oauth/register', content=b'x' * 9000,
                                                 headers={'Content-Type': 'application/json'})).status_code, 400)
            foreign = await program.post('/oauth/register', json={'redirect_uris': ['http://localhost:1/cb']},
                                         headers={'Origin': 'http://evil.test'})
            self.assertEqual(foreign.json()['code'], 'cross_origin_denied')
            self.assertEqual((await program.get('/oauth/token')).status_code, 405)

    async def test_allow_exchange_use_refresh_and_revoke(self):
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            client_id = (await self.register(program, name='<b>Claude</b> Code')).json()['client_id']
            page, location = await self.allow(browser, client_id)
            self.assertIn('&lt;b&gt;Claude&lt;/b&gt; Code', page.text)
            self.assertNotIn('<b>Claude</b>', page.text)
            self.assertIn("form-action 'self' http://localhost:60123", page.headers['content-security-policy'])
            # Without it the app-wide no-referrer policy makes the form send "Origin: null".
            self.assertIn('<meta name="referrer" content="same-origin">', page.text)
            target = urlsplit(location)
            self.assertEqual((target.scheme, target.netloc, target.path), ('http', 'localhost:60123', '/callback'))
            answer = parse_qs(target.query)
            self.assertEqual((answer['state'], answer['iss']), (['xyz'], [ORIGIN]))
            code = answer['code'][0]
            listed = (await browser.get('/agent-connections')).json()
            self.assertEqual(listed['connections'], [], 'a connection appears only after the code exchange')
            wrong = await self.exchange(program, client_id, code, verifier='w' * 43)
            self.assertEqual(wrong.json()['error'], 'invalid_grant')
            again = await self.exchange(program, client_id, code)
            self.assertEqual(again.json()['error'], 'invalid_grant', 'a code is consumed by its first use')

            _, location = await self.allow(browser, client_id)
            code = parse_qs(urlsplit(location).query)['code'][0]
            tokens = await self.exchange(program, client_id, code)
            self.assertEqual(tokens.status_code, 200, tokens.text)
            self.assertEqual(tokens.headers['cache-control'], 'no-store')
            first = tokens.json()
            self.assertEqual((first['token_type'], first['scope'], first['expires_in']), ('Bearer', 'research.read', 3600))
            self.assertRegex(first['access_token'], r'^lic_at_')
            self.assertRegex(first['refresh_token'], r'^lic_rt_')
            connection = (await browser.get('/agent-connections')).json()['connections']
            self.assertEqual([(c['name'], c['redirect_host']) for c in connection],
                             [('<b>Claude</b> Code', 'http://localhost:60123')])
            self.assertEqual(connection[0]['expires_at'] - connection[0]['created_at'], 7 * 86400)

            async with self.site.client(Authorization='Bearer ' + first['access_token'], Accept=ACCEPT) as agent:
                ping = await agent.post('/mcp', json=rpc('ping'))
                self.assertEqual(ping.status_code, 200, ping.text)
                found = (await agent.post('/mcp', json=rpc('tools/call', name='find_sources',
                                                           arguments={'kind': 'folder'}))).json()['result']
                self.assertFalse(found['isError'], found)
                self.assertEqual((await agent.get('/agent-keys')).json()['code'], 'agent_scope_denied')
            self.assertEqual(len(self.site.calls), 1)

            refresh = lambda token: program.post('/oauth/token', content=urlencode({'grant_type': 'refresh_token',
                'refresh_token': token, 'client_id': client_id}), headers=FORM)
            second = (await refresh(first['refresh_token'])).json()
            self.assertNotEqual(second['refresh_token'], first['refresh_token'])
            async with self.site.client(Authorization='Bearer ' + first['access_token'], Accept=ACCEPT) as stale:
                self.assertEqual((await stale.post('/mcp', json=rpc('ping'))).status_code, 401)
            async with self.site.client(Authorization='Bearer ' + second['access_token'], Accept=ACCEPT) as agent:
                self.assertEqual((await agent.post('/mcp', json=rpc('ping'))).status_code, 200)
                # A replaced refresh token presented again means it leaked: the connection ends.
                self.assertEqual((await refresh(first['refresh_token'])).json()['error'], 'invalid_grant')
                self.assertEqual((await agent.post('/mcp', json=rpc('ping'))).status_code, 401)
                self.assertEqual((await refresh(second['refresh_token'])).json()['error'], 'invalid_grant')
            self.assertEqual((await browser.get('/agent-connections')).json()['connections'], [])

            _, location = await self.allow(browser, client_id)
            third = (await self.exchange(program, client_id, parse_qs(urlsplit(location).query)['code'][0])).json()
            identity = (await browser.get('/agent-connections')).json()['connections'][0]['id']
            self.assertEqual((await browser.delete('/agent-connections/' + identity)).json(), {'revoked': True, 'id': identity})
            async with self.site.client(Authorization='Bearer ' + third['access_token'], Accept=ACCEPT) as agent:
                self.assertEqual((await agent.post('/mcp', json=rpc('ping'))).status_code, 401)

    async def test_authorize_rules_and_consent_boundaries(self):
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            client_id = (await self.register(program)).json()['client_id']
            for query, status in ((self.authorize_query('lic-client-' + '0' * 32), 400),
                                  (self.authorize_query(client_id, 'http://localhost:60123/other'), 400),
                                  (self.authorize_query(client_id, 'http://evil.test:60123/callback'), 400)):
                page = await browser.get(query)
                self.assertEqual(page.status_code, status, query)
                self.assertIn('text/html', page.headers['content-type'])
            for extra, error in (({'code_challenge_method': 'plain'}, 'invalid_request'), ({'code_challenge': None}, 'invalid_request'),
                                 ({'response_type': 'token'}, 'unsupported_response_type'),
                                 ({'resource': 'http://other.test/mcp'}, 'invalid_target'), ({'scope': 'admin'}, 'invalid_scope')):
                answer = await browser.get(self.authorize_query(client_id, **extra))
                self.assertEqual(answer.status_code, 302, extra)
                returned = parse_qs(urlsplit(answer.headers['location']).query)
                self.assertEqual((returned['error'], returned['state']), ([error], ['xyz']))
            _, denied = await self.allow(browser, client_id, decision='deny')
            self.assertEqual(parse_qs(urlsplit(denied).query)['error'], ['access_denied'])
            page = await browser.get(self.authorize_query(client_id))
            request = re.search(r'name="request" value="([0-9a-f]{32})"', page.text).group(1)
            form = urlencode({'request': request, 'decision': 'allow', 'days': '30'})
            async with self.site.client() as no_origin:
                self.assertEqual((await no_origin.post('/oauth/authorize', content=form, headers=FORM)).json()['code'],
                                 'same_origin_required')
            meta, key = issue(self.site.owner, 'reader')
            async with self.site.client(Authorization='Bearer ' + key, Origin=ORIGIN) as agent:
                self.assertEqual((await agent.post('/oauth/authorize', content=form, headers=FORM)).json()['code'],
                                 'agent_scope_denied')
            for bad in ({'request': request, 'decision': 'allow', 'days': '1000'}, {'request': request, 'decision': 'maybe'},
                        {'request': 'x', 'decision': 'allow'}):
                self.assertEqual((await browser.post('/oauth/authorize', content=urlencode(bad), headers=FORM)).status_code, 400)
            self.assertEqual((await browser.post('/oauth/authorize', content=form, headers=FORM)).status_code, 303)
            self.assertEqual((await browser.post('/oauth/authorize', content=form, headers=FORM)).status_code, 400,
                             'a request is answered once')

    async def test_signed_out_owner_returns_to_the_allow_page(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Installation(directory, 'password')
            async with site.client() as program, site.client(Accept='text/html') as browser:
                client_id = (await self.register(program)).json()['client_id']
                answer = await browser.get(self.authorize_query(client_id))
                self.assertEqual(answer.status_code, 303)
                location = answer.headers['location']
                self.assertTrue(location.startswith('/auth?return=%2Foauth%2Fauthorize%3F'), location)
        source = (ROOT / 'application/self_hosted_ui/auth.js').read_text(encoding='utf-8')
        self.assertIn("/^\\/oauth\\/authorize\\?", source)

    async def test_signed_out_owner_returns_to_the_app_page(self):
        """A reload after the session ended signs in and comes back to the same app page, nowhere else."""
        import self_hosted_auth
        page = '/app-explorer-v3/research?saved=11111111-1111-4111-8111-111111111111'
        with tempfile.TemporaryDirectory() as directory:
            site = Installation(directory, 'password')
            async with site.client(Accept='text/html') as browser:
                answer = await browser.get(page)
                self.assertEqual((answer.status_code, answer.headers['location']),
                                 (303, '/auth?return=%2Fapp-explorer-v3%2Fresearch%3Fsaved%3D11111111-1111-4111-8111-111111111111'))
                for other in ('/setup', '/app-explorer-v3//evil.example', '/app-explorer-v3x'):
                    answer = await browser.get(other)
                    self.assertEqual((answer.status_code, answer.headers['location']), (303, '/auth'), other)
        accepted = ['/app-explorer-v3', '/app-explorer-v3/', '/app-explorer-v3/research', page, page + '&settings=fields']
        refused = ['//evil.example', '/app-explorer-v3//evil.example', '/app-explorer-v3/../auth', '/app-explorer-v3/./research', '/\\evil.example',
                   'https://evil.example/app-explorer-v3', '/app-explorer-v3@evil.example', '/app-explorer-v3x', '/app-explorer-v3/research#top',
                   '/app-explorer-v3/research?x=<b>', '/app-explorer-v3/ x', '/auth', '', '/app-explorer-v3/research\n',
                   '/app-explorer-v3/%2e%2e/auth', '/app-explorer-v3/%2F%2Fevil.example', '/app-explorer-v3/.hidden']
        self.assertEqual([bool(self_hosted_auth.APP_RETURN.match(value)) for value in accepted], [True] * len(accepted))
        self.assertEqual([bool(self_hosted_auth.APP_RETURN.match(value)) for value in refused], [False] * len(refused))
        # The same pattern as the browser's (auth.js, ownerSession.ts): JS escapes '/', Python ends with \Z.
        source = (ROOT / 'application/self_hosted_ui/auth.js').read_text(encoding='utf-8')
        browser_pattern = re.search(r"const RETURN_PATHS = \[/.*?/, /(.*?)/\];", source).group(1)
        self.assertEqual('^' + self_hosted_auth.APP_RETURN.pattern.removesuffix('\\Z').replace('/', '\\/') + '$', browser_pattern)

    def test_redirect_rules(self):
        allowed = ['http://127.0.0.1:1234/cb', 'http://localhost/callback', 'http://[::1]:9/x',
                   'cursor://anysphere.cursor-retrieval/oauth/cb', 'vscode://vscode.mcp/callback',
                   'vscode-insiders://vscode.mcp/callback']
        refused = ['https://localhost/cb', 'http://localhost.evil.test/cb', 'http://127.0.0.2/cb', 'file:///x',
                   'javascript:alert(1)', 'data:text/html,x', 'myapp:/cb', 'http://localhost:0/cb', 'http://u:p@localhost/cb',
                   'com.example.app:/oauth', 'webcal://attacker.invalid/cb']
        self.assertEqual([agent_oauth.redirect_allowed(uri) for uri in allowed], [True] * len(allowed))
        self.assertEqual([agent_oauth.redirect_allowed(uri) for uri in refused], [False] * len(refused))
        self.assertTrue(agent_oauth.redirect_matches(['http://localhost/callback'], 'http://localhost:5555/callback'))
        self.assertTrue(agent_oauth.redirect_matches(['http://127.0.0.1:33418/'], 'http://127.0.0.1:4000/'))
        self.assertFalse(agent_oauth.redirect_matches(['http://localhost/callback'], 'http://127.0.0.1:5555/callback'))
        self.assertFalse(agent_oauth.redirect_matches(['http://localhost/callback'], 'http://localhost:5555/other'))
        self.assertFalse(agent_oauth.redirect_matches(['cursor://a.b/cb'], 'cursor://a.b/other'))
        # The authorize step parses as strictly as registration; only a loopback port may differ.
        registered = ['http://localhost:12345/callback', 'http://127.0.0.1:12345/callback', 'http://[::1]:12345/callback']
        for spoof in ('http://localhost:61234@attacker.invalid/callback', 'http://localhost@attacker.invalid/callback',
                      'http://attacker.invalid#@localhost:1/callback', 'http://localhost:1:2/callback',
                      'http://localhost:/callback', 'http://LOCALHOST.attacker.invalid:1/callback',
                      'http://127.0.0.1.attacker.invalid/callback', 'http://[::1]@attacker.invalid/callback',
                      'http://localhost:1/callback#frag', 'http://localhost:99999/callback', 'http://localhost:1\\@x/callback',
                      'http://localhost:1/callback ', 'https://localhost:1/callback'):
            with self.subTest(spoof=spoof):
                self.assertFalse(agent_oauth.redirect_matches(registered, spoof))
        for genuine in ('http://localhost:61234/callback', 'http://LocalHost:1/callback', 'http://127.0.0.1:80/callback',
                        'http://[::1]:61234/callback', 'http://localhost/callback'):
            with self.subTest(genuine=genuine):
                self.assertTrue(agent_oauth.redirect_matches(registered, genuine))


class AuthorizationRegressionTests(OAuthCase):
    """Regression tests for agent authorization."""

    async def test_userinfo_redirect_never_reaches_the_allow_page(self):
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            client_id = (await self.register(program, ('http://localhost:12345/callback',))).json()['client_id']
            page = await browser.get(self.authorize_query(client_id, 'http://localhost:61234@attacker.invalid/callback'))
            self.assertEqual(page.status_code, 400)
            self.assertNotIn('name="request"', page.text)
            self.assertNotIn('attacker.invalid', page.headers.get('location', ''))

    async def test_registration_never_evicts_a_sign_in_in_progress(self):
        from db.agent_oauth import AgentOAuthRepository, MAX_CLIENTS
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            client_id = (await self.register(program)).json()['client_id']
            page = await browser.get(self.authorize_query(client_id))
            request = re.search(r'name="request" value="([0-9a-f]{32})"', page.text).group(1)
            store = AgentOAuthRepository(self.site.owner.path)
            now = int(time.time())

            def flood():
                for index in range(MAX_CLIENTS + 5):
                    # 7 s apart: under the per-minute limit, still within the open request's lifetime.
                    store.register('flood %d' % index, ['http://localhost:1/cb'], now + 7 * (index + 1))
            await asyncio.to_thread(flood)
            self.assertIsNotNone(store.client(client_id), 'the client with an open Allow page stays registered')
            answer = await browser.post('/oauth/authorize', content=urlencode({'request': request, 'decision': 'allow',
                                        'days': '7'}), headers=FORM)
            self.assertEqual(answer.status_code, 303, answer.text)
            limited = []
            for _ in range(12):
                limited.append((await self.register(program, ('http://localhost:2/cb',))).status_code)
            self.assertIn(429, limited)
            self.assertNotIn(500, limited)

    async def test_day_old_registration_survives_live_consent_and_code(self):
        from db.agent_oauth import AgentOAuthRepository, UNUSED_CLIENT_SECONDS
        import secrets
        store = AgentOAuthRepository(self.site.owner.path)
        now = int(time.time())
        then = now - UNUSED_CLIENT_SECONDS - 1
        redirect = 'http://localhost:54321/callback'
        pending = store.register('Pending', [redirect], then)
        coded = store.register('Code', [redirect], then)
        unused = store.register('Unused', [redirect], then)
        request = store.open_request(pending, redirect, CHALLENGE, 'pending', ORIGIN + '/mcp', now - 6)
        code_request = store.open_request(coded, redirect, CHALLENGE, 'code', ORIGIN + '/mcp', now - 6)
        code = secrets.token_urlsafe(32)
        store.decide(code_request, True, agent_oauth.digest('code', code), self.site.owner.agent_binding(),
                     'http://localhost', 7, now - 6)
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            self.assertEqual((await self.register(program)).status_code, 201)
            self.assertIsNone(store.client(unused), 'unused old registrations still expire')
            self.assertIsNotNone(store.request(request, now))
            answer = await browser.post('/oauth/authorize', content=urlencode({'request': request,
                                        'decision': 'allow', 'days': '7'}), headers=FORM)
            self.assertEqual(answer.status_code, 303, answer.text)
            pending_code = parse_qs(urlsplit(answer.headers['location']).query)['code'][0]
            for client_id, value in ((pending, pending_code), (coded, code)):
                exchanged = await self.exchange(program, client_id, value, redirect=redirect)
                self.assertEqual(exchanged.status_code, 200, exchanged.text)
                renewed = await program.post('/oauth/token', headers=FORM, content=urlencode({
                    'grant_type': 'refresh_token', 'client_id': client_id,
                    'refresh_token': exchanged.json()['refresh_token']}))
                self.assertEqual(renewed.status_code, 200, renewed.text)

    async def test_batch_discards_buffered_results_when_oauth_is_revoked(self):
        from db.agent_oauth import AgentOAuthRepository
        from db.reads.research_sources import ResearchSourcesRepository
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            client_id = (await self.register(program)).json()['client_id']
            _, location = await self.allow(browser, client_id)
            tokens = (await self.exchange(program, client_id, parse_qs(urlsplit(location).query)['code'][0])).json()
        store = AgentOAuthRepository(self.site.owner.path)
        connection = store.list(self.site.owner.agent_binding(), int(time.time()))[0]['id']
        original = ResearchSourcesRepository.search
        calls = []

        def revoke_during_second(repository, *args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                store.revoke(connection)
            return original(repository, *args, **kwargs)

        messages = [rpc('tools/call', i, name='find_sources', arguments={'kind': 'folder'}) for i in (1, 2)]
        with patch.object(ResearchSourcesRepository, 'search', revoke_during_second):
            async with self.site.client(Accept=ACCEPT, Authorization='Bearer ' + tokens['access_token']) as program:
                answer = await program.post('/mcp', json=messages)
        self.assertEqual(answer.status_code, 401, answer.text)
        self.assertNotIn('3D', answer.text)
        self.assertEqual(len(calls), 2)

    async def test_code_exchange_is_atomic_and_limit_consumes_code(self):
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            client_id = (await self.register(program)).json()['client_id']
            _, location = await self.allow(browser, client_id)
            code = parse_qs(urlsplit(location).query)['code'][0]
            pair = await asyncio.gather(self.exchange(program, client_id, code),
                                        self.exchange(program, client_id, code))
            self.assertEqual(sorted(response.status_code for response in pair), [200, 400])
            self.assertEqual(len((await browser.get('/agent-connections')).json()['connections']), 1)
            _, location = await self.allow(browser, client_id)
            code = parse_qs(urlsplit(location).query)['code'][0]
            with patch('db.agent_oauth.MAX_CONNECTIONS', 1):
                limited = await self.exchange(program, client_id, code)
            self.assertEqual(limited.status_code, 503, limited.text)
            self.assertEqual((await self.exchange(program, client_id, code)).json()['error'], 'invalid_grant')

    async def test_redirect_handlers_and_ascii_are_checked_before_storing(self):
        from db.agent_oauth import AgentOAuthRepository
        store = AgentOAuthRepository(self.site.owner.path)
        store.initialize()
        bad = ['http://localhost:54321/обратный-вызов', 'http://localhost:54321/\ud800',
               'http://localhost:54321/café', 'com.example.app:/oauth']
        bad += [scheme + '://attacker.invalid/cb' for scheme in
                ('webcal', 'feed', 'web+leti', 'microsoft-edge', 'smb', 'ssh', 'sftp', 'ftps', 'irc',
                 'magnet', 'mailto', 'search-ms', 'ms-word', 'x-safari-https', 'googlechrome')]
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            for uri in bad:
                with self.subTest(uri=uri):
                    answer = await program.post('/oauth/register', content=json.dumps({'redirect_uris': [uri]}),
                                                headers={'Content-Type': 'application/json'})
                    self.assertEqual(answer.status_code, 400)
            with store.connection() as database:
                self.assertEqual(database.execute('SELECT count(*) FROM clients').fetchone()[0], 0)
            for uri in ('cursor://anysphere.cursor-retrieval/oauth/cb?x=1&y=2',
                        'http://localhost:54321/%D0%BE%D0%B1%D1%80%D0%B0%D1%82%D0%BD%D0%BE'):
                client_id = (await self.register(program, (uri,))).json()['client_id']
                page, location = await self.allow(browser, client_id, redirect=uri)
                import html
                self.assertIn('<code>' + html.escape(uri) + '</code>', page.text)
                self.assertTrue(location.startswith(uri))

    async def test_tokens_are_bound_to_this_resource(self):
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            client_id = (await self.register(program)).json()['client_id']
            _, location = await self.allow(browser, client_id)
            tokens = (await self.exchange(program, client_id, parse_qs(urlsplit(location).query)['code'][0])).json()
        self.assertIsNotNone(agent_oauth.check_access(self.site.owner, tokens['access_token'], resource=ORIGIN + '/mcp'))
        self.assertIsNone(agent_oauth.check_access(self.site.owner, tokens['access_token'], resource='http://127.0.0.1:18021/mcp'))
        self.assertIsNone(agent_oauth.check_access(self.site.owner, tokens['access_token'], resource=None))
        from db.agent_oauth import AgentOAuthRepository
        store = AgentOAuthRepository(self.site.owner.path)
        body = urlencode({'grant_type': 'refresh_token', 'client_id': client_id, 'refresh_token': tokens['refresh_token']}).encode()
        with self.assertRaisesRegex(agent_oauth.OAuthError, 'invalid_grant'):
            agent_oauth.token(store, body, self.site.owner.agent_binding(), 'http://127.0.0.1:18021', int(time.time()))
        # The refused attempt under another address did not end the connection here.
        rotated = agent_oauth.token(store, body, self.site.owner.agent_binding(), ORIGIN, int(time.time()))
        self.assertRegex(rotated['access_token'], r'^lic_at_')
        async with self.site.client(Accept=ACCEPT, Origin=ORIGIN) as browser:
            _, location = await self.allow(browser, client_id)
        code = parse_qs(urlsplit(location).query)['code'][0]
        exchange = urlencode({'grant_type': 'authorization_code', 'client_id': client_id, 'code': code,
                              'code_verifier': VERIFIER}).encode()
        with self.assertRaisesRegex(agent_oauth.OAuthError, 'invalid_grant'):
            agent_oauth.token(store, exchange, self.site.owner.agent_binding(), 'http://127.0.0.1:18021', int(time.time()))

    async def test_malformed_public_input_is_a_controlled_refusal(self):
        async with self.site.client(Accept=ACCEPT) as program, self.site.client(Origin=ORIGIN) as browser:
            for shape in ({'grant_types': [{}]}, {'response_types': [{}]}, {'grant_types': 'code'}, {'grant_types': [None]},
                          {'response_types': [1]}, {'client_name': {}}, {'token_endpoint_auth_method': 1}, {'scope': []},
                          {'redirect_uris': [{}, 'http://localhost:1/cb']}):
                body = {'redirect_uris': ['http://localhost:1/cb'], **shape}
                answer = await program.post('/oauth/register', json=body)
                if shape.get('redirect_uris'):
                    self.assertEqual(answer.status_code, 201, shape)
                    self.assertEqual(answer.json()['redirect_uris'], ['http://localhost:1/cb'])
                else:
                    self.assertEqual((answer.status_code, answer.json()['error']), (400, 'invalid_client_metadata'), shape)
            crowded = '/oauth/authorize?' + '&'.join('p%d=x' % index for index in range(30))
            page = await browser.get(crowded)
            self.assertEqual(page.status_code, 400)
            self.assertIn('text/html', page.headers['content-type'])



class ExpiryTests(OAuthCase):
    """The lifetimes docs/MCP.md promises, with the clock shifted instead of waited for.

    A code works within two minutes, an access token for one hour, renewal up to the
    connection lifetime chosen on the Allow page.
    """
    REDIRECT = 'http://localhost:54321/callback'

    def setUp(self):
        super().setUp()
        from db.agent_oauth import AgentOAuthRepository
        self.store = AgentOAuthRepository(self.site.owner.path)
        self.binding = self.site.owner.agent_binding()
        self.t0 = int(time.time())
        self.client_id = self.store.register('Expiry', [self.REDIRECT], self.t0)

    def code_at(self, now, days=7):
        request = self.store.open_request(self.client_id, self.REDIRECT, CHALLENGE, 'st', ORIGIN + '/mcp', now)
        location = agent_oauth.decide(self.store, request, True, days, self.binding, ORIGIN, now)
        return parse_qs(urlsplit(location).query)['code'][0]

    def grant(self, now, **fields):
        body = urlencode({'client_id': self.client_id, **fields}).encode()
        return agent_oauth.token(self.store, body, self.binding, ORIGIN, now)

    def exchange_at(self, code, now):
        return self.grant(now, grant_type='authorization_code', code=code, code_verifier=VERIFIER,
                          redirect_uri=self.REDIRECT)

    def refresh_at(self, refresh_token, now):
        return self.grant(now, grant_type='refresh_token', refresh_token=refresh_token)

    def valid_at(self, access_token, now):
        return agent_oauth.check_access(self.site.owner, access_token, resource=ORIGIN + '/mcp',
                                        clock=lambda: now) is not None

    def connections(self, now):
        return self.store.list(self.binding, now)

    def test_the_documented_lifetimes(self):
        from db.agent_oauth import CODE_SECONDS
        self.assertEqual((CODE_SECONDS, agent_oauth.ACCESS_SECONDS, agent_oauth.LIFETIMES), (120, 3600, (7, 30, 90, 365)))

    def test_a_code_works_within_two_minutes_only(self):
        t0 = self.t0
        late = self.code_at(t0)
        on_time = self.code_at(t0)
        self.assertRegex(self.exchange_at(on_time, t0 + 119)['access_token'], r'^lic_at_')
        with self.assertRaisesRegex(agent_oauth.OAuthError, 'invalid_grant'):
            self.exchange_at(late, t0 + 120)
        self.assertEqual(len(self.connections(t0 + 120)), 1, 'an expired code creates no connection')
        with self.assertRaisesRegex(agent_oauth.OAuthError, 'invalid_grant'):
            self.exchange_at(late, t0 + 60)  # a refused code is gone: a clock step back does not revive it

    def test_an_access_token_lasts_one_hour_and_renews(self):
        t0 = self.t0
        first = self.exchange_at(self.code_at(t0), t0)
        self.assertEqual(first['expires_in'], 3600)
        self.assertTrue(self.valid_at(first['access_token'], t0 + 3599))
        self.assertFalse(self.valid_at(first['access_token'], t0 + 3600))
        self.assertFalse(self.valid_at(first['access_token'], t0 + 86400))
        # The program renews after its access token expired; the new one again lasts an hour.
        second = self.refresh_at(first['refresh_token'], t0 + 3600)
        self.assertEqual(second['expires_in'], 3600)
        self.assertFalse(self.valid_at(first['access_token'], t0 + 3600))
        self.assertTrue(self.valid_at(second['access_token'], t0 + 7199))
        self.assertFalse(self.valid_at(second['access_token'], t0 + 7200))

    def test_renewal_ends_with_the_chosen_lifetime(self):
        t0, end = self.t0, self.t0 + 7 * 86400
        first = self.exchange_at(self.code_at(t0, days=7), t0)
        self.assertEqual([c['expires_at'] for c in self.connections(t0)], [end])
        # Renewed ten minutes before the end: the access token stops with the connection.
        last = self.refresh_at(first['refresh_token'], end - 600)
        self.assertEqual(last['expires_in'], 600)
        self.assertTrue(self.valid_at(last['access_token'], end - 1))
        self.assertFalse(self.valid_at(last['access_token'], end))
        with self.assertRaisesRegex(agent_oauth.OAuthError, 'invalid_grant'):
            self.refresh_at(last['refresh_token'], end)
        self.assertEqual(self.connections(end), [])

    async def test_the_http_routes_apply_the_same_clocks(self):
        # Issued in the past instead of waiting: the middleware and /oauth/token use the real clock.
        from db.agent_oauth import CODE_SECONDS
        now = int(time.time())
        expired_code = self.code_at(now - CODE_SECONDS)
        fresh_code = self.code_at(now - CODE_SECONDS + 30)
        old = self.exchange_at(self.code_at(now - 3600), now - 3600)
        recent = self.exchange_at(self.code_at(now - 3600 + 60), now - 3600 + 60)
        async with self.site.client(Accept=ACCEPT) as program:
            late = await self.exchange(program, self.client_id, expired_code, redirect=self.REDIRECT)
            self.assertEqual((late.status_code, late.json()['error']), (400, 'invalid_grant'))
            fresh = await self.exchange(program, self.client_id, fresh_code, redirect=self.REDIRECT)
            self.assertEqual(fresh.status_code, 200, fresh.text)
            for access, status in ((old['access_token'], 401), (recent['access_token'], 200),
                                   (fresh.json()['access_token'], 200)):
                async with self.site.client(Authorization='Bearer ' + access, Accept=ACCEPT) as agent:
                    self.assertEqual((await agent.post('/mcp', json=rpc('ping'))).status_code, status)
            renewed = await program.post('/oauth/token', headers=FORM, content=urlencode({
                'grant_type': 'refresh_token', 'client_id': self.client_id, 'refresh_token': old['refresh_token']}))
            self.assertEqual(renewed.status_code, 200, renewed.text)
            async with self.site.client(Authorization='Bearer ' + renewed.json()['access_token'], Accept=ACCEPT) as agent:
                self.assertEqual((await agent.post('/mcp', json=rpc('ping'))).status_code, 200)


if __name__ == '__main__':
    unittest.main()
