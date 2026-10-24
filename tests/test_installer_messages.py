"""Release-readiness CLI diagnostics with local fakes only; no Docker/provider I/O."""
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'application'), str(ROOT)]
import install
import manage
from ops import release_install, self_hosted_owner, updater
from db.self_hosted_auth import AuthStoreError, OwnerAuthRepository


class DockerDiagnosticTests(unittest.TestCase):
    def test_exhausted_pool_has_fixed_reason_without_raw_output_or_extra_commands(self):
        secret = 'private-command-output-do-not-print'
        result = subprocess.CompletedProcess(['docker'], 1, secret,
            'Error response from daemon: all predefined address pools have been fully subnetted\n' + secret)
        output, error = StringIO(), StringIO()
        with patch.object(install.subprocess, 'run', return_value=result) as run, \
                redirect_stdout(output), redirect_stderr(error):
            with self.assertRaisesRegex(install.InstallError, '^docker_network_address_pool_exhausted$'):
                install.command(['docker', '--host', 'unix:///run/docker.sock', 'compose', 'up', '-d'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(output.getvalue() + error.getvalue(), '')
        hint = install.REASON_HINTS['docker_network_address_pool_exhausted']
        self.assertIn('Review unused Docker networks', hint)
        self.assertIn('does not remove networks automatically', hint)
        self.assertNotIn(secret, hint)

    def test_other_failures_are_generic_and_success_is_not_reclassified(self):
        phrase = 'all predefined address pools have been fully subnetted'
        for arguments, stderr in ((['docker', 'info'], 'private failure'), (['other'], phrase)):
            with self.subTest(arguments=arguments), patch.object(install.subprocess, 'run',
                    return_value=subprocess.CompletedProcess(arguments, 2, 'private', stderr)):
                with self.assertRaisesRegex(install.InstallError, '^command_failed_2$'):
                    install.command(arguments)
        with patch.object(install.subprocess, 'run',
                return_value=subprocess.CompletedProcess(['docker'], 0, 'ok', phrase)):
            self.assertEqual(install.command(['docker', 'info']), 'ok')


class InstallerPathOrderTests(unittest.TestCase):
    def test_interactive_folder_storage_refusal_reprompts_with_existing_hint(self):
        ask = Mock(side_effect=['/tmp/new', '/opt/new', ''])
        lines = []
        with patch.object(install, 'new_directory', side_effect=Path), \
                patch.object(install, 'check_update_copy_storage',
                    side_effect=[install.InstallError('update_copy_storage_unavailable'), None]) as storage:
            plan = install.choose_plan(SimpleNamespace(directory=None, port=None),
                                       ask=ask, emit=lines.append, available=lambda port: True)
        self.assertEqual(plan['directory'], Path('/opt/new'))
        self.assertEqual(storage.call_count, 2)
        self.assertIn('Invalid choice: update_copy_storage_unavailable', lines)
        self.assertIn(install.REASON_HINTS['update_copy_storage_unavailable'], lines)
        self.assertEqual(ask.call_count, 3)

    def entry(self, signed, storage_error=None):
        order = []
        stdout, stderr = StringIO(), StringIO()
        def validate_storage(directory):
            order.append('storage')
            if storage_error is not None:
                raise storage_error
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(patch.object(install, 'check_docker', return_value={}))
            stack.enter_context(patch.object(install, 'check_time_sync'))
            stack.enter_context(patch.object(install, 'new_directory', side_effect=Path))
            storage = stack.enter_context(patch('ops.update_copy_store.validate_new_layout',
                side_effect=validate_storage))
            stack.enter_context(patch.object(install, 'print_plan'))
            stack.enter_context(patch.object(install.os, 'umask'))
            key = stack.enter_context(patch.object(install, 'hidden_api_key',
                side_effect=lambda: order.append('key') or 'synthetic_key_123456789012345'))
            # Folder checks are exercised here; separate tests own port selection.
            stack.enter_context(patch.object(install, 'available_port', return_value=8002))
            stack.enter_context(patch.object(install, 'ask_value', return_value=8002))
            stack.enter_context(redirect_stdout(stdout))
            stack.enter_context(redirect_stderr(stderr))
            if signed:
                verified = SimpleNamespace(manifest={'version': '1.0.0'}, files={})
                stack.enter_context(patch.object(release_install, 'load_source', return_value=object()))
                stack.enter_context(patch.object(release_install, 'verify_release', return_value=verified))
                create = stack.enter_context(patch.object(release_install, 'install_verified_release', return_value={}))
                status = release_install.main(['--package', 'unused', '--directory', '/tmp/new'])
            else:
                stack.enter_context(patch.object(sys, 'argv', ['install.py', '--install', '--directory', '/tmp/new']))
                stack.enter_context(patch.object(install, 'verify_bundle', return_value=({}, {})))
                create = stack.enter_context(patch.object(install, 'install', return_value={}))
                status = install.main()
        return status, order, stderr.getvalue(), key, create, storage

    def test_both_entrypoints_refuse_private_path_error_before_key_or_installation(self):
        for signed in (False, True):
            with self.subTest(signed=signed):
                status, order, error, key, create, storage = self.entry(signed, ValueError('update_copy_parent_untrusted'))
                self.assertEqual((status, order), (1, ['storage']))
                self.assertIn('reason=update_copy_storage_unavailable', error)
                self.assertIn('root-owned directory', error)
                key.assert_not_called()
                create.assert_not_called()
                storage.assert_called_once_with(Path('/tmp/new'))

    def test_both_entrypoints_validate_storage_before_key_on_valid_path(self):
        for signed in (False, True):
            with self.subTest(signed=signed):
                status, order, _, key, create, _ = self.entry(signed)
                self.assertEqual((status, order), (0, ['storage', 'key']))
                key.assert_called_once()
                create.assert_called_once()


class SetupCodeMessageTests(unittest.TestCase):
    def test_completed_password_and_local_modes_issue_no_code_or_access(self):
        for mode in ('password', 'local'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'owner.sqlite3'
                repository = OwnerAuthRepository.initialize(path, 'dummy', 1800000000)
                with repository.transaction() as database:
                    database.execute('UPDATE owner SET access_mode=? WHERE singleton=1', (mode,))
                    repository._session(database, 'kept-session', 1800000000)
                    before = tuple(database.execute('SELECT * FROM owner').fetchone())
                    sessions = [tuple(row) for row in database.execute('SELECT * FROM sessions')]
                terminal = StringIO()
                terminal.isatty = lambda: True
                terminal.close = lambda: None
                output, error = StringIO(), StringIO()
                with patch.object(sys, 'argv', ['owner.py', '--database', str(path), 'setup-code']), \
                        patch.object(sys.stdin, 'isatty', return_value=True), \
                        patch.object(self_hosted_owner, 'open', return_value=terminal, create=True), \
                        redirect_stdout(output), redirect_stderr(error):
                    self.assertEqual(self_hosted_owner.main(), 1)
                self.assertIn('Access setup is already complete.', error.getvalue())
                self.assertIn('No new setup code was issued.', error.getvalue())
                self.assertEqual(output.getvalue() + terminal.getvalue(), '')
                with repository.transaction() as database:
                    self.assertEqual(tuple(database.execute('SELECT * FROM owner').fetchone()), before)
                    self.assertEqual([tuple(row) for row in database.execute('SELECT * FROM sessions')], sessions)

    def test_other_owner_errors_remain_generic(self):
        error = StringIO()
        with patch.object(sys, 'argv', ['owner.py', '--database', 'unused', 'setup-code']), \
                patch.object(self_hosted_owner, 'OwnerAuthRepository', side_effect=AuthStoreError('private-error')), \
                redirect_stderr(error):
            self.assertEqual(self_hosted_owner.main(), 1)
        self.assertIn('Owner operation refused', error.getvalue())
        self.assertNotIn('private-error', error.getvalue())


class UpdateCheckMessageTests(unittest.TestCase):
    def invoke(self, failure, *, recovery=False, check=True):
        output, error = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            Path(directory, 'installed.json').write_text(json.dumps({'project': 'leti-cook-0123456789ab'}))
            stack.enter_context(patch.object(manage, '__file__', str(Path(directory, 'manage.py'))))
            stack.enter_context(patch('ops.uninstall.continue_removal', return_value=False))
            if recovery:
                stack.enter_context(patch.object(updater, 'layout_for', return_value=object()))
                stack.enter_context(patch.object(updater.journal, 'update_lock', side_effect=lambda layout: nullcontext()))
                stack.enter_context(patch.object(updater.journal, 'load_journal', return_value={'phase': 'prepared'}))
                stack.enter_context(patch.object(updater, 'Host'))
                engine = stack.enter_context(patch.object(updater, 'UpdateEngine'))
                engine.return_value.recover.side_effect = ValueError(failure)
            else:
                stack.enter_context(patch.object(updater, 'management', side_effect=lambda *args: nullcontext((None, False))))
            update = stack.enter_context(patch.object(updater, 'update', side_effect=ValueError(failure)))
            stack.enter_context(patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(patch.object(sys, 'argv', ['manage.py', 'update'] + (['--check'] if check else [])))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(error))
            status = manage.main()
        if recovery:
            self.assertIn('An interrupted update was found. Recovering it first.', output.getvalue())
        return status, error.getvalue(), update

    def test_missing_release_and_old_candidate_check_do_not_claim_a_journal(self):
        for code in ('release_not_found', 'update_version_not_newer'):
            with self.subTest(code=code):
                status, error, update = self.invoke(code)
                self.assertEqual(status, 1)
                self.assertIn('Command stopped: ' + code, error)
                self.assertIn('No update was started.', error)
                self.assertNotIn('journal', error)
                self.assertNotIn('recovery', error)
                update.assert_called_once_with(None, None, check=True, pre=False, build_local=False)

    def test_actual_recovery_failure_still_warns_and_does_not_run_check(self):
        status, error, update = self.invoke('update_recovery_unavailable', recovery=True)
        self.assertEqual(status, 1)
        self.assertIn('journal records any recovery still needed', error)
        self.assertNotIn('No update was started', error)
        update.assert_not_called()

    def test_started_update_and_non_domain_errors_keep_existing_messages(self):
        _, error, _ = self.invoke('update_failed', check=False)
        self.assertIn('journal records any recovery still needed', error)
        _, error, _ = self.invoke('private-secret-error')
        self.assertNotIn('private-secret-error', error)
        self.assertIn('Command failed.', error)


if __name__ == '__main__':
    unittest.main()
