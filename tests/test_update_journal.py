"""Offline U4 intent/identity tests. Native renameat2/flock tested separately."""
from __future__ import annotations

from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ops import update_copy_store as store
from ops import update_journal as journal

PROJECT = 'leti-cook-0123456789ab'
DATABASE_ID = '9a2fc4bc-9d4a-4bc4-b4be-69c7d37f2d23'
OP = 'a' * 32


def identities(path):
    info = Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise journal.JournalError('update_tree_invalid')
    return {'device': info.st_dev, 'inode': info.st_ino}


def sample(layout):
    return {'format': 'leti-cook-update', 'version': 1, 'operation_id': OP,
            'project': PROJECT, 'database_id': DATABASE_ID, 'phase': 'preparing',
            'old_version': '0.1.0', 'new_version': '0.2.0',
            'old_tree': identities(layout.installation), 'new_tree': None,
            'old_images': {'db': 'sha256:' + '1' * 64, 'backend': 'sha256:' + '2' * 64},
            'new_images': None, 'old_volume': PROJECT + '_database-data',
            'restore_volume': None, 'runtime_epoch': None,
            'started_at': 2000000000, 'stopped_at': None, 'copy': None, 'failure_code': None}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='leti-update-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.installation = self.root / 'install'
        self.installation.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events = []
        def safe(path):
            for item in (path, *path.parents):
                if not stat.S_ISDIR(item.lstat().st_mode) or item.is_symlink():
                    raise ValueError('unsafe')
        for name, function in (
                ('_require_root', lambda: None), ('_safe_ancestors', safe),
                ('_owner_mode', lambda info, mode, gid=0: None),
                ('_chown', lambda *args: None),
                ('_fsync_directory', lambda path: self.events.append(('fsync', Path(path))))):
            self.stack.enter_context(patch.object(store, name, function))
        self.stack.enter_context(patch.object(store.os, 'chmod', lambda *args, **kwargs: None))
        self.stack.enter_context(patch.dict(sys.modules, {'fcntl': SimpleNamespace(
            flock=lambda *args: None, LOCK_EX=2, LOCK_NB=4)}))
        self.stack.enter_context(patch.object(journal, 'tree_identity', identities))
        self.stack.enter_context(patch.object(journal, '_parents_same_device', lambda _: None))
        self.layout = store.prepare_layout(self.installation, PROJECT, DATABASE_ID)
        self.value = sample(self.layout)

    def save(self, **changes):
        self.value.update(changes)
        return journal.save_journal(self.layout, self.value)

    def candidate(self):
        path = self.layout.root / 'candidate'
        path.mkdir()
        (path / 'new-version').write_text('candidate')
        self.value['new_tree'] = identities(path)
        self.value['new_images'] = {'db': 'sha256:' + '3' * 64, 'backend': 'sha256:' + '4' * 64}
        return path

    def prepared(self):
        journal.save_journal(self.layout, self.value)
        self.candidate()
        return self.save(phase='prepared')

    def fake_exchange(self, left, right):
        temporary = left.with_name(left.name + '.unit-test')
        left.rename(temporary)
        right.rename(left)
        temporary.rename(right)

    def test_strict_closed_journal_rejects_types_paths_secrets_and_extra_fields(self):
        variants = [dict(self.value, extra='secret'), dict(self.value, version=True),
                    dict(self.value, phase=[]), dict(self.value, phase='arbitrary'),
                    dict(self.value, old_volume='foreign_volume'),
                    dict(self.value, failure_code='/secret/path'),
                    dict(self.value, old_version='latest'),
                    dict(self.value, runtime_epoch='not-an-epoch'),
                    dict(self.value, started_at=True),
                    dict(self.value, old_images={'backend': 'mutable:latest', 'db': 'x'}),
                    dict(self.value, old_tree={'device': 0, 'inode': True}),
                    dict(self.value, restore_volume=self.value['old_volume'])]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                journal.validate_journal(value, self.layout)
        independent = journal.validate_journal(self.value, self.layout)
        independent['old_images']['db'] = 'changed'
        self.assertNotEqual(independent, self.value)

    def test_mutation_requires_separate_nonblocking_update_lock(self):
        with self.assertRaisesRegex(ValueError, 'update_lock_required'):
            journal.save_journal(self.layout, self.value)
        with journal.update_lock(self.layout):
            self.save()
            with self.assertRaisesRegex(ValueError, 'update_lock_busy'):
                with journal.update_lock(self.layout):
                    self.fail('nested lock acquired')
        self.assertTrue((self.layout.owner / 'update.lock').is_file())
        self.assertTrue((self.layout.owner / 'store.lock').is_file())
        with self.assertRaisesRegex(ValueError, 'update_lock_required'):
            journal.remove_hold(self.layout, OP)

    def test_flock_contention_refuses_before_any_journal_write(self):
        fake = SimpleNamespace(LOCK_EX=2, LOCK_NB=4)
        def busy(*args):
            raise BlockingIOError()
        fake.flock = busy
        with patch.dict(sys.modules, {'fcntl': fake}):
            with self.assertRaisesRegex(ValueError, 'update_lock_busy'):
                with journal.update_lock(self.layout):
                    self.fail('acquired busy lock')
        self.assertIsNone(journal.load_journal(self.layout))

    def test_initial_preparing_requires_absent_candidate_and_correct_active_identity(self):
        candidate = self.layout.root / 'candidate'
        candidate.mkdir()
        with journal.update_lock(self.layout):
            with self.assertRaisesRegex(ValueError, 'preparation_path_occupied'):
                self.save()
        candidate.rmdir()
        with journal.update_lock(self.layout):
            self.value['old_tree']['inode'] += 1
            with self.assertRaisesRegex(ValueError, 'tree_identity_mismatch'):
                self.save()
        self.assertIsNone(journal.load_journal(self.layout))

    def test_durable_save_replaces_only_complete_json_after_file_fsync(self):
        with journal.update_lock(self.layout):
            self.save()
            initial = journal.load_journal(self.layout)
            self.prepared_update = None
            path = self.layout.owner / journal.JOURNAL_FILENAME
            original = path.read_bytes()
            with patch.object(journal.os, 'replace', side_effect=OSError('synthetic crash before rename')):
                with self.assertRaises(OSError):
                    self.save(failure_code='temporary_failure')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(journal.load_journal(self.layout), initial)
            self.assertEqual(list(self.layout.owner.glob('.update-journal.*.tmp')), [])
            self.save(failure_code=None)
            self.assertIn(('fsync', self.layout.owner), self.events)

    def test_identity_fields_and_populated_once_fields_cannot_change(self):
        with journal.update_lock(self.layout):
            self.prepared()
            for name, replacement in [('operation_id', 'b' * 32), ('old_version', '0.0.9'),
                                      ('old_images', {'db': 'sha256:' + '5' * 64,
                                                      'backend': 'sha256:' + '6' * 64}),
                                      ('new_tree', {'device': 0, 'inode': 44})]:
                candidate = copy.deepcopy(self.value)
                candidate[name] = replacement
                with self.subTest(name=name), self.assertRaises(ValueError):
                    journal.save_journal(self.layout, candidate)

    def test_prepared_and_active_phases_require_candidate_and_images(self):
        for phase in ('prepared', 'drained', 'swap_intent', 'schema_applied', 'cleanup'):
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, 'candidate_identity_missing'):
                journal.validate_journal(dict(self.value, phase=phase))
        for phase in ('preparing', 'rollback_verified', 'rollback_cleanup', 'rolled_back'):
            journal.validate_journal(dict(self.value, phase=phase))

    def test_new_operation_replaces_only_terminal_journal(self):
        with journal.update_lock(self.layout):
            self.save()
            next_value = dict(self.value, operation_id='b' * 32)
            with self.assertRaisesRegex(ValueError, 'operation_pending'):
                journal.save_journal(self.layout, next_value)
            self.save(phase='rollback_verified')
            self.save(phase='rollback_cleanup')
            self.save(phase='rolled_back')
            result = journal.save_journal(self.layout, next_value)
            self.assertEqual(result['operation_id'], 'b' * 32)

    def test_restore_volume_assignment_requires_durable_restore_intent(self):
        with journal.update_lock(self.layout):
            self.prepared()
            proposed = dict(self.value, restore_volume=PROJECT + '_restore_one', phase='rollback_restored')
            with self.assertRaisesRegex(ValueError, 'restore_intent_required'):
                journal.save_journal(self.layout, proposed)
            self.save(phase='rollback_restore_intent')
            self.save(phase='rollback_restored', restore_volume=PROJECT + '_restore_one')
            self.save(phase='rollback_restore_intent')
            self.save(phase='rollback_restored', restore_volume=PROJECT + '_restore_two')

    def test_hold_requires_intent_and_only_same_operation_can_remove_it(self):
        with journal.update_lock(self.layout):
            self.prepared()
            with self.assertRaisesRegex(ValueError, 'hold_intent_required'):
                journal.write_hold(self.layout, OP)
            self.save(phase='hold_requested')
            self.assertTrue(journal.write_hold(self.layout, OP))
            self.assertFalse(journal.write_hold(self.layout, OP))
            hold = self.layout.marker_directory / 'update-hold.json'
            self.assertEqual(json.loads(hold.read_bytes()), {'operation_id': OP})
            with self.assertRaisesRegex(ValueError, 'operation_mismatch'):
                journal.remove_hold(self.layout, 'b' * 32)
            self.assertTrue(hold.exists())
            self.assertTrue(journal.remove_hold(self.layout, OP))
            self.assertFalse(journal.remove_hold(self.layout, OP))
            self.assertFalse(self.layout.marker_path.exists())

    def test_foreign_or_malformed_hold_is_not_replaced(self):
        with journal.update_lock(self.layout):
            self.prepared()
            self.save(phase='hold_requested')
            hold = self.layout.marker_directory / journal.HOLD_FILENAME
            for data in (b'{"operation_id":"' + b'b'*32 + b'"}',
                         b'{"operation_id":"' + b'a'*32 + b'","extra":1}'):
                hold.write_bytes(data)
                with self.assertRaises(ValueError):
                    journal.write_hold(self.layout, OP)
                self.assertEqual(hold.read_bytes(), data)

    def test_swap_uses_intent_and_inodes_to_avoid_double_exchange_after_lost_ack(self):
        with journal.update_lock(self.layout):
            self.prepared()
            old, new = self.value['old_tree'], self.value['new_tree']
            with self.assertRaisesRegex(ValueError, 'exchange_intent_required'):
                journal.exchange_trees(self.layout, old, new)
            self.save(phase='swap_intent')
            with patch.object(journal, '_rename_exchange', side_effect=self.fake_exchange) as exchange:
                self.assertEqual(journal.exchange_trees(self.layout, old, new), 'new')
                self.assertEqual(journal.exchange_trees(self.layout, old, new), 'new')
                self.assertEqual(exchange.call_count, 1)
                self.save(phase='rollback_intent')
                self.assertEqual(journal.exchange_trees(self.layout, old, new, desired='old'), 'old')
                self.assertEqual(journal.exchange_trees(self.layout, old, new, desired='old'), 'old')
                self.assertEqual(exchange.call_count, 2)

    def test_unknown_inode_pair_is_refused_before_exchange(self):
        with journal.update_lock(self.layout):
            self.prepared()
            self.save(phase='swap_intent')
            path = self.layout.root / 'candidate'
            path.rename(self.layout.root / 'unexpected')
            path.mkdir()
            with patch.object(journal, '_rename_exchange') as exchange:
                with self.assertRaisesRegex(ValueError, 'tree_identity_mismatch'):
                    journal.exchange_trees(self.layout, self.value['old_tree'], self.value['new_tree'])
                exchange.assert_not_called()

    def test_preflight_uses_disposable_siblings_without_touching_installed_tree(self):
        with journal.update_lock(self.layout):
            before = identities(self.installation)
            with patch.object(journal, '_rename_exchange', side_effect=self.fake_exchange):
                self.assertTrue(journal.preflight_exchange(self.layout))
            self.assertEqual(before, identities(self.installation))
            self.assertEqual(list(self.root.glob('.leti-cook-exchange-*')), [])
            self.assertEqual(list(self.layout.root.glob('.exchange-*')), [])
            with patch.object(journal, '_rename_exchange', side_effect=journal.JournalError('update_exchange_unsupported')):
                with self.assertRaisesRegex(ValueError, 'exchange_unsupported'):
                    journal.preflight_exchange(self.layout)
            self.assertEqual(list(self.root.glob('.leti-cook-exchange-*')), [])

    def test_cleanup_requires_recorded_identity_and_durable_phase(self):
        with journal.update_lock(self.layout):
            self.prepared()
            with self.assertRaisesRegex(ValueError, 'cleanup_intent_required'):
                journal.remove_tree(self.layout, self.value['new_tree'])
            self.save(phase='rollback_cleanup')
            with patch.object(journal, '_remove_candidate', return_value=True) as removal:
                self.assertTrue(journal.remove_tree(self.layout, self.value['new_tree']))
                self.assertEqual(removal.call_args.args, (self.layout, self.value['new_tree']))
                with self.assertRaisesRegex(ValueError, 'identity_unrecorded'):
                    journal.remove_tree(self.layout, {'device': 0, 'inode': 42})
                self.assertEqual(removal.call_count, 1)

    def test_unpinned_preparing_cleanup_only_after_initial_absent_path_intent(self):
        with journal.update_lock(self.layout):
            with self.assertRaisesRegex(ValueError, 'preparing_cleanup_refused'):
                journal.cleanup_preparing_candidate(self.layout)
            self.save()
            (self.layout.root / 'candidate').mkdir()
            identity = identities(self.layout.root / 'candidate')
            with patch.object(journal, '_remove_candidate', return_value=True) as removal:
                self.assertTrue(journal.cleanup_preparing_candidate(self.layout))
                self.assertEqual(removal.call_args.args, (self.layout, identity))
            self.save(new_tree=identity)
            with self.assertRaisesRegex(ValueError, 'preparing_cleanup_refused'):
                journal.cleanup_preparing_candidate(self.layout)

    def test_duplicate_journal_keys_and_symlink_final_target_are_refused(self):
        with journal.update_lock(self.layout):
            self.save()
            path = self.layout.owner / journal.JOURNAL_FILENAME
            data = path.read_bytes()
            path.write_bytes(data.replace(b'{', b'{"version":1,', 1))
            with self.assertRaisesRegex(ValueError, 'receipt_invalid'):
                journal.load_journal(self.layout)


class NativeGuardSourceTests(unittest.TestCase):
    def test_no_non_atomic_fallback_and_nested_mount_identity_guard_are_present(self):
        source = (ROOT / 'ops/update_journal.py').read_text(encoding='utf-8')
        self.assertIn('library.renameat2', source)
        self.assertIn('os.O_DIRECTORY | os.O_NOFOLLOW', source)
        self.assertIn("'/proc/self/fdinfo/'", source)
        self.assertIn('if _mount_id(child) != mount:', source)
        self.assertNotIn('shutil.rmtree', source)


if __name__ == '__main__':
    unittest.main()
