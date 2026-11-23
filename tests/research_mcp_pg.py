"""Small MCP-to-PostgreSQL acceptance, only a newly created disposable PG17 cluster.

The preview app is served over real HTTP; the official MCP SDK (tests/mcp_reference)
is the reference client for /mcp, and also drives the stdio bridge. Uses the shared
native fixture and real query/router/auth implementations. This entrypoint is
explicit (not discovered by the offline suite). No production DSN, provider calls
or saved user changes.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from research_query_pg import NativeQueryTests, FIXTURE
from research_query_preview import create_preview_app
from agent_access import issue
from db.self_hosted_auth import OwnerAuthRepository
from db.agent_access import AgentAccessRepository
import httpx
import httpx2
import uvicorn
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
import re
from urllib.parse import parse_qs, urlsplit
from db.reads.research_query import ResearchQueryRepository
from db.base.postgres import PostgresDatabase
from collection_schedule import CollectionSchedules


class ToolError(Exception):
    pass


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class Agent:
    """One SDK session to /mcp with a given key, opened per key."""

    def __init__(self, url, key):
        self.url, self.key, self.calls = url, key, 0

    async def __call__(self, name, arguments):
        self.calls += 1
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.key}, timeout=40) as http:
            async with streamable_http_client(self.url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    answer = await session.call_tool(name, arguments)
        if answer.is_error:
            raise ToolError(answer.content[0].text)
        return answer.structured_content


class Memory:
    """Token storage of the SDK's OAuth client for one test run."""
    tokens = client = None

    async def get_tokens(self): return self.tokens
    async def set_tokens(self, tokens): self.tokens = tokens
    async def get_client_info(self): return self.client
    async def set_client_info(self, info): self.client = info


