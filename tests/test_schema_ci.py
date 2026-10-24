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
