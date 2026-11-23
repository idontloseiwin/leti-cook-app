"""Auth + setup + actual research router, including revocation during a read."""
import asyncio
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
from agent_access import issue
from db.agent_access import AgentAccessRepository
from db.self_hosted_auth import OwnerAuthRepository
from self_hosted_auth import SelfHostedAuthMiddleware
from self_hosted_onboarding import SetupMiddleware
from web.research import create_research_router
from research_field_policy import FIELDS, TYPES


class AgentResearchHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_auth_survives_setup_and_only_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = OwnerAuthRepository.initialize(Path(directory)/'owner.sqlite3', 'dummy', int(time.time()))
            owner.issue_setup('setup', int(time.time()))
            owner.register('owner', lambda _:True, lambda:'hash', 'session', int(time.time()))
            meta, token = issue(owner, 'reader')
            store = AgentAccessRepository(owner.path)
            preferences = {'revision':1,'effective':{kind:{field:'off' for field in FIELDS} for kind in TYPES}}
            database = SimpleNamespace(config=SimpleNamespace(max_size=4))
            source_calls = []
            def rows(*args, **kwargs):
                source_calls.append((args, kwargs))
                return [{'id':'a0000000-0000-4000-8000-000000000001','name':'3D'}]
            database.read_rows = rows
            api = FastAPI()
            api.include_router(create_research_router(database))
            ready = [True]
            app = SelfHostedAuthMiddleware(SetupMiddleware(api, manager=SimpleNamespace(ready=lambda:ready[0])),
                                          repository=owner, origin='http://127.0.0.1:8002')
            with patch('web.research.ResearchPreferencesRepository.read', return_value=preferences):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:8002',
                    headers={'Authorization':'Bearer '+token,'Origin':'http://127.0.0.1:8002'}) as client:
                    for path in ['/research/capabilities?describe=1', '/research/contracts?section=canvas',
                                 '/research/sources?kind=folder&search=3D']:
                        response = await client.get(path)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(len(source_calls), 1)
                    from db.base.postgres import DatabaseError
                    with patch.object(database, 'read_rows', side_effect=DatabaseError('private diagnostic')):
                        failed = await client.get('/research/sources')
                        self.assertEqual(failed.status_code, 503)
                        self.assertEqual(failed.json()['code'], 'backend_unavailable')
                        self.assertNotIn('private diagnostic', failed.text)
                    for path in ['/research/sources?kind=channels', '/research/sources?limit=999999999999999999999999',
                                 '/research/contracts?section=canvas&section=query', '/research/capabilities?describe=2']:
                        self.assertEqual((await client.get(path)).status_code, 422)
                    for method, path in [('GET','/research/fields'),('POST','/research/fields/apply'),('POST','/setup/prepare')]:
                        self.assertEqual((await client.request(method,path,json={})).status_code, 403)
                    ready[0] = False
                    self.assertEqual((await client.get('/research/capabilities')).status_code, 409)
                    ready[0] = True
                    def revoked(*args, **kwargs):
                        store.revoke(meta['id'])
                        return rows(*args, **kwargs)
                    database.read_rows = revoked
                    response = await client.get('/research/sources?kind=folder')
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn('3D', response.text)


if __name__ == '__main__': unittest.main()
