"""CI must distinguish prose from code and fail closed on incomplete results."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import ci_policy as policy


def record(path, old='100644', new='100644', status='M'):
    return f':{old} {new} aaaaaaa bbbbbbb {status}\0{path}\0'.encode()


class ClassificationTests(unittest.TestCase):
    def test_allowlisted_prose_and_image_edits_are_short(self):
        self.assertEqual(policy.classify_diff(b''.join(record(p) for p in policy.DOC_FILES)), 'docs')

    def test_docs_deletion_is_short_but_still_requires_link_and_bundle_checks(self):
        self.assertEqual(policy.classify_diff(record('docs/DATA.md', new='000000', status='D')), 'docs')

    def test_unknown_mixed_generated_and_executable_files_require_full(self):
        for path in ('docs/new.md', 'docs/SQL_CATALOG.md', 'application/server.py',
                     '.github/workflows/ci.yml', 'application/requirements.lock',
                     'packaging/files.json', 'schema/schema.sql', 'LICENSE',
                     'README.md\napplication/server.py', '../README.md', 'Readme.md'):
            with self.subTest(path=path):
                self.assertEqual(policy.classify_diff(record('README.md') + record(path)), 'full')
        for old, new, status in [('100644', '100755', 'M'), ('100644', '120000', 'T'),
                                 ('160000', '160000', 'M'), ('100644', '100644', 'U')]:
            self.assertEqual(policy.classify_diff(record('README.md', old, new, status)), 'full')

    def test_empty_truncated_or_unknown_diff_cannot_skip_tests(self):
        for raw in (b'', record('README.md')[:-1], b'junk\0README.md\0',
                    record('README.md') + b'incomplete\0',
                    record('README.md').replace(b' M\0', b' R100\0')):
            self.assertEqual(policy.classify_diff(raw), 'full')

    def test_many_docs_do_not_hide_later_code(self):
        self.assertEqual(policy.classify_diff(record('README.md') * 501 + record('manage.py')), 'full')

    def test_manual_missing_base_and_git_errors_use_full(self):
        self.assertEqual(policy.select_profile('workflow_dispatch', {})[0], 'full')
        self.assertEqual(policy.select_profile('push', {'before': '0' * 40, 'after': 'a' * 40})[0], 'full')
        self.assertEqual(policy.select_profile('pull_request', {})[0], 'full')
        event = {'before': 'a' * 40, 'after': 'b' * 40}
        with patch.object(policy, 'git', side_effect=subprocess.CalledProcessError(1, ['git'])):
            self.assertEqual(policy.select_profile('push', event)[0], 'full')


class RealGitTests(unittest.TestCase):
    def test_pr_merge_base_push_range_and_code_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return policy.git(*args, root=root).decode().strip()
            def commit(message):
                git('add', '-A')
                git('-c', 'user.name=CI test', '-c', 'user.email=ci@example.invalid',
                    'commit', '-qm', message)
                return git('rev-parse', 'HEAD')
            git('init', '-q')
            (root / 'README.md').write_text('# Readme\n')
            (root / 'code.py').write_text('pass\n')
            base = commit('base')
            (root / 'README.md').write_text('# Changed\n')
            head = commit('documentation')
            pr = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}
            self.assertEqual(policy.select_profile('pull_request', pr, root=root)[0], 'docs')
            self.assertEqual(policy.select_profile('push', {'before': base, 'after': head}, root=root)[0], 'docs')
            # Base branch advances independently: classify only the PR changes.
            git('checkout', '-q', '--detach', base)
            (root / 'code.py').write_text('print(1)\n')
            advanced = commit('base advances')
            pr['pull_request']['base']['sha'] = advanced
            self.assertEqual(policy.select_profile('pull_request', pr, root=root)[0], 'docs')
            git('checkout', '-q', '--detach', head)
            (root / 'docs').mkdir()
            git('mv', 'code.py', 'docs/DATA.md')
            renamed = commit('code renamed into docs')
            self.assertEqual(policy.select_profile('push', {'before': head, 'after': renamed}, root=root)[0], 'full')


class ResultTests(unittest.TestCase):
    def needs(self, profile):
        return {'scope': {'result': 'success', 'outputs': {'profile': profile}},
                **{key: {'result': 'success' if profile == 'full' else 'skipped'}
                   for key in policy.FULL_JOBS}}

    def test_expected_docs_and_full_graphs_pass(self):
        for profile in ('docs', 'full'):
            self.assertEqual(policy.verify_results(self.needs(profile)), profile)

    def test_all_failure_missing_cancelled_and_unexpected_skip_paths_block(self):
        for profile in ('docs', 'full'):
            for job in ('scope', *policy.FULL_JOBS):
                for status in ('failure', 'cancelled', 'skipped', 'success', None):
                    needs = self.needs(profile)
                    if needs[job]['result'] == status:
                        continue
                    needs[job]['result'] = status
                    with self.subTest(profile=profile, job=job, status=status):
                        with self.assertRaises(ValueError):
                            policy.verify_results(needs)
                needs = self.needs(profile)
                del needs[job]
                with self.assertRaises(ValueError):
                    policy.verify_results(needs)

    def test_missing_profile_and_unknown_jobs_block(self):
        for value in ('', 'unexpected', None):
            needs = self.needs(value)
            with self.assertRaises(ValueError):
                policy.verify_results(needs)
        needs = self.needs('docs')
        needs['extra'] = {'result': 'success'}
        with self.assertRaises(ValueError):
            policy.verify_results(needs)


class WorkflowRoutingTests(unittest.TestCase):
    def text(self, name):
        return (ROOT / '.github/workflows' / name).read_text(encoding='utf-8')

    def test_only_coordinator_receives_pr_and_main_push(self):
        ci = self.text('ci.yml')
        trigger = ci.split('\npermissions:', 1)[0]
        self.assertIn('\n  pull_request:\n', trigger)
        self.assertIn('branches: [main]', trigger)
        self.assertNotRegex(trigger, r'paths(?:-ignore)?:')
        for name in ('package.yml', 'audit.yml', 'images.yml', 'release-check.yml', 'release.yml'):
            text = self.text(name)
            self.assertNotIn('\n  pull_request:', text, name)
            self.assertNotIn('branches: [main]', text, name)
            if name != 'release.yml':
                self.assertIn('workflow_call:', text)
        self.assertIn("tags: ['v*']", self.text('release.yml'))

    def test_required_result_cannot_be_skipped_and_has_complete_dependencies(self):
        text = self.text('ci.yml')
        self.assertIn('name: CI result\n    if: always()', text)
        self.assertIn('needs: [scope, application, dependencies, images, delivery]', text)
        self.assertIn('CI_NEEDS: ${{ toJSON(needs) }}', text)
        self.assertEqual(text.count("if: needs.scope.outputs.profile == 'full'"), 4)
        self.assertIn('fetch-depth: 0', text)
        self.assertIn('run: python -B tools/check_docs.py', text)
        self.assertIn("if: steps.scope.outputs.profile == 'docs'", text)
        self.assertIn('tools/package_release.py --output', text)

    def test_security_schedule_and_delivery_pins_survive_routing(self):
        self.assertIn("cron: '17 6 * * 1'", self.text('audit.yml'))
        self.assertIn("cron: '41 6 * * 1'", self.text('images.yml'))
        dry = self.text('release-check.yml')
        signed = self.text('release.yml')
        self.assertEqual(dry.split('\nenv:\n', 1)[1].split('\njobs:', 1)[0],
                         signed.split('\nenv:\n', 1)[1].split('\njobs:', 1)[0])
        for required in ('tools/release_ci.py dry-run', 'tools/package_extension.py',
                         '--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1',
                         'if: always()'):
            self.assertIn(required, dry)

    def test_pr_graph_has_read_only_permissions_no_publication_or_secret_inheritance(self):
        for name in ('ci.yml', 'package.yml', 'audit.yml', 'images.yml', 'release-check.yml'):
            text = self.text(name)
            for forbidden in ('contents: write', 'packages: write', 'secrets:', 'secrets.',
                              'pull_request_target', 'release_workflow.py sign', 'release_workflow.py draft'):
                self.assertNotIn(forbidden, text, name)
            self.assertIn('cancel-in-progress: true', text, name)
            for action in re.findall(r'uses: ([^\s]+)', text):
                if not action.startswith('./'):
                    self.assertRegex(action, r'^[^@]+@[a-f0-9]{40}$')


if __name__ == '__main__':
    unittest.main()
