"""The real source package fits, while the finite shared file limit refuses excess."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install
from ops.bundle_contract import MAX_FILES
from tools.package_release import package


class BundleFileLimitTests(unittest.TestCase):
    def test_actual_package_and_exact_file_limit_then_one_extra(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'bundle'
            packaged = package(root)
            manifest, files = install.verify_bundle(root)
            self.assertEqual(len(files), packaged['files'])
            for index in range(MAX_FILES - len(files)):
                name = f'limit-fixture-{index:04d}.txt'
                (root / name).write_bytes(b'fixture\n')
                manifest['files'][name] = hashlib.sha256(b'fixture\n').hexdigest()
            (root / 'bundle.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.assertEqual(len(install.verify_bundle(root)[1]), MAX_FILES)
            # Reject the oversized manifest before attempting to read this file.
            manifest['files']['one-too-many.txt'] = hashlib.sha256(b'fixture\n').hexdigest()
            (root / 'bundle.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(install.InstallError, '^invalid_bundle_files$'):
                install.verify_bundle(root)


if __name__ == '__main__':
    unittest.main()
