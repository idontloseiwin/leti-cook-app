"""Release acquisition uses synthetic GitHub HTTP and real ephemeral signatures."""
from __future__ import annotations

from dataclasses import replace
from email.message import Message
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops import release_download as download
from ops.release_manifest import ReleaseError, ReleaseSource
from tests import test_release_verification as fixtures

SOURCE = ReleaseSource('example-owner', 'example-app')
BASE = 'https://api.github.com/repos/example-owner/example-app/releases'


def release(version='0.1.0', ident=7, *, prerelease=False, draft=False, assets=None):
    return {'id': ident, 'tag_name': 'v' + version, 'draft': draft, 'prerelease': prerelease,
            'assets': assets if assets is not None else [
                {'id': index, 'name': name, 'size': size, 'state': 'uploaded'} for index, (name, size) in enumerate(
                    ((f'leti-cook-{version}.tar.gz', 100), ('release.json', 100), ('SHA256SUMS', 100), ('SHA256SUMS.sig', 64)), 10)]}


class Transport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, *, accept, limit):
        self.calls.append((url, accept, limit))
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value if type(value) is bytes else json.dumps(value).encode()


def routes(values, selected=None):
    selected = values[0] if selected is None else selected
    return {BASE + '?per_page=100&page=1': values, BASE + '/' + str(selected['id']): selected}


class SelectionTests(unittest.TestCase):
    def test_latest_stable_is_semver_not_api_order_or_latest_pointer(self):
        values = [release('2.0.0-rc.1', 1, prerelease=True), release('1.9.0', 2), release('1.10.0', 3),
                  release('8.0.0', 4, draft=True), release('3.0.0-rc.1', 5)]
        client = Transport(routes(values, values[2]))
        selected = download.select_release(SOURCE, transport=client)
        self.assertEqual((selected.version, selected.release_id), ('1.10.0', 3))
        self.assertEqual(client.calls[-1][0], BASE + '/3')
        self.assertFalse(any('latest' in call[0] for call in client.calls))

    def test_pre_flag_and_exact_version(self):
        values = [release('1.0.0', 1), release('2.0.0-rc.2', 2, prerelease=True)]
        client = Transport(routes(values, values[1]))
        self.assertEqual(download.select_release(SOURCE, include_prereleases=True, transport=client).version, '2.0.0-rc.2')
        version = '2.0.0-rc.2+build.1'
        candidate = release(version, 12, prerelease=True)
        client = Transport({BASE + '/tags/v2.0.0-rc.2%2Bbuild.1': candidate, BASE + '/12': candidate})
        self.assertEqual(download.select_release(SOURCE, version=version, include_prereleases=True, transport=client).version, version)
        with self.assertRaisesRegex(ReleaseError, 'prerelease_requires_pre'):
            download.select_release(SOURCE, version=version, transport=client)

    def test_metadata_cannot_redirect_selection_or_asset_requests(self):
        candidate = release()
        candidate.update(url='https://evil.invalid/release', assets_url='http://127.0.0.1/private')
        for item in candidate['assets']:
            item['browser_download_url'] = 'file:///etc/passwd'
        selected = download.select_release(SOURCE, transport=Transport(routes([candidate])))
        self.assertEqual({asset.asset_id for asset in selected.assets}, {10, 11, 12, 13})
        self.assertFalse(hasattr(selected.assets[0], 'url'))

    def test_bounded_pagination_includes_later_highest_version(self):
        first = [release('0.1.' + str(n), n + 1) for n in range(100)]
        last = release('0.9.0', 101)
        client = Transport({BASE + '?per_page=100&page=1': first,
                            BASE + '?per_page=100&page=2': [last], BASE + '/101': last})
        self.assertEqual(download.select_release(SOURCE, transport=client).version, '0.9.0')
        with patch.object(download, 'MAX_PAGES', 1), self.assertRaisesRegex(ReleaseError, 'discovery_limit_exceeded'):
            download.select_release(SOURCE, transport=client)

    def test_duplicates_and_selected_metadata_change_fail_closed(self):
        first = release()
        for values, fixed, reason in (([first, first], first, 'discovery_changed'),
                ([first], release('9.0.0'), 'discovery_changed'),
                ([first], release(draft=True), 'discovery_changed'),
                ([first], release(ident=8), 'discovery_changed')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ReleaseError, reason):
                download.select_release(SOURCE, transport=Transport({BASE + '?per_page=100&page=1': values, BASE + '/7': fixed}))

    def test_missing_duplicated_and_unuploaded_assets_refused(self):
        for mutation in (lambda a:a.pop(), lambda a:a.append(dict(a[0])),
                         lambda a:a[0].update(state='new'), lambda a:a[-1].update(size=63),
                         lambda a:a[0].update(size=download.MAX_ARCHIVE+1),
                         lambda a:a[1].update(name='../../release.json')):
            candidate = release()
            mutation(candidate['assets'])
            with self.subTest(mutation=mutation), self.assertRaises(ReleaseError):
                download.select_release(SOURCE, transport=Transport(routes([candidate])))

    def test_no_release_invalid_metadata_and_equal_precedence_are_not_silently_selected(self):
        for values, reason in (([], 'release_not_found'), ([{'id': 7}], 'metadata_invalid'),
                              ([release('1.0.0+a',1),release('1.0.0+b',2)],'version_ambiguous')):
            with self.subTest(values=values), self.assertRaisesRegex(ReleaseError, reason):
                download.select_release(SOURCE, transport=Transport({BASE+'?per_page=100&page=1':values}))
        with self.assertRaisesRegex(ReleaseError, 'source_invalid'):
            download.select_release({'owner':'untrusted'}, transport=Transport({}))
        with self.assertRaisesRegex(ReleaseError, 'version_invalid'):
            download.select_release(SOURCE, version='../evil', transport=Transport({}))

    def test_injected_transport_remains_subject_to_size_and_json_guards(self):
        for data in (b'x' * (download.MAX_METADATA+1), b'[{"id":1,"id":2}]', b'NaN'):
            with self.subTest(size=len(data)), self.assertRaises(ReleaseError):
                download.select_release(SOURCE, transport=Transport({BASE+'?per_page=100&page=1':data}))


