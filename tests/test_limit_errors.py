"""A quota or rate-limit stop waits in yellow only when it explains every error.

Each run counts, next to its errors, the ones that a quota or HTTP 429
refusal explains (``limit_errors``). Here: the actual collection services,
steps and pass with synthetic YouTube answers, the actual write and read of a
stored run, then the one Health and Home rule (collection_state.run_kind). No
database, network or key.
"""
from functools import partial
from unittest.mock import Mock, patch
import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
import requests
import collection_outcome
import collection_state
import provider_runtime
import quota_buckets
from collection import channel_metadata as metadata
from collection import cycle
from collection.inventory import run_typed_inventory_batch
from collection.runs import sync_run_counts_from_summary
from collection_outcome import sync_completion_fields
from collection_state import run_kind
from db import VideoCollectionClaim
from db.runs.sync_run_summary import (normalize_sync_run_summary_text, persisted_summary_copy,
    persisted_summary_text, project_sync_run_summary)
from operational_copy_policy import safe_summary_copy
from sync_run_summary import compact_sync_run_summary
from video_collection_service import VideoCollectionService
from youtube_batch_stats import is_batch_stats_quota_error
from youtube_errors import YouTubeDailyQuotaExceededError, YouTubeRateLimitError, is_youtube_daily_quota_exhausted
# Fixture modules, not their test classes: importing a class would run its tests here again.
import test_auto_all_phases as all_phases
import test_channel_metadata_steps as metadata_steps
import test_collection_phase_stop as phase_stop
import test_initial_video_parallel as initial_parallel
import test_quota_split as quota_split
import test_shared_inventory_engine as inventory_engine

QUOTA_DETECTOR = partial(is_youtube_daily_quota_exhausted, is_batch_stats_quota_error=is_batch_stats_quota_error)
STAMP = '2026-11-08T12:00:00+00:00'
COLUMNS = ('errors', 'started_at', 'finished_at', 'metadata_processed', 'metadata_updated', 'video_count_changed',
           'discovery_processed', 'new_video_stats_updated', 'regular_stats_requested', 'regular_stats_updated')


def stored_run(summary):
    """The run as Health and Home read it back: finish_sync_run's values,
    the writer's normalization and persisted projection, then the reader's."""
    summary = dict(summary)
    summary.update(sync_completion_fields(summary))
    values = sync_run_counts_from_summary(summary)
    values.update(started_at=summary.get('started_at') or STAMP, finished_at=summary.get('finished_at') or STAMP)
    columns = {name: values[name] for name in COLUMNS}
    normalized = normalize_sync_run_summary_text(json.dumps(values['summary_json']), json.dumps(columns))
    row = {**columns, 'status': summary['completion_outcome'], 'summary_json': json.loads(persisted_summary_text(normalized))}
    return {**row, 'summary_json': safe_summary_copy(project_sync_run_summary(row))}


def cycle_summary_chain(summary):
    """Compact, persisted and safe copies of the cycle summary."""
    return {'status': 'error', 'errors': summary['errors'],
            'summary_json': safe_summary_copy(persisted_summary_copy(compact_sync_run_summary(summary)))}


def counted(summary):
    return summary['errors'], summary.get('limit_errors')


def profile_step(error=None, *, channel=None, quota_detector=False):
    """The actual channel-profile step for one channel; ``error`` from channels.list."""
    case = metadata_steps.MetadataProviderStepsTests()
    case.setUp()
    try:
        if quota_detector:
            case.context.sources.is_youtube_daily_quota_exhausted = QUOTA_DETECTOR
        if error is not None:
            def fetch(ids):
                raise error
            case.context.sources.fetch_channel_metadata_batch = fetch
        with patch.object(metadata, 'mark_channel_metadata_error'):
            return case.drive(case.steps([channel] if channel is not None else None))
    finally:
        case.doCleanups()


