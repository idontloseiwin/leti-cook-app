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

    def test_it_runs_weekly_and_when_a_pin_changes(self):
        self.assertIn("cron: '41 6 * * 1'", WORKFLOW)
        push = WORKFLOW.split('\n  push:\n', 1)[1].split('\n  pull_request:', 1)[0]
        for path in ('database/compose.yaml', 'database/Dockerfile', 'tools/schema_ci_postgres_image.sh', 'application/Dockerfile', 'application/requirements.lock'):
            self.assertIn(f'      - {path}\n', push + '\n', path)
        self.assertIn('permissions:\n  contents: read\n', WORKFLOW)

    def test_required_scan_runs_for_every_pull_request(self):
        # A workflow-level path filter leaves required checks pending on docs-only PRs.
        pull_request = WORKFLOW.split('\n  pull_request:\n', 1)[1].split('  schedule:\n', 1)[0]
        self.assertEqual(pull_request.strip(), '')
        self.assertNotIn('pull_request_target', WORKFLOW)
        jobs = WORKFLOW.split('\njobs:\n', 1)[1]
        self.assertNotRegex(jobs, r'(?m)^    if:')


if __name__ == '__main__':
    unittest.main()