class Response:
    def __init__(self, body=b'payload', status=200, **headers):
        self.status, self.headers, self.body = status, Message(), io.BytesIO(body)
        for name, value in headers.items():
            self.headers[name.replace('_','-')] = str(value)

    def read1(self, size):
        return self.body.read(size)


class Connection:
    def __init__(self, response):
        self.response, self.sock, self.calls, self.closed = response, None, [], False

    def request(self, *args, **kwargs):
        self.calls.append((args,kwargs))

    def getresponse(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


class ProductionTransportTests(unittest.TestCase):
    def transfer(self, responses, *, accept=download.BINARY_ACCEPT, limit=100):
        connections = [Connection(value) for value in responses]
        self.connections = connections
        with patch.object(download.http.client, 'HTTPSConnection', side_effect=connections) as connect:
            self.connect = connect
            return download.GitHubTransport().get(BASE+'/assets/1',accept=accept,limit=limit)

    def test_direct_200_and_explicit_asset_redirect_without_credentials(self):
        self.assertEqual(self.transfer([Response()]), b'payload')
        location='https://release-assets.githubusercontent.com/asset?opaque=signature'
        with patch.dict(os.environ, {'HTTPS_PROXY':'http://evil.invalid','GH_TOKEN':'not-forwarded','GITHUB_TOKEN':'not-forwarded'}):
            self.assertEqual(self.transfer([Response(status=302,Location=location),Response()]),b'payload')
        self.assertEqual(self.connect.call_args_list[1].args,('release-assets.githubusercontent.com',))
        for connection in self.connections:
            headers=connection.calls[0][1]['headers']
            self.assertNotIn('Authorization',headers)
            self.assertTrue(connection.closed)

    def test_wrong_host_scheme_credentials_fragment_and_metadata_redirect_refused(self):
        for target in ('http://release-assets.githubusercontent.com/asset','https://evil.invalid/asset',
                       'https://release-assets.githubusercontent.com.evil.invalid/asset',
                       'https://user@release-assets.githubusercontent.com/asset',
                       'https://release-assets.githubusercontent.com:444/asset','https://release-assets.githubusercontent.com/asset#x'):
            with self.subTest(target=target),self.assertRaisesRegex(ReleaseError,'url_invalid'):
                self.transfer([Response(status=302,Location=target)])
        with self.assertRaisesRegex(ReleaseError,'redirect_invalid'):
            self.transfer([Response(status=302,Location='https://api.github.com/else')],accept=download.API_ACCEPT)

    def test_redirect_bound_and_http_errors_are_fixed_codes(self):
        with self.assertRaisesRegex(ReleaseError,'redirect_invalid'):
            self.transfer([Response(status=302,Location='https://release-assets.githubusercontent.com/loop') for _ in range(4)])
        for status,code in ((403,'access_denied'),(404,'not_found'),(429,'rate_limited'),(500,'http_error')):
            with self.subTest(status=status),self.assertRaisesRegex(ReleaseError,code):
                self.transfer([Response(body=b'private body must not be exposed',status=status)])

    def test_timeout_chunk_limits_truncation_and_encoding(self):
        cases=((Response(body=b'12345'),3,'too_large'),
               (Response(body=b'12345',Content_Length=8),10,'incomplete'),
               (Response(body=b'12345',Content_Length=11),10,'too_large'),
               (Response(Content_Encoding='gzip'),100,'encoding_invalid'),
               (TimeoutError('opaque private URL'),100,'timeout'))
        for response,limit,reason in cases:
            with self.subTest(reason=reason),self.assertRaisesRegex(ReleaseError,'release_download_'+reason):
                self.transfer([response],limit=limit)
        client=download.GitHubTransport()
        client._deadline=-1
        with self.assertRaisesRegex(ReleaseError,'timeout'):
            client.get(BASE,accept=download.API_ACCEPT,limit=100)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.ReleaseVerificationTests.setUpClass()

    def setUp(self):
        self.fixture=fixtures.ReleaseVerificationTests(methodName='test_real_ed25519_verification_returns_immutable_captured_bytes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.assets=[]
        self.payloads={}
        for index,path in enumerate(sorted(self.fixture.directory.iterdir()),10):
            self.assets.append({'id':index,'name':path.name,'state':'uploaded','size':path.stat().st_size})
            self.payloads[BASE+'/assets/'+str(index)]=path.read_bytes()
        self.candidate=release(assets=self.assets)
        self.transport=Transport({**routes([self.candidate]),**self.payloads})

    def fetch(self, **kwargs):
        return download.fetch_release(source=SOURCE,public_key_path=self.fixture.public_file,
                    transport=self.transport,openssl=self.fixture.openssl,**kwargs)

    def test_real_signature_and_captured_bytes_match_offline_verifier(self):
        expected=self.fixture.verify()
        actual=self.fetch()
        self.assertEqual(actual,expected)
        self.assertEqual(len(self.transport.calls),6)
        self.assertEqual({url for url,accept,_ in self.transport.calls if accept==download.BINARY_ACCEPT},set(self.payloads))
        self.fixture.archive.write_bytes(b'changed afterwards')
        self.assertEqual(actual.files['application/VERSION'],b'0.1.0\n')

    def test_tampered_asset_or_signature_is_rejected_and_quarantine_cleaned(self):
        selection=download.select_release(SOURCE,transport=self.transport)
        for name in ('SHA256SUMS.sig','leti-cook-0.1.0.tar.gz'):
            asset=next(item for item in selection.assets if item.name==name)
            url=BASE+'/assets/'+str(asset.asset_id)
            original=self.transport.routes[url]
            self.transport.routes[url]=bytes([original[0]^1])+original[1:]
            with self.subTest(name=name),self.assertRaises(ReleaseError):
                self.fetch()
            self.transport.routes[url]=original
        changed=replace(selection,assets=(replace(selection.assets[0],size=1),*selection.assets[1:]))
        with self.assertRaisesRegex(ReleaseError,'asset_changed'):
            download.download_verified_release(changed,public_key_path=self.fixture.public_file,transport=self.transport)

    def test_local_http_transport_uses_same_selection_asset_and_signature_path(self):
        payloads={urlsplit(url).path+('?' + urlsplit(url).query if urlsplit(url).query else ''):
                    (value if type(value)is bytes else json.dumps(value).encode()) for url,value in self.transport.routes.items()}
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body=payloads[self.path]
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        class LocalTransport:
            def get(self,url,*,accept,limit):
                parsed=urlsplit(url)
                if parsed.hostname!='api.github.com':
                    raise AssertionError('production URL changed')
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                try:
                    connection.request('GET',parsed.path+('?' + parsed.query if parsed.query else ''),headers={'Accept':accept})
                    response=connection.getresponse()
                    return response.read(limit+1)
                finally:
                    connection.close()
        try:
            actual=download.fetch_release(source=SOURCE,public_key_path=self.fixture.public_file,
                        transport=LocalTransport(),openssl=self.fixture.openssl)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(actual,self.fixture.verify())
        self.assertEqual(len(seen),6)


if __name__=='__main__':
    unittest.main()
