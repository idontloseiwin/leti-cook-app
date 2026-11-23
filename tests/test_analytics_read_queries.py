"""Saved collection member read: focused preparation tests; recording reads only, never connects to PostgreSQL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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

    def read_rows(self, operation, statement, params, *, max_rows):
        # Exercise the real local statement guard, without opening a database.
        _reviewed_statement(statement, read=True, max_rows=max_rows)
        self.calls.append((operation, statement, deepcopy(params), max_rows))
        rows = deepcopy(self.rows)
        if operation == 'analytics_saved_sources':
            for row in rows:
                row.setdefault('source_expires_at', DEADLINE)
        return rows


def source_row(video=VIDEO):
    return dict(video_id=video, channel_id=None, source_status='unavailable',
                source_reason='inventory_missing', video_row=None, source_expires_at=DEADLINE)


COLLECTION = 'c0000000-0000-4000-8000-000000000001'


class SavedSourceTests(unittest.TestCase):
    def read(self, database, **patches):
        values = dict(collection_id=COLLECTION, video_ids=[VIDEO], as_of=NOW)
        values.update(patches)
        return queries.read_saved_collection_sources(database, **values)

    def test_exact_input_identity_and_unavailable_null_shape(self):
        rows = [source_row(OTHER), source_row(VIDEO)]
        database = RecordingDatabase(rows)
        self.assertEqual(self.read(database, video_ids=[OTHER,VIDEO]), rows)
        self.assertEqual(len(database.calls), 1)
        self.assertEqual(database.calls[0][3], 2)
        self.assertEqual(database.calls[0][2]['video_ids'], [OTHER,VIDEO])
        self.assertEqual(database.calls[0][2]['scope_id'], COLLECTION)

    def test_invalid_unique_ascii_array_and_clock_use_22023_without_dispatch(self):
        for change in ({'video_ids':None}, {'video_ids':[]}, {'video_ids':[VIDEO]*501},
                       {'video_ids':[VIDEO,VIDEO]}, {'video_ids':[' '+VIDEO]},
                       {'video_ids':['é'*11]}, {'video_ids':[None]}, {'as_of':None},
                       {'as_of':'infinity'}, {'collection_id':None}, {'collection_id':COLLECTION.upper()},
                       {'collection_id':'c0000000-0000-1000-8000-000000000001'}):
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
        available = dict(source_row(), source_status='available', source_reason=None, channel_id='channel')
        for rows in ([], [dict(source_row(), video_row=[])], [source_row(OTHER)], [dict(source_row(), source_reason='type_unsupported')],
                     [dict(source_row(), source_reason=None)], [available], [dict(available, video_row={}, source_reason='source_stale')],
                     [dict(source_row(), source_status='hidden')]):
            with self.assertRaises(DatabaseError):
                self.read(RecordingDatabase(rows))


class StaticQueryBoundaryTests(unittest.TestCase):
    def test_each_query_has_one_captured_clock_and_no_retained_function_call(self):
        from db.reads.research_sql import compile_collection_members
        members, _ = compile_collection_members(COLLECTION, [VIDEO], NOW.isoformat())
        self.assertEqual(members.count('statement_timestamp()'), 1)
        self.assertNotIn(';', members)
        _reviewed_statement(members, read=True, max_rows=500)
        # The old growth and Timeline reads left; only this one remains.
        self.assertEqual([name for name in dir(queries) if name.endswith('_SQL')], [])


if __name__ == '__main__':
    unittest.main()