async def signed_in_answer(origin, arguments):
    """The SDK's own OAuth client: discovery, registration, PKCE; the owner clicks Allow."""
    answer = {}

    async def owner_allows(url):
        async with httpx.AsyncClient(headers={'Origin': origin}, timeout=20) as browser:
            page = await browser.get(url)
            assert page.status_code == 200, page.text
            request = re.search(r'name="request" value="([0-9a-f]{32})"', page.text).group(1)
            allowed = await browser.post(origin + '/oauth/authorize', content='request=%s&decision=allow&days=7' % request,
                                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
            assert allowed.status_code == 303, allowed.text
            query = parse_qs(urlsplit(allowed.headers['location']).query)
            answer.update(code=query['code'][0], state=query.get('state', [None])[0], iss=query['iss'][0])

    async def callback():
        return AuthorizationCodeResult(code=answer['code'], state=answer['state'], iss=answer['iss'])
    provider = OAuthClientProvider(origin + '/mcp', OAuthClientMetadata(client_name='SDK reference client',
        redirect_uris=['http://localhost:3118/callback'], grant_types=['authorization_code', 'refresh_token'],
        response_types=['code'], token_endpoint_auth_method='none'), Memory(), owner_allows, callback)
    async with httpx2.AsyncClient(auth=provider, timeout=40) as http:
        async with streamable_http_client(origin + '/mcp', http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool('query_catalogue', arguments)
    assert answer, 'the owner Allow page was not used'
    assert not result.is_error, result.content
    return result.structured_content


async def probe(directory, database, origin):
    state = Path(directory)
    app = create_preview_app(database, state/'owner.sqlite3', origin, FIXTURE['folders'],
                             projection=SimpleNamespace(initial_fill_complete=True), start_projection=False)
    owner = OwnerAuthRepository(state/'owner.sqlite3')
    meta, key = issue(owner, 'synthetic native acceptance')
    token_file = state/'key'; token_file.write_text(key); token_file.chmod(0o600)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(origin.rsplit(':', 1)[1]),
                                           log_level='warning', lifespan='off'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, 'server did not start'
        await asyncio.sleep(.05)
    try:
        await accept(app, owner, meta, key, token_file, origin)
    finally:
        server.should_exit = True
        thread.join(10)


async def accept(app, owner, meta, key, token_file, origin):
    agent = Agent(origin + '/mcp', key)
    call = agent
    async with httpx.AsyncClient(base_url=origin, headers={'Origin':origin}, timeout=40) as browser:
        sources = await call('find_sources', {'kind':'folder','search':'Fixture'})
        assert sources['items'] and all(item['scope']['kind']=='folder' for item in sources['items'])
        for kind in ('long','short','stream'):
            answer = await call('query_catalogue', {'scope':{'kind':'folder','id':FIXTURE['folders'][0]},
                        'video_type':kind,'order_by':[{'field':'video.views','direction':'desc'}], 'page_size':2})
            expected = await browser.post('/research/queries/execute',json=answer['query'])
            assert expected.status_code == 200, expected.text
            assert answer['result']['rows'] == expected.json()['rows']
            assert answer['result']['coverage'] == expected.json()['coverage']
        channels = await call('find_sources', {'kind':'channel','search':'Synthetic'})
        assert channels['rows']
        for section in ('fields','operators','query','operations','canvas'):
            info = await call('describe_catalogue',{'section':section})
            assert info
        info = await call('describe_catalogue',{'section':'canvas'})
        definition = info['example']
        assert (await call('run_canvas',{'definition':definition}))['valid']
        result = await call('run_canvas',{'definition':definition,'validate_only':False})
        assert result['context']['valid_before']
        answer = await call('query_catalogue', {'scope':{'kind':'library'},'video_type':'long','page_size':1})
        cursor = answer['result']['page']['next_cursor']
        assert cursor, 'fixture must cover cross-key continuation'
        second_meta, second_key = issue(owner, 'second synthetic reader')
        try:
            await Agent(origin + '/mcp', second_key)('query_catalogue', {'scope':{'kind':'library'},'video_type':'long','page_size':1,'cursor':cursor})
        except ToolError as error:
            assert 'cursor_context_changed' in str(error)
        else: raise AssertionError('cursor crossed credential boundary')
        # The stdio bridge reaches the same endpoint and returns the same rows.
        params = StdioServerParameters(command=sys.executable, args=['-I', '-B', str(ROOT/'integrations/mcp/server.py'),
            '--base-url', origin, '--token-file', str(token_file)])
        with tempfile.TemporaryFile(mode='w+') as stderr:
            async with asyncio.timeout(40):
                async with stdio_client(params, errlog=stderr) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        initialized = await session.initialize()
                        assert 'Shortest path' in initialized.instructions
                        assert len((await session.list_tools()).tools) == 5
                        bridged = await session.call_tool('query_catalogue', {'scope':{'kind':'library'},'video_type':'long','page_size':1})
                        assert not bridged.is_error and bridged.structured_content['result']['rows'] == answer['result']['rows']
            stderr.seek(0)
            assert key not in stderr.read()
        # Sign-in with Allow returns the same rows as the key; revoking it ends access.
        signed = await signed_in_answer(origin, {'scope':{'kind':'library'},'video_type':'long','page_size':1})
        assert signed['result']['rows'] == answer['result']['rows']
        connections = (await browser.get('/agent-connections')).json()['connections']
        assert [c['name'] for c in connections] == ['SDK reference client'], connections
        revoked = await browser.delete('/agent-connections/' + connections[0]['id'])
        assert revoked.json()['revoked'] is True
        headers = {'Authorization':'Bearer '+key}
        # A token used directly over HTTP must not consume the owner's frozen slots.
        frozen_query = {**answer['query'], 'consistency':'frozen'}
        fixed = await browser.post('/research/queries/execute', json=frozen_query)
        assert fixed.status_code == 200, fixed.text
        for _ in range(9):
            refused = await browser.post('/research/queries/execute', json=frozen_query, headers=headers)
            assert refused.status_code == 403 and refused.json()['code'] == 'policy_denied', refused.text
        next_query = deepcopy(frozen_query)
        next_query['page']['after'] = fixed.json()['page']['next_cursor']
        assert next_query['page']['after']
        continued = await browser.post('/research/queries/execute', json=next_query)
        assert continued.status_code == 200, continued.text
        for route in ('validate', 'execute'):
            graph = deepcopy(definition)
            graph['run'] = {'consistency':'frozen','frozen_ref':fixed.json()['context']['frozen_ref']}
            refused = await browser.post('/research/graphs/'+route,json=graph,headers=headers)
            assert refused.status_code == 403, refused.text
        operation = {'version':1,'operation':'video.static_details.v1','query':answer['query'],
                     'parameters':{'video_id':'a0000000001','population':{
                         'kind':'frozen','frozen_ref':fixed.json()['context']['frozen_ref']}}}
        refused = await browser.post('/research/operations/execute', json=operation, headers=headers)
        assert refused.status_code == 403, refused.text
        assert (await browser.get('/research/capabilities',headers=headers)).json()['consistency_modes'] == ['current']
        # Hold one real agent read, queue a different key, and let the owner finish.
        started, release = threading.Event(), threading.Event()
        original_read = ResearchQueryRepository.read_page
        entered = []
        def held_read(repo, plan, continuation, **kwargs):
            entered.append(plan.context_fingerprint)
            if len(entered) == 1:
                started.set()
                assert release.wait(4), 'probe failed to release agent'
            return original_read(repo, plan, continuation, **kwargs)
        with patch.object(ResearchQueryRepository, 'read_page', held_read):
            first = asyncio.create_task(browser.post('/research/queries/execute',json=answer['query'],headers=headers))
            second = None
            try:
                assert await asyncio.to_thread(started.wait, 2)
                second = asyncio.create_task(browser.post('/research/queries/execute',json=answer['query'],
                                               headers={'Authorization':'Bearer '+second_key}))
                owner_read = await asyncio.wait_for(browser.post('/research/queries/execute',json=answer['query']),2)
                assert owner_read.status_code == 200, owner_read.text
                assert not first.done() and not second.done() and len(entered) == 2
            finally:
                release.set()
                replies = await asyncio.gather(first, *([second] if second else []))
                assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
        AgentAccessRepository(owner.path).revoke(second_meta['id'])
        # A query copied from page two calculates the same full population.
        page_two = deepcopy(answer['query'])
        page_two['page']['after'] = cursor
        aggregate = await call('calculate',{'query':page_two,'operation':'video.summary.v1'})
        expected = await browser.post('/research/operations/execute',json={'version':1,'operation':'video.summary.v1',
                                                                         'query':answer['query'],'parameters':{}})
        assert expected.status_code == 200, expected.text
        assert aggregate['result'] == expected.json()['result']
        fixture = NativeQueryTests()
        with fixture.label([0]):
            labels = await call('find_sources',{'kind':'label','search':'Fixture'})
            assert labels['items']
        with fixture.collection(['a0000000001']):
            collections = await call('find_sources',{'kind':'collection','search':'Fixture'})
            assert collections['items']
            saved = await call('query_catalogue',{'scope':collections['items'][0]['scope'],'video_type':'long'})
            assert [r['id'] for r in saved['result']['rows']] == ['a0000000001']
        # March clients can batch through HTTP and through the shipped stdio bridge.
        march = {'Authorization': 'Bearer ' + key, 'Accept': 'application/json, text/event-stream',
                 'MCP-Protocol-Version': '2025-03-26'}
        notice = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        batch = [{'jsonrpc': '2.0', 'id': 101, 'method': 'tools/call',
                  'params': {'name': 'find_sources', 'arguments': {'kind': 'folder', 'search': 'Fixture'}}},
                 notice, {'jsonrpc': '2.0', 'id': 102, 'method': 'ping'},
                 {'jsonrpc': '2.0', 'id': 103, 'method': 'unknown'}]
        direct = await browser.post('/mcp', headers=march, json=batch)
        assert direct.status_code == 200, direct.text
        assert [row['id'] for row in direct.json()] == [101, 102, 103]
        assert direct.json()[0]['result']['structuredContent']['items']
        lines = [{'jsonrpc': '2.0', 'id': 100, 'method': 'initialize',
                  'params': {'protocolVersion': '2025-03-26'}}, batch, [notice]]
        process = await asyncio.to_thread(subprocess.run,
            [sys.executable, '-I', '-B', str(ROOT/'integrations/mcp/server.py'), '--base-url', origin,
             '--token-file', str(token_file)], input='\n'.join(map(json.dumps, lines)), text=True,
            capture_output=True, timeout=45)
        assert process.returncode == 0, process.stderr
        outputs = [json.loads(line) for line in process.stdout.splitlines()]
        assert len(outputs) == 2 and outputs[0]['result']['protocolVersion'] == '2025-03-26'
        assert outputs[1] == direct.json(), 'stdio batch must preserve IDs, errors and stored data'
        AgentAccessRepository(owner.path).revoke(meta['id'])
        async with httpx.AsyncClient(base_url=origin, timeout=20) as program:
            revoked = await program.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'ping'},
                                         headers={'Authorization':'Bearer '+key, 'Accept':'application/json, text/event-stream'})
            assert revoked.status_code == 401 and revoked.headers['www-authenticate'].startswith('Bearer resource_metadata='), revoked.text
    print(json.dumps({'status':'passed','mcp_calls':agent.calls,'transport':'streamable HTTP (SDK), stdio bridge, SDK OAuth sign-in','parity':'all types, aggregate from page two, sources, canvas, revocation',
                      'regressions':'owner frozen list survives 9 agent refusals; two keys share one lane; owner reads concurrently; March batches match over HTTP and real stdio'}))


