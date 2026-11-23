"""Historical errors never clear or invent current work failures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from youtube_pipeline_health import (classify_health, collect_pipeline_health,
    present_all_type_queue, safe_latest_sync)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


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

    def test_last_failed_sync_is_not_hidden_and_partial_work_is_normal(self):
        self.assertEqual(self.classify(latest={'status': 'error', 'errors': 1})[0], 'critical')
        # A partial run without errors carries work over; not a warning.
        self.assertEqual(self.classify(latest={'status': 'partial', 'errors': 0}), ('ok', [], []))
        self.assertEqual(self.classify(errors=[{'component': 'stats_queue', 'error_type': 'DatabaseError'}])[0], 'critical')

    def test_carried_over_evidence_in_the_latest_run_is_not_a_current_warning(self):
        latest = safe_latest_sync({'status': 'partial', 'errors': 0, 'summary_json': {
            'completion_outcome': 'partial', 'incomplete': True, 'unresolved': 7,
            'video_collection': {'metadata_deferred': 7, 'source_view_count_absent': 3,
                'completion_outcome': 'partial', 'incomplete': True, 'unresolved': 7}}}, NOW)
        self.assertEqual(latest['run_kind'], 'carried_over')
        self.assertEqual(self.classify(latest=latest), ('ok', [], []))

    def test_quota_or_rate_limit_stop_waits_in_yellow_and_a_real_failure_stays_red(self):
        def stopped(errors=1, explained=1, **flags):
            # The stop counts the refused request as an error and explains it.
            return safe_latest_sync({'status': 'error', 'errors': errors,
                                     'summary_json': {'errors': errors, 'limit_errors': explained, **flags}}, NOW)
        for flags, warning in (
            ({'general_quota_exhausted': True}, 'General quota is exhausted; counters keep updating through '
                                                'Batch statistics. It resumes at midnight Pacific.'),
            ({'batch_quota_exhausted': True}, 'Batch statistics quota is exhausted; video details, channel profiles '
                                              'and playlist checks continue through General. It resumes at midnight Pacific.'),
            ({'general_quota_exhausted': True, 'batch_quota_exhausted': True},
             'Collection is waiting for YouTube quota; it resumes after the daily reset at midnight Pacific.'),
            ({}, 'Collection is waiting for YouTube quota; it resumes after the daily reset at midnight Pacific.'),
        ):
            with self.subTest(flags=flags):
                status, critical, warnings = self.classify(latest=stopped(quota_exhausted=True, **flags))
                self.assertEqual((status, critical, warnings), ('warning', [], [warning]))
        # An independent error beside the stop stays red, the bucket notwithstanding.
        status, critical, _ = self.classify(latest=stopped(2, 1, quota_exhausted=True, batch_quota_exhausted=True))
        self.assertEqual((status, critical), ('critical', ['latest sync run failed']))
        status, critical, warnings = self.classify(latest=stopped(rate_limited=True))
        self.assertEqual((status, critical, warnings),
                         ('warning', [], ['YouTube asked to slow down; collection resumes on the next sync.']))
        for flags in ({'quota_exhausted': True, 'infrastructure_error': True}, {'rate_limited': True, 'batch_unavailable': True},
                      {'settlement_unknown': True}, {}):
            with self.subTest(flags=flags):
                status, critical, _ = self.classify(latest=stopped(**flags))
                self.assertEqual((status, critical), ('critical', ['latest sync run failed']))

    def classify_at(self, *, latest, full_pass, started, cadence=None):
        return classify_health(
            latest, full_pass, {}, {}, {'all_due': 10, 'scope': 'all_types'}, {}, [], False, 90, 120,
            collection_capacity={'status': 'ok', 'stats_limit': 20000}, last_sync_without_errors=latest,
            now=NOW, server_started_at=started, collection_cadence=cadence or {'interval_minutes': 30, 'automatic_collection': True})

    def stored_run(self, status, minutes_ago, errors=0):
        ended = (NOW - timedelta(minutes=minutes_ago)).isoformat()
        return safe_latest_sync({'status': status, 'errors': errors, 'started_at': ended, 'finished_at': ended}, NOW)

    def test_downtime_is_not_counted_against_the_last_full_pass(self):
        # Last full pass 26 h ago, but the server was off most of that time.
        result = self.classify_at(latest=self.stored_run('partial', 12), full_pass=self.stored_run('completed', 26 * 60),
                                  started=NOW - timedelta(hours=2))
        self.assertEqual(result, ('ok', [], []))

    def test_no_sync_finishing_while_the_server_runs_is_a_warning(self):
        started = NOW - timedelta(hours=3)
        status, critical, warnings = self.classify_at(latest=self.stored_run('completed', 120), full_pass=self.stored_run('completed', 120), started=started)
        self.assertEqual((status, critical), ('warning', []))
        self.assertEqual(warnings, ['No sync has finished for 2 h while the server was running. Check that automatic collection is running.'])
        # Up to 90 minutes, three 30-minute intervals, is normal; time before the start is not counted.
        self.assertEqual(self.classify_at(latest=self.stored_run('completed', 89), full_pass=self.stored_run('completed', 89), started=started)[0], 'ok')
        self.assertEqual(self.classify_at(latest=self.stored_run('completed', 600), full_pass=self.stored_run('completed', 600),
                                          started=NOW - timedelta(minutes=80))[0], 'ok')
        paused = {'interval_minutes': 30, 'automatic_collection': False}
        self.assertEqual(self.classify_at(latest=self.stored_run('completed', 600), full_pass=self.stored_run('completed', 600),
                                          started=started, cadence=paused)[0], 'ok')

    def test_a_day_of_running_time_without_a_full_pass_is_a_warning(self):
        status, critical, warnings = self.classify_at(latest=self.stored_run('partial', 5), full_pass=self.stored_run('completed', 25 * 60),
                                                      started=NOW - timedelta(hours=30))
        self.assertEqual((status, critical), ('warning', []))
        self.assertEqual(warnings, ['No full pass in 24 h of running time; work is accumulating. Check quota use and the number of channels.'])
        self.assertEqual(self.classify_at(latest=self.stored_run('partial', 5), full_pass=None, started=NOW - timedelta(hours=24, minutes=1))[1:],
                         ([], ['No full pass in 24 h of running time; work is accumulating. Check quota use and the number of channels.']))
        self.assertEqual(self.classify_at(latest=self.stored_run('partial', 5), full_pass=self.stored_run('completed', 23 * 60 + 59),
                                          started=NOW - timedelta(hours=30))[0], 'ok')

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
                 latest=lambda: latest, latest_completed=lambda: latest, latest_without_errors=lambda: latest,
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
        self.assertEqual(report['quota_control']['reason'], 'quota_state_unavailable')
        self.assertNotIn('exhausted_buckets', report['youtube_quota'])
        self.assertTrue(any('Quota state is unavailable' in warning for warning in report['warnings']))

    def report_at(self, runs, *, started):
        with patch('youtube_pipeline_health.read_provider_quota_control', return_value={
                 'status': 'ok', 'exhausted_buckets': {'general': False, 'video_batch_get_stats': False}}), \
             patch('youtube_pipeline_health.read_collection_capacity', return_value={'status': 'ok', 'stats_limit': 20000}), \
             patch('youtube_pipeline_health.read_collection_cadence', return_value={
                 'status': 'ok', 'interval_minutes': 30, 'automatic_collection': True}), \
             patch('youtube_pipeline_health.SyncRunRepository', return_value=runs), \
             patch('youtube_pipeline_health.fetch_paginated', return_value=([], False)), \
             patch('youtube_pipeline_health.get_all_video_queue_health', return_value={'due': 10, 'leased': 0, 'failed': 0}), \
             patch('youtube_pipeline_health.get_all_discovery_queue_health', return_value={'due': 0, 'leased': 0, 'failed': 0}), \
             patch('youtube_pipeline_health.ChannelSyncRepository', return_value=SimpleNamespace(health=lambda: {})), \
             patch('youtube_pipeline_health.YouTubeQuotaRepository', return_value=SimpleNamespace(
                 summarize_day=lambda *a, **k: {'truncated': False, 'status_counts': {}},
                 exhausted_buckets=lambda: ())):
            return collect_pipeline_health(object(), now=NOW, server_started_at=started,
                database_video_collection_enabled=True, database_channel_discovery_enabled=True,
                quota_buffer_status_reader=lambda: {'status': 'ok', 'pending_events': 0})

    def test_summary_after_downtime_is_healthy_with_a_neutral_gap_line(self):
        at = lambda hours: (NOW - timedelta(hours=hours)).isoformat()
        partial = {'id': 3, 'status': 'partial', 'errors': 0, 'started_at': at(0.4), 'finished_at': at(0.2),
                   'summary_json': {'completion_outcome': 'partial', 'incomplete': True, 'unresolved': 12}}
        full = {'id': 1, 'status': 'completed', 'errors': 0, 'started_at': at(26.5), 'finished_at': at(26)}
        before = {'id': 2, 'status': 'partial', 'errors': 0, 'started_at': at(24.5), 'finished_at': at(24)}
        cutoffs = []
        runs = SimpleNamespace(latest=lambda: partial, latest_completed=lambda: full, latest_without_errors=lambda: partial,
                               latest_started_before=lambda moment: cutoffs.append(moment) or before,
                               list_since=lambda *a, **k: [partial])
        report = self.report_at(runs, started=NOW - timedelta(hours=2))
        self.assertEqual((report['status'], report['critical'], report['warnings']), ('ok', [], []))
        self.assertEqual(cutoffs, [(NOW - timedelta(hours=2)).isoformat()])
        self.assertEqual(report['latest_sync']['run_kind'], 'carried_over')
        self.assertEqual(report['last_sync_without_errors']['finished_at'], at(0.2))
        self.assertEqual(report['last_successful_sync']['finished_at'], at(26))
        self.assertEqual(report['server_started_at'], (NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(report['collection_gap'], {'server_started_at': (NOW - timedelta(hours=2)).isoformat(),
                                                    'collection_last_ran_at': at(24), 'hours': 22.0})
        # The command line has no server start: no gap, and the clock alone measures.
        report = self.report_at(runs, started=None)
        self.assertIsNone(report['collection_gap'])
        self.assertEqual(report['warnings'], ['No full pass in 24 h of running time; work is accumulating. Check quota use and the number of channels.'])


if __name__ == '__main__':
    unittest.main()