class RunKindRuleTests(unittest.TestCase):
    def test_a_limit_stop_waits_only_when_it_explains_every_error(self):
        def row(errors, explained, status='error', **flags):
            summary = dict(flags)
            if explained is not ...:
                summary['limit_errors'] = explained
            return {'status': status, 'errors': errors, 'summary_json': summary}
        for value, kind in (
            (row(1, 1, batch_quota_exhausted=True, quota_exhausted=True), 'quota'),
            # Several refusals can share one cause: not errors > 1.
            (row(8, 8, general_quota_exhausted=True, quota_exhausted=True), 'quota'),
            (row(0, 0, general_quota_exhausted=True, quota_exhausted=True), 'quota'),
            (row(1, 1, rate_limited=True), 'rate_limited'),
            (row(2, 2, rate_limited=True, batch_quota_exhausted=True, quota_exhausted=True), 'quota'),
            (row(2, 1, batch_quota_exhausted=True, quota_exhausted=True), 'failed'),
            (row(2, 1, rate_limited=True), 'failed'),
            (row(1, 0, quota_exhausted=True), 'failed'),
            (row(1, 2, quota_exhausted=True), 'failed'),
            (row(1, True, quota_exhausted=True), 'failed'),
            (row(1, -1, quota_exhausted=True), 'failed'),
            (row(1, '1', quota_exhausted=True), 'failed'),
            (row(1, 1, quota_exhausted=True, infrastructure_error=True), 'failed'),
            (row(1, 1), 'failed'),
            # Runs recorded before the count: a stop waits only without errors.
            (row(1, ..., quota_exhausted=True, batch_quota_exhausted=True), 'failed'),
            (row(1, ..., rate_limited=True), 'failed'),
            (row(0, ..., quota_exhausted=True), 'quota'),
            (row(0, ..., status='partial'), 'carried_over'),
            (row(0, ..., status='completed'), 'complete'),
            (row(0, 0, status='partial'), 'carried_over'),
            (row(0, 0, status='completed'), 'complete'),
        ):
            with self.subTest(row=value):
                self.assertEqual(run_kind(value), kind)

    def test_the_one_exhausted_bucket_of_a_stored_run(self):
        bucket = collection_state.quota_wait_bucket
        for summary, expected in (
            ({'general_quota_exhausted': True, 'quota_exhausted': True}, 'general'),
            ({'batch_quota_exhausted': True, 'quota_exhausted': True}, 'stats'),
            ({'general_quota_exhausted': True, 'batch_quota_exhausted': True, 'quota_exhausted': True}, None),
            ({'quota_exhausted': True}, None),
            ({'general_quota_exhausted': 'true'}, None),
        ):
            with self.subTest(summary=summary):
                self.assertEqual(bucket({'summary_json': summary}), expected)
        self.assertIsNone(bucket(None))

    def test_a_stop_counted_as_an_error_is_explained_only_by_a_limit(self):
        count = collection_outcome.count_stop_as_error
        for flags, explained in (
            ({'rate_limited': True}, 1),
            ({'quota_exhausted': True}, 1),
            ({'general_quota_exhausted': True, 'quota_exhausted': True}, 1),
            ({'batch_unavailable': True}, 0),
            ({'rate_limited': True, 'batch_unavailable': True}, 0),
            ({'quota_exhausted': True, 'infrastructure_error': True}, 0),
            ({'rate_limited': True, 'hot_runtime_interrupted': True}, 0),
            ({'settlement_unknown': True}, 0),
        ):
            with self.subTest(flags=flags):
                summary = {'errors': 0, 'limit_errors': 0}
                count(summary, flags)
                self.assertEqual(counted(summary), (1, explained))
        # An already counted error is not counted again, whatever explains it.
        for summary in ({'errors': 1, 'limit_errors': 0}, {'errors': 3, 'limit_errors': 3}):
            before = dict(summary)
            count(summary, {'rate_limited': True})
            self.assertEqual(summary, before)

    def test_the_count_is_written_and_read_back(self):
        summary = {'started_at': STAMP, 'finished_at': STAMP, 'errors': 3, 'limit_errors': 2,
                   'quota_exhausted': True, 'batch_quota_exhausted': True,
                   'video_collection': {'errors': 2, 'limit_errors': 2, 'updated': 5, 'batch_quota_exhausted': True},
                   'channel_metadata': {'processed': 1, 'updated': 1, 'errors': 1, 'limit_errors': 0},
                   'timings': {'total_seconds': 1.5}}
        row = stored_run(summary)
        self.assertEqual(row['errors'], 3)
        self.assertEqual(row['summary_json']['limit_errors'], 2)
        self.assertEqual(row['summary_json']['video_collection']['limit_errors'], 2)
        self.assertEqual(row['summary_json']['channel_metadata']['limit_errors'], 0)
        self.assertEqual(cycle_summary_chain(summary)['summary_json']['limit_errors'], 2)
        # Numeric only: it never carries text.
        self.assertEqual(safe_summary_copy({'limit_errors': 'two', 'errors': 1}), {'errors': 1})
        self.assertEqual(run_kind(row), 'failed')


