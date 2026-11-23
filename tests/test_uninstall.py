"""Deletion confirmation and restartable ordering, without Docker or filesystem writes."""
import contextlib
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
import unittest
import sys
import tempfile
from unittest.mock import Mock, patch

from ops import uninstall as removal
from ops.update_journal import JournalError


@dataclass(frozen=True)
class Operation:
    phase: str = 'prepared'
    layout: object = None
    inventory: object = None
    helper_path: object = Path('/owned/resume.py')


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.layout = SimpleNamespace(project='leti-cook-0123456789ab', installation=Path('/opt/leti-cook-test'))
        self.inventory = {'docker': {name: [] for name in ('containers', 'volumes', 'networks', 'images')},
                          'port': 8101, 'update_operation_id': None}
        self.op = Operation(layout=self.layout, inventory=self.inventory)
        self.events = []
        self.docker = Mock()
        self.docker.inspect_plan.return_value = self.inventory['docker']
        for name in ('stop', 'remove_containers', 'remove_volumes', 'remove_networks', 'remove_images', 'verify_removed'):
            getattr(self.docker, name).side_effect = lambda *a, _name=name: self.events.append(_name) or {'removed': [], 'absent': [], 'retained': []}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(removal, 'DockerResources', return_value=self.docker))
        self.stack.enter_context(patch.object(removal.store, 'advance', side_effect=self.advance))
        self.stack.enter_context(patch.object(removal, 'cancel_copy', side_effect=lambda op: self.events.append('cancel_copy')))
        self.stack.enter_context(patch.object(removal.store, 'remove_installation_trees',
            side_effect=lambda op: self.events.append('trees') or replace(op, phase='trees_removed')))
        self.stack.enter_context(patch.object(removal.store, 'finalize_uninstall', side_effect=lambda op: self.events.append('scope')))

    def advance(self, operation, phase):
        self.events.append('intent:' + phase)
        return replace(operation, phase=phase)

    def test_stop_then_key_first_then_resources_then_files(self):
        self.assertEqual(removal._run(self.op, emit=lambda value: None), 0)
        order = ['stop', 'cancel_copy', 'remove_containers', 'remove_volumes', 'remove_networks', 'remove_images',
                 'verify_removed', 'trees', 'scope']
        self.assertEqual([v for v in self.events if not v.startswith('intent:')], order)
        for intent, effect in [('stop_intent', 'stop'), ('copy_intent', 'cancel_copy'), ('resources_intent', 'remove_containers')]:
            self.assertLess(self.events.index('intent:' + intent), self.events.index(effect))

    def test_resume_skips_completed_groups(self):
        expected = {'workers_stopped': 'cancel_copy', 'copy_removed': 'remove_containers',
                    'resources_removed': 'trees', 'trees_removed': 'scope', 'scope_intent': 'scope'}
        for phase, first in expected.items():
            with self.subTest(phase=phase):
                self.events.clear()
                removal._run(replace(self.op, phase=phase), emit=lambda value: None)
                self.assertEqual(next(v for v in self.events if not v.startswith('intent:')), first)

    def test_failure_never_discards_code_or_resume_helper(self):
        self.docker.remove_volumes.side_effect = ValueError('uninstall_foreign_reference')
        with self.assertRaises(ValueError):
            removal._run(self.op, emit=lambda value: None)
        self.assertNotIn('trees', self.events)
        self.assertNotIn('scope', self.events)

    def test_shared_image_is_reported_and_does_not_block_owned_cleanup(self):
        self.docker.remove_images.side_effect = None
        self.docker.remove_images.return_value = {'removed': [], 'absent': [], 'retained': [{'id': 'sha256:abc', 'reason': 'shared'}]}
        lines = []
        self.assertEqual(removal._run(self.op, emit=lines.append), 0)
        self.assertIn('retained: sha256:abc (shared)', lines)
        self.assertIn('scope', self.events)

    def entry(self, answer):
        with patch.object(removal, 'load_layout', return_value=self.layout), \
                patch.object(removal, 'update_lock', lambda _: contextlib.nullcontext()), \
                patch.object(removal, 'load_journal', return_value=None), \
                patch.object(removal.store, 'load_uninstall', return_value=None), \
                patch.object(removal.store, 'inspect_trees'), patch.object(removal, 'inspect_copy'), \
                patch.object(removal.store, 'prepare_uninstall', return_value=self.op) as prepare:
            code = removal.uninstall(self.layout.installation, {'project': self.layout.project, 'database_id': 'identity', 'port': 8101},
                                     ask=lambda prompt: answer, emit=lambda value: None)
            return code, prepare.call_count

    def test_only_exact_typed_project_authorizes_removal(self):
        for value in ('', 'yes', 'INSTALL', self.layout.project + ' ', 'other-project'):
            self.assertEqual(self.entry(value), (2, 0))
            self.assertEqual(self.events, [])
        self.assertEqual(self.entry(self.layout.project), (0, 1))

    def test_successful_update_recovery_requires_rerun_from_recovered_code(self):
        pending = {'operation_id': 'a' * 32, 'phase': 'swapped'}
        with patch.object(removal, 'load_layout', return_value=self.layout), \
                patch.object(removal, 'update_lock', lambda _: contextlib.nullcontext()), \
                patch.object(removal, 'load_journal', return_value=pending), \
                patch.object(removal.store, 'load_uninstall', return_value=None), \
                patch('ops.update_host.Host'), patch('ops.update_engine.UpdateEngine') as engine:
            answer = Mock(side_effect=AssertionError('confirmation must wait for fresh code'))
            self.assertEqual(removal.uninstall(self.layout.installation, {'port': 8101}, ask=answer, emit=lambda _: None), 0)
            engine.return_value.recover.assert_called_once()
            self.docker.inspect_plan.assert_not_called()
            self.assertEqual(self.events, [])

    def test_failed_update_recovery_still_requires_confirmation_and_ownership(self):
        pending = {'operation_id': 'a' * 32, 'phase': 'failed'}
        with patch.object(removal, 'load_layout', return_value=self.layout), \
                patch.object(removal, 'update_lock', lambda _: contextlib.nullcontext()), \
                patch.object(removal, 'load_journal', return_value=pending), \
                patch.object(removal.store, 'load_uninstall', return_value=None), \
                patch.object(removal.store, 'inspect_trees') as trees, \
                patch.object(removal, 'inspect_copy') as copy, \
                patch('ops.update_host.Host'), patch('ops.update_engine.UpdateEngine') as engine:
            engine.return_value.recover.side_effect = ValueError('update_restore_failed')
            lines = []
            self.assertEqual(removal.uninstall(self.layout.installation, {'port': 8101}, ask=lambda _: '', emit=lines.append), 2)
            self.docker.inspect_plan.assert_called_once_with(pending)
            trees.assert_called_once_with(self.layout)
            copy.assert_called_once_with(self.layout, pending['operation_id'])
            self.assertTrue(any('remaining recovery copy' in line for line in lines))
            self.assertEqual(self.events, [])

    def plan(self, partial, answer=''):
        lines = []
        with patch.object(removal, 'store_created', return_value=True),                 patch.object(removal, 'load_layout', return_value=self.layout),                 patch.object(removal, 'update_lock', lambda _: contextlib.nullcontext()),                 patch.object(removal, 'load_journal', return_value=None),                 patch.object(removal.store, 'load_uninstall', return_value=None),                 patch.object(removal.store, 'inspect_trees') as trees, patch.object(removal, 'inspect_copy'),                 patch.object(removal.store, 'prepare_uninstall', return_value=self.op) as prepare:
            code = removal.uninstall(self.layout.installation, {'project': self.layout.project,
                                     'database_id': 'identity', 'port': 8101}, partial=partial,
                                     ask=lambda prompt: answer, emit=lines.append)
            trees.assert_called_once_with(self.layout)
            return code, lines, prepare.call_count

    def test_unfinished_installation_has_the_same_plan_and_confirmation_without_backup_advice(self):
        code, lines, prepared = self.plan(True)
        self.assertEqual((code, prepared), (2, 0))
        self.assertIn('This installation did not finish, so it has no work to back up.', lines)
        self.assertFalse(any('backup' in line.lower() and 'no work' not in line for line in lines))
        for name in ('containers', 'volumes', 'networks', 'images'):
            self.assertIn(name + ': 0', lines)
        self.assertEqual(lines[-1], 'Nothing was removed.')
        self.assertEqual(self.events, [])
        code, lines, prepared = self.plan(True, self.layout.project)
        self.assertEqual((code, prepared), (0, 1))
        self.assertIn('Installation removed. Shared images were kept where needed.', lines)
        self.assertIn('scope', self.events)

    def test_finished_installation_still_advises_a_backup(self):
        _, lines, _ = self.plan(False)
        self.assertIn('Use Backup and restore → Download backup to keep your work.', lines)
        self.assertNotIn('This installation did not finish, so it has no work to back up.', lines)

    def test_unfinished_installation_before_its_store_only_names_the_folders(self):
        for keys in (False, True):
            with self.subTest(keys=keys), tempfile.TemporaryDirectory() as base:
                directory = Path(base, 'installation')
                directory.mkdir()
                if keys:
                    Path(base, 'installation.update-keys').mkdir()
                lines = []
                with patch.object(removal, 'load_layout', side_effect=AssertionError('no store to load')):
                    code = removal.uninstall(directory, {'project': self.layout.project, 'port': 8101,
                                             'database_id': 'identity'}, partial=True,
                                             ask=Mock(side_effect=AssertionError('nothing to confirm')),
                                             emit=lines.append)
                folders = str(directory) + (' ' + str(Path(base, 'installation.update-keys')) if keys else '')
                self.assertEqual((code, lines), (1, [
                    'This installation stopped before it created any Docker resources. Nothing was removed.',
                    'Remove its folders with: sudo rm -r -- ' + folders]))
                self.docker.inspect_plan.assert_not_called()
                self.assertEqual(self.events, [])

    def test_busy_lock_of_an_unfinished_installation_means_its_installer_still_runs(self):
        @contextlib.contextmanager
        def busy(layout):
            raise JournalError('update_lock_busy')
            yield
        state = {'project': self.layout.project, 'database_id': 'identity', 'port': 8101}
        stop = Mock(side_effect=AssertionError('nothing to confirm'))
        with patch.object(removal, 'store_created', return_value=True), \
                patch.object(removal, 'load_layout', return_value=self.layout), \
                patch.object(removal, 'update_lock', busy):
            with self.assertRaisesRegex(ValueError, '^uninstall_installation_in_progress$'):
                removal.uninstall(self.layout.installation, state, partial=True, ask=stop, emit=lambda _: None)
            # A finished installation keeps the shared code and its manage.py hint.
            with self.assertRaisesRegex(ValueError, '^update_lock_busy$'):
                removal.uninstall(self.layout.installation, state, ask=stop, emit=lambda _: None)
        self.docker.inspect_plan.assert_not_called()
        self.assertEqual(self.events, [])

    def test_only_taking_the_lock_is_read_as_a_running_install(self):
        # The same code from inside the held section is not renamed.
        self.docker.inspect_plan.side_effect = JournalError('update_lock_busy')
        with patch.object(removal, 'store_created', return_value=True), \
                patch.object(removal, 'load_layout', return_value=self.layout), \
                patch.object(removal, 'update_lock', lambda _: contextlib.nullcontext()), \
                patch.object(removal, 'load_journal', return_value=None), \
                patch.object(removal.store, 'load_uninstall', return_value=None):
            with self.assertRaisesRegex(ValueError, '^update_lock_busy$'):
                removal.uninstall(self.layout.installation, {'port': 8101}, partial=True,
                                  ask=lambda _: '', emit=lambda _: None)

    def test_store_receipt_decides_and_an_unreadable_one_is_not_absent(self):
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base, 'installation')
            owner = Path(base, 'installation.update', 'owner')
            owner.mkdir(parents=True)
            self.assertFalse(removal.store_created(directory))
            (owner / 'scope.json').write_text('{}')
            self.assertTrue(removal.store_created(directory))
            with patch.object(removal, 'load_layout', side_effect=ValueError('update_copy_root_required')):
                with self.assertRaisesRegex(ValueError, '^update_copy_root_required$'):
                    removal.uninstall(directory, {}, partial=True, emit=lambda _: None)
        with patch.object(removal.os, 'lstat', side_effect=PermissionError(13, 'denied')):
            with self.assertRaises(PermissionError):
                removal.store_created(Path('/opt/leti-cook-test'))

    def test_saved_confirmation_resumes_without_second_prompt(self):
        with patch.object(removal, 'load_layout', return_value=self.layout), \
                patch.object(removal, 'update_lock', lambda _: contextlib.nullcontext()), \
                patch.object(removal.store, 'load_uninstall', return_value=self.op):
            self.assertEqual(removal.uninstall(self.layout.installation, {},
                ask=Mock(side_effect=AssertionError('already confirmed')), emit=lambda _: None), 0)
            self.assertIn('scope', self.events)


