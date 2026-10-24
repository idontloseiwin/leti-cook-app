"""Focused preparation tests; recording reads only, never connects to PostgreSQL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'application'))
from db.reads import analytics_read_queries as queries
from db.base.postgres import DatabaseError, _reviewed_statement

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
VIDEO = 'aaaaaaaaaaa'
OTHER = 'bbbbbbbbbbb'
DEADLINE = (datetime.now(timezone.utc)+timedelta(days=1)).isoformat().replace('+00:00','Z')


class RecordingDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    @contextmanager
    def transaction(self, operation):
        assert operation.name == 'analytics_video_growth' and operation.read_only
        database = self
        class ReadTransaction:
            def fetch_one(self, statement):
                assert statement == "SELECT set_config('jit','off',true) AS value"
                return {'value': 'off'}
            def fetch_all(self, statement, params, *, max_rows):
                return database.read_rows(operation.name, statement, params, max_rows=max_rows)
        yield ReadTransaction()

    def read_rows(self, operation, statement, params, *, max_rows):
        # Exercise the real local statement guard, without opening a database.
        _reviewed_statement(statement, read=True, max_rows=max_rows)
        self.calls.append((operation, statement, deepcopy(params), max_rows))
        rows = deepcopy(self.rows)
        if operation in {'analytics_channel_timeline', 'analytics_video_growth', 'analytics_saved_sources'}:
            for row in rows:
                row.setdefault('source_expires_at', DEADLINE)
                if operation == 'analytics_channel_timeline':
                    row.setdefault('response_source_expires_at', DEADLINE)
        return rows


def timeline_row(**patches):
    return dict(bucket_at=NOW.isoformat(), recorded_at=NOW.isoformat(), views=90,
                subscribers=None, correction_within_bucket=True,
                scan_truncated=False, snapshot_count=2, source_expires_at=DEADLINE, **patches)


def growth_row(video=VIDEO):
    return dict(video_id=video, growth_status='partial', delta_views=-10,
                tracked_days=1, observed_start_at=(NOW-timedelta(hours=25)).isoformat(),
                observed_end_at=NOW.isoformat(), observed_current_views=90,
                actual_interval_seconds=90000, boundary_distance_seconds=None,
                reason='full_window_history_unavailable', source_expires_at=DEADLINE)


def source_row(video=VIDEO):
    return dict(video_id=video, channel_id=None, source_status='unavailable',
                source_reason='inventory_missing', video_row=None, source_expires_at=DEADLINE)


class TimelineTests(unittest.TestCase):
    def read(self, database, **patches):
        values = dict(channel_id='channel', metric='views', recorded_from=None,
                      recorded_through=NOW, bucket_seconds=300)
        values.update(patches)
        return queries.read_channel_timeline_series(database, **values)

    def test_defaults_keep_one_statement_and_501_row_sentinel_cap(self):
        database = RecordingDatabase([timeline_row()])
        result = self.read(database)
        self.assertEqual(result, [timeline_row()])
        self.assertEqual(len(database.calls), 1)
        params, maximum = database.calls[0][2:]
        self.assertEqual((params['scan_limit'], params['return_limit'], maximum), (20000, 500, 501))
        self.assertIsNone(params['recorded_from'])
        self.assertEqual(params['recorded_through'], '2026-10-05T12:00:00Z')

    def test_null_limits_differ_from_omission_and_bucket_has_own_default(self):
        database = RecordingDatabase([])
        self.read(database, scan_limit=None, return_limit=None, bucket_seconds=None)
        params, maximum = database.calls[0][2:]
        self.assertEqual((params['scan_limit'], params['return_limit'], params['bucket_seconds'], maximum), (1, 1, 3600, 2))

    def test_integer_limits_clamp_without_changing_types(self):
        for inputs, expected in [((-20, 900, 1), (1, 500, 300, 501)),
                                 ((30000, 0, 99999999), (20000, 1, 31536000, 2))]:
            database = RecordingDatabase([])
            self.read(database, scan_limit=inputs[0], return_limit=inputs[1], bucket_seconds=inputs[2])
            params, maximum = database.calls[0][2:]
            self.assertEqual((params['scan_limit'], params['return_limit'], params['bucket_seconds'], maximum), expected)

    def test_metric_and_nonfinite_clocks_reject_before_dispatch(self):
        for change in ({'metric': None}, {'metric': 'likes'}, {'recorded_through': None},
                       {'recorded_through': 'infinity'}, {'recorded_from': '-infinity'}):
            database = RecordingDatabase([])
            with self.subTest(change=change), self.assertRaises(DatabaseError) as error:
                self.read(database, **change)
            self.assertEqual(error.exception.code, 'P0001')
            self.assertEqual(database.calls, [])

    def test_future_expired_reversed_bounds_are_evaluated_with_database_clock(self):
        for lower, upper in ((NOW+timedelta(days=2), NOW+timedelta(days=1)),
                             (NOW-timedelta(days=40), NOW-timedelta(days=30))):
            database = RecordingDatabase([])
            self.assertEqual(self.read(database, recorded_from=lower, recorded_through=upper), [])
            self.assertEqual(len(database.calls), 1)
            self.assertEqual(database.calls[0][2]['recorded_from'], lower.isoformat().replace('+00:00','Z'))

    def test_nullable_channel_is_not_rejected_or_normalized(self):
        for channel in (None, '', '  channel  ', "x'; SELECT 1 --"):
            database = RecordingDatabase([])
            self.read(database, channel_id=channel)
            self.assertEqual(database.calls[0][2]['channel_id'], channel)
            if channel:
                self.assertNotIn(channel, database.calls[0][1])

    def test_sentinel_and_negative_correction_survive_unchanged(self):
        rows = [timeline_row(), timeline_row()]
        database = RecordingDatabase(rows)
        self.assertEqual(self.read(database, return_limit=1), rows)
        self.assertTrue(database.calls[0][3] == 2)
        database = RecordingDatabase(rows + [timeline_row()])
        with self.assertRaisesRegex(DatabaseError, 'database_result_limit_exceeded'):
            self.read(database, return_limit=1)

    def test_alias_and_aggregate_shape_fail_closed(self):
        for row in (dict(timeline_row(), truncated=False),
                    dict(timeline_row(), snapshot_count=20001),
                    dict(timeline_row(), scan_truncated=1)):
            with self.subTest(row=row), self.assertRaises(DatabaseError):
                self.read(RecordingDatabase([row]))

    def test_integer_input_overflow_has_safe_sqlstate(self):
        with self.assertRaises(DatabaseError) as error:
            self.read(RecordingDatabase([]), scan_limit=2**31)
        self.assertEqual(error.exception.code, '22003')
        with self.assertRaises(TypeError):
            self.read(RecordingDatabase([]), return_limit=True)


class GrowthTests(unittest.TestCase):
    def read(self, database, **patches):
        values = dict(channel_id='channel', video_ids=[VIDEO], as_of=NOW)
        values.update(patches)
        return queries.read_video_view_growth_28d(database, **values)

    def test_one_query_keeps_signed_delta_and_exact_requested_order(self):
        rows = [growth_row(OTHER), growth_row(VIDEO)]
        database = RecordingDatabase(rows)
        self.assertEqual(self.read(database, video_ids=[OTHER, ' '+VIDEO+' ', OTHER]), rows)
        self.assertEqual(len(database.calls), 1)
        self.assertEqual(database.calls[0][2]['video_ids'], [OTHER, ' '+VIDEO+' ', OTHER])
        self.assertEqual(database.calls[0][3], 2)
        self.assertEqual(rows[1]['delta_views'], -10)

    def test_raw_cardinality_is_checked_before_deduplication(self):
        database = RecordingDatabase([growth_row()])
        with self.assertRaises(DatabaseError) as error:
            self.read(database, video_ids=[VIDEO]*5001)
        self.assertEqual(error.exception.code, 'P0001')
        self.assertEqual(database.calls, [])
        self.read(database, video_ids=[VIDEO]*5000)
        self.assertEqual(database.calls[0][3], 1)

    def test_null_and_empty_arrays_do_not_read_but_validate_clock_first(self):
        for ids in (None, []):
            database = RecordingDatabase([])
            self.assertEqual(self.read(database, video_ids=ids), [])
            self.assertEqual(database.calls, [])
            with self.assertRaises(DatabaseError):
                self.read(database, video_ids=ids, as_of=None)

    def test_blank_ids_fail_but_nonblank_non_youtube_ids_are_bound(self):
        for value in (None, '', '   '):
            with self.assertRaises(DatabaseError):
                self.read(RecordingDatabase([]), video_ids=[value])
        for value in ('\t', "x' OR true --"):
            database = RecordingDatabase([growth_row(value)])
            self.read(database, video_ids=[value])
            self.assertEqual(database.calls[0][2]['video_ids'], [value])
            self.assertNotIn(value, database.calls[0][1])

    def test_channel_space_validation_does_not_trim_comparison_value(self):
        for value in (None, '   '):
            with self.assertRaises(DatabaseError):
                self.read(RecordingDatabase([]), channel_id=value)
        database = RecordingDatabase([growth_row()])
        self.read(database, channel_id=' channel ')
        self.assertEqual(database.calls[0][2]['channel_id'], ' channel ')

    def test_missing_duplicate_or_unknown_identity_rejects_entire_result(self):
        for rows in ([], [growth_row(), growth_row()],
                     [growth_row(), growth_row('unknown')]):
            with self.subTest(rows=rows), self.assertRaises(DatabaseError):
                self.read(RecordingDatabase(rows), video_ids=[VIDEO, OTHER])

    def test_complete_results_are_normalized_to_requested_order(self):
        rows = self.read(RecordingDatabase([growth_row(OTHER), growth_row()]), video_ids=[VIDEO, OTHER])
        self.assertEqual([row['video_id'] for row in rows], [VIDEO, OTHER])

    def test_unavailable_keeps_current_fields_when_sql_has_fresh_current_data(self):
        row = dict(growth_row(), growth_status='unavailable', delta_views=None,
                   tracked_days=None, observed_start_at=None, actual_interval_seconds=None,
                   reason='current_snapshot_missing')
        self.assertEqual(self.read(RecordingDatabase([row])), [row])

    def test_future_as_of_and_lower_are_bound_without_python_clock_clamp(self):
        database = RecordingDatabase([growth_row()])
        future = NOW + timedelta(days=1)
        self.read(database, as_of=future, recorded_from=future)
        self.assertEqual(database.calls[0][2]['as_of'], future.isoformat().replace('+00:00','Z'))
        self.assertEqual(database.calls[0][2]['recorded_from'], database.calls[0][2]['as_of'])


class SavedSourceTests(unittest.TestCase):
    def read(self, database, **patches):
        values = dict(video_ids=[VIDEO], as_of=NOW)
        values.update(patches)
        return queries.read_saved_collection_sources(database, **values)

    def test_exact_input_identity_and_unavailable_null_shape(self):
        rows = [source_row(OTHER), source_row(VIDEO)]
        database = RecordingDatabase(rows)
        self.assertEqual(self.read(database, video_ids=[OTHER,VIDEO]), rows)
        self.assertEqual(len(database.calls), 1)
        self.assertEqual(database.calls[0][3], 2)
        self.assertEqual(database.calls[0][2]['video_ids'], [OTHER,VIDEO])

    def test_invalid_unique_ascii_array_and_clock_use_22023_without_dispatch(self):
        for change in ({'video_ids':None}, {'video_ids':[]}, {'video_ids':[VIDEO]*501},
                       {'video_ids':[VIDEO,VIDEO]}, {'video_ids':[' '+VIDEO]},
                       {'video_ids':['é'*11]}, {'video_ids':[None]}, {'as_of':None},
                       {'as_of':'infinity'}):
            database = RecordingDatabase([])
            with self.subTest(change=change), self.assertRaises(DatabaseError) as error:
                self.read(database, **change)
            self.assertEqual(error.exception.code, '22023')
            self.assertEqual(database.calls, [])

    def test_available_nested_json_keeps_nullable_and_negative_counters(self):
        row = dict(source_row(), channel_id='channel', source_status='available', source_reason=None,
                   video_row={'video_id':VIDEO, 'views':-2, 'like_count':None,
                              'source_expires_at':NOW.isoformat()})
        self.assertEqual(self.read(RecordingDatabase([row])), [row])

    def test_missing_row_and_nonobject_video_json_fail_closed(self):
        for rows in ([], [dict(source_row(), video_row=[])], [source_row(OTHER)]):
            with self.assertRaises(DatabaseError):
                self.read(RecordingDatabase(rows))


class StaticQueryBoundaryTests(unittest.TestCase):
    def test_each_query_has_one_captured_clock_and_no_retained_function_call(self):
        for statement, clock in ((queries.TIMELINE_SQL,'statement_timestamp'),
                                 (queries.GROWTH_SQL,'statement_timestamp'),
                                 (queries.SAVED_SOURCES_SQL,'clock_timestamp')):
            self.assertEqual(statement.count(clock+'()'), 1)
            self.assertIn('read_clock AS MATERIALIZED', statement)
            self.assertNotIn(';', statement)
            self.assertNotIn('public.read_', statement)
            self.assertNotRegex(statement, r'\bp_(channel|video|as_of|metric|recorded)')
            _reviewed_statement(statement, read=True, max_rows=501)


if __name__ == '__main__':
    unittest.main()