def main():
    NativeQueryTests.setUpClass()
    # Match the installation's normal pool; the generic SQL fixture uses one
    # connection and therefore deliberately admits only one read in total.
    probe_db = None
    try:
        probe_db = PostgresDatabase(replace(NativeQueryTests.db.config, max_size=4),
            cadence_snapshot=CollectionSchedules().cadence_snapshot(), admission=lambda _:None).open()
        NativeQueryTests.admin.execute("UPDATE public.channels SET title='Synthetic channel',subscribers=42,views=10000,"
            "video_count=10,channel_metadata_status='ok',channel_metadata_checked_at=clock_timestamp()-interval '1 hour'")
        NativeQueryTests.refresh()
        with tempfile.TemporaryDirectory(prefix='lic-mcp-native-') as directory, patch.dict(os.environ,
            LIC_DEPLOYMENT_PROFILE='self-hosted',
            CHANNEL_BASELINE_ANALYTICS_ENABLED='true',CHANNEL_DERIVED_INSIGHTS_ENABLED='true',
            CHANNEL_LAUNCH_ANALYTICS_ENABLED='false',TELEMETRY_ENABLED='false'), \
                patch('hot_runtime.require_hot_runtime_ready', lambda **kw: None):
            origin = 'http://127.0.0.1:%d' % free_port()
            with patch.dict(os.environ, LIC_PUBLIC_ORIGIN=origin, LIC_AUTH_DATABASE=str(Path(directory)/'owner.sqlite3')):
                asyncio.run(probe(directory, probe_db, origin))
    finally:
        if probe_db is not None:
            probe_db.close()
        NativeQueryTests.tearDownClass()


if __name__ == '__main__': main()