class ManagementRemovalTests(unittest.TestCase):
    def test_management_does_not_revive_a_confirmed_removal(self):
        import manage
        for args in (['restart'], ['update'], ['--version']):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as folder:
                Path(folder, 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
                with patch.object(manage, '__file__', str(Path(folder, 'manage.py'))), \
                        patch.object(manage, 'safe_path', Path), patch.object(sys, 'argv', ['manage.py', *args]), \
                        patch.object(removal, 'continue_removal', return_value=True) as resume, \
                        patch('ops.updater.management', side_effect=AssertionError('must not run an update')), \
                        patch.object(manage, '_dispatch', side_effect=AssertionError('must not restart')), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(manage.main(), 0)
                    resume.assert_called_once()


RECEIPT = {'format': 'leti-cook-owner-install', 'project': 'leti-cook-0123456789ab', 'port': 8171,
           'status': 'preparing', 'database_id': '9a2fc4bc-9d4a-4bc4-b4be-69c7d37f2d23',
           'version': '0.1.1', 'source': 'release', 'bundle_sha256': '0' * 64}


class UnfinishedInstallationTests(unittest.TestCase):
    """Without installed.json, uninstall works from installation.json; nothing else runs."""

    def invoke(self, args, *, receipt=RECEIPT, installed=None, update_journal=False, failure=None):
        import manage
        output, error = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as base:
            directory = Path(base, 'installation')
            directory.mkdir()
            if receipt is not None:
                raw = receipt if isinstance(receipt, bytes) else json.dumps(receipt).encode()
                (directory / 'installation.json').write_bytes(raw)
            if installed is not None:
                (directory / 'installed.json').write_text(json.dumps(installed))
            if update_journal:
                owner = Path(base, 'installation.update', 'owner')
                owner.mkdir(parents=True)
                (owner / 'update-journal.json').write_text('{}')
            with patch.object(manage, '__file__', str(directory / 'manage.py')), \
                    patch.object(sys, 'argv', ['manage.py', *args]), \
                    patch.object(sys.stdin, 'isatty', return_value=True), \
                    patch.object(removal, 'uninstall', return_value=0, side_effect=failure) as uninstall, \
                    patch.object(removal, 'continue_removal', return_value=False), \
                    patch('ops.updater.management', side_effect=AssertionError('must not manage')), \
                    patch.object(manage, '_dispatch', side_effect=AssertionError('must not run')), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                status = manage.main()
        return status, output.getvalue(), error.getvalue(), uninstall, directory

    def test_uninstall_removes_it_from_installation_json(self):
        status, _, _, uninstall, directory = self.invoke(['uninstall'])
        self.assertEqual(status, 0)
        uninstall.assert_called_once_with(directory, {'project': RECEIPT['project'], 'port': 8171,
                                                      'database_id': RECEIPT['database_id']}, partial=True)

    def test_other_commands_say_it_did_not_finish_and_how_to_remove_it(self):
        for args in (['restart'], ['setup-code'], ['set-key'], ['update'], ['update', '--check'],
                     ['check-settings'], ['agent-key-list'], ['purge-update-copy'], ['restore-update-copy']):
            with self.subTest(args=args):
                status, output, error, uninstall, directory = self.invoke(args)
                self.assertEqual((status, output), (1, ''))
                self.assertEqual(error, 'This installation did not finish. Remove it with: sudo python3 '
                                 + str(directory / 'manage.py') + ' uninstall. Then install again into a new folder.\n')
                uninstall.assert_not_called()

    def test_damaged_or_foreign_receipt_refuses_removal(self):
        bad = [b'x' * 4097, b'not json', json.dumps([RECEIPT]).encode(),
               dict(RECEIPT, format='other'), dict(RECEIPT, project='leti-cook-other'), dict(RECEIPT, project=None),
               dict(RECEIPT, port=80), dict(RECEIPT, port=True), dict(RECEIPT, port='8171'),
               dict(RECEIPT, database_id='not-an-id'), {k: v for k, v in RECEIPT.items() if k != 'database_id'}]
        for receipt in bad:
            for args in (['uninstall'], ['restart']):
                with self.subTest(receipt=str(receipt)[:60], args=args):
                    status, output, error, uninstall, _ = self.invoke(args, receipt=receipt)
                    self.assertEqual((status, output), (1, ''))
                    self.assertEqual(error, 'Command failed. Run in the installed directory with Docker access.\n')
                    uninstall.assert_not_called()

    def test_updated_installation_without_marker_is_never_unfinished(self):
        for args in (['uninstall'], ['restart']):
            with self.subTest(args=args):
                status, _, error, uninstall, _ = self.invoke(args, update_journal=True)
                self.assertEqual(status, 1)
                self.assertIn('Command failed.', error)
                uninstall.assert_not_called()

    def test_finished_installation_keeps_its_own_path(self):
        installed = {'project': RECEIPT['project'], 'port': 8171, 'database_id': RECEIPT['database_id'],
                     'status': 'owner_setup_ready'}
        status, _, _, uninstall, directory = self.invoke(['uninstall'], installed=installed)
        self.assertEqual(status, 0)
        uninstall.assert_called_once_with(directory, installed)

    def test_folder_without_any_receipt_refuses_removal(self):
        status, _, error, uninstall, _ = self.invoke(['uninstall'], receipt=None)
        self.assertEqual(status, 1)
        self.assertIn('Command failed.', error)
        uninstall.assert_not_called()

    def test_uninstall_during_a_running_install_says_to_wait(self):
        import manage
        status, output, error, _, _ = self.invoke(
            ['uninstall'], failure=removal.store.RemovalError('uninstall_installation_in_progress'))
        self.assertEqual((status, output), (1, ''))
        self.assertEqual(error, 'Command stopped: uninstall_installation_in_progress. '
                         + manage.UPDATE_STOP_HINTS['uninstall_installation_in_progress'] + '\n')
        self.assertIn('installer', error)


class CopyRemovalTests(unittest.TestCase):
    def test_mismatched_copy_operation_is_rejected_before_purge(self):
        layout = SimpleNamespace(key_path=Path('/key'), cipher_path=Path('/cipher'), partial_path=Path('/partial'),
                                 marker_path=Path('/marker'), owner=Path('/owner'))
        with patch.object(removal.os.path, 'lexists', return_value=True), \
                patch.object(removal.copies, '_reservation', return_value=SimpleNamespace(copy_id='a' * 32)):
            with self.assertRaisesRegex(ValueError, 'uninstall_copy_operation_mismatch'):
                removal.inspect_copy(layout, 'b' * 32)

    def test_changed_systemd_unit_is_rejected_before_purge(self):
        layout = SimpleNamespace(key_path=Path('/key'), cipher_path=Path('/cipher'), partial_path=Path('/partial'),
                                 marker_path=Path('/marker'), owner=Path('/owner'), systemd_directory=Path('/systemd'))
        with patch.object(removal.os.path, 'lexists', return_value=True), \
                patch.object(removal.copies, '_reservation', return_value=SimpleNamespace(copy_id='a' * 32)), \
                patch.object(removal.copies, '_unit_data', return_value={'owned.timer': b'expected'}), \
                patch.object(removal.copies, '_read', return_value=b'changed'):
            with self.assertRaisesRegex(ValueError, 'uninstall_copy_unit_changed'):
                removal.inspect_copy(layout, 'a' * 32)


if __name__ == '__main__':
    unittest.main()
