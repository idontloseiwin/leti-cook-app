"""Exercise the real V3 HTML handler without starting collectors or a database."""
from pathlib import Path
import tempfile
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from http_test_support import ROOT
from web.frontend import create_frontend_router

class ExplorerV3Tests(unittest.TestCase):
    def test_v3_entry_and_deep_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            app = FastAPI()
            app.include_router(create_frontend_router(FRONTEND_DIST_DIR=path))
            with TestClient(app) as client:
                self.assertEqual(client.get('/app-explorer-v3').status_code, 404)
                for old in ('/app-next', '/app-explorer', '/app-explorer-v2', '/app'):
                    for suffix in ('', '/', '/channels/UCtest', '/health', '/assets/old.js') if old != '/app' else ('', '/'):
                        retired = client.get(old + suffix)
                        self.assertEqual(retired.status_code, 410)
                        self.assertEqual(retired.json()['detail']['replacement'], '/app-explorer-v3')
                (path / 'assets').mkdir()
                (path / 'assets/check.js').write_text('console.log(1)', encoding='utf-8')
                self.assertEqual(client.get('/app-explorer-v3/assets/check.js').status_code, 200)
                self.assertEqual(client.get('/app-explorer-v3/assets/missing.js').status_code, 404)
                (path / 'explorer-v3.html').write_text('<html>V3</html>', encoding='utf-8')
                for suffix in ('', '/channels/UCtest', '/saved', '/saved/collections/test', '/health', '/telemetry'):
                    response = client.get('/app-explorer-v3' + suffix)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text, '<html>V3</html>')

    def test_root_opens_v3(self):
        app = FastAPI()
        app.include_router(create_frontend_router(FRONTEND_DIST_DIR=Path('unused-dist')))
        with TestClient(app) as client:
            for method in ('GET', 'HEAD'):
                response = client.request(method, '/', follow_redirects=False)
                self.assertEqual(response.status_code, 303, method)
                self.assertEqual(response.headers['location'], '/app-explorer-v3', method)

    def test_self_hosted_features_stay_in_v3(self):
        entry = (ROOT / 'application/frontend/src/explorer-v3-main.tsx').read_text(encoding='utf-8')
        self.assertIn('mountWithOwnerAccess(() =>', entry)
        self.assertIn('userWorkEnabled={true}', entry)
        app = (ROOT / 'application/frontend/src/explorer/ExplorerApp.tsx').read_text(encoding='utf-8')
        self.assertIn('useSourceExpiry(sidebarState', app)
        for page in ('Health', 'Telemetry', 'Saved'):
            self.assertIn("pageBoundary('" + page + "',", app)
        for name in ('auth.js', 'setup.html', 'index.html'):
            ui = (ROOT / 'application/self_hosted_ui' / name).read_text(encoding='utf-8')
            self.assertIn('/app-explorer-v3', ui)
            self.assertNotIn('/app-explorer-v2', ui)
