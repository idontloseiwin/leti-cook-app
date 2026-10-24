"""Real setup store/middleware, fake job reads. No PostgreSQL or provider calls."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from self_hosted_configuration import ConfigurationStore, STARTER_CHANNEL_URLS
from self_hosted_onboarding import SetupManager, SetupMiddleware, initial_loading_complete
from self_hosted_settings import SettingsError
from http_test_support import channel_http, jobs_stub, library_stub, endpoint

COMPLETE = {'status': 'completed', 'all_initial_checks_complete': True,
            'current_profile_ready': False, 'action_required': None,
            'job': {'status': 'completed', 'phase': 'verify', 'errors': 0}}


class WorkspacePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.store = ConfigurationStore.initialize(self.temp.name)
        self.store.update(0, lambda v: v.update(api_key='synthetic_test_key_no_requests', prepared=True, phase='ready'))
        self.manager = self.manager_for(self.store)
        self.urls = sorted(STARTER_CHANNEL_URLS)
        self.ids = ['UC' + str(i) * 22 for i in range(5)]

    def manager_for(self, store):
        return SetupManager(store, reference=lambda: {'status': 'current'}, apply=lambda _: None)

    def record(self, i):
        self.manager.begin_starter(self.urls[i])
        self.manager.record_starter(self.urls[i], {'status': 'added', 'channel': {'channel_id': self.ids[i]}})

    def finish_function(self, reader, ready=lambda **kw: None):
        http = channel_http(jobs=jobs_stub(_load_initial_hydration_state=reader),
            library=library_stub(load_visible_channel_or_404=lambda id: {'channel_id': id}))
        finish = endpoint(http.add_router, 'finish_starter_setup')
        with patch('self_hosted_onboarding.get_manager', return_value=self.manager), patch('hot_runtime.require_hot_runtime_ready', ready):
            return finish()

    def test_selection_and_unconfirmed_add_survive_restart(self):
        self.manager.start_workspace(self.urls[:2])
        self.record(0)
        restarted = self.manager_for(ConfigurationStore(self.temp.name))
        self.assertEqual(restarted.public()['workspace'], {'phase': 'loading', 'channels': {
            self.urls[0]: self.ids[0], self.urls[1]: None}})
        self.assertTrue(restarted.workspace_pending())
        self.assertEqual(self.finish_function(lambda _: COMPLETE), {'ready': False})
        # The stored file is ciphertext; random base64 can contain "UC", so check
        # that no channel ID or link is readable, not two letters.
        stored = self.store.path.read_bytes()
        for value in self.ids + self.urls[:2]:
            self.assertNotIn(value.encode(), stored)

    def test_parallel_add_results_are_not_lost(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(self.record, range(5)))
        channels = self.manager.public()['workspace']['channels']
        self.assertEqual(channels, dict(zip(self.urls, self.ids)))

    def test_invalid_or_expanded_selection_cannot_enter_configuration(self):
        for value in (self.urls * 2, ['https://evil.invalid/'], None, [self.urls[0], self.urls[0]], [1]):
            with self.assertRaises(SettingsError): self.manager.start_workspace(value)
        self.manager.start_workspace(self.urls[:1])
        with self.assertRaises(SettingsError): self.manager.begin_starter(self.urls[1])
        self.assertEqual(set(self.manager.public()['workspace']['channels']), {self.urls[0]})

    def test_old_configuration_does_not_reenter_setup(self):
        value = self.store.read(); value.pop('workspace'); self.store._write(value)
        old = self.manager_for(ConfigurationStore(self.temp.name))
        self.assertFalse(old.workspace_pending())
        self.assertTrue(old.ready())

    def test_completed_checks_include_unavailable_videos_but_not_unfinished_verification(self):
        self.assertTrue(initial_loading_complete(COMPLETE))
        for value in ({**COMPLETE, 'all_initial_checks_complete': False},
                      {**COMPLETE, 'job': {'status': 'running', 'phase': 'verify', 'errors': 0}},
                      {**COMPLETE, 'job': {'status': 'completed', 'phase': 'verify', 'errors': 1}},
                      {**COMPLETE, 'action_required': 'retry'},
                      {'status': 'running', 'progress': {'percent': 100}}, {}):
            self.assertFalse(initial_loading_complete(value), value)

    def test_finish_requires_all_jobs_and_rechecks_hot(self):
        self.manager.start_workspace(self.urls[:2]); self.record(0); self.record(1)
        self.assertEqual(self.finish_function(lambda c: COMPLETE if c['channel_id'] == self.ids[0] else {}), {'ready': False})
        calls = []
        self.assertEqual(self.finish_function(lambda _: COMPLETE, lambda **kw: calls.append(kw)), {'ready': True})
        self.assertEqual(calls, [{'operation': 'http'}])
        self.assertFalse(self.manager.workspace_pending())
        self.assertEqual(self.finish_function(lambda _: self.fail('must not reread completed setup')), {'ready': True})

    def test_empty_selection_still_requires_hot(self):
        self.manager.start_workspace([])
        def blocked(**kw): raise RuntimeError('hot blocked')
        with self.assertRaises(RuntimeError): self.finish_function(lambda _: COMPLETE, blocked)
        self.assertTrue(self.manager.workspace_pending())
        self.assertEqual(self.finish_function(lambda _: COMPLETE), {'ready': True})

    def test_changed_selection_cannot_pass_an_older_completion_check(self):
        self.manager.start_workspace(self.urls[:1]); self.record(0)
        def read(_):
            self.manager.start_workspace(self.urls[:2])
            return COMPLETE
        with self.assertRaises(HTTPException) as caught: self.finish_function(read)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.manager.workspace_pending())

    def test_workspace_navigation_waits_but_progress_and_worker_routes_remain_available(self):
        async def downstream(scope, receive, send):
            if scope['type'] == 'lifespan':
                await receive(); await send({'type': 'lifespan.startup.complete'})
                await receive(); await send({'type': 'lifespan.shutdown.complete'})
                return
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'allowed'})
        gate = SetupMiddleware(downstream, manager=self.manager)
        async def authenticated(scope, receive, send):
            scope.setdefault('state', {})['installation_access'] = True
            await gate(scope, receive, send)
        with TestClient(authenticated) as client:
            for path in ('/', '/app-explorer-v3', '/app-explorer-v3/channels/test'):
                for method in ('GET', 'HEAD'):
                    response = client.request(method, path, follow_redirects=False)
                    self.assertEqual(response.status_code, 303, path)
                    self.assertEqual(response.headers['location'], '/setup')
            for path in ('/channels/test/hydration', '/system/hot-runtime', '/internal/hot-runtime', '/app-explorer-v3/assets/test.js'):
                self.assertEqual(client.get(path).status_code, 200, path)
            response = client.post('/setup/workspace/start', json={'channels': []})
            self.assertEqual(response.status_code, 409)  # custom CSRF header required
            response = client.post('/setup/workspace/start', json={'channels': []}, headers={'X-LIC-Setup': '1'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['workspace']['phase'], 'loading')
            self.finish_function(lambda _: COMPLETE)
            self.assertEqual(client.get('/app-explorer-v3').status_code, 200)


if __name__ == '__main__': unittest.main()