class PassScenarioTests(unittest.TestCase):
    """The actual pass and its steps; the stored run as Health and Home read it."""

    def setUp(self):
        # These scenarios supply provider outcomes over a database-free cycle
        # fixture. Its quota control read is known available; separate control
        # tests exercise unavailable storage and its distinct failed run.
        quota_state=patch.object(quota_buckets,'read_remembered',return_value=())
        quota_state.start()
        self.addCleanup(quota_state.stop)

    def tearDown(self):
        self.assertFalse(quota_buckets.active())

    def test_stats_quota_beside_a_failed_profile_read_is_a_failure(self):
        # Stats refuses with quotaExceeded and
        # reading channel profiles meets an HTTP 500 that sets no stop flag.
        video, _ = phase_stop.run_video_phase('403-quota')
        profile = profile_step(requests.HTTPError('synthetic HTTP 500'))
        self.assertEqual(counted(profile), (1, 0))
        _, summary = phase_stop.run_pass(video, metadata_summary=profile)
        self.assertEqual(counted(summary), (2, 1))
        self.assertTrue(summary['batch_quota_exhausted'])
        self.assertEqual(run_kind(cycle_summary_chain(summary)), 'failed')
        self.assertEqual(run_kind(stored_run(summary)), 'failed')

    def test_stats_quota_alone_waits_for_its_bucket(self):
        video, _ = phase_stop.run_video_phase('403-quota')
        self.assertEqual(counted(video), (1, 1))
        _, summary = phase_stop.run_pass(video, metadata_summary=profile_step())
        row = stored_run(summary)
        self.assertEqual((row['errors'], row['summary_json']['limit_errors']), (1, 1))
        self.assertEqual((run_kind(row), collection_state.quota_wait_bucket(row)), ('quota', 'stats'))

    def test_general_quota_beside_an_independent_error_is_a_failure(self):
        class PrepareFails(quota_split.Repository):
            def reconcile_typed(self, video_type, limit):
                raise OSError('synthetic queue preparation failure')
        _, summary, _ = quota_split.run_pass(quota_split.Provider(general='quota'), repository=PrepareFails())
        # Two failed queue preparations (Shorts, live) and one General refusal.
        self.assertEqual(counted(summary), (3, 1))
        self.assertTrue(summary['general_quota_exhausted'])
        self.assertEqual(run_kind(stored_run(summary)), 'failed')

    def test_general_quota_alone_waits_for_its_bucket(self):
        _, summary, _ = quota_split.run_pass(quota_split.Provider(general='quota'))
        row = stored_run(summary)
        self.assertEqual((row['errors'], row['summary_json']['limit_errors']), (1, 1))
        self.assertEqual((run_kind(row), collection_state.quota_wait_bucket(row)), ('quota', 'general'))

    def test_both_buckets_refused_in_one_pass_wait_with_the_general_notice(self):
        _, summary, _ = quota_split.run_pass(quota_split.Provider(general='quota', stats='quota'))
        row = stored_run(summary)
        self.assertGreaterEqual(row['errors'], 2)
        self.assertEqual(row['summary_json']['limit_errors'], row['errors'])
        self.assertEqual((run_kind(row), collection_state.quota_wait_bucket(row)), ('quota', None))

    def test_buckets_refused_earlier_today_wait_without_errors(self):
        for remembered, bucket in ((('general',), 'general'), (('stats',), 'stats'), (('general', 'stats'), None)):
            with self.subTest(remembered=remembered):
                _, summary, _ = quota_split.run_pass(quota_split.Provider(), remembered=remembered)
                row = stored_run(summary)
                self.assertEqual((row['status'], row['errors'], row['summary_json']['limit_errors']), ('error', 0, 0))
                self.assertEqual((run_kind(row), collection_state.quota_wait_bucket(row)), ('quota', bucket))

    def test_rate_limit_beside_an_ordinary_error_is_a_failure(self):
        video, _ = phase_stop.run_video_phase('429')
        _, summary = phase_stop.run_pass(video, metadata_summary=profile_step(requests.HTTPError('synthetic HTTP 500')))
        self.assertEqual(counted(summary), (2, 1))
        self.assertEqual(run_kind(stored_run(summary)), 'failed')

    def test_rate_limit_alone_waits_for_the_next_sync(self):
        video, _ = phase_stop.run_video_phase('429')
        _, summary = phase_stop.run_pass(video, metadata_summary=profile_step())
        row = stored_run(summary)
        self.assertEqual((row['errors'], row['summary_json']['limit_errors']), (1, 1))
        self.assertEqual(run_kind(row), 'rate_limited')

    def test_a_failed_batch_request_stays_a_failure(self):
        video, _ = phase_stop.run_video_phase('503')
        _, summary = phase_stop.run_pass(video, metadata_summary=profile_step())
        self.assertEqual(counted(summary), (1, 0))
        self.assertEqual(run_kind(stored_run(summary)), 'failed')

    def test_a_step_stop_without_a_counted_error_is_explained_by_its_own_flag(self):
        # A one-off job's 429 or quota stop marks the running step without an
        # error of that step; the pass still counts one.
        for video, kind, explained in (
            ({'errors': 0, 'rate_limited': True, 'stopped_reason': 'provider_rate_limited'}, 'rate_limited', 1),
            ({'errors': 0, 'quota_exhausted': True, 'stopped_reason': 'quota_exhausted'}, 'quota', 1),
            ({'errors': 0, 'batch_unavailable': True, 'stopped_reason': 'batch_unavailable'}, 'failed', 0),
            ({'errors': 0, 'quota_exhausted': True, 'infrastructure_error': True}, 'failed', 0),
        ):
            with self.subTest(video=video):
                _, summary = phase_stop.run_pass(video)
                self.assertEqual(counted(summary), (1, explained))
                self.assertEqual(run_kind(stored_run(summary)), kind)

    def test_partial_and_complete_runs_record_no_explained_errors(self):
        _, summary = phase_stop.run_pass({'requested': 5, 'updated': 2, 'errors': 0, 'metadata_deferred': 3,
                                          'completion_outcome': 'partial', 'incomplete': True, 'unresolved': 3})
        row = stored_run(summary)
        self.assertEqual((row['status'], row['errors'], row['summary_json']['limit_errors']), ('partial', 0, 0))
        self.assertEqual(run_kind(row), 'carried_over')
        _, summary = phase_stop.run_pass({'requested': 5, 'updated': 5, 'errors': 0, 'completion_outcome': 'completed'})
        row = stored_run(summary)
        self.assertEqual((row['status'], row['errors'], row['summary_json']['limit_errors']), ('completed', 0, 0))
        self.assertEqual(run_kind(row), 'complete')


