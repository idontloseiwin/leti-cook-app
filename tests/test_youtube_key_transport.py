"""The YouTube API key travels in a header, never in a URL. No network or provider requests."""
import io
from contextlib import nullcontext
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import requests
import hot_runtime
import youtube_client

KEY='AIza'+'k'*35

class Ready:
    def require_ready(self):pass

    require_collection_ready = require_ready
    require_database_ready = require_ready

class Response:
    status_code=200
    headers={}
    def iter_content(self, chunk_size): return iter((b'{}',))
    def close(self): pass

def get(endpoint='videos',**options):
    return youtube_client.youtube_api_get(endpoint,phase='test',quota_bucket='general',attempt_recorder=None,
                                          params={'part':'snippet','id':'abc'},api_key=KEY,**options)

class YouTubeKeyTransportTests(unittest.TestCase):
    def setUp(self):
        environ={k:v for k,v in os.environ.items() if k!='LIC_SETUP_DIRECTORY'}
        environ.update({
            'LIC_DEPLOYMENT_PROFILE':'self-hosted',
            'LIC_DATABASE_TRANSPORT':'postgres',
            'LIC_DATABASE_ID':'91f5d9f3-9230-4ee6-b170-155d4c3c1719',
            'LIC_HOT_RUNTIME_SECRET':'synthetic-internal-admission-secret-32',
        })
        for patcher in (patch.dict(os.environ,environ,clear=True),patch.object(hot_runtime,'_local_controller',Ready()),
                        patch('provider_runtime.admit_request', side_effect=lambda **kw: nullcontext(kw.get('cancel')))):
            patcher.start();self.addCleanup(patcher.stop)

    def test_key_is_a_header_and_not_part_of_the_url(self):
        calls=[]
        with patch.object(youtube_client.requests,'get',lambda url,**kw:calls.append((url,kw)) or Response()):
            get()
        url,options=calls[0]
        self.assertNotIn('key',options['params'])
        self.assertEqual(options['headers']['X-Goog-Api-Key'],KEY)
        prepared=requests.Request('GET',url,params=options['params'],headers=options['headers']).prepare()
        self.assertNotIn(KEY,prepared.url)

    def test_transport_errors_cannot_carry_the_key(self):
        # urllib3 puts the request path and query into "Max retries exceeded with url: ..." messages.
        def fail(url,**kw):
            path=requests.Request('GET',url,params=kw['params']).prepare().path_url
            raise requests.ConnectionError(f"HTTPSConnectionPool(host='www.googleapis.com', port=443): Max retries exceeded with url: {path}")
        with patch.object(youtube_client.requests,'get',fail):
            with self.assertRaises(requests.ConnectionError) as caught:get()
        self.assertIn('/youtube/v3/videos?',str(caught.exception))
        self.assertNotIn(KEY,str(caught.exception))

    def test_key_query_parameter_is_refused(self):
        with patch.object(youtube_client.requests,'get',lambda *a,**kw:self.fail('request sent')):
            with self.assertRaises(ValueError):
                youtube_client.youtube_api_get('videos',phase='test',quota_bucket='general',attempt_recorder=None,
                                               params={'part':'snippet','key':KEY},api_key=KEY)

    def test_conditional_metadata_request_keeps_both_headers(self):
        calls=[]
        with patch.object(youtube_client.requests,'get',lambda url,**kw:calls.append(kw) or Response()):
            get('videoCategories',if_none_match='etag-1')
            for tag in ('"etag-1"','etag-1\r\nX-Other: 1','a,b'):
                with self.assertRaises(ValueError):get('videoCategories',if_none_match=tag)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['headers'],{'If-None-Match':'etag-1','X-Goog-Api-Key':KEY})

    def test_redirect_is_not_followed_with_the_key(self):
        # Real requests redirect handling; only the network adapter is replaced.
        sent,finished=[],[]
        def send(adapter,request,**kw):
            sent.append((request.url,dict(request.headers)))
            response=requests.Response()
            response.status_code,response.url,response.request=200,request.url,request
            response.raw,response._content=io.BytesIO(b''),b'{"items":[]}'
            if request.url.startswith(youtube_client.YOUTUBE_DATA_API_BASE_URL):
                response.status_code,response._content=302,b''
                response.headers['Location']='https://collector.example/next'
            return response
        class Recorder:
            def start_attempt(self,**kw):return 'attempt-1'
            def finish_attempt(self,attempt_id,**kw):finished.append(kw)
        with patch.object(requests.adapters.HTTPAdapter,'send',send):
            with self.assertRaises(requests.HTTPError) as caught:
                youtube_client.youtube_api_get('videos',phase='test',quota_bucket='general',attempt_recorder=Recorder(),
                                               params={'part':'snippet','id':'abc'},api_key=KEY)
        self.assertEqual([url.split('?')[0] for url,_ in sent],[youtube_client.YOUTUBE_DATA_API_BASE_URL+'/videos'])
        self.assertNotIn('collector.example',str(caught.exception))
        self.assertEqual((finished[0]['status'],finished[0]['http_status']),('http_error',302))

if __name__=='__main__':unittest.main()
