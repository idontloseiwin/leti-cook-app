"""Focused uninstall persistence checks; real Linux cleanup has a separate probe."""
from __future__ import annotations
from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops import uninstall_store as removal
from ops import update_copy_store as store
from ops import update_journal as journal

PROJECT = 'leti-cook-0123456789ab'
DATABASE = '9a2fc4bc-9d4a-4bc4-b4be-69c7d37f2d23'
REAL_OWNER_MODE = store._owner_mode


def identity(path):
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError('not_directory')
    return {'device': info.st_dev, 'inode': info.st_ino}


def inventory(installation):
    return {'docker': {'format': 'leti-cook-uninstall-resources', 'version': 1, 'project': PROJECT,
                      'installation': str(installation), 'operation_id': None,
                      'docker_host': 'unix:///var/run/docker.sock', 'containers': [], 'volumes': [], 'networks': [], 'images': []},
            'port': 8100, 'update_operation_id': None}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='leti-uninstall-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'installation'
        self.path.mkdir()
        (self.path / 'owned.txt').write_bytes(b'own')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def ancestors(path):
            for parent in (path, *path.parents):
                if parent.is_symlink() or not parent.is_dir():
                    raise ValueError('unsafe_parent')
        for name, function in (('_require_root', lambda: None), ('_safe_ancestors', ancestors),
                               ('_owner_mode', lambda *a, **k: None), ('_chown', lambda *a: None),
                               ('_fsync_directory', lambda *a: None)):
            self.stack.enter_context(patch.object(store, name, function))
        self.stack.enter_context(patch.object(store.os, 'chmod', lambda *a, **k: None))
        self.stack.enter_context(patch.object(os, 'O_NOFOLLOW', 0, create=True))
        self.stack.enter_context(patch.object(journal, 'tree_identity', identity))
        self.stack.enter_context(patch.object(journal, '_parents_same_device', lambda *a: None))
        self.stack.enter_context(patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *a: None)}))
        # Docker's pure production path schema is Linux-only. Filesystem fixtures
        # run under a Windows temporary root; the pure-contract test below uses
        # the real validator with a canonical /opt path.
        self.stack.enter_context(patch.object(removal, 'validate_inventory', lambda value: copy.deepcopy(value)))
        self.layout = store.prepare_layout(self.path, PROJECT, DATABASE)
        self.input = inventory(self.path)
        self.sources = {name: b'# fixed test module\n' for name in removal.RUNTIME_FILES}
        self.sources.update({name: b'# fixed helper\n' for name in removal.GENERATED_FILES})
        self.stack.enter_context(patch.object(removal, '_runtime_sources', lambda: dict(self.sources)))

    def prepare(self):
        return removal.prepare_uninstall(self.layout, self.input)

    def through(self, operation, phase):
        while operation.phase != phase:
            next_phase = removal.PHASES[removal.PHASES.index(operation.phase) + 1]
            operation = removal.advance(operation, next_phase)
        return operation

    def test_cipher_scope_preserves_canonical_group_write_without_weakening_tree_guard(self):
        actual = self.layout.cipher_directory.lstat()
        info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o730, st_uid=0, st_gid=10001,
                               st_dev=actual.st_dev, st_ino=actual.st_ino)
        with patch.object(Path, 'lstat', return_value=info), \
                patch.object(store, '_owner_mode', REAL_OWNER_MODE), \
                patch.object(journal, 'tree_identity', side_effect=AssertionError('generic guard must not own cipher')):
            self.assertEqual(removal._scope_identity(self.layout, 'cipher', self.layout.cipher_directory),
                             {'device': actual.st_dev, 'inode': actual.st_ino})
            for wrong_mode, wrong_gid in ((0o737, 10001), (0o730, 0), (0o777, 10001)):
                info.st_mode, info.st_gid = stat.S_IFDIR | wrong_mode, wrong_gid
                with self.assertRaisesRegex(ValueError, 'permissions_invalid'):
                    removal._scope_identity(self.layout, 'cipher', self.layout.cipher_directory)

    def test_prepare_requires_update_lock_and_captures_closed_private_runtime(self):
        with self.assertRaisesRegex(ValueError, 'update_lock_required'):
            self.prepare()
        with journal.update_lock(self.layout):
            operation = self.prepare()
            self.assertEqual(operation.phase, 'prepared')
            self.assertEqual(operation.inventory, self.input)
            self.assertEqual(set(operation.value['runtime']), set(self.sources))
            for name, data in self.sources.items():
                self.assertEqual((operation.helper_path.parent / name).read_bytes(), data)
        exposed = operation.inventory
        exposed['port'] = 9999
        self.assertEqual(operation.inventory['port'], 8100)

    def test_reentry_is_same_operation_not_new_confirmation_or_new_source(self):
        with journal.update_lock(self.layout):
            first = self.prepare()
            self.assertEqual(self.prepare(), first)
            changed = dict(self.input, port=8101)
            with self.assertRaisesRegex(ValueError, 'operation_changed'):
                removal.prepare_uninstall(self.layout, changed)

    def test_interrupted_runtime_capture_resumes_only_recorded_bytes(self):
        original = store._create_file
        def interrupt(path, data, **kwargs):
            if path.name in ('install.py', '.pending-' + hashlib.sha256(b'install.py').hexdigest()):
                raise OSError('synthetic interruption')
            return original(path, data, **kwargs)
        with journal.update_lock(self.layout):
            with patch.object(store, '_create_file', side_effect=interrupt), self.assertRaises(OSError):
                self.prepare()
            pending = removal.load_uninstall(self.layout)
            self.assertEqual(pending.phase, 'preparing')
            result = self.prepare()
            self.assertEqual(result.phase, 'prepared')
            self.assertEqual(result.value['removal_id'], pending.value['removal_id'])

    def test_partial_staging_write_never_publishes_module_and_retry_finishes(self):
        original = store._create_file
        pending_name = '.pending-' + hashlib.sha256(b'install.py').hexdigest()
        def partial(path, data, **kwargs):
            if path.name in ('install.py', pending_name):
                original(path, data[:3], **kwargs)
                raise OSError('crash after exclusive create and partial write')
            return original(path, data, **kwargs)
        with journal.update_lock(self.layout):
            with patch.object(store, '_create_file', side_effect=partial), self.assertRaises(OSError):
                self.prepare()
            operation = removal.load_uninstall(self.layout)
            runtime = operation.helper_path.parent
            self.assertEqual(operation.phase, 'preparing')
            self.assertFalse((runtime / 'install.py').exists())
            self.assertEqual((runtime / pending_name).read_bytes(), self.sources['install.py'][:3])
            completed = self.prepare()
            self.assertEqual(completed.phase, 'prepared')
            self.assertEqual((runtime / 'install.py').read_bytes(), self.sources['install.py'])
            self.assertFalse(list(runtime.glob('.pending-*')))

    def test_interrupted_runtime_cannot_switch_to_different_source_modules(self):
        with journal.update_lock(self.layout):
            with patch.object(removal, '_complete_runtime', side_effect=OSError()), self.assertRaises(OSError):
                self.prepare()
            self.sources['install.py'] = b'# changed since confirmation\n'
            with self.assertRaisesRegex(ValueError, 'runtime_source_changed'):
                self.prepare()

    def test_unexplained_candidate_or_helper_refused_before_intent(self):
        for path in (self.layout.root / 'candidate', self.layout.owner / removal.RUNTIME_DIRECTORY):
            path.mkdir()
            with journal.update_lock(self.layout), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse((self.layout.owner / removal.JOURNAL_FILENAME).exists())
            path.rmdir()

    def test_inventory_scope_and_nonsecret_closed_fields(self):
        for changed in (dict(self.input, port=True), dict(self.input, port=65536),
                        dict(self.input, extra='password'), dict(self.input, update_operation_id='bad')):
            with journal.update_lock(self.layout), self.assertRaises(ValueError):
                removal.prepare_uninstall(self.layout, changed)
        changed = copy.deepcopy(self.input)
        changed['docker']['project'] = 'leti-cook-fedcba987654'
        with journal.update_lock(self.layout), self.assertRaisesRegex(ValueError, 'scope_mismatch'):
            removal.prepare_uninstall(self.layout, changed)

    def test_phase_order_is_durable_and_skips_or_stale_caller_refused(self):
        with journal.update_lock(self.layout):
            operation = self.prepare()
            with self.assertRaisesRegex(ValueError, 'phase_order_invalid'):
                removal.advance(operation, 'resources_removed')
            newer = removal.advance(operation, 'stop_intent')
            self.assertEqual(removal.advance(newer, 'stop_intent').phase, 'stop_intent')
            with self.assertRaisesRegex(ValueError, 'operation_changed'):
                removal.advance(operation, 'stop_intent')
            with self.assertRaisesRegex(ValueError, 'phase_order_invalid'):
                removal.advance(newer, 'prepared')

    def test_journal_corruption_unknown_fields_and_duplicate_keys_fail(self):
        with journal.update_lock(self.layout):
            self.prepare()
        path = self.layout.owner / removal.JOURNAL_FILENAME
        saved = path.read_bytes()
        path.write_bytes(saved.replace(b'"version":1', b'"version":1,"version":1'))
        with self.assertRaises(ValueError):
            removal.load_uninstall(self.layout)
        value = json.loads(saved)
        value['password'] = 'not-permitted'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            removal.load_uninstall(self.layout)

    def test_replaced_tree_is_never_adopted_even_with_same_name(self):
        with journal.update_lock(self.layout):
            self.prepare()
        saved = self.path.with_name('preserved')
        self.path.rename(saved)
        self.path.mkdir()
        with self.assertRaisesRegex(ValueError, 'tree_changed'):
            removal.load_uninstall(self.layout)
        self.assertEqual((saved / 'owned.txt').read_bytes(), b'own')

    def test_missing_installation_allowed_only_after_tree_intent(self):
        with journal.update_lock(self.layout):
            operation = self.prepare()
            moved = self.path.with_name('moved')
            self.path.rename(moved)
            with self.assertRaisesRegex(ValueError, 'tree_missing'):
                removal.load_uninstall(self.layout)
            moved.rename(self.path)
            operation = self.through(operation, 'trees_intent')
            self.path.rename(moved)
            loaded = removal.resume_scope(operation.helper_path.parent)
            self.assertEqual(loaded.phase, 'trees_intent')
            moved.rename(self.path)

    def test_tree_deletion_needs_resources_confirmation_and_key_first(self):
        with journal.update_lock(self.layout):
            operation = self.prepare()
            with self.assertRaisesRegex(ValueError, 'tree_removal_intent_required'):
                removal.remove_installation_trees(operation)
            operation = self.through(operation, 'resources_removed')
            self.layout.key_path.write_bytes(b'key')
            with patch.object(removal, '_remove_recorded') as delete, self.assertRaisesRegex(ValueError, 'copy_not_removed'):
                removal.remove_installation_trees(operation)
            delete.assert_not_called()
            self.assertEqual(removal.load_uninstall(self.layout).phase, 'trees_intent')

    def test_tree_removal_uses_only_captured_fixed_paths_then_records_completion(self):
        with journal.update_lock(self.layout):
            operation = self.through(self.prepare(), 'resources_removed')
            with patch.object(removal, '_remove_recorded', return_value=True) as delete:
                operation = removal.remove_installation_trees(operation)
            self.assertEqual(operation.phase, 'trees_removed')
            self.assertEqual(delete.call_args_list[0].args,
                             (self.path, operation.value['trees']['installation']))
            self.assertEqual(len(delete.call_args_list), 1)

    def test_finalize_writes_terminal_intent_before_standalone_cleanup(self):
        with journal.update_lock(self.layout):
            operation = self.through(self.prepare(), 'trees_removed')
            seen = []
            def finish(owner, expected):
                seen.append((owner, removal.load_uninstall(self.layout).phase, expected))
            with patch.object(removal, '_finish_scope', side_effect=finish):
                removal.finalize_uninstall(operation)
            self.assertEqual(seen[0][1], 'scope_intent')
            self.assertEqual(seen[0][2], (self.layout.owner / removal.JOURNAL_FILENAME).read_bytes())

    def test_terminal_partial_scope_can_load_while_ordinary_validation_refuses(self):
        with journal.update_lock(self.layout):
            operation = self.through(self.prepare(), 'scope_intent')
        self.layout.keys.rmdir()
        with self.assertRaises(FileNotFoundError):
            store._validate_layout(self.layout)
        loaded = removal.load_uninstall(self.layout)
        self.assertEqual(loaded.phase, 'scope_intent')
        with removal.removal_lock(loaded):
            self.assertEqual(removal.load_uninstall(self.layout), loaded)

    def test_removal_lock_contention_has_no_write_or_delete(self):
        with journal.update_lock(self.layout):
            operation = self.prepare()
        def busy(*args):
            raise BlockingIOError()
        with patch.dict(sys.modules, {'fcntl': SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=busy)}):
            with self.assertRaisesRegex(ValueError, 'update_lock_busy'):
                with removal.removal_lock(operation):
                    self.fail('acquired foreign lock')
        self.assertEqual(removal.load_uninstall(self.layout), operation)

    def test_terminal_helper_keeps_local_finalizer_before_application_imports(self):
        source = Path(removal.__file__).read_text(encoding='utf-8-sig')
        self.assertLess(source.index("if value.get('phase')=='scope_intent':"),
                        source.index('from ops.uninstall import resume'))
        self.assertIn("keep=('resume.py',)", source)
        self.assertIn("keep=('uninstall-runtime', 'uninstall-journal.json', 'update.lock')", source)
        self.assertNotIn('shutil.rmtree', source)


class PureContractTests(unittest.TestCase):
    def test_real_docker_inventory_validator_is_part_of_closed_store_contract(self):
        layout = store.StoreLayout(PurePosixPath('/opt/leti-cook-example'), PROJECT, DATABASE)
        value = inventory('/opt/leti-cook-example')
        self.assertEqual(removal._inventory(value, layout), value)
        value['docker']['containers'] = [{'Env': ['SECRET=bad']}]
        with self.assertRaises(ValueError):
            removal._inventory(value, layout)

    def test_real_resume_capture_compiles_without_importing_runtime_or_docker(self):
        files = removal._runtime_sources()
        compile(files['resume.py'], 'resume.py', 'exec')
        self.assertLess(sum(map(len, files.values())), removal.MAX_RUNTIME_BYTES)
        self.assertEqual(set(files), set(removal.RUNTIME_FILES) | set(removal.GENERATED_FILES))
        self.assertFalse(any('.env' in name or 'installed.json' in name for name in files))


if __name__ == '__main__':
    unittest.main()
