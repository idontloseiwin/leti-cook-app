"""Opt-in metadata credit/drain tests; real steps/threads, fake persistence only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, get_ident
from types import SimpleNamespace
import time
import unittest
from unittest.mock import Mock, patch

import test_channel_metadata_steps as fixtures
from test_channel_metadata_steps import CHANNEL, row, item
from collection import channel_metadata as metadata
from collection import metadata_dispatch as adapter
from collection.dispatch import Ack, Completion, Coordinator, DispatchBlocked, Prepared, Skipped
from collection.provider_steps import ProviderResult
from hot_runtime import HotDatabaseAdmissionDenied, HotRuntimeUnavailable
import collection_shutdown


class MetadataDispatchTests(unittest.TestCase):
    db = fixtures.MetadataProviderStepsTests.db
    persist = fixtures.MetadataProviderStepsTests.persist
    fetch = fixtures.MetadataProviderStepsTests.fetch

    def setUp(self):
        fixtures.MetadataProviderStepsTests.setUp(self)
        self.stack.enter_context(patch.object(collection_shutdown, '_requested', Event()))
        self.stack.enter_context(patch.object(adapter, 'ChannelSyncRepository', return_value=self.repository))
        self.repository.update_channel.side_effect = lambda *a, **kw: self.db('status')
        self.repository.clear_initial_inventory_error.side_effect = lambda *a, **kw: self.db('clear') or {'channel_id': CHANNEL, 'cleared': True}
        self.repository.drain_metadata_running.side_effect = lambda rows: self.db('drain') or {'status': 'drained', 'remaining': 0}
        self.progress, self.ready = Mock(), Mock()
        self.context.enqueue_initial_inventory = Mock(return_value={'channel_id': CHANNEL, 'job_id': 'known-job', 'status': 'queued'})
        self.context.sources.parse_dt = lambda value: datetime.fromisoformat(value) if value else None
        self.channels = [row()]

    def unit(self, **kwargs):
        return adapter.MetadataBatchUnit(self.context, key='metadata', lane='auto', channels=self.channels,
            require_ready=self.ready, on_settled=self.progress, **kwargs)

    def response(self, prepared):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(prepared.fetch, Event()).result(timeout=2)

    def settle(self, unit, prepared):
        return unit.settle(Completion('response', value=self.response(prepared), fetch_started=True))

    def recheck(self, count=0):
        self.channels = [row()]
        self.channels[0].update(collection_mode='metadata_only',
            initial_long_inventory_status='skipped_no_long_playlist', initial_long_inventory_error_code='failed')
        def count_source(identity, **kwargs):
            self.assertNotEqual(get_ident(), self.owner)
            self.events.append('count-source')
            return count
        self.stack.enter_context(patch.object(metadata, 'get_long_video_count', side_effect=count_source))

    def test_optional_global_stop_drains_between_provider_steps(self):
        self.recheck()
        stopped=Event()
        unit=self.unit(should_stop=stopped.is_set)
        prepared=unit.prepare()
        response=self.response(prepared)
        stopped.set()
        self.assertIsInstance(unit.settle(Completion('response',value=response,fetch_started=True)),Ack)
        self.assertEqual(unit.summary['provider_steps'],1)
        self.assertNotIn('count-source',self.events)
        self.repository.drain_metadata_running.assert_called_once()

    def test_real_provider_thread_and_owner_writes_same_captured_scope(self):
        unit = self.unit()
        prepared = unit.prepare()
        self.assertEqual(self.events, ['preflight', 'running'])
        self.assertIsInstance(self.settle(unit, prepared), Ack)
        self.assertEqual(self.events, ['preflight', 'running', ('fetch', (CHANNEL,)), 'persist'])
        self.assertEqual(unit.summary['updated'], 1)
        self.assertEqual(unit.summary['provider_steps'], 1)
        self.assertFalse(unit.state.pending)
        self.progress.assert_called_once()

    def test_count_continuation_preserves_credit_deadline_and_owner_clear(self):
        self.recheck()
        unit = self.unit()
        first = unit.prepare()
        second = self.settle(unit, first)
        self.assertIsInstance(second, Prepared)
        self.assertEqual(second.deadline, first.deadline)
        self.progress.assert_not_called()
        self.assertIsInstance(self.settle(unit, second), Ack)
        self.assertEqual(unit.summary['provider_steps'], 2)
        self.assertEqual(unit.summary['still_no_long_playlist'], 1)
        self.assertIn('clear', self.events)
        self.repository.clear_initial_inventory_error.assert_called_once_with(CHANNEL, expected_epoch=7)

    def test_bounded_captured_input_and_one_pass_no_due_loader(self):
        unit = self.unit()
        self.channels[0]['collection_control_epoch'] = 8
        with patch.object(metadata, 'load_due_channel_metadata_channels', side_effect=AssertionError('unexpected paging')):
            prepared = unit.prepare()
            self.assertIsInstance(self.settle(unit, prepared), Ack)
        self.assertEqual(self.repository.mark_metadata_running.call_args.args[0][0]['collection_control_epoch'], 7)
        for values in ([], [row()] * 2, [row('UC' + f'{i:022d}') for i in range(51)]):
            self.channels = values
            with self.assertRaises(ValueError):
                self.unit()

    def test_cancel_or_expiry_unsent_drains_exact_marker_before_progress(self):
        for reason in ('cancelled', 'expired'):
            with self.subTest(reason=reason):
                unit = self.unit()
                unit.prepare()
                self.assertIsInstance(unit.settle(Completion(reason)), Ack)
                marker = self.repository.drain_metadata_running.call_args.args[0][0]
                self.assertEqual(marker['channel_id'], CHANNEL)
                self.assertEqual(marker['control_epoch'], 7)
                self.assertEqual(marker['started_at'], self.repository.mark_metadata_running.call_args.args[1])
                self.assertTrue(unit.summary['incomplete'])
                self.assertEqual(unit.summary.get('provider_steps', 0), 0)
                self.assertFalse(unit.state.pending)

    def test_known_hot_denial_at_continuation_drains_without_count_attempt(self):
        self.recheck()
        self.ready.side_effect = [None, None, HotRuntimeUnavailable()]
        unit = self.unit()
        self.assertIsInstance(self.settle(unit, unit.prepare()), Ack)
        self.assertEqual(unit.summary['provider_steps'], 1)
        self.assertTrue(unit.summary['hot_runtime_interrupted'])
        self.assertNotIn('count-source', self.events)
        self.repository.drain_metadata_running.assert_called_once()

    def test_known_hot_denied_metadata_write_drains_previously_marked_scope(self):
        self.repository.update_metadata_batch.side_effect = HotDatabaseAdmissionDenied()
        unit = self.unit()
        self.assertIsInstance(self.settle(unit, unit.prepare()), Ack)
        self.repository.drain_metadata_running.assert_called_once()
        self.assertTrue(unit.summary['incomplete'])

    def test_known_hot_before_running_mark_does_not_invent_owned_marker(self):
        self.repository.mark_metadata_running.side_effect = HotDatabaseAdmissionDenied()
        unit = self.unit()
        self.assertIsInstance(unit.prepare(), Skipped)
        self.repository.drain_metadata_running.assert_not_called()
        self.progress.assert_called_once()

    def test_unknown_running_mark_blocks_real_coordinator_without_replay(self):
        self.repository.mark_metadata_running.side_effect = OSError('synthetic unknown')
        unit = self.unit()
        with ThreadPoolExecutor(max_workers=1) as executor:
            owner = Coordinator(executor, 1)
            owner.offer(unit.work)
            for _ in range(2):
                with self.assertRaises(DispatchBlocked):
                    owner.step()
            self.assertEqual(owner.snapshot().held, 1)
        self.repository.mark_metadata_running.assert_called_once()
        self.repository.drain_metadata_running.assert_not_called()
        self.progress.assert_not_called()

    def test_unknown_metadata_commit_blocks_real_coordinator_without_cleanup(self):
        self.repository.update_metadata_batch.side_effect = OSError('synthetic lost ack')
        unit = self.unit()
        with ThreadPoolExecutor(max_workers=1) as executor:
            owner = Coordinator(executor, 1)
            owner.offer(unit.work)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    owner.step()
                except DispatchBlocked:
                    break
                owner.wait(.01)
            else:
                self.fail('unknown write did not block')
            self.assertEqual(owner.snapshot().held, 1)
            with self.assertRaises(DispatchBlocked):
                owner.step()
        self.repository.update_metadata_batch.assert_called_once()
        self.repository.update_channel.assert_not_called()
        self.repository.load_channel_record.assert_not_called()
        self.repository.drain_metadata_running.assert_not_called()
        self.progress.assert_not_called()

    def test_prior_unknown_flag_overrides_known_admission_denial(self):
        error = HotDatabaseAdmissionDenied()
        error.outcome_may_be_committed = True
        self.repository.update_metadata_batch.side_effect = error
        unit = self.unit()
        with self.assertRaises(metadata.MetadataSettlementUnknown):
            self.settle(unit, unit.prepare())
        self.repository.drain_metadata_running.assert_not_called()

    def test_unknown_clear_does_not_become_still_no_long_ack(self):
        self.recheck()
        self.repository.clear_initial_inventory_error.side_effect = OSError('unknown clear')
        unit = self.unit()
        next_step = self.settle(unit, unit.prepare())
        with self.assertRaises(metadata.MetadataSettlementUnknown):
            self.settle(unit, next_step)
        self.repository.clear_initial_inventory_error.assert_called_once()
        self.repository.update_metadata_batch.assert_not_called()
        self.progress.assert_not_called()

    def test_unknown_missing_and_error_marker_writes_do_not_ack(self):
        for response in (ProviderResult(value=[], observed_at=datetime.now(timezone.utc)),
                         ProviderResult(error=RuntimeError('provider failure'))):
            with self.subTest(error=response.error):
                self.repository.update_channel.side_effect = OSError('unknown marker write')
                unit = self.unit()
                unit.prepare()
                with self.assertRaises(metadata.MetadataSettlementUnknown):
                    unit.settle(Completion('response', value=response, fetch_started=True))
                self.progress.assert_not_called()

    def test_unknown_or_denied_enqueue_retains_committed_metadata_summary(self):
        self.recheck(count=1)
        for error in (OSError('unknown enqueue'), HotDatabaseAdmissionDenied()):
            with self.subTest(error=type(error).__name__):
                self.context.enqueue_initial_inventory.side_effect = error
                unit = self.unit()
                second = self.settle(unit, unit.prepare())
                with self.assertRaises(metadata.MetadataSettlementUnknown):
                    self.settle(unit, second)
                self.assertEqual(unit.summary['updated'], 1)
                self.assertFalse(unit.state.pending)
                self.progress.assert_not_called()
                self.repository.drain_metadata_running.assert_not_called()

    def test_successful_enqueue_and_deferral_keep_original_epoch_and_ack(self):
        self.recheck(count=1)
        self.context.enqueue_initial_inventory.return_value['status'] = 'error'
        unit = self.unit()
        second = self.settle(unit, unit.prepare())
        self.assertIsInstance(self.settle(unit, second), Ack)
        self.context.enqueue_initial_inventory.assert_called_once_with(CHANNEL, expected_epoch=7)
        self.assertEqual(unit.summary['no_long_reactivation_deferred'], 1)
        self.assertEqual(self.repository.update_channel.call_args.kwargs['expected_epoch'], 7)
        self.progress.assert_called_once()

    def test_unknown_deferral_after_committed_enqueue_holds_credit(self):
        self.recheck(count=1)
        self.context.enqueue_initial_inventory.return_value['status'] = 'error'
        self.repository.update_channel.side_effect = OSError('unknown deferral')
        unit = self.unit()
        second = self.settle(unit, unit.prepare())
        with self.assertRaises(metadata.MetadataSettlementUnknown):
            self.settle(unit, second)
        self.assertEqual(unit.summary['updated'], 1)
        self.progress.assert_not_called()

    def test_unknown_priority_holds_credit_after_successful_source_commit(self):
        self.context.settings.typed_playlist_inventory_enabled = True
        self.stack.enter_context(patch.object(metadata, 'has_completed_initial_long_inventory', return_value=True))
        self.channels[0]['video_count'] = 0
        typed = Mock()
        typed.prioritize_channels.side_effect = OSError('unknown priority')
        self.stack.enter_context(patch.object(metadata, 'TypedInventoryRepository', return_value=typed))
        unit = self.unit()
        with self.assertRaises(metadata.MetadataSettlementUnknown):
            self.settle(unit, unit.prepare())
        self.assertEqual(unit.summary['updated'], 1)
        typed.prioritize_channels.assert_called_once()
        self.progress.assert_not_called()

    def test_unknown_drain_or_progress_not_replayed(self):
        for method in (self.repository.drain_metadata_running, self.progress):
            with self.subTest(method=method):
                method.side_effect = OSError('unknown terminal operation')
                unit = self.unit()
                unit.prepare()
                with self.assertRaises(OSError):
                    unit.settle(Completion('cancelled'))
                with self.assertRaisesRegex(RuntimeError, 'settle_repeated'):
                    unit.settle(Completion('cancelled'))
                method.side_effect = None

    def test_partial_cancel_keeps_completed_clear_but_drains_remaining_marker(self):
        self.recheck()
        second_channel = row('UC' + 'b' * 22)
        second_channel.update(self.channels[0], channel_id='UC' + 'b' * 22)
        self.channels.append(second_channel)
        self.repository.clear_initial_inventory_error.side_effect = lambda identity, **kw: self.db('clear') or {'channel_id': identity, 'cleared': True}
        unit = self.unit()
        step = self.settle(unit, unit.prepare())
        second_count = self.settle(unit, step)
        self.assertIsInstance(second_count, Prepared)
        self.assertIn('clear', self.events)
        self.assertEqual(unit.summary['initial_inventory_errors_cleared'], 1)
        self.assertIsInstance(unit.settle(Completion('cancelled')), Ack)
        self.assertEqual(len(self.repository.drain_metadata_running.call_args.args[0]), 2)
        self.assertEqual(unit.summary['provider_steps'], 2)

    def test_provider_payload_without_response_envelope_is_unknown(self):
        unit = self.unit()
        unit.prepare()
        with self.assertRaisesRegex(RuntimeError, 'response_unknown'):
            unit.settle(Completion('fetch_error', error=OSError('unwrapped callback')))
        self.progress.assert_not_called()
        self.repository.drain_metadata_running.assert_not_called()

    def test_daily_quota_ack_follows_known_error_markers_without_source_refresh(self):
        self.context.sources.is_youtube_daily_quota_exhausted = lambda error: True
        unit = self.unit()
        unit.prepare()
        self.assertIsInstance(unit.settle(Completion('response',
            value=ProviderResult(error=RuntimeError('synthetic daily quota')), fetch_started=True)), Ack)
        self.assertTrue(unit.summary['quota_exhausted'])
        self.assertFalse(unit.state.pending)
        values = self.repository.update_channel.call_args.args[1]
        self.assertNotIn('channel_metadata_checked_at', values)
        self.assertEqual(values['channel_metadata_error_count'], 0)
        self.repository.update_metadata_batch.assert_not_called()

    def test_later_followup_failure_retains_all_earlier_batch_commit_scopes(self):
        self.recheck(count=1)
        other = 'UC' + 'b' * 22
        self.channels.append(row(other))
        self.context.enqueue_initial_inventory.side_effect = OSError('unknown enqueue')
        unit = self.unit()
        second = self.settle(unit, unit.prepare())
        with self.assertRaises(metadata.MetadataSettlementUnknown):
            self.settle(unit, second)
        self.assertEqual(unit.summary['metadata_persisted_channel_scopes'], [
            {'channel_id': other, 'control_epoch': 7}, {'channel_id': CHANNEL, 'control_epoch': 7}])
        self.assertEqual(unit.summary['updated'], 1)
        self.progress.assert_not_called()

    def test_captured_running_hidden_and_not_due_candidates_do_not_mark_or_dispatch(self):
        for changes in ({'channel_metadata_status': 'running'}, {'is_hidden': True},
                        {'channel_metadata_next_after': '9999-01-01T00:00:00+00:00'}):
            with self.subTest(changes=changes):
                self.channels = [dict(row(), **changes)]
                unit = self.unit()
                self.assertIsInstance(unit.prepare(), Skipped)
                self.assertEqual(self.events, [])
                self.progress.assert_not_called()

    def test_owner_methods_cannot_run_in_provider_thread(self):
        unit = self.unit()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaisesRegex(RuntimeError, 'owner_thread'):
                pool.submit(unit.prepare).result(timeout=2)
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
