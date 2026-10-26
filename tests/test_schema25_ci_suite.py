"""Offline fail-closed guards for the complete current-schema CI suite."""
from pathlib import Path
import ast,hashlib,importlib.util,json,os,re,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'application'),str(ROOT/'tests'),str(ROOT)]
from schema25_regressions import manifest,fixtures,suite,cases_readiness
from db.base.postgres import _runtime_admission,DatabaseError
import hot_runtime
from tools.sql_catalog import statements,parse_function

def tool(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tools'/f'{name}.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

class Schema25SuiteTests(unittest.TestCase):
    def test_current_manifest_exact_ten_plus_seventeen(self):
        data=manifest.load()
        self.assertEqual(len(data['sql']),10)
        self.assertEqual(set(data['python']),set(suite.CASES));self.assertEqual(len(suite.CASES),17)
        self.assertEqual(len(data['fixtures']),10)
        self.assertNotIn('historical',data)
        manifest.validate_dispatch(data,suite.CASES)

    def test_missing_case_or_current_sql_hash_fails_closed(self):
        original=manifest.ROOT/'tests/schema25_regressions/manifest.json';data=json.loads(original.read_text())
        with patch.object(manifest.Path,'read_text',return_value=json.dumps({**data,'expected_python_count':0})):
            with self.assertRaisesRegex(AssertionError,'python_manifest_count'):manifest.load()
        with patch.object(manifest.hashlib,'sha256') as digest:
            digest.return_value.hexdigest.return_value='0'*64
            with self.assertRaisesRegex(AssertionError,'sql_digest'):manifest.load()

    def test_current_suite_has_no_historical_file_dependencies(self):
        files=list((ROOT/'tests/schema25_regressions').glob('*.py'))
        files += [ROOT/'tools/check_schema25_repositories.py']
        for path in files:
            source=path.read_text()
            for retired in ('schema24_oracle','historical_json','schema/migrations','schema_migrations'):
                self.assertNotIn(retired,source,(path.name,retired))

    def test_exact_large_fixtures_and_explicit_budgets(self):
        data=manifest.load()
        raw=(ROOT/'tests/sql/40_user_work_limits.sql').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'10b8aadd091aef160fcd6ce3293f826b8152f36c87bcc88117a36f1c8eecd55d')
        text=raw.decode('utf-8')
        for term in ('generate_series(1, 1000)','1001 - (select count(*)','generate_series(1, 9999)','user_work_limit_reached'):self.assertIn(term,text)
        window=manifest.load_fixture('inventory_window_seed.sql',kind='seed')
        self.assertEqual(window.count('generate_series(1, 1500)'),2)
        source=(ROOT/'tests/schema25_regressions/cases_retention.py').read_text()
        self.assertIn('pages==1 and deleted==5',source);self.assertIn("page.scanned==5 and page.complete",source);self.assertIn('==1495',source)
        self.assertEqual(data['sql']['40_user_work_limits.sql']['row_budget'],15000)
        self.assertEqual(data['python']['inventory_window']['row_budget'],6500)

    def test_current_sql_does_not_call_retired_functions(self):
        schema=(ROOT/'schema/schema.sql').read_text()
        current={parse_function(s).name for s in statements(schema) if s.startswith('CREATE FUNCTION ')}
        tables={m.group(1) for m in re.finditer(r'CREATE (?:TABLE|VIEW) public\.([a-z_]+)',schema)}
        for path in (ROOT/'tests/sql').glob('*.sql'):
            for name in re.findall(r'public\.([a-z_]+)\s*\(',path.read_text()):
                self.assertIn(name,current|tables,(path.name,name))
            self.assertIn('rollback;',path.read_text().lower())
    def test_intrinsic_pins_match_current_baseline(self):
        pins=json.loads((ROOT/'schema/function-body-pins.json').read_text())
        actual={f.key:hashlib.sha256(f.body.encode()).hexdigest() for f in (parse_function(s) for s in statements((ROOT/'schema/schema.sql').read_text()) if s.startswith('CREATE FUNCTION '))}
        self.assertEqual(len(actual),13);self.assertEqual(actual,pins['functions'])
        self.assertEqual(hashlib.sha256((ROOT/'schema/schema.sql').read_bytes()).hexdigest(),pins['schema_sha256'])

    def test_current_identity_checks_real_body_digests_and_readiness(self):
        pins=json.loads((ROOT/'schema/function-body-pins.json').read_text())
        fixture=object.__new__(fixtures.Fixture);fixture.database=object()
        observed=[]
        def rows(sql):
            observed.append(sql)
            return [{'signature':name,'digest':digest} for name,digest in pins['functions'].items()]
        fixture.rows=rows
        with patch.object(fixtures.capabilities,'assert_typed_inventory_runtime_schema') as readiness:
            fixture.identity()
            readiness.assert_called_once_with(fixture.database)
        self.assertEqual(len(observed),1);self.assertIn('p.prosrc',observed[0])
        fixture.rows=lambda _: [{'signature':'unexpected()','digest':'0'*64}]
        with patch.object(fixtures.capabilities,'assert_typed_inventory_runtime_schema') as readiness:
            with self.assertRaisesRegex(AssertionError,'intrinsic_bodies'):fixture.identity()
            readiness.assert_not_called()

    def test_all_cases_have_real_assertions_not_skip_or_constant_return(self):
        for name,fn in suite.CASES.items():
            import inspect
            tree=ast.parse(inspect.getsource(fn))
            checks=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='check']
            self.assertGreaterEqual(len(checks),2,name)
            for n in checks:self.assertFalse(isinstance(n.args[0],ast.Constant),name)
        sources='\n'.join(p.read_text() for p in (ROOT/'tests/schema25_regressions').glob('*.py'))
        self.assertNotIn('SkipTest',sources);self.assertNotIn('skipTest',sources)
        self.assertNotIn('LegacyOracleDatabase',sources);self.assertNotIn('database.call(',sources)
    def test_repository_contracts_are_named_in_concrete_code(self):
        contracts={'cases_jobs.py':('enqueue_initial_inventory','enqueue_current_stats','prepare_chunk','release','ChannelTrashRepository','retry_not_before','retry_limit_reached'),
        'cases_user_work.py':('import_document','bind_pending','change_members','applied_revision','parent_channel_id','restore_owner_ref'),
        'cases_retention.py':('load_recheck_candidate_ids','start_attempt','finish_attempt','ensure_video_collection_state','list_cases','1495','availability_unsupported'),
        'cases_policy.py':('load_cadence_snapshot','reconcile_typed_video_collection_state','n==108','typed_due_at','metadata_due_at')}
        for file,names in contracts.items():
            text=(ROOT/'tests/schema25_regressions'/file).read_text()
            for name in names:self.assertIn(name,text)
    def test_actual_admission_and_blocked_closed_scope(self):
        with fixtures.admission() as gate:
            _runtime_admission(None);gate.ready=False
            with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):_runtime_admission(None)
            with hot_runtime.collection_control_access():
                _runtime_admission('abort_hot_collection_claims')
                _runtime_admission('interrupt_hot_sync_run')
                with self.assertRaises(hot_runtime.HotDatabaseAdmissionDenied):_runtime_admission(None)
        self.assertIsNone(hot_runtime._local_controller)
    def test_dsn_external_or_owner_runtime_denied(self):
        valid='host=127.0.0.1 port=5432 dbname=leti-cook user={} password='+('a'*64)
        env={'LIC_SCHEMA_CI_OWNED':'schema25_disposable','LIC_SCHEMA_CI_ADMIN_DSN':valid.format('postgres'),'LIC_SCHEMA_CI_DSN':valid.format('authenticator')}
        with patch.dict(os.environ,env):self.assertEqual(len(fixtures.dsns()),2)
        for key,value in (('LIC_SCHEMA_CI_OWNED',''),('LIC_SCHEMA_CI_DSN',valid.format('postgres')),('LIC_SCHEMA_CI_ADMIN_DSN',valid.format('postgres').replace('127.0.0.1','db.example')),('LIC_SCHEMA_CI_DSN',valid.format('authenticator').replace('dbname=leti-cook','dbname=let_it_cook'))):
            with patch.dict(os.environ,{**env,key:value}):
                with self.assertRaises(AssertionError):fixtures.dsns()
    def test_no_native_without_explicit_large_authorization(self):
        runner=tool('check_schema25_repositories')
        with patch.dict(os.environ,{'LIC_SCHEMA_CI_MAX_ROWS':'1000'}),patch.object(runner,'Fixture') as fixture:
            with self.assertRaisesRegex(AssertionError,'large_fixture_authorization_required'):runner.main()
            fixture.assert_not_called()
    def test_runner_exact_completion_and_cleanup(self):
        text=(ROOT/'tools/check_schema25_repositories.py').read_text()
        for term in ("validate_dispatch(manifest,CASES)","execute_python_case(fixture,CASES[name])","len(names)==27","finally:fixture.close()","fixture.admin.rollback();fixture.reset()","fixture.identity()"):
            self.assertIn(term,text)
        self.assertNotIn('argparse',text)
        main=next(node for node in ast.parse(text).body if isinstance(node,ast.FunctionDef) and node.name=='main')
        self.assertFalse(any(isinstance(node,ast.Continue) for node in ast.walk(main)))
        fixture=(ROOT/'tests/schema25_regressions/fixtures.py').read_text()
        for term in ('self.sequences()==self.sequence_baseline','self.catalog()==self.catalog_baseline','intrinsic_bodies','schema_digest'):
            self.assertIn(term,fixture)

    def test_budget_observation_cannot_pass_an_empty_case(self):
        runner=tool('check_schema25_repositories')
        class Counter:
            checks=0
            observed=False
            def observe(self):
                self.observed=True;self.checks+=1
        fixture=Counter()
        with self.assertRaisesRegex(AssertionError,'empty_python_case'):
            runner.execute_python_case(fixture,lambda _:None)
        self.assertFalse(fixture.observed)
        runner.execute_python_case(fixture,lambda f:setattr(f,'checks',f.checks+2))
        self.assertTrue(fixture.observed);self.assertEqual(fixture.checks,3)

    def test_invalid_dispatch_fails_before_database_creation(self):
        runner=tool('check_schema25_repositories')
        cases=dict(suite.CASES);cases.pop('availability_recheck')
        with patch.object(runner,'CASES',cases),patch.object(runner,'Fixture') as fixture:
            with self.assertRaisesRegex(AssertionError,'exact_dispatch'):runner.main()
            fixture.assert_not_called()

    def test_preflight_102_exact_and_real_public_boundary(self):
        raw=(ROOT/'application/db/readiness/collection_preflight.py').read_bytes()
        # Approved chain proof revision; historical preflight102 native evidence is not re-labelled.
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'6ba7fb860be3c1d1ef833f3f3ffae09efce2cdc3aa170667c168f99aaaa16e1a')
        text=(ROOT/'tests/schema25_regressions/cases_readiness.py').read_text()
        for term in ('assert_metadata_batch_ready','assert_forced_metadata_claim_ready',"code='22P02'",'DISABLE TRIGGER','REVOKE SELECT ON public.channel_collection_state','schema_ci_scan_fk'):
            self.assertIn(term,text)
        from db.readiness.capabilities import CHECKS
        self.assertEqual(set(CHECKS),cases_readiness.EXPECTED_CHECKS)
    def test_fresh_suite_keeps_two_container_isolation(self):
        fresh=(ROOT/'tools/check_schema_postgres.sh').read_text()
        shared=(ROOT/'tools/check_schema_ci_suite.sh').read_text()
        self.assertIn('tools/check_schema_ci_suite.sh',fresh);self.assertNotIn('for check in ',fresh)
        self.assertIn('--network none',fresh);self.assertNotIn('--publish',fresh)
        self.assertIn('--network "container:$LIC_SCHEMA_CI_PG_ID"',shared)
        self.assertIn('--read-only',shared);self.assertNotIn('pip install',shared)
        for text in (fresh,shared):self.assertIn('--memory 768m --memory-swap 768m --cpus 1.5',text)
        self.assertIn('[[ $# == 1 ]]',shared)
        for retired in ('seed24','verify25','/state:rw'):self.assertNotIn(retired,shared)

    def test_locked_dependency_preparation_separate(self):
        text=(ROOT/'tools/prepare_schema_ci_python.sh').read_text()
        self.assertIn('--require-hashes --no-deps',text);self.assertIn('application/requirements.lock',text);self.assertIn('application/Dockerfile',text)
        workflow=(ROOT/'.github/workflows/package.yml').read_text()
        self.assertEqual(workflow.count('bash tools/prepare_schema_ci_python.sh'),1)
        self.assertEqual(workflow.count("LIC_SCHEMA_CI_MAX_ROWS: '15000'"),1)
        self.assertNotIn("check_schema_upgrade.sh",workflow)

if __name__=='__main__':unittest.main()