class ErrorSiteTests(unittest.TestCase):
    """Every place that counts a quota or 429 refusal as an error also explains it."""

    def test_channel_profile_request(self):
        for error, detector, explained, flag in (
            (YouTubeDailyQuotaExceededError('provider_quota_exceeded'), True, 1, 'general_quota_exhausted'),
            (YouTubeRateLimitError(), False, 1, 'rate_limited'),
            (requests.HTTPError('synthetic HTTP 500'), True, 0, None),
            (requests.Timeout('synthetic timeout'), True, 0, None),
        ):
            with self.subTest(error=type(error).__name__):
                summary = profile_step(error, quota_detector=detector)
                self.assertEqual(counted(summary), (1, explained))
                if flag:
                    self.assertTrue(summary[flag])

    def test_playlist_count_recheck_inside_a_profile_update(self):
        channel = metadata_steps.row()
        channel.update(video_count=0, collection_mode='metadata_only',
                       initial_long_inventory_status='skipped_no_long_playlist')
        for error, flag in ((YouTubeRateLimitError(), 'rate_limited'),
                            (YouTubeDailyQuotaExceededError('provider_quota_exceeded'), 'general_quota_exhausted')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(metadata, 'get_long_video_count', side_effect=error):
                summary = profile_step(channel=dict(channel), quota_detector=True)
                self.assertEqual(counted(summary), (1, 1))
                self.assertTrue(summary[flag])

    def test_playlist_pages(self):
        def run(*answers):
            # One claim per answer: a stop then has no unstarted claim to release.
            repository = inventory_engine.Repository()
            kinds = ('long', 'short')[:len(answers)]
            repository.claim_pass = Mock(return_value=[inventory_engine.claim(kind) for kind in kinds])
            with patch('sys.stdout', io.StringIO()):
                return run_typed_inventory_batch(repository=repository, fetch_page=Mock(side_effect=answers),
                    limit=len(answers), worker_id='limit-errors-test', prepare=False, now=lambda: inventory_engine.NOW)
        summary = run(YouTubeRateLimitError())
        self.assertEqual(counted(summary), (1, 1))
        self.assertTrue(summary['rate_limited'])
        summary = run(requests.HTTPError('synthetic HTTP 500'), YouTubeRateLimitError())
        self.assertEqual(counted(summary), (2, 1))
        summary = run(requests.HTTPError('synthetic HTTP 500'), inventory_engine.page())
        self.assertEqual(counted(summary), (1, 0))

    def test_typed_full_details_request(self):
        claims = [VideoCollectionClaim(video_id='vid00000001', channel_id=phase_stop.CHANNEL, collection_mode='metadata',
                  lease_token='t', lease_expires_at=STAMP, next_due_at=STAMP, video_type='short')]
        for error, explained in ((YouTubeRateLimitError(), 1), (YouTubeDailyQuotaExceededError('provider_quota_exceeded'), 1),
                                 (requests.HTTPError('synthetic HTTP 500'), 0)):
            with self.subTest(error=type(error).__name__):
                service = VideoCollectionService(Mock(), fetch_full=Mock(side_effect=error), fetch_counters=Mock(),
                    build_full_payload=Mock(), build_counter_payload=Mock(), is_quota_exhausted=QUOTA_DETECTOR)
                with quota_buckets.remembered():
                    summary = service.process_typed_claims(claims, video_type='short', reason='typed_short_current_data')
                self.assertEqual(counted(summary), (1, explained))

    def test_counter_fallback_of_an_initial_profile(self):
        for error, explained in ((YouTubeDailyQuotaExceededError('quota'), 1), (YouTubeRateLimitError(), 1),
                                 (TimeoutError('synthetic timeout'), 0)):
            with self.subTest(error=type(error).__name__):
                items = [initial_parallel.video('good'), initial_parallel.video('missing')]
                items[1]['statistics'].pop('viewCount')
                repository = initial_parallel.AtomicRepository()
                collector = initial_parallel.service(repository, lambda ids, phase: items)
                collector.fetch_counters = Mock(side_effect=error)
                summary = initial_parallel.run(collector, ['good', 'missing'])
                self.assertEqual(counted(summary), (1, explained))


class InstalledBindingTests(unittest.TestCase):
    """The installed whole-pass binding: one window, the actual step feeders and finish."""
    db = metadata_steps.MetadataProviderStepsTests.db
    fetch = metadata_steps.MetadataProviderStepsTests.fetch
    persist = metadata_steps.MetadataProviderStepsTests.persist

    def setUp(self):
        all_phases.AllPhaseBindingTests.setUp(self)

    def refuse_counters(self, error):
        self.video_repo.mode = 'counters'
        def batch(ids, reason):
            raise error
        self.video_service.fetch_counters = batch

    def whole_pass(self, bound):
        context = self.context
        with patch.object(cycle, 'validate_required_auto_sync_rpcs', lambda **k: {'status': 'ok'}), \
             patch.object(cycle, 'recover_stale_running_statuses', lambda **k: {'errors': 0}), \
             patch.object(cycle, 'update_sync_run_progress', lambda *a, **k: None), \
             patch.object(cycle, 'emit_phase', lambda *a, **k: None), \
             patch.object(cycle, 'wait_for_handoff', lambda seconds: False), \
             patch.object(provider_runtime, '_runtime', None), \
             patch.object(quota_buckets, 'read_remembered', lambda database: ()):
            summary = cycle._run_auto_sync_cycle(stats_limit=1, new_video_stats_limit=50, metadata_limit=1,
                sync_run_id=1, inventory_page_budget=3, context=context, common_dispatch=bound)
        bound.finish(summary)
        bound.close()
        return summary

    def test_stats_quota_beside_a_failed_profile_read_through_the_installed_pass(self):
        self.refuse_counters(YouTubeDailyQuotaExceededError('provider_quota_exceeded'))
        def profiles(ids):
            raise requests.HTTPError('synthetic HTTP 500')
        self.context.sources.fetch_channel_metadata_batch = profiles
        bound = all_phases.runtime.bind_process_common_dispatch(self.context, whole_cycle=True)
        summary = self.whole_pass(bound)
        self.assertEqual((summary['video_collection']['limit_errors'], summary['channel_metadata']['limit_errors']), (1, 0))
        self.assertEqual(counted(summary), (2, 1))
        self.assertEqual(run_kind(stored_run(summary)), 'failed')

    def test_stats_quota_alone_through_the_installed_pass(self):
        self.refuse_counters(YouTubeDailyQuotaExceededError('provider_quota_exceeded'))
        bound = all_phases.runtime.bind_process_common_dispatch(self.context, whole_cycle=True)
        summary = self.whole_pass(bound)
        self.assertEqual(counted(summary), (1, 1))
        row = stored_run(summary)
        self.assertEqual((run_kind(row), collection_state.quota_wait_bucket(row)), ('quota', 'stats'))

    def test_playlist_rate_limit_through_the_installed_window(self):
        def playlist(*args, **options):
            raise YouTubeRateLimitError()
        self.context.sources.fetch_playlist_items_page = playlist
        bound = all_phases.runtime.bind_process_common_dispatch(self.context, whole_cycle=True)
        bound(limit=1, common_preflight_checked=True)
        bound.metadata(limit=1)
        inventory = bound.inventory(limit=3, run_id=42, on_page=lambda result: None)
        self.assertGreaterEqual(inventory['errors'], 1)
        self.assertEqual(inventory['limit_errors'], inventory['errors'])
        self.assertTrue(inventory['rate_limited'])
        bound.close()

    def test_finish_counts_a_rate_limit_stop_as_explained_and_a_failure_not(self):
        self.refuse_counters(YouTubeRateLimitError())
        bound = all_phases.runtime.bind_process_common_dispatch(self.context, whole_cycle=True)
        bound(limit=1, common_preflight_checked=True)
        summary = {'errors': 0, 'limit_errors': 0}
        bound.finish(summary)
        self.assertEqual(counted(summary), (1, 1))
        self.assertTrue(summary['rate_limited'])
        bound = all_phases.runtime.bind_process_common_dispatch(self.context, whole_cycle=True)
        bound(limit=1, common_preflight_checked=True)
        summary = {'errors': 0, 'limit_errors': 0}
        bound.finish(summary, failed=True)
        self.assertEqual(counted(summary), (1, 0))
        self.assertTrue(summary['infrastructure_error'])


if __name__ == '__main__':
    unittest.main()
