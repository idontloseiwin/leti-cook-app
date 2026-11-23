"""Real standalone bootstrap verification with ephemeral trust and no network."""
import ast
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops.release_manifest import ReleaseError
from tools import build_release_bootstrap as builder
import test_release_verification as release_fixture


class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        release_fixture.ReleaseVerificationTests.setUpClass()

    def setUp(self):
        self.fixture = release_fixture.ReleaseVerificationTests('test_real_ed25519_verification_returns_immutable_captured_bytes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source = self.fixture.root / 'bootstrap-source'
        self.source.mkdir()
        for name in builder.BOOTSTRAP_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name == 'application/release-source.json':
                target.write_bytes(release_fixture.source_bytes())
            elif name == 'application/release-signing.pub':
                target.write_bytes(self.fixture.public_file.read_bytes())
            else:
                target.write_bytes((ROOT / name).read_bytes())
        self.output = self.fixture.root / builder.BOOTSTRAP_NAME

    def execute(self, *args):
        environment = dict(os.environ)
        environment['PATH'] = str(Path(self.fixture.openssl).parent) + os.pathsep + environment.get('PATH', '')
        return subprocess.run([sys.executable, '-B', str(self.output), *args],
                              capture_output=True, text=True, cwd=self.fixture.root,
                              env=environment, timeout=30)

    def test_exact_trusted_source_key_modules_are_embedded_and_build_is_deterministic(self):
        first = builder.bootstrap_bytes(self.source)
        self.assertEqual(first, builder.bootstrap_bytes(self.source))
        assignments = {item.targets[0].id: ast.literal_eval(item.value)
                       for item in ast.parse(first).body
                       if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name)
                       and item.targets[0].id in {'_PAYLOAD', '_LENGTH', '_SHA256'}}
        raw = zlib.decompress(base64.b85decode(assignments['_PAYLOAD']))
        values = {name: base64.b64decode(value) for name, value in json.loads(raw).items()}
        self.assertEqual(values, dict(builder.bootstrap_files(self.source)))
        self.assertIn('ops/release_images.py', values)
        for name in builder.BOOTSTRAP_FILES:
            self.assertEqual(values[name], (self.source / name).read_bytes(), name)
        self.assertLess(len(first), 150000)
        # A future host import must be deliberately added to the small closure.
        for name, data in values.items():
            if not name.endswith('.py'):
                continue
            for node in ast.walk(ast.parse(data)):
                modules = [node.module] if isinstance(node, ast.ImportFrom) else (
                    [item.name for item in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    if module and module.startswith(('application.', 'ops.')):
                        self.assertIn(module.replace('.', '/') + '.py', values, (name, module))

    def test_production_missing_key_and_placeholder_source_fail_before_output(self):
        key = self.source / 'application/release-signing.pub'
        key.unlink()
        with self.assertRaisesRegex(ReleaseError, 'release_input_unavailable'):
            builder.build_bootstrap(self.source, self.output)
        self.assertFalse(self.output.exists())
        key.write_bytes(self.fixture.public_file.read_bytes())
        (self.source / 'application/release-source.json').write_bytes(release_fixture.source_bytes(owner='OWNER'))
        with self.assertRaisesRegex(ReleaseError, 'release_source_unconfigured'):
            builder.build_bootstrap(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_private_or_invalid_key_is_not_embedded(self):
        (self.source / 'application/release-signing.pub').write_bytes(self.fixture.key_file.read_bytes())
        with self.assertRaisesRegex(ReleaseError, 'release_public_key_invalid'):
            builder.build_bootstrap(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_no_output_overwrite_and_only_the_canonical_bootstrap_name(self):
        builder.build_bootstrap(self.source, self.output)
        with self.assertRaises(FileExistsError):
            builder.build_bootstrap(self.source, self.output)
        with self.assertRaisesRegex(ReleaseError, 'release_bootstrap_filename_invalid'):
            builder.build_bootstrap(self.source, self.output.with_name('another.py'))

    def test_standalone_executes_real_signature_verifier_without_candidate_code(self):
        builder.build_bootstrap(self.source, self.output)
        # The package deliberately contains a throwing install.py; trusted
        # bootstrap modules must take precedence over these captured files.
        result = self.execute('--check', '--package', str(self.fixture.archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'status': 'verified', 'version': '0.1.0',
                                                    'source': 'release', 'changes': False})

    def test_standalone_rejects_tampered_signature_and_candidate_key_change(self):
        builder.build_bootstrap(self.source, self.output)
        signature = self.fixture.directory / 'SHA256SUMS.sig'
        signature.write_bytes(bytes(64))
        result = self.execute('--check', '--package', str(self.fixture.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('release_signature_invalid', result.stderr)
        self.fixture.files['application/release-signing.pub'] = b'candidate supplied replacement'
        self.fixture.refresh_bundle()
        self.fixture.write_assets()
        result = self.execute('--check', '--package', str(self.fixture.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('release_signing_key_changed', result.stderr)

    def test_unrelated_modules_in_bootstrap_folder_do_not_replace_trusted_modules(self):
        builder.build_bootstrap(self.source, self.output)
        (self.fixture.root / 'install.py').write_text("raise RuntimeError('untrusted local install')\n", encoding='utf-8')
        (self.fixture.root / 'ops').mkdir()
        (self.fixture.root / 'ops/__init__.py').write_text("raise RuntimeError('untrusted local package')\n", encoding='utf-8')
        result = self.execute('--check', '--package', str(self.fixture.archive))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_host_sources_parse_as_python_310(self):
        for name, data in builder.bootstrap_files(self.source).items():
            if name.endswith('.py'):
                ast.parse(data, filename=name, feature_version=(3, 10))
        ast.parse(builder.bootstrap_bytes(self.source), feature_version=(3, 10))


if __name__ == '__main__':
    unittest.main()
