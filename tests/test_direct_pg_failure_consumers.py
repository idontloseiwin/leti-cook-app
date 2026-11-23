"""Offline cutover contracts for owned failures and Saved maintenance dispatch."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.videos import video_failures
from db.hot.hot_collection_control import uncertain_collection_ack
from db.hot.hot_retention import CleanupPage, HotRetentionRepository, LocalTestRetentionRepository, RetentionError
from db.base.postgres import DatabaseUnavailable
from db.videos.video_collection import VideoCollectionRepository
from hot_runtime import HotDatabaseAdmissionDenied
from test_video_failures import Database, payload, parsed, locked

TARGET = 'deleted_saved_objects'
IDENTITY = 'c0000000-0000-4000-8000-000000000061'


def page(**changes):
    result = dict(status='ok', target=TARGET, as_of='2026-10-06T12:00:00+00:00',
                  cutoff='2026-09-06T12:00:00+00:00', scanned=1, deleted=1,
                  scrubbed=0, deferred=0, complete=False, next_cursor=IDENTITY,
                  oldest_remaining_expired_source_at=None, invalid_clock_remaining=0)
    result.update(changes)
    return result


class FailureConsumerTests(unittest.TestCase):
    def setUp(self):
        self.database = Mock()
        self.database.call.side_effect = AssertionError('failure RPC forbidden')
        self.repository = VideoCollectionRepository(self.database)
        self.telemetry_patch = patch('db.videos.video_collection.emit_event')
        self.telemetry = self.telemetry_patch.start()
        self.addCleanup(self.telemetry_patch.stop)

    def test_all_three_consumers_execute_atomic_protocol_without_rpc(self):
        for mode, kind in (('long', 'long'), ('typed', 'short'), ('common', 'stream')):
            if mode == 'long':
                database = Database({'count': 1}, [parsed()], [locked()], 1)
            elif mode == 'typed':
                database = Database({'video_type': kind}, [locked(video_type=kind)], [parsed(video_type=kind)], 1)
            else:
                database = Database([parsed(video_type=kind)], [locked(video_type=kind)], 1)
            database.call = Mock(side_effect=AssertionError('failure RPC forbidden'))
            repository = VideoCollectionRepository(database)
            with patch('db.channel.channel_control.assert_channel_scopes') as fence:
                result = repository.fail([payload()]) if mode == 'long' else (
                    repository.fail_typed(kind, [payload(video_type=kind)]) if mode == 'typed'
                    else repository.fail_batch([payload(video_type=kind)]))
            self.assertEqual(result, 1)
            self.assertEqual(database.events, ['commit'])
            self.assertEqual(len(database.operations), 1)
            self.assertIn('UPDATE public.video_collection_state', database.calls[-1][1])
            fence.assert_called_once()
            database.call.assert_not_called()

    def test_long_batches_stay_at_1000_and_preserve_order(self):
        rows = [payload(video_id=str(index)) for index in range(1001)]
        with patch.object(video_failures, 'fail_video_collection_claims', side_effect=lambda database, rows: len(rows)) as helper:
            self.assertEqual(self.repository.fail(rows), 1001)
        self.assertEqual([len(call.args[1]) for call in helper.call_args_list], [1000, 1])
        self.assertEqual([row for call in helper.call_args_list for row in call.args[1]], rows)
        self.database.call.assert_not_called()

    def test_typed_batches_stay_at_50_and_normalize_type_only(self):
        rows = [payload(video_id=str(index), error_type=None) for index in range(51)]
        with patch.object(video_failures, 'fail_typed_video_collection_claims', side_effect=lambda database, kind, rows: len(rows)) as helper:
            self.assertEqual(self.repository.fail_typed(' SHORT ', rows), 51)
        self.assertEqual([call.args[1] for call in helper.call_args_list], ['short', 'short'])
        self.assertEqual([len(call.args[2]) for call in helper.call_args_list], [50, 1])
        self.assertIsNot(helper.call_args_list[0].args[2][0], rows[0])
        self.assertIsNone(helper.call_args_list[0].args[2][0]['error_type'])

    def test_common_normalization_and_fifty_row_limit_remain(self):
        raw = payload(video_id=' aaaaaaaaaaa ', video_type=' STREAM ', error_type=' fetch_failed ', retry_after_seconds='30', ignored='discard')
        with patch.object(video_failures, 'fail_video_collection_batch', return_value=1) as helper:
            self.assertEqual(self.repository.fail_batch([raw]), 1)
            helper.assert_called_once_with(self.database, [payload(video_type='stream', retry_after_seconds=30)])
            with self.assertRaises(ValueError): self.repository.fail_batch([payload()] * 51)
            with self.assertRaises(ValueError): self.repository.fail_batch([payload(), payload()])
            with self.assertRaises(ValueError): self.repository.fail_batch([payload(retry_after_seconds=True)])
            self.assertEqual(helper.call_count, 1)

    def test_empty_inputs_do_not_dispatch_and_typed_still_validates_type(self):
        with patch.object(video_failures, 'fail_video_collection_claims') as long, \
             patch.object(video_failures, 'fail_typed_video_collection_claims') as typed, \
             patch.object(video_failures, 'fail_video_collection_batch') as common:
            self.assertEqual(self.repository.fail([]), 0)
            self.assertEqual(self.repository.fail_typed('short', []), 0)
            self.assertEqual(self.repository.fail_batch([]), 0)
            with self.assertRaises(ValueError): self.repository.fail_typed('long', [])
            long.assert_not_called(); typed.assert_not_called(); common.assert_not_called()

    def test_partial_count_remains_an_error_for_every_consumer(self):
        for method, name, args in self.variants():
            with patch.object(video_failures, name, return_value=0) as helper:
                with self.assertRaisesRegex(RuntimeError, 'released 0 of 1'): method(*args)
                helper.assert_called_once()

    def variants(self):
        return ((self.repository.fail, 'fail_video_collection_claims', ([payload()],)),
                (self.repository.fail_typed, 'fail_typed_video_collection_claims', ('short', [payload(video_type='short')])),
                (self.repository.fail_batch, 'fail_video_collection_batch', ([payload(video_type='stream')],)))

    def test_unknown_ack_propagates_once_without_rpc_or_abort(self):
        for method, name, args in self.variants():
            failure = DatabaseUnavailable('database_connection_failed', code='08006', outcome_unknown=True)
            with patch.object(video_failures, name, side_effect=failure) as helper, \
                 patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort:
                with self.assertRaises(DatabaseUnavailable) as caught: method(*args)
                self.assertIs(caught.exception, failure)
                self.assertTrue(caught.exception.outcome_unknown)
                helper.assert_called_once(); abort.assert_not_called()
        self.database.call.assert_not_called()

    def test_pre_dispatch_denial_keeps_exact_owned_abort_identity(self):
        for method, name, args in self.variants():
            failure = HotDatabaseAdmissionDenied()
            with patch.object(video_failures, name, side_effect=failure), \
                 patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort', return_value={}) as abort:
                with self.assertRaises(HotDatabaseAdmissionDenied) as caught: method(*args)
                row = args[-1][0]
                abort.assert_called_once_with(videos=[{key: row[key] for key in ('video_id', 'video_type', 'lease_token')}])
                self.assertIs(caught.exception, failure)
                self.assertTrue(failure.control_abort_attempted)

    def test_prior_uncertain_ack_denial_never_aborts_owned_claim(self):
        for method, name, args in self.variants():
            failure = HotDatabaseAdmissionDenied()
            with patch.object(video_failures, name, side_effect=failure), \
                 patch('db.hot.hot_collection_control.HotCollectionControlRepository.abort') as abort, uncertain_collection_ack():
                with self.assertRaises(HotDatabaseAdmissionDenied): method(*args)
                self.assertTrue(failure.outcome_may_be_committed)
                self.assertEqual(failure.control_video_ids, ('aaaaaaaaaaa',))
                abort.assert_not_called()

    def test_telemetry_keeps_legacy_operation_and_bounded_status(self):
        for method, name, args in self.variants():
            self.telemetry.reset_mock()
            with patch.object(video_failures, name, return_value=1): method(*args)
            self.assertEqual([call.args[3] for call in self.telemetry.call_args_list], ['rpc:' + name] * 2)
            self.assertEqual([call.kwargs['status'] for call in self.telemetry.call_args_list], ['started', 'success'])
            self.assertEqual(self.telemetry.call_args_list[1].kwargs['count'], None)


class SavedCleanupConsumerTests(unittest.TestCase):
    def setUp(self):
        self.database = Mock()
        self.database.call.side_effect = AssertionError('Saved cleanup RPC forbidden')
        self.database.cleanup_deleted_saved_objects.return_value = page()
        self.repository = HotRetentionRepository(self.database)

    def test_saved_dispatch_uses_closed_entry_and_parses_page(self):
        result = self.repository.cleanup_page(TARGET, after_id=IDENTITY, limit=3)
        self.assertIsInstance(result, CleanupPage)
        self.assertEqual((result.deleted, result.next_cursor, result.complete), (1, IDENTITY, False))
        self.database.cleanup_deleted_saved_objects.assert_called_once_with(target=TARGET, after_id=IDENTITY, limit=3)
        self.database.call.assert_not_called()
        self.assertNotIn('next_cursor', result.summary())
        self.assertEqual(result.summary()['telemetry_scope'], 'scanned_page')

    def test_other_families_keep_their_named_maintenance_entry(self):
        self.database.cleanup_hot_inventory.return_value = page(target='inventory', cutoff='2026-09-07T12:00:00+00:00')
        self.assertEqual(self.repository.cleanup_page('inventory', limit=1).target, 'inventory')
        self.database.cleanup_hot_inventory.assert_called_once_with(target='inventory', after_id=None, limit=1)
        self.database.call.assert_not_called()
        self.database.cleanup_deleted_saved_objects.assert_not_called()

    def test_local_synthetic_override_remains_the_dispatch_seam(self):
        repository = LocalTestRetentionRepository(psql='synthetic_psql', database='typed_audit_cleanup', port=5439)
        with patch.object(repository, '_call', return_value=page()) as local:
            self.assertEqual(repository.cleanup_page(TARGET).deleted, 1)
            local.assert_called_once_with('cleanup_deleted_saved_objects', {'p_target': TARGET, 'p_after_id': None, 'p_limit': 100})
        self.assertFalse(hasattr(repository, 'database'))

    def test_local_validation_stays_before_dispatch(self):
        for kwargs in ({'limit': 101}, {'limit': True}, {'limit': None}, {'after_id': ''}, {'after_id': '\0'}, {'after_id': 'x' * 2049}):
            with self.assertRaises(ValueError): self.repository.cleanup_page(TARGET, **kwargs)
        with self.assertRaises(ValueError): self.repository.cleanup_page('unowned')
        self.database.cleanup_deleted_saved_objects.assert_not_called()
        self.database.call.assert_not_called()

    def test_uncertain_commit_and_admission_errors_are_safe_without_retry(self):
        for failure in (DatabaseUnavailable('private data', code='08006', outcome_unknown=True), HotDatabaseAdmissionDenied()):
            self.database.cleanup_deleted_saved_objects.reset_mock()
            self.database.cleanup_deleted_saved_objects.side_effect = failure
            with self.assertRaises(RetentionError) as caught: self.repository.cleanup_page(TARGET)
            self.assertEqual(str(caught.exception), 'cleanup_outcome_unknown')
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
            self.database.cleanup_deleted_saved_objects.assert_called_once()
            self.database.call.assert_not_called()

    def test_bad_result_is_rejected_after_one_call(self):
        self.database.cleanup_deleted_saved_objects.return_value = page(cutoff='2026-09-08T12:00:00+00:00')
        with self.assertRaisesRegex(RetentionError, 'invalid_cleanup_result'): self.repository.cleanup_page(TARGET)
        self.database.cleanup_deleted_saved_objects.assert_called_once()


if __name__ == '__main__': unittest.main()
