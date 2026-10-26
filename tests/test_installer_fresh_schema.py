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
        self.assertEqual(schema.splitlines()[0], FIRST_LINE)
        self.assertIn(b'CREATE TABLE operator_private.schema_migrations', schema)
        self.assertNotIn(b'INSERT INTO operator_private.schema_migrations', schema)
        self.assertIn(b"VALUES (true,'leti-cook-release-01',37,37)", schema)
        self.assertEqual(digest(SCHEMA / 'baseline/schema.sql'),
                         'c349be588ffd48cf170095755aeb96145e4a9371ecf559cbf4566145a0f4c8ec')
        self.assertEqual(sorted(path.name for path in (SCHEMA / 'steps').glob('*.sql')),
                         ['0026-release-readiness.sql', '0027-data-source-provenance.sql', '0028-unified-video-availability.sql', '0029-automatic-long-short-policy.sql', '0030-automatic-type-reader-permissions.sql', '0031-publication-lifecycle.sql', '0032-automatic-type-read-plan.sql', '0033-health-queue-projections.sql', '0034-all-type-queue-health.sql', '0035-research-read-model.sql', '0036-inventory-receipt-scan-index.sql', '0037-type-resolution-membership-index.sql'])
        self.assertNotIn(b'Draft', schema)

    def test_manifest_states_only_facts_that_match_the_files(self):
        manifest = json.loads((SCHEMA / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest), {'api_data_rows', 'configuration_seed_counts', 'files', 'format',
                                         'fresh_database_only', 'postgres_major', 'schema_version', 'schemas', 'user_work_rows',
                                         'version', 'baseline_id', 'baseline_version', 'steps'})
        self.assertEqual((manifest['format'], manifest['version'], manifest['postgres_major']),
                         ('leti-cook-self-hosted-schema', 1, 17))
        self.assertEqual(manifest['schemas'], ['public', 'operator_private'])
        self.assertEqual(manifest['schema_version'], 37)
        self.assertEqual((manifest['baseline_id'], manifest['baseline_version']),
                         ('leti-cook-release-01', 25))
        self.assertEqual(manifest['steps'], [{'version': 26, 'file': 'steps/0026-release-readiness.sql',
                                             'sha256': digest(SCHEMA / 'steps/0026-release-readiness.sql')},
                                            {'version': 27, 'file': 'steps/0027-data-source-provenance.sql',
                                             'sha256': digest(SCHEMA / 'steps/0027-data-source-provenance.sql')},
                                            {'version': 28, 'file': 'steps/0028-unified-video-availability.sql',
                                             'sha256': digest(SCHEMA / 'steps/0028-unified-video-availability.sql')},
                                            {'version': 29, 'file': 'steps/0029-automatic-long-short-policy.sql',
                                             'sha256': digest(SCHEMA / 'steps/0029-automatic-long-short-policy.sql')},
                                            {'version': 30, 'file': 'steps/0030-automatic-type-reader-permissions.sql',
                                             'sha256': digest(SCHEMA / 'steps/0030-automatic-type-reader-permissions.sql')},
                                            {'version': 31, 'file': 'steps/0031-publication-lifecycle.sql',
                                             'sha256': digest(SCHEMA / 'steps/0031-publication-lifecycle.sql')},
                                            {'version': 32, 'file': 'steps/0032-automatic-type-read-plan.sql',
                                             'sha256': digest(SCHEMA / 'steps/0032-automatic-type-read-plan.sql')},
                                            {'version': 33, 'file': 'steps/0033-health-queue-projections.sql',
                                             'sha256': digest(SCHEMA / 'steps/0033-health-queue-projections.sql')},
                                            {'version': 34, 'file': 'steps/0034-all-type-queue-health.sql',
                                             'sha256': digest(SCHEMA / 'steps/0034-all-type-queue-health.sql')},
                                            {'version': 35, 'file': 'steps/0035-research-read-model.sql',
                                             'sha256': digest(SCHEMA / 'steps/0035-research-read-model.sql')},
                                            {'version': 36, 'file': 'steps/0036-inventory-receipt-scan-index.sql',
                                             'sha256': digest(SCHEMA / 'steps/0036-inventory-receipt-scan-index.sql')},
                                            {'version': 37, 'file': 'steps/0037-type-resolution-membership-index.sql',
                                             'sha256': digest(SCHEMA / 'steps/0037-type-resolution-membership-index.sql')}])
        self.assertIs(manifest['fresh_database_only'], True)
        self.assertEqual((manifest['api_data_rows'], manifest['user_work_rows']), (0, 0))
        self.assertEqual(manifest['files'], {name: digest(SCHEMA / name) for name in
                                             ('schema.sql', 'baseline/schema.sql', 'configuration-seeds.sql',
                                              'steps/0026-release-readiness.sql', 'steps/0027-data-source-provenance.sql', 'steps/0028-unified-video-availability.sql', 'steps/0029-automatic-long-short-policy.sql', 'steps/0030-automatic-type-reader-permissions.sql', 'steps/0031-publication-lifecycle.sql', 'steps/0032-automatic-type-read-plan.sql', 'steps/0033-health-queue-projections.sql', 'steps/0034-all-type-queue-health.sql', 'steps/0035-research-read-model.sql', 'steps/0036-inventory-receipt-scan-index.sql', 'steps/0037-type-resolution-membership-index.sql')})
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
