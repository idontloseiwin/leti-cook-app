"""Paused collection is shown as paused, not as an outage (tester finding T-30). No server or database."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from self_hosted_configuration import ConfigurationStore, collection_paused  # noqa: E402

FAILED_RUNS = {'status': 'critical', 'critical': ['latest sync run failed'], 'warnings': []}


from types import SimpleNamespace
from http_test_support import endpoint, system_http


def health(*, paused, refusal=None):
    router = system_http()
    with patch('self_hosted_configuration.key_refusal', return_value=refusal), \
         patch('self_hosted_configuration.collection_paused', return_value=paused), \
         patch('web.system.collect_pipeline_health', return_value=dict(FAILED_RUNS)):
        return endpoint(router, 'system_pipeline_health')()


def summary(*, paused, refusal=None):
    values = {'system-queue-health': {}, 'api-quota': {},
              'video-type-review-summary': {'available': True, 'has_open': False}}
    cache = SimpleNamespace(cached_read=lambda key, ttl, load: values[key] if key in values else load())
    router = system_http(cache=cache)
    repository = SimpleNamespace(list_recent=lambda **kw: [{'status': 'error', 'errors': 3}])
    with patch('self_hosted_configuration.key_refusal', return_value=refusal), \
         patch('self_hosted_configuration.collection_paused', return_value=paused), \
         patch('web.system.SyncRunRepository', return_value=repository):
        return endpoint(router, 'system_summary')()


class PausedReportTests(unittest.TestCase):
    def test_a_pause_is_not_reported_as_an_outage(self):
        report = health(paused=True)
        self.assertEqual((report['status'], report['collection']), ('paused', {'state': 'paused'}))
        self.assertEqual(report['critical'], ['latest sync run failed'])  # kept as history
        result = summary(paused=True)
        self.assertEqual((result['status'], result['status_reason']), ('paused', 'collection_paused'))
        self.assertIn('new channels wait', result['warnings'][0]['message'])
        self.assertEqual(result['collection'], {'state': 'paused'})

    def test_running_collection_keeps_its_own_status(self):
        self.assertEqual(health(paused=False), FAILED_RUNS)
        result = summary(paused=False)
        self.assertEqual((result['status'], result['collection']), ('error', None))

    def test_a_refused_key_outranks_the_pause(self):
        self.assertEqual(health(paused=True, refusal='provider_key_rejected')['status'], 'critical')
        result = summary(paused=True, refusal='provider_key_rejected')
        self.assertEqual((result['status'], result['collection']), ('error', None))


class PausedSettingTests(unittest.TestCase):
    def test_the_pause_is_read_from_config_ini(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'LIC_SETUP_DIRECTORY': directory}):
            store = ConfigurationStore.initialize(directory)
            store.update(0, lambda v: v.update(api_key='synthetic_test_key_no_requests', prepared=True, phase='ready'))
            self.assertFalse(collection_paused())
            config = Path(directory, 'config.ini')
            text = config.read_text(encoding='utf-8')
            self.assertIn('automatic_collection = true', text)
            config.write_text(text.replace('automatic_collection = true', 'automatic_collection = false'), encoding='utf-8')
            self.assertTrue(collection_paused())


class UploadsLimitTests(unittest.TestCase):
    """YouTube lists only a channel's latest 20,000 uploads (tester finding T-29)."""

    def test_the_limit_is_explained_where_it_shows(self):
        page = (ROOT / 'application/frontend/src/explorer/ExplorerWorkspace.tsx').read_text(encoding='utf-8')
        self.assertIn('const YOUTUBE_UPLOADS_LIST_LIMIT = 20_000;', page)
        self.assertIn('youtubeVideoTotal > YOUTUBE_UPLOADS_LIST_LIMIT', page)
        data = (ROOT / 'docs/DATA.md').read_text(encoding='utf-8')
        self.assertIn('latest 20,000 uploads', data)
        self.assertIn('https://issuetracker.google.com/issues/166292064', data)


if __name__ == '__main__':
    unittest.main()
