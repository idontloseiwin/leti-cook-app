"""The installer refuses a host whose clock is not kept synchronised. No Docker, network or file changes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install  # noqa: E402

QUESTION = ['timedatectl', 'show', '-p', 'NTPSynchronized', '--value']


def answering(value=None, error=None):
    calls = []

    def run(args, **options):
        calls.append((list(args), options))
        if error is not None:
            raise error
        return value
    return run, calls


class CheckTimeSyncTests(unittest.TestCase):
    def test_a_synchronised_clock_passes(self):
        run, calls = answering('yes\n')
        install.check_time_sync(run)
        self.assertEqual(calls, [(QUESTION, {'timeout': 15})])

    def test_an_unsynchronised_clock_is_refused(self):
        run, _ = answering('no\n')
        with self.assertRaisesRegex(install.InstallError, '^clock_not_synchronized$'):
            install.check_time_sync(run)

    def test_an_unknown_answer_is_refused(self):
        for value in ('', 'maybe', 'yes yes', 'YES', None):
            with self.subTest(value=value):
                run, _ = answering(value)
                with self.assertRaisesRegex(install.InstallError, '^clock_sync_unverifiable$'):
                    install.check_time_sync(run)

    def test_a_missing_or_failing_timedatectl_is_refused(self):
        for error in (FileNotFoundError('timedatectl'), PermissionError(), install.InstallError('command_failed_1'),
                      subprocess.TimeoutExpired(QUESTION, 15)):
            with self.subTest(error=error):
                run, _ = answering(error=error)
                with self.assertRaisesRegex(install.InstallError, '^clock_sync_unverifiable$'):
                    install.check_time_sync(run)

    def test_each_refusal_says_how_to_fix_it(self):
        self.assertIn('sudo timedatectl set-ntp true', install.REASON_HINTS['clock_not_synchronized'])
        self.assertIn('timedatectl show -p NTPSynchronized --value', install.REASON_HINTS['clock_sync_unverifiable'])
        for reason in install.REASON_HINTS:
            self.assertRegex(reason, r'^[a-z0-9_]+$')


class InstallerStopsTests(unittest.TestCase):
    def run_main(self, clock_error):
        stdout, stderr = StringIO(), StringIO()
        stdin = Mock()
        stdin.isatty.return_value = True
        with ExitStack() as stack:
            stack.enter_context(patch.object(install.sys, 'argv', ['install.py', '--install']))
            stack.enter_context(patch.object(install.sys, 'stdin', stdin))
            stack.enter_context(patch.object(install, 'verify_bundle', return_value=({}, {'example': b'code'})))
            stack.enter_context(patch.object(install, 'check_docker', return_value={}))
            stack.enter_context(patch.object(install, 'check_time_sync', side_effect=clock_error))
            plan = stack.enter_context(patch.object(install, 'choose_plan', return_value={'directory': '/srv/lic',
                                                                                         'port': 8019}))
            stack.enter_context(patch.object(install, 'print_plan'))
            stack.enter_context(patch.object(install.os, 'umask'))
            key = stack.enter_context(patch.object(install.getpass, 'getpass', return_value='test_key_' + 'x' * 24))
            create = stack.enter_context(patch.object(install, 'install', return_value={'status': 'owner_setup_ready'}))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            status = install.main()
        return status, stdout.getvalue(), stderr.getvalue(), (plan, key, create)

    def test_main_stops_before_the_folder_port_and_key_questions(self):
        for reason, fix in (('clock_not_synchronized', 'sudo timedatectl set-ntp true'),
                            ('clock_sync_unverifiable', 'systemd')):
            with self.subTest(reason=reason):
                status, stdout, stderr, untouched = self.run_main(install.InstallError(reason))
                self.assertEqual(status, 1)
                self.assertIn('reason=' + reason + '\n', stderr)
                self.assertIn(fix, stderr)
                self.assertIn('Docker checks passed.', stdout)
                for mock in untouched:
                    mock.assert_not_called()

    def test_main_goes_on_when_the_clock_is_synchronised(self):
        status, stdout, _, (plan, key, create) = self.run_main(None)
        self.assertEqual(status, 0)
        self.assertIn('The system clock is synchronised.', stdout)
        for mock in (plan, key, create):
            mock.assert_called_once()

    def test_other_refusals_print_no_clock_hint(self):
        _, _, stderr, _ = self.run_main(install.InstallError('compose_2_24_4_required'))
        self.assertNotIn('timedatectl', stderr)

    def test_install_refuses_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'new-installation'
            run, calls = answering('no\n')
            with patch.object(install.sys, 'platform', 'linux'), \
                    patch.object(install, 'verify_bundle', return_value=({}, {})), \
                    patch.object(install, 'check_docker', return_value={}) as docker, \
                    patch.object(install, 'new_directory') as new_directory, \
                    patch.object(install, 'port_available') as port:
                with self.assertRaisesRegex(install.InstallError, '^clock_not_synchronized$'):
                    install.install(Path('/verified-release'), directory, 8019, run=run)
            docker.assert_called_once_with(run)
            self.assertEqual([args for args, _ in calls], [QUESTION])
            new_directory.assert_not_called()
            port.assert_not_called()
            self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
