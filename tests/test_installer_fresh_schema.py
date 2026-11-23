"""Fresh schema, administrative baseline and checked release-step metadata."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / 'schema'
FIRST_LINE = b'-- leti-cook self-hosted clean schema; format=1; application-data=none'
BASELINE_VERSION = 51
BASELINE_SHA256 = '70f9af54a8ea29f350b48e97abc0a674da976226fd6b6992436e8d246c1a4eac'
SEED = re.compile(rb'INSERT INTO public\.[a-z_]+\([^)]*\)\s*VALUES\s*.*?;\n', re.S)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(statement):
    """Top-level parenthesised rows after VALUES."""
    depth = count = 0
    for char in statement.split(b'VALUES', 1)[1]:
        if char == ord('('):
            count += depth == 0
            depth += 1
        elif char == ord(')'):
            depth -= 1
    return count


class FreshSchemaDeliveryTests(unittest.TestCase):
    def test_schema_folder_holds_only_the_fresh_delivery(self):
        self.assertEqual({p.name for p in SCHEMA.iterdir()},
                         {'schema.sql', 'schema.sql.sha256', 'manifest.json', 'configuration-seeds.sql',
                          'function-body-pins.json', 'baseline', 'steps'})

    def test_schema_has_admin_ledger_but_no_fictitious_applied_history(self):
        schema = (SCHEMA / 'schema.sql').read_bytes()
        manifest = json.loads((SCHEMA / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(schema.splitlines()[0], FIRST_LINE)
        self.assertIn(b'CREATE TABLE operator_private.schema_migrations', schema)
        self.assertNotIn(b'INSERT INTO operator_private.schema_migrations', schema)
        version = manifest['schema_version']
        self.assertIn(b"VALUES (true,'leti-cook-release-01',%d,%d)" % (version, version), schema)
        # The distributed baseline is the version 51 schema and never changes;
        # later versions are reached only through numbered steps.
        baseline = (SCHEMA / 'baseline/schema.sql').read_bytes()
        self.assertEqual(hashlib.sha256(baseline).hexdigest(), BASELINE_SHA256)
        self.assertEqual(baseline.splitlines()[0], FIRST_LINE)
        self.assertIn(b"VALUES (true,'leti-cook-release-01',%d,%d)" % (BASELINE_VERSION, BASELINE_VERSION), baseline)
        self.assertNotIn(b'INSERT INTO operator_private.schema_migrations', baseline)
        self.assertEqual(sorted(path.name for path in (SCHEMA / 'steps').glob('*.sql')),
                         [entry['file'].removeprefix('steps/') for entry in manifest['steps']])
        if not manifest['steps']:
            self.assertEqual(schema, baseline)
        self.assertNotIn(b'Draft', schema)

    def test_manifest_states_only_facts_that_match_the_files(self):
        manifest = json.loads((SCHEMA / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest), {'api_data_rows', 'configuration_seed_counts', 'files', 'format',
                                         'fresh_database_only', 'postgres_major', 'schema_version', 'schemas', 'user_work_rows',
                                         'version', 'baseline_id', 'baseline_version', 'steps'})
        self.assertEqual((manifest['format'], manifest['version'], manifest['postgres_major']),
                         ('leti-cook-self-hosted-schema', 1, 17))
        self.assertEqual(manifest['schemas'], ['public', 'operator_private'])
        self.assertEqual((manifest['baseline_id'], manifest['baseline_version']),
                         ('leti-cook-release-01', BASELINE_VERSION))
        # One numbered file per version after the baseline, in order, with its own checksum.
        names = sorted(path.name for path in (SCHEMA / 'steps').glob('*.sql'))
        self.assertEqual(manifest['schema_version'], BASELINE_VERSION + len(names))
        for version, name in enumerate(names, BASELINE_VERSION + 1):
            self.assertRegex(name, r'\A%04d-[a-z][a-z0-9-]{0,79}\.sql\Z' % version)
        self.assertEqual(manifest['steps'], [
            {'version': version, 'file': 'steps/' + name, 'sha256': digest(SCHEMA / 'steps' / name)}
            for version, name in enumerate(names, BASELINE_VERSION + 1)])
        self.assertIs(manifest['fresh_database_only'], True)
        self.assertEqual((manifest['api_data_rows'], manifest['user_work_rows']), (0, 0))
        self.assertEqual(manifest['files'], {name: digest(SCHEMA / name) for name in
                                             ('schema.sql', 'baseline/schema.sql', 'configuration-seeds.sql',
                                              *('steps/' + name for name in names))})
        schema_digest = manifest['files']['schema.sql']
        self.assertEqual((SCHEMA / 'schema.sql.sha256').read_text(encoding='utf-8').split(),
                         [schema_digest, 'schema.sql'])
        pins = json.loads((SCHEMA / 'function-body-pins.json').read_text(encoding='utf-8'))
        self.assertEqual(pins['schema_sha256'], schema_digest)

    def test_seed_counts_and_reference_copy_match_the_schema(self):
        statements = SEED.findall((SCHEMA / 'schema.sql').read_bytes())
        self.assertEqual(statements, SEED.findall((SCHEMA / 'configuration-seeds.sql').read_bytes()))
        counts = {re.match(rb'INSERT INTO public\.([a-z_]+)', s).group(1).decode(): rows(s) for s in statements}
        self.assertEqual(len(counts), len(statements), 'one seed statement per table')
        manifest = json.loads((SCHEMA / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(counts, manifest['configuration_seed_counts'])

    def test_installation_has_no_upgrade_path(self):
        installer = (ROOT / 'install.py').read_text(encoding='utf-8')
        for retired in ('--upgrade', 'schema/migrations', 'UPGRADE', 'pending_migrations', 'transport'):
            self.assertNotIn(retired, installer)
        for path in ('tools/pending_migrations.py', 'tools/direct_pg_installation_rehearsal.py'):
            self.assertFalse((ROOT / path).exists(), path)
        sys.path.insert(0, str(ROOT / 'application'))
        import config_migrations
        for writer in ('apply_config_upgrade', 'save_config_upgrade_backup', 'read_config_for_upgrade'):
            self.assertFalse(hasattr(config_migrations, writer), writer)


if __name__ == '__main__':
    unittest.main()
