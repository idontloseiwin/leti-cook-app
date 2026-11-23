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
    def test_graph_execution_uses_owner_scope_and_lifetime_middleware(self):
        from test_research_graph import graph_document
        doc=graph_document();doc['query']=self.q
        self.repo.scope_revision=lambda *args,**kwargs:self.repo.data['scope_revision']
        answer=self.client.post('/research/graphs/execute',json=doc)
        self.assertEqual(answer.status_code,200,answer.text)
        result=answer.json()
        self.assertEqual(result['nodes']['result']['value']['rows'][0]['cells']['video.views']['value'],'9007199254740993')
        self.assertEqual(result['source_expires_at'],result['context']['valid_before'])
        self.assertEqual(answer.headers['cache-control'],'no-store')
        self.assertEqual(self.client.post('/research/graphs/validate',json=doc).status_code,200)
        doc['graph']['edges'][0]['output']='rows'
        self.assertEqual(self.client.post('/research/graphs/validate',json=doc).status_code,422)
        self.denied=True
        self.assertEqual(self.client.post('/research/graphs/execute',json=doc).status_code,503)
        self.denied=False;self.owner.mode='password'
        self.assertEqual(self.client.post('/research/graphs/execute',json=doc).status_code,401)

    def test_registered_calculation_is_gated_and_keeps_http_lifetime(self):
        from research_query.aggregate import CALENDAR
        payload=dict(version=1,operation=CALENDAR,query=self.q,parameters={'year':2026,'time_zone':'UTC'})
        self.assertEqual(self.client.post('/research/operations/execute',json=payload).status_code,403)
        os.environ['CHANNEL_BASELINE_ANALYTICS_ENABLED']='true'
        self.addCleanup(lambda:os.environ.pop('CHANNEL_BASELINE_ANALYTICS_ENABLED',None))
        self.repo.read_operation=lambda *args,**kwargs:{**self.repo.data,'result':dict(population=1,known_count=1,year=2026,time_zone='UTC',days=[dict(date='2026-10-01',count=1)],hours=[dict(hour=1,count=1)],weekdays=[dict(weekday=4,count=1)])}
        answer=self.client.post('/research/operations/execute',json=payload)
        self.assertEqual(answer.status_code,200,answer.text)
        self.assertEqual(answer.json()['source_expires_at'],answer.json()['context']['valid_before'])
        self.denied=True
        self.assertEqual(self.client.post('/research/operations/execute',json=payload).status_code,503)

    def test_optional_field_off_rejects_before_source_read(self):
        self.q['select']=['video.description']
        response=self.post()
        self.assertEqual((response.status_code,response.json()['code']),(422,'field_disabled'))
        self.assertEqual(self.repo.reads,0)
        fields={f['id']:f for f in self.client.get('/research/capabilities').json()['fields']}
        self.assertEqual(fields['video.description']['enabled_video_types'],[])

    def test_collection_strict_coverage_basis_survives_http(self):
        self.q.update(scope={'kind':'collection','id':'11111111-1111-4111-8111-111111111111'},
                      coverage={'mode':'require_complete'})
        self.repo.data['channels_complete'] = 0
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['coverage'], {'status':'complete','basis':'collection_members',
            'channels_in_scope':1, 'channels_complete':0, 'eligible_count':None, 'reasons':{}})
        self.repo.data['unresolved_collection_members'] = 1
        self.assertEqual(self.post().status_code, 409)

    def test_extra_field_capabilities_and_json_types_over_http(self):
        capabilities = self.client.get('/research/capabilities').json()
        fields = {f['id']:f for f in capabilities['fields']}
        self.assertIn('has_all', fields['video.tags']['operators'])
        self.assertFalse(fields['video.tags']['sortable'])
        self.q['select'] = ['video.likes', 'video.tags', 'video.caption_available']
        self.repo.data['rows'][0].update(likes=9007199254740993, likes_state='known',
            tags=['<script>text is data</script>', 'Straße'], tags_state='known',
            caption_available=False, caption_available_state='known')
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        cells = response.json()['rows'][0]['cells']
        self.assertEqual(cells['video.likes']['value'], '9007199254740993')
        self.assertEqual(cells['video.tags']['value'], ['<script>text is data</script>', 'Straße'])
        self.assertIs(cells['video.caption_available']['value'], False)
        self.q['order_by'] = [{'field':'video.tags','direction':'asc'}]
        self.assertEqual(self.post().json()['code'], 'unsupported_operator')
        self.assertEqual(self.repo.reads, 1)

    def setUp(self):
        self.repo, self.owner, self.denied = Repository(), Owner(), False
        self.q = query(select=['video.id', 'video.title', 'video.views'])
        self.env = patch.dict(os.environ, LIC_DEPLOYMENT_PROFILE='self-hosted')
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
        from research_field_fixture import preference_read
        self.database = SimpleNamespace(read_row=preference_read)
        self.app.include_router(create_research_router(self.database))
        self.app.add_middleware(RequestBodyLimitMiddleware)
        self.app.add_middleware(SelfHostedAuthMiddleware, repository=self.owner, origin='http://127.0.0.1:8002')
        self.app.add_middleware(HttpReadScopeMiddleware)
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8002', headers={'Origin': 'http://127.0.0.1:8002'})
        self.addCleanup(self.client.close)

    def admission(self, **kwargs):
        if self.denied: raise HotRuntimeUnavailable()

    def post(self, **kwargs): return self.client.post('/research/queries/execute', json=self.q, **kwargs)

    def test_research_api_is_on_without_a_setting(self):
        # No backend.env line opens or closes it: the signed-in owner can always use it.
        reads = self.repo.reads
        self.assertEqual(self.client.get('/research/capabilities').status_code, 200)
        self.assertEqual(self.client.get('/research/fields').status_code, 200)
        self.assertEqual(self.client.post('/research/queries/validate', json=self.q).status_code, 200)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.repo.reads, reads + 1)

    def test_unauthorized_owner_never_reads(self):
        self.owner.mode = 'password'
        self.assertEqual(self.client.get('/research/capabilities').status_code, 401)
        self.assertEqual(self.client.get('/research/fields').status_code, 401)
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.repo.reads, 0)

    def test_field_settings_still_end_with_owner_access(self):
        from research_field_fixture import preference_read
        def revoked_during_read(*args):
            self.owner.mode = 'password'
            return preference_read(*args)
        self.database.read_row = revoked_during_read
        response = self.client.get('/research/fields')
        self.assertEqual((response.status_code, response.json()), (403, {'code': 'policy_denied'}))

    def test_analytics_gates_apply_to_research_operations(self):
        # The research API grants no analytics: summaries stay behind their own flag.
        from research_query.aggregate import SUMMARY
        os.environ.pop('CHANNEL_BASELINE_ANALYTICS_ENABLED', None)
        operations = self.client.get('/research/capabilities').json()['operations']
        self.assertNotIn(SUMMARY, operations)
        payload = dict(version=1, operation=SUMMARY, query=self.q, parameters={})
        self.assertEqual(self.client.post('/research/operations/execute', json=payload).status_code, 403)
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
