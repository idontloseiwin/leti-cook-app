"""Supported owners stay independent; retired modules cannot return through packaging/imports."""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = ROOT / 'application'
RETIRED_MODULES = {'sync_service', 'collection._previous_long'}
RETIRED_FILES = {'application/sync_service.py', 'application/collection/_previous_long.py'}


def retired_imports(source, package):
    """Resolve absolute and package-relative imports without loading application code."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ''
            if node.level:
                name = importlib.util.resolve_name('.' * node.level + name, package)
            names = [name, *(name + '.' + alias.name for alias in node.names)]
        else:
            continue
        found.update(name for name in names if any(name == retired or name.startswith(retired + '.')
                                                  for retired in RETIRED_MODULES))
    return found


class RetainedOwnerBoundaryTests(unittest.TestCase):
    def test_retired_files_and_package_entries_are_absent(self):
        manifest = json.loads((ROOT / 'packaging/files.json').read_text(encoding='utf-8'))
        for relative in RETIRED_FILES:
            with self.subTest(path=relative):
                self.assertFalse((ROOT / relative).exists())
                self.assertNotIn(relative, manifest)
                self.assertNotIn(relative, manifest.values())

    def test_supported_owners_do_not_import_retired_modules(self):
        for path in APPLICATION.rglob('*.py'):
            relative = path.relative_to(APPLICATION)
            package = '.'.join(relative.parts[:-1])
            with self.subTest(path=relative.as_posix()):
                self.assertFalse(retired_imports(path.read_text(encoding='utf-8'), package))

    def test_import_guard_resolves_absolute_and_relative_forms(self):
        for statement, package, expected in (
            ('import sync_service as old', 'collection', {'sync_service'}),
            ('from sync_service import anything', 'collection', {'sync_service', 'sync_service.anything'}),
            ('import collection._previous_long as old', 'collection', {'collection._previous_long'}),
            ('from collection import _previous_long as old', 'runtime', {'collection._previous_long'}),
            ('from . import _previous_long as old', 'collection', {'collection._previous_long'}),
            ('from ._previous_long import anything', 'collection', {'collection._previous_long', 'collection._previous_long.anything'}),
            ('from .. import _previous_long', 'collection.subpackage', {'collection._previous_long'}),
        ):
            with self.subTest(statement=statement):
                self.assertEqual(retired_imports(statement, package), expected)
        self.assertFalse(retired_imports('from . import core', 'third_party'))
        self.assertFalse(retired_imports('from collection.initial import initial_28d_stats_import', 'runtime'))

    def test_retired_modules_cannot_be_imported(self):
        script = r'''
import importlib, sys
sys.path.insert(0, sys.argv[1])
for name in ('sync_service', 'collection._previous_long'):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as error:
        assert error.name == name, (name, error.name)
    else:
        raise AssertionError('retired module importable: ' + name)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(APPLICATION)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_and_api_import_without_previous_owner_or_bootstrap(self):
        script = r'''
import builtins, importlib, socket, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
sys.modules['collection._previous_long'] = None
sys.modules['sync_service'] = None
original = builtins.__import__
def checked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and name.split('.')[0] in {'core', 'sync_service', 'server', 'dotenv'}:
        raise AssertionError('bootstrap import: ' + name)
    return original(name, globals, locals, fromlist, level)
with patch('builtins.__import__', side_effect=checked_import), \
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network during import')):
    for name in sys.argv[2:]:
        importlib.import_module(name)
'''
        owners = ['collection.lifecycle', 'collection.initial', 'collection.channel_metadata',
                  'collection.cycle', 'collection.api', 'runtime.collection']
        for order in (owners, list(reversed(owners))):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(APPLICATION), *order],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_retained_functions_and_constants_have_their_supported_owner(self):
        sys.path.insert(0, str(APPLICATION))
        from collection import channel_metadata, cycle, initial, lifecycle
        owners = {
            lifecycle: ('update_channel_values', 'update_channel_lifecycle_status'),
            initial: ('load_recent_long_inventory_rows', 'initial_28d_stats_import'),
            channel_metadata: ('get_long_video_count', 'has_completed_initial_long_inventory',
                               'int_or_none', 'stored_channel_error_code', 'safe_initial_inventory_error_code',
                               'clear_initial_inventory_failure'),
            cycle: ('require_channel_discovery_enabled',),
        }
        for owner, names in owners.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(getattr(owner, name).__module__, owner.__name__)
        self.assertEqual((initial.INITIAL_28D_STATS_DAYS, initial.INITIAL_28D_STATS_MAX_VIDEOS), (28, 150))
        self.assertEqual(channel_metadata.INITIAL_LONG_INVENTORY_AUTO_LIMIT, 2500)
        self.assertTrue(channel_metadata.STORED_ERROR_CODE_RE.fullmatch('youtube_quota_exhausted'))
        self.assertIsNone(channel_metadata.STORED_ERROR_CODE_RE.fullmatch('provider text'))


if __name__ == '__main__': unittest.main()
