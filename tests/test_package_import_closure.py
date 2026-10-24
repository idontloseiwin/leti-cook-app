"""Static release dependency closure; never imports the backend or providers."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_import_check', ROOT / 'tools/package_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class PackageImportClosureTests(unittest.TestCase):
    def test_actual_application_selection_has_all_static_local_imports(self):
        selection = json.loads((ROOT / 'packaging/files.json').read_text(encoding='utf-8'))
        files = {target: (ROOT / source).read_bytes() for target, source in selection.items()}
        release.validate_local_imports(files)
        del files['application/db/channel/channel_inventory_estimate_normalization.py']
        with self.assertRaisesRegex(ValueError, 'missing_selected_local_import: application/db/channel/channel_inventory_estimate_normalization.py'):
            release.validate_local_imports(files)

    def test_relative_imports_and_package_initializers_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                'application/db/__init__.py': b'',
                'application/db/a.py': b'from . import b\nfrom .nested.c import value\nimport httpx\n',
                'application/db/b.py': b'',
                'application/db/nested/__init__.py': b'',
                'application/db/nested/c.py': b'from ..b import value\n',
            }
            for name, data in files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            release.validate_local_imports(files, root)
            for missing in ('application/db/b.py', 'application/db/nested/__init__.py'):
                reduced = dict(files); reduced.pop(missing)
                with self.assertRaisesRegex(ValueError, 'missing_selected_local_import'):
                    release.validate_local_imports(reduced, root)

    def test_imports_are_parsed_without_executing_top_level_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release.validate_local_imports({'application/example.py': b'raise RuntimeError("must_not_execute")\nimport httpx\n'}, root)


if __name__ == '__main__':
    unittest.main()
