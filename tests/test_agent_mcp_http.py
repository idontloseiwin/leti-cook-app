"""/mcp and owner key management through the real auth, setup and research middleware.

Synthetic owner store and research database; no PostgreSQL, provider or network.
The app is assembled like server.py, so tool calls re-enter the full stack.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from fastapi import FastAPI
import httpx
from agent_access import TOKEN, issue
from db.agent_access import AgentAccessRepository
from db.self_hosted_auth import OwnerAuthRepository
from request_limits import RequestBodyLimitMiddleware
from research_field_policy import FIELDS, TYPES
from self_hosted_auth import SelfHostedAuthMiddleware
from self_hosted_onboarding import SetupMiddleware
from web.agents import create_agents_router
from web.research import create_research_router
import research_mcp

ORIGIN = 'http://127.0.0.1:8002'
FOLDER = 'a0000000-0000-4000-8000-000000000001'
ACCEPT = 'application/json, text/event-stream'


class Installation:
    def __init__(self, directory, mode='password'):
        now = int(time.time())
        self.owner = OwnerAuthRepository.initialize(Path(directory) / 'owner.sqlite3', 'dummy', now)
        self.owner.issue_setup('setup', now)
        if mode == 'local':
            # Separate simulated requests: completing must not use a clock older
            # than the claim's committed session, or setup stays unfinished.
            assert self.owner.claim_setup(lambda digest: True, 'claim', int(time.time())) == 'ok'
            assert self.owner.complete_setup('claim', 'local', None, lambda: None, None, int(time.time())) == 'ok'
        else:
            self.owner.register('owner', lambda _: True, lambda: 'hash', 'session', now)
        self.calls = []
        database = SimpleNamespace(config=SimpleNamespace(max_size=4))

        def rows(*args, **kwargs):
            self.calls.append(args)
            return [{'id': FOLDER, 'name': '3D'}]
        database.read_rows = rows
        api = FastAPI()
        api.include_router(create_research_router(database))
        api.include_router(create_agents_router())
        api.add_middleware(RequestBodyLimitMiddleware)
        api.add_middleware(SetupMiddleware, manager=SimpleNamespace(ready=lambda: True, workspace_pending=lambda: False))
        api.add_middleware(SelfHostedAuthMiddleware, repository=self.owner, origin=ORIGIN)
        self.app = api

    def client(self, **headers):
        return httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url=ORIGIN, headers=headers)


def rpc(method, identity=1, **params):
    return {'jsonrpc': '2.0', 'id': identity, 'method': method, 'params': params}


class McpEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.site = Installation(directory.name)
        self.meta, self.token = issue(self.site.owner, 'reader')
        preferences = {'revision': 1, 'effective': {kind: {field: 'off' for field in FIELDS} for kind in TYPES}}
        for active in (patch.dict(os.environ, {'LIC_PUBLIC_ORIGIN': ORIGIN}),
                       patch('web.research.ResearchPreferencesRepository.read', return_value=preferences)):
            active.start()
            self.addCleanup(active.stop)

    def agent(self):
        # A real MCP client: Bearer key, both media types, no Origin header.
        return self.site.client(Authorization='Bearer ' + self.token, Accept=ACCEPT)

    async def post(self, client, message, **headers):
        return await client.post('/mcp', content=json.dumps(message), headers={'Content-Type': 'application/json', **headers})

    async def test_lifecycle_tools_and_call_through_the_research_routes(self):
        async with self.agent() as client:
            init = await self.post(client, rpc('initialize', protocolVersion='2025-06-18', capabilities={},
                                               clientInfo={'name': 'test', 'version': '1'}))
            self.assertEqual(init.status_code, 200, init.text)
            result = init.json()['result']
            self.assertEqual(result['protocolVersion'], '2025-06-18')
            self.assertEqual(result['capabilities'], {'tools': {'listChanged': False}})
            self.assertIn('Shortest path', result['instructions'])
            self.assertNotIn('mcp-session-id', init.headers)
            self.assertEqual(init.headers['cache-control'], 'no-store')
            newer = await self.post(client, rpc('initialize', protocolVersion='2099-01-01'))
            self.assertEqual(newer.json()['result']['protocolVersion'], research_mcp.PROTOCOL_VERSIONS[0])
            notified = await self.post(client, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                       **{'MCP-Protocol-Version': '2025-06-18'})
            self.assertEqual((notified.status_code, notified.content), (202, b''))
            tools = (await self.post(client, rpc('tools/list', 2))).json()['result']['tools']
            self.assertEqual([t['name'] for t in tools],
                             ['find_sources', 'describe_catalogue', 'query_catalogue', 'calculate', 'run_canvas'])
            for tool in tools:
                self.assertTrue(tool['annotations']['readOnlyHint'])
                self.assertFalse(tool['inputSchema']['additionalProperties'])
            self.assertEqual((await self.post(client, rpc('ping', 3))).json()['result'], {})
            found = (await self.post(client, rpc('tools/call', 4, name='find_sources',
                                                  arguments={'kind': 'folder', 'search': '3D'}))).json()['result']
            self.assertFalse(found['isError'], found)
            self.assertEqual(found['structuredContent']['items'][0]['id'], FOLDER)
            self.assertEqual(json.loads(found['content'][0]['text']), found['structuredContent'])
            self.assertEqual(len(self.site.calls), 1)
            described = (await self.post(client, rpc('tools/call', 5, name='describe_catalogue',
                                                      arguments={'section': 'canvas'}))).json()['result']
            self.assertFalse(described['isError'], described)

    async def test_arguments_methods_and_messages_are_strict(self):
        async with self.agent() as client:
            for arguments in ({'kind': 'playlist'}, {'kind': 'folder', 'limit': '20'}, {'kind': 'folder', 'extra': 1},
                              {}, {'kind': 'folder', 'limit': True}):
                answer = (await self.post(client, rpc('tools/call', name='find_sources', arguments=arguments))).json()
                self.assertEqual(answer['result'], {'content': [{'type': 'text', 'text': 'invalid_arguments'}], 'isError': True})
            calculated = (await self.post(client, rpc('tools/call', name='query_catalogue',
                arguments={'scope': {'kind': 'library'}, 'video_type': 'short', 'options': {'consistency': 'frozen'}}))).json()
            self.assertEqual(calculated['result']['content'][0]['text'], 'invalid_query_options')
            unknown = (await self.post(client, rpc('tools/call', name='write_anything'))).json()
            self.assertEqual(unknown['error']['code'], -32602)
            self.assertEqual((await self.post(client, rpc('resources/list'))).json()['error']['code'], -32601)
            # Batches exist only in MCP 2025-03-26, the version assumed without a header.
            batch = await self.post(client, [rpc('ping')], **{'MCP-Protocol-Version': '2025-06-18'})
            self.assertEqual((batch.status_code, batch.json()['error']['code']), (400, -32600))
            march = await self.post(client, [rpc('ping', 1), {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                             rpc('initialize', 3), rpc('ping', 4)], **{'MCP-Protocol-Version': '2025-03-26'})
            self.assertEqual(march.status_code, 200, march.text)
            self.assertEqual([(a['id'], 'result' in a) for a in march.json()], [(1, True), (3, False), (4, True)])
            self.assertEqual(march.json()[1]['error']['message'], 'initialize_cannot_be_batched')
            unversioned = await self.post(client, [rpc('ping', 5)])
            self.assertEqual(unversioned.json(), [{'jsonrpc': '2.0', 'id': 5, 'result': {}}])
            notices = await self.post(client, [{'jsonrpc': '2.0', 'method': 'notifications/initialized'}])
            self.assertEqual((notices.status_code, notices.content), (202, b''))
            for bad in ([], [rpc('ping')] * 21):
                self.assertEqual((await self.post(client, bad)).status_code, 400)
            broken = await client.post('/mcp', content=b'{', headers={'Content-Type': 'application/json'})
            self.assertEqual((broken.status_code, broken.json()['error']['code']), (400, -32700))
            version = await self.post(client, rpc('ping'), **{'MCP-Protocol-Version': '2024-01-01'})
            self.assertEqual(version.status_code, 400)
            text = await client.post('/mcp', content=json.dumps(rpc('ping')), headers={'Content-Type': 'text/plain'})
            self.assertEqual(text.status_code, 415)
            html = await self.post(client, rpc('ping'), Accept='text/html')
            self.assertEqual(html.status_code, 406)
            for method in ('GET', 'DELETE'):
                answer = await client.request(method, '/mcp')
                self.assertEqual((answer.status_code, answer.headers['allow']), (405, 'POST'))
            large = await self.post(client, rpc('tools/call', name='run_canvas', arguments={'definition': {'x': 'a' * 140_000}}))
            self.assertEqual(large.status_code, 413)

    async def test_key_is_required_and_never_falls_back_to_owner_access(self):
        for mode in ('password', 'local'):
            with tempfile.TemporaryDirectory() as directory:
                site = Installation(directory, mode)
                _, token = issue(site.owner, 'reader')
                async with site.client(Accept=ACCEPT) as client:
                    for headers in ({}, {'Authorization': 'Bearer lic_ro_' + 'A' * 43}, {'Authorization': 'Basic eDp5'},
                                    {'Origin': ORIGIN}):
                        answer = await client.post('/mcp', content=json.dumps(rpc('ping')),
                                                   headers={'Content-Type': 'application/json', **headers})
                        self.assertEqual(answer.status_code, 401, (mode, headers))
                        self.assertTrue(answer.headers['www-authenticate'].startswith('Bearer resource_metadata="'))
                    good = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
                    foreign = await client.post('/mcp', content=json.dumps(rpc('ping')), headers={**good, 'Origin': 'http://evil.test'})
                    self.assertEqual(foreign.json()['code'], 'cross_origin_denied')
                    site_header = await client.post('/mcp', content=json.dumps(rpc('ping')),
                                                    headers={**good, 'Sec-Fetch-Site': 'cross-site'})
                    self.assertEqual(site_header.status_code, 403)
                    host = await client.post('http://localhost:8002/mcp', content=json.dumps(rpc('ping')), headers=good)
                    self.assertEqual(host.json()['code'], 'invalid_host')
                    self.assertEqual((await client.post('/mcp', content=json.dumps(rpc('ping')), headers=good)).status_code, 200)

    async def test_revocation_and_key_scope(self):
        store = AgentAccessRepository(self.site.owner.path)
        async with self.agent() as client:
            for method, path in (('GET', '/agent-keys'), ('POST', '/agent-keys'), ('DELETE', '/agent-keys/' + self.meta['id'])):
                answer = await client.request(method, path, json={}, headers={'Origin': ORIGIN})
                self.assertEqual(answer.json()['code'], 'agent_scope_denied', path)
            store.revoke(self.meta['id'])
            self.assertEqual((await self.post(client, rpc('ping'))).status_code, 401)
        self.assertEqual(self.site.calls, [])

    async def test_batch_discards_buffered_results_when_key_is_revoked(self):
        from db.reads.research_sources import ResearchSourcesRepository
        original = ResearchSourcesRepository.search
        calls = []

        def revoke_during_second(repository, *args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                AgentAccessRepository(self.site.owner.path).revoke(self.meta['id'])
            return original(repository, *args, **kwargs)

        messages = [rpc('tools/call', i, name='find_sources', arguments={'kind': 'folder'}) for i in (1, 2, 3)]
        with patch.object(ResearchSourcesRepository, 'search', revoke_during_second):
            async with self.agent() as client:
                answer = await self.post(client, messages)
        self.assertEqual(answer.status_code, 401, answer.text)
        self.assertNotIn('3D', answer.text)
        self.assertIn('resource_metadata=', answer.headers['www-authenticate'])
        self.assertEqual(len(calls), 2, 'the third read must not start')

    async def test_batch_has_a_total_response_budget(self):
        payload = {'rows': [{'description': 'x' * 5000} for _ in range(50)]}
        calls = []

        async def large(*args, **kwargs):
            calls.append(True)
            return payload

        messages = [rpc('tools/call', i, name='find_sources', arguments={'kind': 'folder'}) for i in range(20)]
        with patch.object(research_mcp.ResearchCaller, 'request', large):
            async with self.agent() as client:
                answer = await self.post(client, messages)
        self.assertEqual(answer.status_code, 200, answer.text[:200])
        self.assertLess(len(answer.content), research_mcp.MAX_BATCH_RESPONSE)
        self.assertEqual([item['id'] for item in answer.json()], list(range(20)))
        self.assertTrue(all(item['error']['message'] == 'batch_response_too_large_reduce_calls_or_columns'
                            for item in answer.json()))
        self.assertLess(len(calls), 20, 'stop work at the byte budget')

    async def test_batch_deadline_discards_earlier_data_and_stops_work(self):
        calls = []
        cancelled = []

        async def slow(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                try:
                    await asyncio.sleep(5)
                finally:
                    cancelled.append(True)
            return {'private': 'buffered-first-result'}

        messages = [rpc('tools/call', i, name='find_sources', arguments={'kind': 'folder'}) for i in (1, 2, 3)]
        with patch.object(research_mcp.ResearchCaller, 'request', slow), patch('web.agents.BATCH_SECONDS', .1):
            async with self.agent() as client:
                answer = await self.post(client, messages)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertNotIn('buffered-first-result', answer.text)
        self.assertEqual([item['id'] for item in answer.json()], [1, 2, 3])
        self.assertTrue(all(item['error']['message'] == 'batch_time_budget_exceeded_reduce_calls'
                            for item in answer.json()))
        self.assertEqual((len(calls), len(cancelled)), (2, 1))


class OwnerKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_downloads_client_setup_without_credentials_or_new_grants(self):
        from io import BytesIO
        import tomllib
        from zipfile import ZipFile

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LIC_PUBLIC_ORIGIN': ORIGIN,
                'LIC_AUTH_DATABASE': str(Path(directory) / 'owner.sqlite3')}):
            site = Installation(directory, 'local')
            async with site.client(Origin=ORIGIN) as owner:
                for client, suffix in [('claude-desktop', '.mcpb'), ('codex', '.zip'), ('claude-code', '.zip')]:
                    reply = await owner.get('/agent-setup/' + client)
                    self.assertEqual(reply.status_code, 200)
                    self.assertTrue(reply.headers['content-disposition'].endswith(suffix + '"'))
                    with ZipFile(BytesIO(reply.content)) as archive:
                        if client == 'claude-desktop':
                            self.assertEqual(set(archive.namelist()), {'manifest.json', 'server/bridge.cjs'})
                            manifest = json.loads(archive.read('manifest.json'))
                            self.assertTrue(manifest['user_config']['agent_key']['sensitive'])
                            env = manifest['server']['mcp_config']['env']
                            self.assertEqual(env, {'LETI_MCP_URL': ORIGIN + '/mcp',
                                                   'LETI_AGENT_KEY': '${user_config.agent_key}'})
                            self.assertEqual(archive.read('server/bridge.cjs'),
                                             (ROOT / 'application' / 'claude_desktop_bridge.cjs').read_bytes())
                        else:
                            self.assertTrue(archive.getinfo('Leti Cook/Open on macOS.command').external_attr >> 16 & 0o100)
                            if client == 'claude-code':
                                settings = json.loads(archive.read('Leti Cook/.mcp.json'))
                                self.assertEqual(settings['mcpServers'], {'leti-cook': {'type': 'http', 'url': ORIGIN + '/mcp'}})
                            else:
                                settings = tomllib.loads(archive.read('Leti Cook/.codex/config.toml').decode())
                                self.assertEqual(settings['mcp_servers'], {'leti-cook': {'url': ORIGIN + '/mcp'}})
                self.assertEqual((await owner.get('/agent-setup/unknown')).status_code, 404)
                with patch.dict(os.environ, {'LIC_PUBLIC_ORIGIN': 'https://outside.example'}):
                    self.assertEqual((await owner.get('/agent-setup/claude-desktop')).status_code, 409)
                self.assertEqual((await owner.get('/agent-keys')).json()['keys'], [])
                self.assertEqual((await owner.get('/agent-connections')).json()['connections'], [])
            _, key = issue(site.owner, 'reader')
            async with site.client(Authorization='Bearer ' + key) as agent:
                self.assertIn((await agent.get('/agent-setup/claude-desktop')).status_code, (401, 403))


    async def test_owner_creates_lists_uses_and_revokes_a_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LIC_PUBLIC_ORIGIN': ORIGIN,
                'LIC_AUTH_DATABASE': str(Path(directory) / 'owner.sqlite3')}):
            site = Installation(directory, 'local')
            async with site.client(Origin=ORIGIN) as owner:
                empty = (await owner.get('/agent-keys')).json()
                self.assertEqual(empty, {'keys': [], 'scope': 'research.read', 'mcp_url': ORIGIN + '/mcp', 'max_keys': 32})
                created = await owner.post('/agent-keys', json={'name': ' Claude Code ', 'days': 7})
                self.assertEqual(created.status_code, 200, created.text)
                self.assertEqual(created.headers['cache-control'], 'no-store')
                body = created.json()
                self.assertTrue(TOKEN.fullmatch(body['secret']))
                self.assertEqual(body['key']['name'], 'Claude Code')
                self.assertEqual(body['key']['expires_at'] - body['key']['created_at'], 7 * 86400)
                listed = (await owner.get('/agent-keys')).json()
                self.assertEqual([key['id'] for key in listed['keys']], [body['key']['id']])
                self.assertNotIn(body['secret'], json.dumps(listed))
                async with site.client(Authorization='Bearer ' + body['secret'], Accept=ACCEPT) as agent:
                    ping = await agent.post('/mcp', content=json.dumps(rpc('ping')), headers={'Content-Type': 'application/json'})
                    self.assertEqual(ping.status_code, 200)
                    revoked = await owner.delete('/agent-keys/' + body['key']['id'])
                    self.assertEqual(revoked.json(), {'revoked': True, 'id': body['key']['id']})
                    ping = await agent.post('/mcp', content=json.dumps(rpc('ping')), headers={'Content-Type': 'application/json'})
                    self.assertEqual(ping.status_code, 401)
                for invalid in ({'name': '', 'days': 30}, {'name': 'x', 'days': 0}, {'name': 'x', 'days': 366},
                                {'name': 'x', 'days': True}, {'name': 'x', 'days': '30'}, {'name': 'x', 'scope': 'all'},
                                {'name': 'x' * 81}, ['x'], {'name': 'a\nb'}):
                    answer = await owner.post('/agent-keys', json=invalid)
                    self.assertEqual((answer.status_code, answer.json()['code']), (400, 'invalid_agent_key_options'), invalid)
                self.assertEqual((await owner.delete('/agent-keys/not-a-key')).json()['code'], 'invalid_agent_key_id')
                self.assertEqual((await owner.delete('/agent-keys/' + body['key']['id'])).json()['revoked'], False)
            async with site.client() as no_origin:
                answer = await no_origin.post('/agent-keys', json={'name': 'x'})
                self.assertEqual(answer.json()['code'], 'same_origin_required')


if __name__ == '__main__':
    unittest.main()
