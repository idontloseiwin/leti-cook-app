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

    def test_count_increase_prioritizes_only_the_increased_channel(self):
        self.context.settings.typed_playlist_inventory_enabled = True
        typed = Mock()
        typed.prioritize_channels.return_value = 3
        self.stack.enter_context(patch.object(metadata, 'TypedInventoryRepository', return_value=typed))
        increased, decreased = row(), row('UC' + 'b' * 22)
        increased['video_count'], decreased['video_count'] = 0, 2
        result = self.drive(self.steps([increased, decreased]))
        typed.prioritize_channels.assert_called_once_with([CHANNEL],
            expected_channel_scopes=[{'channel_id': CHANNEL, 'control_epoch': 7}])
        self.assertEqual(result['channel_discovery_prioritized'], 1)
        self.assertEqual(result['video_count_changed'], 2)
        updates = self.repository.update_metadata_batch.call_args.args[0]
        self.assertIn('channel_discovery_next_after', updates[0]['values'])
        self.assertNotIn('channel_discovery_next_after', updates[1]['values'])

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
                                  result['channel_discovery_prioritized']), (1, 0, 1))
                update = self.repository.update_metadata_batch.call_args.args[0][0]
                self.assertNotIn('control_action', update)
                self.assertNotIn('initial_long_inventory_status', update['values'])
                self.assertTrue(channel_sync_eligibility(current).allow_video_collection)
                typed.prioritize_channels.assert_called_with([CHANNEL],
                    expected_channel_scopes=[{'channel_id': CHANNEL, 'control_epoch': 7}])
        probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
