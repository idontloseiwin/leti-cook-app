"""Recovery snapshot cursor bounds and ownership; native parity is separate."""
from contextlib import contextmanager
from pathlib import Path
import sys,unittest
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
from db.base.postgres import Transaction,DatabaseError,DatabaseServerError
from db.runs import sync_run_recovery as recovery
from test_direct_pg_transactions import Connection,ServerFailure
from test_video_scheduled_cursor import Cursor


class RecoveryCursorTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'id':i,'columns_text':'{}','summary_text':'{}'} for i in range(101)]
        self.cursor=Cursor(self.rows);self.connection=Connection([])
        self.connection.cursor=Mock(return_value=self.cursor);self.gate=Mock()
        self.tx=Transaction(self.connection,self.gate,total_timeout_ms=8000)
    def stream(self):return self.tx.sync_run_recovery_candidates(before='2026-10-06',finished_at=None)
    def test_fetches_one_row_without_prefetch_or_total_cutoff(self):
        with self.stream() as rows:self.assertEqual(list(rows),self.rows)
        self.assertEqual(self.cursor.sizes,[1]*102)
        self.assertEqual(len(self.cursor.calls),1)
        self.assertTrue(self.cursor.closed)
        self.assertFalse(self.tx._failed)
    def test_fixed_native_query_and_one_nonholdable_cursor(self):
        with self.stream() as rows:next(rows)
        self.assertEqual(self.cursor.calls,[(recovery._RECOVERY_CANDIDATES,dict(before='2026-10-06',finished_at=None))])
        options=self.connection.cursor.call_args.kwargs
        self.assertEqual(options['name'],'lic_sync_run_recovery')
        self.assertFalse(options['withhold']);self.assertFalse(options['scrollable'])
        statement=recovery._RECOVERY_CANDIDATES.as_string()
        self.assertIn("run.status='running' AND run.started_at<%(before)s::timestamptz",statement)
        self.assertIn('FOR NO KEY UPDATE OF run',statement)
        self.assertNotIn('LIMIT',statement);self.assertNotIn('SKIP LOCKED',statement)
        self.assertIn("((run.summary_json->>'column_fields')::integer & 4)",statement)
    def test_readonly_rpc_and_missing_total_budget_rejected_before_dispatch(self):
        for kwargs in ({'read_only':True,'total_timeout_ms':8000},{'rpc_name':'recover_stale_sync_runs','total_timeout_ms':8000},{}):
            self.tx=Transaction(self.connection,self.gate,**kwargs)
            with self.assertRaises(DatabaseError):
                with self.stream():pass
            self.assertTrue(self.tx._failed)
        self.connection.cursor.assert_not_called()
    def test_each_fetch_rechecks_admission_and_deadline(self):
        self.tx._check_budget=Mock()
        with self.stream() as rows:next(rows);next(rows)
        self.assertEqual(self.gate.call_count,6)
        self.assertEqual(sum(c.kwargs.get('configure',False) for c in self.tx._check_budget.call_args_list),3)
    def test_cursor_cannot_escape_context(self):
        with self.stream() as rows:next(rows)
        with self.assertRaises(DatabaseError):next(rows)
        self.assertTrue(self.tx._failed)
    def test_nested_cursor_denial_preserves_outer_ownership_until_exit(self):
        with self.assertRaises(DatabaseError):
            with self.stream():
                with self.assertRaises(DatabaseError):
                    with self.stream():pass
                self.assertTrue(self.tx._scheduled_cursor_active)
        self.assertFalse(self.tx._scheduled_cursor_active)
        self.assertEqual(self.connection.cursor.call_count,1)
    def test_fetch_error_is_sanitized_and_poisons_even_if_caught(self):
        self.cursor.error=ServerFailure('55P03')
        with self.assertRaises(DatabaseError):
            with self.stream() as rows:
                with self.assertRaises(DatabaseServerError) as caught:next(rows)
                self.assertEqual(caught.exception.code,'55P03')
                self.assertNotIn('private',str(caught.exception))
        self.assertTrue(self.tx._failed);self.assertTrue(self.cursor.closed)
    def test_deadline_stops_before_next_fetch(self):
        with self.assertRaises(DatabaseServerError):
            with self.stream() as rows:
                next(rows);self.tx._deadline=0;next(rows)
        self.assertEqual(self.cursor.sizes,[1])
    def test_overreturn_fails_closed(self):
        self.cursor.fetchmany=lambda count:self.rows[:2]
        with self.assertRaises(DatabaseError):
            with self.stream() as rows:next(rows)
        self.assertTrue(self.tx._failed)


class RecoveryOwnerTests(unittest.TestCase):
    def transaction(self,rows,changed=1):
        tx=Mock()
        @contextmanager
        def borrow():yield tx
        @contextmanager
        def cursor(**kwargs):yield iter(rows)
        tx.borrow=Mock(side_effect=borrow);tx.sync_run_recovery_candidates=Mock(side_effect=cursor)
        tx.execute.return_value=changed
        return tx
    def test_one_borrow_cursor_and_count_without_new_transaction(self):
        rows=[dict(id=i,columns_text='{"id":'+str(i)+'}',summary_text='{}') for i in (1,2)]
        tx=self.transaction(rows)
        with patch.object(recovery,'_final_summary',return_value='{"errors":1}') as normalize:
            self.assertEqual(recovery.recover_stale_sync_runs(tx,before='cutoff',finished_at='finish'),2)
        tx.borrow.assert_called_once();tx.sync_run_recovery_candidates.assert_called_once_with(before='cutoff',finished_at='finish')
        self.assertEqual(normalize.call_count,2)
        self.assertEqual(tx.execute.call_args_list[1].args[1],('{"errors":1}','{"id":2}',2))
    def test_no_rows_never_normalizes_or_writes(self):
        tx=self.transaction([])
        with patch.object(recovery,'_final_summary') as normalize:
            self.assertEqual(recovery.recover_stale_sync_runs(tx,before='cutoff',finished_at=None),0)
        normalize.assert_not_called();tx.execute.assert_not_called()
    def test_malformed_row_or_missing_locked_update_fails(self):
        for row,count in [({'id':True,'columns_text':'{}','summary_text':'{}'},1),
                          ({'id':1,'columns_text':'{}','summary_text':'{}'},0)]:
            tx=self.transaction([row],changed=count)
            with patch.object(recovery,'_final_summary',return_value='{}'),self.assertRaises(DatabaseError):
                recovery.recover_stale_sync_runs(tx,before='cutoff',finished_at=None)


if __name__=='__main__':unittest.main()
