"""Preview routes share owner, HOT, source-lifetime and body-limit boundaries."""
from datetime import timedelta
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'application'), str(ROOT/'tests')]
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hot_runtime import HotRuntimeUnavailable
from self_hosted_auth import SelfHostedAuthMiddleware
from request_limits import RequestBodyLimitMiddleware
from web.middleware import install_runtime_middleware, HttpReadScopeMiddleware
from web.research import create_research_router
from research_query.service import QueryService
from test_research_query_service import Repository, NOW
from test_research_query_contract import query


class Owner:
    mode = 'local'
    def configured(self): return True
    def access_state(self, now): return {'mode': self.mode}
    def session(self, *args, **kwargs): return None


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.owner, self.denied = Repository(), Owner(), False
        self.q = query(select=['video.id', 'video.title', 'video.views'])
        self.env = patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE='self-hosted', LIC_RESEARCH_QUERY_PREVIEW='1')
        self.env.start(); self.addCleanup(self.env.stop)
        for target, value in [
            ('web.research.ResearchQueryRepository', lambda database: self.repo),
            ('web.research.QueryService', lambda *a, **kw: QueryService(*a, **kw, clock=lambda: NOW)),
            ('web.middleware.hot_read_now', lambda: NOW),
            ('hot_runtime.require_hot_runtime_ready', self.admission),
        ]:
            item = patch(target, value); item.start(); self.addCleanup(item.stop)
        self.app = FastAPI()
        self.projection = SimpleNamespace(initial_fill_complete=True, preparation_failing=False)
        self.app.state.research_projection = self.projection
        install_runtime_middleware(self.app, core=SimpleNamespace())
        self.app.include_router(create_research_router(None))
        self.app.add_middleware(RequestBodyLimitMiddleware)
        self.app.add_middleware(SelfHostedAuthMiddleware, repository=self.owner, origin='http://127.0.0.1:8002')
        self.app.add_middleware(HttpReadScopeMiddleware)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8002', headers={'Origin': 'http://127.0.0.1:8002'})
        self.addCleanup(self.client.close)

    def admission(self, **kwargs):
        if self.denied: raise HotRuntimeUnavailable()

    def post(self, **kwargs): return self.client.post('/research/queries/execute', json=self.q, **kwargs)

    def test_disabled_preview_or_unauthorized_owner_never_reads(self):
        os.environ['LIC_RESEARCH_QUERY_PREVIEW'] = '0'
        self.assertEqual(self.client.get('/research/capabilities').status_code, 403)
        self.assertEqual(self.post().status_code, 403)
        self.owner.mode = 'password'
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.repo.reads, 0)

    def test_initial_preparation_returns_fixed_retryable_state_without_reading(self):
        self.projection.initial_fill_complete = False
        answer = self.post()
        self.assertEqual((answer.status_code, answer.json()['code']), (503, 'catalogue_preparing'))
        self.assertEqual(answer.headers['retry-after'], '2')
        self.assertEqual(answer.headers['cache-control'], 'no-store')
        self.assertEqual(self.repo.reads, 0)
        self.assertEqual(self.client.get('/research/capabilities').status_code, 200)
        self.projection.preparation_failing = True
        answer = self.post()
        self.assertEqual(answer.json()['code'], 'backend_unavailable')
        self.assertNotIn('retry-after', answer.headers)
        self.assertEqual(self.repo.reads, 0)
        self.projection.initial_fill_complete = True
        self.assertEqual(self.post().status_code, 200)

    def test_missing_worker_fails_closed_without_reading(self):
        self.app.state.research_projection = None
        self.assertEqual(self.post().json()['code'], 'backend_unavailable')
        self.assertEqual(self.repo.reads, 0)

    def test_same_origin_and_body_limits_precede_database(self):
        self.assertEqual(self.post(headers={'Origin': 'https://elsewhere.invalid'}).status_code, 403)
        response = self.client.post('/research/queries/execute', content=b'x' * 65537)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.repo.reads, 0)

    def test_source_lifetime_and_exact_numbers_survive_transport(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['rows'][0]['cells']['video.views']['value'], '9007199254740993')
        self.assertEqual(data['source_expires_at'], data['context']['valid_before'])
        self.assertEqual(data['source_expires_at'], response.headers['x-source-expires-at'])
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_hot_denial_before_or_after_post_discards_results(self):
        self.denied = True
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.repo.reads, 0)
        self.denied = False
        self.repo.on_recheck = lambda: setattr(self, 'denied', True)
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private fixture text', response.text)

    def test_owner_revocation_during_read_discards_results(self):
        self.repo.on_recheck = lambda: setattr(self.owner, 'mode', 'password')
        response = self.post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'policy_denied')

    def test_expiry_during_response_envelope_discards_results(self):
        with patch('web.middleware.hot_read_now', side_effect=[NOW, NOW+timedelta(seconds=121)]):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private fixture text', response.text)

    def test_cursor_key_survives_request_binding(self):
        self.q['page'] = {'size': 1}
        self.repo.data['rows'] *= 2
        first = self.post()
        self.assertEqual(first.status_code, 200, first.text)
        self.q['page']['after'] = first.json()['page']['next_cursor']
        second = self.post()
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.repo.reads, 2)


if __name__ == '__main__': unittest.main()
