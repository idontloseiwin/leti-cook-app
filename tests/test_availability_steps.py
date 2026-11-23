"""Availability steps use the real video service with owner-thread DB doubles."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import partial
import io
import os
from pathlib import Path
import sys
from threading import get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT / 'tests')]
from collection import availability
from collection.provider_steps import ProviderStep
from db import VideoCollectionClaim
from video_collection_service import addressed_video_steps
from test_common_video_dispatch import Repository as CommonRepository
from test_initial_video_parallel import service, video, CHANNEL, STAMP


class Repository(CommonRepository):
    def __init__(self):
        super().__init__()
        self.claims, self.scopes, self.prepares = [], [], 0
        self.epoch = 7
        self.prepare_error = False

    def load_video_channel_map(self, ids):
        assert get_ident() == self.owner
        return dict.fromkeys(ids, CHANNEL)

    def load_channel_scopes(self, ids):
        raise AssertionError('parent scope must not be refreshed')

    def ensure_video_ids(self, ids, **kwargs):
        assert get_ident() == self.owner
        self.prepares += 1
        if self.prepare_error:
            raise ConnectionError('prepare unknown')

    def claim_video_metadata_ids(self, ids, **kwargs):
        assert get_ident() == self.owner
        self.scopes.append(kwargs['channel_scopes'])
        if kwargs['channel_scopes'] != [{'channel_id': CHANNEL, 'control_epoch': self.epoch}]:
            raise RuntimeError('scope_lost')
        self.claims.append((list(ids), kwargs['run_id']))
        return [VideoCollectionClaim(value, CHANNEL, 'metadata', 'token-' + value,
                    STAMP, STAMP, 'long') for value in ids]


class AvailabilityStepsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.dict(os.environ, {'LIC_DEPLOYMENT_PROFILE': 'self-hosted'}))
        self.stack.enter_context(patch('sys.stdout', io.StringIO()))
        self.stack.enter_context(patch('hot_runtime.require_hot_runtime_ready', return_value=None))
        self.stack.enter_context(patch('collection_shutdown.yield_requested', return_value=False))
        self.repository = Repository()
        self.collector = service(self.repository, Mock())
        self.calls = []
        self.collector.fetch_metadata = self.metadata
        self.collector.fetch_counters = self.counters
        self.context = SimpleNamespace(database=object())
        self.run_video_steps = partial(addressed_video_steps,
            self.repository, self.collector, worker_id='availability-test')

    def metadata(self, ids, reason):
        self.assertNotEqual(get_ident(), self.repository.owner)
        self.calls.append(('metadata', list(ids)))
        return [video(value) for value in ids]

    def counters(self, ids, reason):
        self.assertNotEqual(get_ident(), self.repository.owner)
        self.calls.append(('counters', list(ids)))
        return {'items': [{'id': value, 'views': 123, 'like_count': 7, 'comment_count': 2}
                          for value in ids], 'summary': {'failed_ids': []}}

    def drive(self, steps):
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                step = next(steps)
                while True:
                    self.assertIsInstance(step, ProviderStep)
                    step = steps.send(executor.submit(step.execute).result(timeout=2))
            except StopIteration as complete:
                return complete.value
            finally:
                steps.close()

    def loss_steps(self, ids):
        self.stack.enter_context(patch.object(availability, 'load_initial_long_inventory_status', return_value='completed'))
        self.stack.enter_context(patch.object(availability, 'load_long_availability_loss_candidate_ids', return_value=ids))
        def counts(returned, *, context):
            self.assertEqual(get_ident(), self.repository.owner)
            return {'public': len(returned)}
        self.status_counts = self.stack.enter_context(patch.object(availability,
            'load_video_availability_status_counts', side_effect=counts))
        return availability.availability_loss_steps(CHANNEL, context=self.context,
            expected_epoch=7, run_video_steps=self.run_video_steps)

    def test_loss_selection_provider_and_final_status_keep_owner_boundaries(self):
        result = self.drive(self.loss_steps(['abcdefghijk']))
        self.assertEqual((result['updated'], result['public'], result['errors']), (1, 1, 0))
        self.assertEqual([kind for kind, _ in self.calls], ['metadata'])
        self.assertEqual(self.repository.scopes, [[{'channel_id': CHANNEL, 'control_epoch': 7}]])
        self.assertEqual([kind for kind, _ in self.repository.writes], ['metadata'])

    def test_more_than_fifty_uses_one_action_and_same_claim_run(self):
        ids = [f'v{number:010d}' for number in range(51)]
        result = self.drive(self.loss_steps(ids))
        self.assertEqual(result['updated'], 51)
        self.assertEqual([len(values) for values, _ in self.repository.claims], [50, 1])
        self.assertEqual(len({run for _, run in self.repository.claims}), 1)
        self.assertEqual(self.repository.prepares, 1)
        self.assertEqual([len(values) for _, values in self.calls], [50, 1])

    def test_hidden_epoch_change_does_not_reauthorize_remaining_ids(self):
        original = self.metadata
        def metadata(ids, reason):
            result = original(ids, reason)
            self.repository.epoch = 8
            return result
        self.collector.fetch_metadata = metadata
        result = self.drive(self.loss_steps([f'v{number:010d}' for number in range(51)]))
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.repository.claims), 1)
        self.status_counts.assert_not_called()

    def test_unknown_commit_stops_followup_and_keeps_original_batch(self):
        self.repository.fail_commit = True
        result = self.drive(self.loss_steps([f'v{number:010d}' for number in range(51)]))
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(self.repository.claims), 1)
        self.assertEqual(len(self.calls), 1)
        self.status_counts.assert_not_called()

    def test_unknown_prepare_sends_no_request(self):
        self.repository.prepare_error = True
        result = self.drive(self.loss_steps(['abcdefghijk']))
        self.assertTrue(result['settlement_unknown'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.repository.claims, [])
        self.status_counts.assert_not_called()

    def test_stale_recheck_preserves_trigger_and_exact_ids(self):
        self.stack.enter_context(patch.object(availability, 'load_availability_recheck_candidate_ids',
            return_value=['abcdefghijk']))
        result = self.drive(availability.availability_recheck_steps(CHANNEL,
            {'status': 'no_new_videos'}, trigger_allowed=False, context=self.context,
            expected_epoch=7, run_video_steps=self.run_video_steps))
        self.assertEqual(result['trigger'], 'stale_unavailable')
        self.assertEqual(result['restored'], 1)
        self.assertEqual(self.repository.claims[0][0], ['abcdefghijk'])

    def test_non_trigger_does_not_prepare_or_send(self):
        result = self.drive(availability.availability_recheck_steps(CHANNEL,
            {'status': 'new_videos_found'}, context=self.context,
            expected_epoch=7, run_video_steps=self.run_video_steps))
        self.assertFalse(result['triggered'])
        self.assertEqual(self.repository.prepares, 0)
        self.assertEqual(self.calls, [])

    def test_invalid_parent_scope_stops_before_selection(self):
        with self.assertRaises(ValueError):
            self.drive(availability.availability_loss_batch_steps([CHANNEL],
                context=self.context, channel_scopes=[], run_video_steps=self.run_video_steps))
        self.assertEqual(self.repository.prepares, 0)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
