"""Exact opt-in timeout reuse with modeled PostgreSQL LOCAL/savepoint rollback.

No database is contacted. Native savepoint/GUC behavior still needs the scoped
PG17 check before acceptance; the fake models that behavior rather than merely
asserting a private cache attribute.
"""
from contextlib import contextmanager
from pathlib import Path
import math
import sys
import unittest
from unittest.mock import patch

import psycopg
from psycopg import sql
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'application'))
from db.base import postgres as pg
from test_direct_pg_transactions import Connection,Cursor,Pool,Result,ServerFailure,DSN,render

TIMER="SELECT set_config('statement_timeout', %s, true)"


class LocalCursor(Cursor):
    def execute(self,statement,parameters):
        self.connection.domain_timeouts.append(self.connection.timeout)
        super().execute(statement,parameters)
        if self.connection.domain_timeout_change is not None:
            self.connection.timeout=self.connection.domain_timeout_change
            self.connection.domain_timeout_change=None


class LocalConnection(Connection):
    def __init__(self,events):
        super().__init__(events)
        self.timeout=None
        self.depth=0
        self.domain_timeouts=[]
        self.domain_timeout_change=None
        self.timer_failure=None

    def execute(self,statement,parameters=()):
        super().execute(statement,parameters)
        text=render(statement)
        if text==TIMER:
            if self.timer_failure is not None:
                error,self.timer_failure=self.timer_failure,None
                raise error
            self.timeout=int(parameters[0][:-2])
        elif text.startswith("SELECT set_config('search_path',"):
            self.timeout=int(parameters[0][:-2])

    @contextmanager
    def transaction(self):
        previous=self.timeout
        self.depth+=1
        try:
            with super().transaction():
                yield self
        except BaseException:
            self.timeout=previous
            raise
        else:
            if self.depth==1:self.timeout=previous
        finally:self.depth-=1

    def cursor(self):return LocalCursor(self)


class TimeoutCacheTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.connection=LocalConnection(self.events)
        self.pool=Pool(self.connection,self.events);self.admissions=[]
        self.db=pg.PostgresDatabase(pg.PostgresConfig(DSN),admission=self.admissions.append,
            pool_factory=lambda config:self.pool).open()
        self.addCleanup(self.db.close);self.now=10.0
        timer=patch.object(pg.time,'monotonic',side_effect=lambda:self.now)
        timer.start();self.addCleanup(timer.stop)

    def operation(self,**changes):
        return pg.Operation('reviewed_protocol',**dict(total_timeout_ms=1000,
            cache_statement_timeout=True,**changes))

    def read(self,tx,statement='SELECT 1 AS value'):
        self.connection.results.append(Result(rows=({'value':1},)))
        return tx.fetch_one(statement)

    def timers(self):return [p[0] for q,p in self.connection.setup if q==TIMER]

    def test_identical_integer_reused_without_skipping_admission_or_domain_statements(self):
        with self.db.transaction(self.operation()) as tx:
            self.read(tx);self.read(tx)
        self.assertEqual(self.timers(),['1000ms'])
        self.assertEqual(len(self.admissions),6) # two acquisition and two per dispatch
        self.assertEqual(self.connection.domain_timeouts,[1000,1000])
        self.assertEqual(self.connection.commits,1)

    def test_default_disabled_preserves_every_configuration_including_commit(self):
        with self.db.transaction(pg.Operation('unreviewed',total_timeout_ms=1000)) as tx:
            self.read(tx,'SELECT public.arbitrary_function()');self.read(tx)
        self.assertEqual(self.timers(),['1000ms']*3)

    def test_no_total_deadline_still_does_no_per_statement_configuration(self):
        with self.db.transaction(pg.Operation('reviewed',cache_statement_timeout=True)) as tx:
            self.read(tx);self.read(tx)
        self.assertEqual(self.timers(),[])
        self.assertEqual(self.connection.domain_timeouts,[60000,60000])

    def test_ceiling_and_exact_ceil_millisecond_calculation_are_unchanged(self):
        with self.db.transaction(self.operation(statement_timeout_ms=900)) as tx:
            expected=[]
            for now in (10.0,10.0001,10.125,10.1251,10.1262,10.9995):
                self.now=now;target=min(900,max(1,math.ceil((11.0-now)*1000)))
                if not expected or expected[-1]!=str(target)+'ms':expected.append(str(target)+'ms')
                self.read(tx)
                self.assertEqual(self.connection.domain_timeouts[-1],target)
        self.assertEqual(self.timers(),expected)
        self.assertEqual(expected[-1],'1ms')

    def test_database_ceiling_still_caps_the_cached_integer(self):
        db=pg.PostgresDatabase(pg.PostgresConfig(DSN,statement_timeout_ceiling_ms=200),
            admission=lambda name:None,pool_factory=lambda config:self.pool).open()
        self.addCleanup(db.close)
        with db.transaction(self.operation()) as tx:self.read(tx);self.read(tx)
        self.assertEqual(self.timers(),['200ms'])

    def test_expired_budget_prevents_dispatch_even_when_previous_integer_matches(self):
        with self.assertRaises(pg.DatabaseServerError) as error:
            with self.db.transaction(self.operation()) as tx:
                self.read(tx);self.now=11.0;self.read(tx)
        self.assertEqual(error.exception.code,'57014')
        self.assertEqual(len(self.connection.queries),1);self.assertEqual(self.connection.commits,0)

    def test_commit_checks_budget_even_without_another_domain_statement(self):
        with self.assertRaises(pg.DatabaseServerError) as error:
            with self.db.transaction(self.operation()) as tx:self.read(tx);self.now=11.0
        self.assertEqual(error.exception.code,'57014');self.assertEqual(self.connection.commits,0)

    def test_second_admission_can_deny_a_cache_hit_before_dispatch(self):
        with self.assertRaisesRegex(RuntimeError,'closed'):
            with self.db.transaction(self.operation()) as tx:
                self.read(tx)
                calls=[]
                def gate(name):
                    calls.append(name)
                    if len(calls)==2:raise RuntimeError('closed')
                tx._admission=gate
                self.read(tx)
        self.assertEqual(calls,[None,None]);self.assertEqual(len(self.connection.queries),1)
        self.assertEqual(self.connection.commits,0)

    def test_successful_savepoint_release_preserves_installed_local_value(self):
        with self.db.transaction(self.operation()) as tx:
            self.read(tx)
            with tx.savepoint():self.now=10.25;self.read(tx)
            self.assertEqual(self.connection.timeout,750)
            self.read(tx)
        self.assertEqual(self.timers(),['1000ms','750ms'])

    def test_rollback_restores_local_value_then_requires_reconfiguration_at_same_clock(self):
        with self.db.transaction(self.operation()) as tx:
            self.read(tx)
            with self.assertRaises(ValueError):
                with tx.savepoint():
                    self.now=10.25;self.read(tx)
                    with tx.borrow():raise ValueError('domain')
            self.assertEqual(self.connection.timeout,1000)
            self.read(tx)
            self.assertEqual(self.connection.domain_timeouts[-1],750)
        self.assertEqual(self.timers(),['1000ms','750ms','750ms'])

    def test_nested_rollback_invalidates_each_restored_local_scope(self):
        with self.db.transaction(self.operation()) as tx:
            self.read(tx)
            with self.assertRaises(ValueError):
                with tx.savepoint():
                    self.now=10.25;self.read(tx)
                    with self.assertRaises(ValueError):
                        with tx.savepoint():
                            self.now=10.5;self.read(tx);raise ValueError('inner')
                    self.assertEqual(self.connection.timeout,750);self.read(tx)
                    raise ValueError('outer')
            self.assertEqual(self.connection.timeout,1000);self.read(tx)
        self.assertEqual(self.timers(),['1000ms','750ms','500ms','500ms','500ms'])

    def test_swallowed_statement_error_poison_requires_savepoint_rollback(self):
        with self.db.transaction(self.operation()) as tx:
            self.read(tx)
            with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'):
                with tx.savepoint():
                    self.connection.results.append(Result(execute_error=ServerFailure('23505')))
                    try:tx.fetch_one('SELECT 1')
                    except pg.DatabaseServerError:pass
            self.read(tx)
        self.assertEqual(self.timers(),['1000ms','1000ms'])
        self.assertEqual(self.connection.rollbacks,1)

    def test_failed_configuration_does_not_cache_the_attempted_value(self):
        with self.db.transaction(self.operation()) as tx:
            self.read(tx)
            with self.assertRaises(pg.DatabaseServerError) as error:
                with tx.savepoint():
                    self.now=10.25;self.connection.timer_failure=ServerFailure('57014')
                    self.read(tx)
            self.assertEqual(error.exception.code,'57014')
            self.assertEqual(self.connection.timeout,1000)
            self.read(tx);self.assertEqual(self.connection.domain_timeouts[-1],750)
        self.assertEqual(self.timers(),['1000ms','750ms','750ms'])

    def test_failed_savepoint_rollback_invalidates_but_never_clears_poison(self):
        with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'):
            with self.db.transaction(self.operation()) as tx:
                self.read(tx)
                @contextmanager
                def broken():
                    try:yield
                    finally:raise psycopg.OperationalError('private')
                with patch.object(self.connection,'transaction',broken):
                    with self.assertRaises(pg.DatabaseUnavailable):
                        with tx.savepoint():raise ValueError('domain')
                self.assertIsNone(tx._known_statement_timeout_ms)
                with self.assertRaisesRegex(pg.DatabaseError,'database_transaction_aborted'):self.read(tx)
        self.assertEqual(self.connection.commits,0)

    def test_set_config_and_escaped_or_quoted_names_invalidate_before_and_after(self):
        statements=["SELECT set_config('statement_timeout','7s',true)",
            """SELECT pg_catalog."set_config"('statement_timeout','7s',true)""",
            "SELECT PG_CATALOG.SET_CONFIG('statement_timeout','7s',true)",
            r"""SELECT U&"set\005fconfig"('statement_timeout','7s',true)""",
            sql.SQL("SELECT {}('statement_timeout','7s',true)").format(sql.Identifier('set_config'))]
        for statement in statements:
            with self.subTest(statement=str(statement)):
                self.connection.setup.clear()
                with self.db.transaction(self.operation()) as tx:
                    self.read(tx)
                    self.connection.domain_timeout_change=7000;self.read(tx,statement)
                    self.assertEqual(self.connection.timeout,7000)
                    self.read(tx);self.assertEqual(self.connection.domain_timeouts[-1],1000)
                self.assertEqual(self.timers(),['1000ms']*3)

    def test_rpc_label_disables_opt_in_even_for_transient_native_local_settings(self):
        with self.db._transaction(self.operation(),rpc_name='retained_native',allowed=frozenset()) as tx:
            self.connection.domain_timeout_change=7000;self.read(tx,'SELECT public.retained_native()')
            self.read(tx)
        self.assertEqual(self.timers(),['1000ms']*3)
        self.assertEqual(self.connection.domain_timeouts,[1000,1000])
        self.assertTrue(all(name=='retained_native' for name in self.admissions))

    def test_cache_is_not_shared_when_pool_connection_is_reused(self):
        for _ in range(2):
            with self.db.transaction(self.operation()) as tx:self.read(tx)
        self.assertEqual(self.timers(),['1000ms','1000ms']);self.assertEqual(self.pool.borrowed,2)

    def test_unknown_commit_preserves_outcome_and_has_no_retry(self):
        self.connection.results.append(Result(has_columns=False,rowcount=1))
        with self.assertRaises(pg.DatabaseUnavailable) as error:
            with self.db.transaction(self.operation()) as tx:
                tx.execute('DELETE FROM public.folders');self.connection.commit_error=psycopg.OperationalError('private')
        self.assertTrue(error.exception.outcome_unknown);self.assertEqual(len(self.connection.queries),1)
        self.assertNotIn('private',str(error.exception))

    def test_flag_requires_exact_bool_and_default_is_off(self):
        self.assertFalse(pg.Operation('default').cache_statement_timeout)
        for value in (None,0,1,'yes'):
            with self.assertRaises(ValueError):pg.Operation('invalid',cache_statement_timeout=value)

    def test_only_reviewed_owners_opt_in_and_borrowed_helpers_do_not_enable_it(self):
        from db.user_work import saved_mutations,user_work_read_queries
        from unittest.mock import Mock
        self.assertTrue(user_work_read_queries._EXPORT_OPERATION.cache_statement_timeout)
        self.assertFalse(user_work_read_queries._STATUS_OPERATION.cache_statement_timeout)
        owned=Mock();captured=[]
        @contextmanager
        def transaction(operation):captured.append(operation);yield owned
        database=Mock();database.transaction.side_effect=transaction
        with saved_mutations._transaction(database,None,'create_saved_object') as tx:self.assertIs(tx,owned)
        self.assertTrue(captured[0].cache_statement_timeout)
        borrowed=Mock()
        # A contextmanager fake avoids granting any new authority to a borrower.
        @contextmanager
        def borrow():yield borrowed
        borrowed.borrow.side_effect=borrow
        with saved_mutations._transaction(database,borrowed,'create_saved_object') as tx:self.assertIs(tx,borrowed)
        self.assertEqual(len(captured),1)


if __name__=='__main__':unittest.main()
