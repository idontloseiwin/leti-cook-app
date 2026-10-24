"""Every shipped image is pinned by digest and scanned by a pinned scanner (audit S2-7). Source only."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/images.yml').read_text(encoding='utf-8')
COMPOSE = (ROOT / 'database/compose.yaml').read_text(encoding='utf-8')
DIGEST = r'@sha256:[0-9a-f]{64}'


class ImageScanTests(unittest.TestCase):
    def test_every_base_image_is_pinned_and_database_is_built(self):
        self.assertNotRegex(COMPOSE, r'(?m)^\s+image:')
        self.assertIn('build:', COMPOSE)
        for relative in ('application/Dockerfile', 'database/Dockerfile'):
            dockerfile = (ROOT / relative).read_text(encoding='utf-8')
            self.assertRegex(dockerfile.splitlines()[0], r'^FROM \S+' + DIGEST + '$')
        database = (ROOT / 'database/Dockerfile').read_text(encoding='utf-8')
        self.assertRegex(database.splitlines()[0], r'^FROM postgres:17\.')

    def test_the_scanner_is_pinned_and_fails_on_fixable_findings(self):
        self.assertRegex(WORKFLOW, r'TRIVY_IMAGE: aquasec/trivy:\d+\.\d+\.\d+' + DIGEST + r'\n')
        for option in ('--severity HIGH,CRITICAL', '--ignore-unfixed', '--exit-code 1', '--scanners vuln'):
            self.assertIn(option, WORKFLOW)
        self.assertIn('docker build --tag leti-cook-backend:scan application', WORKFLOW)
        self.assertIn('schema_ci_postgres_build "$PWD"', WORKFLOW)
        self.assertIn('schema_ci_postgres_require "$PWD"', WORKFLOW)
        self.assertIn('POSTGRES_IMAGE: ${{ steps.postgres.outputs.image }}', WORKFLOW)
        self.assertIn('for image in "$POSTGRES_IMAGE" leti-cook-backend:scan;', WORKFLOW)
        self.assertNotIn('report_gosu_symbols', WORKFLOW)
        for exclusion in ('--skip-files', '--skip-dirs', '--ignorefile', '--ignore-policy'):
            self.assertNotIn(exclusion, WORKFLOW)

    def test_scheduled_scan_and_reusable_ci_entry_remain_available(self):
        self.assertIn("cron: '41 6 * * 1'", WORKFLOW)
        self.assertIn('  workflow_call:', WORKFLOW)
        self.assertIn('  workflow_dispatch:', WORKFLOW)
        self.assertIn('permissions:\n  contents: read\n', WORKFLOW)
        self.assertNotIn('pull_request_target', WORKFLOW)
        coordinator = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn('uses: ./.github/workflows/images.yml', coordinator)
        self.assertIn("if: needs.scope.outputs.profile == 'full'", coordinator)


if __name__ == '__main__':
    unittest.main()
