"""Publication gates and least-privilege CI; no GitHub or registry writes."""
from pathlib import Path
import json
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.release_manifest import ReleaseError
from tools import release_workflow as workflow
from tools.package_extension import package_extension


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'application').mkdir()
        (self.root / 'application/VERSION').write_bytes(b'0.1.0\n')
        (self.root / 'application/release-source.json').write_text(json.dumps({
            'format': 'leti-cook-release-source', 'format_version': 1,
            'owner': 'publisher', 'repository': 'leti-cook'}))
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo)
        (self.root / 'application/release-signing.pub').write_bytes(public)
        self.identity = {'ref': 'refs/tags/v0.1.0', 'repository': 'publisher/leti-cook', 'commit': 'a' * 40}
        shutil.copytree(ROOT / 'extensions/chromium', self.root / 'extensions/chromium')
        self.extension_assets = package_extension(self.root, root=self.root)

    def test_only_matching_tag_repository_checkout_and_pinned_key_can_release(self):
        run = Mock(return_value='a' * 40)
        self.assertEqual(workflow.preflight(self.root, **self.identity, run=run)['tag'], 'v0.1.0')
        for name, value in (('ref', 'refs/heads/main'), ('ref', 'refs/tags/v0.2.0'),
                            ('repository', 'other/leti-cook'), ('commit', 'main')):
            with self.subTest(name=name, value=value), self.assertRaises(ReleaseError):
                workflow.preflight(self.root, **{**self.identity, name: value}, run=run)
        with self.assertRaisesRegex(ReleaseError, 'checkout_mismatch'):
            workflow.preflight(self.root, **self.identity, run=Mock(return_value='b' * 40))
        (self.root / 'application/release-signing.pub').write_bytes(b'not a key')
        with self.assertRaises(ReleaseError):
            workflow.preflight(self.root, **self.identity, run=run)

    def test_placeholder_repository_cannot_publish(self):
        path = self.root / 'application/release-source.json'
        data = json.loads(path.read_bytes())
        data['owner'] = 'OWNER'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ReleaseError, 'source_unconfigured'):
            workflow.preflight(self.root, **self.identity, run=Mock(return_value='a' * 40))

    def test_signing_key_is_pinned_before_sign_and_temp_file_removed_on_failure(self):
        raw = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode('ascii')
        seen = []
        def fail(directory, key, **kwargs):
            seen.append(Path(key))
            self.assertEqual(Path(key).read_text(), raw)
            self.assertNotEqual(Path(key).parent, self.root)
            raise ReleaseError('release_test_failure')
        with patch.object(workflow, 'verify_checkout_artifacts'), \
                patch('tools.sign_release.sign_release', side_effect=fail), \
                self.assertRaisesRegex(ReleaseError, 'release_test_failure'):
            workflow.sign_from_environment(self.root, self.root, environ={'LETI_COOK_RELEASE_SIGNING_KEY': raw})
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].exists())
        other = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode('ascii')
        with patch('tools.sign_release.sign_release') as sign, self.assertRaisesRegex(ReleaseError, 'key_mismatch'):
            workflow.sign_from_environment(self.root, self.root, environ={'LETI_COOK_RELEASE_SIGNING_KEY': other})
        sign.assert_not_called()

    def test_missing_private_secret_refuses_without_artifacts(self):
        before = set(self.root.rglob('*'))
        with self.assertRaisesRegex(ReleaseError, 'secret_missing'):
            workflow.sign_from_environment(self.root, self.root, environ={})
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_remote_tag_change_blocks_draft_and_no_publish_fallback_exists(self):
        (self.root / 'install-release.py').write_bytes(b'# verified bootstrap fixture\n')
        verified = Mock(manifest={'version': '0.1.0'})
        calls = []
        def run(args):
            calls.append(args)
            return 'a' * 40 if args[0] == 'git' else 'b' * 40
        with patch.object(workflow, 'verify_release', return_value=verified), \
                patch.object(workflow, 'bootstrap_bytes', return_value=b'# verified bootstrap fixture\n'), \
                self.assertRaisesRegex(ReleaseError, 'remote_tag_changed'):
            workflow.create_draft(self.root, self.root, **self.identity, run=run)
        self.assertFalse(any(args[0:3] == ['gh', 'release', 'create'] for args in calls))

    def test_draft_keeps_owner_publish_step_and_exact_assets(self):
        (self.root / 'install-release.py').write_bytes(b'# verified bootstrap fixture\n')
        calls = []
        def run(args):
            calls.append(args)
            return 'a' * 40
        with patch.object(workflow, 'verify_release', return_value=Mock(manifest={'version': '0.1.0'})), \
                patch.object(workflow, 'bootstrap_bytes', return_value=b'# verified bootstrap fixture\n'):
            workflow.create_draft(self.root, self.root, **self.identity, run=run)
        create = calls[-1]
        self.assertEqual(create[:4], ['gh', 'release', 'create', 'v0.1.0'])
        self.assertIn('--draft', create)
        self.assertIn('--verify-tag', create)
        self.assertNotIn('--clobber', create)
        self.assertEqual([Path(value).name for value in create[-8:]],
            ['leti-cook-0.1.0.tar.gz', 'release.json', 'SHA256SUMS', 'SHA256SUMS.sig', 'install-release.py']
            + self.extension_assets)

    def test_changed_extension_blocks_draft_before_github_calls(self):
        (self.root / 'install-release.py').write_bytes(b'trusted installer')
        (self.root / self.extension_assets[0]).write_bytes(b'changed extension')
        run = Mock(return_value='a' * 40)
        with patch.object(workflow, 'verify_release', return_value=Mock(manifest={'version': '0.1.0'})), \
                patch.object(workflow, 'bootstrap_bytes', return_value=b'trusted installer'), \
                self.assertRaisesRegex(ReleaseError, 'extension_mismatch'):
            workflow.create_draft(self.root, self.root, **self.identity, run=run)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][0], 'git')

    def test_changed_bootstrap_blocks_draft_before_github_calls(self):
        (self.root / 'install-release.py').write_bytes(b'changed installer')
        run = Mock(return_value='a' * 40)
        with patch.object(workflow, 'verify_release', return_value=Mock(manifest={'version': '0.1.0'})), \
                patch.object(workflow, 'bootstrap_bytes', return_value=b'trusted installer'), \
                self.assertRaisesRegex(ReleaseError, 'bootstrap_mismatch'):
            workflow.create_draft(self.root, self.root, **self.identity, run=run)
        self.assertEqual(len(run.call_args_list), 1)
        self.assertEqual(run.call_args.args[0][0], 'git')

    def test_changed_checkout_artifacts_block_sign_before_private_file_creation(self):
        raw = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()).decode('ascii')
        with patch.object(workflow, 'verify_checkout_artifacts', side_effect=ReleaseError('release_workflow_source_mismatch')), \
                patch('tools.sign_release.sign_release') as sign, \
                patch.object(workflow.tempfile, 'TemporaryDirectory') as temporary, \
                self.assertRaisesRegex(ReleaseError, 'source_mismatch'):
            workflow.sign_from_environment(self.root, self.root, environ={'LETI_COOK_RELEASE_SIGNING_KEY': raw})
        sign.assert_not_called()
        temporary.assert_not_called()

    def test_checkout_rebuild_accepts_exact_bundle_and_rejects_changed_archive(self):
        from tools import package_release
        from tools.release_archive import _write_archive, canonical_json
        files = {'application/VERSION': b'0.1.0\n', 'file.txt': b'trusted content'}
        manifest = {'files': {'file.txt': 'fixture'}}
        archive = self.root / 'leti-cook-0.1.0.tar.gz'
        _write_archive({**files, 'bundle.json': canonical_json(manifest)}, archive)
        (self.root / 'install-release.py').write_bytes(b'trusted bootstrap')
        with patch.object(package_release, 'package') as package, \
                patch('install.verify_bundle', return_value=(manifest, files)), \
                patch.object(workflow, 'bootstrap_bytes', return_value=b'trusted bootstrap'):
            workflow.verify_checkout_artifacts(self.root, self.root)
            self.assertEqual(package.call_args.kwargs, {'root': self.root})
            data = archive.read_bytes()
            archive.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
            with self.assertRaisesRegex(ReleaseError, 'source_mismatch'):
                workflow.verify_checkout_artifacts(self.root, self.root)


