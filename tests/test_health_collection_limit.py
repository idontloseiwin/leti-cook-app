"""Current INI capacity is shared by both health reports; no runtime or provider calls."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from self_hosted_settings import (browser_environment, preset, read_settings_bytes,
                                  settings_ini, configured_settings_path)
from self_hosted_configuration import default_settings
from youtube_pipeline_health import (classify_health, collect_pipeline_health,
                                     read_collection_capacity, video_queue_capacity_warning)
from http_test_support import endpoint, system_http


class HealthCollectionLimitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'config.ini'
        self.raw = (ROOT / 'config/config.ini').read_bytes()
        environment = patch.dict(os.environ, {
            'LIC_SETTINGS_FILE': str(self.path),
            'LIC_SETUP_DIRECTORY': str(Path(temp.name) / 'unrelated'),
            'LIC_DEPLOYMENT_PROFILE': 'self-hosted',
            'LIC_AUTO_SYNC_STATS_LIMIT': '6000',  # Deliberately stale process snapshot.
            'DATABASE_VIDEO_COLLECTION_ENABLED': 'true',
            'DATABASE_CHANNEL_DISCOVERY_ENABLED': 'true',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.write_limit(20000)

    def write_limit(self, limit):
        self.path.write_bytes(self.raw.replace(b'stats_limit = 20000\n',
                                               f'stats_limit = {limit}\n'.encode()))
        self.path.chmod(0o600)

    def test_fresh_defaults_template_and_worker_agree_inventory_stays_separate(self):
        settings = read_settings_bytes(self.raw)
        self.assertEqual(preset('standard')['stats_limit'], 20000)
        self.assertEqual(settings['limits'], default_settings()['limits'])
        self.assertEqual(settings_ini(settings).encode(), self.raw)
        self.assertEqual(browser_environment(settings)['LIC_AUTO_SYNC_STATS_LIMIT'], '20000')
        self.assertEqual(settings['limits']['inventory_page_budget'], 20000)
        self.assertEqual(browser_environment(settings)['LIC_AUTO_SYNC_INVENTORY_PAGE_BUDGET'], '20000')
        self.assertEqual(preset('test')['stats_limit'], 100)

    def test_current_file_overrides_stale_environment_without_restart(self):
        self.assertEqual(configured_settings_path(os.environ), self.path)
        for limit in (20000, 17321, 6000):
            self.write_limit(limit)
            self.assertEqual(read_collection_capacity(), {'status': 'ok', 'stats_limit': limit})

    def test_missing_invalid_or_unselected_config_never_uses_a_default(self):
        invalid = self.raw.replace(b'stats_limit = 20000', b'stats_limit = nope')
        self.path.write_bytes(invalid)
        self.assertEqual(read_collection_capacity(), {'status': 'unavailable', 'stats_limit': None})
        self.path.unlink()
        self.assertEqual(read_collection_capacity()['status'], 'unavailable')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(read_collection_capacity(), {'status': 'unavailable', 'stats_limit': None})

    def test_setup_directory_fallback_matches_store_path_without_reading_credentials(self):
        with patch.dict(os.environ, {'LIC_SETTINGS_FILE': '', 'LIC_SETUP_DIRECTORY': str(self.path.parent)}):
            self.assertEqual(configured_settings_path(os.environ), self.path)
            self.assertEqual(read_collection_capacity()['stats_limit'], 20000)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_queue_capacity_boundaries_and_unknown_remain_honest(self):
        for limit in (6000, 17321, 20000):
            for due in (0, 6500, limit - 1, limit, limit + 1):
                with self.subTest(limit=limit, due=due):
                    warning = video_queue_capacity_warning({'all_due': due}, {'status': 'ok', 'stats_limit': limit})
                    self.assertEqual(warning is not None, due > limit)
                    if warning:
                        self.assertIn(f'{limit}-video', warning['message'])
                        self.assertIn('Long', warning['message'])
                        self.assertIn('shared by all types', warning['message'])
        for capacity in ({}, {'status': 'unavailable', 'stats_limit': 20000},
                         {'status': 'ok', 'stats_limit': True}):
            self.assertEqual(video_queue_capacity_warning({'all_due': 6500}, capacity)['code'],
                             'collection_limit_unavailable')

    def classify(self, due):
        return classify_health(
            {'status': 'completed', 'errors': 0}, {'age_minutes': 1}, {}, {},
            {'mode': 'leased_video_collection_queue', 'all_due': due}, {}, [], False, 90, 90,
            collection_capacity=read_collection_capacity(),
        )

    def test_pipeline_classifier_uses_configured_limit_not_6000(self):
        self.assertEqual(self.classify(6500), ('ok', [], []))
        self.assertEqual(self.classify(20000), ('ok', [], []))
        self.assertEqual(self.classify(20001)[0], 'warning')
        self.path.unlink()
        status, critical, warnings = self.classify(6500)
        self.assertEqual((status, critical), ('warning', []))
        self.assertIn('capacity is unknown', warnings[0])

    def test_pipeline_response_exposes_current_limit_and_all_type_scope(self):
        with patch('youtube_pipeline_health.SyncRunRepository', return_value=SimpleNamespace(
                 latest=lambda: None, latest_completed=lambda: None, list_since=lambda *a, **k: [])), \
             patch('youtube_pipeline_health.fetch_paginated', return_value=([], False)), \
             patch('youtube_pipeline_health.get_all_video_queue_health', return_value={'due': 6500, 'leased': 0, 'scope': 'all_types'}), \
             patch('youtube_pipeline_health.get_all_discovery_queue_health', return_value={'due': 0, 'leased': 0, 'scope': 'all_types'}), \
             patch('youtube_pipeline_health.ChannelSyncRepository', return_value=SimpleNamespace(health=lambda: {})), \
             patch('youtube_pipeline_health.YouTubeQuotaRepository', return_value=SimpleNamespace(
                 summarize_day=lambda *a, **k: {'truncated': False})):
            report = collect_pipeline_health(object(), database_video_collection_enabled=True,
                database_channel_discovery_enabled=True,
                quota_buffer_status_reader=lambda: {'status': 'ok', 'pending_events': 0})
        self.assertEqual(report['runtime']['stats_limit'], 20000)
        self.assertEqual(report['runtime']['stats_limit_read_status'], 'ok')
        self.assertEqual(report['runtime']['stats_queue_scope'], 'all_types')
        self.assertEqual(report['runtime']['stats_limit_scope'], 'all_types_general_and_stats')
        self.assertFalse(any('allowance' in item for item in report['warnings']))

    def summary(self, due, *, discovery=None, metadata=None):
        values = {'api-quota': {}, 'video-type-review-summary': {'available': True, 'has_open': False}}
        cache = SimpleNamespace(cached_read=lambda key, ttl, load: values[key] if key in values else load())
        router = system_http(cache=cache)
        with patch('web.system.SyncRunRepository', return_value=SimpleNamespace(
                 list_recent=lambda **kw: [{'status': 'completed', 'errors': 0}])), \
             patch('web.system.get_system_queue_health', return_value={'videos': {'due': due, 'scope': 'all_types'}, 'channel_discovery': discovery or {'failed': 0}, 'channel_metadata': metadata or {'failed': 0}}), \
             patch('self_hosted_configuration.key_refusal', return_value=None), \
             patch('self_hosted_configuration.collection_paused', return_value=False), \
             patch('hot_runtime.current_update_copy_status', return_value=None), \
             patch('update_copy_guard.warning_for_status', return_value=None):
            return endpoint(router, 'system_summary')()

    def test_summary_actual_endpoint_uses_current_file_and_explains_unknown(self):
        report = self.summary(6500)
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['queue_health']['collection_capacity']['stats_limit'], 20000)
        self.assertEqual(report['queue_health']['scope'], 'all_types')
        self.write_limit(6000)
        report = self.summary(6500)
        self.assertEqual(report['status_reason'], 'stats_queue_backlog')
        self.path.unlink()
        report = self.summary(6500)
        self.assertEqual(report['status_reason'], 'collection_limit_unavailable')
        self.assertIsNone(report['queue_health']['collection_capacity']['stats_limit'])

    def test_home_keeps_other_queue_failures_after_an_unrelated_success(self):
        for field in ('discovery', 'metadata'):
            with self.subTest(field=field):
                report = self.summary(0, **{field: {'failed': 2}})
                self.assertEqual(report['status'], 'warning')
                self.assertTrue(any(item.get('count') == 2 for item in report['warnings']))
        self.assertEqual(self.summary(0, discovery={'mode': 'disabled'})['status_reason'],
                         'database_channel_discovery_disabled')


if __name__ == '__main__':
    unittest.main()
