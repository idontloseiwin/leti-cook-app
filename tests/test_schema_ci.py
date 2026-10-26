"""The SQL checks run in CI against the real bootstrap and leave no data behind. Source only."""
from pathlib import Path
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]

class SchemaCiTests(unittest.TestCase):
    def test_every_sql_check_rolls_back(self):
        checks=sorted((ROOT/'tests/sql').glob('*.sql'))
        self.assertGreaterEqual(len(checks),4)
        for check in checks:
            statements=[line for line in check.read_text(encoding='utf-8').splitlines()
                        if line and not line.startswith('--') and not line.startswith(' ')]
            begins=[i for i,line in enumerate(statements) if line=='begin;']
            rollbacks=[i for i,line in enumerate(statements) if line=='rollback;']
            self.assertTrue(begins and len(begins)==len(rollbacks),check.name)
            self.assertEqual(statements[0],'begin;',check.name)
            self.assertEqual(statements[-1],'rollback;',check.name)
            self.assertNotRegex(check.read_text(encoding='utf-8'),r'(?im)^\s*commit\s*;',check.name)

    def test_ci_uses_the_installation_bootstrap(self):
        workflow=(ROOT/'.github/workflows/package.yml').read_text(encoding='utf-8')
        self.assertIn('run: bash tools/check_schema_postgres.sh',workflow)
        script=(ROOT/'tools/check_schema_postgres.sh').read_text(encoding='utf-8')
        self.assertIn('database/bootstrap.sh:/docker-entrypoint-initdb.d/10-self-hosted.sh:ro',script)
        self.assertIn('schema_ci_postgres_require "$root"',script)
        self.assertIn('image="$SCHEMA_CI_POSTGRES_IMAGE"',script)
        self.assertIsNotNone(re.search(r'postgres:[^\s]+@sha256:[0-9a-f]{64}',(ROOT/'database/Dockerfile').read_text(encoding='utf-8')))
        prepare=(ROOT/'tools/prepare_schema_ci_images.sh').read_text(encoding='utf-8')
        self.assertIn('docker pull "$SCHEMA_CI_POSTGRES_BASE"',prepare)
        self.assertIn('schema_ci_postgres_build "$root"',prepare)
        self.assertNotIn('docker pull',script)

    def test_every_native_suite_runs_in_ci_or_says_why_not(self):
        script=(ROOT/'tools/check_schema_ci_suite.sh').read_text(encoding='utf-8')
        in_ci=set(re.findall(r'/repo/tests/(native_\w+)\.py',script))
        not_in_ci={
            'native_case_runner':'the runner other suites use',
            'native_initial_type_conflict':'its cases run inside native_automatic_type_policy',
            'native_quota_operation_packing':'a long measurement harness with variants, run by hand',
            'native_stats_availability':'superseded by native_unified_availability since step 28 (audit A-N5)',
            'native_data_lifecycle_acquisition':'superseded by native_publication_lifecycle since step 31 (audit A-N6)',
            'native_data_lifecycle_reviews':'superseded by native_automatic_type_policy since steps 29/32 (audit A-N7)',
            'native_data_lifecycle_migration':'needs a schema 26 database (audit A-N8)',
        }
        present={path.stem for path in (ROOT/'tests').glob('native_*.py')}
        self.assertEqual(in_ci|set(not_in_ci),present)
        self.assertFalse(in_ci&set(not_in_ci))

    def test_ci_databases_require_passwords_like_an_installation(self):
        # Without it initdb trusts every 127.0.0.1 connection, and CI never
        # checks the authenticator password an installation requires.
        compose=(ROOT/'database/compose.yaml').read_text(encoding='utf-8')
        self.assertIn('POSTGRES_INITDB_ARGS: --auth-host=scram-sha-256',compose)
        for name in ('check_schema_postgres.sh','check_schema_upgrades.sh','check_research_query_postgres.sh','retention_benchmark.sh'):
            script=(ROOT/'tools'/name).read_text(encoding='utf-8')
            self.assertEqual(script.count('-e POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256'),1,name)

    def test_research_query_sql_runs_on_an_empty_database_in_ci(self):
        # research_query_pg.py builds its own schema and refuses a bootstrapped one.
        workflow=(ROOT/'.github/workflows/package.yml').read_text(encoding='utf-8')
        self.assertIn('run: bash tools/check_research_query_postgres.sh',workflow)
        script=(ROOT/'tools/check_research_query_postgres.sh').read_text(encoding='utf-8')
        self.assertIn('python -B /repo/tests/research_query_pg.py',script)
        self.assertIn('source "$root/tools/schema_ci_resources.sh"',script)
        self.assertEqual(script.count('schema_ci_create '),2)
        self.assertIn('schema_ci_require_pg "$SCHEMA_CI_LAST_ID"',script)
        for absent in ('docker-entrypoint-initdb.d','docker run ','docker rm ','-p ','--publish'):
            self.assertNotIn(absent,script)

    def test_database_image_runtime_checks_actual_process_and_existing_volume(self):
        script=(ROOT/'tools/check_schema_postgres.sh').read_text(encoding='utf-8')
        for guard in ('/proc/1/status','/proc/1/environ','HOME=$home','if command -v gosu >/dev/null; then exit 1; fi',
                'CapEff CapPrm CapAmb CapInh',
                'pg_dump --username postgres --format custom','pg_restore --username postgres --exit-on-error',
                'docker stop --signal SIGTERM --timeout 30','schema_ci_assert_exited','1|retained'):
            self.assertIn(guard,script)
        self.assertLess(script.index('pg_restore --username'),script.index('docker stop --signal'))
        self.assertEqual(script.count('check_postgres_process\n'),2)
        self.assertLess(script.index('docker stop --signal'),script.index("== '1|retained'"))

if __name__=='__main__':unittest.main()
