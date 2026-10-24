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
