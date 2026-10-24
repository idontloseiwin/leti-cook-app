"""Finite admission policy matches reviewed routes, never a broad URL prefix."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "application"))

import json
import re
import unittest
from hot_runtime_routes import ROUTE_ACCESS, MIDDLEWARE_PATHS, route_access


class RuntimeRouteTests(unittest.TestCase):
    def test_every_reviewed_route_has_exact_policy_and_no_stale_exemptions(self):
        data = json.loads((ROOT/'tests/fixtures/read_paths.json').read_text())
        self.assertEqual(set(ROUTE_ACCESS), set(data['routes']))
        self.assertEqual(MIDDLEWARE_PATHS, set(data['middleware_paths']))
        for key, entry in data['routes'].items():
            method, template = key.split(' ', 1)
            path = re.sub(r'\{[^}]+\}', 'fixture', template)
            expected = 'user_work' if entry['verdict'] == 'no_youtube_data' else 'api_read'
            with self.subTest(route=key):
                self.assertEqual(ROUTE_ACCESS[key], expected)
                self.assertEqual(route_access(method,path),expected)

    def test_unknown_path_or_method_is_never_a_user_work_exemption(self):
        for method,path in [('GET','/saved/new-read'),('GET','/user-work/new-read'),
                            ('POST','/sidebar-user-data'),('GET','/setup/private-future'),
                            ('GET','/app-explorer-v4'),('HEAD','/sidebar-data')]:
            with self.subTest(path=path,method=method):
                self.assertEqual(route_access(method,path),'api_read')

    def test_reconnect_intents_remain_available_when_collection_is_paused(self):
        self.assertEqual(route_access('POST', '/user-work/pending/reconnect'), 'user_work')
        self.assertEqual(route_access('POST', '/user-work/pending/pending-id/resolve'), 'user_work')
        self.assertEqual(route_access('GET', '/user-work/pending/reconnect'), 'api_read')
        self.assertEqual(route_access('POST', '/user-work/pending/reconnect-new'), 'api_read')

    def test_static_paths_beat_dynamic_parameters_and_mixed_commands_need_api_gate(self):
        self.assertEqual(route_access('GET','/system/video-type-reviews/hidden'),'user_work')
        self.assertEqual(route_access('GET','/system/video-type-reviews/42'),'api_read')
        self.assertEqual(route_access('GET','/saved/collections/42'),'user_work')
        self.assertEqual(route_access('GET','/saved/collections/42/members'),'api_read')
        self.assertEqual(route_access('POST','/saved/collections/42/members'),'user_work')
        for path in ('/add-channel','/starter-channels/add','/channels/c/hydration',
                     '/channels/c/backfill-current-stats'):
            self.assertEqual(route_access('POST',path),'api_read')
        self.assertEqual(route_access('GET','/app-explorer-v3/assets/nested/file.js'),'user_work')

if __name__=='__main__':unittest.main()