class WorkflowBoundaryTests(unittest.TestCase):
    def test_ci_secret_write_permissions_and_publication_are_tag_job_only(self):
        text = (ROOT / '.github/workflows/release.yml').read_text()
        before, publish = text.split('\n  publish:\n', 1)
        self.assertNotIn('secrets.', before)
        self.assertNotIn('contents: write', before)
        self.assertNotIn('packages: write', before)
        self.assertIn("github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')", publish)
        self.assertIn('needs: prepare', publish)
        self.assertIn('digest-mismatch: error', publish)
        self.assertEqual(text.count('secrets.LETI_COOK_RELEASE_SIGNING_KEY'), 1)
        self.assertLess(publish.index('release_workflow.py sign'), publish.index('release_ci.py publish-images'))
        self.assertLess(publish.index('release_ci.py publish-images'), publish.index('release_workflow.py draft'))
        self.assertNotIn('pull_request_target', text)
        self.assertIn('cancel-in-progress: false', text)
        for action in re.findall(r'uses: ([^\s]+)', text):
            if not action.startswith('./'):
                self.assertRegex(action, r'^[^@]+@[a-f0-9]{40}$')

    def test_tag_checks_reuse_all_existing_checks(self):
        text = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertIn('uses: ./.github/workflows/package.yml', text)
        self.assertIn('uses: ./.github/workflows/audit.yml', text)
        for name in ('package.yml', 'audit.yml'):
            self.assertIn('workflow_call:', (ROOT / '.github/workflows' / name).read_text())
        self.assertIn('--preserve-digests', (ROOT / 'tools/release_ci.py').read_text())


if __name__ == '__main__':
    unittest.main()
