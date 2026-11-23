"""Operational SQL boundaries: local recording fakes, no server or provider."""
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from collection_schedule import CollectionSchedules
from db.reads import operational_read_queries as queries
from db.channel.channel_discovery import ChannelDiscoveryRepository
from db.channel.channel_sync import ChannelSyncRepository
from db.base.postgres import DatabaseError
from db.reads.system_health import SystemHealthRepository
from db.videos.video_collection import VideoCollectionRepository
from db.quota.youtube_quota import YouTubeQuotaRepository

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)


class DirectDatabase:
    def __init__(self, *results):
        self.results, self.calls = deque(results), []
        self.cadence_snapshot=CollectionSchedules().cadence_snapshot()

    def _read(self, operation, statement, params, maximum):
        self.calls.append((operation, statement, dict(params) if isinstance(params, Mapping) else tuple(params), maximum))
        value = self.results.popleft()
        if isinstance(value, Exception):
            raise value
        if isinstance(value, list) and len(value) > maximum:
            raise DatabaseError('database_result_limit_exceeded')
        return value

    def read_row(self, operation, statement, params=()):
        return self._read(operation, statement, params, 1)

    def read_rows(self, operation, statement, params=(), *, max_rows):
        return self._read(operation, statement, params, max_rows)


def empty_summary():
    return dict(schema_version=1, attempts=0, quota_units=0, truncated=False,
        bucket_attempt_counts={}, bucket_units={}, phase_attempt_counts={}, phase_units={},
        endpoint_attempt_counts={}, status_counts={}, http_status_counts={}, error_type_counts={},
        duration_ms=dict(count=0, min=None, avg=None, median=None, max=None),
        unfinished=dict(count=0, stale_count=0, oldest_age_minutes=None, stale_after_minutes=10))


class QuotaReadTests(unittest.TestCase):
    def test_omitted_clock_defaults_to_sql_now_but_explicit_clock_remains_bound(self):
        db = DirectDatabase({'value': empty_summary()}, {'value': empty_summary()})
        repository = YouTubeQuotaRepository(db, project_id="probe'project")
        self.assertEqual(repository.summarize_day('2026-10-05')['duration_ms']['median'], None)
        repository.summarize_day('2026-10-05', now=NOW, max_rows=2, stale_attempt_minutes=1.25)
        self.assertTrue(db.calls[0][2]['use_default_now'])
        self.assertIsNone(db.calls[0][2]['p_now'])
        self.assertFalse(db.calls[1][2]['use_default_now'])
        self.assertEqual(db.calls[1][2]['p_now'], NOW.isoformat())
        self.assertEqual(db.calls[1][2]['p_max_rows'], 2)
        self.assertNotIn("probe'project", db.calls[0][1])
        self.assertIn('THEN now() ELSE %(p_now)s::timestamptz END', db.calls[0][1])
        self.assertEqual([c[3] for c in db.calls], [1, 1])

    def test_retained_explicit_null_and_invalid_arguments_fail_without_dispatch(self):
        original = {'p_project_id': 'primary', 'p_quota_day': '2026-10-05'}
        for fields in ({'p_now': None}, {'p_project_id': ' '}, {'p_quota_day': None},
                       {'p_max_rows': None}, {'p_max_rows': 0}, {'p_max_rows': 100001},
                       {'p_stale_attempt_minutes': None}, {'p_stale_attempt_minutes': -1},
                       {'p_stale_attempt_minutes': Decimal('NaN')}):
            db = DirectDatabase()
            with self.subTest(fields=fields), self.assertRaises(DatabaseError) as caught:
                queries.read_youtube_quota_day(db, {**original, **fields})
            self.assertEqual(caught.exception.code, 'P0001')
            self.assertEqual(db.calls, [])
        for params in ({}, {**original, 'unknown': 'private'}):
            with self.assertRaises(ValueError):
                queries.read_youtube_quota_day(DirectDatabase(), params)

    def test_repository_retains_finite_validation_and_empty_envelope(self):
        db = DirectDatabase({'value': empty_summary()})
        repository = YouTubeQuotaRepository(db)
        for threshold in (float('inf'), float('-inf'), float('nan'), -1):
            with self.assertRaises(ValueError):
                repository.summarize_day('2026-10-05', stale_attempt_minutes=threshold)
        result = repository.summarize_day('2026-10-05')
        self.assertFalse(result['truncated'])
        self.assertEqual(result['attempts'], 0)
        self.assertEqual(result['bucket_units'], {})
        self.assertEqual(result['unfinished']['oldest_age_minutes'], None)
        self.assertEqual(len(db.calls), 1)

    def test_query_preserves_continuous_median_rounding_and_exact_cap(self):
        statement = queries.QUOTA_DAY_SQL
        self.assertIn('percentile_cont(0.5) within group(order by duration_ms)', statement)
        self.assertIn('round(greatest(extract(epoch', statement)
        self.assertIn('/60,3)', statement)
        self.assertIn('count(*)>%(p_max_rows)s::integer', statement)
        self.assertIn('limit %(p_max_rows)s::integer+1', statement)
        self.assertNotIn('public.read_youtube_quota_day(', statement)
        for key in empty_summary():
            self.assertIn("'" + key + "'", statement)

    def test_scalar_alias_and_summary_validation_do_not_hide_bad_results(self):
        for row in ({'wrong': {}}, {'value': {}}, None):
            with self.subTest(row=row), self.assertRaises((DatabaseError, ValueError)):
                YouTubeQuotaRepository(DirectDatabase(row)).summarize_day('2026-10-05')


