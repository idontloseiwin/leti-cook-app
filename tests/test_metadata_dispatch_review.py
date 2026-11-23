"""Independent metadata extraction review; fake repositories, no provider/DB I/O."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
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
from collection.provider_steps import ProviderResult
from youtube_errors import YouTubePlaylistNotFoundError

CHANNEL = 'UC' + 'r' * 22
SOURCE = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
OWNER_RESUME = SOURCE + timedelta(minutes=20)


def channel(**changes):
    return {**dict(channel_id=CHANNEL, collection_control_epoch=7,
        collection_permission='accepted', collection_mode='active', video_count=1), **changes}


def source_item():
    return {'id': CHANNEL, 'snippet': {'title': 'Synthetic'},
            'statistics': {'videoCount': '1', 'viewCount': '8'}}


class SourceClock:
    @staticmethod
    def now(tz):
        return SOURCE


class DateType(type):
    def __instancecheck__(cls, value):
        return isinstance(value, datetime)


class ResumeClock(metaclass=DateType):
    @staticmethod
    def now(tz):
        return OWNER_RESUME


class MetadataDispatchReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('sys.stdout', io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted'}))
        self.owner = get_ident()
        self.events = []
        self.repo = Mock(spec=metadata.ChannelSyncRepository)
        self.repo.assert_metadata_batch_ready.side_effect = lambda: self.record('preflight')
        self.repo.mark_metadata_running.side_effect = lambda *args: self.record('running')
        self.repo.update_metadata_batch.side_effect = self.persist
        self.repo.update_process_state.side_effect = lambda *a, **kw: self.record('status') or True
        self.repo.clear_initial_inventory_error.side_effect = lambda *a, **kw: self.record('clear') or {'cleared': True}
        @contextmanager
        def fresh():
            self.record('fresh')
            yield self.repo
        self.context = SimpleNamespace(database=object(), fresh_channel_repository=fresh,
            settings=SimpleNamespace(typed_playlist_inventory_enabled=False),
            sources=SimpleNamespace(fetch_channel_metadata_batch=self.fetch,
                get_longform_playlist_id=lambda identity: 'UULF' + identity[2:],
                fetch_playlist_items_page=lambda *a, **kw: {'pageInfo': {'totalResults': 0}},
                is_youtube_daily_quota_exhausted=lambda error: False,
                parse_dt=datetime.fromisoformat))
        self.stack.enter_context(patch.object(metadata, 'ChannelSyncRepository', return_value=self.repo))
        self.stack.enter_context(patch.object(metadata, 'stop_at_boundary', return_value=False))
        self.stack.enter_context(patch.object(metadata, 'load_due_channel_metadata_channels', return_value=[channel()]))

    def record(self, name):
        self.assertEqual(get_ident(), self.owner, name)
        self.events.append(name)

    def persist(self, rows):
        self.record('persist')
        return {entry['channel_id']: 'updated' for entry in rows}

    def fetch(self, ids):
        self.assertNotEqual(get_ident(), self.owner)
        return [source_item()]

    def execute(self, step):
        with patch('collection.provider_steps.datetime', SourceClock), ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(step.execute).result(timeout=2)

    def finish(self, steps, response):
        try:
            steps.send(response)
        except StopIteration as done:
            return done.value
        finally:
            steps.close()
        self.fail('unexpected provider continuation')

    def recheck(self, *, missing=False, error_on_clear=False):
        def fetch_page(*args, **kwargs):
            self.assertNotEqual(get_ident(), self.owner)
            if missing:
                raise YouTubePlaylistNotFoundError('playlistNotFound')
            return {'pageInfo': {'totalResults': 0}}
        self.context.sources.fetch_playlist_items_page = fetch_page
        if error_on_clear:
            def failed_clear(*args, **kwargs):
                self.record('clear-unknown')
                raise RuntimeError('synthetic-unknown-clear')
            self.repo.clear_initial_inventory_error.side_effect = failed_clear
        current = channel(collection_mode='metadata_only',
            initial_long_inventory_status='skipped_no_long_playlist',
            initial_long_inventory_error_code='initial_inventory_failed')
        values = {}
        steps = metadata.recheck_no_long_playlist_steps(current, values, context=self.context)
        response = self.execute(next(steps))
        # Force a long settlement queue without sleeping or making a request.
        with patch('channel_inventory_estimate.datetime', ResumeClock):
            result = self.finish(steps, response)
        return result, values, response

    def test_no_long_count_estimate_uses_provider_observation_not_owner_resume(self):
        result, values, response = self.recheck()
        self.assertEqual(response.observed_at, SOURCE)
        self.assertEqual(values['initial_inventory_estimate']['checked_at'], SOURCE.isoformat())
        self.assertEqual(result['status'], 'still_no_long_playlist')

    def test_no_long_absence_estimate_uses_provider_observation_not_owner_resume(self):
        result, values, response = self.recheck(missing=True)
        self.assertEqual(values['initial_inventory_estimate']['checked_at_by_type']['long'], SOURCE.isoformat())
        self.assertIsNone(values['initial_inventory_estimate']['counts']['long'])
        self.assertEqual(values['initial_inventory_estimate']['statuses']['long'], 'playlist_not_found')

    def test_missing_error_observation_cannot_record_absence_or_clear(self):
        current = channel(collection_mode='metadata_only',
            initial_long_inventory_status='skipped_no_long_playlist')
        for stamp in (None, SOURCE.replace(tzinfo=None)):
            with self.subTest(stamp=stamp):
                values = {}
                steps = metadata.recheck_no_long_playlist_steps(current, values, context=self.context)
                next(steps)
                response = ProviderResult(error=YouTubePlaylistNotFoundError('playlistNotFound'),
                                          observed_at=stamp)
                result = self.finish(steps, response)
                self.assertEqual(result['status'], 'no_long_playlist_recheck_error')
                self.assertEqual(values, {})
                self.repo.clear_initial_inventory_error.assert_not_called()

    def test_real_clear_helper_runs_once_on_owner_for_zero_and_absence(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.repo.clear_initial_inventory_error.reset_mock()
                self.events.clear()
                result, values, response = self.recheck(missing=missing)
                self.assertEqual(self.events, ['clear'])
                self.repo.clear_initial_inventory_error.assert_called_once_with(CHANNEL, expected_epoch=7)
                self.assertEqual(result['status'], 'still_no_long_playlist')

    def test_clear_unknown_legacy_domain_status_is_not_a_settlement_receipt(self):
        result, values, response = self.recheck(error_on_clear=True)
        self.assertEqual(self.events, ['clear-unknown'])
        self.repo.clear_initial_inventory_error.assert_called_once_with(CHANNEL, expected_epoch=7)
        # Existing best-effort helper deliberately preserves domain status. A
        # future adapter must separately prove this write before releasing credit.
        self.assertEqual(result['status'], 'still_no_long_playlist')
        self.assertNotIn('settlement_confirmed', result)

    def test_materialization_failure_never_becomes_partial_success_or_missing(self):
        def interrupted(ids):
            self.assertNotEqual(get_ident(), self.owner)
            yield source_item()
            raise RuntimeError('synthetic-stream-failure')
        self.context.sources.fetch_channel_metadata_batch = interrupted
        steps = metadata.channel_metadata_steps(context=self.context)
        response = self.execute(next(steps))
        self.assertIsNotNone(response.error)
        result = self.finish(steps, response)
        self.assertEqual((result['updated'], result['missing'], result['errors']), (0, 0, 1))
        self.repo.update_metadata_batch.assert_not_called()
        self.repo.update_process_state.assert_called_once()
        self.assertEqual(self.events, ['preflight', 'running', 'status'])

    def test_unknown_batch_commit_and_reconcile_failure_never_replay_mutation(self):
        def unknown(rows):
            self.record('persist-unknown')
            raise RuntimeError('synthetic-unknown-commit')
        def read_unknown(identity):
            self.record('read-unknown')
            raise RuntimeError('synthetic-unavailable-reconcile')
        def cleanup_unknown(*args, **kwargs):
            self.record('cleanup-unknown')
            raise RuntimeError('synthetic-unknown-cleanup')
        self.repo.update_metadata_batch.side_effect = unknown
        self.repo.load_channel_record.side_effect = read_unknown
        self.repo.update_process_state.side_effect = cleanup_unknown
        steps = metadata.channel_metadata_steps(context=self.context)
        result = self.finish(steps, self.execute(next(steps)))
        self.assertEqual((result['updated'], result['processed'], result['errors']), (0, 0, 1))
        self.repo.update_metadata_batch.assert_called_once()
        self.repo.load_channel_record.assert_called_once_with(CHANNEL)
        self.repo.update_process_state.assert_called_once()
        self.assertEqual(self.events, ['preflight', 'running', 'persist-unknown', 'fresh',
                                      'read-unknown', 'fresh', 'cleanup-unknown'])
        self.assertNotIn('settlement_confirmed', result)

    def test_exact_source_clock_and_scope_reconcile_commit_without_replay(self):
        self.repo.update_metadata_batch.side_effect = RuntimeError('synthetic-lost-ack')
        self.repo.load_channel_record.return_value = channel(
            channel_metadata_status='ok', channel_metadata_checked_at=SOURCE.isoformat(), is_hidden=False)
        steps = metadata.channel_metadata_steps(context=self.context)
        result = self.finish(steps, self.execute(next(steps)))
        self.assertEqual((result['updated'], result['errors']), (1, 0))
        self.repo.update_metadata_batch.assert_called_once()
        self.repo.update_process_state.assert_not_called()

    def test_new_epoch_or_hidden_reconcile_cannot_become_success(self):
        for changes in ({'collection_control_epoch': 8}, {'is_hidden': True}):
            with self.subTest(changes=changes):
                self.repo.update_metadata_batch.reset_mock()
                self.repo.update_metadata_batch.side_effect = RuntimeError('synthetic-lost-ack')
                self.repo.load_channel_record.return_value = channel(
                    channel_metadata_status='ok', channel_metadata_checked_at=SOURCE.isoformat(), **changes)
                steps = metadata.channel_metadata_steps(context=self.context)
                result = self.finish(steps, self.execute(next(steps)))
                self.assertEqual(result['updated'], 0)
                self.assertEqual(result['errors'], 1)
                self.repo.update_metadata_batch.assert_called_once()

    def test_closing_unsent_continuation_does_not_prove_running_marker_cleared(self):
        steps = metadata.channel_metadata_steps(context=self.context)
        next(steps)
        self.assertEqual(self.events, ['preflight', 'running'])
        steps.close()
        self.assertEqual(self.events, ['preflight', 'running'])
        self.repo.update_process_state.assert_not_called()
        self.repo.update_metadata_batch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
