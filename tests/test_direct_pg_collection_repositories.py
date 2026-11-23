"""Offline collection repository contracts using only the direct database API."""
from __future__ import annotations

import ast
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))

from db.videos.availability import AvailabilityRepository
from db.channel.channel_discovery import ChannelDiscoveryRepository
from db.channel.channel_sync import ChannelSyncRepository, ChannelMetadataColumnsUnavailableError
from db.inventory.inventory_maintenance import InventoryMaintenanceRepository
from db.jobs.operator_jobs import OperatorJobRepository
from db.base.postgres import DatabaseServerError, DatabaseUnavailable
from db.inventory.typed_inventory import TypedInventoryRepository, TypedInventoryClaim, InventoryTokenCycleError
from db.videos.video_collection import VideoCollectionRepository
from collection_schedule import CollectionSchedules

MODULES = ('videos/availability', 'channel/channel_discovery', 'channel/channel_sync', 'inventory/inventory_maintenance',
           'videos/video_collection', 'inventory/typed_inventory', 'jobs/operator_jobs')


class RecordingDatabase:
    """No table/rpc/query DSL; every query records SQL, parameters and its bound."""
    def __init__(self, *results):
        self.results = deque(results)
        self.cadence_snapshot = CollectionSchedules().cadence_snapshot()
        self.calls = []

    def _record(self, kind, operation, statement, parameters, max_rows):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', operation):
            raise AssertionError(f'Invalid operation token: {operation}')
        text = statement if isinstance(statement, str) else statement.as_string()
        self.calls.append((kind, operation, text, parameters, max_rows))
        value = self.results.popleft() if self.results else ([] if kind.endswith('rows') else None)
        if isinstance(value, Exception):
            raise value
        return value

    def read_rows(self, operation, statement, parameters=(), *, max_rows):
        return self._record('read_rows', operation, statement, parameters, max_rows)

    def read_row(self, operation, statement, parameters=()):
        return self._record('read_row', operation, statement, parameters, 1)

    def write(self, operation, statement, parameters=()):
        return self._record('write', operation, statement, parameters, None)

    def write_rows(self, operation, statement, parameters=(), *, max_rows):
        return self._record('write_rows', operation, statement, parameters, max_rows)

    def call(self, name, parameters=None, *, max_rows=1000):
        return self._record('call', name, name, parameters or {}, max_rows)


class DirectCollectionRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.dispatches = []

        def dispatch(database, name, params, callback, *, abort_on_denied=True):
            self.dispatches.append((name, params, abort_on_denied))
            return callback()

        self.patch = mock.patch('db.hot.hot_collection_control.dispatch_owned_rpc', side_effect=dispatch)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_availability_interval_omission_and_explicit_interval(self):
        db = RecordingDatabase([{'video_id': 'v'}], [{'video_id': 'v'}])
        repository = AvailabilityRepository(db)
        self.assertEqual(repository.load_recheck_candidate_ids('c', ['private'], limit=10), ['v'])
        self.assertIsNone(db.calls[0][3]['p_recheck_after'])
        self.assertEqual(db.calls[0][0], 'read_rows')
        repository.load_recheck_candidate_ids('c', ['private'], limit=10, recheck_after=timedelta(hours=2))
        self.assertEqual(db.calls[1][3]['p_recheck_after'], '7200 seconds')
        self.assertEqual(len(db.calls), 2)

    def test_availability_inventory_paging_keeps_offsets_and_descending_nulls(self):
        db = RecordingDatabase([{'video_id': str(n)} for n in range(1000)],
                               [{'video_id': '1000'}, {'video_id': '1001'}])
        result = AvailabilityRepository(db).load_long_inventory_candidate_ids('c', limit=1002)
        self.assertEqual(len(result), 1002)
        self.assertEqual([c[3] for c in db.calls], [('c', 1000, 0), ('c', 2, 1000)])
        self.assertEqual([c[4] for c in db.calls], [1000, 2])
        self.assertIn('published_at DESC NULLS FIRST, video_id ASC', db.calls[0][2])

    def test_availability_id_batches_bind_ids_without_query_interpolation(self):
        malicious = "video'); DELETE FROM videos; --"
        ids = [malicious] + [str(n) for n in range(100)]
        db = RecordingDatabase([], [])
        AvailabilityRepository(db).load_video_availability_rows(ids)
        self.assertEqual([call[4] for call in db.calls], [100, 1])
        self.assertNotIn(malicious, db.calls[0][2])
        self.assertEqual(db.calls[0][3][0][0], malicious)

    def test_discovery_source_cutoff_remains_strict_and_missing_clock_stops(self):
        rows = [
            {'video_id': 'older', 'channel_id': 'c', 'video_type': 'long', 'discovered_at': '2026-10-04T00:00:00Z'},
            {'video_id': 'equal', 'channel_id': 'c', 'video_type': 'long', 'discovered_at': '2026-10-05T00:00:00Z'},
            {'video_id': 'missing', 'channel_id': 'c', 'video_type': 'long', 'discovered_at': None},
            {'video_id': 'short', 'channel_id': 'c', 'video_type': 'short', 'discovered_at': None},
        ]
        result = ChannelDiscoveryRepository(RecordingDatabase(rows)).load_inventory_state(
            'c', [r['video_id'] for r in rows], discovered_before='2026-10-05T00:00:00Z',
        )
        self.assertEqual(result['existing_ids'], {'older', 'equal', 'missing', 'short'})
        self.assertEqual(result['stop_ids'], {'older', 'missing'})

    def test_discovery_plain_readiness_results_without_retired_writer(self):
        db = RecordingDatabase([{'selected_count': 1, 'affected_count': 1}])
        repository = ChannelDiscoveryRepository(db)
        with mock.patch('db.channel.channel_discovery.capabilities.telemetry_check', side_effect=[{'status': 'ok'}, {'status': 'ready'}]) as check:
            self.assertEqual(repository.assert_paging_schema(), {'status': 'ok'})
            self.assertEqual(repository.preflight_queue_ready(), {'status': 'ready'})
        self.assertEqual([call.args[0] for call in check.call_args_list], [db, db])
        self.assertEqual([call.args[1].__name__ for call in check.call_args_list],
                         ['assert_channel_discovery_paging_schema', 'assert_channel_discovery_queue_ready'])
        self.assertTrue(all(call.kwargs == {'phase': 'preflight'} for call in check.call_args_list))
        self.assertFalse(hasattr(repository, 'backfill'))
        self.assertFalse(hasattr(repository, '_rpc'))
        self.assertEqual(db.calls, [])

    def test_channel_sync_preflight_counts_are_not_limited_to_one_row(self):
        channels = [
            {'channel_id': 'active', 'collection_mode': 'active', 'collection_permission': 'accepted',
             'collection_control_epoch': 2, 'is_hidden': False},
            {'channel_id': 'hidden', 'collection_mode': 'hidden', 'collection_permission': 'accepted',
             'collection_control_epoch': 2, 'is_hidden': True},
        ]
        db = RecordingDatabase(channels, {'count': 12345}, {'count': 6789})
        result = ChannelSyncRepository(db).eligibility_preflight()
        self.assertEqual(result['proposed_database_effects']['collection_rows_to_pause'], 12345)
        self.assertEqual(result['proposed_database_effects']['collection_rows_to_resume'], 6789)
        self.assertIn('count(*)', db.calls[1][2])
        self.assertNotIn('LIMIT', db.calls[0][2])
        self.assertEqual(db.calls[0][4], 10000)
        self.assertIn('paused_reason IS NULL', db.calls[1][2])
        self.assertIn('paused_reason = ANY(%s::text[])', db.calls[2][2])
        self.assertEqual(db.calls[1][3], (['hidden'],))

    def test_channel_metadata_clock_is_bound_and_null_order_and_page_are_explicit(self):
        db = RecordingDatabase([])
        ChannelSyncRepository(db).load_due_metadata_page(
            datetime(2026, 10, 5, tzinfo=timezone(timedelta(hours=3))), page=2, page_size=25,
        )
        query = db.calls[0]
        self.assertEqual(query[3], ('2026-10-04T21:00:00Z', 25, 50))
        self.assertIn('channel_metadata_next_after IS NULL OR', query[2])
        self.assertIn('ASC NULLS LAST, channel_id ASC', query[2])
        self.assertNotIn('2026', query[2])

    def test_optional_columns_require_confirmed_undefined_column_sqlstate(self):
        now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        db = RecordingDatabase(DatabaseServerError('database_operation_failed', code='42703'))
        with self.assertRaises(ChannelMetadataColumnsUnavailableError):
            ChannelSyncRepository(db).load_due_metadata_page(now, page=0, page_size=10)
        error = DatabaseUnavailable('database_connection_failed', code='08006')
        with self.assertRaises(DatabaseUnavailable):
            ChannelSyncRepository(RecordingDatabase(error)).load_due_metadata_page(now, page=0, page_size=10)

    def test_running_status_channel_scope_does_not_apply_running_filter(self):
        db = RecordingDatabase([], [])
        repository = ChannelSyncRepository(db)
        repository.load_running_status_page(channel_id='c', page=1, page_size=10)
        repository.load_running_status_page(channel_id=None, page=1, page_size=10)
        self.assertEqual(db.calls[0][3], ('c', 10, 10))
        self.assertNotIn("= 'running'", db.calls[0][2])
        self.assertEqual(db.calls[1][2].count("= 'running'"), 4)

    def test_first_user_input_preserves_database_null_guard(self):
        db = RecordingDatabase(1)
        ChannelSyncRepository(db).preserve_user_input('c', '@valid_handle')
        self.assertIn('user_input IS NULL', db.calls[0][2])
        self.assertEqual(db.calls[0][3], ('https://www.youtube.com/@valid_handle', 'c'))

    def test_unowned_repair_is_retired_and_canonical_repair_routes_to_owned_transaction(self):
        db = RecordingDatabase()
        self.assertFalse(hasattr(InventoryMaintenanceRepository(db), 'repair_untyped_inventory'))
        result = {'status': 'completed'}
        with mock.patch('db.hot.hot_collection_control.dispatch_owned_rpc',
                        side_effect=lambda database, name, params, invoke: invoke()), \
                mock.patch('db.inventory.inventory_canonical_repair.repair_typed_inventory_canonical_types', return_value=result) as owner:
            self.assertIs(TypedInventoryRepository(db)._rpc('repair_typed_inventory_canonical_types',
                {'p_expected_count': 2, 'p_expected_fingerprint': 'native-fingerprint'}), result)
        owner.assert_called_once_with(db, expected_count=2, expected_fingerprint='native-fingerprint')
        self.assertEqual(db.calls, [])

    def test_unowned_upsert_is_retired_and_page_routes_to_owned_source_and_effects(self):
        db = RecordingDatabase()
        self.assertFalse(hasattr(InventoryMaintenanceRepository(db), 'upsert_inventory'))
        keys = ('channel_id', 'video_type', 'lease_token', 'scan_id', 'transition_id',
                'expected_page_token', 'next_page_token', 'page_status', 'items', 'source_checked_at')
        values = dict.fromkeys(keys)
        values.update(channel_id='c', video_type='short', page_status='ok', items=[])
        result = {'status': 'completed'}
        with mock.patch('db.hot.hot_collection_control.dispatch_owned_rpc',
                        side_effect=lambda database, name, params, invoke: invoke()), \
                mock.patch('db.inventory.inventory_scan_pages.settle_typed_inventory_page', return_value=result) as owner:
            self.assertIs(TypedInventoryRepository(db)._rpc('settle_typed_inventory_page',
                {'p_' + key: value for key, value in values.items()}), result)
        owner.assert_called_once_with(db, **values)
        self.assertEqual(db.calls, [])

    def test_maintenance_full_scan_uses_stable_pages_and_rejects_identifiers(self):
        db = RecordingDatabase([{'video_id': 'a'}, {'video_id': 'b'}], [{'video_id': 'c'}])
        result = InventoryMaintenanceRepository(db).fetch_all_rows('videos', 'video_id', 'checked_at', page_size=2)
        self.assertEqual(len(result), 3)
        self.assertEqual([call[3] for call in db.calls], [(2, 0), (2, 2)])
        self.assertIn('"checked_at" ASC NULLS LAST, "video_id" ASC', db.calls[0][2])
        with self.assertRaises(ValueError):
            InventoryMaintenanceRepository(db).load_channels_page('*', offset=0, page_size=2, order_column='title; DELETE')

    def test_unpaged_maintenance_channels_do_not_silently_truncate_at_1000(self):
        db = RecordingDatabase([{'channel_id': str(n)} for n in range(1001)])
        result = InventoryMaintenanceRepository(db).load_channels('channel_id')
        self.assertEqual(len(result), 1001)
        self.assertNotIn('LIMIT', db.calls[0][2])
        self.assertEqual(db.calls[0][4], 10000)

    def test_maintenance_batches_preserve_in_list_duplicate_semantics(self):
        db = RecordingDatabase([])
        InventoryMaintenanceRepository(db).load_videos_by_ids(['v'] * 1001, columns='video_id')
        self.assertEqual(len(db.calls), 1)
        self.assertEqual(db.calls[0][3], (['v'], 1))

    def test_collection_identity_and_scope_reads_keep_input_identity_checks(self):
        db = RecordingDatabase([{'video_id': 'v', 'channel_id': 'c'}], [
            {'channel_id': 'c', 'collection_control_epoch': 7, 'collection_permission': 'accepted', 'is_hidden': False},
        ])
        repository = VideoCollectionRepository(db)
        self.assertEqual(repository.load_video_channel_map(['v']), {'v': 'c'})
        self.assertEqual(repository.load_channel_scopes(['c']), [{'channel_id': 'c', 'control_epoch': 7}])
        invalid = RecordingDatabase([{'video_id': 'not_requested', 'channel_id': 'c'}])
        with self.assertRaisesRegex(ValueError, 'channel_collection_scope_invalid'):
            VideoCollectionRepository(invalid).load_video_channel_map(['v'])

    def test_collection_counter_presence_chunks_remain_bounded(self):
        db = RecordingDatabase([], [], [{'video_id': '1000', 'like_count': 0, 'comment_count': None}])
        result = VideoCollectionRepository(db).load_counter_presence([str(n) for n in range(1001)])
        self.assertEqual(result['1000']['like_count'], 0)
        self.assertEqual([call[4] for call in db.calls], [500, 500, 1])

    def test_collection_preflights_preserve_plain_results_and_direct_empty_dispatch(self):
        common = {
            'status': 'ok', 'schema_version': 1, 'common_pool_version': 1,
            'max_claim_rows': 50, 'max_request_batch': 50, 'max_inspected_rows': 1000,
            'mixed_claim': True, 'sole_lease_owner': 'video_collection_state',
            'availability_incident_owner': True, 'atomic_long_availability_restore': True,
        }
        typed = {
            'status': 'ok', 'schema_version': 1, 'runtime_video_types': ['short', 'stream'],
            'max_request_batch': 50, 'full_metadata_each_refresh': True,
            'completed_streams_only': True, 'product_exposure_changed': False,
            'exact_receipt_payload_fencing': True, 'repair_version': 2, 'bounded_claim_batch': True,
        }
        quota = {
            'status': 'ok', 'schema_version': 1, 'claim_limit': 500,
            'retry_floor_seconds': 3600, 'typed_jitter_seconds': {'short': 21600, 'stream': 21600},
            'schedule_source': 'process_snapshot',
            'lost_ack_receipts': True, 'active_scan_state_preserved': True,
            'quota_claim_floor_guard': True, 'retry_until': 'youtube_quota_reset',
        }
        queue = {'metadata_completion_contract_version': 2}
        db = RecordingDatabase([])
        repository = VideoCollectionRepository(db)
        with mock.patch('db.videos.video_collection.capabilities.telemetry_check', side_effect=[common, typed, quota, queue]) as check:
            self.assertEqual(repository.preflight_common_pool_ready(), common)
            self.assertEqual(repository.preflight_typed_collection_ready(), {
                **typed, 'typed_inventory_quota_defer': quota,
            })
            self.assertEqual(repository.preflight_queue_ready(), queue)
        self.assertEqual([call.args[1].__name__ for call in check.call_args_list], [
            'assert_video_collection_common_pool_schema', 'assert_typed_video_collection_schema',
            'assert_typed_inventory_quota_defer_schema', 'assert_video_collection_queue_ready',
        ])
        self.assertTrue(all(call.args[0] is db and call.kwargs == {'phase': 'preflight'} for call in check.call_args_list))
        with mock.patch('db.readiness.collection_preflight.assert_forced_metadata_claim_ready', return_value=[]) as ready:
            self.assertEqual(repository.assert_forced_metadata_claim_ready(worker_id='worker', run_id='run'), [])
        ready.assert_called_once_with(db, worker_id='worker', run_id='run', lease_seconds=300)
        self.assertEqual(db.calls, [])

    @staticmethod
    def claim():
        return TypedInventoryClaim('c', 'long', 'playlist', 'lease', 1, '2026-10-06T00:00:00Z', 0,
                                   None, None, 'discovery')

    def test_typed_membership_boundary_retains_same_scan_and_clock_semantics(self):
        db = RecordingDatabase([
            {'video_id': 'old', 'first_seen_at': '2026-10-04T00:00:00Z', 'last_seen_scan_id': 'scan'},
            {'video_id': 'new', 'first_seen_at': '2026-10-06T00:00:00Z', 'last_seen_scan_id': 'scan'},
            {'video_id': 'other', 'first_seen_at': None, 'last_seen_scan_id': 'other_scan'},
        ])
        result = TypedInventoryRepository(db).known_membership_ids(
            self.claim(), ['old', 'new', 'other'], scan_id='scan',
            scan_boundary_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(result, {'old', 'other'})
        self.assertEqual(db.calls[0][3], ('c', 'long', ['old', 'new', 'other'], 3))

    def test_typed_token_cycle_only_maps_confirmed_server_errors(self):
        error = DatabaseServerError('inventory_token_cycle', code='P0001')
        db = RecordingDatabase()
        with mock.patch('db.inventory.inventory_scan_pages.settle_typed_inventory_page', side_effect=error) as action:
            with self.assertRaises(InventoryTokenCycleError):
                TypedInventoryRepository(db).settle_page(
                    self.claim(), scan_id='scan', transition_id='transition', expected_page_token=None,
                    next_page_token='next', page_status='page', items=[],
                    source_checked_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
                )
            self.assertEqual(action.call_count, 2)
            self.assertEqual(action.call_args_list[0], action.call_args_list[1])
        self.assertEqual(db.calls, [])
        self.assertEqual([d[2] for d in self.dispatches], [True, False])
        unknown = DatabaseUnavailable('inventory_token_cycle', code='P0001', outcome_unknown=True)
        with mock.patch('db.inventory.inventory_scan_pages.settle_typed_inventory_page', side_effect=unknown) as action:
            with self.assertRaises(DatabaseUnavailable):
                TypedInventoryRepository(db).settle_page(
                    self.claim(), scan_id='scan', transition_id='transition', expected_page_token=None,
                    next_page_token='next', page_status='page', items=[],
                    source_checked_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
                )
            self.assertEqual(action.call_count, 2)
        self.assertEqual(db.calls, [])

    def test_operator_claim_and_read_return_plain_json_with_no_implicit_retry(self):
        db = RecordingDatabase()
        repository = OperatorJobRepository(db)
        with mock.patch('db.jobs.operator_job_read_queries.get_operator_job_status', return_value=None) as latest:
            self.assertIsNone(repository.get_channel_job('c'))
            latest.assert_called_once_with(db, 'c')
        with mock.patch('db.jobs.operator_job_read_queries.list_active_operator_jobs', return_value=[{'id': 'job'}]) as active:
            self.assertEqual(repository.list_active(limit=10), [{'id': 'job'}])
            active.assert_called_once_with(db, 10)
        error = DatabaseUnavailable('database_connection_failed', outcome_unknown=True)
        with mock.patch('db.jobs.operator_job_lifecycle.claim_operator_job', side_effect=error) as action:
            with self.assertRaises(DatabaseUnavailable) as caught:
                repository.claim('worker')
            self.assertIs(caught.exception, error)
            action.assert_called_once_with(db, worker_id='worker', lease_seconds=1200)
        self.assertEqual(db.calls, [])  # all three named operations now use Python helpers

    def test_owned_modules_have_no_http_query_builder_or_postgrest_import(self):
        for name in MODULES:
            source = (ROOT / 'application/db' / (name + '.py')).read_text(encoding='utf-8')
            tree = ast.parse(source)
            methods = [node.func.attr for node in ast.walk(tree)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
            self.assertFalse({'table', 'rpc', 'execute', 'range', 'eq', 'in_', 'is_'} & set(methods), name)
            self.assertNotIn('postgrest', source.lower(), name)


if __name__ == '__main__':
    unittest.main()
