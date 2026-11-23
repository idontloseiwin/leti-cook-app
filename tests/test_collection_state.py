"""Honest collection state: one rule for Health and Home. No server or database."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
import collection_state  # noqa: E402
from collection_outcome import COLLECTION_STOP_FLAGS  # noqa: E402
from collection_state import collection_gap, collection_signals, run_kind  # noqa: E402
from db.runs.sync_run_summary import persisted_summary_copy  # noqa: E402
from operational_copy_policy import safe_summary_copy  # noqa: E402
from sync_run_summary import compact_sync_run_summary  # noqa: E402
from http_test_support import endpoint, system_http  # noqa: E402

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
QUOTA = 'Collection is waiting for YouTube quota; it resumes after the daily reset at midnight Pacific.'
RATE = 'YouTube asked to slow down; collection resumes on the next sync.'
GENERAL = 'General quota is exhausted; counters keep updating through Batch statistics. It resumes at midnight Pacific.'
STATS = ('Batch statistics quota is exhausted; video details, channel profiles and playlist checks continue '
         'through General. It resumes at midnight Pacific.')
NO_FULL = 'No full pass in 24 h of running time; work is accumulating. Check quota use and the number of channels.'


def ago(**delta):
    return NOW - timedelta(**delta)


def codes(**values):
    base = dict(latest_kind='carried_over', latest_finished_at=ago(minutes=5), last_full_at=ago(minutes=5),
                now=NOW, server_started_at=ago(days=3), interval_minutes=30, automatic=True)
    return [item['code'] for item in collection_signals(**{**base, **values})]


class RunKindTests(unittest.TestCase):
    def test_each_stored_ending_has_one_kind(self):
        # The refused request is the one error, and the stop explains it.
        error = lambda **flags: {'status': 'error', 'errors': 1, 'summary_json': {'errors': 1, 'limit_errors': 1, **flags}}
        for row, kind in (
            ({'status': 'running', 'errors': 0}, 'running'),
            ({'status': 'completed', 'errors': 0}, 'complete'),
            ({'status': 'partial', 'errors': 0, 'summary_json': {'yield_requested': True}}, 'carried_over'),
            (error(quota_exhausted=True), 'quota'),
            (error(quota_exhausted=True, batch_quota_exhausted=True), 'quota'),
            ({'status': 'error', 'errors': 0, 'summary_json': {'quota_exhausted': True}}, 'quota'),
            (error(rate_limited=True), 'rate_limited'),
            (error(quota_exhausted=True, infrastructure_error=True), 'failed'),
            (error(rate_limited=True, settlement_unknown=True), 'failed'),
            (error(quota_exhausted=True, stale_run_recovered=True), 'failed'),
            (error(), 'failed'),
            (error(quota_exhausted='true'), 'failed'),
            ({'status': 'completed', 'errors': 2}, 'failed'),
            ({'status': 'partial', 'errors': 1, 'summary_json': {'quota_exhausted': True}}, 'failed'),
            ({'status': 'completed', 'errors': 'many'}, 'failed'),
            ({}, 'failed'), (None, 'failed'),
        ):
            with self.subTest(row=row):
                self.assertEqual(run_kind(row), kind)

    def test_any_other_stop_flag_including_a_new_one_is_a_failure(self):
        limits = set(collection_state.QUOTA_STOP_FLAGS) | set(collection_state.RATE_STOP_FLAGS)
        self.assertEqual(set(COLLECTION_STOP_FLAGS) - limits - set(collection_state.FAILURE_STOP_FLAGS), set())
        self.assertTrue(limits.isdisjoint(collection_state.FAILURE_STOP_FLAGS))

    def test_stored_runs_keep_the_marks_that_tell_a_limit_stop_from_a_failure(self):
        # Persist the flags that identify a rate-limit refusal.
        marks = {name: True for name in ('rate_limited', 'general_quota_exhausted', 'batch_quota_exhausted',
                                         'settlement_unknown', 'batch_unavailable', 'hot_runtime_interrupted')}
        summary = {'errors': 1, 'completion_outcome': 'error', **marks, 'video_collection': {'rate_limited': True}}
        stored = persisted_summary_copy(compact_sync_run_summary(summary))
        self.assertEqual({name: stored.get(name) for name in marks}, marks)
        self.assertTrue(stored['video_collection']['rate_limited'])
        self.assertEqual(safe_summary_copy({'rate_limited': True, 'stopped_reason': 'x'}), {'rate_limited': True})
        read = safe_summary_copy(persisted_summary_copy(compact_sync_run_summary(
            {'errors': 1, 'limit_errors': 1, 'rate_limited': True})))
        self.assertEqual(read['limit_errors'], 1)
        self.assertEqual(run_kind({'status': 'error', 'errors': 1, 'summary_json': read}), 'rate_limited')


class SignalTests(unittest.TestCase):
    def test_partial_work_without_errors_raises_nothing(self):
        self.assertEqual(codes(), [])
        self.assertEqual(codes(latest_kind='complete'), [])

    def test_failure_and_limit_stops(self):
        self.assertEqual(collection_signals(latest_kind='failed', latest_finished_at=NOW, last_full_at=NOW, now=NOW),
                         [{'code': 'latest_run_failed', 'level': 'critical', 'message': 'latest sync run failed'}])
        self.assertEqual(collection_signals(latest_kind='quota', latest_finished_at=NOW, last_full_at=NOW, now=NOW),
                         [{'code': 'collection_waiting_for_quota', 'level': 'warning', 'message': QUOTA}])
        # One exhausted bucket is named with the work that goes on; both or unnamed keep the general notice.
        for bucket, code, message in (('general', 'collection_waiting_for_general_quota', GENERAL),
                                      ('stats', 'collection_waiting_for_stats_quota', STATS),
                                      (None, 'collection_waiting_for_quota', QUOTA),
                                      ('unknown', 'collection_waiting_for_quota', QUOTA)):
            with self.subTest(bucket=bucket):
                self.assertEqual(collection_signals(latest_kind='quota', latest_finished_at=NOW, last_full_at=NOW,
                                                    now=NOW, quota_wait_bucket=bucket),
                                 [{'code': code, 'level': 'warning', 'message': message}])
        # The bucket names only a quota stop.
        self.assertEqual(collection_signals(latest_kind='rate_limited', latest_finished_at=NOW, last_full_at=NOW,
                                            now=NOW, quota_wait_bucket='general')[0]['message'], RATE)
        self.assertEqual(collection_signals(latest_kind='rate_limited', latest_finished_at=NOW, last_full_at=NOW, now=NOW),
                         [{'code': 'collection_rate_limited', 'level': 'warning', 'message': RATE}])

    def test_home_has_a_short_name_for_every_quota_notice(self):
        source = (ROOT / 'application/frontend/src/explorer/homeStatus.ts').read_text(encoding='utf-8')
        for code, _ in collection_state.QUOTA_WAIT_SIGNALS.values():
            with self.subTest(code=code):
                self.assertRegex(source, rf"\n  {code}: '")

    def test_no_sync_finishing_counts_running_time_only(self):
        self.assertEqual(codes(latest_finished_at=ago(minutes=89, seconds=59)), [])
        self.assertEqual(codes(latest_finished_at=ago(minutes=90)), ['collection_not_finishing'])
        # Three intervals when longer than 90 minutes; the CLI threshold is the floor.
        self.assertEqual(codes(latest_finished_at=ago(minutes=100), interval_minutes=40), [])
        self.assertEqual(codes(latest_finished_at=ago(minutes=120), interval_minutes=40), ['collection_not_finishing'])
        self.assertEqual(codes(latest_finished_at=ago(minutes=50), min_silence_minutes=45, interval_minutes=None), ['collection_not_finishing'])
        # Measured from the backend start when the run ended before it.
        self.assertEqual(codes(latest_finished_at=ago(hours=30), server_started_at=ago(minutes=89)), [])
        self.assertEqual(codes(latest_finished_at=ago(hours=30), server_started_at=ago(minutes=91)), ['collection_not_finishing'])
        # A running sync, a pause, or no run at all is not "not finishing".
        for values in ({'latest_kind': 'running'}, {'automatic': False}, {'latest_kind': None}):
            with self.subTest(values=values):
                self.assertEqual(codes(latest_finished_at=ago(hours=5), **values), [])
        self.assertEqual(codes(latest_finished_at=ago(hours=5), automatic=None), ['collection_not_finishing'])
        message = collection_signals(latest_kind='complete', latest_finished_at=ago(minutes=95), last_full_at=ago(minutes=95),
                                     now=NOW, server_started_at=ago(days=1))[0]['message']
        self.assertEqual(message, 'No sync has finished for 95 min while the server was running. Check that automatic collection is running.')

    def test_no_full_pass_needs_a_day_of_running_time(self):
        self.assertEqual(codes(last_full_at=ago(hours=23, minutes=59)), [])
        self.assertEqual(codes(last_full_at=ago(hours=24)), ['no_full_pass'])
        self.assertEqual(codes(last_full_at=ago(hours=26), server_started_at=ago(hours=2)), [])
        self.assertEqual(codes(last_full_at=None, server_started_at=ago(hours=24)), ['no_full_pass'])
        self.assertEqual(codes(last_full_at=None, server_started_at=None), [])
        self.assertEqual(codes(last_full_at=ago(days=3), full_pass_known=False), [])
        self.assertEqual(codes(latest_kind='running', last_full_at=ago(days=3)), ['no_full_pass'])
        self.assertEqual(codes(latest_kind='quota', last_full_at=ago(days=3)), ['collection_waiting_for_quota', 'no_full_pass'])

    def test_downtime_line_only_after_an_hour_without_collection(self):
        started = ago(hours=2)
        self.assertIsNone(collection_gap(started, {'started_at': ago(hours=3), 'finished_at': ago(hours=2, minutes=59)}))
        self.assertEqual(collection_gap(started, {'started_at': ago(hours=3), 'finished_at': ago(hours=3, minutes=1)}),
                         {'server_started_at': started.isoformat(), 'collection_last_ran_at': ago(hours=3, minutes=1).isoformat(),
                          'hours': 1.0})
        # A run cut off by the stop is recovered after the start: its start is the last known activity.
        self.assertEqual(collection_gap(started, {'started_at': ago(hours=26).isoformat(), 'finished_at': ago(hours=1).isoformat()})['hours'], 24.0)
        for value in (None, {}, {'started_at': ago(hours=1)}):
            self.assertIsNone(collection_gap(started, value))
        self.assertIsNone(collection_gap(None, {'started_at': ago(hours=30)}))

    def test_backend_start_is_recorded_by_the_app_lifespan(self):
        with patch.object(collection_state, '_server_started_at', None):
            self.assertIsNone(collection_state.server_started_at())
            self.assertEqual(collection_state.note_server_started('2026-11-08T10:00:00Z'), ago(hours=2))
            self.assertEqual(collection_state.server_started_at(), ago(hours=2))
        source = (ROOT / 'application/web/lifecycle.py').read_text(encoding='utf-8')
        lifespan = source[source.index('async def app_lifespan(app):'):]
        self.assertLess(lifespan.index('note_server_started()'), lifespan.index('try:'))


class HomeStatusTests(unittest.TestCase):
    """Home's System card through the real /system-summary endpoint."""

    def setUp(self):
        for setting in (
            patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted', 'DATABASE_VIDEO_COLLECTION_ENABLED': 'true',
                                    'DATABASE_CHANNEL_DISCOVERY_ENABLED': 'true'}),
            patch.object(collection_state, '_server_started_at', ago(days=3)),
            patch('web.system.read_collection_cadence', return_value={'status': 'ok', 'interval_minutes': 30, 'automatic_collection': True}),
            patch('web.system.read_collection_capacity', return_value={'status': 'ok', 'stats_limit': 20000}),
            patch('web.system.get_system_queue_health', return_value={'videos': {'due': 50, 'failed': 0, 'scope': 'all_types'},
                  'channel_discovery': {'failed': 0}, 'channel_metadata': {'failed': 0}}),
            patch('web.system.datetime', SimpleNamespace(now=lambda tz=None: NOW)),
            patch('self_hosted_configuration.key_refusal', return_value=None),
            patch('self_hosted_configuration.collection_paused', return_value=False),
            patch('hot_runtime.current_update_copy_status', return_value=None),
            patch('update_copy_guard.warning_for_status', return_value=None),
        ):
            setting.start()
            self.addCleanup(setting.stop)

    def summary(self, latest, *, full_pass=None, full_pass_error=None):
        values = {'api-quota': {}, 'video-type-review-summary': {'available': True, 'has_open': False}}
        cache = SimpleNamespace(cached_read=lambda key, ttl, load: values[key] if key in values else load())

        def latest_completed():
            if full_pass_error:
                raise full_pass_error
            return full_pass
        repository = SimpleNamespace(list_recent=lambda **kw: [latest], latest_completed=latest_completed)
        with patch('web.system.SyncRunRepository', return_value=repository), patch('builtins.print'):
            report = endpoint(system_http(cache=cache), 'system_summary')()
        return report['status'], report['status_reason'], [item['message'] for item in report['warnings']]

    def stored(self, status, *, minutes_ago, errors=0, **flags):
        ended = ago(minutes=minutes_ago).isoformat()
        return {'status': status, 'errors': errors, 'started_at': ended, 'finished_at': ended,
                'summary_json': {'errors': errors, **flags}}

    def test_partial_without_errors_is_healthy_even_26_hours_after_the_full_pass_with_downtime(self):
        with patch.object(collection_state, '_server_started_at', ago(hours=2)):
            self.assertEqual(self.summary(self.stored('partial', minutes_ago=12), full_pass=self.stored('completed', minutes_ago=26 * 60)),
                             ('ok', 'latest_run_carried_over', []))
        self.assertEqual(self.summary(self.stored('completed', minutes_ago=12), full_pass=self.stored('completed', minutes_ago=12)),
                         ('ok', 'latest_run_completed', []))

    def test_limit_stops_are_warnings_and_failures_are_errors(self):
        full = self.stored('completed', minutes_ago=60)
        stop = lambda errors=1, explained=1, **flags: self.stored('error', minutes_ago=5, errors=errors,
                                                                 limit_errors=explained, **flags)
        self.assertEqual(self.summary(stop(quota_exhausted=True), full_pass=full),
                         ('warning', 'collection_waiting_for_quota', [QUOTA]))
        self.assertEqual(self.summary(stop(rate_limited=True), full_pass=full),
                         ('warning', 'collection_rate_limited', [RATE]))
        # One exhausted bucket: Home names it and what continues.
        self.assertEqual(self.summary(stop(quota_exhausted=True, general_quota_exhausted=True), full_pass=full),
                         ('warning', 'collection_waiting_for_general_quota', [GENERAL]))
        self.assertEqual(self.summary(stop(quota_exhausted=True, batch_quota_exhausted=True), full_pass=full),
                         ('warning', 'collection_waiting_for_stats_quota', [STATS]))
        self.assertEqual(self.summary(stop(2, 2, quota_exhausted=True, general_quota_exhausted=True,
                                           batch_quota_exhausted=True), full_pass=full),
                         ('warning', 'collection_waiting_for_quota', [QUOTA]))
        # Another error beside the stop is a failure, not a wait.
        for flags in ({'quota_exhausted': True, 'batch_quota_exhausted': True}, {'rate_limited': True}):
            with self.subTest(flags=flags):
                self.assertEqual(self.summary(stop(2, 1, **flags), full_pass=full), ('error', 'latest_run_failed', []))
        # A run recorded before the count shows a stop with errors as failed.
        self.assertEqual(self.summary(self.stored('error', minutes_ago=5, errors=1, quota_exhausted=True), full_pass=full),
                         ('error', 'latest_run_failed', []))
        self.assertEqual(self.summary(self.stored('error', minutes_ago=5, errors=1, infrastructure_error=True), full_pass=full),
                         ('error', 'latest_run_failed', []))

    def test_work_piling_up_or_no_sync_finishing_is_a_warning(self):
        self.assertEqual(self.summary(self.stored('partial', minutes_ago=5), full_pass=self.stored('completed', minutes_ago=25 * 60)),
                         ('warning', 'no_full_pass', [NO_FULL]))
        status, reason, _ = self.summary(self.stored('completed', minutes_ago=180), full_pass=self.stored('completed', minutes_ago=180))
        self.assertEqual((status, reason), ('warning', 'collection_not_finishing'))
        # An unread full pass is unknown, not "never": no warning from it.
        self.assertEqual(self.summary(self.stored('partial', minutes_ago=5), full_pass_error=RuntimeError('PRIVATE')),
                         ('ok', 'latest_run_carried_over', []))


if __name__ == '__main__':
    unittest.main()
