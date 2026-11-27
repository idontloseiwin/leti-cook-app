"""The stdio bridge against a synthetic /mcp server. Standard library only.

No installation, YouTube or model calls. CI runs it in an empty virtual
environment: the bridge must need no packages.
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MCP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_ROOT))
from client import ApiError, MAX_RESPONSE, Transport, origin, token_from_file
import server as bridge_module

TOKEN = 'lic_ro_' + 'a' * 43  # Synthetic, never a credential for any installation.


class Server:
    """A loopback HTTP endpoint that records requests and answers from a script."""

    def __init__(self, answer):
        self.requests, self.answer = [], answer
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                headers = {name.lower(): value for name, value in self.headers.items()}
                outer.requests.append((self.path, headers, json.loads(body)))
                status, headers, raw = outer.answer(json.loads(body), headers)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}'

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(2)


def json_answer(value, status=200):
    return status, {'Content-Type': 'application/json'}, json.dumps(value).encode()


def echo(message, headers):
    if headers.get('authorization') != 'Bearer ' + TOKEN:
        return json_answer({'code': 'agent_auth_required'}, 401)
    if 'id' not in message:
        return 202, {}, b''
    if message['method'] == 'initialize':
        return json_answer({'jsonrpc': '2.0', 'id': message['id'], 'result': {'protocolVersion': '2025-06-18',
                            'capabilities': {'tools': {}}, 'serverInfo': {'name': 'x', 'version': '1'}}})
    return json_answer({'jsonrpc': '2.0', 'id': message['id'],
                        'result': {'seen_version': headers.get('mcp-protocol-version'), 'text': 'Ünïcode'}})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.key = Path(temporary.name) / 'key'
        self.key.write_text(TOKEN)
        self.key.chmod(0o600)

    def serve(self, answer=echo):
        server = Server(answer)
        self.addCleanup(server.close)
        return server

    def run_bridge(self, server, lines):
        written = []
        bridge_module.bridge(Transport(server.base, self.key), lines, written.append)
        return written

    def test_origin_and_key_file_rules(self):
        for url in ('http://example.com:8002', 'http://127.0.0.1', 'http://127.0.0.1:8002/mcp',
                    'http://user:x@127.0.0.1:8002', 'ftp://127.0.0.1:8002', 'http://127.0.0.1:8002?x=1'):
            with self.assertRaises(ApiError):
                origin(url)
        self.assertEqual(origin('http://[::1]:8002/'), 'http://[::1]:8002')
        self.assertEqual(token_from_file(self.key), TOKEN)
        self.key.write_text('not a key')
        with self.assertRaisesRegex(ApiError, 'invalid_credential_file'):
            token_from_file(self.key)
        if os.name != 'nt':
            self.key.write_text(TOKEN)
            self.key.chmod(0o644)
            with self.assertRaisesRegex(ApiError, 'credential_file_must_be_private'):
                token_from_file(self.key)

    def test_messages_headers_version_and_notifications(self):
        server = self.serve()
        written = self.run_bridge(server, [
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}) + '\n',
            '\n',
            json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n',
            json.dumps({'jsonrpc': '2.0', 'id': 'two', 'method': 'tools/list'}) + '\n',
            '{broken\n',
        ])
        self.assertEqual([item.get('id') for item in written], [1, 'two', None])
        self.assertEqual(written[1]['result'], {'seen_version': '2025-06-18', 'text': 'Ünïcode'})
        self.assertEqual(written[2]['error']['code'], -32700)
        paths = {path for path, _, _ in server.requests}
        self.assertEqual(paths, {'/mcp'})
        first = server.requests[0][1]
        self.assertEqual(first['authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(first['accept'], 'application/json, text/event-stream')
        self.assertNotIn('mcp-protocol-version', first)
        self.assertNotIn('origin', first)
        self.assertEqual(len(server.requests), 3)  # The broken line never left the bridge.

    @unittest.skipUnless(shutil.which('node'), 'Claude Desktop bridge needs Node')
    def test_packaged_desktop_bridge(self):
        import zipfile
        from io import BytesIO
        application = MCP_ROOT.parents[1] / 'application'
        sys.path.insert(0, str(application))
        self.addCleanup(sys.path.remove, str(application))
        from agent_setup import download

        def answer(message, headers):
            if message.get('id') == 4:
                return json_answer({'code': 'agent_key_invalid_expired_or_revoked'}, 401)
            if message.get('method') == 'tools/list':
                return json_answer({'jsonrpc': '2.0', 'id': 3, 'result': {'tools': [{'name': 'find_sources'}]}})
            return echo(message, headers)

        endpoint = self.serve(answer)
        _, bundle = download('claude-desktop', endpoint.base)
        with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(BytesIO(bundle)) as archive:
            script = Path(archive.extract('server/bridge.cjs', tmp))
            env = json.loads(archive.read('manifest.json'))['server']['mcp_config']['env']
            messages = [{'jsonrpc': '2.0', 'method': 'initialize', 'id': 1, 'params': {'protocolVersion': '2025-03-26'}},
                        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                        {'jsonrpc': '2.0', 'method': 'tools/list', 'id': 3}, {'jsonrpc': '2.0', 'method': 'ping', 'id': 4}]
            run = subprocess.run([shutil.which('node'), str(script)], env={**os.environ, **env, 'LETI_AGENT_KEY': TOKEN},
                                 input='\n'.join(map(json.dumps, messages)).encode(), capture_output=True, timeout=12)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn(TOKEN.encode(), run.stderr + run.stdout)
        replies = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual([reply['id'] for reply in replies], [1, 3, 4])
        self.assertEqual(replies[1]['result']['tools'], [{'name': 'find_sources'}])
        self.assertIn('revoked', replies[2]['error']['message'])
        self.assertTrue(all(path == '/mcp' and headers['authorization'] == 'Bearer ' + TOKEN
                            for path, headers, _ in endpoint.requests))
        self.assertEqual(endpoint.requests[2][1]['mcp-protocol-version'], replies[0]['result']['protocolVersion'])

    def test_errors_are_closed_and_keep_the_request_id(self):
        cases = [
            (json_answer({'code': 'agent_auth_required'}, 401), 'agent_key_invalid_expired_or_revoked'),
            (json_answer({'code': 'invalid_host'}, 400), 'base_url_must_match_installation_origin'),
            ((307, {'Location': 'http://evil.test/'}, b''), 'base_url_must_match_installation_origin'),
            ((500, {'Content-Type': 'text/html'}, b'<html>Traceback secret</html>'), 'request_failed'),
            ((200, {'Content-Type': 'application/json'}, b'x' * (MAX_RESPONSE + 10)), 'response_too_large_reduce_page_or_columns'),
        ]
        for answer, code in cases:
            with self.subTest(code=code):
                server = self.serve(lambda message, headers, answer=answer: answer)
                written = self.run_bridge(server, [json.dumps({'jsonrpc': '2.0', 'id': 7, 'method': 'ping'})])
                self.assertEqual(written, [{'jsonrpc': '2.0', 'id': 7, 'error': {'code': -32000, 'message': code}}])
                self.assertEqual(len(server.requests), 1)  # Never follows a redirect with the key.
        rpc_error = self.serve(lambda message, headers: json_answer(
            {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'batch_not_supported'}}, 400))
        written = self.run_bridge(rpc_error, [json.dumps({'jsonrpc': '2.0', 'id': 9, 'method': 'ping'})])
        self.assertEqual(written[0]['id'], 9)
        self.assertEqual(written[0]['error']['message'], 'batch_not_supported')
        with self.assertRaisesRegex(ApiError, 'message_too_large'):
            Transport(rpc_error.base, self.key).post({'x': 'a' * 140_000})

    def test_real_stdio_process(self):
        server = self.serve()
        lines = [json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}),
                 json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'x'}})]
        process = subprocess.run([sys.executable, '-I', '-B', str(MCP_ROOT / 'server.py'), '--base-url', server.base,
                                  '--token-file', str(self.key)], input=('\n'.join(lines) + '\n').encode('utf-8'),
                                 capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        answers = [json.loads(line) for line in process.stdout.decode('utf-8').splitlines()]
        self.assertEqual([a['id'] for a in answers], [1, 2])
        self.assertEqual(answers[1]['result']['text'], 'Ünïcode')
        self.assertNotIn(TOKEN.encode(), process.stdout + process.stderr)
        refused = subprocess.run([sys.executable, '-I', '-B', str(MCP_ROOT / 'server.py'), '--base-url', 'http://example.com:1',
                                  '--token-file', str(self.key)], capture_output=True, timeout=30)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(b'base_url_must_be_a_loopback_origin_with_port', refused.stderr)

    def test_real_stdio_process_transports_march_batches(self):
        def answer(message, headers):
            if type(message) is not list:
                return json_answer({'jsonrpc': '2.0', 'id': message['id'],
                                    'result': {'protocolVersion': '2025-03-26'}})
            self.assertEqual(headers.get('mcp-protocol-version'), '2025-03-26')
            replies = [{'jsonrpc': '2.0', 'id': item['id'], **(
                {'result': {'text': 'Ünïcode'}} if item['method'] == 'ping'
                else {'error': {'code': -32601, 'message': 'method_not_found'}})}
                for item in message if 'id' in item]
            return json_answer(replies) if replies else (202, {}, b'')

        server = self.serve(answer)
        notice = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
                    [{'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}, notice,
                     {'jsonrpc': '2.0', 'id': 'three', 'method': 'missing'}], [notice]]
        process = subprocess.run([sys.executable, '-I', '-B', str(MCP_ROOT / 'server.py'), '--base-url', server.base,
                                  '--token-file', str(self.key)], input='\n'.join(map(json.dumps, messages)).encode(),
                                 capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        output = [json.loads(line) for line in process.stdout.decode().splitlines()]
        self.assertEqual(len(output), 2, 'notification-only batch has no reply')
        self.assertEqual([item['id'] for item in output[1]], [2, 'three'])
        self.assertEqual(output[1][0]['result'], {'text': 'Ünïcode'})
        self.assertEqual(output[1][1]['error']['code'], -32601)

    def test_batch_transport_failures_reply_to_each_request(self):
        batch = [{'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
                 {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                 {'jsonrpc': '2.0', 'id': 'three', 'method': 'ping'}]
        for response in (json_answer({'code': 'agent_auth_required'}, 401),
                         json_answer({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600,
                                     'message': 'batch_not_supported'}}, 400),
                         json_answer([{'invalid': 'response'}])):
            server = self.serve(lambda *args, response=response: response)
            written = self.run_bridge(server, [json.dumps(batch)])
            self.assertEqual(len(written), 1)
            self.assertEqual([item['id'] for item in written[0]], [2, 'three'])
            self.assertTrue(all('error' in item for item in written[0]))


if __name__ == '__main__':
    unittest.main()
