"""manage.py needs a terminal only where it asks or shows a secret. No Docker."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import manage  # noqa: E402


def run(action, *arguments, terminal=False, failure=None, docker=lambda **options: None):
    calls, out, err = [], io.StringIO(), io.StringIO()

    def command(args, **kw):
        calls.append(args[2:])
        if failure is not None:
            raise manage.InstallError(failure)
        return ''
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
        with patch.object(manage, '__file__', str(Path(directory, 'manage.py'))), \
                patch.object(manage, 'safe_path', Path), patch.object(manage, 'check_docker', docker), \
                patch.object(manage, 'compose_prefix', lambda *a: ['docker', 'compose']), \
                patch.object(manage, 'command', command), \
                patch('ops.updater.management', lambda *a: contextlib.nullcontext((None, False))), \
                patch.object(manage.subprocess, 'run', lambda args, **kw: types.SimpleNamespace(returncode=0, stdout='')), \
                patch.object(sys, 'argv', ['manage.py', action, *arguments]), \
                patch.object(sys.stdin, 'isatty', lambda: terminal), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = manage.main()
    return code, calls, out.getvalue(), err.getvalue()


class TerminalTests(unittest.TestCase):
    def test_restart_runs_from_a_script(self):
        code, calls, out, err = run('restart')
        self.assertEqual(code, 0, err)
        self.assertIn(['restart', 'backend'], calls)
        self.assertIn('Application restarted.', out)

    def test_commands_that_ask_or_show_a_secret_say_they_need_a_terminal(self):
        for action in ('setup-code', 'set-key', 'redact-secrets', 'uninstall'):
            code, calls, out, err = run(action)
            self.assertEqual((code, calls), (1, []), action)
            self.assertEqual(err, f'{action} needs an interactive terminal. Run it in a terminal; over SSH, use ssh -t.\n')


GENERIC = 'Command failed. Run in the installed directory with Docker access.\n'


class HelperRefusalTests(unittest.TestCase):
    """A helper's fixed refusal is named, never the generic Docker hint."""

    def test_agent_keys_before_access_setup_name_the_reason(self):
        refused = 'command_failed_' + str(manage.HELPER_REFUSED_STATUS)
        for action, arguments, terminal in (
                ('agent-key-list', (), False),
                ('agent-key-revoke', ('--key-id', '00000000-0000-4000-8000-000000000000'), False),
                ('agent-key-create', ('--name', 'Research'), True)):
            with self.subTest(action=action):
                code, calls, out, err = run(action, *arguments, terminal=terminal, failure=refused)
                self.assertEqual((code, len(calls), out), (1, 1, ''))
                self.assertEqual(err, 'Command stopped: owner_access_not_configured. Agent keys need finished '
                                      'access setup: open /auth, enter the setup code and choose account or local '
                                      'access, then run the command again.\n')

    def test_setup_code_after_access_setup_names_the_reason(self):
        refused = 'command_failed_' + str(manage.HELPER_REFUSED_STATUS)
        code, calls, out, err = run('setup-code', terminal=True, failure=refused)
        self.assertEqual((code, len(calls), out, err), (1, 1, '', 'Command stopped: owner_already_created.\n'))

    def test_other_helper_and_docker_failures_stay_generic(self):
        for failure in ('command_failed_1', 'command_failed_125', 'docker_network_address_pool_exhausted'):
            with self.subTest(failure=failure):
                code, _, _, err = run('agent-key-list', failure=failure)
                self.assertEqual((code, err), (1, GENERIC))
        # Only the owner helpers' status is translated, not another command's.
        code, _, _, err = run('clear-sign-in-limit', failure='command_failed_' + str(manage.HELPER_REFUSED_STATUS))
        self.assertEqual((code, err), (1, GENERIC))


def engine(version):
    """manage's own Docker check against a local Docker whose Engine reports this version."""
    answers = {('docker', 'compose', 'version', '--short'): '2.24.4\n',
               ('docker', 'info', '--format', '{{json .}}'): json.dumps({'OSType': 'linux', 'ServerVersion': version})}
    real = manage.check_docker

    def check(*args, **options):
        assert not args, args
        with patch.object(sys, 'platform', 'linux'), \
                patch.dict(os.environ, {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': ''}):
            return real(lambda command, **kw: answers[tuple(command)], **options)
    return check


class DockerEngineTests(unittest.TestCase):
    """An old Docker Engine refuses only install and update; other commands warn and run."""
    WARNING = ('Warning: Docker Engine 27.5.1 is installed; Leti Cook needs Docker Engine 28.3.3 or newer. Older '
               'Engines let other computers on the local network reach ports published on 127.0.0.1, so they could '
               "reach Leti Cook. Install Docker Engine 28.3.3 or newer from Docker's repository: "
               'https://docs.docker.com/engine/install/ Updates are refused until then.\n')

    def test_other_commands_warn_once_and_still_run(self):
        code, calls, out, err = run('restart', docker=engine('27.5.1'))
        self.assertEqual((code, err), (0, self.WARNING))
        self.assertIn(['restart', 'backend'], calls)
        self.assertIn('Application restarted.', out)
        code, calls, out, err = run('clear-sign-in-limit', docker=engine('27.5.1'))
        self.assertEqual((code, len(calls), err), (0, 1, self.WARNING))
        self.assertIn('Sign-in limit cleared.', out)

    def test_current_engine_prints_nothing(self):
        code, _, _, err = run('restart', docker=engine('29.6.1'))
        self.assertEqual((code, err), (0, ''))

    def test_update_on_an_old_engine_names_the_reason_and_starts_nothing(self):
        refuse = engine('27.5.1')
        for arguments in ((), ('--check',)):
            with self.subTest(arguments=arguments), \
                    patch('ops.updater.update', side_effect=lambda *a, **k: refuse(engine='require')) as update:
                code, calls, out, err = run('update', *arguments, docker=engine('29.6.1'))
                update.assert_called_once()
                self.assertEqual((code, calls), (1, []))
                lines = err.splitlines()
                self.assertEqual(len(lines), 2, err)
                self.assertTrue(lines[0].startswith('Docker Engine 27.5.1 is installed; Leti Cook needs Docker '
                                                    'Engine 28.3.3 or newer.'), err)
                self.assertEqual(lines[1], 'Command stopped: docker_engine_28_3_3_required. '
                                           "Install Docker Engine 28.3.3 or newer from Docker's repository: "
                                           'https://docs.docker.com/engine/install/ Then run the update again. '
                                           'No update was started.')


if __name__ == '__main__':
    unittest.main()