class HealthReadTests(unittest.TestCase):
    def test_row_healths_use_one_bounded_select_and_preserve_bigints_and_nulls(self):
        row = {'total': 2**54, 'oldest_overdue_at': None}
        for repository in (ChannelDiscoveryRepository, VideoCollectionRepository):
            db = DirectDatabase([row])
            with patch('db.channel.channel_discovery.emit_event'), patch('db.videos.video_collection.emit_event'):
                self.assertEqual(repository(db).health(), row)
            self.assertEqual(len(db.calls), 1)
            self.assertEqual(db.calls[0][3], 1)
            self.assertIn('FROM readable.', db.calls[0][1])
            self.assertNotIn('get_channel_discovery_queue_health(', db.calls[0][1])
            self.assertNotIn('get_video_collection_health(', db.calls[0][1])

    def test_json_health_envelopes_and_old_telemetry_names_survive(self):
        typed = {'status': 'ok', 'schema_version': 1, 'video_types': {}}
        sync = {'channel_mode_counts': {}, 'collection_rows_policy_drift': 0}
        db = DirectDatabase({'value': typed}, {'value': sync})
        with patch('db.videos.video_collection.emit_event') as emit:
            self.assertEqual(VideoCollectionRepository(db).typed_health(), typed)
        self.assertTrue(all(call.args[3] == 'rpc:get_typed_video_collection_health' for call in emit.call_args_list))
        self.assertEqual(ChannelSyncRepository(db).health(), sync)
        self.assertTrue(all(c[3] == 1 for c in db.calls))

    def test_coverage_has_schema_cap_and_does_not_truncate_overflow(self):
        rows = [{'channel_id': str(i)} for i in range(10000)]
        db = DirectDatabase(rows, rows + [{'channel_id': 'overflow'}])
        repository = SystemHealthRepository(db)
        self.assertEqual(repository.fetch_rpc_rows('get_channel_coverage_summary'), rows)
        self.assertEqual(db.calls[0][3], 10000)
        with self.assertRaises(DatabaseError):
            repository.fetch_rpc_rows('get_channel_coverage_summary')
        self.assertIn('readable.video_inventory', db.calls[0][1])
        self.assertNotIn('public.video_inventory', db.calls[0][1])
        self.assertNotIn('public.hot_video_inventory', db.calls[0][1])
        self.assertIn('base.deep_bypass asc, greatest', db.calls[0][1])

    def test_health_compatibility_dispatch_rejects_arguments_or_unknown_read(self):
        db = DirectDatabase([])
        repository = SystemHealthRepository(db)
        for name, params in (('unknown', None), ('get_video_collection_health', {'p_now': 'private'}),
                             ('get_video_collection_health', [])):
            with self.assertRaises(ValueError):
                repository.fetch_rpc_rows(name, params)
        self.assertEqual(db.calls, [])
        self.assertEqual(repository.fetch_rpc_rows('get_video_collection_health', {}), [])

    def test_bad_health_envelopes_keep_existing_repository_failures(self):
        for repository, value in ((ChannelDiscoveryRepository, []), (VideoCollectionRepository, [])):
            with self.subTest(repository=repository), self.assertRaises(TypeError):
                repository(DirectDatabase(value)).health()
        with self.assertRaises(TypeError):
            ChannelSyncRepository(DirectDatabase({'value': []})).health()
        with self.assertRaises(TypeError):
            VideoCollectionRepository(DirectDatabase({'value': []})).typed_health()

    def test_policy_facts_replace_helpers_and_typed_exposure_flags_unchanged(self):
        schema=(ROOT/'schema/schema.sql').read_text(encoding='utf-8')
        def view(name):
            return schema.split('CREATE VIEW readable.'+name+' ',1)[1].split(';',1)[0]
        discovery=view('channel_discovery_health')
        sync=view('channel_sync_health')
        typed=view('typed_video_collection_health')
        videos=view('video_collection_health')
        self.assertNotIn('public.channel_sync_mode(', queries.CHANNEL_DISCOVERY_SQL)
        self.assertIn('channel_policy_facts', discovery)
        self.assertIn('lease_channel_epoch IS DISTINCT FROM c.control_epoch', discovery)
        self.assertNotIn('public.typed_inventory_channel_eligible(', queries.CHANNEL_SYNC_SQL)
        self.assertIn('channel.typed_runtime_eligible', sync)
        self.assertNotIn('public.channel_inventory_estimate_count(', queries.CHANNEL_COVERAGE_SQL)
        self.assertEqual(queries.CHANNEL_COVERAGE_SQL.count('statement_timestamp()'), 3)
        self.assertIn('left join effective_state', typed)
        self.assertIn('FROM public.video_collection_state state', typed)
        self.assertIn('state.channel_eligible', typed)
        self.assertIn("'channel_eligibility_checked', true", typed)
        for flag in ("'repair_version', 2", "'operational_eligibility_checked', false", "'product_exposure_changed', false"):
            self.assertIn(flag, typed)
        for column in ('total','due','leased','expired_leases','paused','hydration_due','metadata_due','counter_due','failed','oldest_overdue_at'):
            self.assertIn('AS ' + column, videos)


