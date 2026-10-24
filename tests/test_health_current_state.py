"""Historical errors never clear or invent current work failures."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from youtube_pipeline_health import (classify_health, collect_pipeline_health,
    present_all_type_queue, safe_latest_sync)


class CurrentHealthTests(unittest.TestCase):
    def classify(self, *, stats=None, discovery=None, metadata=None, latest=None, errors=None):
        return classify_health(
            latest or {'status': 'completed', 'errors': 0}, {'age_minutes': 2},
            {'error_total': 2}, {'channel_discovery': discovery or {}, 'channel_metadata': metadata or {}},
            stats or {'all_due': 10, 'scope': 'all_types'},
            {'status_counts': {'transport_error': 1}}, errors or [], False, 90, 120,
            collection_capacity={'status': 'ok', 'stats_limit': 20000})

    def test_historical_errors_do_not_mean_an_active_incident(self):
        self.assertEqual(self.classify(), ('ok', [], []))

    def test_green_unrelated_sync_does_not_clear_current_failures(self):
        for queue in ('stats', 'discovery', 'metadata'):
            with self.subTest(queue=queue):
                status, critical, warnings = self.classify(**{queue: {'failed': 3}})
                self.assertEqual(status, 'warning')
                self.assertFalse(critical)
                self.assertIn('3 current queue entries still have a recorded failure', warnings[0])

    def test_excluded_failures_remain_separate_from_actionable_work(self):
        self.assertEqual(self.classify(stats={'failed': 0, 'excluded_failed': 8})[0], 'ok')

    def test_last_failed_or_incomplete_sync_is_not_hidden(self):
        self.assertEqual(self.classify(latest={'status': 'error', 'errors': 1})[0], 'critical')
        self.assertEqual(self.classify(latest={'status': 'partial', 'errors': 0})[0], 'warning')
        self.assertEqual(self.classify(errors=[{'component': 'stats_queue', 'error_type': 'DatabaseError'}])[0], 'critical')

    def test_missing_all_type_projection_is_unknown_not_zero(self):
        queue = present_all_type_queue(None, datetime.now(timezone.utc))
        self.assertEqual(queue['mode'], 'unavailable')
        self.assertIsNone(queue['all_due'])
        self.assertNotIn('failed', queue)

    def test_known_partial_explanation_is_derived_without_extra_reads(self):
        result = safe_latest_sync({'status': 'partial', 'summary_json': {
            'video_collection': {'metadata_deferred': 7, 'source_view_count_absent': 0,
                'completion_outcome': 'partial', 'incomplete': True, 'unresolved': 7}}},
            datetime.now(timezone.utc))
        self.assertIn({'stage': 'video_collection', 'code': 'metadata_followup', 'count': 7}, result['explanation']['reasons'])
        self.assertEqual(result['explanation']['current_recovery'], 'not_evaluated')

    def test_report_preserves_history_and_fails_closed_on_missing_schema(self):
        now = datetime.now(timezone.utc)
        latest = {'status': 'completed', 'errors': 0, 'finished_at': now.isoformat()}
        with patch('youtube_pipeline_health.read_collection_capacity', return_value={'status': 'ok', 'stats_limit': 20000}), \
             patch('youtube_pipeline_health.SyncRunRepository', return_value=SimpleNamespace(
                 latest=lambda: latest, latest_completed=lambda: latest,
                 list_since=lambda *a, **k: [{'errors': 2, 'status': 'error'}])), \
             patch('youtube_pipeline_health.fetch_paginated', return_value=([], False)), \
             patch('youtube_pipeline_health.get_all_video_queue_health', side_effect=RuntimeError('unavailable')), \
             patch('youtube_pipeline_health.get_all_discovery_queue_health', return_value={'due': 0, 'leased': 0, 'failed': 0}), \
             patch('youtube_pipeline_health.ChannelSyncRepository', return_value=SimpleNamespace(health=lambda: {})), \
             patch('youtube_pipeline_health.YouTubeQuotaRepository', return_value=SimpleNamespace(
                 summarize_day=lambda *a, **k: {'truncated': False, 'status_counts': {'transport_error': 1}})):
            report = collect_pipeline_health(object(), now=now,
                database_video_collection_enabled=True, database_channel_discovery_enabled=True,
                quota_buffer_status_reader=lambda: {'status': 'ok', 'pending_events': 0})
        self.assertEqual(report['status'], 'critical')
        self.assertIsNone(report['queues']['stats']['all_due'])
        self.assertEqual(report['history']['sync_errors'], 2)
        self.assertEqual(report['history']['transport_errors'], 1)
        self.assertEqual(report['history']['recovery'], 'not_correlated')


if __name__ == '__main__':
    unittest.main()
