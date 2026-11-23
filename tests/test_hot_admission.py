"""HOT admission paths while stored data is being checked. No server, database or provider."""
import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
import hot_runtime

class Checking:
    def require_ready(self):raise hot_runtime.HotRuntimeUnavailable('checking')
    def status(self):return {'maintenance_failed':False}

def call(method,path,accept='text/html'):
    reached,sent=[],[]
    async def app(scope,receive,send):
        reached.append(scope['path'])
        await send({'type':'http.response.start','status':299,'headers':[]})
        await send({'type':'http.response.body','body':b''})
    async def receive():return {'type':'http.request','body':b''}
    async def send(message):sent.append(message)
    scope={'type':'http','method':method,'path':path,'headers':[(b'accept',accept.encode())]}
    asyncio.run(hot_runtime.HotRuntimeMiddleware(app)(scope,receive,send))
    return bool(reached),sent[0]['status'],dict(sent[0]['headers']).get(b'content-type',b'')

class HotAdmissionTests(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.dict(os.environ,{'LIC_DEPLOYMENT_PROFILE':'self-hosted'}),
                        patch.object(hot_runtime,'_local_controller',Checking())):
            patcher.start();self.addCleanup(patcher.stop)

    def test_root_and_retired_interfaces_skip_the_check(self):
        for method in ('GET','HEAD'):
            self.assertTrue(call(method,'/')[0],method)
        for path in ('/app','/app/','/app-next','/app-next/','/app-explorer','/app-explorer/channels/UCtest',
                     '/app-explorer-v2','/app-explorer-v2/saved'):
            self.assertTrue(call('GET',path)[0],path)

    def test_workspace_shell_and_deep_links_stay_available(self):
        for path in ('/app-explorer-v3','/app-explorer-v3/','/app-explorer-v3/channels/UCtest','/app-explorer-v3/saved/collections/test'):
            reached,status,media=call('GET',path)
            self.assertEqual((reached,status),(True,299),path)

    def test_assets_are_available_while_api_and_unknown_routes_stay_blocked(self):
        self.assertTrue(call('GET','/app-explorer-v3/assets/explorer.js')[0])
        for method,path,accept in (('GET','/app/channels','text/html'),('GET','/app-explorer-v3x','text/html'),
                                   ('GET','/channels','text/html'),('POST','/','text/html'),('POST','/app-next','text/html')):
            self.assertEqual(call(method,path,accept),(False,503,b'application/json'),(method,path))

if __name__=='__main__':unittest.main()
