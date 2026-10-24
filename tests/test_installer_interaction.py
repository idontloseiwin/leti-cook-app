"""Fresh installer prompts and upgrade guard, without Docker or file changes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install


class InstallerInteractionTests(unittest.TestCase):
    def choose(self, answers, *, available=lambda port: True, directory=None):
        ask = Mock(side_effect=answers)
        emit = Mock()
        with patch.object(install, 'new_directory', directory or (lambda value: Path(value))), \
                patch.object(install, 'check_update_copy_storage'):
            plan = install.choose_plan(SimpleNamespace(directory=None, port=None),
                                       ask=ask, emit=emit, available=available)
        return plan, ask, emit

    def test_enter_accepts_folder_and_port_with_explicit_hints(self):
        plan, ask, _ = self.choose(['', ''])
        self.assertEqual(plan, {'directory': Path(install.DEFAULT_DIRECTORY), 'port': 8002})
        prompts = [call.args[0] for call in ask.call_args_list]
        self.assertEqual(len(prompts), 2)
        self.assertIn('Installation folder', prompts[0])
        self.assertIn(install.DEFAULT_DIRECTORY, prompts[0])
        self.assertIn('8002', prompts[1])
        for prompt in prompts:
            self.assertIn('Enter to use this, or type another', prompt)

    def test_custom_folder_and_port_are_used(self):
        plan, _, _ = self.choose(['/srv/leti-cook', '8019'])
        self.assertEqual(plan, {'directory': Path('/srv/leti-cook'), 'port': 8019})

    def test_invalid_folder_and_port_still_retry(self):
        directory = Mock(side_effect=[install.InstallError('new_directory_required_no_automatic_overwrite'),
                                      Path('/srv/leti-cook')])
        plan, ask, emit = self.choose(['/existing', '/srv/leti-cook', 'not-a-port', '80', '8019'],
                                     directory=directory)
        self.assertEqual(plan['port'], 8019)
        self.assertEqual(ask.call_count, 5)
        self.assertEqual(emit.call_count, 3)
        self.assertIn('new_directory_required_no_automatic_overwrite', emit.call_args_list[0].args[0])

    def test_busy_port_is_rejected_without_silently_replacing_choice(self):
        plan, ask, emit = self.choose(['', '8003', '8019'], available=lambda port: port != 8003)
        self.assertEqual(plan['port'], 8019)
        self.assertEqual(ask.call_count, 3)
        emit.assert_called_once_with('Invalid choice: port_in_use_choose_another')

    def run_fresh(self, *, terminal=True, key_error=None):
        stdout, stderr = StringIO(), StringIO()
        stdin = Mock()
        stdin.isatty.return_value = terminal
        stdin.readline.side_effect = AssertionError('Unexpected extra confirmation prompt')
        key = 'test_key_' + 'x' * 24
        plan = {'directory': Path('/srv/leti-cook'), 'port': 8019}
        with ExitStack() as stack:
            stack.enter_context(patch.object(install.sys, 'argv', ['install.py', '--install']))
            stack.enter_context(patch.object(install.sys, 'stdin', stdin))
            stack.enter_context(patch.object(install, 'verify_bundle', return_value=({}, {'example': b'code'})))
            docker = stack.enter_context(patch.object(install, 'check_docker', return_value={}))
            stack.enter_context(patch.object(install, 'check_time_sync'))
            stack.enter_context(patch.object(install, 'choose_plan', return_value=plan))
            stack.enter_context(patch.object(install, 'report_resources'))
            stack.enter_context(patch.object(install.os, 'umask'))
            key_prompt = stack.enter_context(patch.object(install.getpass, 'getpass',
                                                          return_value=key, side_effect=key_error))
            create = stack.enter_context(patch.object(install, 'install',
                                                      return_value={'status': 'owner_setup_ready'}))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = install.main()
        return status, stdout.getvalue(), stderr.getvalue(), key_prompt, create, docker, key

    def test_fresh_install_proceeds_from_hidden_key_without_confirmation(self):
        status, stdout, stderr, key_prompt, create, _, key = self.run_fresh()
        self.assertEqual(status, 0)
        key_prompt.assert_called_once_with('YouTube API key (hidden): ')
        self.assertEqual(create.call_args.args[1:], (Path('/srv/leti-cook'), 8019))
        self.assertEqual(create.call_args.kwargs, {'api_key': key})
        self.assertIn('Installation summary', stdout)
        self.assertIn('http://127.0.0.1:8019/auth', stdout)
        self.assertNotIn('Type INSTALL', stdout)
        self.assertNotIn(key, stdout + stderr)

    def test_ctrl_c_at_key_prompt_does_not_create_installation(self):
        status, _, _, _, create, _, _ = self.run_fresh(key_error=KeyboardInterrupt())
        self.assertEqual(status, 130)
        create.assert_not_called()

    def test_fresh_install_still_requires_terminal(self):
        status, _, stderr, key_prompt, create, docker, _ = self.run_fresh(terminal=False)
        self.assertEqual(status, 1)
        self.assertIn('interactive_terminal_required_for_setup_code', stderr)
        key_prompt.assert_not_called()
        create.assert_not_called()
        docker.assert_not_called()

    def test_hidden_key_input_does_not_fall_back_to_echoing(self):
        with patch.object(install.sys.stdin, 'isatty', return_value=True), \
                patch.object(install.getpass, 'getpass', side_effect=install.getpass.GetPassWarning()):
            with self.assertRaisesRegex(install.InstallError, '^hidden_key_input_unavailable$'):
                install.hidden_api_key()


if __name__ == '__main__':
    unittest.main()
