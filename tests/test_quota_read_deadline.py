"""Shared readonly retry deadline covers pool admission, setup and SQL dispatch."""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'application'))
from db.base import postgres as pg
from test_direct_pg_transactions import Connection,Pool,Result,DSN


class QuotaReadDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.events=[]
        self.connection=Connection(self.events)
        self.pool=Pool(self.connection,self.events)
        self.db=pg.PostgresDatabase(pg.PostgresConfig(DSN),admission=lambda _:None,
            pool_factory=lambda _:self.pool).open()
        self.addCleanup(self.db.close)
        self.now=100.0
        timer=patch.object(pg.time,'monotonic',side_effect=lambda:self.now)
        timer.start(); self.addCleanup(timer.stop)

    def operation(self,deadline=102):
        return pg.Operation('provider_quota_control_read',read_only=True,
            total_timeout_ms=5000,deadline_monotonic=deadline)

    def test_pool_wait_reduces_setup_and_transaction_budget(self):
        original=self.pool.connection
        @contextmanager
        def delayed(**kwargs):
            with original(**kwargs) as connection:
                self.now += 1.5
                yield connection
        self.pool.connection=delayed
        with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'), self.db.transaction(self.operation()) as tx:
            self.assertEqual(self.pool.timeouts,[2.0])
            self.assertIn(("SET LOCAL statement_timeout = '500ms'",()),self.connection.setup)
            self.connection.results.append(Result(rows=({'value':1},)))
            self.assertEqual(tx.fetch_one('SELECT 1 AS value'),{'value':1})
            self.now=102.1
            with self.assertRaises(pg.DatabaseServerError):tx.fetch_one('SELECT 2 AS value')
            self.assertEqual(len(self.connection.queries),1)

    def test_expired_during_pool_wait_sends_no_setup_or_domain_sql(self):
        original=self.pool.connection
        @contextmanager
        def delayed(**kwargs):
            with original(**kwargs) as connection:
                self.now=103
                yield connection
        self.pool.connection=delayed
        with self.assertRaises(pg.DatabaseServerError):
            with self.db.transaction(self.operation()):self.fail('expired read admitted')
        self.assertEqual(self.connection.setup,[])
        self.assertEqual(self.pool.returned,1)

    def test_expired_before_pool_wait_does_not_acquire(self):
        with self.assertRaises(pg.DatabaseServerError):
            with self.db.transaction(self.operation(99)):self.fail('expired read admitted')
        self.assertEqual(self.pool.timeouts,[])

    def test_setup_expiry_never_starts_domain_body(self):
        execute=self.connection.execute
        def delayed(sql,parameters=()):
            execute(sql,parameters)
            self.now += 1.1
        self.connection.execute=delayed
        with self.assertRaises(pg.DatabaseServerError):
            with self.db.transaction(self.operation()):self.fail('expired setup admitted')
        self.assertEqual(self.connection.queries,[])
        self.assertEqual(self.connection.rollbacks,1)

    def test_absolute_deadline_is_readonly_and_finite(self):
        for values in ({'read_only':False,'deadline_monotonic':102},
                       {'read_only':True,'deadline_monotonic':float('inf')},
                       {'read_only':True,'deadline_monotonic':True}):
            with self.assertRaises(ValueError):pg.Operation('invalid_deadline',**values)


if __name__=='__main__':unittest.main()
