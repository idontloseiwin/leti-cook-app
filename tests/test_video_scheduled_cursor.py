"""Fixed scheduled candidate cursor guards; no database/network is used."""
from collections import deque
from pathlib import Path
import sys,unittest
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.base.postgres import Transaction,DatabaseError,DatabaseServerError
from test_direct_pg_transactions import Connection,ServerFailure

class Cursor:
    def __init__(self,rows):self.rows=deque(rows);self.sizes=[];self.calls=[];self.closed=False;self.error=None
    def __enter__(self):return self
    def __exit__(self,*error):self.closed=True
    def execute(self,sql,params):self.calls.append((sql,params))
    def fetchmany(self,size):
        self.sizes.append(size)
        if self.error:raise self.error
        return [self.rows.popleft() for _ in range(min(size,len(self.rows)))]

class ScheduledCursorTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'video_id':str(i),'channel_id':'channel','video_type':'long'} for i in range(80)]
        self.cursor=Cursor(self.rows);self.connection=Connection([])
        self.connection.cursor=Mock(return_value=self.cursor);self.gate=Mock()
        self.tx=Transaction(self.connection,self.gate,total_timeout_ms=8000)
    def stream(self,**changes):return self.tx.video_scheduled_candidates(changes.pop('ids',[row['video_id'] for row in self.rows]),'00000000-0000-4000-8000-000000000001',**changes)
    def test_initial_ten_then_fifty_and_early_close(self):
        with self.stream() as rows:
            self.assertEqual([next(rows)['video_id'] for _ in range(11)],[str(i) for i in range(11)])
        self.assertEqual(self.cursor.sizes,[10,50]);self.assertEqual(len(self.cursor.rows),20)
        self.assertTrue(self.cursor.closed);self.assertFalse(self.tx._scheduled_cursor_active)
        self.assertFalse(self.tx._failed)
    def test_locked_prefix_preserves_fetches_and_unconsumed_tail(self):
        with self.stream() as rows:
            self.assertEqual([row['video_id'] for row in rows.locked_prefix(50)],[str(i) for i in range(10)])
            self.assertEqual([row['video_id'] for row in rows.locked_prefix(40)],[str(i) for i in range(10,50)])
            self.assertEqual(self.cursor.sizes,[10,50])
            self.assertEqual([row['video_id'] for row in rows._buffer],[str(i) for i in range(50,60)])
        self.assertEqual(len(self.cursor.rows),20)
        self.assertFalse(self.tx._failed)
    def test_locked_prefix_does_not_fetch_to_fill_short_buffer(self):
        with self.stream() as rows:
            self.assertEqual(len(rows.locked_prefix(3)),3)
            self.assertEqual(len(rows.locked_prefix(50)),7)
            self.assertEqual(self.cursor.sizes,[10])
            self.assertEqual(next(rows)['video_id'],'10')
            self.assertEqual(self.cursor.sizes,[10,50])
    def test_locked_prefix_rejects_invalid_bounds_and_poison_owner(self):
        for limit in (0,-1,51,True,None):
            with self.subTest(limit=limit):
                self.tx=Transaction(self.connection,self.gate,total_timeout_ms=8000)
                with self.assertRaises(DatabaseError):
                    with self.stream() as rows:rows.locked_prefix(limit)
                self.assertTrue(self.tx._failed)
        self.assertEqual(self.cursor.sizes,[])
    def test_locked_prefix_checks_ownership_deadline_and_sanitizes_errors(self):
        with self.stream() as rows:rows.locked_prefix(1)
        with self.assertRaises(DatabaseError):rows.locked_prefix(1)
        self.tx=Transaction(self.connection,self.gate,total_timeout_ms=8000)
        with self.assertRaises(DatabaseServerError):
            with self.stream() as rows:
                rows.locked_prefix(1);self.tx._deadline=0;rows.locked_prefix(1)
        self.tx=Transaction(self.connection,self.gate,total_timeout_ms=8000)
        self.cursor.error=ServerFailure('55P03')
        with self.assertRaises(DatabaseServerError) as error:
            with self.stream() as rows:rows.locked_prefix(50)
        self.assertEqual(error.exception.code,'55P03');self.assertNotIn('private',str(error.exception))
    def test_locked_prefix_exhaustion_is_stable_and_does_not_poison_owner(self):
        with self.stream() as rows:
            self.assertEqual([len(rows.locked_prefix(50)) for _ in range(5)],[10,50,20,0,0])
            self.assertEqual(self.cursor.sizes,[10,50,50,50])
        self.assertFalse(self.tx._failed)
    def test_end_of_stream_fetches_empty_batch_without_relocking(self):
        with self.stream() as rows:self.assertEqual(list(rows),self.rows)
        self.assertEqual(self.cursor.sizes,[10,50,50,50])
    def test_fixed_query_and_native_cursor_options(self):
        with self.stream(video_type='short') as rows:next(rows)
        options=self.connection.cursor.call_args.kwargs
        self.assertEqual(options['name'],'lic_video_scheduled_candidates');self.assertIs(options['withhold'],False);self.assertIs(options['scrollable'],False)
        sql,params=self.cursor.calls[0];self.assertIn('FOR UPDATE OF state SKIP LOCKED',sql);self.assertIn('ORDER BY page.rank',sql)
        self.assertIn('state.video_type=%(video_type)s::text',sql);self.assertEqual(params['video_type'],'short')
    def test_capability_rejects_readonly_rpc_duplicate_or_unbounded_input(self):
        for options,readonly,rpc in (({},True,None),({},False,'cleanup_hot_operational_copies'),({'ids':['duplicate']*2},False,None),({'ids':['x']*1001},False,None),({'video_type':'long'},False,None)):
            self.tx=Transaction(self.connection,self.gate,read_only=readonly,rpc_name=rpc)
            with self.assertRaises(DatabaseError):
                with self.stream(**options):pass
            self.assertTrue(self.tx._failed)
        self.connection.cursor.assert_not_called()
    def test_nested_refusal_does_not_release_outer_flag(self):
        with self.assertRaises(DatabaseError):
            with self.stream():
                try:
                    with self.stream():pass
                except DatabaseError:pass
                self.assertTrue(self.tx._scheduled_cursor_active)
        self.assertFalse(self.tx._scheduled_cursor_active)
    def test_iterator_cannot_escape_context_or_transaction_owner(self):
        with self.stream() as rows:next(rows)
        with self.assertRaises(DatabaseError):next(rows)
        self.assertTrue(self.tx._failed)
    def test_native_fetch_errors_are_sanitized_and_poison_even_if_caught(self):
        self.cursor.error=ServerFailure('55P03')
        with self.assertRaises(DatabaseError):
            with self.stream() as rows:
                try:next(rows)
                except DatabaseServerError as error:self.assertEqual(error.code,'55P03');self.assertNotIn('private',str(error))
        self.assertTrue(self.tx._failed);self.assertTrue(self.cursor.closed)
    def test_deadline_and_admission_checked_for_each_native_fetch(self):
        self.tx._check_budget=Mock()
        with self.stream() as rows:
            for _ in range(11):next(rows)
        self.assertEqual(sum(call.kwargs.get('configure',False) for call in self.tx._check_budget.call_args_list),3)
        self.assertEqual(self.gate.call_count,6)
    def test_buffer_does_not_outlive_deadline(self):
        with self.assertRaises(DatabaseServerError):
            with self.stream() as rows:
                next(rows);self.tx._deadline=0;next(rows)
        self.assertEqual(self.cursor.sizes,[10]);self.assertTrue(self.tx._failed)
    def test_more_rows_than_bounded_identity_window_fails_closed(self):
        with self.assertRaises(DatabaseError):
            with self.stream(ids=['one']) as rows:next(rows)
        self.assertTrue(self.tx._failed);self.assertTrue(self.cursor.closed)

if __name__=='__main__':unittest.main()
