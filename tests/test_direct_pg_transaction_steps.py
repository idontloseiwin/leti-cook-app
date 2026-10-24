"""Multi-statement transaction budget, rollback recovery and snapshot contracts."""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import psycopg
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.base import postgres as pg
from test_direct_pg_transactions import Connection, Pool, Result, ServerFailure, DSN

class TransactionStepsTests(unittest.TestCase):
    def setUp(self):
        self.trace=[];self.connection=Connection(self.trace);self.pool=Pool(self.connection,self.trace)
        self.db=pg.PostgresDatabase(pg.PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.db.close)
        self.now=10.0
        timer=patch.object(pg.time,'monotonic',side_effect=lambda:self.now)
        timer.start();self.addCleanup(timer.stop)

    def queue_write(self, error=None):
        self.connection.results.append(Result(has_columns=False,rowcount=1,execute_error=error))

    def test_import_lock_uses_fixed_order_nowait_and_same_admission_budget(self):
        self.queue_write()
        with self.db.transaction(pg.Operation('restore',total_timeout_ms=1000)) as tx:
            self.assertIsNone(tx.lock_user_work_import())
        statement,params=self.connection.queries[0]
        self.assertEqual(str(statement),pg._USER_WORK_IMPORT_LOCK)
        self.assertEqual(params,())
        self.assertTrue(str(statement).endswith('IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
        names=['channels','folders','channel_labels','channel_label_assignments','saved_objects',
               'saved_collection_members','saved_collection_commands','video_type_review_cases',
               'video_type_review_history','user_channel_restore_items']
        self.assertEqual([str(statement).index('public.'+name) for name in names],
                         sorted(str(statement).index('public.'+name) for name in names))

    def test_import_lock_denied_for_readonly_and_arbitrary_lock_sql(self):
        with self.assertRaisesRegex(pg.DatabaseError,'database_lock_capability_invalid'):
            with self.db.transaction(pg.Operation('read',read_only=True)) as tx:
                tx.lock_user_work_import()
        with self.assertRaises(pg.DatabaseError):
            with self.db.transaction(pg.Operation('write')) as tx:
                tx.execute('LOCK TABLE public.channels IN SHARE ROW EXCLUSIVE MODE NOWAIT')
        self.assertEqual(self.connection.queries,[])

    def test_borrowed_domain_failure_cannot_be_caught_then_committed(self):
        self.queue_write()
        with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'):
            with self.db.transaction(pg.Operation('composed')) as tx:
                tx.execute('DELETE FROM public.folders')
                try:
                    with tx.borrow():
                        raise pg.DatabaseServerError('channel_collection_scope_lost',code='P0001')
                except pg.DatabaseServerError:
                    pass
        self.assertEqual(self.connection.commits,0)
        self.assertEqual(self.connection.rollbacks,1)

    def test_savepoint_recovers_borrowed_domain_failure_only_after_rollback(self):
        self.queue_write();self.queue_write()
        with self.db.transaction(pg.Operation('composed')) as tx:
            tx.execute('DELETE FROM public.folders')
            with self.assertRaises(pg.DatabaseServerError):
                with tx.savepoint():
                    with tx.borrow():
                        raise pg.DatabaseServerError('channel_collection_scope_lost',code='P0001')
            tx.execute('DELETE FROM public.folders')
        self.assertEqual(self.connection.commits,1)
        self.assertEqual(self.connection.rollbacks,1)
        self.assertEqual(self.pool.borrowed,1)

    def test_statement_budgets_shrink_and_commit_keeps_remaining_budget(self):
        self.queue_write();self.queue_write()
        with self.db.transaction(pg.Operation('batch',total_timeout_ms=1000)) as tx:
            tx.execute('UPDATE public.folders SET name=%s',('one',))
            self.now+=0.25
            tx.execute('UPDATE public.folders SET name=%s',('two',))
            self.now+=0.25
        timers=[p[0] for q,p in self.connection.setup if q=="SELECT set_config('statement_timeout', %s, true)"]
        self.assertEqual(timers,['1000ms','750ms','500ms'])
        self.assertEqual(self.connection.commits,1)

    def test_exhausted_budget_before_second_statement_rolls_back_first(self):
        self.queue_write()
        with self.assertRaises(pg.DatabaseServerError) as error:
            with self.db.transaction(pg.Operation('batch',total_timeout_ms=1000)) as tx:
                tx.execute('UPDATE public.folders SET name=%s',('one',))
                self.now+=1.0
                tx.execute('UPDATE public.folders SET name=%s',('two',))
        self.assertEqual(error.exception.code,'57014')
        self.assertEqual(len(self.connection.queries),1)
        self.assertEqual(self.connection.commits,0)
        self.assertEqual(self.connection.rollbacks,1)

    def test_python_work_after_last_statement_cannot_commit_past_deadline(self):
        self.queue_write()
        with self.assertRaises(pg.DatabaseServerError):
            with self.db.transaction(pg.Operation('batch',total_timeout_ms=1000)) as tx:
                tx.execute('DELETE FROM public.folders')
                self.now+=1.01
        self.assertEqual(self.connection.commits,0)

    def test_savepoint_rolls_back_and_allows_caller_to_handle_known_error(self):
        self.queue_write(ServerFailure('55P03'));self.queue_write()
        with self.db.transaction(pg.Operation('per_item')) as tx:
            with self.assertRaises(pg.DatabaseServerError) as error:
                with tx.savepoint():tx.execute('DELETE FROM public.folders')
            self.assertEqual(error.exception.code,'55P03')
            tx.execute('UPDATE public.folders SET name=%s',('kept',))
        self.assertEqual(self.connection.rollbacks,1)
        self.assertEqual(self.connection.commits,1)
        self.assertEqual(self.pool.borrowed,1)

    def test_failure_swallowed_inside_savepoint_is_rolled_back_not_committed(self):
        self.queue_write(ServerFailure('23505'));self.queue_write()
        with self.db.transaction(pg.Operation('per_item')) as tx:
            with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'):
                with tx.savepoint():
                    try:tx.execute('DELETE FROM public.folders')
                    except pg.DatabaseServerError:pass
            tx.execute('DELETE FROM public.folders')
        self.assertEqual(self.connection.rollbacks,1)
        self.assertEqual(self.connection.commits,1)

    def test_a_savepoint_cannot_clear_prior_poisoning(self):
        self.queue_write(ServerFailure('23505'))
        with self.assertRaises(pg.DatabaseError):
            with self.db.transaction(pg.Operation('per_item')) as tx:
                try:tx.execute('DELETE FROM public.folders')
                except pg.DatabaseServerError:pass
                with self.assertRaises(pg.DatabaseError):
                    with tx.savepoint():pass
        self.assertEqual(self.connection.commits,0)

    def test_failed_savepoint_rollback_keeps_parent_poisoned(self):
        with self.assertRaises(pg.DatabaseError):
            with self.db.transaction(pg.Operation('per_item')) as tx:
                @contextmanager
                def broken_rollback():
                    try:yield
                    finally:raise psycopg.OperationalError('private connection detail')
                with patch.object(self.connection,'transaction',broken_rollback):
                    with self.assertRaises(pg.DatabaseUnavailable) as error:
                        with tx.savepoint():raise ValueError('candidate')
                    self.assertNotIn('private',str(error.exception))
                with self.assertRaises(pg.DatabaseError):tx.execute('DELETE FROM public.folders')
        self.assertEqual(self.connection.commits,0)

    def test_snapshot_is_declared_before_first_query(self):
        self.connection.results.append(Result(rows=({'n':1},)))
        with self.db.transaction(pg.Operation('export',read_only=True,repeatable_read=True)) as tx:
            tx.fetch_one('SELECT 1 AS n')
        commands=[q for q,p in self.connection.setup]
        self.assertEqual(commands[:3],['SET LOCAL ROLE service_role','SET TRANSACTION ISOLATION LEVEL REPEATABLE READ','SET TRANSACTION READ ONLY'])
        with self.assertRaises(ValueError):pg.Operation('import',repeatable_read=True)

    def test_invalid_budget_refused_before_pool(self):
        for value in (0,-1,True,1.5,120001):
            with self.assertRaises(ValueError):pg.Operation('bounded',total_timeout_ms=value)
        self.assertEqual(self.pool.borrowed,0)

if __name__=='__main__':unittest.main()
