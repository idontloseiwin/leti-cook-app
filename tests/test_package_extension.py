"""Offline checks of the extension release bytes and their provenance."""
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.package_extension import FILES, extension_artifacts, package_extension, verify_extension_artifacts


class ExtensionPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'extensions/chromium'
        shutil.copytree(ROOT / 'extensions/chromium', self.source)

    def test_exact_selection_reproducible_and_source_bytes_preserved(self):
        (self.source / 'private-local.txt').write_text('not a release input')
        assets = extension_artifacts(self.root)
        self.assertEqual(assets, extension_artifacts(self.root))
        name = next(name for name in assets if name.endswith('.zip'))
        with zipfile.ZipFile(io.BytesIO(assets[name])) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {'leti-cook-chromium/' + name for name in FILES})
            for item in FILES:
                self.assertEqual(archive.read('leti-cook-chromium/' + item), (self.source / item).read_bytes())
        self.assertEqual(set(json.loads(assets['extension-files.json'])['files']), set(FILES))

    def test_tampered_asset_and_changed_source_refused(self):
        output = self.root / 'out'
        names = package_extension(output, root=self.root)
        self.assertEqual(verify_extension_artifacts(self.root, output), names)
        archive = output / names[0]
        original = archive.read_bytes()
        archive.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, 'extension_mismatch'):
            verify_extension_artifacts(self.root, output)
        archive.write_bytes(original)
        with (self.source / 'content.js').open('ab') as stream:
            stream.write(b'\n// different source\n')
        with self.assertRaisesRegex(ValueError, 'extension_mismatch'):
            verify_extension_artifacts(self.root, output)

    def test_missing_file_or_existing_output_is_not_silently_accepted(self):
        output = self.root / 'out'
        package_extension(output, root=self.root)
        with self.assertRaisesRegex(ValueError, 'extension_output_exists'):
            package_extension(output, root=self.root)
        (self.source / 'bridge.js').unlink()
        with self.assertRaises(ValueError):
            extension_artifacts(self.root)


if __name__ == '__main__':
    unittest.main()