class AvailabilityReadTests(unittest.TestCase):
    def test_bound_status_order_interval_and_telemetry_use_one_read(self):
        from db.videos.availability import AvailabilityRepository, AVAILABILITY_RECHECK_SQL
        db = DirectDatabase([{'video_id':'second'},{'video_id':'first'}])
        with patch('db.videos.availability.emit_event') as emit:
            result = AvailabilityRepository(db).load_recheck_candidate_ids(" channel' ", ['private','deleted','private'],
                limit=2, recheck_after=timedelta(hours=2,microseconds=500000))
        self.assertEqual(result, ['second','first'])
        self.assertEqual(db.calls[0][2], {'p_channel_id':"channel'", 'p_statuses':['private','deleted'],
                                       'p_limit':2,'p_recheck_after':'7200 seconds','p_max_recheck_seconds':2419200})
        self.assertEqual(db.calls[0][3], 2)
        self.assertNotIn("channel'", db.calls[0][1])
        self.assertEqual([call.kwargs['status'] for call in emit.call_args_list], ['started','ok'])
        self.assertTrue(all(call.args[3] == 'get_long_availability_recheck_candidate_ids' for call in emit.call_args_list))
        self.assertEqual(AVAILABILITY_RECHECK_SQL.count('clock_timestamp()'), 2)
        self.assertIn('unavailable.last_seen_at ASC NULLS LAST', AVAILABILITY_RECHECK_SQL)
        self.assertIn('power(2, least(greatest(coalesce(unavailable.attempt_count, 1) - 1, 0), 5))', AVAILABILITY_RECHECK_SQL)

    def test_invalid_lengths_and_noop_inputs_never_dispatch(self):
        from db.videos.availability import AvailabilityRepository
        db = DirectDatabase()
        repository = AvailabilityRepository(db)
        for channel,statuses in (('x'*129,['private']), ('channel',['x'*121])):
            with self.assertRaises(DatabaseError) as caught:
                repository.load_recheck_candidate_ids(channel,statuses,limit=1)
            self.assertEqual(caught.exception.code, 'P0001')
        self.assertEqual(repository.load_recheck_candidate_ids('channel',[],limit=5), [])
        self.assertEqual(repository.load_recheck_candidate_ids('channel',['private'],limit=0), [])
        self.assertEqual(db.calls, [])

    def test_read_error_emits_old_error_event_without_retry(self):
        from db.videos.availability import AvailabilityRepository
        db = DirectDatabase(DatabaseError('database_operation_failed'))
        with patch('db.videos.availability.emit_event') as emit, self.assertRaises(DatabaseError):
            AvailabilityRepository(db).load_recheck_candidate_ids('channel',['private'],limit=1)
        self.assertEqual(len(db.calls),1)
        self.assertEqual([call.kwargs['status'] for call in emit.call_args_list], ['started','error'])


if __name__ == '__main__':
    unittest.main()
