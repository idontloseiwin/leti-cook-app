"""Offline guards for the separate, unexecuted native timeout-cache proof."""
from contextlib import contextmanager
from pathlib import Path
import importlib.util,io,json,sys,tempfile,unittest
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'application'))
SPEC=importlib.util.spec_from_file_location('timeout_cache_probe_test',ROOT/'tools/check_direct_pg_timeout_cache.py')
probe=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(probe)
from test_direct_pg_timeout_cache import LocalConnection,LocalCursor
from test_direct_pg_transactions import Result,Pool,DSN,render


class NativeSettingCursor(LocalCursor):
    def execute(self,statement,parameters):
        text=render(statement)
        if text==probe._SESSION:
            rows=({'backend':123,'login':'authenticator','role':'service_role' if self.connection.depth else 'authenticator',
                'timeout':self.connection.timeout},)
        elif probe._NATIVE_CHANGE in text:
            rows=({'value':'777ms'},);self.connection.domain_timeout_change=777
        elif probe._SETTING in text:rows=({'value':self.connection.timeout},)
        else:raise AssertionError('unexpected native fixture SQL')
        self.connection.results.appendleft(Result(rows=rows))
        super().execute(statement,parameters)


class NativeSettingConnection(LocalConnection):
    def cursor(self):return NativeSettingCursor(self)


class CacheProbeTests(unittest.TestCase):
    def setUp(self):
        events=[];self.connection=NativeSettingConnection(events);self.connection.timeout=0
        pool=Pool(self.connection,events)
        self.database=probe.pg.PostgresDatabase(probe.pg.PostgresConfig(DSN),admission=lambda name:None,pool_factory=lambda config:pool).open()
        self.addCleanup(self.database.close)
        self.fixture=probe.fixtures.Probe(Mock(),self.database,{},{});self.fixture.cache_observations=[]

    def test_cli_is_explicit_offline_and_preserves_existing_report(self):
        with patch.object(probe.safety,'connect') as connect,patch('sys.stdout',new_callable=io.StringIO) as out:
            self.assertEqual(probe.main(['--list-cases']),0)
            body=json.loads(out.getvalue());self.assertEqual(set(body['cases']),{'contract','savepoints','reuse'})
            self.assertEqual(body['seed_row_limit'],0);self.assertFalse(body['phase_accepted'])
            with tempfile.TemporaryDirectory() as directory:
                target=Path(directory)/'result.json';target.write_text('keep')
                for args in ([],['--case','reuse','--case','reuse'],['--case','reuse','--report',str(target)]):
                    with patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit):probe.main(args)
                self.assertEqual(target.read_text(),'keep')
            connect.assert_not_called()

    def test_exact_contract_runs_real_driver_and_actual_check_name_guard(self):
        probe.contract(self.fixture)
        self.assertEqual(self.fixture.checks,['timeout_cache_default_false','timeout_cache_default_true','timeout_cache_exact_integer'])
        self.assertEqual([row['configured_ms'] for row in self.fixture.cache_observations],[[500,500,500],[500],[1000,875,874]])
        self.assertEqual(self.connection.timeout,0)

    def test_savepoint_protocol_reads_restored_local_state_before_reconfiguration(self):
        probe.savepoints(self.fixture)
        self.assertEqual(self.fixture.checks,['timeout_cache_savepoint_rollback','timeout_cache_savepoint_release','timeout_cache_savepoint_native_guc'])
        self.assertEqual([row['configured_ms'] for row in self.fixture.cache_observations],[[1000,750,750],[1000,750],[1000,750,750]])
        self.assertEqual(self.connection.timeout,0)

    def test_savepoint_probe_rejects_stale_cache_after_realistic_local_rollback(self):
        original=probe.pg.Transaction.savepoint
        @contextmanager
        def stale_cache(tx):
            try:
                with original(tx):yield
            except probe.RollbackFixture:
                tx._known_statement_timeout_ms=750
                raise
        with patch.object(probe.pg.Transaction,'savepoint',stale_cache),self.assertRaisesRegex(probe.fixtures.ProbeFailure,'timeout_cache_reconfigured_after_rollback'):
            probe.savepoints(self.fixture)
        self.assertEqual(self.fixture.checks,[])

    def test_reuse_checks_same_backend_and_both_success_and_failed_transaction(self):
        probe.reuse(self.fixture)
        self.assertEqual(self.fixture.checks,['timeout_cache_same_backend_no_leak'])
        self.assertEqual(self.connection.timeout,0)
        before=dict(backend=123,login='authenticator',role='authenticator',timeout=0)
        for field,value in (('backend',456),('role','service_role'),('timeout',777)):
            with patch.object(probe,'session',side_effect=[before,{**before,field:value}]),self.assertRaisesRegex(probe.fixtures.ProbeFailure,'timeout_cache_no_setting_or_role_leak'):
                probe.reuse(self.fixture)

    def test_clock_patch_is_driver_local_and_restored_on_error(self):
        original=probe.pg.time
        with self.assertRaises(ValueError):
            with probe.transaction(self.fixture) as (tx,now,counter):
                self.assertIsNot(probe.pg.time,original)
                self.assertEqual(probe.pg.time.monotonic(),10.0)
                self.assertIs(sys.modules['time'],original)
                raise ValueError('stop')
        self.assertIs(probe.pg.time,original)

    def test_finite_source_has_no_domain_writes_delays_or_threshold_change(self):
        source=Path(probe.__file__).read_text(encoding='utf-8')
        for token in ('INSERT INTO','UPDATE public','DELETE FROM','CREATE TABLE','ALTER TABLE','GRANT ','pg_sleep','time.sleep','prepare_threshold','performance_gates'):
            self.assertNotIn(token,source)
        self.assertIn("patch.object(pg,'time',",source);self.assertNotIn("patch.object(pg.time,",source)
        self.assertIn('safety.preflight(admin)',source);self.assertIn('probe.reset()',source)
        self.assertIn('cursor.fetchmany(2)',source);self.assertEqual(set(probe.RUNNERS),set(probe.CASES))
        self.assertNotIn('private value',str(probe.fixtures.safe_failure(ValueError('private value'))))


if __name__=='__main__':unittest.main()
