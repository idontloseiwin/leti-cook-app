"""Real iterator/thread boundaries with fake repositories and no provider I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import sys
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'application'))
from collection import channel_metadata as metadata
from collection.provider_steps import ProviderResult, ProviderStep
from channel_sync_policy import channel_policy_from_native_facts, channel_sync_eligibility
from db.channel.channel_read_policy import FACT_SQL
from youtube_errors import YouTubePlaylistNotFoundError, YouTubeRateLimitError

CHANNEL = 'UC' + 'a' * 22
SOURCE_TIME = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)


def row(channel_id=CHANNEL, **overrides):
    return dict(channel_id=channel_id, collection_control_epoch=7,
                collection_permission='accepted', collection_mode='active',
                video_count=1, **overrides)


def item(channel_id=CHANNEL):
    return {'id': channel_id, 'snippet': {'title': 'Example'},
            'statistics': {'videoCount': '1', 'viewCount': '8'}}


class MetadataProviderStepsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('sys.stdout', io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted'}))
        self.owner = get_ident()
        self.events = []
        self.repository = Mock(spec=metadata.ChannelSyncRepository)
        self.repository.assert_metadata_batch_ready.side_effect = lambda: self.db('preflight')
        self.repository.mark_metadata_running.side_effect = lambda *args: self.db('running')
        self.repository.update_metadata_batch.side_effect = self.persist
        self.context = SimpleNamespace(database=object(),
            settings=SimpleNamespace(typed_playlist_inventory_enabled=False),
            sources=SimpleNamespace(fetch_channel_metadata_batch=self.fetch,
                is_youtube_daily_quota_exhausted=lambda error: False,
                parse_dt=datetime.fromisoformat))
        self.stack.enter_context(patch.object(metadata, 'ChannelSyncRepository', return_value=self.repository))
        self.stack.enter_context(patch.object(metadata, 'stop_at_boundary', return_value=False))

    def db(self, action):
        self.assertEqual(get_ident(), self.owner, action)
        self.events.append(action)

    def persist(self, rows):
        self.db('persist')
        return {entry['channel_id']: 'updated' for entry in rows}

    def fetch(self, ids):
        self.assertNotEqual(get_ident(), self.owner)
        self.events.append(('fetch', tuple(ids)))
        # Iteration also belongs to the provider thread, not just construction.
        for channel_id in ids:
            self.assertNotEqual(get_ident(), self.owner)
            yield item(channel_id)

    def steps(self, channels=None):
        channels = channels if channels is not None else [row()]
        self.stack.enter_context(patch.object(metadata, 'load_due_channel_metadata_channels', return_value=channels))
        return metadata.channel_metadata_steps(context=self.context)

    def drive(self, steps):
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                step = next(steps)
                while True:
                    self.assertIsInstance(step, ProviderStep)
                    step = steps.send(pool.submit(step.execute).result(timeout=2))
            except StopIteration as complete:
                return complete.value
            finally:
                steps.close()

    def test_only_provider_callbacks_leave_owner_thread(self):
        result = self.drive(self.steps())
        self.assertEqual(result['updated'], 1)
        self.assertEqual(self.events, ['preflight', 'running', ('fetch', (CHANNEL,)), 'persist'])
        self.assertEqual(self.repository.update_metadata_batch.call_args.args[0][0]['control_epoch'], 7)

    def test_batch_bound_and_request_count_unchanged(self):
        channels = [row('UC' + f'{index:022d}') for index in range(101)]
        result = self.drive(self.steps(channels))
        self.assertEqual((result['updated'], result['requested'], result['batches']), (101, 101, 3))
        self.assertEqual([len(event[1]) for event in self.events if isinstance(event, tuple)], [50, 50, 1])

    def test_source_time_is_captured_before_settlement_wait(self):
        steps = self.steps()
        step = next(steps)
        class SourceClock:
            @staticmethod
            def now(tz):
                return SOURCE_TIME
        with patch('collection.provider_steps.datetime', SourceClock), ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(step.execute).result(timeout=2)
        self.assertEqual(response.observed_at, SOURCE_TIME)
        # Owner resumes after the provider clock override has gone away.
        with self.assertRaises(StopIteration):
            steps.send(response)
        values = self.repository.update_metadata_batch.call_args.args[0][0]['values']
        self.assertEqual(values['channel_metadata_checked_at'], SOURCE_TIME.isoformat())
        self.assertEqual(values['country_collected_at'], SOURCE_TIME.isoformat())

    def test_missing_source_clock_cannot_renew_data(self):
        steps = self.steps()
        next(steps)
        with self.assertRaisesRegex(ValueError, 'source_clock_invalid'):
            steps.send(ProviderResult(value=[item()]))
        self.repository.update_metadata_batch.assert_not_called()

    def test_no_due_channels_do_not_request_provider(self):
        result = self.drive(self.steps([]))
        self.assertEqual(result['requested'], 0)
        self.assertEqual(self.events, [])

    def test_running_write_failure_prevents_provider_attempt(self):
        self.repository.mark_metadata_running.side_effect = RuntimeError('write_unknown')
        with self.assertRaisesRegex(RuntimeError, 'write_unknown'):
            self.drive(self.steps())
        self.assertEqual(self.events, ['preflight'])

    def test_provider_failure_is_not_successful_missing_result(self):
        self.context.sources.fetch_channel_metadata_batch = Mock(side_effect=RuntimeError('transport'))
        mark = self.stack.enter_context(patch.object(metadata, 'mark_channel_metadata_error',
            side_effect=lambda *args, **kwargs: self.db('error')))
        result = self.drive(self.steps())
        self.assertEqual((result['errors'], result['missing'], result['updated']), (1, 0, 0))
        self.assertEqual(mark.call_count, 1)
        self.repository.update_metadata_batch.assert_not_called()

    def test_unknown_control_drains_sequential_marker_without_failure_or_clock(self):
        from quota_buckets import QuotaStateUnavailable
        self.context.sources.fetch_channel_metadata_batch=Mock(side_effect=QuotaStateUnavailable())
        mark=self.stack.enter_context(patch.object(metadata,'mark_channel_metadata_error'))
        result=self.drive(self.steps([row(channel_metadata_status='ok')]))
        marker=self.repository.drain_metadata_running.call_args.args[0][0]
        self.assertEqual(marker,dict(channel_id=CHANNEL,control_epoch=7,
            started_at=self.repository.mark_metadata_running.call_args.args[1],restore_status='ok'))
        self.assertTrue(result['quota_control_unknown'])
        self.assertEqual(result['stopped_reason'],'quota_state_unavailable')
        self.assertEqual((result['updated'],result['missing'],result['errors']),(0,0,1))
        mark.assert_not_called()
        self.repository.update_metadata_batch.assert_not_called()

    def test_unknown_control_in_sequential_recheck_preserves_profile(self):
        from quota_buckets import QuotaStateUnavailable
        channel=row(channel_metadata_status='ok')
        channel.update(collection_mode='metadata_only',initial_long_inventory_status='skipped_no_long_playlist',
                       initial_long_inventory_error_code='failed')
        self.stack.enter_context(patch.object(metadata,'get_long_video_count',side_effect=QuotaStateUnavailable()))
        mark=self.stack.enter_context(patch.object(metadata,'mark_channel_metadata_error'))
        result=self.drive(self.steps([channel]))
        self.repository.drain_metadata_running.assert_called_once()
        self.repository.update_metadata_batch.assert_not_called()
        mark.assert_not_called()
        self.assertTrue(result['quota_control_unknown'])

    def test_unknown_control_drain_failure_stays_unconfirmed(self):
        from quota_buckets import QuotaStateUnavailable
        self.context.sources.fetch_channel_metadata_batch=Mock(side_effect=QuotaStateUnavailable())
        self.repository.drain_metadata_running.side_effect=OSError('lost acknowledgement')
        with self.assertRaises(metadata.MetadataSettlementUnknown):
            self.drive(self.steps())
        self.repository.update_metadata_batch.assert_not_called()
        self.repository.update_channel.assert_not_called()

    def channels_list_error(self, kind):
        """The error a real channels.list call raises for one synthetic answer."""
        import requests
        from provider_admission import AdmissionTimeout
        from youtube_channels import fetch_channel_metadata_batch
        class Response:
            headers = {}
            def __init__(self, status, payload):
                self.status_code, self.payload = status, payload
            def json(self):
                if self.payload is None:
                    raise ValueError('no body')
                return self.payload
            def raise_for_status(self):
                raise requests.HTTPError('HTTP %s' % self.status_code)
        def answer(*args, **kwargs):
            if kind == 'timeout': raise requests.Timeout('synthetic read timeout')
            if kind == 'dropped': raise requests.ConnectionError('synthetic reset')
            if kind == 'dropped-body': raise requests.exceptions.ChunkedEncodingError('synthetic cut')
            if kind == 'admission': raise AdmissionTimeout('provider_admission_timeout')
            return {'503': Response(503, {'error': {'code': 503, 'errors': [{'reason': 'backendError'}]}}),
                    '500-nobody': Response(500, None),
                    'key-rejected': Response(400, {'error': {'code': 400, 'errors': [{'reason': 'keyInvalid'}]}}),
                    'forbidden': Response(403, {'error': {'code': 403, 'errors': [{'reason': 'forbidden'}]}})}[kind]
        try:
            fetch_channel_metadata_batch([CHANNEL], youtube_api_get=answer, api_key=None, attempt_recorder=None,
                timeout_seconds=5, emit_event=lambda *a, **k: None, perf_counter=lambda: 0.0)
        except Exception as error:
            return error
        self.fail('synthetic answer was accepted')

    def failed_batch(self, error, channels):
        def fetch(ids):
            self.assertNotEqual(get_ident(), self.owner)
            raise error
        self.context.sources.fetch_channel_metadata_batch = fetch
        started = datetime.now(timezone.utc)
        result = self.drive(self.steps(channels))
        writes = {call.args[0]: call.args[1] for call in self.repository.update_process_state.call_args_list}
        self.repository.update_process_state.reset_mock()
        return result, writes, started

    def test_passing_channels_list_failure_keeps_every_confirmed_profile(self):
        # One 503/timeout/dropped connection/admission timeout must not hide
        # (and let cleanup remove) the profiles of the whole batch of 50.
        from datetime import timedelta
        confirmed = [row('UC' + f'{index:022d}', channel_metadata_status='ok', channel_metadata_error_count=index % 2)
                     for index in range(50)]
        failed = row('UC' + 'f' * 22, channel_metadata_status='channel_not_found', channel_metadata_error_count=4)
        for kind in ('503', '500-nobody', 'timeout', 'dropped', 'dropped-body', 'admission'):
            with self.subTest(kind=kind):
                result, writes, started = self.failed_batch(self.channels_list_error(kind), confirmed + [failed])
                self.assertEqual((result['errors'], result['updated'], result['batches']), (2, 0, 2))
                for channel in confirmed:
                    values = writes[channel['channel_id']]
                    self.assertEqual(values['channel_metadata_status'], 'ok')
                    self.assertEqual(values['channel_metadata_error'], 'provider_unavailable')
                    count = channel['channel_metadata_error_count'] + 1
                    self.assertEqual(values['channel_metadata_error_count'], count)
                    delay = datetime.fromisoformat(values['channel_metadata_next_after']) - started
                    self.assertTrue(timedelta(hours=2 ** count) <= delay < timedelta(hours=2 ** count, minutes=1))
                    self.assertNotIn('channel_metadata_checked_at', values)
                    self.assertNotIn('title', values)
                gone = writes[failed['channel_id']]
                # Its daily recheck failed, not the verdict: "No longer on YouTube"
                # stays shown, and its recorded code stays (see stale-run recovery).
                self.assertEqual(gone['channel_metadata_status'], 'channel_not_found')
                self.assertNotIn('channel_metadata_error', gone)

    def test_real_failure_of_a_gone_channel_recheck_still_marks_error(self):
        gone = [row(channel_metadata_status='channel_not_found', channel_metadata_error_count=3)]
        _, writes, _ = self.failed_batch(self.channels_list_error('key-rejected'), gone)
        self.assertEqual((writes[CHANNEL]['channel_metadata_status'], writes[CHANNEL]['channel_metadata_error']),
                         ('error', 'youtube_api_error'))

    def test_refusals_about_the_request_or_key_still_mark_error(self):
        confirmed = [row(channel_metadata_status='ok', channel_metadata_error_count=0)]
        for kind in ('key-rejected', 'forbidden'):
            with self.subTest(kind=kind):
                result, writes, _ = self.failed_batch(self.channels_list_error(kind), confirmed)
                values = writes[CHANNEL]
                self.assertEqual(values['channel_metadata_status'], 'error')
                self.assertEqual(values['channel_metadata_error'], 'youtube_api_error')
                self.assertEqual(values['channel_metadata_error_count'], 1)
        _, writes, _ = self.failed_batch(RuntimeError('unexpected'), confirmed)
        self.assertEqual((writes[CHANNEL]['channel_metadata_status'], writes[CHANNEL]['channel_metadata_error']),
                         ('error', 'channel_metadata_failed'))

    def test_count_probe_and_failure_clear_have_different_threads(self):
        def missing(channel_id, **kwargs):
            self.assertNotEqual(get_ident(), self.owner)
            raise YouTubePlaylistNotFoundError('playlistNotFound')
        self.stack.enter_context(patch.object(metadata, 'get_long_video_count', side_effect=missing))
        self.stack.enter_context(patch.object(metadata, 'clear_initial_inventory_failure',
            side_effect=lambda *args, **kwargs: self.db('clear')))
        channel = row()
        channel.update(collection_mode='metadata_only', initial_long_inventory_status='skipped_no_long_playlist')
        values = {}
        result = self.drive(metadata.recheck_no_long_playlist_steps(channel, values, context=self.context))
        self.assertEqual(result['status'], 'still_no_long_playlist')
        self.assertEqual(self.events, ['clear'])

    def test_rate_during_count_probe_is_not_daily_quota_or_missing(self):
        self.stack.enter_context(patch.object(metadata, 'get_long_video_count',
            side_effect=YouTubeRateLimitError()))
        channel = row()
        channel.update(collection_mode='metadata_only', initial_long_inventory_status='skipped_no_long_playlist')
        values = {}
        result = self.drive(metadata.recheck_no_long_playlist_steps(channel, values, context=self.context))
        self.assertEqual(result['status'], 'no_long_playlist_recheck_error')
        self.assertTrue(result['rate_limited'])
        self.assertFalse(result['quota_exhausted'])
        self.assertEqual(values, {})
        self.repository.clear_initial_inventory_error.assert_not_called()

    def test_count_decrease_is_saved_without_discovery_or_video_request(self):
        self.context.settings.typed_playlist_inventory_enabled = True
        typed = self.stack.enter_context(patch.object(metadata, 'TypedInventoryRepository'))
        probe = self.stack.enter_context(patch.object(metadata, 'get_long_video_count'))
        current = row()
        current['video_count'] = 2
        result = self.drive(self.steps([current]))
        values = self.repository.update_metadata_batch.call_args.args[0][0]['values']
        self.assertEqual(values['video_count'], 1)
        self.assertIn('channel_video_count_changed_at', values)
        self.assertNotIn('channel_discovery_next_after', values)
        self.assertEqual(result['video_count_decreased'], 1)
        self.assertEqual(result['typed_inventory_prioritized'], 0)
        self.assertEqual(result['video_count_increased_channel_ids'], [])
        self.assertEqual(result['video_count_decreased_channel_ids'], [CHANNEL])
        probe.assert_not_called()
        typed.assert_not_called()
        self.assertEqual([event for event in self.events if isinstance(event, tuple)],
                         [('fetch', (CHANNEL,))])

    def test_count_change_preserves_source_deadlines_and_leaves_generation_to_writer(self):
        self.context.settings.typed_playlist_inventory_enabled = True
        typed = self.stack.enter_context(patch.object(metadata, 'TypedInventoryRepository'))
        increased, decreased = row(), row('UC' + 'b' * 22)
        increased['video_count'], decreased['video_count'] = 0, 2
        result = self.drive(self.steps([increased, decreased]))
        self.assertEqual(result['video_count_changed'], 2)
        self.assertEqual(result['video_count_increased_channel_ids'], [CHANNEL])
        self.assertEqual(result['video_count_decreased_channel_ids'], [decreased['channel_id']])
        for update in self.repository.update_metadata_batch.call_args.args[0]:
            self.assertIn('channel_video_count_changed_at', update['values'])
            self.assertNotIn('channel_discovery_next_after', update['values'])
            self.assertNotIn('channel_discovery_no_new_streak', update['values'])
        typed.assert_not_called()
        self.assertFalse(hasattr(metadata, '_websub_explained'))

    def test_unchanged_count_does_not_create_python_source_demand(self):
        result = self.drive(self.steps([row()]))
        self.assertEqual(result['video_count_changed'], 0)
        values = self.repository.update_metadata_batch.call_args.args[0][0]['values']
        self.assertNotIn('channel_video_count_changed_at', values)
        self.assertNotIn('channel_discovery_next_after', values)

    def test_count_decrease_does_not_probe_a_previously_missing_long_playlist(self):
        current = row()
        current.update(video_count=2, collection_mode='metadata_only',
                       initial_long_inventory_status='skipped_no_long_playlist')
        probe = self.stack.enter_context(patch.object(metadata, 'get_long_video_count'))
        result = self.drive(self.steps([current]))
        self.assertEqual((result['updated'], result['video_count_decreased']), (1, 1))
        probe.assert_not_called()

    def test_terminal_empty_long_count_growth_uses_common_discovery_without_long_probe(self):
        self.context.settings.typed_playlist_inventory_enabled = True
        typed = Mock()
        typed.prioritize_channels.return_value = 3
        self.stack.enter_context(patch.object(metadata, 'TypedInventoryRepository', return_value=typed))
        probe = self.stack.enter_context(patch.object(metadata, 'get_long_video_count'))
        for linked in (False, True):
            facts = {name: False for name in FACT_SQL}
            facts.update(control_present=True, permission_accepted=True, linked=linked,
                         types_long=True, status_completed=True, initial_skipped=True)
            current = row()
            current.update(video_count=0, initial_long_inventory_status='skipped_no_long_playlist',
                           collection_mode=channel_policy_from_native_facts(facts)['mode'])
            with self.subTest(linked=linked):
                result = self.drive(self.steps([current]))
                self.assertEqual((result['updated'], result['manual_required'],
                                  result['channel_discovery_prioritized']), (1, 0, 0))
                update = self.repository.update_metadata_batch.call_args.args[0][0]
                self.assertNotIn('control_action', update)
                self.assertNotIn('initial_long_inventory_status', update['values'])
                self.assertTrue(channel_sync_eligibility(current).allow_video_collection)
                typed.prioritize_channels.assert_not_called()
                self.assertIn('channel_video_count_changed_at', update['values'])
        probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
